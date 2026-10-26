"""Схема базы. Диаграмма и пояснения — docs/ERD.md.

Группы таблиц:
  люди и журнал      users, visitors, auth_sessions, events, agent_sessions, finance_profiles
  квартиры           listings, listing_photos, listing_price_history, complexes, developers
  признаки квартир   features (каталог), listing_features (значения: «квартира → признак → значение»)
  обход площадок     tracked_searches, crawl_runs
  выбор клиента      user_listing_marks
  звонки             calls, call_facts (факты от продавца с уровнем доверия)
  безопасность       security_checks
  сделка             deals (заглушка)

Все время — UTC без часового пояса (utcnow()).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSON, list: JSON}


# ================================================================ люди и журнал


class User(Base):
    """Зарегистрированный пользователь. Пароль — только хеш (scrypt)."""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))  # None — вход только через Сбер ID
    name: Mapped[str | None] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(20), default="user")  # user | admin
    sber_id: Mapped[str | None] = mapped_column(String(64), unique=True)  # заглушка: вход через Сбер ID
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)

    visitors: Mapped[list["Visitor"]] = relationship(back_populates="user")


class Visitor(Base):
    """Браузер посетителя: cookie «vid». До регистрации всё пишется на него, при входе — привязывается к user."""
    __tablename__ = "visitors"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # uuid4 из cookie
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    user_agent: Mapped[str | None] = mapped_column(String(300))
    consent_at: Mapped[datetime | None] = mapped_column(DateTime)  # согласился с использованием cookie

    user: Mapped[User | None] = relationship(back_populates="visitors")


class AuthSession(Base):
    """Вход пользователя. В cookie «sid» — случайный токен, в базе — только его SHA-256."""
    __tablename__ = "auth_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    visitor_id: Mapped[str | None] = mapped_column(ForeignKey("visitors.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    user_agent: Mapped[str | None] = mapped_column(String(300))


class AgentSession(Base):
    """Один разговор с агентом: запрос, понятый профиль, чем закончилось."""
    __tablename__ = "agent_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    visitor_id: Mapped[str | None] = mapped_column(ForeignKey("visitors.id", ondelete="SET NULL"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    query: Mapped[str] = mapped_column(Text)
    profile: Mapped[dict | None] = mapped_column(JSON)  # город, комнаты, бюджет, семья, способ оплаты…
    status: Mapped[str] = mapped_column(String(20), default="running")  # running | completed | cancelled | saving | empty | paused | exhausted | error
    llm_provider: Mapped[str | None] = mapped_column(String(30))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    result: Mapped[dict | None] = mapped_column(JSON)  # итог: объект, цена, экономия


class Event(Base):
    """Журнал действий: что делал посетитель и что ответил агент. Основа персонализации и аналитики."""
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    visitor_id: Mapped[str | None] = mapped_column(ForeignKey("visitors.id", ondelete="SET NULL"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    agent_session_id: Mapped[str | None] = mapped_column(ForeignKey("agent_sessions.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(40), index=True)  # session_started, client_message, confirm, selection, call, …
    payload: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class FinanceProfile(Base):
    """Финансовый профиль (заглушка): в проде — из СберБанк Онлайн с согласия клиента. Сейчас — то, что ввёл клиент."""
    __tablename__ = "finance_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    visitor_id: Mapped[str | None] = mapped_column(ForeignKey("visitors.id", ondelete="SET NULL"), index=True)
    monthly_income: Mapped[int | None] = mapped_column(Integer)
    monthly_expenses: Mapped[int | None] = mapped_column(Integer)
    savings: Mapped[int | None] = mapped_column(Integer)
    children: Mapped[int | None] = mapped_column(Integer)
    youngest_age: Mapped[int | None] = mapped_column(Integer)
    matcap: Mapped[int | None] = mapped_column(Integer)
    payment: Mapped[str | None] = mapped_column(String(30))  # cash | mortgage_ready | mortgage_saving
    source: Mapped[str] = mapped_column(String(30), default="client_input")  # client_input | sber_online (заглушка)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


# ================================================================ квартиры


class Developer(Base):
    __tablename__ = "developers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    inn: Mapped[str | None] = mapped_column(String(12))            # заглушка: заполнит проверка по ЕГРЮЛ
    houses_delivered: Mapped[int | None] = mapped_column(Integer)  # заглушка: наш.дом.рф
    delays: Mapped[int | None] = mapped_column(Integer)            # заглушка: переносы сроков
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Complex(Base):
    """Жилой комплекс (новостройки)."""
    __tablename__ = "complexes"
    __table_args__ = (UniqueConstraint("city", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    city: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(200))
    developer_id: Mapped[int | None] = mapped_column(ForeignKey("developers.id", ondelete="SET NULL"))
    deadline: Mapped[str | None] = mapped_column(String(40))
    finished: Mapped[bool | None] = mapped_column(Boolean)
    nashdom_id: Mapped[str | None] = mapped_column(String(40))  # заглушка: id объекта в ЕИСЖС

    developer: Mapped[Developer | None] = relationship()


class Listing(Base):
    """Объявление о продаже квартиры. Ключ — «площадка-id» (cian-335167546), как в агенте."""
    __tablename__ = "listings"
    __table_args__ = (
        UniqueConstraint("source", "external_id"),
        Index("ix_listings_search", "city", "rooms", "is_new", "status", "price"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    source: Mapped[str] = mapped_column(String(20))
    external_id: Mapped[str] = mapped_column(String(30))
    url: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(12), default="active")  # active | removed

    city: Mapped[str] = mapped_column(String(60))
    district: Mapped[str | None] = mapped_column(String(100))
    okrug: Mapped[str | None] = mapped_column(String(20))
    address: Mapped[str | None] = mapped_column(String(300))
    lat: Mapped[float | None] = mapped_column(Float)
    lng: Mapped[float | None] = mapped_column(Float)

    rooms: Mapped[int] = mapped_column(Integer)
    area: Mapped[float] = mapped_column(Float)
    kitchen: Mapped[float | None] = mapped_column(Float)
    floor: Mapped[int | None] = mapped_column(Integer)
    floors: Mapped[int | None] = mapped_column(Integer)
    year: Mapped[int | None] = mapped_column(Integer)
    condition: Mapped[str | None] = mapped_column(String(40))
    material: Mapped[str | None] = mapped_column(String(40))
    price: Mapped[int] = mapped_column(Integer)
    metro_name: Mapped[str | None] = mapped_column(String(80))
    metro_minutes: Mapped[int | None] = mapped_column(Integer)
    metro_mode: Mapped[str | None] = mapped_column(String(20))
    description: Mapped[str | None] = mapped_column(Text)
    seller_role: Mapped[str | None] = mapped_column(String(60))
    rosreestr_checked: Mapped[bool | None] = mapped_column(Boolean)
    is_apartments: Mapped[bool] = mapped_column(Boolean, default=False)
    is_auction: Mapped[bool] = mapped_column(Boolean, default=False)

    is_new: Mapped[bool] = mapped_column(Boolean, default=False)
    complex_id: Mapped[int | None] = mapped_column(ForeignKey("complexes.id", ondelete="SET NULL"))
    sale_type: Mapped[str | None] = mapped_column(String(60))

    published_at: Mapped[datetime | None] = mapped_column(DateTime)  # когда объявление появилось на площадке
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)  # когда мы его впервые увидели
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)   # последний обход, где оно было
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)     # когда менялись данные
    removed_at: Mapped[datetime | None] = mapped_column(DateTime)
    miss_count: Mapped[int] = mapped_column(Integer, default=0)  # обходов подряд без этого объявления
    page_checked_at: Mapped[datetime | None] = mapped_column(DateTime)  # когда открывали страницу объявления
    page_alive: Mapped[bool | None] = mapped_column(Boolean)  # True — активно, False — снято, None — не удалось проверить
    page_note: Mapped[str | None] = mapped_column(String(120))

    data: Mapped[dict] = mapped_column(JSON)  # объявление целиком в формате агента (sources/base.py)

    complex: Mapped[Complex | None] = relationship()
    photos: Mapped[list["ListingPhoto"]] = relationship(back_populates="listing", cascade="all, delete-orphan",
                                                        order_by="ListingPhoto.position")
    prices: Mapped[list["ListingPrice"]] = relationship(back_populates="listing", cascade="all, delete-orphan",
                                                        order_by="ListingPrice.observed_at")
    features: Mapped[list["ListingFeature"]] = relationship(back_populates="listing", cascade="all, delete-orphan")


class ListingPhoto(Base):
    """Фото: ссылка на площадку и (по STORE_PHOTOS) копия на диске."""
    __tablename__ = "listing_photos"
    __table_args__ = (UniqueConstraint("listing_id", "url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[str] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(String(500))
    position: Mapped[int] = mapped_column(Integer, default=0)
    storage_path: Mapped[str | None] = mapped_column(String(300))
    sha256: Mapped[str | None] = mapped_column(String(64))
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime)

    listing: Mapped[Listing] = relationship(back_populates="photos")


class ListingPrice(Base):
    """История цены: новая строка при каждом изменении. Снижения — аргумент в торге."""
    __tablename__ = "listing_price_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[str] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), index=True)
    price: Mapped[int] = mapped_column(Integer)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    listing: Mapped[Listing] = relationship(back_populates="prices")


# ================================================================ признаки квартир


class Feature(Base):
    """Каталог признаков для персонализации: что это, откуда берётся, в чём измеряется."""
    __tablename__ = "features"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)     # school_distance_m
    title: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(10))                       # num | text | bool
    unit: Mapped[str | None] = mapped_column(String(20))
    group: Mapped[str | None] = mapped_column(String(40))               # квартира, дом, район, транспорт, звонок
    provider: Mapped[str] = mapped_column(String(30))                   # cian | maps | seller_call | model | registry
    implemented: Mapped[bool] = mapped_column(Boolean, default=False)   # False — заглушка, считает другой модуль
    description: Mapped[str | None] = mapped_column(Text)


class ListingFeature(Base):
    """Значение признака у квартиры. Источник и уверенность — у каждого значения."""
    __tablename__ = "listing_features"
    __table_args__ = (UniqueConstraint("listing_id", "feature_key", "source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[str] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), index=True)
    feature_key: Mapped[str] = mapped_column(ForeignKey("features.key"), index=True)
    value_num: Mapped[float | None] = mapped_column(Float)
    value_text: Mapped[str | None] = mapped_column(Text)
    value_bool: Mapped[bool | None] = mapped_column(Boolean)
    source: Mapped[str] = mapped_column(String(30))          # cian | maps | seller_call | registry | model
    confidence: Mapped[float] = mapped_column(Float, default=1.0)  # 0..1
    evidence: Mapped[str | None] = mapped_column(Text)      # цитата, ссылка, как посчитано
    call_fact_id: Mapped[int | None] = mapped_column(ForeignKey("call_facts.id", ondelete="SET NULL"))
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    listing: Mapped[Listing] = relationship(back_populates="features")


# ================================================================ обход площадок


class TrackedSearch(Base):
    """Поиск, который обновляем каждый день: его кто-то уже задавал (город, комнаты, вторичка/новостройки)."""
    __tablename__ = "tracked_searches"
    __table_args__ = (UniqueConstraint("source", "city", "rooms", "is_new", "okrug"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(20), default="cian")
    city: Mapped[str] = mapped_column(String(60))
    rooms: Mapped[int] = mapped_column(Integer)
    is_new: Mapped[bool] = mapped_column(Boolean, default=False)
    okrug: Mapped[str] = mapped_column(String(20), default="")  # "" — весь город
    budget_max: Mapped[int | None] = mapped_column(Integer)     # самый большой бюджет, который спрашивали; None — без ограничения
    requested_count: Mapped[int] = mapped_column(Integer, default=0)
    last_requested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_crawled_at: Mapped[datetime | None] = mapped_column(DateTime)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class CrawlRun(Base):
    """Один обход: когда, что нашли, что сняли. По этой таблице видно, жив ли сервис обновления."""
    __tablename__ = "crawl_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    trigger: Mapped[str] = mapped_column(String(20))  # daily | manual | search
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(12), default="running")  # running | ok | partial | failed
    searches: Mapped[int] = mapped_column(Integer, default=0)
    seen: Mapped[int] = mapped_column(Integer, default=0)
    added: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    price_changed: Mapped[int] = mapped_column(Integer, default=0)
    removed: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list | None] = mapped_column(JSON)


# ================================================================ выбор клиента


class UserListingMark(Base):
    """«Нравится» / «Не то» / просмотр — сигнал для персонального подбора."""
    __tablename__ = "user_listing_marks"
    __table_args__ = (UniqueConstraint("visitor_id", "listing_id", "mark"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    visitor_id: Mapped[str | None] = mapped_column(ForeignKey("visitors.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    listing_id: Mapped[str] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"))
    agent_session_id: Mapped[str | None] = mapped_column(ForeignKey("agent_sessions.id", ondelete="SET NULL"))
    mark: Mapped[str] = mapped_column(String(12))  # like | dislike | shown | chosen
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ================================================================ звонки


class Call(Base):
    """Звонок агента продавцу: расшифровка и итог."""
    __tablename__ = "calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_session_id: Mapped[str | None] = mapped_column(ForeignKey("agent_sessions.id", ondelete="SET NULL"), index=True)
    listing_id: Mapped[str] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), index=True)
    visitor_id: Mapped[str | None] = mapped_column(ForeignKey("visitors.id", ondelete="SET NULL"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    mode: Mapped[str] = mapped_column(String(12))  # auto — продавец-симулятор, human — отвечает человек; voice — заглушка телефонии
    seller_role: Mapped[str | None] = mapped_column(String(60))  # без имени и телефона продавца
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    transcript: Mapped[list] = mapped_column(JSON)  # [{"speaker": "agent"|"seller", "text": ...}]
    status: Mapped[str | None] = mapped_column(String(12))  # agreed | no_deal
    asking_price: Mapped[int | None] = mapped_column(Integer)
    agreed_price: Mapped[int | None] = mapped_column(Integer)
    viewing_slot: Mapped[str | None] = mapped_column(String(60))
    analytics: Mapped[dict | None] = mapped_column(JSON)  # сводка речевой аналитики

    facts: Mapped[list["CallFact"]] = relationship(back_populates="call", cascade="all, delete-orphan")


class CallFact(Base):
    """Факт от продавца. Продавец мог ошибиться, передумать или приукрасить — поэтому у факта есть уверенность."""
    __tablename__ = "call_facts"

    id: Mapped[int] = mapped_column(primary_key=True)
    call_id: Mapped[int] = mapped_column(ForeignKey("calls.id", ondelete="CASCADE"), index=True)
    listing_id: Mapped[str] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(40))     # condition, year, owners, price_agreement…
    label: Mapped[str] = mapped_column(String(80))
    value: Mapped[str] = mapped_column(Text)
    quote: Mapped[str | None] = mapped_column(Text)  # слова продавца — доказательство
    confidence: Mapped[float] = mapped_column(Float)
    level: Mapped[str] = mapped_column(String(8))    # high | medium | low
    hedged: Mapped[bool] = mapped_column(Boolean, default=False)              # «вроде», «не помню»
    contradicts_listing: Mapped[bool] = mapped_column(Boolean, default=False)  # расходится с объявлением
    changed_during_call: Mapped[bool] = mapped_column(Boolean, default=False)  # сначала сказал одно, потом другое
    reasons: Mapped[list | None] = mapped_column(JSON)  # почему такая уверенность — человеческим языком
    verify_with: Mapped[str | None] = mapped_column(String(120))  # чем подтвердить: выписка ЕГРН, просмотр…
    verified_status: Mapped[str] = mapped_column(String(12), default="unverified")  # unverified | confirmed | refuted
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    call: Mapped[Call] = relationship(back_populates="facts")


# ================================================================ безопасность и сделка


class SecurityCheck(Base):
    """Результат одной проверки безопасности по объекту. Реестры — заглушки: статус not_checked, пока нет выписки."""
    __tablename__ = "security_checks"
    __table_args__ = (UniqueConstraint("listing_id", "check_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[str] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), index=True)
    check_key: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(14))  # ok | info | warn | partial | not_checked | done
    method: Mapped[str | None] = mapped_column(Text)   # как проверяли
    source: Mapped[str | None] = mapped_column(String(120))
    source_url: Mapped[str | None] = mapped_column(String(300))
    evidence: Mapped[dict | None] = mapped_column(JSON)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)  # выписки устаревают — перепроверка перед сделкой


class Deal(Base):
    """Сделка (заглушка): в проде — статусы из Домклик и СБР."""
    __tablename__ = "deals"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_session_id: Mapped[str | None] = mapped_column(ForeignKey("agent_sessions.id", ondelete="SET NULL"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    visitor_id: Mapped[str | None] = mapped_column(ForeignKey("visitors.id", ondelete="SET NULL"))
    listing_id: Mapped[str] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | signed | registered | cancelled
    base_price: Mapped[int] = mapped_column(Integer)
    final_price: Mapped[int] = mapped_column(Integer)
    program: Mapped[str | None] = mapped_column(String(60))
    plan: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
