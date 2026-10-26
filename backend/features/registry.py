"""ЗАГЛУШКА. Признаки из реестров (ЕГРН, ФССП, ЕФРСБ, ЕИСЖС) — появятся с подключением выписок.

Тот же контракт: compute(listing) -> list[Value] с source="registry".
Результаты проверок безопасности дополнительно пишутся в security_checks (agent/security.py).
"""
from __future__ import annotations

from .base import Value

PROVIDER = "registry"

FEATURES = [
    ("owners_count", "Собственников по ЕГРН", "num", None, "сделка", PROVIDER, False, None),
    ("ownership_years", "Лет в собственности", "num", None, "сделка", PROVIDER, False, "меньше 3 лет — повод проверить историю"),
    ("encumbrance", "Обременение по ЕГРН", "bool", None, "сделка", PROVIDER, False, "ипотека, арест, рента"),
    ("seller_debts", "Долги продавца (ФССП)", "num", "₽", "сделка", PROVIDER, False, None),
    ("developer_delays", "Переносы сроков у застройщика", "num", None, "новостройка", PROVIDER, False, None),
]


def compute(listing: dict) -> list[Value]:
    raise NotImplementedError("реестры не подключены")
