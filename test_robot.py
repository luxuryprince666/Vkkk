# -*- coding: utf-8 -*-
"""Тесты робота и логики капчи — без Playwright, на фальшивой странице.

Проверяем именно то, что легко сломать: распознавание капчи, поиск поля/кнопки,
передачу капчи человеку, поведение при скрытом окне и учёт капчи в базе.

Запуск:  python -m unittest tests.test_robot -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = tempfile.mkdtemp(prefix="vkfree-robot-")
os.environ["VKFREE_DATA"] = TMP

from core import db, engine, executor  # noqa: E402

SVC = {
    "id": "doctorsmm-vk-followers",
    "site_id": "doctorsmm",
    "site_name": "SMM8",
    "label": "Подписчики ВК",
    "url": "https://doctorsmm.com/free-vk-followers",
    "hold_seconds": 0,
    "amount": 10,
}


class FakeLocator:
    def __init__(self, page, key, kind):
        self.page, self.key, self.kind = page, key, kind

    @property
    def first(self):
        return self

    def count(self) -> int:
        return 1 if self.page.has(self.key, self.kind) else 0

    def is_visible(self) -> bool:
        return self.page.has(self.key, self.kind)

    def fill(self, value: str) -> None:
        self.page.filled = value

    def click(self) -> None:
        self.page.clicks += 1
        self.page.on_click()


class FakePage:
    """Минимальная имитация страницы Playwright."""

    def __init__(self, selectors=(), buttons=(), text="", captcha_on_submit=False, captcha_clicks=1):
        self.selectors = set(selectors)
        self.buttons = set(buttons)
        self.text = text
        self.captcha_on_submit = captcha_on_submit
        self.captcha_clicks = captcha_clicks      # после скольких отправок появляется капча
        self.captcha_active = False
        self.filled = None
        self.clicks = 0
        self.visited = None
        self.shots = 0

    # --- поведение
    def has(self, key, kind="locator") -> bool:
        if kind == "role":
            return key in self.buttons
        if self.captcha_active and (key in executor.CAPTCHA_STRONG or key in executor.CAPTCHA_WEAK):
            return True
        return key in self.selectors

    def on_click(self) -> None:
        if self.captcha_on_submit and self.clicks <= self.captcha_clicks:
            self.captcha_active = True

    # --- API, который использует робот
    def locator(self, sel):
        return FakeLocator(self, sel, "locator")

    def get_by_role(self, role, name=None):
        return FakeLocator(self, name, "role")

    def inner_text(self, sel):
        return self.text

    def content(self):
        return self.text

    def wait_for_timeout(self, ms):
        time.sleep(min(ms, 5) / 1000)     # не тормозим тесты

    def goto(self, url, wait_until=None):
        self.visited = url

    def set_default_timeout(self, ms):
        pass

    def screenshot(self, path=None):
        self.shots += 1


def fresh_db() -> None:
    for f in Path(TMP).glob("app.db*"):
        f.unlink()
    db.DB_PATH = Path(TMP) / "app.db"
    db.DATA = Path(TMP)
    db.BACKUP_DIR = Path(TMP) / "backups"
    db.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    db.init_db()


class TestDetection(unittest.TestCase):
    def setUp(self):
        fresh_db()

    def test_no_captcha_on_clean_page(self):
        page = FakePage(selectors={"input[type=url]", "button[type=submit]"},
                        text="Бесплатные подписчики ВК. Вставьте ссылку и нажмите получить.")
        self.assertIsNone(executor.detect_captcha(page))

    def test_recaptcha_frame(self):
        page = FakePage(selectors={"input[type=url]", "iframe[src*='recaptcha/api2/bframe']"})
        self.assertIn("recaptcha", executor.detect_captcha(page))

    def test_image_captcha(self):
        page = FakePage(selectors={"img[src*='captcha' i]"})
        self.assertIsNotNone(executor.detect_captcha(page))

    def test_captcha_by_text(self):
        page = FakePage(text="Подтвердите, что вы не робот")
        self.assertIsNotNone(executor.detect_captcha(page))

    def test_weak_widget(self):
        page = FakePage(selectors={"iframe[src*='turnstile']"})
        self.assertIsNotNone(executor.detect_captcha(page))


class TestFormHelpers(unittest.TestCase):
    def setUp(self):
        fresh_db()

    def test_find_link_field_prefers_url(self):
        page = FakePage(selectors={"input[type=text]", "input[name*=link i]"})
        self.assertEqual(executor.find_link_field(page), "input[name*=link i]")

    def test_find_link_field_fallback(self):
        page = FakePage(selectors={"form input[type=text]"})
        self.assertEqual(executor.find_link_field(page), "form input[type=text]")

    def test_no_field(self):
        self.assertIsNone(executor.find_link_field(FakePage()))

    def test_click_by_text(self):
        page = FakePage(buttons={"получить"})
        self.assertTrue(executor.click_submit(page))
        self.assertEqual(page.clicks, 1)

    def test_click_fallback(self):
        page = FakePage(selectors={"button[type=submit]"})
        self.assertTrue(executor.click_submit(page))

    def test_click_nothing(self):
        self.assertFalse(executor.click_submit(FakePage()))

    def test_looks_submitted(self):
        self.assertTrue(executor.looks_submitted(FakePage(text="Заказ создан, ожидайте")))
        self.assertFalse(executor.looks_submitted(FakePage(text="введите ссылку")))


class TestRobotFlow(unittest.TestCase):
    def setUp(self):
        fresh_db()
        db.set_setting("captcha_wait_sec", "3")

    def test_happy_path(self):
        page = FakePage(selectors={"input[type=url]", "button[type=submit]"},
                        text="заказ создан")
        ok, note, _shot = executor.robot_flow(page, SVC, "https://vk.com/x", 1, headless=False)
        self.assertTrue(ok)
        self.assertIn("отправил", note)
        self.assertEqual(page.filled, "https://vk.com/x")
        self.assertEqual(page.visited, SVC["url"])

    def test_missing_field_fails_softly(self):
        ok, note, _shot = executor.robot_flow(FakePage(text="что-то"), SVC, "https://vk.com/x", 2, headless=False)
        self.assertFalse(ok)
        self.assertIn("поле для ссылки", note)

    def test_missing_button_fails_softly(self):
        page = FakePage(selectors={"input[type=url]"})
        ok, note, _shot = executor.robot_flow(page, SVC, "https://vk.com/x", 3, headless=False)
        self.assertFalse(ok)
        self.assertIn("кнопк", note)

    def test_captcha_solved_by_human(self):
        page = FakePage(selectors={"input[type=url]", "button[type=submit]"},
                        text="заказ создан", captcha_on_submit=True)
        threading.Timer(0.3, lambda: setattr(page, "captcha_active", False)).start()
        ok, note, _shot = executor.robot_flow(page, SVC, "https://vk.com/x", 4, headless=False)
        self.assertTrue(ok, note)

    def test_captcha_at_the_end_after_success_does_not_cancel(self):
        """Заявку приняли, но на странице всплыла защита — заход не считаем провальным."""

        class LateCaptchaPage(FakePage):
            """Капча всплывает ровно на паузе «ждём таймер сайта» — уже после отправки."""

            def wait_for_timeout(self, ms):
                super().wait_for_timeout(ms)
                if ms == 1000:                        # hold_seconds=1 у услуги ниже
                    self.captcha_active = True

        svc = dict(SVC, hold_seconds=1)
        page = LateCaptchaPage(selectors={"input[type=url]", "button[type=submit]"}, text="заказ создан")
        ok, note, _shot = executor.robot_flow(page, svc, "https://vk.com/x", 8, headless=True)
        self.assertTrue(ok, note)
        self.assertIn("защиту", note)

    def test_captcha_confirmed_by_button(self):
        page = FakePage(selectors={"input[type=url]", "button[type=submit]"},
                        text="заказ создан", captcha_on_submit=True)
        threading.Timer(0.2, lambda: executor.confirm_captcha_by_human(5)).start()
        ok, note, _shot = executor.robot_flow(page, SVC, "https://vk.com/x", 5, headless=False)
        self.assertTrue(ok, note)
        self.assertIn("вручную", note)

    def test_captcha_headless_gives_up_and_learns(self):
        page = FakePage(selectors={"input[type=url]", "button[type=submit]"}, captcha_on_submit=True)
        ok, note, _shot = executor.robot_flow(page, SVC, "https://vk.com/x", 6, headless=True)
        self.assertFalse(ok)
        self.assertIn("скрыто", note)
        with db.rd() as conn:
            cap = conn.execute("SELECT captcha FROM services WHERE id=?", (SVC["id"],)).fetchone()[0]
        self.assertEqual(cap, 1, "робот должен запомнить, что тут капча")

    def test_captcha_timeout(self):
        db.set_setting("captcha_wait_sec", "2")
        page = FakePage(selectors={"input[type=url]", "button[type=submit]"}, captcha_on_submit=True)
        ok, note, _shot = executor.robot_flow(page, SVC, "https://vk.com/x", 7, headless=False)
        self.assertFalse(ok)
        self.assertIn("не решена", note)


class TestCaptchaState(unittest.TestCase):
    def setUp(self):
        fresh_db()

    def test_status_is_captcha_while_waiting(self):
        executor.mark_captcha(SVC["id"], 1)
        with db.tx() as c:
            c.execute("INSERT INTO tasks(service_id,status,created_at) VALUES(?,'running',0)", (SVC["id"],))
            task_id = c.execute("SELECT id FROM tasks").fetchone()["id"]
        page = FakePage(selectors={"iframe[src*='recaptcha/api2/bframe']"})
        threading.Timer(0.2, lambda: setattr(page, "captcha_active", False)).start()
        # капча «висит» в селекторах — снимаем через тот же таймер
        page.captcha_active = True
        threading.Timer(0.3, lambda: page.selectors.discard("iframe[src*='recaptcha/api2/bframe']")).start()
        ok, why = executor.handle_captcha(page, SVC, task_id, headless=False)
        self.assertTrue(ok, why)
        with db.rd() as c:
            self.assertEqual(c.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()[0], "running")

    def test_mark_captcha_persists(self):
        executor.mark_captcha("doctorsmm-vk-followers", 1)
        self.assertEqual(engine.services_map()["doctorsmm-vk-followers"]["captcha"], 1)
        executor.mark_captcha("doctorsmm-vk-followers", 0)
        self.assertEqual(engine.services_map()["doctorsmm-vk-followers"]["captcha"], 0)

    def test_headless_follows_mode(self):
        db.set_setting("captcha_browser_visible", "1")
        db.set_setting("captcha_mode", "all")
        self.assertFalse(executor._headless_now(), "режим «все сайты» — окно видно, капчу решает человек")
        db.set_setting("captcha_mode", "nocaptcha")
        self.assertTrue(executor._headless_now(), "режим «без капчи» — окно скрыто, человек не нужен")
        db.set_setting("captcha_mode", "captcha")
        self.assertFalse(executor._headless_now(), "режим «с капчей» — окно видно")
        db.set_setting("captcha_browser_visible", "0")
        self.assertTrue(executor._headless_now(), "окно отключено в настройках")


if __name__ == "__main__":
    unittest.main(verbosity=2)
