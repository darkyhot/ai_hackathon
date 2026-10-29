"""История диалогов: лента событий в базе, повторное открытие, продолжение, доступ; скрытый шаг проверки."""
import os
os.environ.setdefault("DATABASE_URL", "sqlite://")  # тесты не трогают рабочую базу
import json
import unittest

from fastapi.testclient import TestClient

from backend import db
from backend.agent import orchestrator as O
from backend.db import repo
from backend.llm import build_llm


def sse(resp) -> list[dict]:
    return [json.loads(line[5:]) for line in resp.text.splitlines() if line.startswith("data:")]


class ChatHistoryTest(unittest.TestCase):
    def setUp(self):
        db.configure("sqlite://")
        O.SESSIONS.clear()
        from backend.main import app
        self.app = app

    def finished_chat(self, client) -> str:
        """Диалог, который уже закончился (как после перезапуска сервера: в памяти его нет)."""
        client.get("/api/me")
        vid = client.cookies.get("vid")
        s = O.create_session("Двушка в Казани до 9 млн, наличными", build_llm("mock"), visitor_id=vid)
        s.profile = {"city": "Казань", "rooms": 2, "budget_max": 9_000_000, "payment": "cash",
                     "summary": "Ищу 2-комнатную квартиру в г. Казань до 9 000 000 ₽."}
        s.push("message", role="user", text=s.query)
        s.push("message", role="agent", text="Нашёл объявлений: Циан — 29.")
        s.push("confirm", kind="red_lines", title="Красные линии", text="…")
        s.push("resolved", kind="red_lines", approved=True, label="Подтвердить рамки")
        repo.record(repo.update_agent_session, s.id, status="completed", profile=s.profile)
        s.push("done", status="completed")
        del O.SESSIONS[s.id]
        return s.id

    def test_list_and_replay_from_db(self):
        with TestClient(self.app) as c:
            sid = self.finished_chat(c)
            chats = c.get("/api/chats").json()["chats"]
            self.assertEqual([x["id"] for x in chats], [sid])
            self.assertEqual(chats[0]["title"], "Двушка в Казани до 9 млн, наличными")
            self.assertFalse(chats[0]["live"])
            evs = sse(c.get(f"/api/chat/{sid}/stream"))
            self.assertEqual([e["type"] for e in evs], ["message", "message", "confirm", "resolved", "done", "replay_end"])
            self.assertTrue(all(e.get("replay") for e in evs[:-1]))
            self.assertEqual(evs[-1]["live"], False)
            self.assertEqual([e["seq"] for e in evs[:-1]], [1, 2, 3, 4, 5])
            self.assertEqual([e["type"] for e in sse(c.get(f"/api/chat/{sid}/stream?after=3"))], ["resolved", "done", "replay_end"])

    def test_question_in_finished_chat_is_answered(self):
        with TestClient(self.app) as c:
            sid = self.finished_chat(c)
            r = c.post(f"/api/chat/{sid}/message", json={"text": "что такое СБР?"}).json()
            self.assertEqual((r["continued"], r["handled_as"], r["after"]), (True, "answer", 5))
            evs = sse(c.get(f"/api/chat/{sid}/stream?after={r['after']}"))
            types = [e["type"] for e in evs]
            self.assertIn("idle", types)
            self.assertIn("безопасн", " ".join(e.get("text", "") for e in evs))
            self.assertEqual(evs[0]["seq"], 6)  # нумерация продолжается

    def test_new_wish_continues_with_profile(self):
        with TestClient(self.app) as c:
            sid = self.finished_chat(c)
            r = c.post(f"/api/chat/{sid}/message", json={"text": "а теперь трёшку"}).json()
            self.assertEqual(r["handled_as"], "pipeline")
            s = O.SESSIONS[sid]
            self.assertEqual(s.base_profile["city"], "Казань")
            self.assertEqual(s.seq, 5)

    def test_other_browser_has_no_access(self):
        with TestClient(self.app) as c:
            sid = self.finished_chat(c)
        other = TestClient(self.app)  # без with: старт приложения заново создал бы пустую базу
        self.assertEqual(other.get(f"/api/chat/{sid}/stream").status_code, 404)
        self.assertEqual(other.post(f"/api/chat/{sid}/message", json={"text": "привет"}).status_code, 404)
        self.assertEqual(other.get("/api/chats").json()["chats"], [])

    def test_chat_follows_user_after_login(self):
        with TestClient(self.app) as c:
            sid = self.finished_chat(c)
            c.post("/api/auth/register", json={"email": "chat@x.ru", "password": "password123"})
        phone = TestClient(self.app)  # тот же пользователь на другом устройстве
        r = phone.post("/api/auth/login", json={"email": "chat@x.ru", "password": "password123"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([x["id"] for x in phone.get("/api/chats").json()["chats"]], [sid])
        self.assertEqual(phone.get(f"/api/chat/{sid}/stream").status_code, 200)

    def test_security_step_hidden_by_default(self):
        with TestClient(self.app) as c:
            self.assertFalse(c.get("/api/health").json()["features"]["security_step"])


if __name__ == "__main__":
    unittest.main()
