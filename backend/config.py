"""Настройки приложения из .env."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value and value.strip() else default


@dataclass(frozen=True)
class Settings:
    llm_provider: str = os.getenv("LLM_PROVIDER", "deepseek").strip().lower()
    llm_fallback_to_mock: bool = _bool("LLM_FALLBACK_TO_MOCK", True)
    # Демо-режим: объявления — из сохранённого среза Циана в репозитории (backend/data/demo), без сети
    demo: bool = _bool("DEMO", False)
    # Шаг «Проверка безопасности» (реестры через гос. сайты). Пока скрыт: включить — SECURITY_STEP=true
    security_step: bool = _bool("SECURITY_STEP", False)
    # Площадки объявлений через запятую. Сейчас подключён только Циан; новые площадки — модули в backend/sources
    sources: str = os.getenv("SOURCES", "cian")

    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    deepseek_base_url: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

    gigachat_credentials: str = os.getenv("GIGACHAT_CREDENTIALS", "")
    gigachat_scope: str = os.getenv("GIGACHAT_SCOPE") or "GIGACHAT_API_PERS"
    gigachat_model: str = os.getenv("GIGACHAT_MODEL") or "GigaChat-2-Max"
    gigachat_auth_url: str = os.getenv("GIGACHAT_AUTH_URL") or "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    gigachat_base_url: str = os.getenv("GIGACHAT_BASE_URL") or "https://api.giga.chat/v1"
    gigachat_verify_ssl: bool = _bool("GIGACHAT_VERIFY_SSL", True)
    gigachat_ca_bundle: str = os.getenv("GIGACHAT_CA_BUNDLE", "")

    # База данных: SQLite по умолчанию; для PostgreSQL — postgresql+psycopg://user:pass@host/db
    database_url: str = os.getenv("DATABASE_URL") or f"sqlite:///{ROOT / 'backend' / 'data' / 'app.db'}"
    # Сколько часов сохранённая выдача считается свежей (не ходим на площадку заново)
    listings_fresh_hours: int = _int("LISTINGS_FRESH_HOURS", 24)
    # Ежедневный обход объявлений: время «ЧЧ:ММ» (пусто — выключен в процессе сервера; есть отдельный воркер)
    refresh_at: str = os.getenv("REFRESH_AT", "")
    refresh_pages: int = _int("REFRESH_PAGES", 3)
    # Проверка страницы объявления перед показом клиенту: сколько минут доверяем прошлой проверке
    # и сколько секунд максимум ждём проверку одной подборки
    listing_check_ttl_min: int = _int("LISTING_CHECK_TTL_MIN", 30)
    listing_check_budget_s: int = _int("LISTING_CHECK_BUDGET_S", 25)
    # Сколько обходов подряд объявления нет в выдаче, прежде чем считать его снятым
    remove_after_misses: int = _int("REMOVE_AFTER_MISSES", 2)
    # Скачивать фото квартир на диск (иначе храним только ссылки)
    store_photos: bool = _bool("STORE_PHOTOS", False)
    photos_dir: str = os.getenv("PHOTOS_DIR") or str(ROOT / "backend" / "data" / "photos")

    # Пользователи: срок жизни входа и почты администраторов (через запятую)
    auth_session_days: int = _int("AUTH_SESSION_DAYS", 30)
    admin_emails: str = os.getenv("ADMIN_EMAILS", "")
    # Cookie с флагом Secure (включить за HTTPS)
    cookie_secure: bool = _bool("COOKIE_SECURE", False)


settings = Settings()
