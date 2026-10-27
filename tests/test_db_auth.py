"""База, пользователи, обход объявлений, речевая аналитика и окно проверки безопасности."""
import os
os.environ.setdefault("DATABASE_URL", "sqlite://")  # тесты не трогают рабочую базу
import asyncio
import unittest
from datetime import timedelta
from unittest import mock

from fastapi.testclient import TestClient

from backend import auth, db, sources
from backend.agent import call_analytics as CA
from backend.db import repo
from backend.db.models import Listing, TrackedSearch, utcnow
from backend.sources import cian


def flat(i, price=8_000_000, **kw):
    return {"id": f"cian-{i}", "source": "cian", "source_name": "Циан", "url": f"https://kazan.cian.ru/sale/flat/{i}/",
            "city": "Казань", "district": "Вахитовский", "okrug": None, "address": f"ул. Тестовая, {i}", "rooms": 2,
            "area": 45.0, "kitchen": 8.0, "floor": 3, "floors": 9, "year": 2005, "condition": None, "price": price,
            "days_on_market": 10, "metro": None, "photo": f"https://img/{i}.jpg", "photos": [f"https://img/{i}.jpg"],
            "description": "Хорошая квартира", "rosreestr_checked": True, "is_apartments": False, "is_auction": False,
            "seller_role": "собственник", "is_new": False, **kw}


class Base(unittest.TestCase):
    def setUp(self):
        db.configure("sqlite://")  # чистая база в памяти на каждый тест


class PasswordTest(unittest.TestCase):
    def test_hash_roundtrip(self):
        h = auth.hash_password("секретный пароль")
        self.assertTrue(h.startswith("scrypt$"))
        self.assertNotIn("секрет", h)
        self.assertTrue(auth.verify_password("секретный пароль", h))
        self.assertFalse(auth.verify_password("другой", h))
        self.assertFalse(auth.verify_password("x", None))


class ListingsTest(Base):
    def test_upsert_price_history_and_features(self):
        with db.session() as s:
            st = repo.upsert_listings(s, [flat(1), flat(2)])
        self.assertEqual(st["added"], 2)
        with db.session() as s:
            st = repo.upsert_listings(s, [flat(1, price=7_800_000)])
            self.assertEqual(st["price_changed"], 1)
            x = repo.listing_dict(s.get(Listing, "cian-1"))
        self.assertEqual(x["price"], 7_800_000)
        self.assertEqual(x["price_drop"], 200_000)
        with db.session() as s:
            keys = {f.feature_key for f in s.get(Listing, "cian-1").features}
        self.assertTrue({"area", "price_per_m2", "is_first_floor", "owner_selling"} <= keys)

    def test_collect_reads_fresh_db_without_network(self):
        profile = {"city": "Казань", "rooms": 2, "budget_max": 9_000_000, "housing": "secondary"}
        live = mock.AsyncMock(return_value=([flat(1), flat(2), flat(3, price=9_900_000)], {"status": "live", "note": "стр. 1"}))
        with mock.patch.object(cian, "demo_mode", return_value=False), mock.patch.object(cian, "search", live):
            items, rep = asyncio.run(sources.collect(profile))
            self.assertEqual(rep[0]["status"], "live")
            items, rep = asyncio.run(sources.collect(profile))  # второй раз — из базы
        self.assertEqual(live.await_count, 1)
        self.assertEqual(rep[0]["status"], "db")
        self.assertEqual({x["id"] for x in items}, {"cian-1", "cian-2"})  # дороже бюджета (+5%) не берём

    def test_bigger_budget_goes_live(self):
        profile = {"city": "Казань", "rooms": 2, "budget_max": 9_000_000, "housing": "secondary"}
        live = mock.AsyncMock(return_value=([flat(1)], {"status": "live", "note": ""}))
        with mock.patch.object(cian, "demo_mode", return_value=False), mock.patch.object(cian, "search", live):
            asyncio.run(sources.collect(profile))
            asyncio.run(sources.collect({**profile, "budget_max": 12_000_000}))
        self.assertEqual(live.await_count, 2)

    def test_stale_db_goes_live(self):
        profile = {"city": "Казань", "rooms": 2, "budget_max": 9_000_000, "housing": "secondary"}
        live = mock.AsyncMock(return_value=([flat(1)], {"status": "live", "note": ""}))
        with mock.patch.object(cian, "demo_mode", return_value=False), mock.patch.object(cian, "search", live):
            asyncio.run(sources.collect(profile))
            with db.session() as s:
                for t in s.query(TrackedSearch).all():
                    t.last_crawled_at = utcnow() - timedelta(hours=30)
            asyncio.run(sources.collect(profile))
        self.assertEqual(live.await_count, 2)


class RefreshTest(Base):
    def test_removed_after_two_full_crawls_and_comes_back(self):
        from backend.workers import refresh
        with db.session() as s:
            t = repo.track_search(s, "Казань", 2, False, None, 9_000_000)
            repo.upsert_listings(s, [flat(1), flat(2)])
        pages = {"items": [flat(1)]}

        async def fake(city, rooms, budget, page, okrug, new):
            return pages["items"] if page == 1 else []

        unknown = mock.AsyncMock(return_value=(None, "сеть"))
        with mock.patch.object(cian, "demo_mode", return_value=False), mock.patch.object(cian, "fetch_live", side_effect=fake), \
                mock.patch.object(cian, "check_listing_page", unknown):
            r1 = asyncio.run(refresh.run_once())
            with db.session() as s:
                self.assertEqual(s.get(Listing, "cian-2").status, "active")  # один пропуск — ещё не снимаем
            r2 = asyncio.run(refresh.run_once())
            with db.session() as s:
                self.assertEqual(s.get(Listing, "cian-2").status, "removed")
            pages["items"] = [flat(1), flat(2), flat(4)]
            r3 = asyncio.run(refresh.run_once())
        self.assertEqual((r1["removed"], r2["removed"], r3["added"]), (0, 1, 1))
        with db.session() as s:
            self.assertEqual(s.get(Listing, "cian-2").status, "active")

    def test_incomplete_crawl_does_not_remove(self):
        from backend.workers import refresh
        with db.session() as s:
            repo.track_search(s, "Казань", 2, False, None, 9_000_000)
            repo.upsert_listings(s, [flat(1), flat(2)])

        async def captcha(*a):
            raise cian.SourceUnavailable("Циан недоступен (капча)")

        with mock.patch.object(cian, "demo_mode", return_value=False), mock.patch.object(cian, "fetch_live", side_effect=captcha):
            for _ in range(3):
                r = asyncio.run(refresh.run_once())
        self.assertEqual(r["status"], "failed")
        with db.session() as s:
            self.assertEqual(s.get(Listing, "cian-2").status, "active")

    def test_next_run(self):
        from datetime import datetime
        from backend.workers.refresh import _next_run
        self.assertEqual(_next_run("04:00", datetime(2026, 10, 26, 3, 0)), datetime(2026, 10, 26, 4, 0))
        self.assertEqual(_next_run("04:00", datetime(2026, 10, 26, 5, 0)), datetime(2026, 10, 27, 4, 0))


class PageCheckTest(Base):
    def test_verdicts(self):
        v = cian.page_verdict
        self.assertEqual(v(404, "", "", "1")[0], False)
        self.assertEqual(v(301, "https://kazan.cian.ru/kupit-kvartiru/", "", "123456")[0], False)
        self.assertIsNone(v(302, "https://www.cian.ru/captcha/?redirect=x", "", "123456")[0])
        self.assertEqual(v(200, "", '<h1>Объявление снято с публикации</h1> 123456', "123456")[0], False)
        self.assertEqual(v(200, "", '{"offerData":{"offer":{"cianId":123456,"status":"deactivated"}}}', "123456")[0], False)
        self.assertEqual(v(200, "", '{"cianId":123456,"status":"published","bargainTerms":{"priceRur":1}}', "123456")[0], True)
        self.assertIsNone(v(200, "", "<html>другая страница</html>", "123456")[0])
        self.assertIsNone(v(503, "", "", "123456")[0])

    def test_verify_removes_and_caches(self):
        with db.session() as s:
            repo.upsert_listings(s, [flat(1), flat(2)])
        check = mock.AsyncMock(side_effect=lambda url: (False, "снято") if "/2/" in url else (True, "опубликовано"))
        with mock.patch.object(cian, "demo_mode", return_value=False), mock.patch.object(cian, "check_listing_page", check):
            r = asyncio.run(sources.verify([flat(1), flat(2)]))
            self.assertEqual((r["cian-1"]["alive"], r["cian-2"]["alive"]), (True, False))
            asyncio.run(sources.verify([flat(1)]))  # свежая проверка — из базы, без запроса
        self.assertEqual(check.await_count, 2)
        with db.session() as s:
            self.assertEqual(s.get(Listing, "cian-2").status, "removed")
            self.assertTrue(s.get(Listing, "cian-1").page_alive)

    def test_verify_budget_gives_unknown(self):
        async def slow(url):
            await asyncio.sleep(5)
            return True, "ok"
        with mock.patch.object(cian, "demo_mode", return_value=False), mock.patch.object(cian, "check_listing_page", side_effect=slow):
            r = asyncio.run(sources.verify([flat(1)], budget_s=0.2))
        self.assertIsNone(r["cian-1"]["alive"])

    def test_selection_replaces_removed(self):
        from backend.agent import orchestrator as O
        items = [flat(i) for i in range(1, 6)]
        meta = {x["id"]: {} for x in items}
        check = mock.AsyncMock(side_effect=lambda url: (False, "снято") if "/2/" in url or "/3/" in url else (True, "ok"))
        with mock.patch.object(cian, "demo_mode", return_value=False), mock.patch.object(cian, "check_listing_page", check):
            shown, gone = asyncio.run(O.alive_only(items, 3, meta))
        self.assertEqual([x["id"] for x in shown], ["cian-1", "cian-4", "cian-5"])
        self.assertEqual([x["id"] for x in gone], ["cian-2", "cian-3"])
        self.assertTrue(meta["cian-1"]["page"]["alive"])

    def test_refresh_uses_page_check(self):
        from backend.workers import refresh
        with db.session() as s:
            repo.track_search(s, "Казань", 2, False, None, 9_000_000)
            repo.upsert_listings(s, [flat(1), flat(2), flat(3)])

        async def fake(city, rooms, budget, page, okrug, new):
            return [flat(1)] if page == 1 else []
        # cian-2 ушло ниже в выдаче (страница жива), cian-3 снято
        check = mock.AsyncMock(side_effect=lambda url: (True, "ok") if "/2/" in url else (False, "снято"))
        with mock.patch.object(cian, "demo_mode", return_value=False), mock.patch.object(cian, "fetch_live", side_effect=fake), \
                mock.patch.object(cian, "check_listing_page", check):
            for _ in range(3):
                asyncio.run(refresh.run_once())
        with db.session() as s:
            self.assertEqual(s.get(Listing, "cian-2").status, "active")
            self.assertEqual(s.get(Listing, "cian-3").status, "removed")


class CallAnalyticsTest(unittest.TestCase):
    Q = [{"key": "year", "label": "год постройки"}, {"key": "kitchen", "label": "площадь кухни"},
         {"key": "owners", "label": "собственники"}]

    def test_confidence_levels(self):
        history = [
            {"speaker": "agent", "text": "Какого года дом и какая кухня?"},
            {"speaker": "seller", "text": "Дом 1998 года. Кухня вроде 9 метров.", "ask": 8_000_000},
            {"speaker": "agent", "text": "А собственники?"},
            {"speaker": "seller", "text": "Один собственник, с 2015 года. Точно не помню, уточню.", "ask": 7_900_000},
        ]
        listing = flat(1)  # в объявлении 2005 год, кухня 8
        facts, summary = CA.analyze(history, {"год постройки": "1998", "площадь кухни": "9 м²",
                                              "собственники": "один, с 2015 года"}, self.Q, listing, "human",
                                    "agreed", 7_900_000)
        by = {f["key"]: f for f in facts}
        self.assertTrue(by["year"]["contradicts_listing"])
        self.assertEqual(by["year"]["level"], "medium")
        self.assertTrue(by["kitchen"]["hedged"])
        self.assertFalse(by["kitchen"]["contradicts_listing"])  # 9 vs 8 — в пределах метра
        self.assertTrue(by["owners"]["hedged"])
        self.assertEqual(by["owners"]["level"], "low")
        self.assertIn("ЕГРН", by["owners"]["verify_with"])
        self.assertEqual(by["price_agreement"]["level"], "medium")
        self.assertEqual(summary["contradictions"], 1)
        self.assertEqual(summary["bargain"], 100_000)
        self.assertTrue(all(f["confidence"] < 1 for f in facts))

    def test_changed_answer(self):
        history = [{"speaker": "seller", "text": "Дом 2005 года."},
                   {"speaker": "seller", "text": "Хотя нет, дом 2007 года постройки."}]
        f = CA.fact("year", "год постройки", "2007", [h["text"] for h in history], flat(1), "human")
        self.assertTrue(f["changed_during_call"])
        self.assertIn("называл разное", " ".join(f["reasons"]))

    def test_price_raised_lowers_confidence(self):
        h = [{"speaker": "seller", "text": "8,0", "ask": 8_000_000}, {"speaker": "seller", "text": "7,8", "ask": 7_800_000},
             {"speaker": "seller", "text": "Нет, 7,9", "ask": 7_900_000}]
        self.assertLess(CA.price_fact(h, flat(1), "agreed", 7_900_000)["confidence"], 0.6)


class ApiTest(Base):
    def setUp(self):
        super().setUp()
        from backend.main import app
        auth._fails.clear()
        self.c = TestClient(app)

    def test_anonymous_visitor_then_register_claims_history(self):
        r = self.c.get("/api/me")
        self.assertIsNone(r.json()["user"])
        vid = self.c.cookies.get("vid")
        self.assertTrue(vid)
        with mock.patch("backend.agent.orchestrator.run_session"):
            sid = self.c.post("/api/session", json={"query": "двушка в Казани до 9 млн"}).json()["id"]
        self.c.post("/api/consent")
        r = self.c.post("/api/auth/register", json={"email": "Nik@Example.ru", "password": "password123", "name": "Ник"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertGreaterEqual(r.json()["claimed"]["events"], 2)
        self.assertEqual(r.json()["claimed"]["agent_sessions"], 1)
        self.assertTrue(self.c.cookies.get("sid"))
        me = self.c.get("/api/me").json()
        self.assertEqual(me["user"]["email"], "nik@example.ru")
        self.assertTrue(me["consent"])
        hist = self.c.get("/api/me/history").json()
        self.assertEqual(hist["sessions"][0]["id"], sid)

    def test_session_belongs_to_its_browser(self):
        with mock.patch("backend.agent.orchestrator.run_session"):
            sid = self.c.post("/api/session", json={"query": "двушка в Казани до 9 млн"}).json()["id"]
        stranger = TestClient(self.c.app)  # другой браузер — другая cookie vid
        self.assertEqual(stranger.post(f"/api/session/{sid}/message", json={"text": "привет"}).status_code, 404)
        self.assertEqual(stranger.post(f"/api/session/{sid}/confirm", json={"kind": "red_lines"}).status_code, 404)
        self.assertNotEqual(self.c.post(f"/api/session/{sid}/confirm", json={"kind": "red_lines"}).status_code, 404)

    def test_only_cian_urls_opened(self):
        self.assertTrue(cian.is_cian_url("https://kazan.cian.ru/sale/flat/1/"))
        self.assertTrue(cian.is_cian_url("https://images.cdn-cian.ru/a.jpg", ("cdn-cian.ru",)))
        for bad in ["http://kazan.cian.ru/x", "https://cian.ru.evil.com/x", "https://169.254.169.254/", "file:///etc/passwd"]:
            self.assertFalse(cian.is_cian_url(bad), bad)
        self.assertEqual(asyncio.run(cian.check_listing_page("https://evil.com/sale/flat/123456/"))[0], None)

    def test_login_logout_and_errors(self):
        self.assertEqual(self.c.post("/api/auth/register", json={"email": "bad", "password": "password123"}).status_code, 422)
        self.assertEqual(self.c.post("/api/auth/register", json={"email": "a@b.ru", "password": "short"}).status_code, 422)
        self.assertEqual(self.c.post("/api/auth/register", json={"email": "a@b.ru", "password": "password123"}).status_code, 200)
        self.assertEqual(self.c.post("/api/auth/register", json={"email": "a@b.ru", "password": "password123"}).status_code, 409)
        self.c.post("/api/auth/logout")
        self.assertIsNone(self.c.get("/api/me").json()["user"])
        self.assertEqual(self.c.post("/api/auth/login", json={"email": "a@b.ru", "password": "wrong-pass"}).status_code, 401)
        self.assertEqual(self.c.post("/api/auth/login", json={"email": "A@b.ru", "password": "password123"}).status_code, 200)
        self.assertEqual(self.c.get("/api/me").json()["user"]["email"], "a@b.ru")

    def test_bruteforce_limited(self):
        self.c.post("/api/auth/register", json={"email": "x@y.ru", "password": "password123"})
        codes = [self.c.post("/api/auth/login", json={"email": "x@y.ru", "password": "nope-nope"}).status_code for _ in range(6)]
        self.assertEqual(codes[-1], 429)

    def test_admin_only(self):
        self.assertEqual(self.c.get("/api/admin/stats").status_code, 403)
        with mock.patch.object(auth, "_role_for", return_value="admin"):
            self.c.post("/api/auth/register", json={"email": "boss@x.ru", "password": "password123"})
        r = self.c.get("/api/admin/stats")
        self.assertEqual(r.status_code, 200)
        self.assertIn("listings_active", r.json())

    def test_security_window_with_call_facts(self):
        x = flat(7, description="Квартира в наследство, документы готовы")
        with db.session() as s:
            repo.upsert_listings(s, [x])
        facts, summary = CA.analyze([{"speaker": "seller", "text": "Один собственник, вроде с 2019 года."}],
                                    {"собственники": "один, с 2019"}, [{"key": "owners", "label": "собственники"}],
                                    x, "auto", "no_deal", x["price"])
        repo.record(repo.save_call, sid=None, visitor_id=None, user_id=None, listing=x, mode="auto", seller_role="собственник",
                    transcript=[], status="no_deal", asking=x["price"], agreed=None, viewing=None, analytics=summary, facts=facts)
        r = self.c.get("/api/listing/cian-7/security").json()
        desc = next(c for c in r["checks"] if c["key"] == "description")
        self.assertEqual(desc["status"], "warn")
        self.assertIn("наследство", desc["evidence"][0]["value"])
        egrn = next(c for c in r["before_deal"] if c["key"] == "egrn")
        self.assertTrue(egrn["self_check"]["url"].startswith("https://"))
        self.assertEqual(r["facts"][0]["key"], "owners")
        self.assertTrue(r["facts"][0]["hedged"])
        self.assertTrue(r["trust"])
        self.assertEqual(self.c.get("/api/listing/cian-404/security").status_code, 404)


if __name__ == "__main__":
    unittest.main()
