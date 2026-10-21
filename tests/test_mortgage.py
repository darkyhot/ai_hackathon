"""Ипотечные программы: семейная ипотека с 01.10.2026, рыночная ставка, маткапитал, разбор семьи из текста,
и шаг «Финансы» конвейера целиком (без сети).

Запуск: .venv/bin/python -m unittest tests.test_mortgage -v
"""
import asyncio
import unittest

from backend.agent import finance as F
from backend.agent import mortgage as M
from backend.agent import orchestrator as O
from backend.agent import tools as T
from backend.llm import build_llm


def by_key(r):
    return {c["key"]: c for c in r["programs"]}


class ProgramsTest(unittest.TestCase):
    def test_anna_kazan_secondary(self):
        """Персона Анна: Казань, ребёнок 4 года, вторичка — семейная не проходит, считаем по рыночной."""
        r = M.programs(9_000_000, "Казань", {"children": 1, "youngest_age": 4, "matcap": True})
        p = by_key(r)
        self.assertEqual(r["chosen"], "market")
        self.assertEqual(p["family"]["status"], "no")
        self.assertIn("строится", p["family"]["main_reason"])
        self.assertEqual(p["family_new"]["rate"], 0.10)              # 1 ребёнок, регион
        self.assertEqual(p["family_new"]["down_payment"], 3_000_000)  # лимит 6 млн -> взнос 3 млн
        self.assertLess(p["family_new"]["monthly"], p["market"]["monthly"])
        self.assertEqual(r["matcap"]["amount"], M.MATCAP_FIRST)
        self.assertEqual(r["matcap"]["own_down"], 1_800_000 - M.MATCAP_FIRST)
        self.assertIn("не подходит", r["explanation"])
        self.assertFalse(p["family"]["show_payment"], "кредит выше лимита на вторичку — платёж не показываем")
        self.assertTrue(p["family_new"]["show_payment"])

    def test_scale_by_children_and_region(self):
        self.assertEqual(M.family_terms("Казань", 1), (0.10, 6_000_000))
        self.assertEqual(M.family_terms("Казань", 3), (0.06, 10_000_000))
        self.assertEqual(M.family_terms("Москва", 1), (0.12, 12_000_000))
        self.assertEqual(M.family_terms("Москва", 7), (0.04, 18_000_000))

    def test_family_ok_in_small_town(self):
        r = M.programs(7_000_000, "Альметьевск", {"children": 2, "youngest_age": 2}, secondary_ok=True)
        p = by_key(r)
        self.assertEqual(r["chosen"], "family")
        self.assertEqual(r["rate"], 0.08)
        self.assertLess(p["family"]["monthly"], p["market"]["monthly"])
        self.assertGreater(p["family"]["monthly_after_subsidy"], p["family"]["monthly"])
        self.assertIn("подходит семейная", r["explanation"])

    def test_child_too_old_and_no_children(self):
        self.assertEqual(by_key(M.programs(8_000_000, "Казань", {"children": 2, "youngest_age": 9}))["family"]["status"], "no")
        self.assertEqual(by_key(M.programs(8_000_000, "Казань", {"children": 0}))["family"]["status"], "no")
        self.assertEqual(by_key(M.programs(8_000_000, "Казань", {"children": 1}))["family"]["status"], "check")
        self.assertEqual(by_key(M.programs(8_000_000, "Казань", {}))["family"]["status"], "check")

    def test_limit_blocks_even_where_secondary_allowed(self):
        r = M.programs(9_000_000, "Альметьевск", {"children": 1, "youngest_age": 1}, secondary_ok=True)
        f = by_key(r)["family"]
        self.assertEqual(f["status"], "no")
        self.assertEqual(f["needs_down"], 3_000_000)
        self.assertEqual(r["chosen"], "market")

    def test_market_rate_depends_on_down_payment(self):
        self.assertAlmostEqual(M.market_rate(0.2), 0.175)
        self.assertAlmostEqual(M.market_rate(0.3), 0.165)
        self.assertAlmostEqual(M.market_rate(0.5), 0.155)
        self.assertAlmostEqual(T.DEMO_RATE, 0.175)


class FamilyParsingTest(unittest.TestCase):
    def test_rules(self):
        self.assertEqual(T.family_from_text("у нас сын 4 года и есть маткапитал"),
                         {"children": 1, "youngest_age": 4, "matcap": True})
        self.assertEqual(T.family_from_text("двое детей, младшей 2 года"), {"children": 2, "youngest_age": 2})
        self.assertEqual(T.family_from_text("детей нет"), {"children": 0})
        self.assertEqual(T.family_from_text("малышу 6 месяцев")["youngest_age"], 0)
        self.assertNotIn("matcap", T.family_from_text("маткапитал уже потратили"))

    def test_zero_children_survives_merge(self):
        p = T.merge_profile({"city": "Казань", "children": 0})
        self.assertEqual(p["children"], 0)
        self.assertEqual(T.merge_profile({"rooms": 0})["rooms"], 0, "студия не теряется")


class CalculationsTest(unittest.TestCase):
    def test_matcap_reduces_savings_need(self):
        a = F.plan(9_000_000, {}, matcap=0)
        b = F.plan(9_000_000, {}, matcap=M.MATCAP_FIRST)
        self.assertEqual(a["need"] - b["need"], M.MATCAP_FIRST)
        self.assertEqual(a["down_payment"], b["down_payment"], "кредит считается от полного взноса")

    def test_scenarios_and_deal_use_program_rate(self):
        m = T.mortgage_scenarios(7_000_000, 200_000, rate=0.08, program="Семейная ипотека")
        self.assertTrue(all(s["rate"] == 0.08 for s in m["scenarios"]))
        self.assertEqual(m["program"], "Семейная ипотека")
        market = T.mortgage_scenarios(7_000_000, 200_000)
        self.assertLess(m["scenarios"][0]["monthly"], market["scenarios"][0]["monthly"])
        lst = {"price": 7_200_000}
        d_fam = T.deal_plan(lst, 7_000_000, 1_400_000, rate=0.08, program="Семейная ипотека")
        d_mkt = T.deal_plan(lst, 7_000_000, 1_400_000)
        self.assertLess(d_fam["monthly"], d_mkt["monthly"])
        self.assertIn("Семейная ипотека", d_fam["timeline"][1]["note"])


class PipelineFinanceStepTest(unittest.TestCase):
    """Сценарий Анны через конвейер до конца шага «Финансы» (LLM — офлайн, без поиска)."""

    def run_until_finance(self, query, answers):
        async def go():
            s = O.create_session(query, build_llm("mock"))
            task = asyncio.create_task(O.pipeline(s))
            asked, said = [], []
            try:
                while True:
                    ev = await asyncio.wait_for(s.events.get(), 5)
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

    def test_anna_family_known_from_query(self):
        data, asked, said, s = self.run_until_finance(
            "Двушка в Казани до 9 млн, ипотека, на взнос ещё копим, сын 4 года, есть маткапитал", {})
        self.assertNotIn("family", asked, "всё сказано в запросе — не переспрашиваем")
        self.assertEqual(data["mode"], "saving")
        self.assertEqual(data["programs"]["chosen"], "market")
        self.assertEqual(data["finance"]["matcap"], M.MATCAP_FIRST)
        self.assertEqual(data["mortgage"]["matcap"], M.MATCAP_FIRST)
        self.assertTrue(any("Семейная ипотека здесь не подходит" in t for t in said))
        self.assertIsNotNone(s.mortgage)

    def test_family_question_when_unknown(self):
        data, asked, said, _ = self.run_until_finance(
            "Двушка в Казани до 9 млн, ипотека, на взнос хватает",
            {"family": (True, {"children": 2, "youngest_age": 3, "matcap": 0})})
        self.assertIn("family", asked)
        self.assertEqual(data["mode"], "mortgage")
        self.assertEqual(data["programs"]["family"]["children"], 2)
        self.assertEqual(data["programs"]["chosen"], "market")  # Казань, вторичка
        self.assertEqual(by_key(data["programs"])["family_new"]["rate"], 0.08)

    def test_cash_skips_programs(self):
        data, asked, _, _ = self.run_until_finance("Двушка в Казани до 9 млн, покупаю за наличные", {})
        self.assertNotIn("family", asked)
        self.assertEqual(data["mode"], "cash")


if __name__ == "__main__":
    unittest.main()
