"""Пользователи: регистрация, вход, выход, текущий пользователь. Плюс cookie посетителя.

Cookie:
  vid — id посетителя (uuid4, год). Ставится всем с первого запроса: всё, что человек делает
        до регистрации, пишется на этот id. При регистрации или входе события привязываются к пользователю.
  sid — вход (случайный токен, AUTH_SESSION_DAYS). В базе хранится только SHA-256 токена.
Обе cookie — HttpOnly, SameSite=Lax; Secure — при COOKIE_SECURE=true (за HTTPS).

Пароль: scrypt (стандартная библиотека), соль на пользователя. Перебор паролей ограничен:
5 неудачных попыток на почту за 10 минут.
Вход через Сбер ID — заглушка (sber_id_login): в проде OAuth 2.0 / OpenID Connect Сбер ID.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
import uuid
from datetime import timedelta

from fastapi import HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import repo
from .db.models import AuthSession, User, utcnow

VISITOR_COOKIE = "vid"
SESSION_COOKIE = "sid"
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
MIN_PASSWORD = 8

SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


# ------------------------------------------------------------------ пароли


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **SCRYPT)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${SCRYPT['n']}${SCRYPT['r']}${SCRYPT['p']}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        _, n, r, p, salt, digest = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, base64.b64decode(digest))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ------------------------------------------------------------------ защита от перебора

_fails: dict[str, list[float]] = {}
FAIL_LIMIT, FAIL_WINDOW = 5, 600


def _check_rate(email: str) -> None:
    now = time.time()
    recent = [t for t in _fails.get(email, []) if now - t < FAIL_WINDOW]
    _fails[email] = recent
    if len(recent) >= FAIL_LIMIT:
        raise HTTPException(429, "Слишком много попыток входа. Попробуйте через 10 минут.")


def _fail(email: str) -> None:
    _fails.setdefault(email, []).append(time.time())


# ------------------------------------------------------------------ cookie


def _set_cookie(response: Response, name: str, value: str, days: int) -> None:
    response.set_cookie(name, value, max_age=days * 86400, httponly=True, samesite="lax",
                        secure=settings.cookie_secure, path="/")


def visitor_id(request: Request) -> str | None:
    vid = request.cookies.get(VISITOR_COOKIE) or getattr(request.state, "new_vid", None)
    try:
        return str(uuid.UUID(vid)) if vid else None
    except ValueError:
        return None


async def visitor_middleware(request: Request, call_next):
    """Каждому посетителю — cookie vid; посетитель заносится в базу при первом обращении к API."""
    vid = request.cookies.get(VISITOR_COOKIE)
    fresh = False
    try:
        uuid.UUID(vid or "")
    except ValueError:
        vid, fresh = str(uuid.uuid4()), True
        request.state.new_vid = vid
    if request.url.path.startswith("/api/"):
        repo.record(repo.touch_visitor, vid, request.headers.get("user-agent"))
    response = await call_next(request)
    if fresh:
        _set_cookie(response, VISITOR_COOKIE, vid, 365)
    return response


# ------------------------------------------------------------------ пользователи


def _public(u: User) -> dict:
    return {"id": u.id, "email": u.email, "name": u.name, "role": u.role,
            "created_at": u.created_at.date().isoformat()}


def _role_for(email: str) -> str:
    admins = {e.strip().lower() for e in settings.admin_emails.split(",") if e.strip()}
    return "admin" if email in admins else "user"


def _start_session(db: Session, user: User, request: Request, response: Response) -> dict:
    token = secrets.token_urlsafe(32)
    vid = visitor_id(request)
    db.add(AuthSession(token_hash=_token_hash(token), user_id=user.id, visitor_id=vid,
                       expires_at=utcnow() + timedelta(days=settings.auth_session_days),
                       user_agent=(request.headers.get("user-agent") or "")[:300]))
    user.last_login_at = utcnow()
    claimed = repo.claim_visitor(db, vid, user.id) if vid else {}
    repo.log_event(db, "login", {"claimed": claimed}, vid, user.id)
    _set_cookie(response, SESSION_COOKIE, token, settings.auth_session_days)
    return {"user": _public(user), "claimed": claimed}


def register(db: Session, email: str, password: str, name: str | None, request: Request, response: Response) -> dict:
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(422, "Проверьте почту")
    if len(password) < MIN_PASSWORD:
        raise HTTPException(422, f"Пароль — не короче {MIN_PASSWORD} символов")
    if db.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(409, "Эта почта уже зарегистрирована — войдите")
    user = User(email=email, password_hash=hash_password(password), name=(name or "").strip()[:120] or None,
                role=_role_for(email))
    db.add(user)
    db.flush()
    return _start_session(db, user, request, response)


def login(db: Session, email: str, password: str, request: Request, response: Response) -> dict:
    email = email.strip().lower()
    _check_rate(email)
    user = db.scalar(select(User).where(User.email == email))
    if not user or not verify_password(password, user.password_hash):
        _fail(email)
        raise HTTPException(401, "Неверная почта или пароль")
    _fails.pop(email, None)
    if user.role != "admin" and _role_for(email) == "admin":
        user.role = "admin"
    return _start_session(db, user, request, response)


def logout(db: Session, request: Request, response: Response) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        s = db.get(AuthSession, _token_hash(token))
        if s and s.revoked_at is None:
            s.revoked_at = utcnow()
            repo.log_event(db, "logout", None, visitor_id(request), s.user_id)
    response.delete_cookie(SESSION_COOKIE, path="/")


def current_user(db: Session, request: Request) -> User | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    s = db.get(AuthSession, _token_hash(token))
    if not s or s.revoked_at is not None or s.expires_at < utcnow():
        return None
    return db.get(User, s.user_id)


def whoami(request: Request) -> tuple[str | None, int | None, str | None]:
    """(visitor_id, user_id, role) текущего запроса — для журнала и проверок доступа."""
    def q(db):
        u = current_user(db, request)
        return (u.id, u.role) if u else (None, None)
    uid, role = repo.record(q, default=(None, None))
    return visitor_id(request), uid, role


def public(u: User | None) -> dict | None:
    return _public(u) if u else None


def sber_id_login() -> None:
    """ЗАГЛУШКА: вход через Сбер ID (OAuth 2.0 / OpenID Connect). Нужны client_id и договор с банком."""
    raise HTTPException(501, "Вход через Сбер ID появится после подключения к партнёрской программе Сбер ID")
