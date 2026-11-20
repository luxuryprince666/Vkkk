# -*- coding: utf-8 -*-
"""End-to-end trial sequencing against a fully intercepted offline page."""
import hashlib
import json
import unittest
from urllib.parse import urlsplit

from core import stealth
from tools import boost_like_trial as trial
from tools import boost_trial_attempt as attempt


JS = '/* offline trial module; no remote network */'
TOKEN = 'secret-test-token'
HTML = '''<!doctype html><html><head><script src="/_nuxt/CCrX1cDL.js"></script></head><body>
<div class="free-form"><div class="free-form__card">
<h1>VK Бесплатно подписчики ВК</h1><span>+10 бесплатно</span><span>Осталось 2 попытки</span>
<input class="free-form__input" type="url" placeholder="Вставьте ссылку">
<button class="free-form__submit" type="button">Получить</button></div></div>
<form id="paid"><input><button>Добавить в корзину</button></form>
<script>
const card=document.querySelector('.free-form');
const input=document.querySelector('.free-form__input');
/* AUTOSTART */
document.querySelector('.free-form__submit').onclick=async()=>{
  const r=await fetch('/api/v1/free/start',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({link:input.value,service_slug:'besplatnaya-nakrutka-podpischikov-v-vk'})});
  const v=await r.json(); localStorage.setItem('free_session_token',v.token);
  card.innerHTML='<span class="free-session__timer-value">00:01</span><button class="free-session__complete-btn" disabled>Ожидание</button>';
  const b=card.querySelector('.free-session__complete-btn');
  setTimeout(()=>{b.disabled=false;b.innerText='Получить'},1000);
  b.onclick=async()=>{
    await fetch('/api/v1/free/complete',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({token:localStorage.getItem('free_session_token')})});
    card.innerHTML='<div class="free-result">Заказ оформлен</div>';
  };
};
</script></body></html>'''
AUTO = '''input.addEventListener('input',()=>fetch('/api/v1/free/start',{
  method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({link:input.value,service_slug:'besplatnaya-nakrutka-podpischikov-v-vk'})
}).catch(()=>{}));'''


class ConfirmationTests(unittest.TestCase):
    def test_never_treat_negative_or_future_as_confirmed(self):
        self.assertTrue(attempt.provider_confirms('Ваш заказ успешно оформлен'))
        self.assertFalse(attempt.provider_confirms('Заказ не оформлен'))
        self.assertFalse(attempt.provider_confirms('Заказ будет создан позже'))
        self.assertFalse(attempt.provider_confirms('Заказ оформлен, но затем отклонён'))
        self.assertFalse(attempt.provider_confirms('Не удалось создать заказ'))


def browser_ready():
    try:
        return any(b['id'] == 'playwright' and b['ready'] for b in stealth.backend_status()['backends'])
    except Exception:
        return False


class OfflineFence(trial.TrialFence):
    fixture = HTML
    instance = None

    def __init__(self):
        super().__init__()
        type(self).instance = self
        self.released_paths = []

    def _route(self, route):
        request = route.request
        if not self._allowed(request.method, request.url, request.post_data):
            self.blocked += 1
            if self.stage == 'input' and request.method not in ('GET', 'HEAD'):
                self.input_violation = True
            route.abort()
            return
        self.allowed += 1
        path = urlsplit(request.url).path
        self.released_paths.append((request.method, path))
        if path == trial.PAGE_PATH:
            route.fulfill(status=200, content_type='text/html; charset=utf-8', body=self.fixture)
        elif path == attempt.MODULE:
            route.fulfill(status=200, content_type='application/javascript', body=JS)
        elif path == '/api/v1/free/start':
            route.fulfill(status=200, content_type='application/json', body=json.dumps({'token': TOKEN}))
        elif path == '/api/v1/free/complete':
            route.fulfill(status=200, content_type='application/json', body='{"success":true}')
        else:
            route.fulfill(status=200, content_type='application/json', body='{}')


@unittest.skipUnless(browser_ready(), 'Playwright/Chromium unavailable')
class OfflineAttemptTests(unittest.TestCase):
    def setUp(self):
        OfflineFence.fixture = HTML
        self.state = {'samples': [], 'provider_claimed_order': False}
        self.snapshots = []

    def run_page(self, *, digest=None, read=None, save_override=None):
        def save(state):
            self.snapshots.append(json.loads(json.dumps(state)))
            if save_override:
                save_override(state)
        with stealth.open_page(headless=True, prefer='playwright') as (_, page):
            attempt.run_trial(page, self.state, read or (lambda: (1, 'direct', 'mock VK')), save, lambda: False,
                expected_module_sha256=digest or hashlib.sha256(JS.encode()).hexdigest(),
                fence_factory=OfflineFence, load_wait_ms=100, start_wait_sec=3,
                session_wait_sec=4, result_wait_sec=3, poll_sec=.3)

    def test_waits_for_session_and_records_before_each_free_request(self):
        self.run_page()
        f = OfflineFence.instance
        self.assertTrue(self.state['start']['request_released'])
        self.assertTrue(self.state['complete']['request_released'])
        self.assertTrue(self.state['provider_claimed_order'])
        self.assertEqual(sum(p == ('POST', '/api/v1/free/start') for p in f.released_paths), 1)
        self.assertEqual(sum(p == ('POST', '/api/v1/free/complete') for p in f.released_paths), 1)
        self.assertFalse(any('buy' in p for _, p in f.released_paths))
        self.assertTrue(any(x['start']['possible_at'] and not x['start']['request_released']
                            for x in self.snapshots if 'start' in x))
        self.assertTrue(any(x['complete']['possible_at'] and not x['complete']['request_released']
                            for x in self.snapshots if 'complete' in x))
        self.assertNotIn(TOKEN, json.dumps(self.state, ensure_ascii=False))
        self.assertNotIn(TOKEN, json.dumps(self.snapshots, ensure_ascii=False))

    def test_auto_post_during_input_is_blocked_and_button_not_clicked(self):
        OfflineFence.fixture = HTML.replace('/* AUTOSTART */', AUTO)
        self.run_page()
        f = OfflineFence.instance
        self.assertTrue(f.input_violation)
        self.assertFalse(self.state['start']['request_released'])
        self.assertFalse(self.state['complete']['request_released'])
        self.assertIn('заблокирован', self.state['reason'])
        self.assertFalse(any(method == 'POST' for method, _ in f.released_paths))

    def test_changed_module_stops_before_target_or_request(self):
        self.run_page(digest='0' * 64)
        self.assertIsNone(self.state['start']['possible_at'])
        self.assertIn('изменился', self.state['reason'])
        self.assertFalse(any(method == 'POST' for method, _ in OfflineFence.instance.released_paths))

    def test_no_fresh_counter_blocks_completion_even_after_session_started(self):
        calls = [0]
        def read():
            calls[0] += 1
            if calls[0] > 1:
                raise OSError('offline')
            return 1, 'direct', 'mock VK'
        self.run_page(read=read)
        self.assertTrue(self.state['start']['request_released'])
        self.assertFalse(self.state['complete']['request_released'])
        self.assertIn('счётчика', self.state['reason'])

    def test_failed_durable_snapshot_blocks_input_and_any_post(self):
        def fail_before_input(state):
            if state['start']['possible_at'] is not None:
                raise RuntimeError('snapshot failed')
        with self.assertRaises(RuntimeError):
            self.run_page(save_override=fail_before_input)
        self.assertFalse(any(method == 'POST' for method, _ in OfflineFence.instance.released_paths))

    def test_visible_session_token_is_redacted_from_public_snapshot(self):
        OfflineFence.fixture = HTML.replace('Заказ оформлен</div>', 'Заказ оформлен secret-test-token</div>')
        self.run_page()
        self.assertTrue(self.state['provider_claimed_order'])
        self.assertNotIn(TOKEN, json.dumps(self.state, ensure_ascii=False))
        self.assertNotIn(TOKEN, json.dumps(self.snapshots, ensure_ascii=False))
