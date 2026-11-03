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


@dataclass(frozen=True)
class Settings:
    llm_provider: str = os.getenv("LLM_PROVIDER", "deepseek").strip().lower()

    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    deepseek_base_url: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

    gigachat_credentials: str = os.getenv("GIGACHAT_CREDENTIALS", "")
    gigachat_scope: str = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
    gigachat_model: str = os.getenv("GIGACHAT_MODEL", "GigaChat-2-Max")
    gigachat_auth_url: str = os.getenv(
        "GIGACHAT_AUTH_URL", "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    )
    gigachat_base_url: str = os.getenv("GIGACHAT_BASE_URL") or "https://api.giga.chat/v1"
    gigachat_verify_ssl: bool = _bool("GIGACHAT_VERIFY_SSL", True)
    gigachat_ca_bundle: str = os.getenv("GIGACHAT_CA_BUNDLE", "")

    # Время активной работы: ожидание ответа человека в него не входит.
    agent_turn_timeout_s: float = max(1, float(os.getenv("AGENT_TURN_TIMEOUT_S") or 120))
    gigachat_queue_size: int = max(0, int(os.getenv("GIGACHAT_QUEUE_SIZE") or 6))
    gigachat_queue_timeout_s: float = max(.1, float(os.getenv("GIGACHAT_QUEUE_TIMEOUT_S") or 25))
    llm_call_timeout_s: float = max(1, float(os.getenv("LLM_CALL_TIMEOUT_S") or 60))

    # Voximplant: настоящий звонок продавцу (звонок, синтез речи и распознавание — в облаке Voximplant).
    # Доступ к API — один из двух способов: ID аккаунта + API-ключ или JSON-файл сервисного аккаунта.
    voximplant_account_id: str = os.getenv("VOXIMPLANT_ACCOUNT_ID", "").strip()
    voximplant_api_key: str = os.getenv("VOXIMPLANT_API_KEY", "").strip()
    voximplant_credentials: str = os.getenv("VOXIMPLANT_CREDENTIALS", "").strip()  # путь к JSON сервисного аккаунта
    voximplant_app: str = os.getenv("VOXIMPLANT_APP") or "domclick-agent"         # приложение создаётся само
    # Узел аккаунта для Web SDK (NODE_4 …; виден на главной кабинета). Пусто — по домену приложения «…n4.voximplant…»
    voximplant_node: str = os.getenv("VOXIMPLANT_NODE", "").strip().upper()
    voximplant_api_host: str = os.getenv("VOXIMPLANT_API_HOST") or "api.voximplant.com"
    voximplant_caller_id: str = os.getenv("VOXIMPLANT_CALLER_ID", "").strip()       # пусто — первый арендованный номер
    voximplant_voice: str = os.getenv("VOXIMPLANT_VOICE") or "SaluteSpeech.ru_RU_sergey"  # голос агента (VoiceList)
    voximplant_asr: str = os.getenv("VOXIMPLANT_ASR") or "SaluteSpeech.ru_RU"         # распознавание (ASRProfileList)
    seller_phone: str = os.getenv("SELLER_PHONE", "")
    # PIN для звонков на публичном сервере: без него любой посетитель звонил бы за ваш счёт
    phone_pin: str = os.getenv("PHONE_PIN", "")

    # База: пользователи, история диалогов, объявления с признаками
    database_url: str = os.getenv("DATABASE_URL") or f"sqlite:///{ROOT / 'backend' / 'data' / 'app.db'}"
    auth_session_days: int = int(os.getenv("AUTH_SESSION_DAYS", "30") or 30)
    cookie_secure: bool = _bool("COOKIE_SECURE", False)  # true — за HTTPS

    # 2GIS: каталог инфраструктуры и растровая подложка. Без ключа — OpenStreetMap.
    twogis_api_key: str = os.getenv("TWOGIS_API_KEY", "").strip()
    places_budget_s: float = max(0.1, float(os.getenv("PLACES_BUDGET_S") or 12))

    # Ночной обход объявлений: время (ЧЧ:ММ, пусто — выключен), сколько страниц выдачи на поиск,
    # сколько пропавших объявлений проверить за раз
    refresh_at: str = os.getenv("REFRESH_AT", "").strip()
    refresh_pages: int = int(os.getenv("REFRESH_PAGES", "3") or 3)
    refresh_check_limit: int = int(os.getenv("REFRESH_CHECK_LIMIT", "40") or 40)


settings = Settings()
