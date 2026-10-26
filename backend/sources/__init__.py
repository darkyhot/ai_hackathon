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


async def collect(profile: dict) -> tuple[list[dict], list[dict]]:
    """Возвращает (объявления, отчёт по площадкам): все площадки × вторичка/новостройки, дубли склеены."""
    jobs = [(src, new) for src in enabled(profile.get("city")) for new in markets(profile)]

    async def one(src, new):
        try:
            items, rep = await src.search(profile.get("city"), profile["rooms"], profile["budget_max"], 1,
                                          profile.get("okrug"), new)
            items = cian.drop_outliers(items)
            return items, {"source": _label(src, new, profile), "count": len(items), **rep}
        except SourceUnavailable as exc:
            return [], {"source": _label(src, new, profile), "count": 0, "status": "unavailable", "note": str(exc)}

    results = await asyncio.gather(*(one(s, n) for s, n in jobs))
    items_all = [x for items, _ in results for x in items]
    return dedup.merge(items_all), [rep for _, rep in results]


async def market(profile: dict) -> dict | None:
    """Цены рынка по запросу (без бюджета) — чтобы клиенту было от чего оттолкнуться (по Циану)."""
    if not cian.supported(profile.get("city")) or profile.get("rooms") is None:
        return None
    return await cian.market(profile["city"], profile["rooms"], profile.get("okrug"), profile.get("housing") == "new")


def comparables(profile: dict) -> list[dict]:
    """Все сохранённые объявления того же города и комнатности — база для справедливой цены."""
    out = []
    for src in enabled(profile.get("city")):
        for new in markets(profile):
            out += src.load_snapshot(profile["city"], profile["rooms"], new=new)[0]
    return dedup.merge(out)


async def more(profile: dict, page: int) -> list[dict]:
    """Следующая страница живой выдачи со всех площадок, которые отвечают."""
    out = []
    for src in enabled(profile.get("city")):
        for new in markets(profile):
            try:
                out += (await src.search(profile.get("city"), profile["rooms"], profile["budget_max"], page,
                                         profile.get("okrug"), new))[0]
            except SourceUnavailable:
                pass
    return dedup.merge(out)
