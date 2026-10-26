"""Признаки из полей объявления Циана — считаются сразу при сохранении квартиры."""
from __future__ import annotations

from .base import Value

PROVIDER = "cian"

# key, title, kind, unit, group, provider, implemented, description
FEATURES = [
    ("area", "Общая площадь", "num", "м²", "квартира", PROVIDER, True, None),
    ("kitchen", "Площадь кухни", "num", "м²", "квартира", PROVIDER, True, None),
    ("floor", "Этаж", "num", None, "квартира", PROVIDER, True, None),
    ("floors", "Этажей в доме", "num", None, "дом", PROVIDER, True, None),
    ("is_first_floor", "Первый этаж", "bool", None, "квартира", PROVIDER, True, None),
    ("is_last_floor", "Последний этаж", "bool", None, "квартира", PROVIDER, True, None),
    ("year", "Год постройки", "num", None, "дом", PROVIDER, True, None),
    ("material", "Материал стен", "text", None, "дом", PROVIDER, True, None),
    ("condition", "Ремонт / отделка", "text", None, "квартира", PROVIDER, True, "из поля объявления или из описания"),
    ("metro_minutes", "До метро", "num", "мин", "транспорт", PROVIDER, True, None),
    ("price_per_m2", "Цена за м²", "num", "₽", "цена", PROVIDER, True, None),
    ("days_on_market", "Дней в продаже", "num", "дн", "цена", PROVIDER, True, "долго висит — продавец сговорчивее"),
    ("owner_selling", "Продаёт собственник", "bool", None, "сделка", PROVIDER, True, None),
    ("rosreestr_checked", "Собственник сверен с Росреестром", "bool", None, "сделка", PROVIDER, True, "отметка площадки"),
]


def compute(x: dict) -> list[Value]:
    out = []

    def add(key, value, evidence=None):
        if value is not None:
            out.append(Value(key, value, PROVIDER, 1.0, evidence))

    add("area", x.get("area"))
    add("kitchen", x.get("kitchen"))
    add("floor", x.get("floor"))
    add("floors", x.get("floors"))
    if x.get("floor"):
        add("is_first_floor", x["floor"] == 1)
        if x.get("floors"):
            add("is_last_floor", x["floor"] == x["floors"])
    add("year", x.get("year"))
    add("material", x.get("material"))
    add("condition", x.get("condition"))
    add("metro_minutes", (x.get("metro") or {}).get("minutes"))
    if x.get("price") and x.get("area"):
        add("price_per_m2", round(x["price"] / x["area"]), "цена / площадь")
    add("days_on_market", x.get("days_on_market"))
    if x.get("seller_role"):
        add("owner_selling", x["seller_role"].startswith("собственник"), x["seller_role"])
    add("rosreestr_checked", x.get("rosreestr_checked"))
    return out
