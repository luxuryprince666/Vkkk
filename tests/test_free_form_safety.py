# -*- coding: utf-8 -*-
"""Browser-only safety checks against offline HTML, never real service orders."""
import unittest
from unittest.mock import patch

from core import drivers, stealth


URL = 'https://mock.test/service'
TARGET = 'https://vk.ru/club123'
HTML = '''<!doctype html><html><body>
<form id="free" action="/order">
  <input id="link" name="link" value="https://vk.ru/club123">
  <input id="payment" type="hidden" name="paymentMethod" value="free">
  <button id="free-order" type="button" onclick="window.clickCount=(window.clickCount||0)+1;
          document.querySelector('#status').textContent='Заказ принят'">Получить 10 подписчиков бесплатно</button>
  <a href="/buy"><button id="upsell">ПОЛУЧИТЬ МАКСИМУМ!</button></a>
</form>
<div id="status"></div>
<form id="paid"><input id="paid-link" name="link"><button id="paid-order">Заказать</button></form>
<form id="review"><input name="text"><button id="review-send">Отправить отзыв</button></form>
</body></html>'''
PRSKILL_URL = 'https://prskill.ru/services/vk-vstupivshie#giveaway-form'
PRSKILL_HTML = '''<!doctype html><html><body>
<div id="giveaway-form"></div>
<div class="form"><h2>Накрутка бесплатно - 10 Подписчиков ВК</h2>
  <input placeholder="https://vk.com/***">
  <input type="hidden" name="cf-turnstile-response" value="">
  <button type="button" onclick="window.clickCount=(window.clickCount||0)+1;
        document.querySelector('#status').textContent='Заказ принят'">Заказать</button>
  <div id="status"></div>
</div>
<div><h2>Платный заказ</h2><button>Создать заказ</button></div>
</body></html>'''


def browser_ready():
    try:
        return any(b['id'] == 'playwright' and b['ready'] for b in stealth.backend_status()['backends'])
    except Exception:
        return False


@unittest.skipUnless(browser_ready(), 'Playwright/Chromium недоступен')
class FreeFormBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = stealth.open_page(headless=True, prefer='playwright')
        backend, cls.page = cls.context.__enter__()
        if backend != 'playwright':
            raise RuntimeError('Нужен Playwright для локального теста')
        cls.html = HTML
        cls.prskill_html = PRSKILL_HTML
        cls.page._p.route('https://mock.test/**', lambda route: route.fulfill(
            status=200, content_type='text/html; charset=utf-8', body=cls.html))
        cls.page._p.route('https://prskill.ru/**', lambda route: route.fulfill(
            status=200, content_type='text/html; charset=utf-8', body=cls.prskill_html))

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None)

    def setUp(self):
        self.__class__.html = HTML
        self.__class__.prskill_html = PRSKILL_HTML
        self.page._p.goto(URL)

    def test_scope_excludes_paid_form_review_and_link_nested_button(self):
        state = drivers.discover(self.page)
        scoped = drivers.same_form_state(self.page, state, '#link')
        self.assertIsNotNone(scoped)
        self.assertEqual([b['sel'] for b in scoped['buttons']], ['#free-order'])
        self.assertEqual(drivers.pick_submit_button(scoped['buttons'])['sel'], '#free-order')

    def test_guard_accepts_only_matching_explicit_free_form(self):
        guard = lambda field, button, value=TARGET, url=URL: drivers.free_form_guard(
            self.page, field, button, value, url)
        self.assertTrue(guard('#link', '#free-order'))
        self.assertFalse(guard('#link', '#review-send'))
        self.assertFalse(guard('#link', '#upsell'))
        self.assertFalse(guard('#link', '#free-order', 'https://vk.ru/club456'))
        self.assertFalse(guard('#link', '#free-order', TARGET, 'https://other.test/service'))
        self.page._p.locator('#payment').evaluate("e => e.value = 'yookassa'")
        self.assertFalse(guard('#link', '#free-order'))
        self.page._p.locator('#payment').evaluate("e => e.value = 'free'")
        self.page._p.locator('#free-order').evaluate("e => e.disabled = true")
        self.assertFalse(guard('#link', '#free-order'))

    def test_guard_rejects_paid_or_ambiguous_action(self):
        guard = lambda: drivers.free_form_guard(self.page, '#link', '#free-order', TARGET, URL)
        self.page._p.locator('#free').evaluate("e => e.setAttribute('action','https://elsewhere.test/buy')")
        self.assertFalse(guard())
        self.page._p.locator('#free').evaluate("e => e.setAttribute('action','/order')")
        self.page._p.locator('#free-order').evaluate("e => e.innerText = 'Заказать'")
        self.assertFalse(guard())
        self.page._p.locator('#free-order').evaluate("e => e.innerText = 'Заказать бесплатно'")
        self.page._p.locator('#free').evaluate("e => e.insertAdjacentHTML('beforeend','К оплате: 12 руб.')")
        self.assertFalse(guard())

    def test_prskill_requires_site_provided_token_before_any_click(self):
        self.page._p.goto(PRSKILL_URL)
        with patch.object(self.page, 'wait'):
            result = drivers._run_prskill(self.page, {'url': PRSKILL_URL, 'amount': 10},
                                          'https://vk.com/club123', [], dry_run=False,
                                          on_submit=lambda: self.fail('нельзя отправлять без токена'))
        self.assertEqual(result.verdict, 'captcha')
        self.assertEqual(self.page.js('window.clickCount || 0'), 0)

    def test_prskill_uses_only_free_card_and_records_risk_before_one_click(self):
        self.page._p.goto(PRSKILL_URL)
        self.page._p.locator('input[name="cf-turnstile-response"]').evaluate(
            "e => e.value = 'synthetic-token-for-offline-test'")
        events = []
        result = drivers._run_prskill(self.page, {'url': PRSKILL_URL, 'amount': 10},
                                      'https://vk.com/club123', [], dry_run=False,
                                      on_submit=lambda: events.append(self.page.js('window.clickCount || 0')))
        self.assertEqual(events, [0])
        self.assertEqual(self.page.js('window.clickCount || 0'), 1)
        self.assertEqual(result.verdict, 'ok')

    def test_prskill_never_clicks_if_challenge_changes_during_baseline(self):
        self.page._p.goto(PRSKILL_URL)
        token = self.page._p.locator('input[name="cf-turnstile-response"]')
        token.evaluate("e => e.value = 'synthetic-token-for-offline-test'")
        result = drivers._run_prskill(self.page, {'url': PRSKILL_URL, 'amount': 10},
                                      'https://vk.com/club123', [], dry_run=False,
                                      on_submit=lambda: token.evaluate("e => e.value = ''"))
        self.assertTrue(result.submitted)  # recorded risk remains conservative
        self.assertEqual(self.page.js('window.clickCount || 0'), 0)

    def run_fake_driver(self, callback):
        from contextlib import contextmanager
        @contextmanager
        def browser(*args, **kwargs):
            yield 'playwright', self.page
        svc = {'site_id': 'fake', 'url': URL, 'platform': 'vk', 'stype': 'followers', 'amount': 10}
        with patch.object(drivers, '_open_smart', browser), \
             patch.object(drivers.profiles, 'get_profile', return_value={}), \
             patch.object(drivers, '_memory', return_value={}), \
             patch.object(drivers, 'remember'), patch.object(drivers, '_shot', return_value=None):
            return drivers.run(svc, TARGET, on_submit=callback, normalize_target=False, deadline_sec=20)

    def test_generic_driver_records_risk_before_single_free_click(self):
        events = []
        result = self.run_fake_driver(lambda: events.append(self.page.js('window.clickCount || 0')))
        self.assertEqual(events, [0])
        self.assertEqual(self.page.js('window.clickCount || 0'), 1)
        self.assertEqual(result.verdict, 'ok')

    def test_generic_driver_never_clicks_when_free_payment_is_not_proven(self):
        self.__class__.html = HTML.replace('name="paymentMethod" value="free"',
                                           'name="paymentMethod" value="yookassa"')
        result = self.run_fake_driver(lambda: self.fail('baseline callback запрещён для платной формы'))
        self.assertEqual(result.verdict, 'need_human')
        self.assertEqual(self.page.js('window.clickCount || 0'), 0)


class FreeFormHeuristicTests(unittest.TestCase):
    def test_never_choose_review_or_upsell_over_free_button(self):
        buttons = [{'sel': '#free', 'text': 'Получить 10 подписчиков бесплатно', 'nearForm': True},
                   {'sel': '#review', 'text': 'Отправить отзыв', 'nearForm': True},
                   {'sel': '#upsell', 'text': 'ПОЛУЧИТЬ ЕЩЁ БОЛЬШЕ', 'nearForm': True}]
        self.assertEqual(drivers.pick_submit_button(buttons)['sel'], '#free')

    def test_known_testknop_timer_can_be_selected(self):
        button = {'sel': '#get_free_start', 'text': 'ДОСТУПНО ЧЕРЕЗ 6:59',
                  'attrs': 'get_free_start', 'nearForm': True}
        self.assertEqual(drivers.pick_submit_button([button])['sel'], '#get_free_start')


if __name__ == '__main__':
    unittest.main()
