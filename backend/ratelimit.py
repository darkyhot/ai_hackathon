"""Ограничение частоты запросов (защита от перебора, спама агенту и расходов на модель и телефонию).

Скользящее окно в памяти процесса: ключ — браузер (cookie vid) или IP. Для нескольких серверов — Redis.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque

# путь (префикс) и метод -> (сколько запросов, за сколько секунд)
LIMITS: list[tuple[str, str, int, float]] = [
    ("POST", "/api/session", 10, 600),          # новые диалоги
    ("POST", "/api/chat/", 30, 60),             # сообщения агенту
    ("POST", "/api/session/", 60, 60),          # карточки, продавец, продукты
    ("POST", "/api/stt", 40, 60),               # распознавание речи
    ("POST", "/api/tts", 40, 60),               # синтез речи (платный)
    ("POST", "/api/auth/", 20, 600),            # вход и регистрация
]

_hits: dict[tuple[str, str], deque] = defaultdict(deque)


def check(method: str, path: str, who: str, now: float | None = None) -> float | None:
    """None — можно; иначе через сколько секунд повторить."""
    now = time.monotonic() if now is None else now
    for m, prefix, limit, window in LIMITS:
        if method != m or not path.startswith(prefix):
            continue
        q = _hits[(prefix, who)]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return max(1.0, window - (now - q[0]))
        q.append(now)
        return None
    return None


def reset() -> None:
    _hits.clear()
