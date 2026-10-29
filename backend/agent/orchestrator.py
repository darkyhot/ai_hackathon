"""Конвейер агента: запрос -> финансы -> поиск -> подбор -> проверка -> звонок -> просмотр -> сделка -> итог.

Сессия выполняется фоновой задачей. Каждое событие (сообщение, карточка, шаг, реплика звонка) получает номер,
пишется в ленту диалога в базе и уходит во все открытые вкладки (SSE). Закрытая вкладка агента не останавливает:
диалог можно открыть снова — сначала придёт история, потом живые события.
Где решение за клиентом, конвейер ждёт POST /confirm. Текст и голос клиента приходят через POST /message.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .. import db, economics, geo, sources
from ..db import repo
from ..llm import LLMClient, LLMError
from . import finance as F
from . import mortgage as M
from . import negotiation as N
from . import preferences as P
from . import prompts, qa, seller_sim
from . import tools as T

log = logging.getLogger(__name__)

STEPS = ["Запрос", "Финансы", "Поиск", "Подбор", "Проверка", "Звонок", "Просмотр", "Сделка", "Итог"]
BATCH = 6
MAX_SELECTION_ROUNDS = 6


@dataclass
class Session:
    id: str
    query: str
    llm: LLMClient
    visitor_id: str | None = None
    user_id: int | None = None
    base_profile: dict | None = None  # продолжение диалога: что агент уже знал о запросе
    seq: int = 0                      # номер последнего события диалога
    history: list[dict] = field(default_factory=list)  # события этого запуска (если база недоступна)
    subscribers: set[asyncio.Queue] = field(default_factory=set)  # открытые вкладки
    idle_since: float = field(default_factory=time.monotonic)  # когда закрылась последняя вкладка
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    seller_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    task: asyncio.Task | None = None
    profile: dict = field(default_factory=dict)
    prefs: P.Prefs = field(default_factory=P.Prefs)
    notes: list[str] = field(default_factory=list)
    finance: dict | None = None
    mortgage: dict | None = None   # ипотечные программы: что подходит и по какой ставке считаем
    stage: str | None = None       # текущий шаг — для ответов на вопросы
    shown: list = field(default_factory=list)  # последняя подборка (карточки со справедливой ценой)
    current: dict | None = None    # квартира, по которой идёт звонок: {"listing", "fair"}
    answer_only: bool = False      # продолжение диалога вопросом: только ответ, без нового поиска
    prev_status: str = "running"   # статус диалога до вопроса — после ответа возвращаем его
    opened: dict[str, int] = field(default_factory=dict)
    journal: dict = field(default_factory=dict)
    llm_degraded: bool = False
    map_warned: bool = False

    # ------------------------------------------------------------- события
    def push(self, type_: str, **payload: Any) -> dict:
        """Событие в ленту диалога: номер, база, все открытые вкладки. Синхронно — порядок не путается."""
        self.seq += 1
        ev = json.loads(json.dumps({"type": type_, **payload, "seq": self.seq}, ensure_ascii=False, default=str))
        self.history.append(ev)
        db.safe(repo.save_event, self.id, self.seq, type_, ev)
        for q in list(self.subscribers):
            q.put_nowait(ev)
        if type_ in ("done", "error"):
            status = payload.get("status", "error") if type_ == "done" else "error"
            db.safe(repo.update_chat, self.id, status=status, **({"profile": self.saved_profile()} if self.profile else {}))
        return ev

    async def emit(self, type_: str, **payload: Any) -> None:
        self.push(type_, **payload)

    def saved_profile(self) -> dict:
        """Профиль для продолжения диалога — без цен рынка и служебных полей."""
        return {k: v for k, v in self.profile.items() if k not in ("market",)}

    def save_profile(self) -> None:
        db.safe(repo.update_chat, self.id, profile=json.loads(json.dumps(self.saved_profile(), default=str)))

    async def step(self, n: int, status: str) -> None:
        if status == "active":
            self.stage = STEPS[n - 1]
        await self.emit("step", step=n, title=STEPS[n - 1], status=status)

    async def say(self, text: str) -> None:
        await self.emit("message", role="agent", text=text)

    async def ask(self, kind: str, title: str, text: str, **payload: Any) -> dict:
        """Пауза до решения клиента. Возвращает {"approved": bool, "data": {...}}."""
        fut = asyncio.get_running_loop().create_future()
        self.pending[kind] = fut
        await self.emit("confirm", kind=kind, title=title, text=text, **payload)
        try:
            return await fut
        finally:
            self.pending.pop(kind, None)

    def resolve(self, kind: str, approved: bool, data: dict | None) -> bool:
        """Решение клиента. В ленту уходит событие resolved (с подписью кнопки — для истории),
        а если решение сказано голосом — и сама фраза клиента."""
        fut = self.pending.get(kind)
        if not fut or fut.done():
            return False
        data = dict(data or {})
        label, said = data.pop("_label", None), data.pop("_said", None)
        if said:
            self.push("message", role="user", text=str(said)[:500])
        self.push("resolved", kind=kind, approved=approved, label=label)
        fut.set_result({"approved": approved, "data": data})
        return True

    async def receive_text(self, text: str, marks: dict | None = None) -> str:
        """Текст клиента: ответ на вопрос, пожелание к подборке или заметка на потом."""
        await self.emit("message", role="user", text=text)
        if qa.is_question(text):  # «что такое СБР?» — отвечаем, текущий шаг ждёт дальше
            asyncio.create_task(qa.answer(self, text))
            return "question"
        if self.resolve("clarify", True, {"text": text}):
            return "answer"
        marks = marks or {}
        if self.resolve("selection", True, {"action": "more", "note": text,
                                            "liked": marks.get("liked", []), "disliked": marks.get("disliked", [])}):
            return "feedback"
        self.notes.append(text)
        await self.say("Записал — учту при подборе и в разговоре с продавцом.")
        return "note"

    async def open_product(self, product: str, amount: int) -> None:
        if product not in F.PRODUCTS:
            return
        self.opened[product] = int(amount)
        name = F.PRODUCTS[product]["name"]
        await self.emit("product_opened", product=product, amount=int(amount))
        await self.say(f"Готово: {name} на {T.fmt(amount)} (демо — в проде откроется в СберБанк Онлайн в один клик).")


SESSIONS: dict[str, Session] = {}


def create_session(query: str, llm: LLMClient, visitor_id: str | None = None, user_id: int | None = None) -> Session:
    s = Session(id=uuid.uuid4().hex[:12], query=query.strip(), llm=llm, visitor_id=visitor_id, user_id=user_id)
    SESSIONS[s.id] = s
    db.safe(repo.create_chat, s.id, s.query, visitor_id, user_id)
    return s


def continue_session(chat_id: str, text: str, llm: LLMClient) -> Session:
    """Продолжить диалог, в котором агент уже закончил (или сервер перезапускался): новый запуск в той же ленте.
    Агент помнит запрос: «а если трёшку?» — тот же город и бюджет, но 3 комнаты."""
    info = db.safe(lambda d: (lambda c: c and (c.profile, c.visitor_id, c.user_id, c.last_seq, c.status))(repo.get_chat(d, chat_id)))
    profile, vid, uid, last_seq, status = info or (None, None, None, 0, "running")
    s = Session(id=chat_id, query=text.strip(), llm=llm, visitor_id=vid, user_id=uid, base_profile=profile or None)
    s.seq = last_seq or 0
    if profile and qa.is_question(text):  # «сколько я переплачу?» в завершённом диалоге — ответ без нового поиска
        s.answer_only, s.profile, s.prev_status = True, dict(profile), status
    SESSIONS[chat_id] = s
    db.safe(repo.update_chat, chat_id, status="running")
    return s


def running(s: Session | None) -> bool:
    return s is not None and s.task is not None and not s.task.done()


IDLE_DONE_S = 30 * 60        # завершённый диалог без открытых вкладок — убрать из памяти (история в базе)
IDLE_WAITING_S = 6 * 3600    # агент ждёт решения, а вкладок нет 6 часов — остановить (продолжить можно сообщением)


def reap_idle(now: float | None = None) -> list[str]:
    """Освобождает память от брошенных сессий. Вызывается периодически из main."""
    now = time.monotonic() if now is None else now
    gone = []
    for sid, s in list(SESSIONS.items()):
        if s.subscribers:
            continue
        idle = now - s.idle_since
        if running(s) and idle > IDLE_WAITING_S:
            s.task.cancel()
            db.safe(repo.update_chat, sid, status="paused")
        elif not running(s) and idle > IDLE_DONE_S:
            gone.append(sid)
            del SESSIONS[sid]
    return gone


# ------------------------------------------------------------- помощники


async def mark_degraded(s: Session, exc: Exception) -> None:
    if not s.llm_degraded and s.llm.name != "offline":
        s.llm_degraded = True
        await s.emit("provider", provider=s.llm.name, model=s.llm.model, degraded=True, reason=str(exc)[:160])


async def parse_request(s: Session, text: str, base: dict | None = None) -> dict:
    try:
        raw = await s.llm.chat_json(
            [{"role": "system", "content": prompts.PARSE_REQUEST}, {"role": "user", "content": text}],
            tag="parse_request",
        )
        profile = T.merge_profile(raw, base)
        profile["parsed_by"] = s.llm.name
    except (LLMError, KeyError, ValueError, TypeError) as exc:
        log.warning("parse_request fallback: %s", exc)
        await mark_degraded(s, exc)
        profile = T.merge_profile(T.parse_request_rules(text), base)
        profile["parsed_by"] = "rules"
    # удобства («с балконом», «гардеробная») ищем и правилами: модель их иногда теряет
    profile["features"] = T.merge_features((base or {}).get("features", []),
                                           T.wanted_features(text, *profile.get("wishes") or []))
    # что должно быть рядом («школа рядом», «поликлиника») — проверим по карте
    profile["poi"] = list(dict.fromkeys([*(base or {}).get("poi", []), *(["school"] if profile.get("near_school") else []),
                                         *geo.wanted(text, *profile.get("wishes") or [])]))
    return profile


def ranked(pool: list[dict], meta: dict[str, dict], prefs: P.Prefs) -> list[dict]:
    for x in pool:
        meta[x["id"]]["total"] = round(meta[x["id"]]["score"] + P.adjust(x, prefs), 1)
    return sorted(pool, key=lambda x: meta[x["id"]]["total"], reverse=True)


def call_questions(x: dict, prefs: P.Prefs, limit: int = 3) -> list[dict]:
    """Что спросить у продавца: пробелы в объявлении (пожелания клиента уже проверены в подборке)."""
    return T.missing_info(x, limit, prefs.features)


def wishes_text(prefs: P.Prefs) -> str:
    """«с балконом или лоджией, гардеробной, рядом: школа, вода»."""
    parts = []
    if prefs.features:
        parts.append("с " + ", ".join(T.FEATURE_WITH[k] for k in prefs.features))
    if prefs.poi:
        parts.append("рядом: " + ", ".join(geo.POI[k][0] for k in prefs.poi))
    return ", ".join(parts)


def fits(x: dict, prefs: P.Prefs, map_ok: bool = True) -> bool:
    """Квартира выполняет все пожелания: удобства подтверждены объявлением, нужное — в 1 км по карте."""
    if any(T.feature_status(x, k) is not True for k in prefs.features):
        return False
    return not map_ok or geo.has_all(x, prefs.poi)


def drop_wishes(text: str, prefs: P.Prefs) -> list[str]:
    """Клиент снял условия: «Без условий», «Без условия: гардеробная». Возвращает снятые названия."""
    t = text.lower().replace("ё", "е")
    if not re.search(r"без (?:этих )?услови|не важно", t):
        return []
    every = re.search(r"без условий|без этих|все не важно", t)
    dropped = []
    for k in list(prefs.features):
        if every or T.FEATURES[k][0].split()[0][:6] in t:
            prefs.features.remove(k)
            dropped.append(T.FEATURES[k][0])
    for k in list(prefs.poi):
        if every or geo.POI[k][0].replace("ё", "е").split()[0][:6] in t:
            prefs.poi.remove(k)
            dropped.append(geo.POI[k][0])
    return dropped


def card(x: dict, meta: dict, prefs: P.Prefs) -> dict:
    m = meta[x["id"]]
    return {**x, "fair": m["fair"], "score": m["total"], "pay": m.get("pay"),
            "features": [{"label": T.FEATURES[k][0], "status": T.feature_status(x, k)} for k in prefs.features],
            "nearby": geo.summary(x, prefs.poi),
            "unchecked": [geo.POI[k][0] for k in prefs.poi if k not in (x.get("nearby") or {})],
            "missing": [q["label"] for q in call_questions(x, prefs, 4) if q["key"] != "owners"]}


async def check_map(s: Session, listings: list[dict], announce: bool = False) -> bool:
    """Находит по карте, что рядом с квартирами (школы, парки, вода…). False — проверить не удалось."""
    if not s.prefs.poi or not listings:
        return True
    if announce:
        await s.say("Проверяю по карте, что рядом: " + ", ".join(geo.POI[k][0] for k in s.prefs.poi) + ".")
    ok = await geo.nearby(listings, s.prefs.poi)
    if not ok and not s.map_warned:
        s.map_warned = True
        await s.say("Не смог проверить по карте, что рядом: сервис карты OpenStreetMap не ответил. "
                    "Показываю квартиры без этой проверки — честно отмечу это на карточках.")
    return ok


async def narrow_to_wishes(s: Session, pool: list[dict], before: tuple[list, list], map_ok: bool) -> list[dict]:
    """Клиент добавил пожелание уже в подборке: оставляем только подходящие или честно говорим, что таких нет."""
    if (s.prefs.features, s.prefs.poi) == before:
        return pool
    new_poi = [k for k in s.prefs.poi if k not in before[1]]
    if new_poi:
        map_ok = await check_map(s, pool, announce=True) and map_ok
    good = [x for x in pool if fits(x, s.prefs, map_ok) or x["id"] in s.prefs.liked]
    want = wishes_text(s.prefs)
    if any(x["id"] not in s.prefs.liked for x in good):
        await s.say(f"Оставил квартиры {want}: {len(good)} из {len(pool)}.")
        return good
    s.prefs.features, s.prefs.poi = list(before[0]), list(before[1])
    await s.say(f"Среди найденных квартир {want} нет. Чтобы искать с этим условием заново — и проверить чуть дороже, "
                "начните новый запрос с этим пожеланием.")
    return pool


async def score_into(pool: list[dict], comps: list[dict], profile: dict, meta: dict) -> None:
    for x in pool:
        if x["id"] not in meta:
            fp = T.fair_price(x, comps)
            score = T.score_listing(x, profile, fp)
            # платёж по подходящей программе — у каждой квартиры свой (новостройка может пройти по семейной)
            pay = M.listing_payment(x, profile) if profile.get("payment") not in (None, "cash") else None
            if pay and pay["program"] == "семейная":
                score += 6  # платёж заметно ниже — для семьи это важнее пары метров
            meta[x["id"]] = {"fair": fp, "score": score, "pay": pay}


CORRECTION_LABELS = {"kitchen": ("кухня", " м²"), "area": ("площадь", " м²"), "floor": ("этаж", ""), "floors": ("этажей в доме", ""),
                     "year": ("год постройки", ""), "rooms": ("комнат", ""), "condition": ("ремонт", "")}


async def apply_corrections(s: Session, listing: dict, corrections: dict, persist: bool) -> None:
    """Продавец уточнил параметры: применяем к квартире (просмотр, сделка, карточки) и, если продавец настоящий, —
    запоминаем в базе поверх объявления. Слова ИИ-продавца (симуляция) в общую базу не попадают."""
    said = []
    for key, value in corrections.items():
        if key in CORRECTION_LABELS:
            label, unit = CORRECTION_LABELS[key]
            was = listing.get(key)
            listing.setdefault("listed", {})[key] = was
            listing[key] = value
            said.append(f"{label} {value:g}{unit}" if isinstance(value, (int, float)) else f"{label}: {value}")
            if was is not None:
                said[-1] += f" (в объявлении — {was:g}{unit})" if isinstance(was, (int, float)) else f" (в объявлении — {was})"
        else:
            listing.setdefault("confirmed", {})[key] = value
            said.append(f"{T.FEATURES[key][0]} — {'есть' if value else 'нет'}")
    if persist:
        sources.store.correct(listing["id"], corrections)
    await s.say("Продавец уточнил параметры: " + "; ".join(said) + "."
                + (" Запомнил — в подборках у этой квартиры будет так." if persist else ""))


async def ask_family(s: Session, profile: dict) -> dict:
    """Семья и маткапитал — от них зависит ставка. Спрашиваем один раз и только то, чего не понял из запроса."""
    known = M.normalize_family(profile)
    if known["children"] == 0 or (known["children"] and known["youngest_age"] is not None and profile.get("matcap") is not None):
        return known
    r = await s.ask(
        "family", "Семья и маткапитал",
        "От этого зависит ставка: семейная ипотека — от 10% вместо рыночных ~17,5%, а маткапитал можно направить во взнос.",
        defaults={"children": known["children"] if known["children"] is not None else 1,
                  "youngest_age": known["youngest_age"] if known["youngest_age"] is not None else "",
                  "matcap": known["matcap"] or (M.MATCAP_FIRST if profile.get("matcap") else 0),
                  "matcap_default": M.MATCAP_FIRST},
    )
    return M.normalize_family(r["data"]) if r["approved"] else {"children": 0, "youngest_age": None, "matcap": 0}


async def ask_housing(s: Session, profile: dict, progs: dict | None) -> None:
    """Где искать. Если семейная не проходит на вторичке, но прошла бы в новостройке, — предлагаем выбор.
    Тип жилья, который клиент назвал сам, не переспрашиваем."""
    if profile.get("housing") in ("new", "secondary", "both"):
        return
    alt = next((c for c in (progs or {}).get("programs", []) if c["key"] == "family_new"), None)
    if not alt:
        profile["housing"] = "secondary"
        return
    market = next(c for c in progs["programs"] if c["key"] == "market")
    cap = M.max_family_price(profile["city"], profile)
    limit = f" для квартир до {T.fmt(cap)} при взносе 20%" if cap and cap < profile["budget_max"] else ""
    r = await s.ask(
        "housing", "Где искать",
        f"Семейная ипотека работает в новостройках{limit}: ставка {alt['rate'] * 100:.0f}%, платёж около "
        f"{T.fmt(alt['monthly'])} вместо {T.fmt(market['monthly'])} на вторичке. Искать и там, и там?",
    )
    profile["housing"] = r["data"].get("housing", "secondary") if r["approved"] else "secondary"
    await s.say({"both": "Ищу и в новостройках, и на вторичке — у каждой квартиры покажу платёж по подходящей программе.",
                 "new": "Ищу только в новостройках — посчитаю по семейной ипотеке, где она проходит.",
                 "secondary": "Хорошо, ищу на вторичке."}[profile["housing"]])


async def choose_program(s: Session, profile: dict, price: int) -> dict:
    """Какая ипотека подходит: рыночная или семейная — с объяснением, почему, и разницей платежа."""
    profile.update(await ask_family(s, profile))
    progs = M.programs(price, profile["city"], profile, housing=profile.get("housing"),
                       is_secondary=profile.get("housing") != "new")
    s.mortgage = progs
    await s.say(progs["explanation"])
    return progs


async def ask_affordability(s: Session, profile: dict, market: dict | None) -> tuple[dict, dict]:
    """Клиент не знает бюджет: считаем, на какую квартиру хватит с ипотекой."""
    r = await s.ask(
        "finance", "Сколько вы можете себе позволить",
        "Посчитаю бюджет по доходам, расходам и накоплениям (в демо — пример из СберБанк Онлайн, можно поправить): "
        "платёж по ипотеке — не больше половины дохода, первый взнос 20% — из накоплений к сроку покупки.",
        defaults=F.DEFAULT_INPUTS, price=None,
    )
    inputs = r["data"] if r["approved"] else {}
    aff = F.affordable(inputs, T.DEMO_RATE)
    why = ("ограничивает платёж" if aff["limited_by"] == "payment" else "ограничивает первый взнос")
    text = (f"Комфортный бюджет — до {T.fmt(aff['budget'])} ({why}): платёж до {T.fmt(aff['payment_limit'])} в месяц "
            f"даёт до {T.fmt(aff['by_payment'])}, а накопления к сроку покупки ({aff['horizon_months']} мес.) — "
            f"взнос на квартиру до {T.fmt(aff['by_down'])}.")
    if market and aff["budget"] < market["p25"]:
        text += (f" Это ниже большинства предложений: такие квартиры обычно стоят от {T.fmt(market['p25'])}. "
                 "Поищу в пределах бюджета; если вариантов мало — стоит рассмотреть меньше комнат или другой район.")
    await s.say(text)
    return inputs, aff


# ------------------------------------------------------------- конвейер


async def pipeline(s: Session) -> None:
    await s.emit("provider", provider=s.llm.name, model=s.llm.model, degraded=False)
    await s.emit("message", role="user", text=s.query)

    # 1. Запрос и уточнения: сначала город и комнаты, потом бюджет — с ценами рынка перед глазами
    await s.step(1, "active")
    if s.base_profile:
        await s.say("Продолжаю этот диалог — помню запрос: " + (s.base_profile.get("summary") or "").rstrip(".") + ".")
    profile = await parse_request(s, s.query, base=s.base_profile)
    market = None
    fin_inputs = None
    for _ in range(5):
        unsupported = profile.get("city") and profile["city"] not in sources.SUPPORTED_CITIES
        missing = [m for m in T.missing_required(profile) if m != "budget_max"]
        chips: list[str] = []
        if unsupported:
            question = (f"Город «{profile['city']}» на Циане я пока не знаю: ищу в {len(sources.SUPPORTED_CITIES)} городах — "
                        "областных центрах и крупных городах Подмосковья. Какой подойдёт?")
            profile["city"] = None
            chips = sources.FEATURED_CITIES[:4]
        elif missing:
            question = T.clarify_question(missing)
            chips = sources.FEATURED_CITIES[:4] if "city" in missing else ["1 комната", "2 комнаты", "3 комнаты"]
        elif profile.get("budget_max") is None and profile.get("budget_unknown"):
            fin_inputs, aff = await ask_affordability(s, profile, market)
            profile["budget_max"] = aff["budget"]
            profile["budget_source"] = "по доходам и накоплениям"
            continue
        elif profile.get("budget_max") is None:
            if market is None:
                await s.say(f"Смотрю цены на Циане ({profile['city']}, {T.rooms_text(profile['rooms'])}), чтобы подсказать бюджет…")
                market = await sources.market(profile)
            question = T.budget_question(profile, market)
            chips = T.budget_chips(market) + ["Не знаю — посчитай по моим доходам"]
        else:
            break
        if market and profile.get("budget_max") is None:  # показать цены рынка на экране, пока клиент выбирает бюджет
            await s.emit("result", step=1, kind="profile",
                         data={**profile, "market": market, "summary": "Осталось выбрать бюджет — ниже цены рынка по вашему запросу."})
        r = await s.ask("clarify", "Уточнение", question, chips=chips, market=market)
        text = r["data"].get("text", "")
        profile = await parse_request(s, text, base=profile)
        if profile.get("budget_max") is None and T.wants_budget_help(text):
            profile["budget_unknown"] = True
    profile["market"] = market
    if T.missing_required(profile):
        await s.say("Без города, бюджета и числа комнат искать не получится. Напишите их — и начнём заново.")
        await s.emit("done", status="cancelled")
        return
    profile["summary"] = T.profile_summary(profile)
    s.profile = profile
    s.prefs = P.Prefs(area_min=profile.get("area_min"), kitchen_min=profile.get("kitchen_min"),
                      avoid_first=bool(profile.get("not_first_floor")), metro_max=profile.get("metro_max_minutes"))
    await s.emit("result", step=1, kind="profile", data=profile)
    await s.say(profile["summary"] + " Проверьте «красные линии» — в их пределах я действую сам.")
    r = await s.ask(
        "red_lines", "Красные линии",
        "Агент сам ищет, звонит и торгуется, но не выходит за эти рамки. Любую итоговую цену подтверждаете вы.",
        payload={"budget_max": profile["budget_max"], "rooms": profile["rooms"], "city": profile["city"],
                 "authority_rule": "не выше верхней границы справедливой цены и бюджета"},
    )
    if not r["approved"]:
        await s.say("Хорошо, остановился. Поправьте запрос, и начнём заново.")
        await s.emit("done", status="cancelled")
        return
    if r["data"].get("budget_max"):
        profile["budget_max"] = int(r["data"]["budget_max"])
    s.save_profile()  # с этого места диалог можно продолжить даже после перезапуска сервера
    await s.step(1, "done")

    # 2. Финансы — только то, что нужно этому клиенту: наличные / ипотека / накопить на взнос
    await s.step(2, "active")
    budget = profile["budget_max"]
    payment = profile.get("payment")
    fin = F.plan(budget, fin_inputs) if fin_inputs is not None else None
    if fin is not None and payment in (None, "mortgage_ready"):  # бюджет считали по доходам — значит, ипотека
        payment = "mortgage_saving" if fin["gap"] else "mortgage_ready"  # верим цифрам: не хватает — нужен план
    if payment is None:
        r = await s.ask("payment", "Как планируете покупать?",
                        "От этого зависит, что посчитать: ипотеку, план накопления на взнос — или ничего.")
        payment = r["data"].get("payment", "mortgage_ready") if r["approved"] else "mortgage_ready"
    profile["payment"] = payment
    s.finance = None
    progs = await choose_program(s, profile, budget) if payment != "cash" else None
    chosen = next(c for c in progs["programs"] if c["key"] == progs["chosen"]) if progs else None
    m_kw = ({"rate": progs["rate"] if progs["chosen"] != "market" else None, "program": chosen["title"],
             "matcap": (progs["matcap"] or {}).get("amount", 0)} if progs else {})
    if payment == "cash":
        await s.emit("result", step=2, kind="finance", data={"mode": "cash", "price": budget})
        await s.say("Покупка без ипотеки: деньги уйдут продавцу через сервис безопасных расчётов только после регистрации права. "
                    "Пока ищем квартиру, сумму можно держать на накопительном счёте — по желанию.")
    elif payment == "mortgage_ready":
        mortgage = T.mortgage_scenarios(budget, (fin_inputs or {}).get("monthly_income"), **m_kw)
        rec = next(x for x in mortgage["scenarios"] if x["title"] == mortgage["recommended"])
        await s.emit("result", step=2, kind="finance", data={"mode": "mortgage", "mortgage": mortgage, "programs": progs})
        await s.say(f"Ипотека: при взносе {T.fmt(rec['down_payment'])} и сроке {rec['years']} лет платёж около {T.fmt(rec['monthly'])} в месяц. "
                    f"Каждые 100 000 ₽, которые я сторгую, — минус {T.fmt(mortgage['per_100k_discount'])} в месяц.")
    else:  # ипотека, взнос ещё копим — тут и нужен финансовый помощник
        if fin is None:
            r = await s.ask(
                "finance", "Финансовая подготовка",
                "Подтяну доходы, расходы и накопления из СберБанк Онлайн (в демо — пример, можно поправить) "
                "и посчитаю, как собрать первоначальный взнос.",
                defaults=F.DEFAULT_INPUTS, price=budget,
            )
            fin_inputs = r["data"] if r["approved"] else {}
            fin = F.plan(budget, fin_inputs, matcap=m_kw.get("matcap", 0))
        elif m_kw.get("matcap"):  # план считали до вопроса про маткапитал — пересчитаем с ним
            fin = F.plan(budget, fin["inputs"], matcap=m_kw["matcap"])
        s.finance = fin
        mortgage = T.mortgage_scenarios(budget, fin["inputs"]["monthly_income"], fin["available"], **m_kw)
        await s.emit("result", step=2, kind="finance", data={"mode": "saving", "finance": fin, "mortgage": mortgage, "programs": progs})
        rec = ", ".join(f"{a['name'].lower()} — {T.fmt(a['amount_now'])}" for a in fin["allocation"] if a["amount_now"])
        await s.say(f"{fin['verdict']} Рекомендую разложить накопления: {rec}." + (f" {fin['risk_note']}" if fin["risk_note"] else ""))
        if fin["gap"] and not fin["fits_horizon"]:
            text = (f"По выбранной стратегии взнос накопится {'за ' + str(fin['months']) + ' мес.' if fin['months'] else 'нескоро'}, "
                    f"а вы хотели за {fin['inputs']['horizon_months']} мес. Можно искать уже сейчас: торг уменьшит нужный взнос, "
                    "а я буду следить за ценами.")
        elif fin["gap"]:
            text = "План накоплений готов: продукты из плана можно открыть на экране справа. Ищем квартиру сейчас — к сделке взнос накопится."
        else:
            text = "На взнос хватает. Откройте продукты из плана, чтобы деньги работали, пока ищем квартиру."
        r = await s.ask("finance_choice", "Финансовый план", text, gap=fin["gap"])
        if not r["approved"] and fin["gap"]:
            await s.say("Договорились: сначала копим. Откройте продукты из плана — я напомню, когда взнос будет готов, и начну подбор.")
            await s.step(2, "done")
            await s.emit("done", status="saving")
            return
    await ask_housing(s, profile, progs)
    s.profile = profile
    s.save_profile()
    await s.step(2, "done")

    # 3. Поиск: сначала целые квартиры в рамках, потом район и пожелания. Чего нет — говорим честно и предлагаем, что поменять.
    await s.step(3, "active")
    s.prefs.features = list(profile.get("features") or [])
    s.prefs.poi = list(profile.get("poi") or [])
    map_ok = True
    named: list[str] = []
    strict = False
    for attempt in range(5):
        where = profile["city"] + (f", {profile['okrug']}" if profile.get("okrug") else "")
        market_txt = {"new": "новостройки", "both": "новостройки и вторичка"}.get(profile.get("housing"), "вторичный рынок")
        await s.say(f"Ищу на Циане: {where}, {T.rooms_text(profile['rooms'])}, {market_txt}, до {T.fmt(profile['budget_max'])}.")
        listings, report = await sources.collect(profile)
        # что агент уже видел на Циане за неделю по такому запросу — с посчитанными признаками и «что рядом»
        known = sources.stored(profile, exclude={x["id"] for x in listings})
        if known:
            listings += known
            report.append({"source": "База агента", "count": len(known), "status": "db",
                           "note": f"видели на Циане за {sources.store.SEEN_DAYS} дней"})
            await s.say(f"Добавил из своей базы {len(known)} {T.plural(len(known), 'объявление', 'объявления', 'объявлений')}, "
                        "которые видел на Циане за неделю: признаки и «что рядом» по ним уже посчитаны.")
        pool = [x for x in listings if T.passes_hard_filters(x, profile)]
        page = 1
        if not pool:
            await s.emit("result", step=3, kind="search", data={"report": report, "total": len(listings), "matched": 0, "rows": []})
            if report and all(r["status"] == "unavailable" for r in report):
                await s.say(f"Циан сейчас не отдаёт объявления ({report[0]['note']}). Попробуйте чуть позже.")
                await s.emit("done", status="empty")
                return
            m = await sources.market(profile)
            question = (f"Целых квартир ({T.rooms_text(profile['rooms'])}, {where}) до {T.fmt(profile['budget_max'])} сейчас нет — "
                        "доли и комнаты я не показываю.")
            chips = []
            if m:
                cheapest = int(-(-m["min"] // 100_000) * 100_000)
                question += f" Самые доступные — от {T.mln(m['min'])}, обычно {T.mln(m['p25'])}–{T.mln(m['p75'])}."
                chips.append(f"Бюджет до {T.mln(cheapest)}")
            if profile["rooms"] and profile["rooms"] > 1:
                chips.append(f"{profile['rooms'] - 1}-комнатные")
            if profile.get("okrug"):
                chips.append(f"Искать по всему городу ({profile['city']})")
            question += " Что меняем?"
        else:
            # район, который назвал клиент (Черёмушки): догружаем страницы и оставляем только его, если хватает
            named = profile.get("districts") or []
            strict = False  # True — ищем только в названном районе
            if named:
                while sum(T.in_districts(x, named) for x in pool) < BATCH and page < 3:
                    page += 1
                    seen = {x["id"] for x in listings}
                    extra = [x for x in await sources.more(profile, page) if x["id"] not in seen]
                    listings += extra
                    pool += [x for x in extra if T.passes_hard_filters(x, profile)]
                inside = [x for x in pool if T.in_districts(x, named)]
                s.prefs.districts_plus |= {x["district"] for x in inside}
                if len(inside) >= 3:
                    pool, strict = inside, True
                    await s.say(f"Оставил только район {', '.join(named)}: {len(inside)} подходящих объявлений.")
                else:
                    await s.say(f"В районе {', '.join(named)} сейчас {len(inside)} подходящих объявлений — покажу их первыми, "
                                "дальше соседние районы.")
            if not (s.prefs.features or s.prefs.poi):
                break
            # пожелания (балкон, гардеробная, школа рядом…): показываем только то, что подтверждено
            map_ok = await check_map(s, pool, announce=True)
            good = [x for x in pool if fits(x, s.prefs, map_ok)]
            while len(good) < BATCH and page < 3:
                page += 1
                seen = {x["id"] for x in listings}
                extra = [x for x in await sources.more(profile, page) if x["id"] not in seen]
                listings += extra
                extra = [x for x in extra if T.passes_hard_filters(x, profile) and (not strict or T.in_districts(x, named))]
                await check_map(s, extra)
                pool += extra
                good = [x for x in pool if fits(x, s.prefs, map_ok)]
            want = wishes_text(s.prefs)
            if good:
                await s.say(f"Квартир {want}: {len(good)} из {len(pool)} подходящих по цене и району.")
                pool = good
                break
            # ни одной: смотрим, есть ли такие дороже — и честно говорим
            await s.emit("result", step=3, kind="search", data={"report": report, "total": len(listings), "matched": 0, "rows": []})
            higher_budget = int(profile["budget_max"] * 1.3 // 100_000 * 100_000)
            await s.say(f"В бюджете таких нет — проверяю квартиры до {T.fmt(higher_budget)}.")
            richer = {**profile, "budget_max": higher_budget}
            more_listings, _ = await sources.collect(richer)
            above = [x for x in more_listings if T.passes_hard_filters(x, richer) and x["price"] > profile["budget_max"]
                     and (not strict or T.in_districts(x, named))]
            await check_map(s, above)
            above = sorted((x for x in above if fits(x, s.prefs, map_ok)), key=lambda x: x["price"])
            question = f"Квартир {want} до {T.fmt(profile['budget_max'])} не нашёл (проверил {len(pool)} подходящих по цене)."
            chips = []
            if above:
                cheapest = above[0]["price"]
                question += (f" Есть дороже: {len(above)} шт., самая доступная — {T.fmt(cheapest)}, "
                             f"на {T.fmt(cheapest - profile['budget_max'])} больше бюджета.")
                chips.append(f"Бюджет до {T.mln(int(-(-cheapest // 100_000) * 100_000))}")
            else:
                question += f" И до {T.fmt(higher_budget)} таких тоже нет."
            if s.prefs.features or s.prefs.poi:
                chips.append("Показать без этих условий")
                if len(s.prefs.features) + len(s.prefs.poi) > 1:
                    chips += [f"Без условия: {T.FEATURES[k][0]}" for k in s.prefs.features]
                    chips += [f"Без условия: {geo.POI[k][0]}" for k in s.prefs.poi]
            if profile.get("okrug"):
                chips.append(f"Искать по всему городу ({profile['city']})")
            question += " Что меняем?"
        r = await s.ask("clarify", "Ничего не нашлось", question, chips=chips)
        text = r["data"].get("text", "")
        if T.wants_whole_city(text):
            profile["okrug"] = None
            profile["districts"] = []
        dropped = drop_wishes(text, s.prefs)
        profile = await parse_request(s, text, base=profile)
        # что клиент снял — не возвращаем, даже если модель снова увидела слово в ответе
        profile["features"] = [k for k in profile.get("features") or [] if T.FEATURES[k][0] not in dropped]
        profile["poi"] = [k for k in profile.get("poi") or [] if geo.POI[k][0] not in dropped]
        s.prefs.features, s.prefs.poi = list(profile["features"]), list(profile["poi"])
        profile["summary"] = T.profile_summary(profile)
        s.profile = profile
        s.save_profile()
        await s.emit("result", step=1, kind="profile", data=profile)
    else:
        await s.say("Так и не нашёл подходящих объявлений. Начните новый запрос с другими параметрами.")
        await s.emit("done", status="empty")
        return
    meta: dict[str, dict] = {}
    comps = list({x["id"]: x for x in [*sources.comparables(profile), *listings]}.values())
    await score_into(pool, comps, profile, meta)
    order = ranked(pool, meta, s.prefs)
    rows = [{"id": x["id"], "address": x["address"], "district": x.get("district"), "area": x["area"],
             "price": x["price"], **meta[x["id"]]["fair"]} for x in order[:8]]
    for r in report:  # с учётом страниц, догруженных ради района и пожеланий; из базы — отдельной строкой
        if r["status"] == "db":
            r["count"] = sum(1 for x in listings if x.get("stored_at"))
        else:
            r["count"] = sum(1 for x in listings if not x.get("stored_at") and bool(x.get("is_new")) == r.get("new", False))
    await s.emit("result", step=3, kind="search", data={"report": report, "total": len(listings), "matched": len(pool), "rows": rows,
                                                        "store": sources.store.stats(profile)})
    src =", ".join(f"{r['source']} — {r['count']}" + (" (сохранённый срез)" if r["status"] == "snapshot" else "") for r in report)
    over = sum(1 for x in pool if meta[x["id"]]["fair"]["overprice_pct"] > 1)
    await s.say(f"Нашёл объявлений: {src}. Под ваши параметры подходят {len(pool)}; "
                f"{over} из них дороже справедливой цены — там есть что сторговать.")
    await s.step(3, "done")

    # 4. Подбор с обратной связью
    await s.step(4, "active")
    by_id = {x["id"]: x for x in pool}
    shown: set[str] = set()
    districts = sorted({x["district"] for x in pool if x.get("district")})
    for round_no in range(1, MAX_SELECTION_ROUNDS + 1):
        before = (list(s.prefs.features), list(s.prefs.poi))
        for note in s.notes:
            await P.apply_text(s.prefs, note, s.llm, districts)
        s.notes.clear()
        pool = await narrow_to_wishes(s, pool, before, map_ok)  # «а рядом должен быть парк» — уже в подборке
        order = ranked(pool, meta, s.prefs)
        liked = [x for x in order if x["id"] in s.prefs.liked]
        fresh = [x for x in order if x["id"] not in shown and x["id"] not in s.prefs.disliked and x["id"] not in s.prefs.liked
                 and x.get("district") not in s.prefs.districts_minus]
        if len(fresh) < BATCH and page < 3:
            page += 1
            extra = [x for x in await sources.more(profile, page) if x["id"] not in by_id and T.passes_hard_filters(x, profile)
                     and (not strict or T.in_districts(x, named))]
            await check_map(s, extra)
            extra = [x for x in extra if fits(x, s.prefs, map_ok)]
            if extra:
                pool += extra
                comps += extra
                by_id.update({x["id"]: x for x in extra})
                await score_into(extra, comps, profile, meta)
                order = ranked(pool, meta, s.prefs)
                fresh = [x for x in order if x["id"] not in shown and x["id"] not in s.prefs.disliked and x["id"] not in s.prefs.liked
                 and x.get("district") not in s.prefs.districts_minus]
        batch = liked + fresh[:BATCH]
        shown |= {x["id"] for x in batch}
        s.shown = [card(x, meta, s.prefs) for x in batch]
        await s.emit("result", step=4, kind="selection", data={
            "round": round_no, "cards": s.shown, "liked": s.prefs.liked,
            "disliked": s.prefs.disliked, "learned": s.prefs.learned(), "remaining": len(fresh) - BATCH,
        })
        if round_no == 1:
            await s.say("Вот лучшие варианты. Отметьте «Нравится» или «Не то» — я учту это и подберу ещё. "
                        "Пожелания можно написать или сказать голосом. Когда будете готовы — позвоню по отмеченным.")
        r = await s.ask("selection", "Подборка", "Отметьте квартиры на экране подбора.")
        d = r["data"]
        s.prefs.liked = [i for i in d.get("liked", []) if i in by_id]
        s.prefs.disliked = [i for i in d.get("disliked", []) if i in by_id]
        if d.get("note"):
            before = (list(s.prefs.features), list(s.prefs.poi))
            parsed = await P.apply_text(s.prefs, d["note"], s.llm, districts)
            pool = await narrow_to_wishes(s, pool, before, map_ok)
            if parsed.get("budget_max"):
                await s.say("Бюджет меняю только через «красные линии» — начните новый запрос, если он другой.")
        P.learn(s.prefs, by_id)
        if d.get("action") == "proceed":
            break
        learned = s.prefs.learned()
        await s.say(("Учёл: " + "; ".join(learned) + ". " if learned else "") + "Подобрал ещё варианты.")
    candidates = [by_id[i] for i in s.prefs.liked] or ranked(pool, meta, s.prefs)[:3]
    candidates.sort(key=lambda x: meta[x["id"]]["total"], reverse=True)
    await s.step(4, "done")

    # 5. Проверка
    await s.step(5, "active")
    checks = {x["id"]: T.legal_check(x) for x in candidates}
    await s.emit("result", step=5, kind="legal", data={"checks": list(checks.values()),
                                                         "listings": [card(x, meta, s.prefs) for x in candidates]})
    warned = [x for x in candidates if checks[x["id"]]["status"] == "warn"]
    await s.say(f"Проверил {len(candidates)} {T.plural(len(candidates), 'объект', 'объекта', 'объектов')} по данным объявлений"
                + (f"; есть оговорки у {len(warned)} — покажу юристу." if warned else "; явных рисков не видно.")
                + " Выписки ЕГРН и проверку продавца закажу после выбора квартиры.")
    await s.step(5, "done")

    # 6–9 по кандидатам (если цена не устроила — следующий)
    for listing in candidates[:3]:
        await s.step(6, "active")
        # объявление могли снять или поменять, пока клиент выбирал: открываем страницу и берём свежие параметры
        page = await sources.verify(listing)
        if page["alive"] is False:
            await s.say(f"Объявление {listing['address']} сняли с публикации — звонить некуда, перехожу к следующему.")
            continue
        if page["listing"]:
            fresh = page.pop("listing")
            if fresh["price"] != listing["price"]:
                await s.say(f"Цена в объявлении изменилась: было {T.fmt(listing['price'])}, стало {T.fmt(fresh['price'])}. "
                            "Торгуюсь от новой.")
            listing.update(fresh)
            meta[listing["id"]]["fair"] = T.fair_price(listing, comps)
        else:
            page.pop("listing", None)
        fp = meta[listing["id"]]["fair"]
        authority = min(profile["budget_max"], listing["price"], fp["high"])
        seller = seller_sim.build(listing)
        questions = call_questions(listing, s.prefs)
        slots = T.client_slots()
        r = await s.ask(
            "call", "Звонок продавцу",
            f"Звоню по объекту {listing['address']}: уточню {', '.join(q['label'] for q in questions)}, "
            f"поторгуюсь до {T.fmt(fp['target'])} (без вашего согласия — не выше {T.fmt(authority)}) и договорюсь о просмотре.",
            listing=listing, fair=fp, authority=authority, arguments=T.bargaining_arguments(listing, fp),
            legal=checks[listing["id"]], questions=questions, seller_name=seller["name"], seller_role=seller["role"],
            simulated=True, page=page,
        )
        if not r["approved"]:
            await s.say("Пропускаю этот объект, перехожу к следующему.")
            continue
        mode = r["data"].get("mode") if r["data"].get("mode") in N.LIVE_MODES else "auto"
        neg = N.Negotiation(listing=listing, seller=seller, fair=fp, authority=authority, questions=questions,
                            slots=slots, mode=mode)
        s.journal = {"listing_id": listing["id"], "base_price": listing["price"], "fixed_at": date.today().isoformat(),
                     "url": listing.get("url")}
        s.current = {"listing": listing, "fair": fp}
        end = None
        while not s.seller_queue.empty():  # реплики от прошлого звонка не нужны
            s.seller_queue.get_nowait()
        human = (lambda: N.queue_input(s.seller_queue)) if neg.live else None
        async for ev in N.run(s.llm, neg, human_input=human):
            s.push(ev["type"], **{k: v for k, v in ev.items() if k != "type"})
            if ev["type"] == "call_end":
                end = ev
        if neg.used_fallback:
            await mark_degraded(s, LLMError("модель не ответила в звонке"))
        if end["learned"]:
            await s.say("Узнал у продавца: " + "; ".join(f"{k} — {v}" for k, v in end["learned"].items()) + ".")
        if end.get("corrections"):
            await apply_corrections(s, listing, end["corrections"], persist=neg.live)
        price = end["price"]
        savings = listing["price"] - price
        over_budget = price > profile["budget_max"]
        if end["status"] == "agreed":
            text = f"Продавец согласен на {T.fmt(price)} (было {T.fmt(listing['price'])}). Подтвердить предложение?"
        else:
            text = f"Продавец остановился на {T.fmt(price)} — это выше лимита, который вы мне дали. Согласиться всё равно?"
        if over_budget:
            text += f" Внимание: цена выше вашего бюджета {T.fmt(profile['budget_max'])}."
        r = await s.ask("price", "Подтвердите цену", text, price=price, savings=savings, base=listing["price"],
                        status=end["status"], over_budget=over_budget)
        if not r["approved"]:
            await s.step(6, "pending")
            await s.say("Понял, эта цена не подходит. Звоню по следующему объекту.")
            continue
        s.journal.update(final_price=price, savings=savings)
        await s.step(6, "done")

        # 7. Просмотр
        await s.step(7, "active")
        slot = end.get("viewing") or slots[0]
        await s.emit("result", step=7, kind="viewing", data={
            "listing": card(listing, meta, s.prefs), "slot": slot, "slots": slots, "learned": end["learned"],
            "checklist": T.viewing_checklist(listing),
        })
        await s.say(f"Просмотр: {slot}. Подготовил чек-лист осмотра с учётом того, что узнал у продавца.")
        r = await s.ask("object", "Выбор объекта", "После просмотра: выбираете эту квартиру?")
        if not r["approved"]:
            await s.step(7, "pending")
            await s.say("Хорошо, ищу дальше.")
            continue
        await s.step(7, "done")

        # 8. Оформление
        await s.step(8, "active")
        if profile.get("payment") == "cash":
            down = None
        else:
            down = s.finance["down_payment"] if s.finance else int(profile["budget_max"] * 0.2)
        # ставка — программы, которая подходит именно этой квартире (новостройка может пройти по семейной)
        pay = M.listing_payment(listing, profile, price=price, down_share=down / price) if down else None
        plan = T.deal_plan(listing, price, down, rate=pay["rate"] if pay else None, program=pay["title"] if pay else None)
        await s.emit("result", step=8, kind="deal", data=plan)
        if plan["cash"]:
            await s.say("Собрал план сделки без ипотеки: проверка продавца, договор с электронной подписью, "
                        "деньги через сервис безопасных расчётов, электронная регистрация — около 9 дней.")
        else:
            tail = ("Деньги — продавцу через СБР после регистрации уступки." if plan["assignment"]
                    else "Деньги — на эскроу-счёте до сдачи дома, ДДУ зарегистрируем электронно." if plan["new"]
                    else "Регистрация — электронно, деньги через СБР.")
            await s.say(f"Собрал план сделки: {plan['program'].lower()} {T.fmt(plan['loan'])}, платёж {T.fmt(plan['monthly'])} в месяц "
                        f"(на {T.fmt(plan['monthly_saved'])} меньше благодаря торгу). {tail}")
        contract = ("договор уступки прав по ДДУ" if plan["assignment"]
                    else "договор участия в долевом строительстве (ДДУ)" if plan["new"] else "договор купли-продажи")
        r = await s.ask("sign", "Подписание договора", f"Подписать {contract} электронной подписью?")
        if not r["approved"]:
            await s.say("Подписание отложено. Все документы сохранены — вернёмся, когда будете готовы.")
            await s.emit("done", status="paused")
            return
        await s.step(8, "done")

        # 9. Итог
        await s.step(9, "active")
        econ = economics.unit(economics.Inputs(price=listing["price"], savings_pct=max(0, savings) / listing["price"],
                                               ltv=0 if plan["cash"] else 0.8))
        fin_lines = F.bank_value(s.finance or {}, s.opened)
        summary = {
            "listing": listing,
            "base_price": listing["price"], "final_price": price, "savings": savings,
            "fee": round(max(0, savings) * 0.15), "net": round(max(0, savings) * 0.85),
            "monthly_saved": plan["monthly_saved"], "hours_saved": "60–70 ч",
            "sber": {"revenue": econ["revenue"], "contribution": econ["contribution"], "lines": econ["lines"],
                     "finance": fin_lines, "finance_total": sum(x["value"] for x in fin_lines)},
            "next_events": ["Ремонт и обустройство", "Переезд", "Страхование на следующий год", "Рефинансирование"],
        }
        await s.emit("result", step=9, kind="summary", data=summary)
        await s.say(f"Готово! Экономия {T.fmt(savings)}, плата агенту {T.fmt(summary['fee'])} (15% от экономии), "
                    f"вам остаётся {T.fmt(summary['net'])}. Когда получите ключи — помогу с ремонтом и переездом.")
        await s.step(9, "done")
        await s.emit("done", status="completed")
        return

    await s.say("Отмеченные объекты закончились. Вернёмся к подбору?")
    await s.emit("done", status="exhausted")


async def run_session(s: Session) -> None:
    try:
        if s.answer_only:
            await s.emit("message", role="user", text=s.query)
            await qa.answer(s, s.query)
            s.push("idle")  # агент ответил и снова ждёт — без нового поиска
            db.safe(repo.update_chat, s.id, status=s.prev_status)
            return
        await pipeline(s)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # показать ошибку в UI, а не молча упасть
        log.exception("pipeline failed")
        await s.emit("error", text=f"Ошибка агента: {exc}")
