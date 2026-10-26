"""Источники объявлений. Агент вызывает collect() и не знает деталей площадок.

Сейчас подключён Циан (список — переменная SOURCES в .env). Механизм готов к нескольким
площадкам: они опрашиваются параллельно, а одна и та же квартира с разных сайтов склеивается
в одну карточку со списком площадок и цен (dedup.py). Новая площадка — модуль с search(),
load_snapshot(), supported(), SOURCE, SOURCE_NAME и запись в REGISTRY. У каждой площадки свой путь отката:
живой запрос -> кеш сессии -> сохранённый срез (тоже реальные объявления).
Синтетики нет: если данных нет, агент так и говорит.
"""
from __future__ import annotations

import asyncio

from . import cian, dedup
from .base import SourceUnavailable, description_flags, normalize_city

__all__ = ["collect", "markets", "more", "comparables", "market", "enabled", "names", "normalize_city",
           "description_flags", "SourceUnavailable", "cian", "dedup", "SUPPORTED_CITIES"]

REGISTRY = {"cian": cian}
SUPPORTED_CITIES = list(cian.REGIONS)


def enabled(city: str | None = None) -> list:
    """Включённые площадки (SOURCES в .env), которые знают этот город."""
    from ..config import settings
    keys = [k.strip() for k in settings.sources.split(",") if k.strip() in REGISTRY] or ["cian"]
    return [REGISTRY[k] for k in keys if city is None or REGISTRY[k].supported(city)]


def names(city: str | None = None) -> list[str]:
    return [s.SOURCE_NAME for s in enabled(city)]


def markets(profile: dict) -> list[bool]:
    """Где искать: [False] — вторичка, [True] — новостройки, оба — по выбору клиента."""
    h = profile.get("housing")
    return [True] if h == "new" else [False, True] if h == "both" else [False]


def _label(src, new: bool, profile: dict) -> str:
    return src.SOURCE_NAME + (" · новостройки" if new else " · вторичка" if profile.get("housing") == "both" else "")


def _from_db(src, new: bool, profile: dict) -> tuple[list[dict], str] | None:
    """Свежая выдача из базы (обход был меньше LISTINGS_FRESH_HOURS назад) — без запроса к площадке."""
    from ..config import settings
    from ..db import repo
    found = repo.record(repo.fresh_listings, profile["city"], profile["rooms"], new, profile.get("okrug"),
                        profile.get("budget_max"), settings.listings_fresh_hours, src.SOURCE)
    if not found:
        return None
    items, at = found
    return items, at.strftime("%d.%m %H:%M") + " UTC"


def _to_db(src, new: bool, profile: dict, items: list[dict], crawled: bool) -> None:
    """Сохранить выдачу в базу и запомнить поиск для ежедневного обхода."""
    from ..db import repo

    def save(db):
        repo.upsert_listings(db, items)
        t = repo.track_search(db, profile["city"], profile["rooms"], new, profile.get("okrug"),
                              profile.get("budget_max"), src.SOURCE)
        if crawled:
            repo.mark_crawled(db, t, profile.get("budget_max"))
    repo.record(save)


async def collect(profile: dict) -> tuple[list[dict], list[dict]]:
    """Возвращает (объявления, отчёт по площадкам): все площадки × вторичка/новостройки, дубли склеены.

    Сначала база: если этот поиск недавно обходили — берём оттуда (не тратим запросы и время).
    Иначе — живой запрос к площадке; результат сохраняем в базу, а поиск ставим в ежедневный обход."""
    jobs = [(src, new) for src in enabled(profile.get("city")) for new in markets(profile)]

    async def one(src, new):
        label = _label(src, new, profile)
        if not cian.demo_mode():
            stored = _from_db(src, new, profile)
            if stored:
                items = cian.drop_outliers(stored[0])
                return items, {"source": label, "count": len(items), "status": "db", "note": f"из базы, обход {stored[1]}"}
        try:
            items, rep = await src.search(profile.get("city"), profile["rooms"], profile["budget_max"], 1,
                                          profile.get("okrug"), new)
            _to_db(src, new, profile, items, crawled=rep["status"] == "live")
            items = cian.drop_outliers(items)
            return items, {"source": label, "count": len(items), **rep}
        except SourceUnavailable as exc:
            return [], {"source": label, "count": 0, "status": "unavailable", "note": str(exc)}

    results = await asyncio.gather(*(one(s, n) for s, n in jobs))
    items_all = [x for items, _ in results for x in items]
    return dedup.merge(items_all), [rep for _, rep in results]


async def market(profile: dict) -> dict | None:
    """Цены рынка по запросу (без бюджета) — чтобы клиенту было от чего оттолкнуться (по Циану)."""
    if not cian.supported(profile.get("city")) or profile.get("rooms") is None:
        return None
    return await cian.market(profile["city"], profile["rooms"], profile.get("okrug"), profile.get("housing") == "new")


def comparables(profile: dict) -> list[dict]:
    """Все сохранённые объявления того же города и комнатности (срез + база) — основа справедливой цены."""
    from ..db import repo
    out: dict[str, dict] = {}
    for src in enabled(profile.get("city")):
        for new in markets(profile):
            for x in src.load_snapshot(profile["city"], profile["rooms"], new=new)[0]:
                out[x["id"]] = x
            if not cian.demo_mode():
                for x in repo.record(repo.active_listings, src.SOURCE, profile["city"], profile["rooms"], new,
                                     default=[]):
                    out[x["id"]] = x
    return dedup.merge(list(out.values()))


async def more(profile: dict, page: int) -> list[dict]:
    """Следующая страница живой выдачи со всех площадок, которые отвечают."""
    out = []
    for src in enabled(profile.get("city")):
        for new in markets(profile):
            try:
                items = (await src.search(profile.get("city"), profile["rooms"], profile["budget_max"], page,
                                          profile.get("okrug"), new))[0]
                _to_db(src, new, profile, items, crawled=False)
                out += items
            except SourceUnavailable:
                pass
    return dedup.merge(out)


async def verify(items: list[dict], max_age_min: int | None = None, budget_s: float | None = None) -> dict[str, dict]:
    """Перед показом клиенту: открыть страницы объявлений и убедиться, что они не сняты.

    Возвращает {id: {"alive": True|False|None, "note", "checked_at"}}. Недавние проверки (LISTING_CHECK_TTL_MIN)
    берутся из базы. Если площадка не ответила за LISTING_CHECK_BUDGET_S — «не удалось проверить» (alive None):
    такое объявление показываем с пометкой, а не прячем. Снятые помечаются в базе как removed."""
    from ..config import settings
    from ..db import repo

    ttl = settings.listing_check_ttl_min if max_age_min is None else max_age_min
    budget = settings.listing_check_budget_s if budget_s is None else budget_s
    out: dict[str, dict] = {}
    if ttl > 0:
        out.update(repo.record(repo.recent_page_checks, [x["id"] for x in items], ttl, default={}) or {})
    todo = [x for x in items if x["id"] not in out]
    if not todo:
        return out
    if cian.demo_mode():
        for x in todo:
            out[x["id"]] = {"alive": None, "note": "демо-режим: страницы не проверяются", "checked_at": None}
        return out

    async def one(x):
        src = REGISTRY.get(x.get("source"), cian)
        alive, note = await src.check_listing_page(x.get("url"))
        repo.record(repo.save_page_check, x, alive, note)
        out[x["id"]] = {"alive": alive, "note": note, "checked_at": repo.utcnow().isoformat() + "Z"}

    tasks = [asyncio.create_task(one(x)) for x in todo]
    done, pending = await asyncio.wait(tasks, timeout=budget)
    for t in pending:
        t.cancel()
    for x in todo:
        out.setdefault(x["id"], {"alive": None, "note": "площадка не ответила вовремя", "checked_at": None})
    return out
