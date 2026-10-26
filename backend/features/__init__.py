"""Признаки квартир для персонального подбора.

Каждый поставщик признаков — модуль с PROVIDER, FEATURES и compute(listing) -> list[Value].
Значения пишутся в listing_features (квартира → признак → значение) с источником и уверенностью.

Сейчас считается только cian (из полей объявления). Заглушки — maps (школы, сады, парки,
шум, время до работы) и registry (собственники, обременения): их делает команда
персонализации, схема и каталог признаков уже готовы — достаточно реализовать compute().
Признаки из звонка продавцу пишет речевая аналитика (agent/call_analytics.py), источник seller_call.
"""
from __future__ import annotations

from . import cian, maps, registry
from .base import Value

PROVIDERS = [cian, maps, registry]

# Признаки, которые пишут не поставщики, а агент
AGENT_FEATURES = [
    # key, title, kind, unit, group, provider, implemented, description
    ("fair_price", "Справедливая цена", "num", "₽", "цена", "model", True, "медиана цены м² похожих объявлений × площадь"),
    ("overprice_pct", "Дороже рынка на", "num", "%", "цена", "model", True, "насколько цена объявления выше справедливой"),
    ("owners", "Собственники (со слов продавца)", "text", None, "звонок", "seller_call", True, "сколько собственников и как давно"),
    ("contract", "Договор (со слов продавца)", "text", None, "звонок", "seller_call", True, "ДДУ, уступка, эскроу"),
    ("deadline_call", "Срок сдачи (со слов продавца)", "text", None, "звонок", "seller_call", True, "сдача дома и выдача ключей"),
    ("bargain_room", "Готовность торговаться", "num", "₽", "звонок", "seller_call", True, "на сколько продавец снизил цену в звонке"),
]


def catalog() -> list[tuple]:
    rows = [f for p in PROVIDERS for f in p.FEATURES]
    return rows + AGENT_FEATURES


def compute(listing: dict) -> list[Value]:
    """Все признаки, которые можно посчитать сейчас. Заглушки возвращают пустой список."""
    out: list[Value] = []
    for p in PROVIDERS:
        try:
            out += p.compute(listing)
        except NotImplementedError:
            continue
    return out


__all__ = ["Value", "catalog", "compute", "PROVIDERS"]
