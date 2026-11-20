#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One narrowly scoped Boost-Like browser attempt; caller supplies VK samples.

The caller must quarantine prior possible orders and must observe the VK counter
for a full window after this function returns. `save` must durably publish the
state before any possibly changing action. A recorded action is a risk marker,
not proof a request reached the server or that delivery happened.
"""
from __future__ import annotations

import contextlib
import hashlib
import re
import time
from urllib.parse import urlsplit

from tools import boost_like_trial as trial

MODULE = '/_nuxt/CCrX1cDL.js'


def safe_message(value: object) -> str:
    text = ' '.join(str(value or '').split())[:250]
    text = re.sub(r'https?://\S+', '[адрес скрыт]', text, flags=re.I)
    text = re.sub(r'[\w.+-]+@[\w.-]+', '[email скрыт]', text)
    text = re.sub(r'[A-Za-z0-9_-]{32,}', '[данные скрыты]', text)
    return text[:200]


def provider_confirms(text: str) -> bool:
    text = (text or '').lower()
    if re.search(r'\bне\s+(?:был\s+)?(?:оформлен|создан|принят)\b|не удалось|отклон[её]н', text):
        return False
    return bool(re.search(r'\bзаказ\s+(?:успешно\s+)?(?:оформлен|создан|принят)\b', text))


def add_sample(state: dict, read_count, point: str) -> int:
    count, route, source = read_count()
    if type(count) is not int or count < 0:
        raise ValueError('Нет точного счётчика')
    state.setdefault('samples', []).append({'observed_at': time.time(), 'count': count,
                                            'source': source, 'route': route, 'point': point})
    state['counter_error'] = None
    return count


def run_trial(page, state: dict, read_count, save, cancelled, *, expected_module_sha256: str,
              fence_factory=trial.TrialFence, load_wait_ms: int = 5000,
              start_wait_sec: float = 25, session_wait_sec: float = 1800,
              result_wait_sec: float = 20, poll_sec: float = 30) -> None:
    """Run one trial, or stop without a click on any unexpected state.

    `page` is an isolated Playwright page, and must not have persisted cookies
    or storage. Only exact free-trial network requests are released. The UI is
    kept open through its own timer. No optional bonus or paid button is used.
    The caller should catch errors and still observe after any risk marker.
    """
    raw = page._p
    fence = fence_factory()
    fence.attach(raw)
    start = state.setdefault('start', {'possible_at': None, 'request_released': False, 'http_status': None})
    complete = state.setdefault('complete', {'possible_at': None, 'request_released': False, 'http_status': None})

    def response(resp):
        try:
            u = urlsplit(resp.url)
            if u.scheme != 'https' or u.hostname != trial.HOST:
                return
            if u.path == '/api/v1/free/start':
                start['http_status'] = int(resp.status)
            elif u.path == '/api/v1/free/complete':
                complete['http_status'] = int(resp.status)
        except Exception:
            pass  # Never log a URL, cookie, token or response body.

    def site_message(value):
        text = str(value or '')
        if fence.token:
            text = text.replace(fence.token, '[данные скрыты]')
        return safe_message(text)

    raw.on('response', response)
    try:
        state['phase'] = 'осмотр бесплатной карточки'
        save(state)
        page.goto(trial.PAGE_URL, wait=load_wait_ms)
        loaded = raw.evaluate('''path => performance.getEntriesByType('resource').some(e => {
          try { const u=new URL(e.name); return u.origin===location.origin && u.pathname===path; }
          catch (_) {return false;}
        })''', MODULE)
        module = raw.evaluate('''async path => {
          const r=await fetch(path, {cache:'no-store'});
          if(!r.ok) throw new Error('public trial module unavailable');
          return await r.text();
        }''', MODULE)
        if not loaded or hashlib.sha256(module.encode('utf-8')).hexdigest() != expected_module_sha256:
            state['reason'] = 'Публичный код бесплатной карточки изменился; ссылка не вводилась'
            return
        card = trial.initial_state(raw)
        state['initial_card'] = card
        if not card.get('valid'):
            state['reason'] = 'Бесплатная карточка, число попыток или поле не подтверждены; ссылка не вводилась'
            return
        if cancelled():
            state['reason'] = 'Остановлено до ввода ссылки'
            return

        # A link input can itself fire JS. Snapshot the fresh baseline and the
        # possible-action marker synchronously before putting in any target.
        baseline = add_sample(state, read_count, 'перед вводом')
        state['baseline_count'] = baseline
        start['possible_at'] = time.time()
        state['phase'] = 'возможное действие: ввод ссылки'
        save(state)
        if cancelled():
            state['reason'] = 'Остановлено до ввода; риск отмечен консервативно'
            return
        fence.begin_input()
        raw.locator('.free-form__input').fill(trial.TARGET, timeout=5000)
        page.wait(700)
        if fence.input_violation:
            state['reason'] = 'При вводе заблокирован потенциальный сетевой запрос; кнопка не нажималась'
            return
        if not trial.initial_state(raw, trial.TARGET).get('valid'):
            state['reason'] = 'Бесплатная карточка изменилась после ввода; кнопка не нажималась'
            return
        if cancelled():
            state['reason'] = 'Остановлено до запуска таймера'
            return
        fence.arm_start()
        raw.locator('.free-form__submit').click(timeout=5000)
        start['request_released'] = fence.start_seen
        state['phase'] = 'ожидается ответ на запуск бесплатной сессии'
        save(state)

        end = time.monotonic() + max(1, start_wait_sec)
        session = None
        while time.monotonic() < end and not cancelled():
            page.wait(500)
            current = trial.session_state(raw)
            if current.get('stage') == 'session':
                session = current
                break
            if current.get('stage') == 'result':
                state['site_response'] = site_message(current.get('text'))
                state['reason'] = 'Страница показала итог до явного завершения таймера'
                return
            if current.get('error'):
                state['site_response'] = site_message(current['error'])
                state['reason'] = 'Бесплатная сессия не открылась; повтор не выполняется'
                return
        if not session or not fence.start_seen:
            state['reason'] = 'Подтверждённая бесплатная сессия не появилась; повтор не выполняется'
            return
        # Kept in memory only, never returned or written to the public check.
        token = raw.evaluate("() => localStorage.getItem('free_session_token')")
        fence.set_token(token)
        token = None
        state['phase'] = 'ожидание таймера бесплатной сессии'
        state['timer'] = session.get('timer')
        save(state)
        end = time.monotonic() + max(1, session_wait_sec)
        next_poll = time.monotonic() + max(.1, poll_sec)
        while time.monotonic() < end and not cancelled():
            if time.monotonic() >= next_poll:
                try:
                    add_sample(state, read_count, 'во время таймера')
                except Exception as exc:
                    state['counter_error'] = 'Счётчик временно недоступен: ' + type(exc).__name__
                save(state)
                next_poll = time.monotonic() + max(.1, poll_sec)
            current = trial.session_state(raw)
            state['timer'] = current.get('timer')
            if current.get('stage') == 'result':
                state['site_response'] = site_message(current.get('text'))
                state['reason'] = 'Страница показала итог до явного завершения таймера'
                return
            if current.get('stage') != 'session':
                state['site_response'] = site_message(current.get('error'))
                state['reason'] = 'Бесплатная сессия изменилась; повтор не выполняется'
                return
            if current.get('ready'):
                if fence.early_complete_blocked:
                    state['reason'] = 'Ранее был заблокирован неожиданный запрос завершения; клик запрещён'
                    return
                try:
                    complete['baseline_count'] = add_sample(state, read_count, 'перед завершением')
                except Exception as exc:
                    state['counter_error'] = 'Счётчик недоступен перед завершением: ' + type(exc).__name__
                    state['reason'] = 'Завершение не нажато без свежего счётчика'
                    return
                complete['possible_at'] = time.time()
                state['phase'] = 'возможное действие: завершение бесплатной сессии'
                save(state)
                if cancelled():
                    state['reason'] = 'Остановлено до завершения; риск отмечен консервативно'
                    return
                # Re-check after the potentially slow fresh VK read and snapshot.
                if not trial.session_state(raw).get('ready'):
                    state['reason'] = 'Кнопка завершения изменилась; повтор не выполняется'
                    return
                fence.arm_complete()
                raw.locator('.free-session__complete-btn').click(timeout=5000)
                complete['request_released'] = fence.complete_seen
                state['phase'] = 'ожидание ответа площадки'
                save(state)
                result_end = time.monotonic() + max(1, result_wait_sec)
                while time.monotonic() < result_end and not cancelled():
                    page.wait(500)
                    result = trial.session_state(raw)
                    if result.get('stage') == 'result':
                        text = site_message(result.get('text'))
                        state['site_response'] = text
                        state['provider_claimed_order'] = bool(fence.complete_seen and provider_confirms(text))
                        return
                    if result.get('error'):
                        state['site_response'] = site_message(result['error'])
                        return
                state['reason'] = 'Результат завершения не распознан; повтор запрещён'
                return
            page.wait(500)
        state['reason'] = ('Остановлено пользователем; сессия могла начаться' if cancelled() else
                           'Таймер не завершился в допустимое время; повтор запрещён')
    finally:
        start['request_released'] = bool(fence.start_seen)
        complete['request_released'] = bool(fence.complete_seen)
        # Never remove the network fence while a live page/popup might still
        # issue a request. Close the whole isolated context first.
        with contextlib.suppress(Exception):
            raw.context.close()
        fence.token = None
