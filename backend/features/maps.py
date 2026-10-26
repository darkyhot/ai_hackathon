"""ЗАГЛУШКА. Признаки окружения по координатам квартиры: карты, POI, шум, дорога до работы.

Делает команда персонализации. Что нужно: по listing["lat"], listing["lng"] вернуть Value
для ключей из FEATURES (source="maps", evidence — откуда взято: «Яндекс Карты, 2026-10-26»).
Каталог признаков уже в базе (features), значения ляжут в listing_features без изменений схемы.
"""
from __future__ import annotations

from .base import Value

PROVIDER = "maps"

FEATURES = [
    ("school_distance_m", "До ближайшей школы", "num", "м", "район", PROVIDER, False, None),
    ("kindergarten_distance_m", "До ближайшего сада", "num", "м", "район", PROVIDER, False, None),
    ("park_distance_m", "До парка или сквера", "num", "м", "район", PROVIDER, False, None),
    ("clinic_distance_m", "До поликлиники", "num", "м", "район", PROVIDER, False, None),
    ("shops_500m", "Магазинов в 500 м", "num", None, "район", PROVIDER, False, None),
    ("center_distance_km", "До центра", "num", "км", "район", PROVIDER, False, None),
    ("noise_level", "Шум (дороги, ж/д)", "text", None, "район", PROVIDER, False, "низкий / средний / высокий"),
    ("commute_minutes", "Дорога до работы", "num", "мин", "транспорт", PROVIDER, False, "нужен адрес работы клиента"),
]


def compute(listing: dict) -> list[Value]:
    raise NotImplementedError("признаки окружения делает команда персонализации")
