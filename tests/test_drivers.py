# -*- coding: utf-8 -*-
"""Тесты драйверов площадок (этап M3).

Часть 1 — эвристики без браузера: выбор поля, кнопки, чтение вердикта, таймеры.
Часть 2 — живые прогоны на локальных «панелях-двойниках» (tests/fake_panel.py).
         Браузерные тесты пропускаются, если ни один движок не установлен.

Запуск:  python -m unittest tests.test_drivers -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_generic_please_wait_is_not_a_proven_rejection(self):
        self.assertEqual(drivers.read_verdict("Пожалуйста, подождите", False, False)[0], 'need_human')
        self.assertEqual(drivers.read_verdict_delta('Форма', 'Форма\nПодождите', False, False)[0], 'need_human')
        self.assertEqual(drivers.read_verdict('Заказ принят, пожалуйста подождите', False, False)[0], 'ok')

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

    def test_old_answer_cannot_confirm_or_reject_a_new_order(self):
        for stale in ("Заказ принят", "Неверная ссылка", "Лимит исчерпан"):
            with self.subTest(stale=stale):
                self.assertEqual(drivers.read_verdict_delta(stale, stale, False, False)[0], 'need_human')
                self.assertEqual(drivers.read_verdict_delta(stale, stale + '\nПроверяем', False, False)[0],
                                 'need_human')
        self.assertEqual(drivers.read_verdict_delta('Форма', 'Форма\nЗаказ принят', False, False)[0], 'ok')
        self.assertEqual(drivers.read_verdict_delta('Форма', 'Форма\nНеверная ссылка', False, False)[0], 'bad_link')
        self.assertEqual(drivers.read_verdict_delta('Форма', 'Форма', True, False)[0], 'captcha')

    def test_timer_parsing(self):
        self.assertEqual(drivers.parse_timer("Заказ через 5 минут"), 300)
        self.assertEqual(drivers.parse_timer("подождите 30 секунд"), 30)
        self.assertEqual(drivers.parse_timer("осталось 02:30 до следующего"), 150)
        self.assertEqual(drivers.parse_timer("ничего про время"), 0)

    def test_timer_capped(self):
        self.assertLessEqual(drivers.parse_timer("через 999 минут"), 900)


class TestSMMnakrutkaSafety(unittest.TestCase):
    TARGET = 'https://vk.ru/club123'
    URL = 'https://smmnakrutka.ru/free-vk-followers'
    SVC = {'site_id': 'smmnakrutka', 'platform': 'vk', 'stype': 'followers',
           'url': URL, 'amount': 10}
    READY = {'state': 'ready', 'link': '#form input[name="link"]',
             'button': '#form button[type="submit"]', 'email': '',
             'value': TARGET, 'successUrl': URL + '/?12'}

    class Page:
        def __init__(self):
            self.events = []
        def js(self, script):
            return TestSMMnakrutkaSafety.URL if script == 'window.location.href' else None
        def click_human(self, selector):
            self.events.append('click')
            return False  # даже если движок не уверен, повторять нельзя
        def wait(self, ms):
            pass

    def test_wrong_or_insecure_redirect_is_not_trusted(self):
        url = self.URL
        self.assertTrue(drivers._same_service_location(url, url + '/?12'))
        for other in ('http://smmnakrutka.ru/free-vk-followers',
                      'https://smmnakrutka.ru.evil/free-vk-followers',
                      'https://smmnakrutka.ru@evil.test/free-vk-followers',
                      'https://smmnakrutka.ru/other'):
            with self.subTest(url=other):
                self.assertFalse(drivers._same_service_location(url, other))

    def test_missing_timer_tokens_times_out_without_submission(self):
        page = self.Page()
        clock = [0.0]
        page.wait = lambda ms: clock.__setitem__(0, clock[0] + ms / 1000)
        with patch.object(drivers.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(drivers, '_smmnakrutka_state', return_value={'state': 'waiting_tokens'}), \
             patch.object(drivers, 'discover', return_value={'captcha': []}), \
             patch.object(captcha, 'visible_widgets', return_value=[]):
            res = drivers._run_smmnakrutka(page, self.SVC, self.TARGET, [], dry_run=False, budget=3,
                                           on_submit=lambda: page.events.append('risk'))
        self.assertEqual(res.verdict, 'need_human')
        self.assertIn('проверки таймера', res.detail)
        self.assertEqual(page.events, [])
        self.assertFalse(res.submitted)

    def test_ambiguous_click_never_retries(self):
        page = self.Page()
        with patch.object(drivers, '_smmnakrutka_state', return_value=self.READY.copy()), \
             patch.object(drivers, '_smmnakrutka_response', return_value={'url': self.URL}), \
             patch.object(drivers, 'set_field', return_value=True), \
             patch.object(drivers, 'discover', return_value={'captcha': []}), \
             patch.object(captcha, 'visible_widgets', return_value=[]):
            res = drivers._run_smmnakrutka(page, self.SVC, self.TARGET, [], dry_run=False, budget=3,
                                           on_submit=lambda: page.events.append('risk'))
        self.assertEqual(page.events, ['risk', 'click'])
        self.assertTrue(res.submitted)
        self.assertEqual(res.verdict, 'need_human')
        self.assertIn('повтор запрещён', res.detail)

    def test_unchanged_url_or_trailing_slash_is_not_a_success_redirect(self):
        page = self.Page()
        clock = [0.0]
        page.wait = lambda ms: clock.__setitem__(0, clock[0] + ms / 1000)
        page.click_human = lambda selector: (page.events.append('click') or True)
        ready = {**self.READY, 'successUrl': self.URL + '/'}
        with patch.object(drivers.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(drivers, '_smmnakrutka_state', return_value=ready), \
             patch.object(drivers, '_smmnakrutka_response', return_value={'url': self.URL}), \
             patch.object(drivers, 'set_field', return_value=True), \
             patch.object(drivers, 'discover', return_value={'captcha': []}), \
             patch.object(captcha, 'visible_widgets', return_value=[]):
            res = drivers._run_smmnakrutka(page, self.SVC, self.TARGET, [], dry_run=False, budget=3,
                                           on_submit=lambda: page.events.append('risk'))
        self.assertEqual(page.events, ['risk', 'click'])
        self.assertEqual(res.verdict, 'need_human')
        self.assertTrue(res.submitted)

    def test_visible_error_has_priority_over_success_text(self):
        page = self.Page()
        page.click_human = lambda selector: (page.events.append('click') or True)
        responses = [{'url': self.URL}, {'url': self.URL},
                     {'url': self.URL, 'success': 'Спасибо за доверие!', 'errors': 'Неверная ссылка'}]
        with patch.object(drivers, '_smmnakrutka_state', return_value=self.READY.copy()), \
             patch.object(drivers, '_smmnakrutka_response', side_effect=responses), \
             patch.object(drivers, 'set_field', return_value=True), \
             patch.object(drivers, 'discover', return_value={'captcha': []}), \
             patch.object(captcha, 'visible_widgets', return_value=[]):
            res = drivers._run_smmnakrutka(page, self.SVC, self.TARGET, [], dry_run=False, budget=3,
                                           on_submit=lambda: page.events.append('risk'))
        self.assertEqual(res.verdict, 'bad_link')
        self.assertTrue(res.submitted)
        self.assertEqual(page.events, ['risk', 'click'])

    def test_counter_failure_from_callback_prevents_click(self):
        page = self.Page()
        def fail():
            page.events.append('counter failed')
            raise ValueError('counter unavailable')
        with patch.object(drivers, '_smmnakrutka_state', return_value=self.READY.copy()), \
             patch.object(drivers, '_smmnakrutka_response', return_value={'url': self.URL}), \
             patch.object(drivers, 'set_field', return_value=True), \
             patch.object(drivers, 'discover', return_value={'captcha': []}), \
             patch.object(captcha, 'visible_widgets', return_value=[]):
            with self.assertRaisesRegex(ValueError, 'counter unavailable'):
                drivers._run_smmnakrutka(page, self.SVC, self.TARGET, [], dry_run=False, budget=3,
                                         on_submit=fail)
        self.assertEqual(page.events, ['counter failed'])

    def test_form_rechecked_after_callback_before_click(self):
        page = self.Page()
        states = [self.READY.copy(), self.READY.copy(), {'state': 'service_mismatch'}]
        with patch.object(drivers, '_smmnakrutka_state', side_effect=states), \
             patch.object(drivers, '_smmnakrutka_response', return_value={'url': self.URL}), \
             patch.object(drivers, 'set_field', return_value=True), \
             patch.object(drivers, 'discover', return_value={'captcha': []}), \
             patch.object(captcha, 'visible_widgets', return_value=[]):
            res = drivers._run_smmnakrutka(page, self.SVC, self.TARGET, [], dry_run=False, budget=3,
                                           on_submit=lambda: page.events.append('risk'))
        self.assertEqual(page.events, ['risk'])
        self.assertEqual(res.verdict, 'need_human')
        self.assertTrue(res.submitted)  # риск уже записан, освобождать барьер нельзя

    def test_wait_is_interruptible_before_click(self):
        page, event = self.Page(), threading.Event()
        page.wait = lambda ms: event.set()
        with patch.object(drivers, '_smmnakrutka_state', return_value={'state': 'waiting'}), \
             patch.object(drivers, 'discover', return_value={'captcha': []}), \
             patch.object(captcha, 'visible_widgets', return_value=[]):
            with self.assertRaises(drivers.OperationCancelled):
                drivers._run_smmnakrutka(drivers._InterruptiblePage(page, event), self.SVC, self.TARGET, [],
                                         dry_run=False, budget=10,
                                         on_submit=lambda: page.events.append('risk'))
        self.assertEqual(page.events, [])


class TestLikeBoomSafety(unittest.TestCase):
    """Модель страницы: нет сети и настоящих заказов."""

    class Page:
        def __init__(self, free=False, hidden_polls=0):
            self.free = free
            self.hidden_polls = hidden_polls
            self.polls = 0
            self.events = []

        def js(self, script):
            if script == drivers.LIKEBOOM_FREE_PAYMENT_JS:
                return self.free
            if 'Получить бесплатно' in script:
                self.polls += 1
                return '' if self.polls <= self.hidden_polls else 'Получить бесплатно'
            if script == drivers.LIKEBOOM_SELECT_CATEGORY_JS:
                return 'ok'
            if script == drivers.LIKEBOOM_SERVICE_OPTIONS_JS:
                return json.dumps([{'v': '55', 't': 'Подписчики в группу'}], ensure_ascii=False)
            if script.startswith(drivers.LIKEBOOM_SELECT_SERVICE_JS):
                return 'ok'
            if script == drivers.LIKEBOOM_TOTAL_PRICE_JS:
                return json.dumps(['10,00 ₽'], ensure_ascii=False)
            return None

        def click_human(self, selector):
            self.events.append(selector)
            return True

        def type_human(self, selector, text):
            return True

        def wait(self, ms):
            self.events.append(('wait', ms))

        def text(self):
            return 'Заказ № 123'

    def run_page(self, page, callback):
        with patch.object(drivers, '_contact_email', return_value='tester@example.com'):
            return drivers._run_likeboom(page, 'https://vk.ru/club123', [], dry_run=False,
                                         wait_button_sec=10, on_submit=callback)

    def test_autostarted_timer_is_waited_without_assuming_payment_is_free(self):
        page = self.Page(hidden_polls=2)
        result = self.run_page(page, lambda: self.fail('оплата не подтверждена'))
        self.assertEqual(result[0], 'need_human')
        self.assertIn('бесплатный способ оплаты', result[1])
        self.assertEqual(page.polls, 3)
        self.assertEqual(page.events.count(('wait', 4000)), 2)
        clicks = [e for e in page.events if isinstance(e, str)]
        self.assertEqual(clicks, ["button:has-text('Получить бесплатно')"])

    def test_payment_is_rechecked_after_counter_callback(self):
        page = self.Page(free=True)
        def callback():
            page.events.append('risk')
            page.free = False  # состояние формы изменилось во время замера
        result = self.run_page(page, callback)
        self.assertEqual(result[0], 'need_human')
        self.assertIn('изменился', result[1])
        self.assertEqual(page.events.count('risk'), 1)
        self.assertNotIn("button:has-text('Оформить заказ')", page.events)

    def test_failed_counter_callback_never_clicks_order(self):
        page = self.Page(free=True)
        def callback():
            raise ValueError('счётчик недоступен')
        with self.assertRaisesRegex(ValueError, 'счётчик недоступен'):
            self.run_page(page, callback)
        self.assertNotIn("button:has-text('Оформить заказ')", page.events)

    def test_confirmed_free_payment_allows_at_most_one_order_click(self):
        page = self.Page(free=True)
        result = self.run_page(page, lambda: page.events.append('risk'))
        self.assertEqual(result[0], 'ok', result)
        self.assertEqual(page.events.count('risk'), 1)
        self.assertEqual(page.events.count("button:has-text('Оформить заказ')"), 1)
        self.assertLess(page.events.index('risk'), page.events.index("button:has-text('Оформить заказ')"))


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
        cls.srv.server_close()

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

    def test_likeboom_payment_requires_selected_and_submitted_method_to_agree(self):
        # Локальный HTML повторяет форму с отдельными radio и скрытым полем;
        # вызовов сети, счётчика и кнопки заказа в этом тесте нет.
        html = '''<form><input id="link"><input id="fn_payment_method"
            name="OrderForm[paymentMethod]" type="hidden" value="yookassa">
            <input name="payment_method_radio" type="radio" value="free" checked>
            <button type="submit">Оформить заказ</button></form>'''
        with stealth.open_page(headless=True) as (_, page):
            page.goto('data:text/html,<html><body></body></html>', wait=10)
            page.js('document.body.innerHTML = ' + json.dumps(html))
            self.assertFalse(drivers._likeboom_free_payment(page))
            page.js("document.querySelector('#fn_payment_method').value = 'FREE'")
            self.assertTrue(drivers._likeboom_free_payment(page))
            page.js("document.querySelector('#fn_payment_method').disabled = true")
            self.assertFalse(drivers._likeboom_free_payment(page))
            page.js("document.querySelector('#fn_payment_method').disabled = false")
            page.js("document.querySelector('[name=payment_method_radio]').value = 'yookassa'")
            self.assertFalse(drivers._likeboom_free_payment(page))
            page.js("document.querySelector('[name=payment_method_radio]').value = 'free'")
            page.js("document.querySelector('#fn_payment_method').after(document.querySelector('#fn_payment_method').cloneNode())")
            self.assertFalse(drivers._likeboom_free_payment(page))

    def smm_svc(self, query='', platform='vk'):
        path = '/free-vk-followers' if platform == 'vk' else '/free-telegram-followers'
        svc = self.svc(path + query)
        svc.update(site_id='smmnakrutka', stype='followers', platform=platform, hold_seconds=1)
        return svc

    def test_smmnakrutka_waits_for_real_modal_and_never_clicks_decoy(self):
        self.srv.smm_events.clear()
        def before_click():
            self.srv.smm_events.append(('risk', {}, time.time()))
        res = drivers.run(self.smm_svc(), 'https://vk.ru/club123',
                          headless=True, deadline_sec=20, on_submit=before_click)
        self.assertEqual(res.verdict, 'ok', res.detail + str(res.steps))
        self.assertTrue(res.submitted)
        events = self.srv.smm_events
        self.assertEqual([e[0] for e in events], ['page', 'risk', 'order'])
        self.assertGreaterEqual(events[1][2] - events[0][2], 4.4)
        self.assertEqual(events[-1][1]['link'], 'https://vk.ru/club123')
        self.assertEqual(events[-1][1]['service'], '179')
        self.assertNotIn('Email', events[-1][1])  # необязательную почту не выдумываем

    def test_smmnakrutka_dry_run_does_not_send_or_dismiss_order_modal(self):
        self.srv.smm_events.clear()
        res = drivers.run(self.smm_svc('?fast=1'), 'https://vk.ru/club123',
                          dry_run=True, headless=True, deadline_sec=20,
                          on_submit=lambda: self.fail('dry-run не должен отправлять'))
        self.assertEqual(res.verdict, 'ok', res.detail + str(res.steps))
        self.assertFalse(res.submitted)
        self.assertEqual([e[0] for e in self.srv.smm_events], ['page'])

    def test_smmnakrutka_rejects_wrong_service_and_paid_form_without_click(self):
        for case in ('wrong-service', 'paid'):
            with self.subTest(case=case):
                self.srv.smm_events.clear()
                res = drivers.run(self.smm_svc('?fast=1&case=' + case), 'https://vk.ru/club123',
                                  headless=True, deadline_sec=20,
                                  on_submit=lambda: self.fail('потенциально неверный заказ запрещён'))
                self.assertEqual(res.verdict, 'need_human', res.detail + str(res.steps))
                self.assertFalse(res.submitted)
                self.assertEqual([e[0] for e in self.srv.smm_events], ['page'])

    def test_smmnakrutka_rejects_unconfirmed_telegram_service_code(self):
        self.srv.smm_events.clear()
        res = drivers.run(self.smm_svc('?fast=1', platform='telegram'), 'https://t.me/testchannel',
                          headless=True, deadline_sec=20,
                          on_submit=lambda: self.fail('код другой услуги запрещён'))
        self.assertEqual(res.verdict, 'need_human', res.detail + str(res.steps))
        self.assertIn('код услуги', res.detail)
        self.assertEqual([e[0] for e in self.srv.smm_events], ['page'])

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
        cls.srv.server_close()

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
            srv.server_close()


@unittest.skipUnless(browser_ready(), "нет установленного браузерного движка")
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
        cls.srv.server_close()

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
        cls.srv.server_close()

    def setUp(self):
        db.set_setting("captcha_autosolve", "1")
        db.set_setting("captcha_ocr", "1")

    def svc(self, path: str) -> dict:
        return {"id": "fake-vk-followers", "site_id": "fake", "site_name": "Стенд",
                "label": "Подписчики ВК", "url": self.base + path, "amount": 10,
                "platform": "vk", "stype": "followers", "hold_seconds": 10}

    @unittest.skipUnless(browser_ready(), "нет установленного браузерного движка")
    def test_turnstile_checkbox_solved(self):
        """Turnstile: чекбокс внутри iframe. Робот жмёт его мышью и проходит заказ."""
        res = drivers.run(self.svc("/turnstile"), "https://vk.com/club243159999",
                          headless=True, allow_captcha_wait=True, deadline_sec=120)
        self.assertEqual(res.verdict, "ok", f"{res.verdict}: {res.detail} / {res.steps}")
        self.assertTrue(any("капча решена" in st for st in res.steps), res.steps)

    @unittest.skipUnless(browser_ready(), "нет установленного браузерного движка")
    @unittest.skipUnless(captcha._have("ddddocr"), "ddddocr is optional")
    def test_image_captcha_solved(self):
        """Текстовая капча с картинки: ddddocr распознаёт код, робот его вписывает."""
        res = drivers.run(self.svc("/imgcaptcha"), "https://vk.com/club243159999",
                          headless=True, allow_captcha_wait=True, deadline_sec=120)
        self.assertEqual(res.verdict, "ok", f"{res.verdict}: {res.detail} / {res.steps}")
        self.assertTrue(any("распознал" in st for st in res.steps), res.steps)

    @unittest.skipUnless(browser_ready(), "нет установленного браузерного движка")
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

    @unittest.skipUnless(browser_ready(), "нет установленного браузерного движка")
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
