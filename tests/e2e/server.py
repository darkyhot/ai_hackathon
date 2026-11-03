"""Сервер для сквозной проверки без сети: настоящий backend.main, но
- модель — PolicyLLM (правила ведут агента обычным ходом сделки),
- Циан — сохранённые настоящие объявления из tests/e2e/data (Казань вторичка и новостройки, Москва; 2 комнаты),
  отдаются как «живая» выдача, страница объявления — «активно»,
- карта (OpenStreetMap) — недоступна, как без сети.

Запуск: .venv/bin/python -m tests.e2e.server --port 8790   (база — во временном файле)
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"


def patch(*, live_map: bool = False) -> None:
    from backend import geo, main
    from backend.sources import cian
    from backend.sources import places
    from backend.sources.base import SourceUnavailable

    from .policy_llm import PolicyLLM

    llm = PolicyLLM()
    main.get_llm = lambda: llm

    async def fetch_page(city, rooms, budget, page, okrug=None, new=False):
        if page > 1:
            raise SourceUnavailable("ответ 302 (страниц больше нет)")
        path = DATA / f"{cian.REGIONS[city][0]}_{rooms}{'_new' if new else ''}.json"
        if not path.exists():
            raise SourceUnavailable("e2e: для этого города нет сохранённых объявлений")
        items = json.loads(path.read_text(encoding="utf-8"))["listings"]
        return [x for x in items if not budget or x["price"] <= budget * 1.05], "e2e"

    async def check_page(url):
        return {"alive": True, "note": "e2e: страница открывается", "offer": None}

    async def no_overpass(query, timeout):
        raise geo.httpx.ConnectError("e2e: без сети")

    cian._fetch_page = fetch_page
    cian.check_listing_page = check_page
    geo._overpass = no_overpass
    async def no_places(*args, **kwargs):
        return None
    places.enrich = no_places
    if not live_map:
        main.maptiles.configuration = lambda: {'provider': 'osm'}


def fake_voximplant() -> None:
    """Звонок через Voximplant без облака: Web SDK подменён tests/e2e/fake_voximplant.js, который исполняет
    в странице настоящий backend/voximplant/scenario.js. Сервер — настоящий (/api/phone, вход по одноразовому ключу)."""
    import dataclasses

    from fastapi.responses import FileResponse, JSONResponse
    from starlette.routing import Route

    from backend import main, phone
    from backend.config import settings

    phone.settings = dataclasses.replace(settings, voximplant_account_id="e2e", voximplant_api_key="e2e-key-0123456789",
                                         voximplant_credentials="", phone_pin="")
    cfg = {"username": "agent@domclick-agent.e2e.n4.voximplant.com", "app": "domclick-agent.e2e.n4.voximplant.com",
           "node": "NODE_4",
           "callerid": "74950000000", "voice": "SaluteSpeech.ru_RU_sergey", "asr": "SaluteSpeech.ru_RU"}

    async def setup(force=False):
        return cfg
    phone.setup = setup
    here = Path(__file__).resolve().parent

    async def sdk(request):
        return FileResponse(here / "fake_voximplant.js", media_type="text/javascript")

    async def real_sdk(request):
        return FileResponse(Path(main.__file__).resolve().parents[1] / "frontend" / "vendor" / "voximplant.min.js",
                            media_type="text/javascript")

    async def scenario(request):
        return FileResponse(phone.SCENARIO_FILE, media_type="text/javascript")

    async def verify(request):
        key, got = request.query_params.get("key", ""), request.query_params.get("hash", "")
        return JSONResponse({"ok": bool(key) and got == phone.login_hash(key)})

    for path, fn in (("/vendor/voximplant.min.js", sdk), ("/__fake_vox/scenario.js", scenario), ("/__fake_vox/verify", verify),
                     ("/__fake_vox/real-sdk.js", real_sdk)):
        main.app.router.routes.insert(0, Route(path, fn))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--live-map", action="store_true", help="Разрешить настоящие плитки 2ГИС из .env (расходует квоту)")
    ap.add_argument("--fake-voximplant", action="store_true", help="Звонок через поддельный Web SDK и настоящий сценарий VoxEngine")
    args = ap.parse_args()
    os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/e2e.db")
    patch(live_map=args.live_map)
    if args.fake_voximplant:
        fake_voximplant()
    import uvicorn

    from backend.main import app
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")
