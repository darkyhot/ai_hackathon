"""Ипотечные программы: семейная ипотека с 01.10.2026, рыночная ставка, маткапитал, новостройки,
и шаг «Финансы» конвейера целиком (без сети, модель — офлайн-заглушка)."""
from __future__ import annotations

import asyncio
import unittest

from backend import db
from backend.agent import finance as F
from backend.agent import mortgage as M
from backend.agent import orchestrator as O
from backend.agent import tools as T
from backend.llm import build_llm


def by_key(r):
    return {c["key"]: c for c in r["programs"]}


class ProgramsTest(unittest.TestCase):
    def test_kazan_secondary_family_goes_to_new_builds(self):
        """Казань, ребёнок 4 года, вторичка: семейная не проходит, считаем по рыночной, предлагаем новостройку."""
        r = M.programs(9_000_000, "Казань", {"children": 1, "youngest_age": 4, "matcap": True})
        p = by_key(r)
        self.assertEqual(r["chosen"], "market")
        self.assertEqual(p["family"]["status"], "no")
        self.assertIn("строится", p["family"]["main_reason"])
        self.assertEqual(p["family_new"]["rate"], 0.10)              # 1 ребёнок, регион
        self.assertEqual(p["family_new"]["down_payment"], 3_000_000)  # лимит 6 млн -> взнос 3 млн
        self.assertLess(p["family_new"]["monthly"], p["market"]["monthly"])
        self.assertEqual(r["matcap"]["own_down"], 1_800_000 - M.MATCAP_FIRST)
        self.assertFalse(p["family"]["show_payment"])
        self.assertIn("не подходит", r["explanation"])

    def test_scale_by_children_and_region(self):
        self.assertEqual(M.family_terms("Казань", 1), (0.10, 6_000_000))
        self.assertEqual(M.family_terms("Казань", 3), (0.06, 10_000_000))
        self.assertEqual(M.family_terms("Москва", 1), (0.12, 12_000_000))
        self.assertEqual(M.family_terms("Москва", 7), (0.04, 18_000_000))

    def test_family_ok_in_small_town(self):
        r = M.programs(7_000_000, "Альметьевск", {"children": 2, "youngest_age": 2}, secondary_ok=True)
        p = by_key(r)
        self.assertEqual((r["chosen"], r["rate"]), ("family", 0.08))
        self.assertGreater(p["family"]["monthly_after_subsidy"], p["family"]["monthly"])

    def test_child_too_old_or_unknown(self):
        self.assertEqual(by_key(M.programs(8_000_000, "Казань", {"children": 2, "youngest_age": 9}))["family"]["status"], "no")
        self.assertEqual(by_key(M.programs(8_000_000, "Казань", {"children": 0}))["family"]["status"], "no")
        self.assertEqual(by_key(M.programs(8_000_000, "Казань", {"children": 1}))["family"]["status"], "check")

    def test_limit_blocks_even_where_secondary_allowed(self):
        r = M.programs(9_000_000, "Альметьевск", {"children": 1, "youngest_age": 1}, secondary_ok=True)
        self.assertEqual((by_key(r)["family"]["status"], by_key(r)["family"]["needs_down"], r["chosen"]), ("no", 3_000_000, "market"))

    def test_new_build_passes_family(self):
        r = M.programs(7_000_000, "Казань", {"children": 1, "youngest_age": 4}, housing="new", is_secondary=False)
        self.assertEqual(r["chosen"], "family")
        self.assertIn("Считаю для новостройки", r["explanation"])
        pay = M.listing_payment({"price": 7_000_000, "city": "Казань", "is_new": True}, {"children": 1, "youngest_age": 4})
        self.assertEqual((pay["program"], pay["rate"]), ("семейная", 0.10))

    def test_market_rate_depends_on_down_payment(self):
        self.assertAlmostEqual(M.market_rate(0.2), 0.175)
        self.assertAlmostEqual(M.market_rate(0.5), 0.155)
        self.assertAlmostEqual(T.DEMO_RATE, 0.175)


class CalculationsTest(unittest.TestCase):
    def test_matcap_reduces_savings_need(self):
        a, b = F.plan(9_000_000, {}), F.plan(9_000_000, {}, matcap=M.MATCAP_FIRST)
        self.assertEqual(a["need"] - b["need"], M.MATCAP_FIRST)
        self.assertEqual(a["down_payment"], b["down_payment"], "кредит считается от полного взноса")

    def test_scenarios_and_deal_use_program_rate(self):
        m = T.mortgage_scenarios(7_000_000, 200_000, rate=0.08, program="Семейная ипотека")
        self.assertTrue(all(s["rate"] == 0.08 for s in m["scenarios"]))
        self.assertLess(m["scenarios"][0]["monthly"], T.mortgage_scenarios(7_000_000, 200_000)["scenarios"][0]["monthly"])
        lst = {"price": 7_200_000}
        d_fam = T.deal_plan(lst, 7_000_000, 1_400_000, rate=0.08, program="Семейная ипотека")
        self.assertLess(d_fam["monthly"], T.deal_plan(lst, 7_000_000, 1_400_000)["monthly"])
        self.assertIn("Семейная ипотека", d_fam["timeline"][1]["note"])

    def test_deal_plan_kinds(self):
        new = T.deal_plan({"price": 7_000_000, "is_new": True}, 6_900_000, 1_400_000)
        self.assertTrue(new["new"] and not new["assignment"])
        self.assertTrue(any("Эскроу" in s["title"] for s in new["timeline"]))
        assign = T.deal_plan({"price": 7_000_000, "is_new": True, "sale_type": "договор уступки"}, 6_900_000, 1_400_000)
        self.assertTrue(assign["assignment"])
        cash = T.deal_plan({"price": 7_000_000}, 6_900_000, None)
        self.assertEqual((cash["cash"], cash["monthly"], cash["down_payment"]), (True, 0, 6_900_000))

    def test_zero_children_and_studio_survive_merge(self):
        self.assertEqual(T.merge_profile({"city": "Казань", "children": 0})["children"], 0)
        self.assertEqual(T.merge_profile({"rooms": 0})["rooms"], 0)
        self.assertIs(T.merge_profile({"matcap": False})["matcap"], False)  # «маткапитала нет» — не переспрашиваем


class PipelineFinanceStepTest(unittest.TestCase):
    """Конвейер до конца шага «Финансы»: офлайн-модель, ответы клиента — через подтверждения."""

    def setUp(self):
        db.configure("sqlite://")

    def run_until_finance(self, query, answers):
        async def go():
            s = O.create_session(query, build_llm("mock"))
            q: asyncio.Queue = asyncio.Queue()
            s.subscribers.add(q)
            task = asyncio.create_task(O.pipeline(s))
            asked, said = [], []
            try:
                while True:
                    ev = await asyncio.wait_for(q.get(), 5)
                    if ev["type"] == "message" and ev["role"] == "agent":
                        said.append(ev["text"])
                    if ev["type"] == "confirm":
                        asked.append(ev["kind"])
                        approved, data = answers.get(ev["kind"], (True, {}))
                        s.resolve(ev["kind"], approved, data)
                    if ev["type"] == "result" and ev.get("step") == 2:
                        return ev["data"], asked, said, s
            finally:
                task.cancel()
        return asyncio.run(go())

    def test_family_with_matcap_in_savings_plan(self):
        data, asked, said, s = self.run_until_finance(
            "Двушка в Казани до 9 млн, ипотека, на взнос ещё копим",
            {"family": (True, {"children": 1, "youngest_age": 4, "matcap": M.MATCAP_FIRST})})
        self.assertIn("family", asked)
        self.assertEqual(data["mode"], "saving")
        self.assertEqual(data["programs"]["chosen"], "market")  # Казань, вторичка
        self.assertEqual(data["finance"]["matcap"], M.MATCAP_FIRST)
        self.assertEqual(data["mortgage"]["matcap"], M.MATCAP_FIRST)
        self.assertTrue(any("Семейная ипотека здесь не подходит" in t for t in said))
        self.assertIsNotNone(s.mortgage)

    def test_cash_skips_programs(self):
        data, asked, _, _ = self.run_until_finance("Двушка в Казани до 9 млн, покупаю за наличные", {})
        self.assertNotIn("family", asked)
        self.assertEqual(data["mode"], "cash")


class QuestionsTest(unittest.TestCase):
    def test_questions_vs_new_conditions(self):
        from backend.agent import qa
        self.assertTrue(qa.is_question("Сколько я переплачу?"))
        self.assertTrue(qa.is_question("что такое СБР"))
        self.assertFalse(qa.is_question("а если трёшку?"))  # новое условие поиска, а не вопрос
        self.assertFalse(qa.is_question("можно с балконом?"))


if __name__ == "__main__":
    unittest.main()
