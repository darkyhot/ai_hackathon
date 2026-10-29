"""HTTP-слой: API агента + раздача фронтенда.

Запуск:  .venv/bin/uvicorn backend.main:app --reload
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import fields

import httpx

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, db, economics, sources
from .agent import orchestrator as O
from .config import ROOT, settings
from .db import repo
from .llm import get_llm
from . import phone, stt

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

app = FastAPI(title="Агент жизненных событий — Домклик")
app.middleware("http")(auth.visitor_middleware)


@app.on_event("startup")
async def _startup():
    db.configure()
    stt.preload_in_background()
    asyncio.create_task(_reaper())
    if settings.refresh_at:  # ночной обход объявлений внутри сервера (или отдельно: python -m backend.workers.refresh --once)
        from .workers import refresh
        asyncio.create_task(refresh.daily(settings.refresh_at))


async def _reaper():
    """Раз в 5 минут освобождаем память от брошенных диалогов (история остаётся в базе)."""
    while True:
        await asyncio.sleep(300)
        O.reap_idle()


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


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class AuthRequest(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)
    name: str | None = Field(default=None, max_length=120)


class ProductRequest(BaseModel):
    product: str
    amount: int = Field(ge=0)


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
        "featured_cities": sources.FEATURED_CITIES,
        "stt": stt.status(),
        "phone": phone.status(),
    }


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


def _check_pin(request: Request):
    if not phone.allowed(request.headers.get("X-Phone-Pin")):
        raise HTTPException(403, "Нужен PIN для звонков (PHONE_PIN в .env сервера)")


@app.get("/api/phone")
async def phone_config(request: Request):
    """SIP-доступ Exolve для звонка из браузера. На публичном сервере — только с PIN."""
    _check_pin(request)
    try:
        return {**await phone.credentials(), "seller_phone": settings.seller_phone}
    except phone.PhoneError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/api/tts")
async def text_to_speech(req: SpeakRequest, request: Request):
    """Голос агента для телефонного звонка: текст -> WAV."""
    _check_pin(request)
    try:
        audio = await phone.synthesize(req.text)
    except (phone.PhoneError, httpx.HTTPError) as exc:
        raise HTTPException(502, f"Синтез речи не удался: {exc}") from exc
    return Response(audio, media_type="audio/wav")


# ------------------------------------------------------------------ вход


def _claim_live(request: Request, user: dict) -> None:
    """Вход: живые диалоги этого браузера тоже переходят пользователю (в базе — repo.claim_visitor)."""
    vid = auth.visitor_id(request)
    for s in O.SESSIONS.values():
        if vid and s.visitor_id == vid and s.user_id is None:
            s.user_id = user["id"]


@app.post("/api/auth/register")
async def register(req: AuthRequest, request: Request, response: Response):
    r = auth.register(req.email, req.password, req.name, request, response)
    _claim_live(request, r["user"])
    return r


@app.post("/api/auth/login")
async def login(req: AuthRequest, request: Request, response: Response):
    r = auth.login(req.email, req.password, request, response)
    _claim_live(request, r["user"])
    return r


@app.post("/api/auth/logout")
async def logout(request: Request, response: Response):
    auth.logout(request, response)
    return {"ok": True}


@app.get("/api/me")
async def me(request: Request):
    return {"user": auth.current_user(request)}


# ------------------------------------------------------------------ диалоги


@app.post("/api/session")
async def start(req: StartRequest, request: Request):
    vid, uid = auth.whoami(request)
    s = O.create_session(req.query, get_llm(), visitor_id=vid, user_id=uid)
    return {"id": s.id}


def _owns(s: O.Session, request: Request) -> bool:
    vid, uid = auth.whoami(request)
    return bool((vid and s.visitor_id == vid) or (uid is not None and s.user_id == uid))


def _session(sid: str, request: Request) -> O.Session:
    """Живая сессия агента — только своему браузеру или пользователю; чужой id — как несуществующий."""
    s = O.SESSIONS.get(sid)
    if s is None or not _owns(s, request):
        raise HTTPException(404, "Диалог не найден")
    return s


def _can_open(sid: str, request: Request) -> bool:
    live = O.SESSIONS.get(sid)
    if live is not None:
        return _owns(live, request)
    vid, uid = auth.whoami(request)
    return bool(db.safe(lambda d: repo.can_open(repo.get_chat(d, sid), vid, uid), default=False))


def _sse(ev: dict) -> str:
    return f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"


@app.get("/api/chats")
async def chats(request: Request):
    """Диалоги этого браузера и пользователя — новые сверху."""
    vid, uid = auth.whoami(request)
    items = db.safe(repo.list_chats, vid, uid, default=[]) or []
    for c in items:
        c["live"] = O.running(O.SESSIONS.get(c["id"]))
    return {"chats": items}


@app.get("/api/chat/{sid}/stream")
async def chat_stream(sid: str, request: Request, after: int = 0):
    """Лента диалога: история после события after (replay: true), затем replay_end, затем живые события.
    Новый диалог здесь же и запускается. Закрытая вкладка агента не останавливает."""
    if not _can_open(sid, request):
        raise HTTPException(404, "Диалог не найден")
    live = O.SESSIONS.get(sid)

    async def gen():
        q: asyncio.Queue = asyncio.Queue()
        if live is not None:
            live.subscribers.add(q)  # сначала подписка, потом история — ни одно событие не потеряется
            if live.task is None:  # новый диалог (или продолжение) запускаем, когда вкладка уже слушает
                live.task = asyncio.create_task(O.run_session(live))
        try:
            past = db.safe(repo.chat_events, sid, after)
            if past is None and live is not None:  # база недоступна — из памяти этого запуска
                past = [e for e in live.history if e["seq"] > after]
            sent = after
            for ev in past or []:
                yield _sse({**ev, "replay": True})
                sent = max(sent, ev.get("seq", sent))
            running = live is not None and live.task is not None and not live.task.done()
            yield _sse({"type": "replay_end", "live": running, "seq": sent})
            if not running:
                return
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield ": ping\n\n"
                    continue
                if ev["seq"] <= sent:
                    continue
                sent = ev["seq"]
                yield _sse(ev)
                if ev["type"] in {"done", "error", "idle"}:
                    break
        finally:
            if live is not None:
                live.subscribers.discard(q)
                if not live.subscribers:
                    live.idle_since = time.monotonic()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/chat/{sid}/message")
async def chat_message(sid: str, req: ClientMessage, request: Request):
    """Сообщение в диалог. Агент работает — как обычно; закончил (или сервер перезапускался) —
    продолжаем диалог новым запуском с тем, что агент уже знал. Ответ: continued — нужно переоткрыть ленту."""
    if not _can_open(sid, request):
        raise HTTPException(404, "Диалог не найден")
    live = O.SESSIONS.get(sid)
    if live is not None and (live.task is None or O.running(live)):  # агент ещё не запущен или работает
        kind = await live.receive_text(req.text.strip(), {"liked": req.liked, "disliked": req.disliked})
        return {"ok": True, "handled_as": kind, "continued": False}
    s = O.continue_session(sid, req.text, get_llm())
    return {"ok": True, "handled_as": "continue", "continued": True, "after": s.seq}


@app.post("/api/session/{sid}/confirm")
async def confirm(sid: str, req: ConfirmRequest, request: Request):
    if not _session(sid, request).resolve(req.kind, req.approved, req.data):
        raise HTTPException(409, f"Нет ожидающего подтверждения «{req.kind}»")
    return {"ok": True}


@app.post("/api/session/{sid}/seller")
async def seller(sid: str, req: SellerReply, request: Request):
    await _session(sid, request).seller_queue.put(req.text.strip())
    return {"ok": True}


@app.post("/api/session/{sid}/product")
async def open_product(sid: str, req: ProductRequest, request: Request):
    """Открыть продукт из финансового плана (демо: без реального открытия)."""
    await _session(sid, request).open_product(req.product, req.amount)
    return {"ok": True}


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
