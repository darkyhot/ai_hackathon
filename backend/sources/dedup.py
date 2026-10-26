"""Склейка дублей: одна и та же квартира на разных площадках — одна карточка.

Признаки одной квартиры: те же комнаты, площадь почти совпадает, тот же этаж (если известен
у обоих), дом рядом (по координатам или по адресу) и цена отличается не больше чем на 12%.
Внутри одной площадки не склеиваем: разные объявления одного дома — разные квартиры.

Карточка-итог — самое дешёвое объявление, в нём список всех площадок с ценами и ссылками:
агент может сказать «на другой площадке та же квартира на 50 000 ₽ дешевле». Сейчас подключён только Циан —
склейка включится, когда добавятся новые площадки.
"""
from __future__ import annotations

import math
import re

MAX_KM = 0.35          # координаты одного дома на разных площадках расходятся на сотни метров
MAX_PRICE_DIFF = 0.12


def _km(a: dict, b: dict) -> float | None:
    if not (a.get("lat") and a.get("lng") and b.get("lat") and b.get("lng")):
        return None
    lat1, lng1, lat2, lng2 = map(math.radians, (a["lat"], a["lng"], b["lat"], b["lng"]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def _street_house(address: str | None) -> tuple[str, str] | None:
    """«улица Халева, 3» -> («халева», «3»). Тип улицы и регистр не важны."""
    a = (address or "").lower().replace("ё", "е")
    a = re.sub(r"\b(улица|ул|проспект|пр-кт|переулок|пер|шоссе|бульвар|б-р|проезд|площадь|набережная)\b\.?", " ", a)
    parts = [p.strip() for p in a.split(",") if p.strip()]
    if len(parts) < 2:
        return None
    house = re.match(r"\d+[а-я]?", parts[-1])
    street = re.sub(r"\s+", " ", parts[-2]).strip()
    return (street, house.group(0)) if house and street else None


def _floor_ok(a: dict, b: dict) -> bool:
    """У планировки застройщика этаж — диапазон «от–до»; иначе этажи должны совпасть (если известны)."""
    for x, y in ((a, b), (b, a)):
        if x.get("floor_range") and y.get("floor"):
            lo, hi = (int(v) for v in x["floor_range"].split("–"))
            return lo <= y["floor"] <= hi
    return not (a.get("floor") and b.get("floor")) or a["floor"] == b["floor"]


def difference(a: dict, b: dict) -> float | None:
    """None — разные квартиры; иначе «расстояние» между объявлениями (меньше — похожее)."""
    if a.get("source") == b.get("source") or a.get("rooms") != b.get("rooms"):
        return None
    new = a.get("is_new") or b.get("is_new")
    # в новостройке много почти одинаковых планировок: допуски строже, застройщика с переуступкой не путаем
    area_tol, price_tol = (0.3, 0.03) if new else (max(0.6, 0.015 * a["area"]), MAX_PRICE_DIFF)
    if new and (a.get("seller_role") == "застройщик") != (b.get("seller_role") == "застройщик"):
        return None
    d_area = abs(a["area"] - b["area"])
    d_price = abs(a["price"] - b["price"]) / min(a["price"], b["price"])
    if d_area > area_tol or d_price > price_tol or not _floor_ok(a, b):
        return None
    km = _km(a, b)
    if km is not None:
        if km > MAX_KM:
            return None
    else:  # координат нет хотя бы у одного: сверяем адрес, а у новостроек — ещё и название ЖК
        sa, sb = _street_house(a.get("address")), _street_house(b.get("address"))
        same_complex = bool(new and _complex(a) and _complex(a) == _complex(b))
        if not ((sa and sb and sa == sb) or same_complex):
            return None
    return d_area + d_price * 10


def _complex(x: dict) -> str | None:
    """«ЖК «Батталовский»», «Батталовский», «ЖК Батталовский» -> «батталовский»."""
    name = (x.get("complex") or "").lower().replace("ё", "е")
    name = re.sub(r"^жк\s+|[«»\"]", "", name).strip()
    return name or None


def same_flat(a: dict, b: dict) -> bool:
    return difference(a, b) is not None


def offer_of(x: dict) -> dict:
    return {"source": x.get("source"), "source_name": x.get("source_name"), "price": x["price"], "url": x.get("url"),
            "id": x["id"]}


def merge(items: list[dict]) -> list[dict]:
    """Объединить дубли. Порядок — по цене (самое дешёвое объявление становится карточкой)."""
    groups: list[list[dict]] = []
    for x in sorted(items, key=lambda i: i["price"]):
        best, best_d = None, None
        for g in groups:  # самая похожая группа, где этой площадки ещё нет
            if any(x.get("source") == y.get("source") for y in g):
                continue
            d = difference(g[0], x)
            if d is not None and (best_d is None or d < best_d):
                best, best_d = g, d
        if best is not None:
            best.append(x)
        else:
            groups.append([x])
    out = []
    for g in groups:
        head = dict(g[0])
        if len(g) > 1:
            # чего нет в самом дешёвом объявлении — берём из дубля (кухня, год, описание, ЖК…)
            for other in g[1:]:
                for k, v in other.items():
                    if k == "floor_range" and head.get("floor"):
                        continue  # точный этаж уже есть
                    if head.get(k) in (None, "", []) and v not in (None, "", []):
                        head[k] = v
                if (head.get("address") or "").startswith("ЖК") and _street_house(other.get("address")):
                    head["address"] = other["address"]  # адрес с улицей и домом понятнее названия ЖК
        head["offers"] = [offer_of(y) for y in g]
        head["sources"] = [y.get("source_name") for y in g]
        out.append(head)
    return out


def duplicates_count(items: list[dict]) -> int:
    return sum(len(x.get("offers") or [1]) - 1 for x in items)
