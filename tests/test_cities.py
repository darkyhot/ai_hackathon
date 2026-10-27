"""Города Циана и ключ DeepSeek."""
import os
os.environ.setdefault("DATABASE_URL", "sqlite://")  # тесты не трогают рабочую базу
import asyncio
import unittest
from unittest import mock

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


class CianRedirectTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = Path(tempfile.mkdtemp())
        self.patches = [mock.patch.object(cian, "CACHE_DIR", self.tmp), mock.patch.object(cian, "SUBS_FILE", self.tmp / "subs.json"),
                        mock.patch.object(cian, "_SUBS", None), mock.patch.object(cian, "MIN_INTERVAL", 0)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_follows_redirect_to_city_subdomain_and_remembers(self):
        seen = []

        async def getter(url):
            seen.append(url)
            if url.startswith("https://www.cian.ru/"):
                return 301, "https://kaluga.cian.ru/cat.php?region=4780", ""
            return 200, "", "<title>Купить двухкомнатную квартиру в Калуге</title> \"offers\":[]"

        with mock.patch.object(cian, "_get_httpx", side_effect=getter), mock.patch.dict("sys.modules", {"curl_cffi": None}):
            items, via = asyncio.run(cian._fetch_page("Калуга", 2, 6_000_000, 1))
        self.assertEqual(items, [])
        self.assertTrue(seen[1].startswith("https://kaluga.cian.ru/cat.php?") and "maxprice=" in seen[1], seen)
        self.assertIn("kaluga.cian.ru", cian.build_url("Калуга", 2, 6_000_000))

    def test_captcha_and_foreign_redirects_not_followed(self):
        self.assertIsNone(cian.redirect_target("https://www.cian.ru/x", "https://www.cian.ru/captcha/?r=1"))
        self.assertIsNone(cian.redirect_target("https://www.cian.ru/x", "https://evil.example/cian.ru"))
        self.assertEqual(cian.redirect_target("https://www.cian.ru/x", "/cat.php?a=1"), "https://www.cian.ru/cat.php?a=1")
