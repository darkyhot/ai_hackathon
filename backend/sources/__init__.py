"""Источники объявлений. Агент вызывает collect() и не знает деталей площадок.

Сейчас подключён Циан: живой запрос -> кеш сессии -> сохранённый срез
(тоже реальные объявления Циана). Синтетики нет: если данных нет, агент так и говорит.
"""
from __future__ import annotations

from . import cian
from .base import SourceUnavailable, description_flags, normalize_city

__all__ = ["collect", "markets", "more", "comparables", "market", "normalize_city", "description_flags", "SourceUnavailable", "cian", "SUPPORTED_CITIES"]

SUPPORTED_CITIES = list(cian.REGIONS)


def markets(profile: dict) -> list[bool]:
    """Где искать: [False] — вторичка, [True] — новостройки, оба — по выбору клиента."""
    h = profile.get("housing")
    return [True] if h == "new" else [False, True] if h == "both" else [False]


async def collect(profile: dict) -> tuple[list[dict], list[dict]]:
    """Возвращает (объявления, отчёт по площадкам) — по вторичке и/или новостройкам."""
    items_all, report = [], []
    for new in markets(profile):
        label = cian.SOURCE_NAME + (" · новостройки" if new else " · вторичка" if profile.get("housing") == "both" else "")
        try:
            items, rep = await cian.search(profile.get("city"), profile["rooms"], profile["budget_max"], 1, profile.get("okrug"), new)
            items = cian.drop_outliers(items)
            items_all += items
            report.append({"source": label, "count": len(items), **rep})
        except SourceUnavailable as exc:
            report.append({"source": label, "count": 0, "status": "unavailable", "note": str(exc)})
    return items_all, report


async def market(profile: dict) -> dict | None:
    """Цены рынка по запросу (без бюджета) — чтобы клиенту было от чего оттолкнуться."""
    if not cian.supported(profile.get("city")) or profile.get("rooms") is None:
        return None
    return await cian.market(profile["city"], profile["rooms"], profile.get("okrug"), profile.get("housing") == "new")


def comparables(profile: dict) -> list[dict]:
    """Все сохранённые объявления того же города и комнатности — база для справедливой цены."""
    if not cian.supported(profile.get("city")):
        return []
    out = []
    for new in markets(profile):
        out += cian.load_snapshot(profile["city"], profile["rooms"], new=new)[0]
    return out


async def more(profile: dict, page: int) -> list[dict]:
    """Следующая страница живой выдачи (если площадка отвечает)."""
    out = []
    for new in markets(profile):
        try:
            out += (await cian.search(profile.get("city"), profile["rooms"], profile["budget_max"], page, profile.get("okrug"), new))[0]
        except SourceUnavailable:
            pass
    return out
