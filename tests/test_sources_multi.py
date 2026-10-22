"""Несколько площадок: разбор Домклика, склейка дублей, общий сбор (без сети).

Запуск: .venv/bin/python -m unittest tests.test_sources_multi -v
"""
import asyncio
import json
import unittest
from datetime import date
from unittest import mock

from backend import sources
from backend.sources import cian, dedup, domclick
from backend.sources.static import avito, yandex

# Карточки — в структуре реальной выдачи Домклика по Казани (21.10.2026), личные данные убраны
PRODUCT = {
    "id": 2075942669, "$$type": "ProductSnippet", "offerType": "flat", "price": 8800000,
    "path": "https://kazan.domclick.ru/card/sale__flat__2075942669",
    "address": {"displayName": "Казань, улица Халева, 3",
                "subways": [{"name": "Аметьево", "remoteness": {"time": 9, "kind": 1}}]},
    "objectInfo": {"area": 43.1, "rooms": 2, "floor": 1, "isApartment": False}, "house": {"floors": 5, "buildYear": 1960},
    "roomsOffered": 0, "location": {"lat": 55.7686, "lon": 49.1712},
    "seoInfo": {"displayNameParts": [{"title": "Казань"}, {"isBuilding": True, "uri": "/building/privolzhskij--haleva--3"}]},
    "assignmentSale": False, "seller": {"agent": {"isAgent": True}}, "isOwner": False,
    "description": "Евроремонт, рядом школа", "isRosreestrApproved": True, "publishedDate": "2026-10-01T10:00:00+00:00",
    "hasFamilyMortgage": False, "photos": [{"url": "/vitrina/aa/bb/x.jpg"}],
}
LAYOUT = {
    "id": 2081550907, "$$type": "LayoutSnippet", "offerType": "layout", "price": 8600000,
    "path": "https://kazan.domclick.ru/card/sale__new_flat__2081550907",
    "address": {"displayName": "Россия, Казань, улица Рауиса Гареева, 125/1 стр", "subways": []},
    "complex": {"name": "ЖК Батталовский", "building": {"released": False, "endBuildYear": 2027, "endBuildQuarter": 1}},
    "generalInfo": {"area": 33.15, "rooms": 2, "minFloor": 14, "maxFloor": 18, "floors": 25, "isApartment": False},
    "mortgage": {"hasMortgageSubsidy": True, "minRate": 11}, "location": {"lat": 55.72158, "lon": 49.17884},
    "developerName": "Суварстроит", "publishedDate": "2026-10-20T09:38:49+00:00", "photos": [{"url": "/vitrina/75/6a/y.jpg"}],
}


def cian_flat(**kw):
    base = {"id": "cian-1", "source": "cian", "source_name": "Циан", "url": "https://kazan.cian.ru/sale/flat/1/",
            "city": "Казань", "address": "Халева, 3", "rooms": 2, "area": 43.1, "floor": 1, "floors": 5, "price": 8800000,
            "lat": 55.7690, "lng": 49.1720, "kitchen": 6.0, "is_new": False, "seller_role": "агент продавца"}
    return {**base, **kw}


class DomclickParseTest(unittest.TestCase):
    def test_state_from_page_with_undefined(self):
        page = '<script>window.__SSR_STATE__={"search":{"pages":{"0":{"ids":[1],"entities":{"1":' \
               + json.dumps(PRODUCT, ensure_ascii=False) + '}}}},"x":undefined};window.y=1;</script>'
        st = domclick.extract_state(page)
        self.assertIsNotNone(st)
        self.assertEqual(len(domclick.entities(st)), 1)

    def test_secondary_flat(self):
        x = domclick.normalize(PRODUCT, "Казань", today=date(2026, 10, 21))
        self.assertEqual((x["id"], x["source_name"], x["address"], x["district"]), ("domclick-2075942669", "Домклик", "улица Халева, 3", "Приволжский"))
        self.assertEqual((x["area"], x["floor"], x["floors"], x["year"], x["days_on_market"]), (43.1, 1, 5, 1960, 20))
        self.assertEqual(x["condition"], "евроремонт")
        self.assertFalse(x["is_new"])
        self.assertTrue(x["photo"].startswith("https://img.dmclk.ru/"))
        self.assertEqual(x["metro"]["name"], "Аметьево")

    def test_layout_from_developer(self):
        x = domclick.normalize(LAYOUT, "Казань", new=True)
        self.assertTrue(x["is_new"])
        self.assertEqual((x["complex"], x["deadline"], x["developer"], x["seller_role"]), ("Батталовский", "1 кв. 2027", "Суварстроит", "застройщик"))
        self.assertEqual((x["address"], x["floor"], x["floor_range"], x["sale_type"]), ("улица Рауиса Гареева, 125/1", 14, "14–18", "ДДУ (214-ФЗ)"))
        self.assertEqual(x["developer_rate"], 11)

    def test_assignment_is_new_build(self):
        x = domclick.normalize({**PRODUCT, "assignmentSale": True, "flatComplex": {"name": "Станция Спортивная"}}, "Казань")
        self.assertTrue(x["is_new"])
        self.assertEqual((x["seller_role"], x["sale_type"], x["complex"]), ("агент продавца (переуступка)", "договор уступки", "Станция Спортивная"))

    def test_url(self):
        u = domclick.build_url("Казань", 2, 9_000_000, page=2, new=True)
        for part in ("offer_type=layout", "address=26f533ee", "rooms=2", "offset=20", "sale_price__lte=9450000"):
            self.assertIn(part, u)
        self.assertIn("offer_type=flat", domclick.build_url("Москва", 0, None))
        self.assertIn("rooms=st", domclick.build_url("Москва", 0, None))

    def test_qrator_page_is_not_data(self):
        self.assertTrue(domclick._is_qrator(401, "<script src='/__qrator/qauth.js'>"))
        self.assertFalse(domclick._is_qrator(200, "window.__SSR_STATE__={}"))


class DedupTest(unittest.TestCase):
    def test_same_flat_on_two_sites(self):
        d = domclick.normalize({**PRODUCT, "price": 8790000}, "Казань")
        merged = dedup.merge([cian_flat(), d])
        self.assertEqual(len(merged), 1)
        m = merged[0]
        self.assertEqual(m["source"], "domclick", "карточка — по самому дешёвому объявлению")
        self.assertEqual(m["sources"], ["Домклик", "Циан"])
        self.assertEqual([o["price"] for o in m["offers"]], [8790000, 8800000])
        self.assertEqual(m["kitchen"], 6.0, "чего нет на Домклике — берём с Циана")

    def test_different_flats_stay_apart(self):
        d = domclick.normalize(PRODUCT, "Казань")
        self.assertEqual(len(dedup.merge([cian_flat(floor=3), d])), 2, "другой этаж")
        self.assertEqual(len(dedup.merge([cian_flat(area=48.0), d])), 2, "другая площадь")
        self.assertEqual(len(dedup.merge([cian_flat(id="cian-2", source="cian"), cian_flat()])), 2, "внутри площадки не склеиваем")

    def test_new_build_developer_vs_assignment(self):
        lay = domclick.normalize(LAYOUT, "Казань", new=True)
        dev = cian_flat(address="Братьев Батталовых, 20Д", area=33.15, floor=15, price=8600000, lat=55.7232, lng=49.1801,
                        is_new=True, seller_role="застройщик")
        assign = {**dev, "id": "cian-3", "seller_role": "агент продавца (переуступка)", "price": 8500000}
        self.assertEqual(len(dedup.merge([dev, lay])), 1, "та же планировка у застройщика на двух сайтах")
        self.assertEqual(len(dedup.merge([assign, lay])), 2, "переуступку с квартирой застройщика не путаем")


class CollectTest(unittest.TestCase):
    def test_demo_both_sources_kazan(self):
        with mock.patch.object(cian, "demo_mode", return_value=True), \
             mock.patch.object(domclick, "demo_mode", return_value=True), \
             mock.patch.object(sources, "enabled", return_value=[cian, domclick]):
            items, report = asyncio.run(sources.collect({"city": "Казань", "rooms": 2, "budget_max": 9_000_000, "housing": "both"}))
        self.assertEqual([r["source"] for r in report], ["Циан · вторичка", "Циан · новостройки", "Домклик · вторичка", "Домклик · новостройки"])
        self.assertTrue(all(r["status"] == "demo" for r in report))
        self.assertGreater(dedup.duplicates_count(items), 10)
        self.assertEqual({o["source_name"] for x in items for o in x["offers"]}, {"Циан", "Домклик"})

    def test_one_source_down_other_works(self):
        async def down(*a, **kw):
            raise sources.SourceUnavailable("защита Qrator")
        with mock.patch.object(cian, "demo_mode", return_value=True), \
             mock.patch.object(domclick, "search", side_effect=down), \
             mock.patch.object(sources, "enabled", return_value=[cian, domclick]):
            items, report = asyncio.run(sources.collect({"city": "Казань", "rooms": 2, "budget_max": 9_000_000}))
        self.assertGreater(len(items), 10)
        self.assertEqual([r["status"] for r in report], ["demo", "unavailable"])


class StaticSourcesTest(unittest.TestCase):
    """Авито и Яндекс — только сохранённые объявления; Домклик — без ожидания попыток пробиться через защиту."""

    def test_static_split_by_market(self):
        sec, at = avito.load_snapshot("Казань", 2)
        new, _ = avito.load_snapshot("Казань", 2, new=True)
        self.assertTrue(sec and new and at)
        self.assertTrue(all(not x["is_new"] for x in sec) and all(x["is_new"] for x in new))
        items, rep = asyncio.run(yandex.search("Казань", 2, 9_000_000))
        self.assertEqual(rep["status"], "static")
        self.assertTrue(all(x["source_name"] == "Яндекс Недвижимость" and x["url"].startswith("https://realty.yandex.ru/offer/") for x in items))
        with self.assertRaises(sources.SourceUnavailable):
            asyncio.run(yandex.search("Казань", 2, 9_000_000, page=2))
        self.assertFalse(avito.supported("Новосибирск"), "по другим городам сохранённых объявлений нет — честно")

    def test_no_suburbs_in_kazan(self):
        for src in (avito, yandex):
            for new in (False, True):
                for x in src.load_snapshot("Казань", 2, new=new)[0]:
                    self.assertNotRegex(x["address"], r"^(село|С\.|Д\.|Пос\.)")

    def test_domclick_static_mode_skips_network(self):
        with mock.patch.object(domclick, "static_mode", return_value=True), \
             mock.patch.object(domclick, "demo_mode", return_value=False), \
             mock.patch.object(domclick, "_fetch_page", side_effect=AssertionError("живой запрос не нужен")):
            items, rep = asyncio.run(domclick.search("Казань", 2, 9_000_000))
        self.assertEqual(rep["status"], "static")
        self.assertGreater(len(items), 20)

    def test_new_build_same_complex_without_coords(self):
        lay = domclick.normalize(LAYOUT, "Казань", new=True)
        ya = {"id": "yandex-1", "source": "yandex", "source_name": "Яндекс Недвижимость", "address": "ЖК Батталовский",
              "complex": "Батталовский", "rooms": 2, "area": 33.2, "floor": 15, "price": 8600000, "is_new": True,
              "seller_role": "застройщик", "lat": None, "lng": None}
        self.assertEqual(len(dedup.merge([lay, ya])), 1)
        self.assertEqual(len(dedup.merge([lay, {**ya, "complex": "Экосити"}])), 2)

    def test_four_sources_demo(self):
        with mock.patch.object(cian, "demo_mode", return_value=True), \
             mock.patch.object(domclick, "demo_mode", return_value=True), \
             mock.patch.object(sources, "enabled", return_value=[cian, domclick, avito, yandex]):
            items, report = asyncio.run(sources.collect({"city": "Казань", "rooms": 2, "budget_max": 9_000_000, "housing": "both"}))
        self.assertEqual(len(report), 8)
        self.assertGreaterEqual(dedup.duplicates_count(items), 25)
        self.assertTrue(any(len(x["offers"]) >= 3 for x in items), "есть квартиры сразу на трёх площадках")


if __name__ == "__main__":
    unittest.main()
