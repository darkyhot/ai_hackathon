"""Ежедневный обход объявлений: обновить цены и статусы, снять пропавшие, добавить новые.

Что обходим: tracked_searches — поиски, которые хоть раз задавали клиенты (город, комнаты,
вторичка/новостройки, округ, самый большой бюджет). Для каждого — до REFRESH_PAGES страниц живой выдачи.

Правило снятия: объявления нет в ПОЛНОМ обходе поиска (дошли до последней страницы) —
пропуск +1; после REMOVE_AFTER_MISSES пропусков подряд — статус removed. Если обход неполный
(капча, сеть, страниц больше лимита), пропуски не считаем: лучше показать снятое объявление
на день дольше, чем потерять живое. Объявление снова появилось — возвращается в active.
Каждый обход пишется в crawl_runs.

Запуск:
  python -m backend.workers.refresh --once            # один обход сейчас
  python -m backend.workers.refresh --daily 04:00     # каждый день в 04:00 (отдельный процесс)
  REFRESH_AT=04:00 в .env                             # то же внутри сервера (uvicorn)
  POST /api/admin/refresh                             # вручную, для администратора
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timedelta

from .. import db
from ..config import settings
from ..db import repo
from ..db.models import CrawlRun, TrackedSearch, utcnow
from ..sources import SourceUnavailable, cian

log = logging.getLogger("refresh")

FULL_PAGE = 20  # на странице Циана ~28 объявлений; меньше 20 — значит, это последняя страница

_running = asyncio.Lock()


async def _crawl_search(t: TrackedSearch) -> tuple[list[dict], bool, str | None]:
    """Возвращает (объявления, обход полный, ошибка)."""
    if cian.demo_mode():  # без сети: «обход» по демо-срезу — чтобы показать механизм
        items, _ = cian.load_snapshot(t.city, t.rooms, new=t.is_new)
        items = [x for x in items if (not t.budget_max or x["price"] <= t.budget_max * 1.05)
                 and (not t.okrug or x.get("okrug") == t.okrug)]
        return items, True, None
    out: list[dict] = []
    for page in range(1, settings.refresh_pages + 1):
        try:
            items = await cian.fetch_live(t.city, t.rooms, t.budget_max, page, t.okrug or None, t.is_new)
        except SourceUnavailable as exc:
            if cian.is_end_of_results(exc, page):
                return out, True, None
            return out, False, f"{t.city}, {t.rooms}-комн.{' новостройки' if t.is_new else ''}, стр. {page}: {exc}"
        out += items
        if len(items) < FULL_PAGE:
            return out, True, None
    return out, False, None  # дошли до лимита страниц — дальше не смотрели, пропуски не считаем


async def run_once(trigger: str = "manual") -> dict:
    """Один обход всех отслеживаемых поисков. Параллельно второй не запускается."""
    if _running.locked():
        return {"status": "busy"}
    async with _running:
        with db.session() as s:
            run_id = repo.start_crawl(s, trigger).id
            searches = [(t.id, t) for t in repo.tracked_searches(s)]
        totals = {"searches": 0, "seen": 0, "added": 0, "updated": 0, "price_changed": 0, "removed": 0}
        errors: list[str] = []
        for tid, t in searches:
            items, complete, error = await _crawl_search(t)
            if error:
                errors.append(error)
            with db.session() as s:
                t = s.get(TrackedSearch, tid)
                st = repo.upsert_listings(s, items)
                for k in ("seen", "added", "updated", "price_changed"):
                    totals[k] += st[k]
                if complete:
                    totals["removed"] += repo.sweep_missing(s, t, {x["id"] for x in items}, settings.remove_after_misses)
                    repo.mark_crawled(s, t, t.budget_max)
                totals["searches"] += 1
            log.info("обход %s, %s-комн.%s: %d объявлений%s", t.city, t.rooms, " (новостройки)" if t.is_new else "",
                     len(items), "" if complete else " — неполный, снятия не считаю")
        with db.session() as s:
            run = s.get(CrawlRun, run_id)
            for k, v in totals.items():
                setattr(run, k, v)
            run.errors = errors
            run.finished_at = utcnow()
            run.status = "ok" if not errors else ("partial" if totals["seen"] else "failed")
            result = {"id": run.id, "status": run.status, **totals, "errors": errors}
        if settings.store_photos:
            from . import photos
            await photos.download_missing()
        return result


def _next_run(at: str, now: datetime | None = None) -> datetime:
    hh, mm = (int(v) for v in at.split(":"))
    now = now or datetime.now()
    nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return nxt if nxt > now else nxt + timedelta(days=1)


async def daily(at: str) -> None:
    """Каждый день в at (местное время сервера, «ЧЧ:ММ»)."""
    while True:
        nxt = _next_run(at)
        log.info("следующий обход объявлений: %s", nxt.strftime("%d.%m %H:%M"))
        await asyncio.sleep((nxt - datetime.now()).total_seconds())
        try:
            log.info("обход завершён: %s", await run_once("daily"))
        except Exception:  # обход не должен останавливать расписание
            log.exception("обход упал")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Обновление объявлений в базе")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--once", action="store_true", help="один обход сейчас")
    g.add_argument("--daily", metavar="ЧЧ:ММ", help="каждый день в это время")
    args = ap.parse_args()
    db.configure()
    if args.once:
        print(asyncio.run(run_once("manual")))
    else:
        asyncio.run(daily(args.daily))
