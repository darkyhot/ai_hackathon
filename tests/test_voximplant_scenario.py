"""Сценарий VoxEngine (backend/voximplant/scenario.js) на имитации среды Voximplant (tests/voximplant/harness.js).

Проверяем протокол с браузером: очередь реплик, «said», «heard», отказы, конец звонка, лимиты.
Нужен Node.js; без него тесты пропускаются.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

HARNESS = Path(__file__).resolve().parent / "voximplant" / "harness.js"
NODE = shutil.which("node")


def run(case: str) -> dict:
    out = subprocess.run([NODE, str(HARNESS), case], capture_output=True, text=True, timeout=30)
    if out.returncode:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout)


def actions(res: dict, name: str) -> list:
    return [x for x in res["log"] if x[0] == name]


@unittest.skipUnless(NODE, "нужен Node.js")
class ScenarioTest(unittest.TestCase):
    def test_happy_path(self):
        r = run("happy")
        self.assertEqual(actions(r, "callPSTN"), [["callPSTN", "79991234567", "74951234567"]])
        self.assertIn(["answer", "web"], r["log"])
        types = [m["type"] for m in r["toBrowser"]]
        self.assertEqual(types, ["ringing", "answered", "said", "said", "heard", "ended"])
        # первая реплика пришла до ответа — прозвучала только после Connected, по очереди, голосом по умолчанию
        says = actions(r, "say")
        self.assertEqual([s[1] for s in says], ["Здравствуйте! Я ИИ-ассистент покупателя.", "Квартира продаётся?"])
        self.assertTrue(all(s[2] == "salute:sergey" for s in says))
        self.assertEqual([m["id"] for m in r["toBrowser"] if m["type"] == "said"], [1, 2])
        heard = [m for m in r["toBrowser"] if m["type"] == "heard"]
        self.assertEqual(heard, [{"type": "heard", "text": "Да, продаётся", "confidence": 0.93}])
        # распознаём только голос продавца
        self.assertEqual(actions(r, "createASR"), [["createASR", "salute-asr:ru", False]])
        self.assertEqual(actions(r, "sendMediaTo"), [["sendMediaTo", "asr"]])
        self.assertEqual(r["toBrowser"][-1], {"type": "ended", "reason": "seller"})
        self.assertIn(["terminate"], r["log"])
        self.assertIn(["hangup", "web"], r["log"])

    def test_bad_number_never_dials(self):
        r = run("bad_number")
        self.assertEqual(actions(r, "callPSTN"), [])
        self.assertEqual(r["toBrowser"][0]["type"], "failed")
        self.assertEqual(r["toBrowser"][0]["code"], 484)
        self.assertEqual(r["toBrowser"][-1]["type"], "ended")

    def test_no_callerid(self):
        r = run("no_callerid")
        self.assertEqual(actions(r, "callPSTN"), [])
        self.assertEqual(r["toBrowser"][0]["code"], 403)

    def test_same_number_never_dials(self):
        r = run("same_number")  # звонок с номера на этот же номер оператор отклоняет (403) — не набираем вовсе
        self.assertEqual(actions(r, "callPSTN"), [])
        self.assertEqual(r["toBrowser"][0]["code"], 482)

    def test_busy(self):
        r = run("busy")
        self.assertEqual(r["toBrowser"][0], {"type": "failed", "code": 486, "reason": "Busy Here"})
        self.assertEqual(r["toBrowser"][1]["type"], "ended")

    def test_stop_cuts_speech_and_queue(self):
        r = run("stop")
        self.assertEqual([s[1] for s in actions(r, "say")], ["Длинная реплика"])  # вторая реплика не прозвучала
        self.assertEqual(actions(r, "stopPlayback"), [["stopPlayback"]])
        self.assertEqual([m for m in r["toBrowser"] if m["type"] == "said"], [{"type": "said", "id": 1}])

    def test_asr_restarts_while_call_active(self):
        r = run("asr_restart")
        self.assertEqual(len(actions(r, "createASR")), 2)
        self.assertIn({"type": "heard", "text": "Ниже восьми восьмисот не могу"}, r["toBrowser"])

    def test_voice_and_asr_from_config(self):
        r = run("voices")
        self.assertEqual(actions(r, "say")[0][2], "yandex:alena")
        self.assertEqual(actions(r, "createASR")[0][1], "yandex-asr:ru")

    def test_unknown_voice_falls_back(self):
        r = run("bad_voice")
        self.assertEqual(actions(r, "say")[0][2], "salute:sergey")
        self.assertEqual(actions(r, "createASR")[0][1], "salute-asr:ru")

    def test_client_hangup(self):
        r = run("client_hangup")
        self.assertIn(["hangup", "pstn"], r["log"])
        self.assertEqual(actions(r, "say"), [])  # после конца звонка ничего не говорим
        self.assertEqual(r["toBrowser"][-1], {"type": "ended", "reason": "client"})

    def test_call_time_limit(self):
        r = run("timeout")
        self.assertEqual(r["toBrowser"][-1], {"type": "ended", "reason": "timeout"})
        self.assertIn(["hangup", "pstn"], r["log"])

    def test_one_call_per_session(self):
        r = run("second_call")
        self.assertEqual(actions(r, "reject"), [["reject", "web", 486]])
        self.assertEqual(len(actions(r, "callPSTN")), 1)

    def test_text_limits(self):
        r = run("long_text")
        says = actions(r, "say")
        self.assertEqual(len(says), 1)
        self.assertEqual(len(says[0][1]), 1500)
        self.assertIn({"type": "said", "id": 2}, r["toBrowser"])  # пустая реплика сразу «сказана»


if __name__ == "__main__":
    unittest.main()
