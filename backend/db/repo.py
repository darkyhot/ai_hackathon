"""Запросы к базе. Агент и HTTP-слой работают с базой только через этот модуль.

Функции принимают открытую сессию SQLAlchemy (db). Для вызовов из агента есть record():
он открывает сессию сам и не роняет агента, если база недоступна — запись просто пропускается.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from .models import (AgentSession, Call, CallFact, Complex, CrawlRun, Deal, Developer, Event, Feature,
                     FinanceProfile, Listing, ListingFeature, ListingPhoto, ListingPrice, SecurityCheck,
                     TrackedSearch, User, UserListingMark, Visitor, utcnow)

log = logging.getLogger(__name__)

# поля формата агента, которые лежат в отдельных колонках (остальное — в data)
COLUMNS = ["url", "city", "district", "okrug", "address", "lat", "lng", "rooms", "area", "kitchen", "floor", "floors",
           "year", "condition", "material", "price", "description", "seller_role", "rosreestr_checked", "sale_type"]
TRANSIENT = {"offers", "sources", "fair", "score", "pay", "missing", "total"}  # добавляет агент, в базе не храним


def record(fn: Callable, *args, default: Any = None, **kwargs) -> Any:
    """Вызвать fn(db, ...) в своей транзакции. Ошибка базы не ломает агента: пишем в лог и возвращаем default."""
    from . import session
    try:
        with session() as db:
            return fn(db, *args, **kwargs)
    except Exception as exc:  # база — вспомогательная для агента: сессия с клиентом важнее
        log.warning("база: %s не записано: %s", getattr(fn, "__name__", fn), exc)
        return default


# ================================================================ квартиры


def _split_id(listing_id: str) -> tuple[str, str]:
    source, _, ext = listing_id.partition("-")
    return source, ext or listing_id


def _complex(db: Session, x: dict) -> Complex | None:
    if not x.get("is_new") or not x.get("complex"):
        return None
    c = db.scalar(select(Complex).where(Complex.city == x["city"], Complex.name == x["complex"]))
    if c is None:
        c = Complex(city=x["city"], name=x["complex"])
        db.add(c)
    if x.get("developer"):
        d = db.scalar(select(Developer).where(Developer.name == x["developer"]))
        if d is None:
            d = Developer(name=x["developer"])
            db.add(d)
        c.developer = d
    c.deadline = x.get("deadline") or c.deadline
    c.finished = x.get("finished") if x.get("finished") is not None else c.finished
    return c


def _photos(db: Session, l: Listing, x: dict) -> None:
    urls = list(x.get("photos") or ([x["photo"]] if x.get("photo") else []))
    have = {p.url: p for p in l.photos}
    for i, url in enumerate(urls[:20]):
        if url in have:
            have[url].position = i
        else:
            l.photos.append(ListingPhoto(url=url, position=i))


def _features(db: Session, l: Listing, x: dict, source: str = "cian") -> None:
    from ..features import compute
    current = {f.feature_key: f for f in l.features if f.source == source}
    for v in compute(x):
        if v.source != source:
            continue
        row = current.get(v.key) or ListingFeature(feature_key=v.key, source=source)
        for k, val in v.columns().items():
            setattr(row, k, val)
        row.confidence, row.evidence, row.observed_at = v.confidence, v.evidence, utcnow()
        if row.id is None and row not in l.features:
            l.features.append(row)


def upsert_listings(db: Session, items: Iterable[dict], now: datetime | None = None) -> dict:
    """Сохранить объявления: новые — добавить, известные — обновить, цену — в историю, снятые — вернуть в продажу."""
    now = now or utcnow()
    stats = {"seen": 0, "added": 0, "updated": 0, "price_changed": 0}
    for raw in items:
        # склеенные дубли (offers) и оценки агента не храним: в базе — объявление площадки как есть
        x = {k: v for k, v in raw.items() if k not in TRANSIENT}
        stats["seen"] += 1
        l = db.get(Listing, x["id"])
        if l is None:
            source, ext = _split_id(x["id"])
            l = Listing(id=x["id"], source=x.get("source") or source, external_id=ext, first_seen_at=now)
            l.photos, l.prices, l.features = [], [], []
            db.add(l)
            stats["added"] += 1
            l.prices.append(ListingPrice(price=int(x["price"]), observed_at=now))
        else:
            if l.price != int(x["price"]):
                stats["price_changed"] += 1
                l.prices.append(ListingPrice(price=int(x["price"]), observed_at=now))
            if (l.data or {}) != x:
                stats["updated"] += 1
        changed = l.data != x
        for col in COLUMNS:
            setattr(l, col, x.get(col))
        metro = x.get("metro") or {}
        l.metro_name, l.metro_minutes, l.metro_mode = metro.get("name"), metro.get("minutes"), metro.get("mode")
        l.is_new = bool(x.get("is_new"))
        l.is_apartments, l.is_auction = bool(x.get("is_apartments")), bool(x.get("is_auction"))
        if x.get("published_at"):
            l.published_at = datetime.fromisoformat(x["published_at"])
        elif l.published_at is None and x.get("days_on_market") is not None:
            l.published_at = now - timedelta(days=int(x["days_on_market"]))
        l.complex = _complex(db, x)
        l.data = x
        l.status, l.removed_at, l.miss_count, l.last_seen_at = "active", None, 0, now
        if changed:
            l.updated_at = now
        _photos(db, l, x)
        _features(db, l, x)
    db.flush()
    return stats


def listing_dict(l: Listing, today: date | None = None) -> dict:
    """Объявление в формате агента: данные + актуальные цена и срок в продаже."""
    x = dict(l.data)
    x["price"] = l.price
    if l.published_at:
        x["days_on_market"] = max(0, ((today or date.today()) - l.published_at.date()).days)
    if len(l.prices) > 1:
        first = l.prices[0].price
        x["price_history"] = [{"price": p.price, "date": p.observed_at.date().isoformat()} for p in l.prices]
        if l.price < first:
            x["price_drop"] = first - l.price  # снижение с первого появления — аргумент в торге
    x["first_seen"] = l.first_seen_at.date().isoformat()
    return x


def get_listing(db: Session, listing_id: str) -> Listing | None:
    return db.get(Listing, listing_id)


def _scope(q, city: str, rooms: int, is_new: bool, okrug: str | None, budget: int | None):
    q = q.where(Listing.city == city, Listing.rooms == rooms, Listing.is_new == is_new)
    if okrug:
        q = q.where(Listing.okrug == okrug)
    if budget:
        q = q.where(Listing.price <= int(budget * 1.05))
    return q


def active_listings(db: Session, source: str, city: str, rooms: int, is_new: bool, limit: int = 500) -> list[dict]:
    q = _scope(select(Listing), city, rooms, is_new, None, None).where(Listing.status == "active", Listing.source == source)
    return [listing_dict(l) for l in db.scalars(q.order_by(Listing.last_seen_at.desc()).limit(limit)).all()]


def track_search(db: Session, city: str, rooms: int, is_new: bool, okrug: str | None, budget: int | None,
                 source: str = "cian") -> TrackedSearch:
    """Запомнить поиск: его будет обновлять ежедневный обход."""
    t = db.scalar(select(TrackedSearch).where(TrackedSearch.source == source, TrackedSearch.city == city,
                                              TrackedSearch.rooms == rooms, TrackedSearch.is_new == is_new,
                                              TrackedSearch.okrug == (okrug or "")))
    if t is None:
        t = TrackedSearch(source=source, city=city, rooms=rooms, is_new=is_new, okrug=okrug or "", budget_max=budget,
                          requested_count=0)
        db.add(t)
    t.requested_count = (t.requested_count or 0) + 1
    t.last_requested_at = utcnow()
    t.active = True
    db.flush()
    return t


def fresh_listings(db: Session, city: str, rooms: int, is_new: bool, okrug: str | None, budget: int | None,
                   max_age_hours: int, source: str = "cian") -> tuple[list[dict], datetime] | None:
    """Свежая выдача из базы — если этот поиск обходили недавно и с бюджетом не меньше нужного. Иначе None."""
    since = utcnow() - timedelta(hours=max_age_hours)
    candidates = db.scalars(select(TrackedSearch).where(
        TrackedSearch.source == source, TrackedSearch.city == city, TrackedSearch.rooms == rooms,
        TrackedSearch.is_new == is_new, TrackedSearch.okrug.in_([okrug or "", ""]),
        TrackedSearch.last_crawled_at >= since)).all()
    covering = [t for t in candidates if t.budget_max is None or (budget is not None and budget <= t.budget_max)]
    if not covering:
        return None
    q = _scope(select(Listing), city, rooms, is_new, okrug, budget).where(
        Listing.status == "active", Listing.source == source).order_by(Listing.price).limit(300)
    rows = db.scalars(q).all()
    if not rows:
        return None
    return [listing_dict(l) for l in rows], max(t.last_crawled_at for t in covering)


def mark_crawled(db: Session, t: TrackedSearch, budget: int | None, now: datetime | None = None) -> None:
    t.last_crawled_at = now or utcnow()
    if t.budget_max is not None:
        t.budget_max = None if budget is None else max(t.budget_max, budget)


def sweep_missing(db: Session, t: TrackedSearch, seen_ids: set[str], threshold: int,
                  page_check: Callable[[Listing], bool | None] | None = None) -> int:
    """Объявления этого поиска, которых не было в полном обходе: +1 пропуск; после threshold — снято с продажи.

    page_check(listing) -> True (страница жива) / False (снято) / None (не знаем) — проверка страницы объявления;
    сейчас заглушка (None), тогда решаем по числу пропусков."""
    q = _scope(select(Listing), t.city, t.rooms, t.is_new, t.okrug or None, t.budget_max).where(
        Listing.status == "active", Listing.source == t.source)
    removed = 0
    now = utcnow()
    for l in db.scalars(q).all():
        if l.id in seen_ids:
            continue
        l.miss_count = (l.miss_count or 0) + 1
        alive = page_check(l) if page_check else None
        if alive is False or (alive is None and l.miss_count >= threshold):
            l.status, l.removed_at = "removed", now
            removed += 1
        elif alive is True:
            l.miss_count = 0  # страница жива — просто выпало из первых страниц выдачи
    return removed


def tracked_searches(db: Session) -> list[TrackedSearch]:
    return db.scalars(select(TrackedSearch).where(TrackedSearch.active.is_(True))
                      .order_by(TrackedSearch.requested_count.desc())).all()


def start_crawl(db: Session, trigger: str) -> CrawlRun:
    run = CrawlRun(trigger=trigger, errors=[])
    db.add(run)
    db.flush()
    return run


def recent_crawls(db: Session, limit: int = 10) -> list[dict]:
    rows = db.scalars(select(CrawlRun).order_by(CrawlRun.id.desc()).limit(limit)).all()
    return [{"id": r.id, "trigger": r.trigger, "status": r.status, "started_at": r.started_at.isoformat(timespec="seconds"),
             "finished_at": r.finished_at.isoformat(timespec="seconds") if r.finished_at else None,
             "searches": r.searches, "seen": r.seen, "added": r.added, "updated": r.updated,
             "price_changed": r.price_changed, "removed": r.removed, "errors": r.errors} for r in rows]


# ================================================================ посетители, журнал, пользователи


def touch_visitor(db: Session, visitor_id: str, user_agent: str | None = None) -> Visitor:
    v = db.get(Visitor, visitor_id)
    now = utcnow()
    if v is None:
        v = Visitor(id=visitor_id, first_seen_at=now, last_seen_at=now, user_agent=(user_agent or "")[:300] or None)
        db.add(v)
    elif now - v.last_seen_at > timedelta(minutes=5):
        v.last_seen_at = now
    db.flush()
    return v


def log_event(db: Session, type_: str, payload: dict | None = None, visitor_id: str | None = None,
              user_id: int | None = None, agent_session_id: str | None = None) -> None:
    if visitor_id and db.get(Visitor, visitor_id) is None:
        touch_visitor(db, visitor_id)
    if user_id is None and visitor_id:
        v = db.get(Visitor, visitor_id)
        user_id = v.user_id if v else None
    db.add(Event(type=type_, payload=payload, visitor_id=visitor_id, user_id=user_id, agent_session_id=agent_session_id))


CLAIMABLE = [Event, AgentSession, UserListingMark, Call, FinanceProfile, Deal]


def claim_visitor(db: Session, visitor_id: str, user_id: int) -> dict:
    """Пользователь вошёл: всё, что посетитель делал анонимно, теперь принадлежит ему."""
    touch_visitor(db, visitor_id)
    db.get(Visitor, visitor_id).user_id = user_id
    counts = {}
    for model in CLAIMABLE:
        res = db.execute(update(model).where(model.visitor_id == visitor_id, model.user_id.is_(None))
                         .values(user_id=user_id))
        counts[model.__tablename__] = res.rowcount or 0
    return counts


def history(db: Session, user_id: int | None, visitor_id: str | None, limit: int = 20) -> dict:
    """Что пользователь делал: сессии с агентом, отметки, звонки, число событий."""
    def who(model):
        conds = []
        if user_id is not None:
            conds.append(model.user_id == user_id)
        if visitor_id:
            conds.append(model.visitor_id == visitor_id)
        return or_(*conds) if conds else model.id.is_(None)

    sessions = db.scalars(select(AgentSession).where(who(AgentSession)).order_by(AgentSession.started_at.desc())
                          .limit(limit)).all()
    marks = db.execute(select(UserListingMark.mark, func.count()).where(who(UserListingMark))
                       .group_by(UserListingMark.mark)).all()
    liked = db.scalars(select(UserListingMark.listing_id).where(who(UserListingMark), UserListingMark.mark == "like")
                       .order_by(UserListingMark.created_at.desc()).limit(12)).all()
    liked_rows = [listing_dict(l) for l in (db.get(Listing, i) for i in dict.fromkeys(liked)) if l]
    return {
        "sessions": [{"id": s.id, "query": s.query, "status": s.status, "started_at": s.started_at.isoformat(timespec="minutes"),
                      "summary": (s.profile or {}).get("summary"), "result": s.result} for s in sessions],
        "marks": {m: n for m, n in marks},
        "liked": [{"id": x["id"], "address": x.get("address"), "price": x["price"], "area": x.get("area"),
                   "url": x.get("url"), "photo": x.get("photo")} for x in liked_rows],
        "events": db.scalar(select(func.count()).select_from(Event).where(who(Event))) or 0,
        "calls": db.scalar(select(func.count()).select_from(Call).where(who(Call))) or 0,
    }


# ================================================================ сессии агента


def start_agent_session(db: Session, sid: str, query: str, visitor_id: str | None, user_id: int | None,
                        provider: str | None) -> None:
    if visitor_id:
        touch_visitor(db, visitor_id)
    db.add(AgentSession(id=sid, query=query, visitor_id=visitor_id, user_id=user_id, llm_provider=provider))
    db.flush()
    log_event(db, "session_started", {"query": query}, visitor_id, user_id, sid)


def update_agent_session(db: Session, sid: str, **fields) -> None:
    s = db.get(AgentSession, sid)
    if s is None:
        return
    for k, v in fields.items():
        setattr(s, k, v)
    if fields.get("status") and fields["status"] != "running":
        s.finished_at = utcnow()


def save_finance_profile(db: Session, visitor_id: str | None, user_id: int | None, profile: dict,
                         inputs: dict | None) -> None:
    fp = FinanceProfile(visitor_id=visitor_id, user_id=user_id, payment=profile.get("payment"),
                        children=profile.get("children"), youngest_age=profile.get("youngest_age"),
                        matcap=profile.get("matcap") or None)
    if inputs:
        fp.monthly_income = inputs.get("monthly_income")
        fp.monthly_expenses = inputs.get("monthly_expenses")
        fp.savings = inputs.get("savings")
    db.add(fp)


def save_marks(db: Session, sid: str, visitor_id: str | None, user_id: int | None, marks: dict[str, Iterable[str]]) -> None:
    for mark, ids in marks.items():
        for lid in ids:
            if db.get(Listing, lid) is None:
                continue
            exists = db.scalar(select(UserListingMark.id).where(UserListingMark.visitor_id == visitor_id,
                                                                UserListingMark.listing_id == lid,
                                                                UserListingMark.mark == mark))
            if not exists:
                db.add(UserListingMark(visitor_id=visitor_id, user_id=user_id, listing_id=lid, mark=mark,
                                       agent_session_id=sid))


# ================================================================ звонки и факты


SELLER_FEATURE = {"deadline": "deadline_call"}  # ключ вопроса -> признак, если в каталоге он называется иначе


def save_call(db: Session, *, sid: str | None, visitor_id: str | None, user_id: int | None, listing: dict,
              mode: str, seller_role: str | None, transcript: list[dict], status: str | None,
              asking: int | None, agreed: int | None, viewing: str | None, analytics: dict,
              facts: list[dict]) -> int:
    if db.get(Listing, listing["id"]) is None:
        upsert_listings(db, [listing])
    call = Call(agent_session_id=sid, listing_id=listing["id"], visitor_id=visitor_id, user_id=user_id, mode=mode,
                seller_role=seller_role, transcript=transcript, status=status, asking_price=asking,
                agreed_price=agreed, viewing_slot=viewing, analytics=analytics, finished_at=utcnow())
    db.add(call)
    db.flush()
    keys = {k for (k,) in db.execute(select(Feature.key)).all()}
    for f in facts:
        cf = CallFact(call_id=call.id, listing_id=listing["id"], **{k: f.get(k) for k in (
            "key", "label", "value", "quote", "confidence", "level", "hedged", "contradicts_listing",
            "changed_during_call", "reasons", "verify_with")})
        db.add(cf)
        db.flush()
        fkey = SELLER_FEATURE.get(f["key"], f["key"])
        if fkey in keys:
            row = db.scalar(select(ListingFeature).where(ListingFeature.listing_id == listing["id"],
                                                         ListingFeature.feature_key == fkey,
                                                         ListingFeature.source == "seller_call"))
            row = row or ListingFeature(listing_id=listing["id"], feature_key=fkey, source="seller_call")
            num = f.get("value_num")
            row.value_num, row.value_text = (float(num) if num is not None else None), f["value"]
            row.confidence, row.evidence, row.call_fact_id, row.observed_at = (
                f["confidence"], f.get("quote"), cf.id, utcnow())
            db.add(row)
    if analytics.get("bargain"):  # на сколько продавец уступил — признак для подбора и аналитики рынка
        row = db.scalar(select(ListingFeature).where(ListingFeature.listing_id == listing["id"],
                                                     ListingFeature.feature_key == "bargain_room",
                                                     ListingFeature.source == "seller_call"))
        row = row or ListingFeature(listing_id=listing["id"], feature_key="bargain_room", source="seller_call")
        row.value_num, row.confidence, row.observed_at = float(analytics["bargain"]), 0.6, utcnow()
        row.evidence = "устная договорённость в звонке"
        db.add(row)
    return call.id


def listing_facts(db: Session, listing_id: str) -> list[dict]:
    """Факты о квартире из звонков: по каждому ключу — последний, плюс сколько раз и одинаково ли отвечали."""
    rows = db.scalars(select(CallFact).where(CallFact.listing_id == listing_id)
                      .order_by(CallFact.created_at.desc(), CallFact.id.desc())).all()
    groups: dict[str, list[CallFact]] = {}
    for f in rows:
        groups.setdefault(f.key, []).append(f)
    return [{**_fact(g[0]), "times": len(g), "consistent": len({x.value.lower() for x in g}) == 1,
             "previous": [x.value for x in g[1:4] if x.value.lower() != g[0].value.lower()]} for g in groups.values()]


def _fact(f: CallFact) -> dict:
    return {"id": f.id, "key": f.key, "label": f.label, "value": f.value, "quote": f.quote, "confidence": f.confidence,
             "level": f.level, "hedged": f.hedged, "contradicts_listing": f.contradicts_listing,
             "changed_during_call": f.changed_during_call, "reasons": f.reasons or [], "verify_with": f.verify_with,
             "verified_status": f.verified_status, "date": f.created_at.date().isoformat()}


# ================================================================ безопасность и сделка


def save_security(db: Session, listing: dict, checks: list[dict], ttl_days: int = 30) -> None:
    if db.get(Listing, listing["id"]) is None:
        upsert_listings(db, [listing])
    now = utcnow()
    for c in checks:
        row = db.scalar(select(SecurityCheck).where(SecurityCheck.listing_id == listing["id"],
                                                    SecurityCheck.check_key == c["key"]))
        row = row or SecurityCheck(listing_id=listing["id"], check_key=c["key"])
        row.title, row.status, row.method = c["title"], c["status"], c.get("method")
        row.source, row.source_url = c.get("source"), c.get("source_url")
        row.evidence = {"items": c.get("evidence") or []}
        row.checked_at, row.expires_at = now, now + timedelta(days=ttl_days)
        db.add(row)


def save_deal(db: Session, *, sid: str | None, visitor_id: str | None, user_id: int | None, listing: dict,
              final_price: int, plan: dict, status: str) -> None:
    db.add(Deal(agent_session_id=sid, visitor_id=visitor_id, user_id=user_id, listing_id=listing["id"],
                base_price=listing["price"], final_price=final_price, program=plan.get("program"), plan=plan,
                status=status))


# ================================================================ админка


def stats(db: Session) -> dict:
    def count(model, *where):
        return db.scalar(select(func.count()).select_from(model).where(*where)) or 0

    return {
        "listings_active": count(Listing, Listing.status == "active"),
        "listings_removed": count(Listing, Listing.status == "removed"),
        "photos": count(ListingPhoto),
        "features": count(ListingFeature),
        "tracked_searches": count(TrackedSearch, TrackedSearch.active.is_(True)),
        "users": count(User),
        "visitors": count(Visitor),
        "events": count(Event),
        "agent_sessions": count(AgentSession),
        "calls": count(Call),
        "call_facts": count(CallFact),
        "crawls": recent_crawls(db, 5),
    }
