"""HTTP-слой: API агента + раздача фронтенда.

Запуск:  .venv/bin/uvicorn backend.main:app --reload
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import fields

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, db, economics, sources
from .agent import orchestrator as O
from .agent import security
from .db import repo
from .config import ROOT, settings
from .llm import get_llm
from . import stt

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

app = FastAPI(title="Агент жизненных событий — Домклик")
app.middleware("http")(auth.visitor_middleware)


@app.on_event("startup")
async def _startup():
    db.configure()
    stt.preload_in_background()
    if settings.refresh_at:  # ежедневный обход объявлений внутри сервера (или отдельным процессом — workers/refresh.py)
        from .workers import refresh
        asyncio.create_task(refresh.daily(settings.refresh_at))


class StartRequest(BaseModel):
    query: str = Field(min_length=3, max_length=500)


class ConfirmRequest(BaseModel):
    kind: str
    approved: bool = True
    data: dict = {}


class SellerReply(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class ClientMessage(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    liked: list[str] = []
    disliked: list[str] = []


class ProductRequest(BaseModel):
    product: str
    amount: int = Field(ge=0)


class RegisterRequest(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)
    name: str | None = Field(default=None, max_length=120)


class LoginRequest(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)


@app.get("/api/health")
async def health():
    llm = get_llm()
    return {
        "ok": True,
        "provider": llm.name,
        "model": llm.model,
        "configured": settings.llm_provider,
        "reason": getattr(llm, "reason", None),
        "cities": sources.SUPPORTED_CITIES,
        "demo": settings.demo,
        "stt": stt.status(),
        "db": repo.record(lambda d: d.bind.dialect.name, default=None),
    }


# ------------------------------------------------------------------ пользователи


@app.post("/api/auth/register")
async def register(req: RegisterRequest, request: Request, response: Response):
    with db.session() as d:
        return auth.register(d, req.email, req.password, req.name, request, response)


@app.post("/api/auth/login")
async def login(req: LoginRequest, request: Request, response: Response):
    with db.session() as d:
        return auth.login(d, req.email, req.password, request, response)


@app.post("/api/auth/logout")
async def logout(request: Request, response: Response):
    with db.session() as d:
        auth.logout(d, request, response)
    return {"ok": True}


@app.get("/api/auth/sberid")
async def sber_id():
    auth.sber_id_login()


@app.get("/api/me")
async def me(request: Request):
    """Кто я: пользователь (если вошёл) и посетитель. Работает и без входа."""
    vid = auth.visitor_id(request)
    with db.session() as d:
        u = auth.current_user(d, request)
        v = d.get(repo.Visitor, vid) if vid else None
        return {"user": auth.public(u), "visitor_id": vid, "consent": bool(v and v.consent_at)}


@app.get("/api/me/history")
async def my_history(request: Request):
    vid = auth.visitor_id(request)
    with db.session() as d:
        u = auth.current_user(d, request)
        return repo.history(d, u.id if u else None, vid)


@app.post("/api/consent")
async def consent(request: Request):
    """Посетитель закрыл баннер о cookie."""
    vid = auth.visitor_id(request)

    def save(d):
        v = repo.touch_visitor(d, vid, request.headers.get("user-agent"))
        v.consent_at = v.consent_at or repo.utcnow()
        repo.log_event(d, "cookie_consent", None, vid)
    repo.record(save)
    return {"ok": True}


MAX_AUDIO = 5 * 1024 * 1024  # ~ минута речи в webm/opus


@app.post("/api/stt")
async def speech_to_text(request: Request):
    """Распознать фразу: тело запроса — аудио (webm/ogg/wav) из MediaRecorder."""
    if not stt.installed():
        raise HTTPException(503, "Серверное распознавание не установлено (pip install faster-whisper)")
    audio = await request.body()
    if not audio:
        raise HTTPException(400, "Пустая запись")
    if len(audio) > MAX_AUDIO:
        raise HTTPException(413, "Слишком длинная запись")
    try:
        return await stt.transcribe(audio)
    except Exception as exc:
        raise HTTPException(500, f"Не удалось распознать: {exc}") from exc


@app.post("/api/session")
async def start(req: StartRequest, request: Request):
    vid, uid, _ = auth.whoami(request)
    s = O.create_session(req.query, get_llm(), visitor_id=vid, user_id=uid)
    return {"id": s.id}


def _session(sid: str) -> O.Session:
    s = O.SESSIONS.get(sid)
    if not s:
        raise HTTPException(404, "Сессия не найдена")
    return s


@app.get("/api/session/{sid}/stream")
async def stream(sid: str, request: Request):
    s = _session(sid)
    if s.task is not None:
        raise HTTPException(409, "Сессия уже запущена")
    s.task = asyncio.create_task(O.run_session(s))

    async def gen():
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(s.events.get(), timeout=15)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield ": ping\n\n"
                    continue
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                if ev["type"] in {"done", "error"}:
                    break
        finally:
            if not s.task.done():
                s.task.cancel()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/session/{sid}/confirm")
async def confirm(sid: str, req: ConfirmRequest):
    if not _session(sid).resolve(req.kind, req.approved, req.data):
        raise HTTPException(409, f"Нет ожидающего подтверждения «{req.kind}»")
    return {"ok": True}


@app.post("/api/session/{sid}/seller")
async def seller(sid: str, req: SellerReply):
    await _session(sid).seller_queue.put(req.text.strip())
    return {"ok": True}


@app.post("/api/session/{sid}/message")
async def message(sid: str, req: ClientMessage):
    """Текст или голос клиента: ответ на вопрос, пожелание к подборке, заметка."""
    kind = await _session(sid).receive_text(req.text.strip(), {"liked": req.liked, "disliked": req.disliked})
    return {"ok": True, "handled_as": kind}


@app.post("/api/session/{sid}/product")
async def open_product(sid: str, req: ProductRequest):
    """Открыть продукт из финансового плана (демо: без реального открытия)."""
    await _session(sid).open_product(req.product, req.amount)
    return {"ok": True}


@app.get("/api/listing/{listing_id}/security")
async def listing_security(listing_id: str, sid: str | None = None):
    """Окно проверки безопасности: каждый критерий с методом, доказательством и ссылкой на официальный сервис,
    плюс факты из звонков продавцу с уверенностью."""
    s = O.SESSIONS.get(sid) if sid else None
    dossier = s.checks.get(listing_id) if s else None
    listing = None
    if dossier is None or s is None:
        with db.session() as d:
            row = repo.get_listing(d, listing_id)
            listing = repo.listing_dict(row) if row else None
        if listing is None:
            raise HTTPException(404, "Объявление не найдено")
        dossier = security.dossier(listing)
    facts = repo.record(repo.listing_facts, listing_id, default=[])
    return {**dossier, "facts": facts}


def _admin(request: Request) -> None:
    _, _, role = auth.whoami(request)
    if role != "admin":
        raise HTTPException(403, "Только для администратора (ADMIN_EMAILS в .env)")


@app.get("/api/admin/stats")
async def admin_stats(request: Request):
    _admin(request)
    with db.session() as d:
        return repo.stats(d)


@app.post("/api/admin/refresh")
async def admin_refresh(request: Request):
    """Запустить обход объявлений сейчас (в фоне). Результат — в /api/admin/stats → crawls."""
    _admin(request)
    from .workers import refresh
    asyncio.create_task(refresh.run_once("manual"))
    return {"ok": True, "started": True}


@app.get("/api/economics")
async def get_economics(request: Request):
    """Юнит-экономика и сценарии ROI. Любое поле Inputs можно передать в query."""
    params = {}
    types = {f.name: f.type for f in fields(economics.Inputs)}
    for key, value in request.query_params.items():
        if key not in types:
            continue
        kind = str(types[key])
        try:
            if kind == "bool":
                params[key] = value.lower() in {"1", "true", "yes", "on"}
            elif kind == "float":
                params[key] = float(value)
            else:
                params[key] = int(float(value))
        except ValueError:
            raise HTTPException(422, f"Неверное значение {key}")
    inputs = economics.Inputs(**params)
    base = economics.Inputs(fin_enabled=inputs.fin_enabled)
    return {"unit": economics.unit(inputs), "scenarios": economics.scenarios(base)}


app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")
