"""Новостройки (разбор Циана, платёж по объекту, звонок застройщику, план сделки) и проверка безопасности.

Запуск: .venv/bin/python -m unittest tests.test_newbuild_security -v
"""
import asyncio
import unittest
from unittest import mock

from backend import sources
from backend.agent import mortgage as M
from backend.agent import seller_sim
from backend.agent import security as S
from backend.agent import tools as T
from backend.sources import cian

ANNA = {"city": "Казань", "children": 1, "youngest_age": 4, "matcap": M.MATCAP_FIRST, "payment": "mortgage_saving"}


def flat(**kw):
    base = {"id": "cian-1", "source": "cian", "source_name": "Циан", "city": "Казань", "district": "Советский",
            "address": "Галии Кайбицкой, 2", "rooms": 2, "area": 55.0, "kitchen": 10.0, "floor": 5, "floors": 16,
            "year": 2015, "condition": None, "price": 7_200_000, "days_on_market": 40, "metro": None,
            "description": "", "rosreestr_checked": True, "is_apartments": False, "is_auction": False,
            "seller_role": "собственник", "is_new": False}
    return {**base, **kw}


def new_flat(**kw):
    return flat(**{"id": "cian-n1", "is_new": True, "year": None, "complex": "Солнечный", "deadline": "4 кв. 2027",
                   "finished": False, "developer": "ГК Тест", "sale_type": "ДДУ (214-ФЗ)", "seller_role": "застройщик",
                   "rosreestr_checked": None, **kw})


class CianNewBuildTest(unittest.TestCase):
    def test_url_switches_market(self):
        self.assertIn("object_type%5B0%5D=1", cian.build_url("Казань", 2, 9_000_000))
        self.assertIn("object_type%5B0%5D=2", cian.build_url("Казань", 2, 9_000_000, new=True))

    def test_normalize_keeps_ddu_only_for_new_builds(self):
        o = {"cianId": 5, "totalArea": "55", "roomsCount": 2, "bargainTerms": {"priceRur": 7_000_000, "saleType": "fz214"},
             "newbuilding": {"name": "Солнечный", "house": {"isFinished": False}},
             "building": {"deadline": {"quarter": "fourth", "year": 2027, "isComplete": False}, "floorsCount": 16},
             "user": {"isBuilder": True, "companyName": "ГК Тест"}}
        self.assertIsNone(cian.normalize(o, "Казань"), "во вторичке ДДУ не нужен")
        x = cian.normalize(o, "Казань", new=True)
        self.assertTrue(x["is_new"])
        self.assertEqual((x["complex"], x["deadline"], x["developer"]), ("Солнечный", "4 кв. 2027", "ГК Тест"))
        self.assertEqual(x["seller_role"], "застройщик")
        self.assertEqual(x["sale_type"], "ДДУ (214-ФЗ)")

    def test_markets_by_housing(self):
        self.assertEqual(sources.markets({"housing": None}), [False])
        self.assertEqual(sources.markets({"housing": "new"}), [True])
        self.assertEqual(sources.markets({"housing": "both"}), [False, True])

    def test_collect_both_markets_labels_sources(self):
        async def fake(city, rooms, budget, page=1, okrug=None, new=False):
            return [new_flat() if new else flat()], {"status": "demo", "note": "тест"}
        with mock.patch.object(cian, "search", side_effect=fake):
            items, report = asyncio.run(sources.collect({"city": "Казань", "rooms": 2, "budget_max": 9_000_000, "housing": "both"}))
        self.assertEqual({x["is_new"] for x in items}, {False, True})
        self.assertEqual([r["source"] for r in report], ["Циан · вторичка", "Циан · новостройки"])


class PaymentPerListingTest(unittest.TestCase):
    def test_new_build_gets_family_rate_secondary_does_not(self):
        p_new = M.listing_payment(new_flat(price=7_000_000), ANNA)
        p_sec = M.listing_payment(flat(price=7_000_000), ANNA)
        self.assertEqual((p_new["program"], p_new["rate"]), ("семейная", 0.10))
        self.assertEqual(p_sec["program"], "рыночная")
        self.assertLess(p_new["monthly"] * 1.6, p_sec["monthly"], "платёж по семейной в 1,6+ раза ниже")

    def test_limit_hint(self):
        p = M.listing_payment(new_flat(price=9_000_000), ANNA)
        self.assertEqual(p["program"], "рыночная")
        self.assertIn("при взносе от 3 000 000", p["hint"])
        self.assertEqual(M.max_family_price("Казань", ANNA), 7_500_000)
        self.assertIsNone(M.max_family_price("Казань", {"children": 0}))

    def test_fair_price_compares_same_market(self):
        pool = [flat(id=f"s{i}", price=6_000_000 + i * 10_000) for i in range(5)] + \
               [new_flat(id=f"n{i}", price=8_000_000 + i * 10_000) for i in range(5)]
        fp_new = T.fair_price(new_flat(id="x", price=8_100_000), pool)
        fp_sec = T.fair_price(flat(id="y", price=6_100_000), pool)
        self.assertGreater(fp_new["fair"], fp_sec["fair"] * 1.2)


class NewBuildCallAndDealTest(unittest.TestCase):
    def test_developer_is_firmer(self):
        s = seller_sim.build(new_flat(price=8_000_000))
        self.assertIn("отдел продаж", s["role"])
        self.assertGreaterEqual(s["min_price"], 8_000_000 * 0.975)
        self.assertIn("contract", s["facts"])
        self.assertEqual([q["key"] for q in T.missing_info(new_flat(condition=None))], ["condition", "contract"])

    def test_deal_plan_ddu_escrow(self):
        plan = T.deal_plan(new_flat(), 7_000_000, 1_400_000, rate=0.10, program="Семейная ипотека")
        titles = " ".join(t["title"] for t in plan["timeline"])
        self.assertTrue(plan["new"])
        self.assertIn("ДДУ", titles)
        self.assertIn("Эскроу", titles)
        self.assertNotIn("Безопасные расчёты", titles)


class SecurityTest(unittest.TestCase):
    def test_nothing_is_called_clean_without_registries(self):
        d = S.dossier(flat())
        self.assertEqual(d["done"], 0)
        self.assertTrue(all(i["status"] in ("not_checked", "partial") for i in d["before_deal"]))
        keys = [i["key"] for i in d["before_deal"]]
        for k in ("egrn", "seller_debts", "bankruptcy", "children"):
            self.assertIn(k, keys)
        self.assertNotIn("poa", keys, "доверенность — только если есть повод")
        self.assertEqual(next(i for i in d["before_deal"] if i["key"] == "egrn")["status"], "partial")

    def test_description_raises_priority(self):
        d = S.dossier(flat(description="Квартира в наследстве, есть перепланировка, продаёт представитель по доверенности"))
        top = [i["key"] for i in d["before_deal"] if i["priority"]]
        self.assertEqual(set(top), {"history", "replanning", "poa"})
        self.assertEqual(d["before_deal"][0]["priority"], True, "важные — наверху")
        self.assertEqual(d["status"], "warn")

    def test_new_build_dossier(self):
        d = S.dossier(new_flat())
        keys = [i["key"] for i in d["before_deal"]]
        self.assertIn("escrow", keys)
        self.assertIn("developer", keys)
        self.assertNotIn("commissioning", keys, "дом не сдан")
        self.assertNotIn("egrn", keys)
        self.assertNotIn("escrow", [i["key"] for i in S.dossier(new_flat(finished=True))["before_deal"]])


if __name__ == "__main__":
    unittest.main()
