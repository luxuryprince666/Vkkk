# -*- coding: utf-8 -*-
"""Тесты драйверов площадок (этап M3).

Часть 1 — эвристики без браузера: выбор поля, кнопки, чтение вердикта, таймеры.
Часть 2 — живые прогоны на локальных «панелях-двойниках» (tests/fake_panel.py).
         Браузерные тесты пропускаются, если ни один движок не установлен.

Запуск:  python -m unittest tests.test_drivers -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

TMP = os.environ.get("VKFREE_DATA") or tempfile.mkdtemp(prefix="vkfree-drv-")
os.environ.setdefault("VKFREE_DATA", TMP)

from core import captcha, db, drivers, stealth  # noqa: E402
import fake_panel  # noqa: E402


def fresh_db() -> None:
    for f in Path(TMP).glob("app.db*"):
        f.unlink()
    db.DB_PATH = Path(TMP) / "app.db"
    db.DATA = Path(TMP)
    db.BACKUP_DIR = Path(TMP) / "backups"
    db.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    db.init_db()


def browser_ready() -> bool:
    try:
        return stealth.backend_status()["best"] is not None
    except Exception:
        return False


# ------------------------------------------------------------------ 1. эвристики

class TestHeuristics(unittest.TestCase):
    def test_link_field_by_name(self):
        fields = [
            {"sel": "#email", "type": "email", "name": "email", "ph": "", "label": "почта", "attrs": "email", "w": 300},
            {"sel": "#link", "type": "text", "name": "link", "ph": "Ссылка", "label": "", "attrs": "link", "w": 300},
        ]
        self.assertEqual(drivers.pick_link_field(fields)["sel"], "#link")

    def test_link_field_skips_email_and_count(self):
        fields = [
            {"sel": "#mail", "type": "text", "name": "email", "ph": "you@mail.ru", "label": "почта", "attrs": "email", "w": 250},
            {"sel": "#cnt", "type": "number", "name": "count", "ph": "", "label": "количество", "attrs": "count", "w": 80},
        ]
        self.assertIsNone(drivers.pick_link_field(fields))

    def test_link_field_fallback_single_wide(self):
        fields = [{"sel": "#a", "type": "text", "name": "", "ph": "", "label": "", "attrs": "", "w": 420}]
        self.assertEqual(drivers.pick_link_field(fields)["sel"], "#a")

    def test_link_field_ignores_readonly(self):
        fields = [{"sel": "#r", "type": "text", "name": "link", "ph": "", "label": "",
                   "attrs": "link", "w": 420, "readonly": True}]
        self.assertIsNone(drivers.pick_link_field(fields))

    def test_count_field(self):
        fields = [{"sel": "#l", "type": "text", "name": "link", "ph": "", "label": ""},
                  {"sel": "#c", "type": "number", "name": "count", "ph": "", "label": "кол-во"}]
        self.assertEqual(drivers.pick_count_field(fields, "#l")["sel"], "#c")

    def test_submit_button_by_text(self):
        buttons = [{"sel": "#a", "text": "Отмена", "disabled": False},
                   {"sel": "#b", "text": "Отправить заказ", "disabled": False}]
        self.assertEqual(drivers.pick_submit_button(buttons)["sel"], "#b")

    def test_disabled_button_returned_but_marked(self):
        """Выключенную кнопку драйвер берёт: он дождётся, когда площадка её включит."""
        buttons = [{"sel": "#a", "text": "Отправить", "disabled": True}]
        btn = drivers.pick_submit_button(buttons)
        self.assertIsNotNone(btn)
        self.assertTrue(btn["disabled"])

    def test_enabled_preferred_over_disabled(self):
        buttons = [{"sel": "#a", "text": "Отправить", "disabled": True},
                   {"sel": "#b", "text": "Отправить заказ", "disabled": False}]
        self.assertEqual(drivers.pick_submit_button(buttons)["sel"], "#b")

    def test_no_submit_like_button(self):
        buttons = [{"sel": "#a", "text": "Отмена", "disabled": False}]
        self.assertIsNone(drivers.pick_submit_button(buttons))

    def test_verdict_success(self):
        self.assertEqual(drivers.read_verdict("Заказ принят, ожидайте", False, False)[0], "ok")
        self.assertEqual(drivers.read_verdict("Ваш заказ создан", False, False)[0], "ok")
        self.assertEqual(drivers.read_verdict("order created", False, False)[0], "ok")

    def test_verdict_quota(self):
        self.assertEqual(drivers.read_verdict("Лимит на сегодня исчерпан", False, False)[0], "quota")
        self.assertEqual(drivers.read_verdict("Попробуйте позже", False, False)[0], "quota")

    def test_verdict_bad_link(self):
        self.assertEqual(drivers.read_verdict("Неверная ссылка", False, False)[0], "bad_link")
        self.assertEqual(drivers.read_verdict("Ссылка не найдена", False, False)[0], "bad_link")

    def test_verdict_bad_link_wins_over_success(self):
        text = "Заказ принят. Неверная ссылка в другом поле"
        self.assertEqual(drivers.read_verdict(text, False, False)[0], "bad_link")

    def test_verdict_captcha(self):
        self.assertEqual(drivers.read_verdict("Подтвердите, что вы не робот", False, False)[0], "captcha")
        self.assertEqual(drivers.read_verdict("", True, False)[0], "captcha")

    def test_verdict_success_wins_over_captcha(self):
        self.assertEqual(drivers.read_verdict("Заказ принят, ожидайте", True, False)[0], "ok")

    def test_verdict_need_human(self):
        self.assertEqual(drivers.read_verdict("Войдите в аккаунт", False, False)[0], "need_human")
        self.assertEqual(drivers.read_verdict("непонятно что", False, False)[0], "need_human")

    def test_timer_parsing(self):
        self.assertEqual(drivers.parse_timer("Заказ через 5 минут"), 300)
        self.assertEqual(drivers.parse_timer("подождите 30 секунд"), 30)
        self.assertEqual(drivers.parse_timer("осталось 02:30 до следующего"), 150)
        self.assertEqual(drivers.parse_timer("ничего про время"), 0)

    def test_timer_capped(self):
        self.assertLessEqual(drivers.parse_timer("через 999 минут"), 900)


# ------------------------------------------------------------------ 2. браузерные прогоны

@unittest.skipUnless(browser_ready(), "нет ни одного браузерного движка")
class TestDriverAgainstFakePanels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fresh_db()
        cls.srv, cls.base = fake_panel.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def svc(self, path: str, amount: int = 10) -> dict:
        return {"id": "fake-vk-followers", "site_id": "fake", "site_name": "Стенд",
                "label": "Подписчики ВК", "url": self.base + path, "amount": amount,
                "platform": "vk", "stype": "followers"}

    LINK = "https://vk.com/club243157309"

    def test_order_accepted(self):
        res = drivers.run(self.svc("/panel"), self.LINK, headless=True)
        self.assertEqual(res.verdict, "ok", res.detail)
        self.assertTrue(res.backend)
        self.assertIn("заказ принят", res.detail.lower())

    def test_quota_verdict(self):
        res = drivers.run(self.svc("/quota"), self.LINK, headless=True)
        self.assertEqual(res.verdict, "quota", res.detail)

    def test_bad_link_verdict(self):
        res = drivers.run(self.svc("/badlink"), self.LINK, headless=True)
        self.assertEqual(res.verdict, "bad_link", res.detail)

    def test_captcha_detected(self):
        res = drivers.run(self.svc("/captcha"), self.LINK, headless=True)
        self.assertEqual(res.verdict, "captcha", res.detail)

    def test_broken_page(self):
        res = drivers.run(self.svc("/broken"), self.LINK, headless=True)
        self.assertEqual(res.verdict, "site_broken", res.detail)

    def test_email_required_without_setting(self):
        db.set_setting("order_email", "")
        res = drivers.run(self.svc("/email"), self.LINK, headless=True)
        self.assertEqual(res.verdict, "need_human", res.detail)
        self.assertIn("e-mail", res.detail.lower())

    def test_email_taken_from_settings(self):
        db.set_setting("order_email", "robot@example.com")
        try:
            res = drivers.run(self.svc("/email"), self.LINK, headless=True)
            self.assertEqual(res.verdict, "ok", res.detail)
        finally:
            db.set_setting("order_email", "")

    def test_dry_run_does_not_submit(self):
        res = drivers.run(self.svc("/panel"), self.LINK, dry_run=True, headless=True)
        self.assertEqual(res.verdict, "ok", res.detail)
        self.assertIn("проверка прошла", res.detail)
        self.assertIn("link_sel", res.learned)
        self.assertNotIn("заказ принят", res.detail.lower())

    def test_dry_run_reports_button(self):
        res = drivers.run(self.svc("/panel"), self.LINK, dry_run=True, headless=True)
        self.assertIn("Отправить заказ", res.detail)

    def test_timer_page_waits_and_orders(self):
        res = drivers.run(self.svc("/timer"), self.LINK, headless=True)
        self.assertEqual(res.verdict, "ok", res.detail)

    def test_each_backend_can_run(self):
        """Проверяем, что драйвер не привязан к одному движку."""
        ok_backends = []
        for name in ("patchright", "camoufox", "nodriver", "playwright"):
            with self.subTest(backend=name):
                res = drivers.run(self.svc("/panel"), self.LINK, dry_run=True,
                                  prefer=name, headless=True)
                if res.verdict == "ok":
                    ok_backends.append(name)
                else:
                    self.assertIn("недоступен", res.detail.lower() + str(res.steps).lower())
        self.assertTrue(ok_backends, "ни один движок не смог пройти форму")


@unittest.skipUnless(stealth.curl_available(), "curl_cffi не установлен")
class TestNoJsDriver(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fresh_db()
        cls.srv, cls.base = fake_panel.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def svc(self) -> dict:
        return {"id": "fake-plain", "site_id": "fake", "url": self.base + "/plain",
                "amount": 25, "platform": "vk"}

    def test_form_found(self):
        info = drivers.nojs_capable(self.base + "/plain")
        self.assertTrue(info["ok"])
        self.assertIn("link", info["form"]["fields"])

    def test_no_form_on_js_panel(self):
        info = drivers.nojs_capable(self.base + "/panel")
        self.assertFalse(info["ok"])

    def test_dry_run(self):
        res = drivers.run_nojs(self.svc(), "https://vk.com/wall-1_2", dry_run=True)
        self.assertEqual(res.verdict, "ok")
        self.assertEqual(res.backend, "curl_cffi")

    def test_real_submit(self):
        res = drivers.run_nojs(self.svc(), "https://vk.com/wall-1_2")
        self.assertEqual(res.verdict, "ok", res.detail)
        self.assertIn("заказ создан", res.detail.lower())


class TestDriverMemory(unittest.TestCase):
    def setUp(self):
        fresh_db()          # каталог сам создаёт площадку doctorsmm

    def test_remember_and_forget(self):
        drivers.remember("doctorsmm", {"link_sel": "#link", "submit_sel": "#go"})
        mem = drivers._memory("doctorsmm")
        self.assertEqual(mem["link_sel"], "#link")
        drivers.forget("doctorsmm")
        self.assertEqual(drivers._memory("doctorsmm"), {})

    def test_remember_unknown_site_is_silent(self):
        drivers.remember("нет-такой", {"link_sel": "#x"})     # не должно падать
        self.assertEqual(drivers._memory("нет-такой"), {})

    def test_memory_merges(self):
        drivers.remember("doctorsmm", {"link_sel": "#a"})
        drivers.remember("doctorsmm", {"submit_sel": "#b"})
        mem = drivers._memory("doctorsmm")
        self.assertEqual(mem["link_sel"], "#a")
        self.assertEqual(mem["submit_sel"], "#b")


class TestBackendStatus(unittest.TestCase):
    def test_status_shape(self):
        st = stealth.backend_status()
        self.assertIn("backends", st)
        self.assertEqual(len(st["backends"]), 4)
        for b in st["backends"]:
            self.assertIn("installed", b)
            self.assertIn("pip", b)

    def test_preferred_order(self):
        order = stealth.preferred("nodriver")
        self.assertEqual(order[0], "nodriver")
        self.assertEqual(len(order), 4)

    def test_curl_get(self):
        if not stealth.curl_available():
            self.skipTest("curl_cffi не установлен")
        srv, base = fake_panel.start()
        try:
            res = stealth.http_get(base + "/panel")
            self.assertEqual(res["status"], 200)
            self.assertIn("Отправить заказ", res["text"])
        finally:
            srv.shutdown()


class CaptchaAfterSubmitTest(unittest.TestCase):
    """Капча, которая приезжает ПОСЛЕ нажатия кнопки (Cloudflare Turnstile у doctorsmm).

    Настоящий случай: робот вписал ссылку, нажал «Get It Now», кнопка стала «Checking…»
    и выехал Turnstile. Раньше это уходило в «ответ страницы непонятен» — теперь честно
    «капча», чтобы в режиме «С капчей» позвать человека.
    """

    @classmethod
    def setUpClass(cls):
        fresh_db()
        cls.srv, cls.base = fake_panel.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def svc(self, path: str) -> dict:
        return {"id": "fake-vk-followers", "site_id": "fake", "site_name": "Стенд",
                "label": "Подписчики ВК", "url": self.base + path, "amount": 10,
                "platform": "vk", "stype": "followers", "hold_seconds": 10}

    def test_detects_captcha_after_click(self):
        res = drivers.run(self.svc("/captcha-after"), "https://vk.com/club243159999",
                          headless=True, allow_captcha_wait=True, deadline_sec=90)
        self.assertEqual(res.verdict, "captcha", f"{res.verdict}: {res.detail}")
        self.assertIn("капч", res.detail.lower())
        self.assertTrue(res.shot, "скриншот не сделан")

    def test_modal_before_order_is_dismissed(self):
        """nakruti: после клика выезжает «Важно знать перед запуском» с кнопкой «Ок, понятно».

        Робот должен сам закрыть окно и добить заказ, а не решить, что ответ непонятен.
        """
        res = drivers.run(self.svc("/modal"), "https://vk.com/club243159999",
                          headless=True, deadline_sec=90)
        self.assertEqual(res.verdict, "ok", f"{res.verdict}: {res.detail}")
        self.assertTrue(any("закрыл окно" in st for st in res.steps), res.steps)

    def test_plain_panel_still_ok(self):
        """Обычная панель не должна превращаться в «капчу» из-за слова «Checking»."""
        res = drivers.run(self.svc("/panel"), "https://vk.com/club243159999",
                          headless=True, deadline_sec=90)
        self.assertEqual(res.verdict, "ok", f"{res.verdict}: {res.detail}")

    def test_quota_says_tomorrow(self):
        """«На сегодня использована, возвращайтесь завтра» — откладываем до завтра, не на час."""
        res = drivers.run(self.svc("/tomorrow"), "https://vk.com/club243159999",
                          headless=True, deadline_sec=90)
        self.assertEqual(res.verdict, "quota", f"{res.verdict}: {res.detail}")
        self.assertGreater(int(res.learned.get("retry_sec") or 0), 3600, res.learned)
        self.assertIn("завтра", res.detail)



class TestRetryDelay(unittest.TestCase):
    """Пауза после лимита: час, «через 5 минут» или до завтра — по тексту страницы."""

    def test_tomorrow(self):
        self.assertGreater(drivers.retry_delay_sec("на сегодня использована, возвращайтесь завтра"), 3600)

    def test_minutes(self):
        self.assertTrue(300 <= drivers.retry_delay_sec("следующий заказ через 5 минут") <= 400)

    def test_default_hour(self):
        self.assertEqual(drivers.retry_delay_sec("лимит исчерпан"), 3600)


class CaptchaSolveTest(unittest.TestCase):
    """Капчи робот решает сам: галочка «я не робот» и текст с картинки (локальный OCR)."""

    @classmethod
    def setUpClass(cls):
        fresh_db()
        cls.srv, cls.base = fake_panel.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        db.set_setting("captcha_autosolve", "1")
        db.set_setting("captcha_ocr", "1")

    def svc(self, path: str) -> dict:
        return {"id": "fake-vk-followers", "site_id": "fake", "site_name": "Стенд",
                "label": "Подписчики ВК", "url": self.base + path, "amount": 10,
                "platform": "vk", "stype": "followers", "hold_seconds": 10}

    def test_turnstile_checkbox_solved(self):
        """Turnstile: чекбокс внутри iframe. Робот жмёт его мышью и проходит заказ."""
        res = drivers.run(self.svc("/turnstile"), "https://vk.com/club243159999",
                          headless=True, allow_captcha_wait=True, deadline_sec=120)
        self.assertEqual(res.verdict, "ok", f"{res.verdict}: {res.detail} / {res.steps}")
        self.assertTrue(any("капча решена" in st for st in res.steps), res.steps)

    @unittest.skipUnless(captcha._have("ddddocr"), "ddddocr is optional")
    def test_image_captcha_solved(self):
        """Текстовая капча с картинки: ddddocr распознаёт код, робот его вписывает."""
        res = drivers.run(self.svc("/imgcaptcha"), "https://vk.com/club243159999",
                          headless=True, allow_captcha_wait=True, deadline_sec=120)
        self.assertEqual(res.verdict, "ok", f"{res.verdict}: {res.detail} / {res.steps}")
        self.assertTrue(any("распознал" in st for st in res.steps), res.steps)

    def test_autosolve_off_keeps_old_behaviour(self):
        """Выключили авторешение — робот честно зовёт человека, а не ломается."""
        db.set_setting("captcha_autosolve", "0")
        try:
            res = drivers.run(self.svc("/imgcaptcha"), "https://vk.com/club243159999",
                              headless=True, allow_captcha_wait=True, deadline_sec=80)
            self.assertEqual(res.verdict, "captcha", f"{res.verdict}: {res.detail}")
        finally:
            db.set_setting("captcha_autosolve", "1")

    def test_solvers_status(self):
        rows = captcha.solvers_status()
        ids = {r["id"] for r in rows}
        self.assertTrue({"click", "ocr", "cloudflare", "human"} <= ids, ids)
        ocr = next(r for r in rows if r["id"] == "ocr")
        self.assertEqual(ocr["ready"], captcha._have("ddddocr"))

    def test_cloudflare_wall_passed(self):
        """Заглушка Cloudflare проходит сама — робот ждёт и продолжает заход."""
        res = drivers.run(self.svc("/cloudflare"), "https://vk.com/club243159999",
                          headless=True, allow_captcha_wait=True, deadline_sec=120)
        self.assertEqual(res.verdict, "ok", f"{res.verdict}: {res.detail} / {res.steps}")
        self.assertTrue(any("челлендж" in st for st in res.steps), res.steps)

    def test_cloudflare_wall_detected(self):
        self.assertTrue(captcha.is_cloudflare_wall("Just a moment... checking your browser"))
        self.assertTrue(captcha.is_cloudflare_wall("Проверяем ваш браузер"))
        self.assertFalse(captcha.is_cloudflare_wall("Заказ принят, ожидайте"))

if __name__ == "__main__":
    unittest.main(verbosity=2)
