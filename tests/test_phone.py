"""Звонок через Voximplant, серверная часть (backend/phone.py, /api/phone).

Облако Voximplant подменено поддельным Management API (httpx.MockTransport): названия методов и полей —
как в официальном клиенте @voximplant/apiclient-nodejs. Проверяем автонастройку приложения (без дублей),
оба способа доступа к API, хеш входа по одноразовому ключу, понятные ошибки и то, что пароль не уходит в браузер.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
from fastapi.testclient import TestClient

from backend import config as C
from backend import phone, ratelimit


class FakeVoximplant:
    """Состояние аккаунта Voximplant и обработчик запросов /platform_api/<Метод>/."""

    def __init__(self, numbers=("74951234567",), callerids=()):
        self.apps: list[dict] = []
        self.scenarios: list[dict] = []
        self.rules: list[dict] = []
        self.users: list[dict] = []
        self.numbers = [{"phone_id": i + 1, "phone_number": n, "deactivated": False, "canceled": False}
                        for i, n in enumerate(numbers)]
        self.callerids = [{"callerid_id": 1, "callerid_number": n, "active": True, "verified": True} for n in callerids]
        self.requests: list[tuple[str, dict, dict]] = []
        self.error: dict | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.hosts = getattr(self, "hosts", []) + [request.url.host]
        cmd = request.url.path.rstrip("/").split("/")[-1]
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.requests.append((cmd, form, dict(request.headers)))
        if self.error:
            return httpx.Response(200, json={"error": self.error})
        return httpx.Response(200, json=getattr(self, cmd)(form))

    def GetAccountInfo(self, f):
        return {"result": {"account_id": 42, "account_name": "team", "live_balance": 100.5, "currency": "RUR"}}

    def GetApplications(self, f):
        return {"result": self.apps, "total_count": len(self.apps)}

    def AddApplication(self, f):
        app = {"application_id": len(self.apps) + 10, "application_name": f"{f['application_name']}.team.n2.voximplant.com"}
        self.apps.append(app)
        return {"result": 1, **app}

    def GetScenarios(self, f):
        return {"result": [s for s in self.scenarios if s["scenario_name"] == f.get("scenario_name")]}

    def AddScenario(self, f):
        s = {"scenario_id": len(self.scenarios) + 100, "scenario_name": f["scenario_name"], "script": f["scenario_script"]}
        self.scenarios.append(s)
        return {"result": 1, "scenario_id": s["scenario_id"]}

    def SetScenarioInfo(self, f):
        next(s for s in self.scenarios if s["scenario_id"] == int(f["scenario_id"]))["script"] = f["scenario_script"]
        return {"result": 1}

    def GetRules(self, f):
        return {"result": [r for r in self.rules if r["rule_name"] == f.get("rule_name")]}

    def AddRule(self, f):
        self.rules.append({"rule_id": len(self.rules) + 1, **f})
        return {"result": 1, "rule_id": len(self.rules)}

    def GetUsers(self, f):
        return {"result": [u for u in self.users if u["user_name"] == f.get("user_name")]}

    def AddUser(self, f):
        self.users.append({"user_id": len(self.users) + 1, "user_name": f["user_name"], "password": f["user_password"]})
        return {"result": 1, "user_id": len(self.users)}

    def SetUserInfo(self, f):
        next(u for u in self.users if u["user_id"] == int(f["user_id"]))["password"] = f["user_password"]
        return {"result": 1}

    def GetPhoneNumbers(self, f):
        return {"result": self.numbers}

    def GetCallerIDs(self, f):
        return {"result": self.callerids}


def settings(**kw):
    base = dict(voximplant_account_id="42", voximplant_api_key="test-key-0123456789", voximplant_credentials="",
                voximplant_caller_id="", phone_pin="", seller_phone="")
    return dataclasses.replace(C.settings, **{**base, **kw})


class PhoneTestCase(unittest.TestCase):
    def setUp(self):
        self.cloud = FakeVoximplant()
        real = httpx.AsyncClient
        self.client_patch = patch.object(phone.httpx, "AsyncClient",
                                         lambda **kw: real(transport=httpx.MockTransport(self.cloud), **kw))
        self.client_patch.start()
        self.use(settings())

    def use(self, s):
        if hasattr(self, "settings_patch"):
            self.settings_patch.stop()
        self.settings_patch = patch.object(phone, "settings", s)
        self.settings_patch.start()
        phone._setup = None

    def tearDown(self):
        self.client_patch.stop()
        self.settings_patch.stop()
        phone._setup = None


class SetupTest(PhoneTestCase):
    def test_setup_creates_everything_once(self):
        cfg = asyncio.run(phone.setup())
        self.assertEqual(cfg["username"], "agent@domclick-agent.team.n2.voximplant.com")
        self.assertEqual(cfg["callerid"], "74951234567")
        self.assertEqual(cfg["voice"], "SaluteSpeech.ru_RU_sergey")
        self.assertEqual(len(self.cloud.apps), 1)
        self.assertEqual(len(self.cloud.scenarios), 1)
        self.assertIn("callPSTN", self.cloud.scenarios[0]["script"])  # загружен настоящий scenario.js
        self.assertEqual(self.cloud.rules[0]["rule_pattern"], ".*")
        self.assertEqual(self.cloud.rules[0]["scenario_id"], "100")
        self.assertEqual(self.cloud.users[0]["password"], phone._user_password())
        # повторная настройка (новый запуск сервера) ничего не дублирует, но обновляет сценарий
        self.cloud.scenarios[0]["script"] = "старая версия"
        asyncio.run(phone.setup(force=True))
        self.assertEqual((len(self.cloud.apps), len(self.cloud.scenarios), len(self.cloud.rules), len(self.cloud.users)),
                         (1, 1, 1, 1))
        self.assertIn("callPSTN", self.cloud.scenarios[0]["script"])
        # в пределах процесса настройка кешируется
        n = len(self.cloud.requests)
        asyncio.run(phone.setup())
        self.assertEqual(len(self.cloud.requests), n)

    def test_node_and_api_host(self):
        self.assertEqual(asyncio.run(phone.setup())["node"], "NODE_2")  # домен приложения «…n2.voximplant.com»
        self.assertEqual(phone._node("domclick-agent.team.n11.voximplant.com"), "NODE_11")
        self.assertEqual(phone._node("domclick-agent.team.voximplant.com"), "")
        # адрес API из кабинета «api-node3.voximplant.com» -> узел 3 (российский)
        self.use(settings(voximplant_api_host="api-node3.voximplant.com"))
        self.assertEqual(phone._node("domclick-agent.alndis.voximplant.com"), "NODE_3")
        self.use(settings(voximplant_node="node_4", voximplant_api_host="api.example.test"))
        self.assertEqual(phone._node("x.team.n2.voximplant.com"), "NODE_4")  # из .env важнее домена
        asyncio.run(phone.api("GetAccountInfo"))
        self.assertEqual(self.cloud.hosts[-1], "api.example.test")
        self.assertEqual(self.cloud.hosts[0], "api.voximplant.com")

    def test_api_key_auth_fields(self):
        asyncio.run(phone.api("GetAccountInfo"))
        cmd, form, headers = self.cloud.requests[-1]
        self.assertEqual((cmd, form["account_id"], form["api_key"]), ("GetAccountInfo", "42", "test-key-0123456789"))
        self.assertNotIn("authorization", headers)

    def test_service_account_jwt(self):
        import jwt
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        path = Path(tempfile.mkdtemp()) / "voximplant_credentials.json"
        path.write_text(json.dumps({"account_id": 42, "key_id": "kid-1", "private_key": pem}))
        self.use(settings(voximplant_account_id="", voximplant_api_key="", voximplant_credentials=str(path)))
        self.assertTrue(phone.configured())
        asyncio.run(phone.api("GetAccountInfo"))
        _, form, headers = self.cloud.requests[-1]
        self.assertNotIn("api_key", form)
        token = headers["authorization"].removeprefix("Bearer ")
        self.assertEqual(jwt.get_unverified_header(token)["kid"], "kid-1")
        claims = json.loads(jwt.api_jws.decode(token, key.public_key(), algorithms=["RS256"]))
        self.assertEqual(claims["iss"], 42)
        self.assertLessEqual(claims["exp"] - claims["iat"], 64)
        # пароль пользователя выводится из ключа сервисного аккаунта
        self.assertTrue(phone._user_password().startswith("Vx"))

    def test_not_configured(self):
        self.use(settings(voximplant_account_id="", voximplant_api_key=""))
        self.assertFalse(phone.configured())
        with self.assertRaisesRegex(phone.PhoneError, "VOXIMPLANT_ACCOUNT_ID"):
            asyncio.run(phone.config())

    def test_bad_key_is_explained(self):
        self.cloud.error = {"code": 100, "msg": "Invalid account_id or api_key"}
        with self.assertRaisesRegex(phone.PhoneError, "не принял ключ доступа"):
            asyncio.run(phone.setup())

    def test_callerid_choice(self):
        self.cloud.numbers = []
        self.assertEqual(asyncio.run(phone.setup())["callerid"], "")
        with self.assertRaisesRegex(phone.PhoneError, "Нет номера, с которого звонить"):
            asyncio.run(phone.config())
        self.cloud.callerids = [{"callerid_number": "+7 (916) 000-11-22", "active": True, "verified": True}]
        self.assertEqual(asyncio.run(phone.setup(force=True))["callerid"], "79160001122")
        self.use(settings(voximplant_caller_id="+7 999 000-00-01"))
        self.assertEqual(asyncio.run(phone.setup())["callerid"], "79990000001")

    def test_callerid_status(self):
        self.cloud.callerids = [
            {"callerid_number": "79953277135", "active": True, "verified_until": "2027-11-03"},
            {"callerid_number": "79160001122", "active": True, "code_entering_attempts_left": 3,
             "verification_call_attempts_left": 5},
        ]
        st = lambda n: asyncio.run(phone.callerid_status(n))  # noqa: E731
        self.assertEqual(st("+7 995 327-71-35"), "подтверждён до 2027-11-03")
        self.assertIn("НЕ ПОДТВЕРЖДЁН", st("79160001122"))
        self.assertIn("НЕ НАЙДЕН", st("79990000000"))
        self.assertEqual(st("74951234567"), "арендован в Voximplant")
        # неподтверждённый номер сервер сам не выбирает
        self.cloud.numbers = []
        self.cloud.callerids = self.cloud.callerids[1:]
        self.assertEqual(asyncio.run(phone.setup(force=True))["callerid"], "")

    def test_test_mode_callerid(self):
        self.use(settings(voximplant_caller_id="default"))
        cfg = asyncio.run(phone.config())
        self.assertEqual(cfg["callerid"], "default")
        self.assertIn("тестовый режим", asyncio.run(phone.callerid_status("default")))

    def test_one_time_key_hash(self):
        key = "4c7ab8e1d2"
        expected = hashlib.md5(
            f"{key}|{hashlib.md5(f'agent:voximplant.com:{phone._user_password()}'.encode()).hexdigest()}".encode()
        ).hexdigest()
        self.assertEqual(phone.login_hash(key), expected)
        with self.assertRaises(phone.PhoneError):
            phone.login_hash("")
        # пароль зависит от ключа доступа и стабилен между запусками
        p1 = phone._user_password()
        self.assertEqual(p1, phone._user_password())
        self.use(settings(voximplant_api_key="other-key-987654321"))
        self.assertNotEqual(p1, phone._user_password())


class PhoneHttpTest(PhoneTestCase):
    def setUp(self):
        super().setUp()
        from backend import auth, db
        from backend import main as M

        db.configure("sqlite://")
        auth._fails.clear()
        ratelimit.reset()
        self.M = M
        self.startup = patch.object(M.app.router, "on_startup", [])
        self.startup.start()
        self.c = TestClient(M.app)

    def tearDown(self):
        self.startup.stop()
        ratelimit.reset()
        super().tearDown()

    def test_phone_config_has_no_password(self):
        r = self.c.get("/api/phone")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["username"], "agent@domclick-agent.team.n2.voximplant.com")
        self.assertEqual(data["callerid"], "74951234567")
        self.assertNotIn(phone._user_password(), r.text)
        self.assertNotIn("test-key-0123456789", r.text)
        h = self.c.post("/api/phone/login", json={"key": "abc"}).json()
        self.assertEqual(h["hash"], phone.login_hash("abc"))

    def test_pin_required(self):
        self.use(settings(phone_pin="2468"))
        self.assertEqual(self.c.get("/api/phone").status_code, 403)
        self.assertEqual(self.c.post("/api/phone/login", json={"key": "abc"}).status_code, 403)
        self.assertEqual(self.c.get("/api/phone", headers={"X-Phone-Pin": "2468"}).status_code, 200)

    def test_errors_are_503_with_reason(self):
        self.cloud.error = {"code": 100, "msg": "Invalid account_id or api_key"}
        r = self.c.get("/api/phone")
        self.assertEqual(r.status_code, 503)
        self.assertIn("не принял ключ", r.json()["detail"])

    def test_health_and_old_tts_gone(self):
        h = self.c.get("/api/health").json()
        self.assertTrue(h["phone"]["configured"])
        self.assertEqual(h["phone"]["provider"], "voximplant")
        self.assertIn(self.c.post("/api/tts", json={"text": "привет"}).status_code, (404, 405))

    def test_sdk_is_served(self):
        r = self.c.get("/vendor/voximplant.min.js")
        self.assertEqual(r.status_code, 200)
        self.assertIn("VoxImplant", r.text[:2000])


if __name__ == "__main__":
    unittest.main()
