# -*- coding: utf-8 -*-
"""Тесты нативного пульта: логика без GUI (сеть и выжимка состояния)."""
from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import panel  # noqa: E402


STATE = {
    "ok": True,
    "stats": {"in_work_hours": True, "gap_left": 0, "total_claims_today": 7,
              "captcha": {"mode": "nocaptcha", "mode_label": "Без капчи"}},
    "health": {"playwright": True, "captcha_tasks": 2},
    "settings": {"autopilot": "1"},
    "services": [{"ready": True}, {"ready": True}, {"ready": False}],
}


class TestSummary(unittest.TestCase):
    def test_online(self):
        s = panel.summary(STATE)
        self.assertTrue(s["online"])
        self.assertEqual(s["line1"], "заходы разрешены")
        self.assertEqual(s["ready"], 2)
        self.assertEqual(s["today"], 7)
        self.assertEqual(s["mode"], "nocaptcha")
        self.assertTrue(s["autopilot"])
        self.assertEqual(s["captcha_wait"], 2)

    def test_offline(self):
        s = panel.summary({"ok": False, "error": "нет связи"})
        self.assertFalse(s["online"])
        self.assertIn("нет связи", s["line1"])

    def test_outside_work_hours(self):
        st = json.loads(json.dumps(STATE))
        st["stats"]["in_work_hours"] = False
        self.assertEqual(panel.summary(st)["line1"], "вне рабочих часов")

    def test_gap_pause(self):
        st = json.loads(json.dumps(STATE))
        st["stats"]["gap_left"] = 12
        self.assertIn("12", panel.summary(st)["line1"])

    def test_empty_state_does_not_crash(self):
        s = panel.summary({})
        self.assertIn("line1", s)
        self.assertEqual(s["ready"], 0)


class TestEvents(unittest.TestCase):
    def test_lines(self):
        lines = panel.events_lines({"events": [{"level": "warn", "message": "капча"},
                                               {"level": "info", "message": "ок"}]})
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("!"))
        self.assertTrue(lines[1].startswith("·"))

    def test_empty(self):
        self.assertEqual(panel.events_lines({}), [])

    def test_limit(self):
        payload = {"events": [{"level": "info", "message": str(i)} for i in range(20)]}
        self.assertEqual(len(panel.events_lines(payload, limit=5)), 5)


class _Stub(BaseHTTPRequestHandler):
    """Крошечный сервер-заглушка: проверяем, что пульт правильно говорит по HTTP."""
    def do_GET(self):
        body = json.dumps(STATE).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = self.rfile.read(length) if length else b"{}"
        body = json.dumps({"ok": True, "echo": json.loads(payload)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class TestApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _Stub)
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_get_state(self):
        data = panel.api(self.base, "/api/state")
        self.assertTrue(data["ok"])
        self.assertEqual(panel.summary(data)["today"], 7)

    def test_post_settings(self):
        data = panel.api(self.base, "/api/settings", "POST", {"autopilot": "0"})
        self.assertTrue(data["ok"])
        self.assertEqual(data["echo"]["autopilot"], "0")

    def test_dead_port_returns_error(self):
        data = panel.api("http://127.0.0.1:9", "/api/state", timeout=1.0)
        self.assertFalse(data["ok"])
        self.assertIn("error", data)

    def test_no_display_means_no_panel(self):
        if os.environ.get("DISPLAY") or os.name == "nt":
            self.skipTest("есть дисплей — проверять нечего")
        self.assertFalse(panel.available(), "без дисплея окно создаваться не должно")


if __name__ == "__main__":
    unittest.main(verbosity=2)
