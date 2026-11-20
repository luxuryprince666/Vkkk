# -*- coding: utf-8 -*-
"""Offline tests only: no actual Boost-Like or VK requests."""
import json
import unittest

from core import stealth
from tools import boost_like_trial as trial


BASE = 'https://boost-like.ru'
START = BASE + '/api/v1/free/start'
COMPLETE = BASE + '/api/v1/free/complete'
HEARTBEAT = BASE + '/api/v1/free/heartbeat'
STATUS = BASE + '/api/v1/free/status?token=secret-test-token'
START_BODY = json.dumps({'link': trial.TARGET, 'service_slug': trial.SERVICE_SLUG})
TOKEN_BODY = json.dumps({'token': 'secret-test-token'})


class FenceTests(unittest.TestCase):
    def setUp(self):
        self.fence = trial.TrialFence()

    def test_loading_allows_only_scoped_readonly_resources(self):
        for url in (trial.PAGE_URL, BASE + trial.SERVICE_CHECK, BASE + '/api/v1/free/check',
                    BASE + '/_nuxt/CCrX1cDL.js', BASE + '/_nuxt/index.css',
                    BASE + '/_nuxt/builds/meta/abcdef12-3456-7890-abcd-ef1234567890.json',
                    BASE + '/api/v1/free/activity?limit=5'):
            self.assertTrue(self.fence._allowed('GET', url, None), url)
        for url in (BASE + '/api/v1/free/start', BASE + '/shop/order/buy',
                    BASE + '/_nuxt/CCrX1cDL.js?link=club243396555',
                    BASE + '/api/v1/free/check?link=club243396555',
                    'https://boost-like.ru.evil.test' + trial.PAGE_PATH,
                    'http://boost-like.ru' + trial.PAGE_PATH,
                    'https://user:password@boost-like.ru' + trial.PAGE_PATH):
            self.assertFalse(self.fence._allowed('GET', url, None), url)
        self.assertFalse(self.fence._allowed('POST', START, START_BODY))
        self.assertFalse(self.fence._allowed('POST', COMPLETE, TOKEN_BODY))

    def test_one_start_one_completion_and_no_paid_or_bonus_requests(self):
        f = self.fence
        f.begin_input()
        self.assertFalse(f._allowed('POST', START, START_BODY))
        f.arm_start()
        self.assertFalse(f._allowed('POST', START, json.dumps({'link': 'https://vk.com/club999',
                                                               'service_slug': trial.SERVICE_SLUG})))
        self.assertFalse(f._allowed('POST', START, START_BODY[:-1] + ', "price": 1}'))
        self.assertTrue(f._allowed('POST', START, START_BODY))
        self.assertFalse(f._allowed('POST', START, START_BODY))
        self.assertFalse(f._allowed('POST', HEARTBEAT, TOKEN_BODY))
        self.assertFalse(f._allowed('GET', STATUS, None))
        f.set_token('secret-test-token')
        self.assertTrue(f._allowed('POST', HEARTBEAT, TOKEN_BODY))
        self.assertTrue(f._allowed('GET', STATUS, None))
        self.assertFalse(f._allowed('GET', STATUS + '&link=club243396555', None))
        self.assertFalse(f._allowed('POST', BASE + '/api/v1/free/bonus', TOKEN_BODY))
        self.assertFalse(f._allowed('POST', BASE + '/shop/order/buy', TOKEN_BODY))
        f.arm_complete()
        self.assertFalse(f._allowed('POST', COMPLETE, json.dumps({'token': 'another-token'})))
        self.assertTrue(f._allowed('POST', COMPLETE, TOKEN_BODY))
        self.assertFalse(f._allowed('POST', COMPLETE, TOKEN_BODY))
        self.assertFalse(f._allowed('POST', HEARTBEAT, TOKEN_BODY))

    def test_duplicate_json_keys_and_wrong_target_are_rejected(self):
        f = self.fence
        f.begin_input(); f.arm_start()
        duplicate = ('{"link":"' + trial.TARGET + '","link":"' + trial.TARGET +
                     '","service_slug":"' + trial.SERVICE_SLUG + '"}')
        self.assertFalse(f._allowed('POST', START, duplicate))
        self.assertFalse(f.start_seen)
        with self.assertRaises(ValueError):
            trial.TrialFence('https://vk.com/club999')

    def test_blocked_early_completion_prevents_final_click(self):
        f = self.fence
        f.begin_input(); f.arm_start()
        self.assertTrue(f._allowed('POST', START, START_BODY))
        f.set_token('secret-test-token')
        self.assertFalse(f._allowed('POST', COMPLETE, TOKEN_BODY))
        self.assertTrue(f.early_complete_blocked)
        with self.assertRaises(RuntimeError):
            f.arm_complete()

    def test_blocked_input_post_stops_start(self):
        class Request:
            method, url, post_data = 'POST', BASE + '/autostart', START_BODY
        class Route:
            request = Request()
            aborted = False
            def continue_(self):
                raise AssertionError('unexpected request was permitted')
            def abort(self):
                self.aborted = True
        f = self.fence
        f.begin_input()
        route = Route()
        f._route(route)
        self.assertTrue(route.aborted)
        self.assertTrue(f.input_violation)
        with self.assertRaises(RuntimeError):
            f.arm_start()


def browser_ready():
    try:
        return any(b['id'] == 'playwright' and b['ready'] for b in stealth.backend_status()['backends'])
    except Exception:
        return False


HTML = '''<!doctype html><html><body>
<div class="free-form"><div class="free-form__card">
  <h1>VK Бесплатно подписчики ВК</h1><span>+10 бесплатно</span>
  <span>Осталось 2 попытки</span>
  <input class="free-form__input" type="url" placeholder="Вставьте ссылку">
  <button class="free-form__submit" type="button">Получить</button>
</div></div>
<form id="paid"><input type="url"><button>Добавить в корзину</button></form>
</body></html>'''


@unittest.skipUnless(browser_ready(), 'Playwright/Chromium unavailable')
class TrialDomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = stealth.open_page(headless=True, prefer='playwright')
        _, cls.page = cls.ctx.__enter__()
        cls.raw = cls.page._p
        cls.raw.route(trial.PAGE_URL, lambda route: route.fulfill(
            status=200, content_type='text/html; charset=utf-8', body=HTML))

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def setUp(self):
        self.raw.goto(trial.PAGE_URL)

    def test_initial_card_guard_excludes_paid_form_and_checks_exact_value(self):
        self.assertTrue(trial.initial_state(self.raw)['valid'])
        self.raw.locator('.free-form__input').fill(trial.TARGET)
        self.assertTrue(trial.initial_state(self.raw, trial.TARGET)['valid'])
        self.assertFalse(trial.initial_state(self.raw)['valid'])
        self.raw.locator('.free-form__submit').evaluate("e => e.closest('.free-form').appendChild(document.createElement('input'))")
        self.assertFalse(trial.initial_state(self.raw, trial.TARGET)['valid'])

    def test_initial_rejects_no_attempts_and_paid_form(self):
        self.raw.locator('.free-form').evaluate("e => e.innerHTML=e.innerHTML.replace('Осталось 2', 'Осталось 0')")
        self.assertFalse(trial.initial_state(self.raw)['valid'])
        self.raw.goto(trial.PAGE_URL)
        self.raw.locator('.free-form__card').evaluate("e => {const f=document.createElement('form'); e.appendChild(f); f.appendChild(e.querySelector('input')); f.appendChild(e.querySelector('button'));}")
        self.assertFalse(trial.initial_state(self.raw)['valid'])

    def test_session_button_must_be_ready_and_scoped(self):
        self.raw.locator('.free-form').evaluate("e => e.innerHTML='<span class=free-session__timer-value>00:03</span><button class=free-session__complete-btn disabled>Ожидание</button><button class=free-session__bonus-btn>Получить сейчас</button>'")
        state = trial.session_state(self.raw)
        self.assertEqual(state['stage'], 'session')
        self.assertFalse(state['ready'])
        self.raw.locator('.free-session__complete-btn').evaluate("e => {e.disabled=false;e.classList.add('is-ready');e.innerText='Получить'}")
        self.assertFalse(trial.session_state(self.raw)['ready'])
        self.raw.locator('.free-session__timer-value').evaluate("e => e.innerText='00:00'")
        self.assertTrue(trial.session_state(self.raw)['ready'])
        self.raw.locator('.free-form').evaluate("e => e.insertAdjacentHTML('beforeend','<button class=free-session__complete-btn>Получить</button>')")
        self.assertEqual(trial.session_state(self.raw)['stage'], 'other')
