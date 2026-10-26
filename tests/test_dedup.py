"""Склейка дублей (sources/dedup.py) — механизм на будущее, когда площадок станет больше одной."""
import os
os.environ.setdefault("DATABASE_URL", "sqlite://")  # тесты не трогают рабочую базу
import unittest

from backend.sources import dedup


def flat(src, **kw):
    base = {"id": f"{src}-1", "source": src, "source_name": src.title(), "address": "улица Халева, 3", "rooms": 2,
            "area": 43.1, "floor": 1, "price": 8_800_000, "lat": 55.7690, "lng": 49.1720, "is_new": False,
            "seller_role": "агент продавца"}
    return {**base, **kw}


class DedupTest(unittest.TestCase):
    def test_same_flat_two_sources(self):
        merged = dedup.merge([flat("a"), flat("b", price=8_790_000, kitchen=6.0)])
        self.assertEqual(len(merged), 1)
        self.assertEqual([o["price"] for o in merged[0]["offers"]], [8_790_000, 8_800_000])
        self.assertEqual(merged[0]["kitchen"], 6.0)

    def test_not_merged(self):
        self.assertEqual(len(dedup.merge([flat("a"), flat("b", floor=3)])), 2)
        self.assertEqual(len(dedup.merge([flat("a"), flat("a", id="a-2")])), 2, "внутри одной площадки не склеиваем")

    def test_new_build_by_complex_without_coords(self):
        a = flat("a", is_new=True, complex="Батталовский", seller_role="застройщик", area=33.2, floor=12, lat=None, lng=None, address="ЖК Батталовский")
        b = flat("b", is_new=True, complex="ЖК Батталовский", seller_role="застройщик", area=33.23, floor=None, floor_range="9–12", price=8_600_000, lat=55.72, lng=49.18, address="ул. Рауиса Гареева, 125/1")
        self.assertEqual(len(dedup.merge([a | {"price": 8_600_000}, b])), 1)
        self.assertEqual(len(dedup.merge([a | {"seller_role": "агент продавца (переуступка)", "price": 8_600_000}, b])), 2)


if __name__ == "__main__":
    unittest.main()
