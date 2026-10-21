"""«Спросить агента» и демо-режим (без сети).

Запуск: .venv/bin/python -m unittest tests.test_qa_demo -v
"""
import asyncio
import unittest
from unittest import mock

from backend.agent import mortgage as M
from backend.agent import orchestrator as O
from backend.agent import qa
from backend.llm import LLMResponse, build_llm
from backend.sources import cian


class QuestionDetectionTest(unittest.TestCase):
    def test_is_question(self):
        for t in ["Что такое СБР?", "почему цена такая", "А если продавец откажется?", "сколько я переплачу"]:
            self.assertTrue(qa.is_question(t), t)
        for t in ["кухня побольше", "не первый этаж", "Казань", "9 млн"]:
            self.assertFalse(qa.is_question(t), t)

    def test_prompt_comes_from_team_doc(self):
        self.assertIn("ИИ-агент покупателя квартиры", qa.agent_prompt())
        self.assertIn("Красные линии", qa.agent_prompt())


class AnswerTest(unittest.TestCase):
    def session(self, llm):
        s = O.create_session("Двушка в Казани до 9 млн", llm)
        s.profile = {"city": "Казань", "rooms": 2, "budget_max": 9_000_000, "payment": "mortgage_ready", "summary": "Ищу двушку"}
        s.mortgage = M.programs(9_000_000, "Казань", {"children": 1, "youngest_age": 4, "matcap": True})
        return s

    def test_offline_answers_with_session_numbers(self):
        s = self.session(build_llm("mock"))
        reply = asyncio.run(qa.answer(s, "Сколько я переплачу по ипотеке?"))
        self.assertIn("17,5%", reply)
        self.assertIn("105 600", reply)
        self.assertIn("безопасных расчётов", qa.rules_answer("что такое СБР?", {}))
        self.assertIn("Казань", asyncio.run(qa.answer(s, "Нам подходит семейная ипотека?")))

    def test_llm_gets_team_prompt_and_context(self):
        llm = build_llm("mock")
        seen = {}

        async def chat(messages, **kw):
            seen["system"] = messages[0]["content"]
            return LLMResponse(text="Ответ модели")
        llm.chat = chat
        s = self.session(llm)
        self.assertEqual(asyncio.run(qa.answer(s, "Почему такая ставка?")), "Ответ модели")
        self.assertIn("Красные линии", seen["system"])
        self.assertIn("105 600", seen["system"], "числа — из контекста сессии")

    def test_question_does_not_resolve_pending_step(self):
        async def go():
            s = self.session(build_llm("mock"))
            fut = asyncio.get_running_loop().create_future()
            s.pending["selection"] = fut
            kind = await s.receive_text("что такое безопасные расчёты?")
            await asyncio.sleep(0.05)
            return kind, fut.done()
        kind, done = asyncio.run(go())
        self.assertEqual(kind, "question")
        self.assertFalse(done, "подборка ждёт отметок дальше")


class DemoModeTest(unittest.TestCase):
    def test_demo_uses_repo_snapshot_without_network(self):
        with mock.patch.object(cian, "demo_mode", return_value=True), \
             mock.patch.object(cian, "_fetch_page", side_effect=AssertionError("сеть в демо не нужна")):
            items, rep = asyncio.run(cian.search("Казань", 2, 9_000_000))
        self.assertEqual(rep["status"], "demo")
        self.assertGreaterEqual(len(items), 20)
        self.assertTrue(all(x["price"] <= 9_450_000 for x in items))
        self.assertTrue(all("phone" not in x for x in items), "телефонов продавцов в срезе нет")

    def test_demo_city_without_snapshot_is_honest(self):
        with mock.patch.object(cian, "demo_mode", return_value=True):
            with self.assertRaises(cian.SourceUnavailable):
                asyncio.run(cian.search("Новосибирск", 2, 7_000_000))


if __name__ == "__main__":
    unittest.main()
