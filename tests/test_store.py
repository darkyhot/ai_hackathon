"""База объявлений агента: запоминаем выдачу с признаками, «что рядом» считаем по карте один раз."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from sqlalchemy import select

from backend import db, geo
from backend.db import repo
from backend.db.models import ListingPrice
from backend.sources import store


def flat(id_="cian-1", price=9_000_000, **kw) -> dict:
    x = {"id": id_, "source": "cian", "source_name": "Циан", "city": "Казань", "rooms": 2, "price": price, "area": 54,
         "okrug": None, "lat": 55.79, "lng": 49.12, "description": "Квартира с балконом", "balconies": 1, "loggias": 0}
    return {**x, **kw}


class StoreTest(unittest.TestCase):
    def setUp(self):
        db.configure("sqlite://")
        geo._live_cache.clear()

    def test_remember_keeps_price_history_and_features(self):
        store.remember([flat()])
        store.remember([flat(price=8_700_000)])  # продавец снизил цену
        with db.session() as s:
            prices = [p.price for p in s.scalars(select(ListingPrice).order_by(ListingPrice.id))]
            feats = repo.load_features(s, ["cian-1"])["cian-1"]
        self.assertEqual(prices, [9_000_000, 8_700_000])
        self.assertIs(feats["balcony"], True)
        self.assertIs(feats["balcony_loggia"], False)  # лоджий 0 по данным площадки
        found = store.stored({"city": "Казань", "rooms": 2, "budget_max": 9_000_000})
        self.assertEqual([x["id"] for x in found], ["cian-1"])
        self.assertTrue(found[0]["stored_at"].endswith("Z"))
        self.assertEqual(store.stored({"city": "Казань", "rooms": 2, "budget_max": 8_000_000}), [])
        self.assertEqual(store.stored({"city": "Казань", "rooms": 2, "budget_max": 9_000_000}, exclude={"cian-1"}), [])

    def test_map_is_asked_once_per_flat(self):
        store.remember([flat()])
        calls = []

        async def fake_overpass(query, timeout):
            calls.append(query)
            return [{"type": "node", "id": 7, "lat": 55.7905, "lon": 49.1205, "tags": {"amenity": "school", "name": "Школа № 1"}}]

        with patch.object(geo, "_overpass", fake_overpass):
            first = flat()
            self.assertTrue(asyncio.run(geo.nearby([first], ["school"])))
            geo._live_cache.clear()  # как после перезапуска сервера: в памяти пусто, в базе есть
            again = flat()
            self.assertTrue(asyncio.run(geo.nearby([again], ["school"])))
        self.assertEqual(len(calls), 1)
        self.assertEqual(again["nearby"]["school"]["name"], "Школа № 1")
        self.assertEqual(store.stats({"city": "Казань", "rooms": 2}), {"total": 1, "mapped": 1, "crawl": None})


class OldDatabaseTest(unittest.TestCase):
    """База от версии до агента с tool calling: откладывается рядом, новая создаётся — история не теряется молча."""

    def test_outdated_sqlite_is_backed_up(self):
        import sqlite3
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        path = d / "app.db"
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE chat_events (id INTEGER PRIMARY KEY, agent_session_id VARCHAR NOT NULL, seq INTEGER)")
        con.execute("INSERT INTO chat_events (agent_session_id, seq) VALUES ('old', 1)")
        con.commit()
        con.close()
        db.configure(f"sqlite:///{path}")
        try:
            backups = list(d.glob("app.db.old-*"))
            self.assertEqual(len([b for b in backups if not b.name.endswith(("-wal", "-shm"))]), 1)
            self.assertEqual(sqlite3.connect(backups[0]).execute("SELECT agent_session_id FROM chat_events").fetchone()[0], "old")
            self.assertEqual(db._outdated_tables(db._engine), [])
            db.configure(f"sqlite:///{path}")  # вторая загрузка новой базы — ничего не откладывает
            self.assertEqual(len([b for b in d.glob("app.db.old-*") if not b.name.endswith(("-wal", "-shm"))]), 1)
        finally:
            db.configure("sqlite://")


if __name__ == "__main__":
    unittest.main()
