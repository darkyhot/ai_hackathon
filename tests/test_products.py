"""Финансовые продукты под выбранную квартиру и сравнение обслуживания в Сбере с рынком.

Проверяем арифметику предложения (страхование, вклад, бонусы, сравнение ставок),
карточку выбора в диалоге и то, что план сделки показывает экономию против банка-конкурента.
"""
from __future__ import annotations

import asyncio
import unittest

from backend import db
from backend.agent import actions, products as PR, session as O, validation
from backend.agent.mortgage import annuity
from backend.agent.profile import merge_profile
from backend.llm import ToolCall
from tests.fakes import ScriptedLLM
from tests.test_llm_and_agent import listing


class ProductsMathTest(unittest.TestCase):
    """Числа предложения: Сбер выгоднее рынка, но не в убыток банку."""

    def test_insurance_is_cheaper_than_market_and_scales_with_price(self):
        small = PR.insurance(6_000_000, 4_800_000)
        big = PR.insurance(12_000_000, 9_600_000)
        self.assertGreater(small["saving"], 0)
        self.assertGreater(big["sber_year"], small["sber_year"])
        self.assertEqual(small["sber_year"], small["life"]["sber"] + small["property"]["sber"])
        self.assertEqual(small["saving"], small["market_year"] - small["sber_year"])

    def test_cash_purchase_has_no_life_insurance(self):
        ins = PR.insurance(9_000_000, 0)
        self.assertEqual(ins["life"]["sber"], 0)
        self.assertGreater(ins["property"]["sber"], 0)

    def test_paperwork_has_no_commission_in_sber(self):
        doc = PR.paperwork(9_000_000, 7_200_000)
        self.assertNotIn("Комиссия за оформление ипотеки", doc["sber"])
        self.assertGreater(doc["saving"], 0)
        self.assertEqual(doc["saving"], doc["market_total"] - doc["sber_total"])

    def test_market_commission_has_minimum(self):
        cheap = PR.paperwork(5_000_000, 500_000)
        self.assertEqual(cheap["market"]["Комиссия за оформление ипотеки"], PR.MARKET["deal_fee_min"])

    def test_compare_explains_rate_gap_with_discounts(self):
        cmp = PR.compare(9_000_000, 7_200_000, 0.155, 30)
        self.assertAlmostEqual(cmp["rate"]["market"] - cmp["rate"]["sber"], PR.MARKET["rate_spread"], places=4)
        self.assertAlmostEqual(sum(d["value"] for d in cmp["rate"]["discounts"]), PR.MARKET["rate_spread"], places=6)
        self.assertGreater(cmp["payment"]["saving_month"], 0)
        expected = round((annuity(7_200_000, 0.173, 30) - annuity(7_200_000, 0.155, 30)) * 12 * 30, -2)
        self.assertAlmostEqual(cmp["payment"]["saving_term"], expected, delta=200)
        self.assertAlmostEqual(cmp["interest"]["saving"], cmp["payment"]["saving_term"], delta=200)
        self.assertEqual(cmp["total_saving"],
                         cmp["paperwork"]["saving"] + cmp["insurance"]["saving"] + cmp["interest"]["saving"])

    def test_compare_payments_match_annuity(self):
        cmp = PR.compare(9_000_000, 7_200_000, 0.155, 30)
        self.assertAlmostEqual(cmp["payment"]["sber"], round(annuity(7_200_000, 0.155, 30)), delta=60)
        self.assertAlmostEqual(cmp["payment"]["market"], round(annuity(7_200_000, 0.173, 30)), delta=60)

    def test_cash_compare_skips_loan(self):
        cmp = PR.compare(9_000_000, 0, None, 0)
        self.assertTrue(cmp["cash"])
        self.assertIsNone(cmp["payment"])
        self.assertEqual(cmp["total_saving"], cmp["paperwork"]["saving"] + cmp["insurance"]["saving"])

    def test_savings_closes_gap_with_deposit(self):
        sav = PR.savings(down=1_800_000, gap=2_000_000, monthly=100_000)
        self.assertEqual(sav["mode"], "gap")
        self.assertGreater(sav["months"], 0)
        self.assertGreater(sav["income"], sav["market_income"])
        self.assertEqual(sav["extra"], sav["income"] - sav["market_income"])

    def test_savings_parks_money_until_deal(self):
        sav = PR.savings(down=1_800_000, gap=0, monthly=0)
        self.assertEqual(sav["mode"], "parking")
        self.assertEqual(sav["horizon_months"], PR.SBER["parking_months"])
        self.assertGreater(sav["income"], 0)

    def test_cross_sell_counts_spasibo_and_prime(self):
        cross = PR.cross_sell()
        names = " ".join(i["name"] for i in cross["items"])
        self.assertIn("Спасибо", names)
        self.assertIn("СберПрайм", names)
        self.assertEqual(cross["total"], sum(i["value"] for i in cross["items"]))
        self.assertGreater(len(cross["items"]), 3)

    def test_offer_totals_add_up(self):
        o = PR.offer(price=9_000_000, down=1_800_000, loan=7_200_000, rate=0.155, years=30)
        self.assertEqual(o["total_benefit"],
                         o["insurance"]["saving"] + o["savings"]["extra"] + o["cross"]["total"])
        self.assertEqual(o["assumptions"]["mode"], "demo")

    def test_offer_is_not_a_loss_leader(self):
        """Скидка к ставке меньше маржи банка: предложение выгодно клиенту, но не в убыток."""
        margin = 0.03  # economics.mortgage_margin_pct
        self.assertLess(PR.MARKET["rate_spread"], margin)
        self.assertGreater(PR.compare(9_000_000, 7_200_000, 0.155, 30)["rate"]["sber"], 0.1)
        cross = PR.cross_sell()
        self.assertLess(cross["total"], 0.01 * 9_000_000, "бонусы — маркетинг, а не возврат комиссии")


class ProductsCardTest(unittest.IsolatedAsyncioTestCase):
    """Карточка продуктов в диалоге и сравнение в плане сделки."""

    def setUp(self):
        db.configure("sqlite://")

    def agent(self, payment="cash"):
        s = O.create_session("", ScriptedLLM())
        s.profile = merge_profile({"city": "Казань", "rooms": 2, "budget_max": 9_000_000, "payment": payment})
        s.profile["_approved_budget_max"] = s.profile["budget_max"]
        s.red_lines_ok = True
        s.prefs.sync(s.profile)
        x = listing()
        s.search = {"listings": [x], "pool": [x], "shown_ids": {x["id"]}, "meta": {}, "comps": [x]}
        s.shown = [x]
        s.calls = {x["id"]: {"status": "agreed", "price": 7_000_000, "viewing": "завтра", "slots": ["завтра"], "learned": {}}}
        s.agreed = {x["id"]: 7_000_000}
        s.chosen = x["id"]
        if payment != "cash":
            s.mortgage = {"chosen": "market", "rate": 0.155}
        s.remember()
        return s, x

    async def wait_card(self, s, kind, task):
        for _ in range(200):
            if kind in s.pending:
                return
            if task.done():
                task.result()
                self.fail("Операция завершилась до карточки " + kind)
            await asyncio.sleep(0.001)
        self.fail("Карточка не появилась: " + kind)

    async def test_offer_products_card_and_choice(self):
        s, x = self.agent()
        task = asyncio.create_task(actions.execute(s, ToolCall("offer_products", {"listing_id": x["id"]}, "offer")))
        await self.wait_card(s, "products", task)
        payload = s.pending_decisions["products"]["payload"]
        self.assertIn("insurance", payload)
        self.assertIn("cross", payload)
        self.assertGreater(payload["total_benefit"], 0)
        self.assertTrue(s.resolve("products", True, {"choice": "all"}))
        result = await task
        self.assertEqual(result["client_choice"], "all")
        self.assertEqual(s.products_choice["choice"], "all")
        self.assertIsNotNone(s.products_offer)

    async def test_offer_products_requires_chosen_flat(self):
        s, x = self.agent()
        s.chosen = None
        result = await actions.execute(s, ToolCall("offer_products", {"listing_id": x["id"]}, "offer"))
        self.assertIn("plan_viewing", result["error"])

    async def test_prepare_deal_shows_sber_package(self):
        s, x = self.agent()
        task = asyncio.create_task(actions.execute(s, ToolCall("prepare_deal", {"listing_id": x["id"]}, "deal")))
        await self.wait_card(s, "sign", task)
        self.assertTrue(s.resolve("sign", True, {}))
        await task
        deal = [e for e in s.history if e.get("type") == "result" and e.get("kind") == "deal"][-1]["data"]
        self.assertTrue(deal["sber"]["cash"])
        self.assertGreater(deal["sber"]["total_saving"], 0)

    async def test_prepare_deal_compares_mortgage_rates(self):
        s, x = self.agent(payment="mortgage_ready")
        task = asyncio.create_task(actions.execute(s, ToolCall("prepare_deal", {"listing_id": x["id"]}, "deal")))
        await self.wait_card(s, "sign", task)
        self.assertTrue(s.resolve("sign", True, {}))
        await task
        deal = [e for e in s.history if e.get("type") == "result" and e.get("kind") == "deal"][-1]["data"]
        sber = deal["sber"]
        self.assertFalse(sber["cash"])
        self.assertGreater(sber["rate"]["market"], sber["rate"]["sber"])
        self.assertGreater(sber["total_saving"], sber["paperwork"]["saving"])


class ProductsConfirmationTest(unittest.TestCase):
    def test_choice_is_validated(self):
        validation.confirmation("products", {"choice": "all"})
        validation.confirmation("products", {"choice": "none", "text": "не нужно"})
        with self.assertRaises(ValueError):
            validation.confirmation("products", {"choice": "всё подряд"})

    def test_policy_discounts_explain_market_gap(self):
        self.assertAlmostEqual(sum(v for _, v in PR.SBER["discounts"]), PR.MARKET["rate_spread"], places=6)


if __name__ == "__main__":
    unittest.main()
