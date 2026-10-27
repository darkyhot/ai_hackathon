"""Города Циана и ключ DeepSeek."""
import os
os.environ.setdefault("DATABASE_URL", "sqlite://")  # тесты не трогают рабочую базу
import unittest

from backend.agent import tools as T
from backend.llm.deepseek import DeepSeekClient
from backend.sources import SUPPORTED_CITIES, cian
from backend.sources.base import normalize_city


class CitiesTest(unittest.TestCase):
    def test_declensions(self):
        cases = {"ищу квартиру в калуге": "Калуга", "в Ростове-на-Дону": "Ростов-на-Дону", "в туле": "Тула",
                 "в перми": "Пермь", "в сочи": "Сочи", "в нижнем новгороде": "Нижний Новгород", "спб": "Санкт-Петербург",
                 "в химках": "Химки", "Новокузнецк": "Новокузнецк", "в Самаре": "Самара"}
        for text, city in cases.items():
            self.assertEqual(normalize_city(text), city, text)

    def test_no_false_cities(self):
        for text in ["читаю объявления", "сочетание цены", "ищу 2-комнатную до 9 млн", "курсы валют"]:
            self.assertIsNone(normalize_city(text), text)

    def test_more_than_hundred_cities(self):
        self.assertGreater(len(SUPPORTED_CITIES), 100)
        self.assertIn("region=4780", cian.build_url("Калуга", 2, 6_000_000))
        self.assertIn("kazan.cian.ru", cian.build_url("Казань", 2, 6_000_000))

    def test_rules_parse_kaluga(self):
        p = T.parse_request_rules("ищу двушку в калуге до 6 млн")
        self.assertEqual((p["city"], p["rooms"], p["budget_max"]), ("Калуга", 2, 6_000_000))


class DeepSeekKeyTest(unittest.TestCase):
    def test_sk_prefix_added(self):
        c = DeepSeekClient("9ce5de7fd2564676b69b6a736ede4800", "https://api.deepseek.com", "deepseek-chat")
        self.assertEqual(c._http.headers["Authorization"], "Bearer sk-9ce5de7fd2564676b69b6a736ede4800")
        c = DeepSeekClient("sk-abc", "https://api.deepseek.com", "deepseek-chat")
        self.assertEqual(c._http.headers["Authorization"], "Bearer sk-abc")


if __name__ == "__main__":
    unittest.main()
