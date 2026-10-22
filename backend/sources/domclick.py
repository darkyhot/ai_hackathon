"""Домклик: поиск объявлений через страницу выдачи (без телефонов).

Страница поиска Домклика отдаёт объявления прямо в HTML — в объекте window.__SSR_STATE__
(search.pages[<offset>].entities). Два вида карточек:
- ProductSnippet — квартира от продавца: вторичка или переуступка (assignmentSale);
- LayoutSnippet  — планировка от застройщика в новостройке (цена «от», этажи «от–до»).

Вторичка — offer_type=flat, новостройки — offer_type=layout. Город задаётся GUID-ом
(address=…), коды семи городов прототипа проверены на сайте 21.10.2026.

Сайт закрыт защитой Qrator: она пропускает браузер после проверки на JavaScript.
Порядок попыток: обычный HTTP → curl_cffi с отпечатком Chrome → Playwright (настоящий
браузер, если установлен) → сохранённый срез. В продукте этот модуль не нужен:
Сбер берёт объявления Домклика изнутри.

Проверка с ноутбука (лучше без VPN):
    .venv/bin/python -m backend.sources.domclick --city Казань --rooms 2 --budget 9000000
    .venv/bin/python -m backend.sources.domclick --city Казань --rooms 2 --budget 9000000 --new
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import date, datetime
from pathlib import Path

import httpx

from .base import SourceUnavailable

log = logging.getLogger(__name__)

SOURCE = "domclick"
SOURCE_NAME = "Домклик"
PAGE_SIZE = 20
CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache" / "domclick"
DEMO_DIR = Path(__file__).resolve().parent.parent / "data" / "demo" / "domclick"
CACHE_TTL = 30 * 60
SNAPSHOT_VERSION = 1
MIN_INTERVAL = 3.0
PHOTO_HOST = "https://img.dmclk.ru/c960x640q80"

# город -> GUID населённого пункта на Домклике (проверено на сайте 21.10.2026)
LOCALITIES = {
    "Москва": "1d1463ae-c80f-4d19-9331-a1b68a85b553",
    "Санкт-Петербург": "7b4698a7-f8b8-424c-9195-e24f3ddb88f3",
    "Казань": "26f533ee-f4c6-4fd8-9cb5-a1910250622e",
    "Новосибирск": "01dc15bf-a360-4e88-a57b-aad6cc74ef6a",
    "Екатеринбург": "0d475b79-88de-4054-818c-37d8f9d0d440",
    "Нижний Новгород": "494487ff-5431-45ab-8b6e-77c1c9c22339",
    "Краснодар": "cdd48d17-f8b2-4839-af94-11609a721ed6",
}

# округа Москвы и районы Казани — из адреса в ссылке на дом (/yuzao/konkovo/…, /building/privolzhskij--…)
MOSCOW_OKRUGS = {"cao": "ЦАО", "sao": "САО", "svao": "СВАО", "vao": "ВАО", "yuvao": "ЮВАО", "yuao": "ЮАО",
                 "yuzao": "ЮЗАО", "zao": "ЗАО", "szao": "СЗАО"}
DISTRICTS = {"privolzhskij": "Приволжский", "sovetskij": "Советский", "vahitovskij": "Вахитовский",
             "kirovskij": "Кировский", "moskovskij": "Московский", "novo-savinovskij": "Ново-Савиновский",
             "aviastroitelnyj": "Авиастроительный"}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/130.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

_memory: dict[tuple, tuple[float, list[dict]]] = {}
_lock = asyncio.Lock()
_last_request = 0.0
DUMP_RAW = False


def supported(city: str | None) -> bool:
    return city in LOCALITIES


def demo_mode() -> bool:
    from ..config import settings
    return settings.demo


_blocked_until = 0.0
BLOCK_PAUSE = 30 * 60   # после неудачи 30 минут не пробуем вживую — сразу сохранённые объявления


def static_mode() -> bool:
    """Живой поиск отключён настройкой STATIC_SOURCES или недавно не прошёл защиту (не ждём попыток снова)."""
    from ..config import settings
    return SOURCE in [x.strip() for x in settings.static_sources.split(",")] or time.time() < _blocked_until


def build_url(city: str, rooms: int, budget: int | None, page: int = 1, new: bool = False) -> str:
    room = "st" if rooms == 0 else "5%2B" if rooms >= 5 else str(rooms)
    url = (f"https://domclick.ru/search?deal_type=sale&category=living&offer_type={'layout' if new else 'flat'}"
           f"&address={LOCALITIES[city]}&rooms={room}&sort=published&sort_dir=desc&offset={(page - 1) * PAGE_SIZE}")
    if budget:
        url += f"&sale_price__lte={int(budget * 1.05)}"
    return url


# ------------------------------------------------------------------ разбор страницы


def extract_state(page: str) -> dict | None:
    """window.__SSR_STATE__={…} — это JS-объект: вырезаем по скобкам и заменяем undefined на null."""
    i = page.find("__SSR_STATE__")
    if i < 0:
        return None
    start = page.find("{", i)
    depth, in_str, esc = 0, False, False
    for j in range(start, len(page)):
        c = page[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                raw = re.sub(r":undefined(?=[,}\]])", ":null", page[start:j + 1])
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return None
    return None


def entities(state: dict) -> list[dict]:
    pages = ((state or {}).get("search") or {}).get("pages") or {}
    out = []
    for pg in pages.values():
        ents = pg.get("entities") or {}
        for i in pg.get("ids") or list(ents):
            e = ents.get(str(i)) or ents.get(i)
            if e:
                out.append(e)
    return out


def _address(display: str | None, city: str) -> str:
    a = re.sub(r"^Россия,\s*", "", display or "")
    a = re.sub(rf"^{re.escape(city)},\s*", "", a)
    a = re.sub(r"^жилой комплекс [^,]+,\s*", "", a)  # ЖК — отдельным полем
    return re.sub(r"\s+стр$", "", a).strip() or city


def _place(e: dict) -> tuple[str | None, str | None]:
    """(район, округ) из ссылок на дом."""
    for part in ((e.get("seoInfo") or {}).get("displayNameParts") or []):
        uri = part.get("uri") or ""
        m = re.match(r"^/([a-z]+)/", uri)
        if m and m.group(1) in MOSCOW_OKRUGS:
            return None, MOSCOW_OKRUGS[m.group(1)]
        m = re.match(r"^/building/([a-z-]+?)--", uri)
        if m and m.group(1) in DISTRICTS:
            return DISTRICTS[m.group(1)], None
    return None, None


def _deadline(building: dict | None) -> tuple[str | None, bool | None]:
    b = building or {}
    if b.get("released"):
        return "дом сдан", True
    if b.get("endBuildYear"):
        q = b.get("endBuildQuarter")
        return (f"{q} кв. {b['endBuildYear']}" if q else f"{b['endBuildYear']} г."), False
    return None, None


def _days(published: str | None, today: date) -> int | None:
    try:
        return max(0, (today - datetime.fromisoformat((published or "")[:19]).date()).days)
    except ValueError:
        return None


def normalize(e: dict, city: str, today: date | None = None, new: bool = False) -> dict | None:
    """Карточка Домклика -> общий формат объявления (см. base.py)."""
    from .cian import condition_from_text  # то же определение ремонта по описанию

    today = today or date.today()
    kind = e.get("$$type")
    price = e.get("price")
    layout = kind == "LayoutSnippet"
    info = (e.get("generalInfo") if layout else e.get("objectInfo")) or {}
    area = info.get("area")
    if not price or not area or (e.get("roomsOffered") or 0) > 0:  # продажа комнаты, а не квартиры
        return None
    rooms = info.get("rooms") or 0
    if area < 18 or area < 9 * max(1, rooms):
        return None
    district, okrug = _place(e)
    subway = ((e.get("address") or {}).get("subways") or [None])[0]
    metro = None
    if subway and subway.get("name"):
        metro = {"name": subway["name"], "minutes": (subway.get("remoteness") or {}).get("time"), "mode": ""}
    photo = ((e.get("photos") or [{}])[0] or {}).get("url")
    loc = e.get("location") or {}
    description = (e.get("description") or "")[:1500]
    base = {
        "id": f"domclick-{e.get('id') or e.get('offerId')}",
        "source": SOURCE,
        "source_name": SOURCE_NAME,
        "url": (e.get("path") or "").split("?")[0] or None,
        "city": city,
        "district": district,
        "okrug": okrug,
        "address": _address((e.get("address") or {}).get("displayName"), city),
        "rooms": rooms,
        "area": float(area),
        "kitchen": None,
        "condition": condition_from_text(description),
        "material": None,
        "lat": loc.get("lat"),
        "lng": loc.get("lon"),
        "price": int(price),
        "days_on_market": _days(e.get("publishedDate"), today),
        "metro": metro,
        "photo": (PHOTO_HOST + photo) if photo and photo.startswith("/") else photo,
        "description": description,
        "rosreestr_checked": e.get("isRosreestrApproved"),
        "is_apartments": bool(info.get("isApartment")),
        "is_auction": bool(e.get("isAuction")),
        "family_mortgage_flag": e.get("hasFamilyMortgage"),  # Домклик сам отмечает «подходит под семейную»
    }
    if layout:
        cx = e.get("complex") or {}
        deadline, finished = _deadline(cx.get("building"))
        mort = e.get("mortgage") or {}
        return {**base,
                "floor": info.get("minFloor"),
                "floor_range": (f"{info['minFloor']}–{info['maxFloor']}" if info.get("minFloor") != info.get("maxFloor")
                                else None) if info.get("minFloor") else None,
                "floors": info.get("floors"), "year": None,
                "is_new": True, "complex": re.sub(r"^ЖК\s+", "", cx.get("name") or "") or None,
                "deadline": deadline, "finished": finished, "developer": e.get("developerName"),
                "sale_type": "договор купли-продажи" if finished else "ДДУ (214-ФЗ)",
                "seller_role": "застройщик",
                "developer_rate": mort.get("minRate") if mort.get("hasMortgageSubsidy") else None}
    house = e.get("house") or {}
    fc = e.get("flatComplex") or {}
    deadline, finished = _deadline(fc.get("building"))
    assignment = bool(e.get("assignmentSale"))
    is_new = assignment or finished is False or (new and bool(fc))
    agent = ((e.get("seller") or {}).get("agent") or {})
    if e.get("isOwner"):
        role = "собственник (переуступка)" if assignment else "собственник"
    elif assignment:
        role = "агент продавца (переуступка)" if agent.get("isAgent") else "собственник (переуступка)"
    else:
        role = "агент продавца" if agent.get("isAgent") else "собственник"
    return {**base,
            "floor": info.get("floor"), "floors": house.get("floors"), "year": house.get("buildYear"),
            "is_new": is_new,
            **({"complex": fc.get("name"), "deadline": deadline, "finished": finished, "developer": None,
                "sale_type": "договор уступки" if assignment else None} if is_new else {}),
            "seller_role": role}


# ------------------------------------------------------------------ загрузка


def _is_qrator(status: int, text: str) -> bool:
    return status in (401, 403, 429) or ("__qrator" in text and "__SSR_STATE__" not in text)


SESSION_FILE = CACHE_DIR / "session.json"   # «пропуск» Qrator после прохода браузером: cookie + User-Agent
BROWSER_MODES = ["headless", "window"]     # CLI --window: сразу с окном


def _load_session() -> dict:
    try:
        return json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_session(ua: str, cookies: list[dict]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        jar = {c["name"]: c["value"] for c in cookies if "domclick" in (c.get("domain") or "")}
        SESSION_FILE.write_text(json.dumps({"ua": ua, "cookies": jar, "saved_at": datetime.now().isoformat(timespec="minutes")},
                                           ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        log.warning("не удалось сохранить сессию Домклика: %s", exc)


async def _get_httpx(url: str) -> tuple[int, str]:
    """Обычный запрос; если браузер уже прошёл проверку — с его cookie и User-Agent (быстро, без браузера)."""
    sess = _load_session()
    headers = {**HEADERS, **({"User-Agent": sess["ua"]} if sess.get("ua") else {})}
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=headers, cookies=sess.get("cookies") or {},
                                 transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0")) as http:
        r = await http.get(url)
    return r.status_code, r.text


async def _get_browser_like(url: str) -> tuple[int, str]:
    from curl_cffi.requests import AsyncSession

    async with AsyncSession(impersonate="chrome", timeout=20) as http:
        r = await http.get(url, headers={"Accept-Language": "ru-RU,ru;q=0.9"})
    return r.status_code, r.text


STEALTH = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"


async def _get_playwright(url: str) -> tuple[int, str]:
    """Настоящий Chrome: проходит проверку Qrator на JavaScript. Сначала без окна (полноценный режим Chrome,
    без признаков автоматизации), не вышло — с окном за краем экрана. Профиль браузера хранится в кеше,
    поэтому пропуск Qrator переживает перезапуск; после прохода cookie уходят в обычные запросы."""
    from playwright.async_api import async_playwright

    profile = CACHE_DIR / "browser-profile"
    profile.mkdir(parents=True, exist_ok=True)
    last = "нет ответа"
    async with async_playwright() as p:

        async def launch(mode: str, ua: str | None):
            args = ["--disable-blink-features=AutomationControlled", "--lang=ru-RU"]
            if mode == "window":
                args += ["--window-position=-2400,0", "--window-size=1280,900"]
            ctx = await p.chromium.launch_persistent_context(
                str(profile), headless=(mode == "headless"), channel="chromium", args=args, locale="ru-RU",
                viewport={"width": 1280, "height": 900}, **({"user_agent": ua} if ua else {}))
            await ctx.add_init_script(STEALTH)
            await ctx.set_extra_http_headers({"Accept-Language": "ru-RU,ru;q=0.9"})
            return ctx

        for mode in BROWSER_MODES:
            ctx = None
            try:
                ctx = await launch(mode, None)
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                real_ua = await page.evaluate("navigator.userAgent")
                ua = real_ua.replace("HeadlessChrome", "Chrome")
                if ua != real_ua:  # без окна Chrome выдаёт себя словом HeadlessChrome — перезапускаем с обычным
                    await ctx.close()
                    ctx = await launch(mode, ua)
                    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                deadline = time.monotonic() + 35
                while time.monotonic() < deadline:  # Qrator проверяет JS и перезагружает страницу
                    await asyncio.sleep(1)
                    try:
                        html = await page.content()
                    except Exception:  # страница как раз перезагружается
                        continue
                    if "__SSR_STATE__" in html:
                        _save_session(ua, await ctx.cookies())
                        log.info("Домклик: браузер (%s) прошёл проверку Qrator", mode)
                        return 200, html
                last = f"{mode}: не дождался объявлений за 35 с"
                try:  # снимок для разбора: что показала защита
                    await page.screenshot(path=str(CACHE_DIR / f"debug_{mode}.png"))
                    (CACHE_DIR / f"debug_{mode}.html").write_text((await page.content())[:20000], encoding="utf-8")
                except Exception:
                    pass
            except Exception as exc:
                last = f"{mode}: {type(exc).__name__}: {str(exc)[:120]}"
            finally:
                if ctx is not None:
                    try:
                        await ctx.close()
                    except Exception:
                        pass
    return 401, f"__qrator ({last})"


async def _fetch_page(city: str, rooms: int, budget: int | None, page: int, new: bool) -> tuple[list[dict], str]:
    global _last_request
    url = build_url(city, rooms, budget, page, new)
    attempts = [("httpx", _get_httpx)]
    for module, name, getter in (("curl_cffi", "curl_cffi", _get_browser_like), ("playwright", "браузер", _get_playwright)):
        try:
            __import__(module)
            attempts.append((name, getter))
        except ImportError:
            pass
    problem = "нет ответа"
    async with _lock:
        for name, getter in attempts:
            wait = MIN_INTERVAL - (time.monotonic() - _last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            _last_request = time.monotonic()
            try:
                status, text = await getter(url)
            except Exception as exc:  # сеть, TLS, нет браузера — пробуем следующий способ
                problem = f"{name}: {type(exc).__name__}"
                continue
            if _is_qrator(status, text):
                problem = f"{name}: защита Qrator" + (f" — {text[10:-1]}" if text.startswith("__qrator (") else "")
                log.info("Домклик через %s: защита Qrator — пробую дальше", name)
                continue
            state = extract_state(text)
            if state is None:
                problem = f"{name}: не нашёл данные на странице (ответ {status})"
                continue
            raw = entities(state)
            if DUMP_RAW:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                (CACHE_DIR / f"raw_{_key(city, rooms, new)}_p{page}.json").write_text(
                    json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
            return [x for x in (normalize(e, city, new=new) for e in raw) if x], name
    hint = "" if any(n == "браузер" for n, _ in attempts) else \
        " — для обхода нужен браузер: .venv/bin/pip install playwright && .venv/bin/playwright install chromium"
    raise SourceUnavailable(f"Домклик недоступен ({problem}){hint}")


def _key(city: str, rooms: int, new: bool) -> str:
    return f"{LOCALITIES[city][:8]}_{rooms}{'_new' if new else ''}"


def _snapshot_path(city: str, rooms: int, base: Path | None = None, new: bool = False) -> Path:
    return (base or CACHE_DIR) / f"{_key(city, rooms, new)}.json"


def _save_snapshot(city: str, rooms: int, listings: list[dict], new: bool) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        old = load_snapshot(city, rooms, allow_demo=False, new=new)[0]
        merged = {x["id"]: x for x in old} | {x["id"]: x for x in listings}
        _snapshot_path(city, rooms, new=new).write_text(json.dumps(
            {"version": SNAPSHOT_VERSION, "fetched_at": datetime.now().isoformat(timespec="minutes"),
             "listings": list(merged.values())}, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        log.warning("не удалось сохранить срез Домклика: %s", exc)


def load_snapshot(city: str, rooms: int, allow_demo: bool = True, new: bool = False) -> tuple[list[dict], str | None]:
    if not supported(city):
        return [], None
    path = _snapshot_path(city, rooms, new=new)
    if allow_demo and (demo_mode() or not path.exists()):
        path = _snapshot_path(city, rooms, DEMO_DIR, new=new)
    if not path.exists():
        return [], None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != SNAPSHOT_VERSION:
        return [], None
    return data["listings"], data.get("fetched_at")


def _filter(items: list[dict], budget: int | None, okrug: str | None) -> list[dict]:
    return [x for x in items if (not budget or x["price"] <= budget * 1.05) and (not okrug or x.get("okrug") == okrug)]


async def search(city: str, rooms: int, budget: int | None, page: int = 1, okrug: str | None = None,
                 new: bool = False) -> tuple[list[dict], dict]:
    """Возвращает (объявления, отчёт). Отчёт: status live | cached | snapshot | demo | unavailable."""
    if not supported(city):
        raise SourceUnavailable(f"для города «{city}» Домклик не подключён")
    if demo_mode() or static_mode():
        why = "демо-режим" if demo_mode() else "живой поиск закрыт защитой площадки"
        if page > 1:
            raise SourceUnavailable(f"{why}: больше страниц нет")
        saved, fetched_at = load_snapshot(city, rooms, new=new)
        saved = _filter(saved, budget, okrug)
        if not saved:
            raise SourceUnavailable(f"{why}: нет сохранённых объявлений Домклика для «{city}»")
        return saved, {"status": "demo" if demo_mode() else "static",
                       "note": f"{why}: сохранённые объявления Домклика от {fetched_at}"}
    key = (city, rooms, budget, page, new)
    hit = _memory.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return _filter(hit[1], budget, okrug), {"status": "cached", "note": "из кеша сессии"}
    try:
        listings, via = await _fetch_page(city, rooms, budget, page, new)
        _memory[key] = (time.time(), listings)
        _save_snapshot(city, rooms, listings, new)
        return _filter(listings, budget, okrug), {"status": "live", "note": f"страница {page} ({via})", "via": via}
    except SourceUnavailable as exc:
        global _blocked_until
        log.warning("%s", exc)
        _blocked_until = time.time() + BLOCK_PAUSE
        if page > 1:
            raise
        saved, fetched_at = load_snapshot(city, rooms, new=new)
        saved = _filter(saved, budget, okrug)
        if saved:
            return saved, {"status": "snapshot", "note": f"{exc}; сохранённый срез от {fetched_at}"}
        raise


if __name__ == "__main__":  # проверка с ноутбука и прогрев среза
    import argparse

    ap = argparse.ArgumentParser(description="Проверить Домклик и сохранить срез объявлений")
    ap.add_argument("--city", default="Казань")
    ap.add_argument("--rooms", type=int, default=2)
    ap.add_argument("--budget", type=int, default=9_000_000)
    ap.add_argument("--pages", type=int, default=2)
    ap.add_argument("--new", action="store_true", help="новостройки вместо вторички")
    ap.add_argument("--dump", action="store_true", help="сохранить сырые карточки (для проверки разбора)")
    ap.add_argument("--window", action="store_true", help="браузер сразу с окном (если без окна не пускает)")
    args = ap.parse_args()
    DUMP_RAW = args.dump
    static_mode = lambda: False  # noqa: E731 — проверка с ноутбука всегда пробует вживую
    if args.window:
        BROWSER_MODES[:] = ["window"]
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    async def main():
        total = 0
        for p in range(1, args.pages + 1):
            try:
                items, report = await search(args.city, args.rooms, args.budget, p, new=args.new)
            except SourceUnavailable as exc:
                print(f"страница {p}: {exc}")
                break
            total += len(items)
            print(f"страница {p}: {len(items)} объявлений ({report['status']}: {report['note']})")
            for x in items[:3]:
                print(f"   {x['price']:,} ₽ · {x['area']} м² · {x['address']}".replace(",", " ")
                      + (f" · ЖК {x.get('complex')} · {x.get('deadline')} · {x.get('developer')}" if x.get("is_new") else ""))
        print(f"готово: {total}, срез — {_snapshot_path(args.city, args.rooms, new=args.new)}")

    asyncio.run(main())
