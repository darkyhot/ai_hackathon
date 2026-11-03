"""Настоящий звонок продавцу через Voximplant.

Звонит облако Voximplant, а не браузер:
  браузер (Web SDK) входит пользователем приложения и «звонит» в сценарий VoxEngine (backend/voximplant/scenario.js);
  сценарий набирает продавца (callPSTN), произносит реплики агента (синтез речи Voximplant) и распознаёт
  ответы продавца (ASR Voximplant), а браузеру присылает только текст.

Сервер здесь:
- сам настраивает приложение Voximplant: приложение, сценарий, правило, пользователь (setup, идемпотентно);
- выдаёт браузеру имя пользователя и номер, с которого звонить, — без пароля;
- считает хеш для входа по одноразовому ключу (loginWithOneTimeKey): пароль пользователя не покидает сервер.

Доступ к API — VOXIMPLANT_ACCOUNT_ID + VOXIMPLANT_API_KEY или VOXIMPLANT_CREDENTIALS (JSON сервисного аккаунта).

Проверка и настройка:  .venv/bin/python -m backend.phone          (проверить доступ, номер, приложение)
                       .venv/bin/python -m backend.phone setup    (создать/обновить приложение и сценарий)
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
from pathlib import Path
from typing import Any

import httpx

from .config import settings

SCENARIO_FILE = Path(__file__).resolve().parent / "voximplant" / "scenario.js"
SCENARIO_NAME = "domclick_seller_call"
RULE_NAME = "seller_call"
USER_NAME = "agent"

_setup: dict | None = None
_setup_lock = asyncio.Lock()


class PhoneError(RuntimeError):
    """Voximplant не настроен или не ответил."""


def configured() -> bool:
    return bool((settings.voximplant_account_id and settings.voximplant_api_key) or settings.voximplant_credentials)


# ------------------------------------------------------------------ HTTP API

def _service_key() -> dict:
    try:
        key = json.loads(Path(settings.voximplant_credentials).expanduser().read_text())
    except (OSError, ValueError) as exc:
        raise PhoneError(f"Не прочитать VOXIMPLANT_CREDENTIALS ({settings.voximplant_credentials}): {exc}") from exc
    for field in ("account_id", "key_id", "private_key"):
        if not key.get(field):
            raise PhoneError(f"В файле сервисного аккаунта Voximplant нет поля {field}")
    return key


def _auth() -> tuple[dict, dict]:
    """(поля формы, заголовки) для запроса к Management API."""
    if settings.voximplant_account_id and settings.voximplant_api_key:
        return {"account_id": settings.voximplant_account_id, "api_key": settings.voximplant_api_key}, {}
    if settings.voximplant_credentials:
        import jwt  # pyjwt[crypto]: подпись RS256 ключом сервисного аккаунта

        key = _service_key()
        now = int(time.time())
        # iss — число, как в официальном клиенте Voximplant; jwt.encode требует строку, поэтому подписываем JWS напрямую
        payload = json.dumps({"iss": key["account_id"], "iat": now, "exp": now + 60}).encode()
        token = jwt.api_jws.encode(payload, key["private_key"], algorithm="RS256", headers={"kid": key["key_id"]})
        return {}, {"Authorization": f"Bearer {token}"}
    raise PhoneError("Voximplant не настроен: в .env нужны VOXIMPLANT_ACCOUNT_ID и VOXIMPLANT_API_KEY "
                     "(или VOXIMPLANT_CREDENTIALS)")


async def api(cmd: str, **params: Any) -> dict:
    """Вызов Management API: POST /platform_api/<cmd>. Ошибка Voximplant — PhoneError с его текстом."""
    form, headers = _auth()
    for k, v in params.items():
        if v is None:
            continue
        form[k] = ("true" if v else "false") if isinstance(v, bool) else str(v)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(f"https://{settings.voximplant_api_host}/platform_api/{cmd}/", data=form, headers=headers)
    except httpx.HTTPError as exc:  # сеть до api.voximplant.com — понятная ошибка, а не 500
        raise PhoneError(f"Нет связи с Voximplant ({type(exc).__name__}). Проверьте интернет и попробуйте ещё раз.") from exc
    try:
        data = r.json()
    except ValueError:
        raise PhoneError(f"Voximplant {cmd}: HTTP {r.status_code} {r.text[:200]}") from None
    if r.status_code != 200 or "error" in data:
        err = data.get("error") or {}
        msg = err.get("msg") if isinstance(err, dict) else str(err)
        code = err.get("code") if isinstance(err, dict) else None
        if code in (100, 101, 103) or r.status_code == 401:
            msg = f"Voximplant не принял ключ доступа ({msg}). Проверьте VOXIMPLANT_ACCOUNT_ID и VOXIMPLANT_API_KEY."
        raise PhoneError(f"Voximplant {cmd}: {msg or r.text[:200]}")
    return data


# ------------------------------------------------------------------ настройка приложения

def _user_password() -> str:
    """Пароль пользователя приложения: выводится из ключа доступа, нигде не хранится и браузеру не уходит."""
    secret = settings.voximplant_api_key or settings.voximplant_credentials and _service_key()["private_key"] or ""
    digest = hmac.new(secret.encode(), b"domclick-agent/voximplant-user", hashlib.sha256).hexdigest()
    return f"Vx{digest[:22]}!9"  # требования Voximplant: буквы разного регистра, цифры, не короче 8


def _digits(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def _verified(c: dict) -> bool:
    """Caller ID подтверждён: у подтверждённого есть дата verified_until, у неподтверждённого — попытки ввода кода."""
    if "verified_until" in c or "verified" in c:
        return bool(c.get("verified_until") or c.get("verified"))
    return c.get("code_entering_attempts_left") is None


async def callerid_status(number: str) -> str:
    """Можно ли звонить с этого номера: арендован, подтверждён (до какой даты) или нет — для проверки настроек."""
    if number == "default":
        return ("тестовый режим (callerid «default»): звонить можно только на личный номер, подтверждённый "
                "в кабинете для тестовых звонков")
    digits = _digits(number)
    for n in (await api("GetPhoneNumbers", count=50)).get("result") or []:
        if _digits(n.get("phone_number", "")) == digits:
            return "арендован в Voximplant"
    for c in (await api("GetCallerIDs", count=50)).get("result") or []:
        if _digits(c.get("callerid_number", "")) == digits:
            if not c.get("active", True):
                return "НЕ АКТИВЕН — включите его в кабинете (Настройки → Caller ID)"
            if _verified(c):
                return f"подтверждён до {c.get('verified_until')}" if c.get("verified_until") else "подтверждён"
            return ("НЕ ПОДТВЕРЖДЁН — в кабинете (Настройки → Caller ID) запросите звонок с кодом и введите код"
                    f" (попыток ввода кода: {c.get('code_entering_attempts_left', '?')})")
    return "НЕ НАЙДЕН в аккаунте — добавьте и подтвердите его (Настройки → Caller ID) или арендуйте номер"


async def _caller_id() -> str:
    """Номер, с которого звонить: из .env, иначе первый арендованный, иначе первый подтверждённый Caller ID."""
    if settings.voximplant_caller_id:
        if settings.voximplant_caller_id.strip().lower() == "default":
            return "default"  # тестовый режим российской площадки: звонки только на подтверждённый личный номер
        return _digits(settings.voximplant_caller_id)
    numbers = (await api("GetPhoneNumbers", count=20)).get("result") or []
    for n in numbers:
        if n.get("phone_number") and not n.get("deactivated") and not n.get("canceled"):
            return _digits(n["phone_number"])
    callers = (await api("GetCallerIDs", active=True, count=20)).get("result") or []
    for c in callers:
        if c.get("callerid_number") and _verified(c):
            return _digits(c["callerid_number"])
    return ""


def _node(app_full: str) -> str:
    """Узел аккаунта для Web SDK: из .env или из домена приложения «name.account.n4.voximplant.com» -> NODE_4.
    Российские аккаунты (voximplant.ru) живут на своих узлах, и без узла Web SDK не войдёт."""
    if settings.voximplant_node:
        n = re.sub(r"\D", "", settings.voximplant_node)
        return f"NODE_{n}" if n else ""
    m = re.search(r"\.n(\d+)\.voximplant\.", app_full) or re.match(r"api-node(\d+)\.", settings.voximplant_api_host)
    return f"NODE_{m.group(1)}" if m else ""


async def _find_app(name: str) -> dict | None:
    """Приложение по короткому имени: API отдаёт полное «name.account.n2.voximplant.com»."""
    apps = (await api("GetApplications", count=100)).get("result") or []
    return next((a for a in apps if a.get("application_name", "").split(".")[0] == name), None)


async def setup(force: bool = False) -> dict:
    """Создаёт или обновляет приложение, сценарий, правило и пользователя. Повторный вызов ничего не дублирует.

    -> {"username": "agent@app.account.n2.voximplant.com", "app": …, "callerid": "7…", "voice": …, "asr": …}
    """
    global _setup
    async with _setup_lock:
        if _setup and not force:
            return _setup
        app_name = settings.voximplant_app
        app = await _find_app(app_name)
        if not app:
            await api("AddApplication", application_name=app_name)
            app = await _find_app(app_name)
            if not app:
                raise PhoneError(f"Voximplant не создал приложение {app_name}")
        app_id, app_full = app["application_id"], app["application_name"]

        script = SCENARIO_FILE.read_text(encoding="utf-8")
        scenarios = (await api("GetScenarios", scenario_name=SCENARIO_NAME, application_id=app_id)).get("result") or []
        if scenarios:
            scenario_id = scenarios[0]["scenario_id"]
            await api("SetScenarioInfo", scenario_id=scenario_id, scenario_script=script)
        else:
            added = await api("AddScenario", scenario_name=SCENARIO_NAME, scenario_script=script, application_id=app_id)
            scenario_id = added.get("scenario_id")
            if not scenario_id:  # старые версии API не возвращают id
                scenario_id = (await api("GetScenarios", scenario_name=SCENARIO_NAME, application_id=app_id))["result"][0]["scenario_id"]

        rules = (await api("GetRules", application_id=app_id, rule_name=RULE_NAME)).get("result") or []
        if not rules:
            await api("AddRule", application_id=app_id, rule_name=RULE_NAME, rule_pattern=".*", scenario_id=scenario_id)

        password = _user_password()
        users = (await api("GetUsers", application_id=app_id, user_name=USER_NAME)).get("result") or []
        if users:
            await api("SetUserInfo", user_id=users[0]["user_id"], user_password=password, user_active=True)
        else:
            await api("AddUser", application_id=app_id, user_name=USER_NAME, user_display_name="Агент сделки",
                      user_password=password)

        _setup = {
            "username": f"{USER_NAME}@{app_full}",
            "app": app_full,
            "node": _node(app_full),
            "callerid": await _caller_id(),
            "voice": settings.voximplant_voice,
            "asr": settings.voximplant_asr,
        }
        return _setup


async def config() -> dict:
    """Что нужно браузеру для звонка (без пароля)."""
    if not configured():
        raise PhoneError("Voximplant не настроен: нет VOXIMPLANT_ACCOUNT_ID / VOXIMPLANT_API_KEY в .env")
    cfg = await setup()
    if not cfg["callerid"]:
        raise PhoneError("Нет номера, с которого звонить: арендуйте номер в Voximplant (Номера → Купить) "
                         "или подтвердите свой (Настройки → Caller ID) и укажите его в VOXIMPLANT_CALLER_ID.")
    return cfg


def login_hash(one_time_key: str) -> str:
    """Хеш для Client.loginWithOneTimeKey: MD5(key + "|" + MD5(user + ":voximplant.com:" + password))."""
    if not one_time_key or len(one_time_key) > 200:
        raise PhoneError("Некорректный одноразовый ключ")
    inner = hashlib.md5(f"{USER_NAME}:voximplant.com:{_user_password()}".encode()).hexdigest()
    return hashlib.md5(f"{one_time_key}|{inner}".encode()).hexdigest()


def status() -> dict:
    return {"configured": configured(), "provider": "voximplant", "seller_phone": settings.seller_phone,
            "pin": bool(settings.phone_pin)}


def allowed(pin: str | None) -> bool:
    """Звонок — только с PIN, если он задан (PHONE_PIN в .env)."""
    return not settings.phone_pin or secrets.compare_digest(pin or "", settings.phone_pin)


if __name__ == "__main__":
    import sys

    async def check():
        if not configured():
            print("Нет VOXIMPLANT_ACCOUNT_ID и VOXIMPLANT_API_KEY (или VOXIMPLANT_CREDENTIALS) в .env")
            return 1
        info = await api("GetAccountInfo", return_live_balance=True)
        acc = info.get("result") or {}
        print(f"Аккаунт: {acc.get('account_name')} (id {acc.get('account_id')}), "
              f"баланс {acc.get('live_balance', acc.get('balance'))} {acc.get('currency', '')}")
        cfg = await setup(force=True)
        print(f"Приложение: {cfg['app']} (узел: {cfg['node'] or 'по умолчанию'})")
        if not cfg["node"]:
            print("  Если на главной кабинета в блоке «Данные для API, SDK, SIP» указан узел (NODE_…), "
                  "впишите его в VOXIMPLANT_NODE. Если узла там нет — ничего делать не нужно.")
        print(f"Пользователь для звонков: {cfg['username']}")
        print(f"Сценарий {SCENARIO_NAME} загружен, правило {RULE_NAME} → сценарий")
        print(f"Голос: {cfg['voice']}, распознавание: {cfg['asr']}")
        print(f"Номер для звонков: {cfg['callerid'] or 'НЕТ — арендуйте номер или укажите VOXIMPLANT_CALLER_ID'}")
        if cfg["callerid"]:
            print(f"  Статус номера: {await callerid_status(cfg['callerid'])}")
            if cfg["callerid"] != "default":
                print("  Звоните на другой телефон: звонок с номера на этот же номер оператор отклонит (403 Forbidden).")
        return 0 if cfg["callerid"] else 2

    try:
        sys.exit(asyncio.run(check()))
    except PhoneError as exc:
        print("Ошибка:", exc)
        sys.exit(1)
