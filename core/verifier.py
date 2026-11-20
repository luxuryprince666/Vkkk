# -*- coding: utf-8 -*-
"""Последовательная проверка прироста подписчиков, не путать с приёмом заявки.

Каждый заказ получает собственный исходный счётчик. Следующий сайт запускается
только после 5 минут без роста или двух измерений роста и периода стабилизации.
Если счётчик недоступен или результат отправки неоднозначен, мы не выдумываем
числа и не повторяем заказ вслепую. Все измерения и попытки сохраняются в SQLite.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

from core import db, delivery, drivers, engine, executor, network

ATTRIBUTION_NOTE = (
    'Показан чистый рост публичного счётчика за время наблюдения. '
    'Сам по себе счётчик не доказывает источник подписок: на него могут влиять '
    'органический рост, отписки и запоздавшие предыдущие заказы.'
)

_RUNS: dict[int, threading.Event] = {}
_RUNS_LOCK = threading.Lock()
MAX_ROUTES = 3
MAX_ATTEMPTS = 5
MAX_GROWTH_OBSERVATION_SEC = 7200


@dataclass
class Observation:
    status: str                       # working | not_working | skipped | unverified | rejected | stopped
    final_count: int | None
    added_count: int | None
    time_to_growth_sec: float | None = None
    reason: str = ''


def observe_growth(
    read_count: Callable[[], int], baseline: int, started: float, *,
    timeout_sec: float = 300, poll_sec: float = 15, settle_sec: float = 90,
    max_observe_sec: float = MAX_GROWTH_OBSERVATION_SEC,
    clock: Callable[[], float] = time.monotonic,
    wait: Callable[[float], bool] | None = None,
    on_sample: Callable[[int, float], None] | None = None,
    rejected: Callable[[], str | None] | None = None,
    delayed_until: Callable[[], float | None] | None = None,
) -> Observation:
    """Независимый наблюдатель. Время первого роста — время *обнаружения*.

    Опрос продолжается не меньше заданного окна (обычно 5 минут), даже если
    рост стабилизировался раньше. Рост должен обнаружиться в пределах окна,
    подтвердиться вторым положительным измерением и затем не меняться settle_sec.
    Сбой счётчика не превращается в нулевой прирост. delayed_until удерживает
    наблюдение для сайтов с автоматической отправкой после длинного таймера.
    """
    if isinstance(baseline, bool) or not isinstance(baseline, int) or baseline < 0:
        raise ValueError('Исходный счётчик должен быть точным неотрицательным целым')
    timeout_sec = max(0.001, float(timeout_sec))
    poll_sec = max(0.001, float(poll_sec))
    settle_sec = max(0.0, float(settle_sec))
    max_observe_sec = max(timeout_sec + settle_sec + 2 * poll_sec, float(max_observe_sec))
    wait = wait or (lambda seconds: (time.sleep(seconds) or False))
    deadline = started + timeout_sec
    last_count = baseline
    last_success: float | None = None
    last_change = started
    candidate_at: float | None = None
    hits = 0
    confirmed = False
    last_error = ''
    while True:
        if delayed_until is not None:
            end_of_driver = delayed_until()
            deadline = (max(started, end_of_driver) + timeout_sec
                        if end_of_driver is not None else float('inf'))
        if rejected:
            why = rejected()
            if why:
                return Observation('rejected', last_count if last_success is not None else None,
                                   None, reason=why)
        try:
            count = read_count()
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise delivery.CounterError('счётчик не является точным целым числом')
            at = clock()
            last_error = ''
            last_success = at
            if on_sample:
                on_sample(count, at)
            if count != last_count:
                last_count = count
                last_change = at
            if count > baseline:
                if candidate_at is None:
                    candidate_at = at
                    hits = 1
                else:
                    hits += 1
                if candidate_at <= deadline and hits >= 2:
                    confirmed = True
                if confirmed and at >= deadline and at - last_change >= settle_sec:
                    return Observation('working', count, count - baseline,
                                       max(0.0, round(candidate_at - started, 1)))
            else:
                # Однократный скачок или последующие отписки не являются устойчивым приростом.
                candidate_at = None
                hits = 0
                confirmed = False
        except (delivery.CounterError, OSError, ValueError) as exc:
            at = clock()
            last_error = str(exc)[:200]

        if rejected:
            why = rejected()
            if why:
                return Observation('rejected', last_count if last_success is not None else None,
                                   None, reason=why)
        if at - started >= max_observe_sec:
            return Observation('unverified', last_count if last_success is not None else None, None,
                               reason='Рост не стабилизировался за допустимое время наблюдения')
        if confirmed and last_success is not None and at - last_success > max(60, poll_sec * 3):
            return Observation('unverified', last_count, None,
                               reason='Счётчик недоступен во время стабилизации: ' + last_error)
        if at >= deadline and not confirmed:
            # Если первый положительный отсчёт сделан у границы 5 минут, ждём
            # ещё одно измерение: один скачок нельзя считать подтверждением.
            awaiting_confirmation = (candidate_at is not None and candidate_at <= deadline
                                     and hits == 1 and at <= deadline + poll_sec)
            if not awaiting_confirmation:
                if last_success is None or last_success < deadline:
                    return Observation('unverified', last_count if last_success is not None else None, None,
                                       reason='Нет надёжного измерения счётчика к концу 5 минут: ' + last_error)
                if last_count > baseline and candidate_at and candidate_at > deadline:
                    return Observation('unverified', last_count, None,
                                       reason='Первое измерение роста получено после 5 минут; время начала неизвестно')
                return Observation('not_working', last_count, last_count - baseline,
                                   reason='За период ожидания подтверждённого чистого прироста не обнаружено')
        now = clock()
        if not confirmed and now < deadline:
            delay = min(poll_sec, deadline - now)
        else:
            delay = poll_sec
        if wait(max(0.001, delay)):
            return Observation('stopped', last_count if last_success is not None else None, None,
                               reason='Остановлено пользователем; заказ может продолжаться')


def _settings() -> dict:
    s = db.get_settings()
    def bounded(key: str, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(s.get(key, default))))
        except (ValueError, TypeError):
            return default
    return {
        'timeout': bounded('verify_timeout_sec', 300, 300, 3600),
        'poll': bounded('verify_poll_sec', 15, 1, 300),
        'settle': bounded('verify_settle_sec', 90, 1, 1800),
        'proxy_enabled': s.get('proxy_rotation', '0') == '1',
        'manual': s.get('proxy_list', ''),
        'subscription': s.get('proxy_subscription_url', ''),
        'autodetect': s.get('proxy_autodetect_local', '1') == '1',
        'tor': s.get('proxy_use_tor', '0') == '1',
        'nojs': s.get('nojs_enabled', '1') == '1',
        'backend': s.get('browser_backend', 'auto'),
        'headless': s.get('captcha_mode') == 'nocaptcha' or s.get('captcha_browser_visible', '1') != '1',
        'allow_captcha': s.get('captcha_mode', 'all') != 'nocaptcha',
        'captcha_mode': s.get('captcha_mode', 'all'),
        'gap': bounded('global_min_gap_sec', 20, 0, 3600),
        'daily_limit': bounded('daily_total_limit', 0, 0, 100000),
    }


def _routes(cfg: dict) -> list[network.Endpoint]:
    direct = network.Endpoint(id='direct', kind='direct', label='Прямое соединение')
    if not cfg['proxy_enabled']:
        return [direct]
    routes = network.collect_endpoints(manual_raw=cfg['manual'], autodetect_local=cfg['autodetect'],
                                       include_direct=True, include_tor=cfg['tor'])
    if cfg['subscription']:
        sub, note = network.fetch_subscription(cfg['subscription'])
        if note:
            db.log('info', note)
        routes.extend(e for e in sub if e.ready and e.proxy_url)
    out: list[network.Endpoint] = []
    seen = set()
    for route in routes:
        key = route.proxy_url or 'direct'
        if key not in seen:
            seen.add(key)
            out.append(route)
    return (out or [direct])[:MAX_ROUTES]


def _counter(target: dict, routes: list[network.Endpoint]) -> tuple[int, str, str]:
    errors = []
    for route in routes:
        try:
            source: list[str] = []
            value = delivery.read_target_count(target, 'followers', proxy=route.proxy_url,
                                               allow_resolve=True, source_out=source)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise delivery.CounterError('получен неточный счётчик')
            return value, network.mask_proxy_url(route.proxy_url), (source[0] if source else 'источник не указан')
        except (delivery.CounterError, OSError, ValueError) as exc:
            errors.append(f'{route.label}: {_safe_detail(str(exc))}')
    raise delivery.CounterError('; '.join(errors)[:500] or 'Счётчик недоступен')


def _safe_detail(text: str) -> str:
    text = str(text or '').replace('\n', ' ')
    # В исключениях библиотек могут встречаться URL прокси, подписок или Bot API.
    return re.sub(r'(?:https?|socks5h?|vless)://[^\s<>]+', '[адрес скрыт]', text, flags=re.I)[:350]


def _sample(campaign_id: int, result_id: int | None, count: int, source: str, route: str) -> None:
    with db.tx() as conn:
        conn.execute('INSERT INTO campaign_samples(campaign_id,result_id,observed_at,count,source,route) '
                     'VALUES(?,?,?,?,?,?)', (campaign_id, result_id, time.time(), count, source, route))
        conn.execute('UPDATE campaigns SET current_count=? WHERE id=?', (count, campaign_id))


def _update_campaign(campaign_id: int, **values) -> None:
    if not values:
        return
    allowed = {'initial_count', 'current_count', 'total_added', 'services_working', 'services_failed',
               'status', 'current_service', 'current_phase', 'finished_at', 'error_reason'}
    if not set(values) <= allowed:
        raise ValueError('Недопустимое поле кампании')
    with db.tx() as conn:
        conn.execute('UPDATE campaigns SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?',
                     (*values.values(), campaign_id))


def _update_result(result_id: int, **values) -> None:
    if not values:
        return
    allowed = {'baseline_count', 'final_count', 'added_count', 'status', 'time_to_growth_sec',
               'method_used', 'attempts_json', 'failure_reason', 'started_at', 'submitted_at', 'finished_at'}
    if not set(values) <= allowed:
        raise ValueError('Недопустимое поле результата')
    with db.tx() as conn:
        conn.execute('UPDATE campaign_results SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?',
                     (*values.values(), result_id))


def _site_results(conn, platform: str, kind: str, cfg: dict) -> list[dict]:
    """Одна подходящая услуга на сайт; отсутствующие и выключенные видны как пропуски."""
    sites = conn.execute('SELECT * FROM sites ORDER BY rowid').fetchall()
    rows = conn.execute("SELECT * FROM services WHERE platform=? AND stype='followers' ORDER BY rowid", (platform,)).fetchall()
    by_site: dict[str, list[dict]] = {}
    for row in rows:
        item = engine.apply_overrides(dict(row))
        if engine.subject_fits(item.get('subject'), kind):
            by_site.setdefault(item['site_id'], []).append(item)
    result = []
    for site in sites:
        available = by_site.get(site['id'], [])
        available.sort(key=lambda x: (not bool(x['enabled']), not bool(x.get('confirmed'))))
        svc = available[0] if available else None
        reason = ''
        if not site['enabled']:
            reason = 'площадка выключена пользователем'
        elif not svc:
            reason = 'нет подходящей услуги подписчиков для этой ссылки'
        elif not svc['enabled']:
            reason = 'услуга выключена пользователем'
        elif site['mode'] == 'manual':
            reason = 'площадка настроена только для ручного режима'
        elif site['reg_required']:
            reason = 'требуется вход; автоматическая авторизованная сессия не настроена'
        elif site['breaker_until'] > time.time():
            reason = 'площадка временно отключена предохранителем'
        elif not engine.captcha_allowed(svc, cfg['captcha_mode']):
            reason = 'услуга исключена выбранным режимом капчи'
        result.append({'site_id': site['id'], 'site_name': site['name'],
                       'service_id': svc['id'] if svc else '', 'service_label': svc['label'] if svc else '',
                       'service_url': svc['url'] if svc else (site['free_page'] or ''),
                       'status': 'skipped' if reason else 'pending', 'failure_reason': reason or None})
    return result


def _eligibility(svc: dict, cfg: dict) -> str:
    """Не нарушаем заданные пользователем и площадкой ограничения по частоте."""
    now = time.time()
    since = engine.day_start(int(now))
    shared = svc['site_id'] in db.SHARED_SLOT_SITES
    with db.rd() as conn:
        if shared:
            old = conn.execute("SELECT MAX(COALESCE(t.finished_at,t.started_at)) m, "
                               "SUM(CASE WHEN t.started_at>=? THEN 1 ELSE 0 END) c FROM tasks t "
                               "JOIN services v ON v.id=t.service_id WHERE v.site_id=? AND t.started_at IS NOT NULL",
                               (since, svc['site_id'])).fetchone()
            camps = conn.execute('SELECT MAX(submitted_at) m, SUM(CASE WHEN submitted_at>=? THEN 1 ELSE 0 END) c '
                                 'FROM campaign_results WHERE site_id=?', (since, svc['site_id'])).fetchone()
        else:
            old = conn.execute('SELECT MAX(COALESCE(finished_at,started_at)) m, '
                               'SUM(CASE WHEN started_at>=? THEN 1 ELSE 0 END) c FROM tasks '
                               'WHERE service_id=? AND started_at IS NOT NULL', (since, svc['id'])).fetchone()
            camps = conn.execute('SELECT MAX(submitted_at) m, SUM(CASE WHEN submitted_at>=? THEN 1 ELSE 0 END) c '
                                 'FROM campaign_results WHERE service_id=?', (since, svc['id'])).fetchone()
        last = max(old['m'] or 0, camps['m'] or 0)
        cooldown = int(svc.get('cooldown_min') or 0) * 60
        if last and now - last < cooldown:
            return f'действует интервал площадки: ещё {int(cooldown - (now - last)) + 1} с'
        daily = svc.get('max_claims_day')
        if daily is not None and int(daily) > 0 and int(old['c'] or 0) + int(camps['c'] or 0) >= int(daily):
            return 'исчерпан суточный лимит услуги'
        if cfg['daily_limit']:
            visits = conn.execute('SELECT COUNT(*) FROM tasks WHERE started_at>=?', (since,)).fetchone()[0]
            verified_visits = conn.execute('SELECT COUNT(*) FROM campaign_results WHERE submitted_at>=?', (since,)).fetchone()[0]
            if visits + verified_visits >= cfg['daily_limit']:
                return 'достигнут установленный общий суточный лимит'
    return ''


def _alternative_vk(url: str) -> str | None:
    sp = urlsplit(url)
    if sp.hostname in ('vk.ru', 'm.vk.ru'):
        host = 'vk.com'
    elif sp.hostname in ('vk.com', 'm.vk.com'):
        host = 'vk.ru'
    else:
        return None
    return urlunsplit(('https', host, sp.path, '', ''))


def _possible_click(result: drivers.DriverResult) -> bool:
    return bool(getattr(result, 'submitted', False)) or any(
        any(w in str(step).lower() for w in ('нажал', 'нажала', 'жму', 'кнопка нажата', 'заявка стартовала'))
        for step in (result.steps or []))


def _stop_requested(campaign_id: int, event: threading.Event) -> bool:
    if event.is_set():
        return True
    with db.rd() as conn:
        row = conn.execute('SELECT status FROM campaigns WHERE id=?', (campaign_id,)).fetchone()
    if not row or row['status'] == 'stopping':
        event.set()
        return True
    return False


def _run_attempt(campaign_id: int, result_id: int, svc: dict, target: dict, baseline: int,
                 route: network.Endpoint, method: str, link: str, routes: list[network.Endpoint],
                 cfg: dict, stop_event: threading.Event) -> tuple[drivers.DriverResult, Observation | None, dict]:
    """Драйвер выполняется в отдельном потоке, счётчик опрашивается уже во время отправки."""
    signalled = threading.Event()
    done = threading.Event()
    lock = threading.Lock()
    state: dict = {'result': None, 'started': None, 'submitted_at': None, 'done_at': None,
                   'baseline': baseline, 'baseline_error': None, 'late': False}
    attempt = {'method': method, 'route': network.mask_proxy_url(route.proxy_url),
               'target_url': link, 'started_at': time.time(), 'baseline_count': baseline}

    def record_submission(*, check_stop: bool) -> None:
        with lock:
            if state['started'] is not None:
                if check_stop:
                    # Один вызов callback соответствует максимум одному действию отправки.
                    raise drivers.OperationCancelled('Повторная отправка запрещена')
                return
            if check_stop:
                if _stop_requested(campaign_id, stop_event):
                    raise drivers.OperationCancelled('Остановлено до отправки')
                # Форма может открываться несколько минут. Старый baseline уже не
                # годится для оценки заказа: измеряем ещё раз перед самим кликом.
                try:
                    count, label, source = _counter(target, routes)
                except delivery.CounterError as exc:
                    state['baseline_error'] = 'Счётчик недоступен перед отправкой: ' + _safe_detail(str(exc))
                    raise
                if _stop_requested(campaign_id, stop_event):
                    raise drivers.OperationCancelled('Остановлено до отправки')
                wall = time.time()
                # Замер и отметка риска фиксируются вместе; между ними нельзя
                # разрешить отправку или потерять значение после сбоя процесса.
                with db.tx() as conn:
                    row = conn.execute('SELECT status FROM campaigns WHERE id=?', (campaign_id,)).fetchone()
                    if stop_event.is_set() or not row or row['status'] != 'running':
                        stop_event.set()
                        raise drivers.OperationCancelled('Остановлено до отправки')
                    conn.execute('INSERT INTO campaign_samples(campaign_id,result_id,observed_at,count,source,route) '
                                 'VALUES(?,?,?,?,?,?)', (campaign_id, result_id, wall, count, source, label))
                    conn.execute('UPDATE campaigns SET current_count=? WHERE id=?', (count, campaign_id))
                    conn.execute('UPDATE campaign_results SET baseline_count=?, submitted_at=? WHERE id=?',
                                 (count, wall, result_id))
                state['baseline'] = count
            else:
                # Старый драйвер сообщил об отправке только после возможного
                # клика: не выдаём более поздний замер за исходный.
                wall = time.time()
                _update_result(result_id, submitted_at=wall)
            state['started'] = time.monotonic()
            state['submitted_at'] = wall
            signalled.set()

    def on_submit():
        record_submission(check_stop=True)

    def execute():
        try:
            if method == 'http':
                res = drivers.run_nojs(svc, link, proxy=route.proxy_url, on_submit=on_submit,
                                       cancel_event=stop_event, normalize_target=False)
            else:
                prefer = None if cfg['backend'] in ('', 'auto') else cfg['backend']
                res = drivers.run(svc, link, proxy=route.proxy_url, on_submit=on_submit,
                                  cancel_event=stop_event, normalize_target=False, prefer=prefer,
                                  headless=cfg['headless'], allow_captcha_wait=cfg['allow_captcha'],
                                  deadline_sec=max(240, int(svc.get('hold_seconds') or 0) + 180))
            # Старые/специальные драйверы могут подтвердить отправку без callback.
            if not signalled.is_set() and (res.ok or _possible_click(res)):
                # Момент уже упущен: отмечаем риск повтора, но не утверждаем точное время.
                state['late'] = True
                record_submission(check_stop=False)
            state['result'] = res
        except Exception as exc:
            state['result'] = drivers.DriverResult('site_broken', 'ошибка драйвера: ' + type(exc).__name__)
        finally:
            state['done_at'] = time.monotonic()
            done.set()

    thread = threading.Thread(target=execute, name=f'verify-driver-{result_id}', daemon=True)
    thread.start()
    try:
        while not signalled.is_set() and not done.is_set():
            if _stop_requested(campaign_id, stop_event):
                break
            done.wait(0.25)
        observation: Observation | None = None
        if signalled.is_set() and not stop_event.is_set():
            _update_campaign(campaign_id, current_phase='Ожидание фактического прироста')
            last_measurement = ['direct', 'источник не указан']
            def read():
                count, label, source = _counter(target, routes)
                last_measurement[:] = [label, source]
                return count
            def record(count, at):
                del at
                _sample(campaign_id, result_id, count, last_measurement[1], last_measurement[0])
            def explicit_rejection():
                res = state.get('result') if done.is_set() else None
                if res is not None and res.verdict in ('bad_link', 'quota'):
                    return _safe_detail(res.detail)
                return None
            def waiting(seconds):
                # Также реагируем на остановку, запрошенную другим процессом.
                end = time.monotonic() + seconds
                while time.monotonic() < end:
                    if _stop_requested(campaign_id, stop_event):
                        return True
                    stop_event.wait(min(1.0, max(0, end - time.monotonic())))
                return _stop_requested(campaign_id, stop_event)
            # Драйвер может отправить заказ после капчи или длинного таймера
            # (SMM8). Наблюдаем непрерывно и ещё пять минут после его окончания,
            # чтобы не считать отсутствие роста во время ожидания отказом.
            delayed = lambda: state['done_at'] if done.is_set() else None
            observation = observe_growth(read, state['baseline'], state['started'], timeout_sec=cfg['timeout'],
                                         poll_sec=cfg['poll'], settle_sec=cfg['settle'],
                                         wait=waiting, on_sample=record, rejected=explicit_rejection,
                                         delayed_until=delayed)
        if _stop_requested(campaign_id, stop_event):
            stop_event.set()
        if observation and observation.status == 'unverified' and not done.is_set():
            stop_event.set()  # прекращаем небезопасный длительный драйвер, но не освобождаем барьер
        # Нельзя запускать новый сайт, пока прежний драйвер ещё может отправить заказ.
        informed = False
        while not done.wait(0.25):
            _stop_requested(campaign_id, stop_event)
            if stop_event.is_set() and not informed:
                _update_campaign(campaign_id, current_phase='Ожидаем закрытия драйвера; следующий сайт заблокирован')
                informed = True
        thread.join()
    finally:
        if not done.is_set():
            stop_event.set()
            thread.join()  # не освобождать блокировку, пока драйвер может отправлять заказ
    res = state['result'] or drivers.DriverResult('site_broken', 'Драйвер не вернул результат')
    # Если свежий замер перед кликом не получен, не подменяем его старым.
    # Даже если ошибочный старый драйвер проигнорировал исключение callback и
    # позднее сообщил об отправке, сохраняем риск и не объявляем успех.
    if state['baseline_error'] and not stop_event.is_set():
        observation = Observation('unverified', observation.final_count if observation else None, None,
                                  reason=state['baseline_error'])
    # Отказ пришёл после последнего измерения; он имеет приоритет над случайным ростом.
    elif observation and res.verdict in ('bad_link', 'quota'):
        observation = Observation('rejected', observation.final_count, None, reason=_safe_detail(res.detail))
    elif observation and observation.status != 'stopped' and res.verdict != 'ok':
        observation = Observation('unverified', observation.final_count, None,
                                  reason='После возможной отправки заказ не подтверждён: ' + _safe_detail(res.detail))
    elif observation and state['late'] and observation.status == 'working':
        observation.time_to_growth_sec = None
        observation.reason = 'Рост обнаружен, но точный момент отправки драйвер не зафиксировал'
    attempt.update({'finished_at': time.time(), 'submitted_at': state['submitted_at'],
                    'baseline_count': state['baseline'], 'verdict': res.verdict,
                    'detail': _safe_detail(res.detail), 'backend': res.backend or method})
    return res, observation, attempt


def _refresh_baseline(campaign_id: int, result_id: int, target: dict, routes: list[network.Endpoint]) -> int:
    count, label, source = _counter(target, routes)
    _sample(campaign_id, result_id, count, source, label)
    _update_result(result_id, baseline_count=count)
    return count


def _service(campaign_id: int, result_id: int, svc: dict, target: dict, baseline: int,
             routes: list[network.Endpoint], cfg: dict, stop_event: threading.Event) -> tuple[Observation, list[dict], str]:
    attempts: list[dict] = []
    primary = drivers.normalize_link(svc['site_id'], target['url'])
    alternative = _alternative_vk(primary) if target['platform'] == 'vk' else None
    variants = [primary]
    last_reason = 'Нет доступного способа отправить заказ'
    terminal = False
    method_used = ''
    for route in routes:
        if terminal or len(attempts) >= MAX_ATTEMPTS:
            break
        # Быстрый HTML-путь не должен обходить заявленный таймер площадки.
        methods = ['browser']
        if cfg['nojs'] and int(svc.get('hold_seconds') or 0) <= 0:
            methods.append('http')
        for method in methods:
            if terminal or len(attempts) >= MAX_ATTEMPTS:
                break
            variant_idx = 0
            while variant_idx < len(variants) and len(attempts) < MAX_ATTEMPTS:
                if _stop_requested(campaign_id, stop_event):
                    return Observation('stopped', None, None, reason='Остановлено пользователем'), attempts, method_used
                link = variants[variant_idx]
                _update_campaign(campaign_id, current_phase=f'Попытка: {method}, {route.label}')
                res, obs, attempt = _run_attempt(campaign_id, result_id, svc, target, baseline,
                                                 route, method, link, routes, cfg, stop_event)
                attempts.append(attempt)
                method_used = f'{method} / {network.mask_proxy_url(route.proxy_url)}'
                _update_result(result_id, attempts_json=json.dumps(attempts, ensure_ascii=False), method_used=method_used)
                last_reason = attempt['detail'] or res.verdict
                if stop_event.is_set():
                    return Observation('stopped', obs.final_count if obs else None, None,
                                       reason='Остановлено пользователем; заказ может продолжаться'), attempts, method_used
                if obs and obs.status != 'rejected':
                    return obs, attempts, method_used
                if res.verdict == 'quota':
                    # Лимит площадки не обходим сменой IP.
                    terminal = True
                    break
                if res.verdict == 'bad_link':
                    if variant_idx == 0 and alternative and alternative != link:
                        if alternative not in variants:
                            variants.append(alternative)
                        try:
                            baseline = _refresh_baseline(campaign_id, result_id, target, routes)
                        except delivery.CounterError as exc:
                            return Observation('unverified', None, None, reason=_safe_detail(str(exc))), attempts, method_used
                        variant_idx += 1
                        continue
                    terminal = True
                    break
                # После возможного клика/POST без явного отказа повтор может создать дубликат.
                if attempt['submitted_at'] is not None:
                    return Observation('unverified', None, None,
                                       reason='Ответ после возможной отправки неясен; повтор запрещён'), attempts, method_used
                if any(w in last_reason.lower() for w in ('e-mail', 'email', 'регистрац', 'авторизац',
                                                          'бесплатный способ оплаты')):
                    terminal = True
                    break
                network.mark_endpoint_bad(svc['site_id'], route.id, last_reason)
                break
    # Без подтверждённой отправки и пяти минут наблюдения нет оснований
    # объявлять площадку неработающей или приписывать ей нулевой прирост.
    return Observation('skipped', None, None, reason='Не удалось проверить заказ: ' + last_reason), attempts, method_used


def start(channel_url: str, *, acknowledge_risk: bool = False, background: bool = True) -> dict:
    """Создаёт и запускает один прогон. Никаких запросов к площадкам до исходного счётчика."""
    platform, kind, url = delivery.normalize_channel_input(channel_url)
    if kind != 'community':
        raise ValueError('Проверяем только подписчиков сообщества ВК или публичного Telegram-канала')
    cfg = _settings()
    if db.get_setting('dry_run', '0') == '1':
        raise ValueError('Включён dry-run: выключите его, чтобы отправлять реальные заявки')
    if not executor.AUTO_LOCK.acquire(blocking=False):
        raise ValueError('Робот уже занят другой задачей')
    campaign_id = None
    try:
        with db.tx() as conn:
            active = conn.execute("SELECT id FROM campaigns WHERE status IN ('running','stopping') LIMIT 1").fetchone()
            if active:
                raise ValueError(f'Уже выполняется кампания #{active[0]}')
            task = conn.execute("SELECT id FROM tasks WHERE status IN "
                                "('running','wait_confirm','captcha','waiting_delivery','delivery_uncertain') LIMIT 1").fetchone()
            if task:
                raise ValueError(f'Сначала завершите или проверьте открытую задачу #{task[0]}')
            order = conn.execute("SELECT task_id FROM deliveries WHERE state IN "
                                 "('reserved','submitting','waiting','uncertain') LIMIT 1").fetchone()
            if order:
                raise ValueError(f'Сначала проверьте незавершённый заказ #{order[0]}')
            unresolved = conn.execute("SELECT r.id FROM campaign_results r WHERE r.status='unverified' "
                                      "AND r.submitted_at IS NOT NULL AND r.acknowledged_at IS NULL LIMIT 1").fetchone()
            old_unverified = conn.execute("SELECT id FROM tasks WHERE status='submitted_unverified' "
                                          "AND unverified_ack_at IS NULL LIMIT 1").fetchone()
            if (unresolved or old_unverified) and not acknowledge_risk:
                raise ValueError('Есть прерванный/непроверенный заказ, который может ещё выполняться. '
                                 'Проверьте его и явно подтвердите риск повторного запуска.')
            if unresolved or old_unverified:
                conn.execute("UPDATE campaign_results SET acknowledged_at=? WHERE status='unverified' "
                             "AND submitted_at IS NOT NULL AND acknowledged_at IS NULL", (time.time(),))
                conn.execute("UPDATE tasks SET unverified_ack_at=? WHERE status='submitted_unverified' "
                             "AND unverified_ack_at IS NULL", (time.time(),))
            conn.execute('INSERT OR IGNORE INTO targets(name,platform,url,kind,created_at) VALUES(?,?,?,?,?)',
                         (url, platform, url, kind, int(time.time())))
            tid = conn.execute('SELECT id FROM targets WHERE platform=? AND url=?', (platform, url)).fetchone()[0]
            candidates = _site_results(conn, platform, kind, cfg)
            cur = conn.execute('INSERT INTO campaigns(target_id,channel_url,platform,metric,services_total,status,'
                               'growth_timeout_sec,poll_interval_sec,settle_sec,started_at,owner_pid,owner_birth,current_phase) '
                               "VALUES(?,?,?,'followers',?,'running',?,?,?,?,?,?,'Получение исходного счётчика')",
                               (tid, url, platform, len(candidates), cfg['timeout'], cfg['poll'], cfg['settle'],
                                int(time.time()), os.getpid(), db._process_birth(os.getpid())))
            campaign_id = cur.lastrowid
            for seq, item in enumerate(candidates, 1):
                conn.execute('INSERT INTO campaign_results(campaign_id,seq_no,site_id,site_name,service_id,'
                             'service_label,service_url,channel_url,status,failure_reason) VALUES(?,?,?,?,?,?,?,?,?,?)',
                             (campaign_id, seq, item['site_id'], item['site_name'], item['service_id'],
                              item['service_label'], item['service_url'], url, item['status'], item['failure_reason']))
        event = threading.Event()
        with _RUNS_LOCK:
            _RUNS[campaign_id] = event
        target = {'url': url, 'platform': platform, 'kind': kind}
        if background:
            threading.Thread(target=_worker, args=(campaign_id, target, cfg, event),
                             daemon=True, name=f'verify-{campaign_id}').start()
        else:
            _worker(campaign_id, target, cfg, event)
        return {'ok': True, 'campaign_id': campaign_id}
    except Exception:
        if campaign_id is not None:
            with _RUNS_LOCK:
                _RUNS.pop(campaign_id, None)
            try:
                _update_campaign(campaign_id, status='failed', finished_at=int(time.time()),
                                 error_reason='Не удалось запустить поток проверки')
            except Exception:
                pass
        executor.AUTO_LOCK.release()
        raise


def _worker(campaign_id: int, target: dict, cfg: dict, stop_event: threading.Event) -> None:
    final_status = 'completed'
    error = None
    try:
        routes = _routes(cfg)
        if _stop_requested(campaign_id, stop_event):
            final_status = 'stopped'
            return
        try:
            initial, label, source = _counter(target, routes)
        except delivery.CounterError as exc:
            final_status, error = 'failed', 'Исходный счётчик недоступен: ' + _safe_detail(str(exc))
            return
        _sample(campaign_id, None, initial, source, label)
        _update_campaign(campaign_id, initial_count=initial, current_count=initial)
        with db.rd() as conn:
            rows = [dict(r) for r in conn.execute('SELECT * FROM campaign_results WHERE campaign_id=? ORDER BY seq_no',
                                                  (campaign_id,)).fetchall()]
        for row in rows:
            if row['status'] != 'pending':
                continue
            if _stop_requested(campaign_id, stop_event):
                final_status = 'stopped'
                break
            svc = engine.services_map().get(row['service_id'])
            if not svc or not svc['site_enabled'] or not svc['enabled']:
                _update_result(row['id'], status='skipped', failure_reason='услуга отключена или удалена',
                               finished_at=time.time())
                continue
            reason = _eligibility(svc, cfg)
            if reason:
                _update_result(row['id'], status='skipped', failure_reason=reason, finished_at=time.time())
                continue
            # Общая пауза между отправками, без ожидания внутри транзакции.
            with db.rd() as conn:
                a = conn.execute('SELECT MAX(started_at) FROM tasks').fetchone()[0] or 0
                b = conn.execute('SELECT MAX(submitted_at) FROM campaign_results').fetchone()[0] or 0
            gap = max(0.0, cfg['gap'] - (time.time() - max(a, b)))
            if gap and stop_event.wait(gap):
                final_status = 'stopped'
                break
            _update_campaign(campaign_id, current_service=row['site_name'], current_phase='Снимаем исходный счётчик')
            _update_result(row['id'], status='running', started_at=time.time())
            try:
                baseline = _refresh_baseline(campaign_id, row['id'], target, routes)
            except delivery.CounterError as exc:
                _update_result(row['id'], status='unverified', failure_reason='Счётчик недоступен до запуска: ' + _safe_detail(str(exc)),
                               finished_at=time.time())
                final_status, error = 'failed', 'Проверка остановлена: нет точного счётчика'
                break
            obs, attempts, method = _service(campaign_id, row['id'], svc, target, baseline, routes, cfg, stop_event)
            status = 'unverified' if obs.status == 'stopped' else obs.status
            _update_result(row['id'], status=status, final_count=obs.final_count, added_count=obs.added_count,
                           time_to_growth_sec=obs.time_to_growth_sec, failure_reason=obs.reason or None,
                           method_used=method or None, attempts_json=json.dumps(attempts, ensure_ascii=False),
                           finished_at=time.time())
            with db.rd() as conn:
                agg = conn.execute("SELECT COALESCE(SUM(CASE WHEN status='working' THEN added_count ELSE 0 END),0) total, "
                                   "SUM(CASE WHEN status='working' THEN 1 ELSE 0 END) working, "
                                   "SUM(CASE WHEN status='not_working' THEN 1 ELSE 0 END) failed "
                                   "FROM campaign_results WHERE campaign_id=?", (campaign_id,)).fetchone()
            _update_campaign(campaign_id, total_added=agg['total'] or 0,
                             services_working=agg['working'] or 0, services_failed=agg['failed'] or 0)
            if obs.status in ('unverified', 'stopped'):
                final_status = 'stopped' if obs.status == 'stopped' else 'failed'
                error = obs.reason
                break
        if _stop_requested(campaign_id, stop_event) and final_status == 'completed':
            final_status = 'stopped'
    except Exception as exc:
        final_status, error = 'failed', 'Ошибка проверки: ' + _safe_detail(str(exc) or type(exc).__name__)
        db.log('error', f'Кампания #{campaign_id}: {error}')
    finally:
        # Оставшиеся площадки никогда не объявляются неработающими без проверки.
        try:
            with db.tx() as conn:
                conn.execute("UPDATE campaign_results SET status='unverified', added_count=NULL, finished_at=?, "
                             "failure_reason=COALESCE(failure_reason,'Проверка прервана; результат неизвестен') "
                             "WHERE campaign_id=? AND status='running'", (time.time(), campaign_id))
                conn.execute("UPDATE campaign_results SET status='skipped', failure_reason=COALESCE(failure_reason,?), "
                             "finished_at=? WHERE campaign_id=? AND status='pending'",
                             ('кампания завершилась до запуска площадки', time.time(), campaign_id))
            _update_campaign(campaign_id, status=final_status, finished_at=int(time.time()),
                             error_reason=error, current_service=None, current_phase=None)
        finally:
            with _RUNS_LOCK:
                _RUNS.pop(campaign_id, None)
            executor.AUTO_LOCK.release()


def stop(campaign_id: int | None = None) -> dict:
    with db.tx() as conn:
        if campaign_id is None:
            row = conn.execute("SELECT id FROM campaigns WHERE status IN ('running','stopping') ORDER BY id DESC LIMIT 1").fetchone()
            campaign_id = row['id'] if row else None
        if not campaign_id:
            raise ValueError('Нет активной кампании')
        changed = conn.execute("UPDATE campaigns SET status='stopping', current_phase='Останавливаемся' "
                               "WHERE id=? AND status IN ('running','stopping')", (campaign_id,)).rowcount
        if not changed:
            raise ValueError('Кампания уже завершена или не найдена')
    with _RUNS_LOCK:
        event = _RUNS.get(campaign_id)
        if event:
            event.set()
    return {'ok': True, 'campaign_id': campaign_id, 'status': 'stopping'}


def status(campaign_id: int | None = None) -> dict | None:
    with db.rd() as conn:
        if campaign_id is None:
            row = conn.execute("SELECT * FROM campaigns ORDER BY (status IN ('running','stopping')) DESC, id DESC LIMIT 1").fetchone()
        else:
            row = conn.execute('SELECT * FROM campaigns WHERE id=?', (campaign_id,)).fetchone()
        if not row:
            return None
        campaign = dict(row)
        results = [dict(r) for r in conn.execute('SELECT * FROM campaign_results WHERE campaign_id=? ORDER BY seq_no',
                                                 (row['id'],)).fetchall()]
        samples = [dict(s) for s in conn.execute('SELECT result_id,observed_at,count,source,route FROM campaign_samples '
                                                  'WHERE campaign_id=? ORDER BY id', (row['id'],)).fetchall()]
    for result in results:
        try:
            result['attempts'] = json.loads(result.pop('attempts_json') or '[]')
        except (ValueError, TypeError):
            result['attempts'] = []
        result['samples'] = [s for s in samples if s['result_id'] == result['id']]
    campaign['baseline_samples'] = [s for s in samples if s['result_id'] is None]
    campaign['results'] = results
    if not any(r['status'] in ('working', 'not_working') and r['added_count'] is not None for r in results):
        campaign['total_added'] = None  # сумма ещё не измерена, это не ноль
    campaign['attribution_note'] = ATTRIBUTION_NOTE
    return campaign


def unresolved_count() -> int:
    with db.rd() as conn:
        return db.unresolved_order_count(conn)


def acknowledge_risk(confirm: bool) -> dict:
    if confirm is not True:
        raise ValueError('Требуется явное подтверждение риска')
    now = time.time()
    with db.tx() as conn:
        a = conn.execute("UPDATE campaign_results SET acknowledged_at=? WHERE status='unverified' "
                         "AND submitted_at IS NOT NULL AND acknowledged_at IS NULL", (now,)).rowcount
        b = conn.execute("UPDATE tasks SET unverified_ack_at=? WHERE status='submitted_unverified' "
                         "AND unverified_ack_at IS NULL", (now,)).rowcount
    if a + b:
        db.log('warn', f'Пользователь подтвердил риск перекрытия непроверенных заказов: {a + b}')
    return {'ok': True, 'acknowledged': a + b}


def active() -> dict | None:
    with db.rd() as conn:
        row = conn.execute("SELECT id,status FROM campaigns WHERE status IN ('running','stopping') LIMIT 1").fetchone()
    return dict(row) if row else None


def history(limit: int = 20) -> list[dict]:
    with db.rd() as conn:
        rows = conn.execute("SELECT c.id,c.channel_url,c.platform,c.initial_count,c.current_count, "
                            "CASE WHEN EXISTS (SELECT 1 FROM campaign_results r WHERE r.campaign_id=c.id "
                            "AND r.status IN ('working','not_working') AND r.added_count IS NOT NULL) "
                            "THEN c.total_added ELSE NULL END AS total_added, c.status, "
                            "c.services_total,c.services_working,c.services_failed,c.started_at,c.finished_at,c.error_reason "
                            "FROM campaigns c ORDER BY c.id DESC LIMIT ?", (max(1, min(100, int(limit))),)).fetchall()
    return [dict(r) for r in rows]


def format_report(campaign: dict) -> str:
    """Текстовый отчёт: пропуск/недоступный счётчик обозначаются «нет данных», а не нулём."""
    labels = {'working': 'работает', 'not_working': 'не работает', 'unverified': 'не проверено',
              'skipped': 'не проверено', 'pending': 'ожидает', 'running': 'проверяется'}
    def num(value):
        return 'нет данных' if value is None else str(value)
    started = campaign.get('started_at')
    timestamp = datetime.fromtimestamp(started, timezone.utc).isoformat() if started is not None else 'нет данных'
    lines = [f"Ссылка: {campaign.get('channel_url', '')}",
             f"Начало проверки (UTC): {timestamp}",
             f"Исходный счётчик: {num(campaign.get('initial_count'))}",
             f"Последний счётчик: {num(campaign.get('current_count'))}",
             f"Состояние проверки: {campaign.get('status', '')}"]
    if campaign.get('error_reason'):
        lines.append('Причина остановки: ' + str(campaign['error_reason']))
    for r in campaign.get('results', []):
        growth = r.get('time_to_growth_sec')
        lines.extend(['', f"{r.get('site_name', r.get('site_id', ''))} — {labels.get(r.get('status'), r.get('status', ''))}",
                      f"  Страница площадки: {r.get('service_url') or 'нет данных'}",
                      f"  Услуга: {r.get('service_label') or 'нет подходящей услуги'}",
                      f"  Было: {num(r.get('baseline_count'))}; стало: {num(r.get('final_count'))}; "
                      f"наблюдаемый прирост: {num(r.get('added_count'))}",
                      f"  До первого обнаруженного роста: {num(growth)}" + (' с' if growth is not None else '')])
        if r.get('failure_reason'):
            lines.append('  Причина: ' + str(r['failure_reason']))
        for a in r.get('attempts', []):
            lines.append(f"  Попытка: {a.get('method')} / {a.get('route')}: {a.get('verdict')} — {a.get('detail', '')}")
        if r.get('samples'):
            lines.append('  Измерения (Unix-время: значение): ' + ', '.join(
                f"{int(s['observed_at'])}: {s['count']}" for s in r['samples']))
    lines.extend(['', 'Время до роста отсчитывается от первого возможного действия отправки; '
                      'ожидание до этого действия не входит. Задержка обнаружения при опросе входит.',
                  campaign.get('attribution_note') or ATTRIBUTION_NOTE])
    return '\n'.join(lines)
