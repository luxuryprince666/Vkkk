#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One guarded Boost-Like trial, only after the previous live run finishes.

This workflow is queued in the same GitHub concurrency group as the existing
sequential run. It validates its finished snapshot, waits at least one more
hour and requires a stable VK counter, then makes at most one free-trial
attempt. It never handles payment, bonuses, logins or captcha challenges.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import network, stealth, verifier  # noqa: E402
from tools import boost_trial_attempt as attempt, ci_boost_guard_preflight as preflight  # noqa: E402
from tools import run_continuation_ci as continuation, run_resume_after_cancel_ci as resume  # noqa: E402
from tools.ci_progress import GitHubProgress, ProgressError  # noqa: E402
from tools.run_unsent_followup_ci import REPO, TARGET, github_get  # noqa: E402

PRIOR_RUN = 44815806287
PRIOR_JOB = 136563690073
PRIOR_CHECK = 136564526676
PRIOR_SHA = 'ad85a43360ad2ad9c44417e7d32ad69ed1b2ee39'
PREFLIGHT_RUN = 44819111502
PREFLIGHT_CHECK = 136575225339
PREFLIGHT_SHA = 'ee01791d71be09ca37c25803545fa011462a9300'
BRANCH = 'arena/e372c48b-vkkk'
WORKFLOW = 'vk-boost-after-resume.yml'
MARKER_FILE = ROOT / '.github/vk-boost-after-resume-club243396555.once'
MARKER_CONTENT = 'boost-free-once-after-44815806287\n'
CHECK_NAME = 'VK Boost-Like trial after sequential remainder'
QUARANTINE_SEC = 3600
STABLE_SEC = 120
POLL_SEC = 30
OBSERVE_SEC = 3600
WARNING = ('Возможна поздняя доставка предыдущих площадок или органическое изменение счётчика; '
           'источник любого роста не установлен.')


def _time(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('missing timezone')
        return parsed.timestamp()
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError('Неизвестно время завершения предыдущего задания') from exc


def validate_evidence(prior_run: dict, prior_job: dict, prior_check: dict,
                      preflight_run: dict, preflight_check: dict, *, now: float | None = None) -> dict:
    """Fail closed unless the exact previous run and read-only inspection finished."""
    now = time.time() if now is None else now
    resume._completed(prior_run, PRIOR_RUN, PRIOR_SHA, 'success')
    resume._completed(preflight_run, PREFLIGHT_RUN, PREFLIGHT_SHA, 'success')
    if (prior_job.get('id') != PRIOR_JOB or prior_job.get('run_id') != PRIOR_RUN
            or prior_job.get('status') != 'completed' or prior_job.get('conclusion') != 'success'):
        raise ValueError('Предыдущий live-job не завершился успешно')
    ended = _time(prior_job.get('completed_at'))
    if not 0 < ended <= now + 60:
        raise ValueError('Некорректное время завершения предыдущего live-job')
    state = resume._check_snapshot(prior_check, PRIOR_CHECK, PRIOR_SHA, resume.CHECK_NAME)
    rows = state.get('results')
    if (str(state.get('actions_run_id')) != str(PRIOR_RUN) or state.get('target') != TARGET
            or state.get('phase') != 'завершено' or state.get('error') is not None
            or state.get('current') is not None or tuple(state.get('queue') or ()) != resume.QUEUE
            or not isinstance(rows, list) or tuple(r.get('site_id') for r in rows) != resume.QUEUE):
        raise ValueError('Предыдущая очередь не завершена или относится к другой ссылке')
    for row in rows:
        samples = row.get('samples') or []
        if not isinstance(samples, list) or any(
            type(x.get('count')) is not int or x['count'] < 0 or type(x.get('observed_at')) not in (int, float)
            or not 0 < x['observed_at'] <= ended + 120 for x in samples):
            raise ValueError('Некорректные замеры предыдущей площадки')
        sub = row.get('submitted_at')
        if sub is not None and (type(sub) not in (int, float) or not 0 < sub <= ended):
            raise ValueError('Некорректное время предыдущего возможного действия')
        raw = dict(row, status=row.get('verifier_status'))
        sample_now = samples[-1]['observed_at'] if samples else ended
        try:
            okay, _ = continuation.can_continue({'status': row.get('campaign_status')}, raw, now=sample_now)
        except (TypeError, KeyError, ValueError) as exc:
            raise ValueError('Неполный итог предыдущей площадки') from exc
        if not okay:
            raise ValueError('Предыдущее окно наблюдения или статус не подтверждены')
    checked = resume._check_snapshot(preflight_check, PREFLIGHT_CHECK, PREFLIGHT_SHA, preflight.CHECK)
    card, digest = checked.get('initial_card') or {}, checked.get('module_sha256') or ''
    if (checked.get('phase') != 'завершено' or checked.get('orders_sent') != 0
            or checked.get('target_entered') is not False or checked.get('clicked') is not False
            or checked.get('changing_requests_allowed') is not False or not card.get('valid')
            or checked.get('module_path') != attempt.MODULE or not re.fullmatch(r'[0-9a-f]{64}', digest)):
        raise ValueError('Сетевой preflight без заявки не подтверждён')
    return {'prior_ended_at': ended, 'module_sha256': digest,
            'prior_sites': [{'site_id': r['site_id'], 'status': r.get('verifier_status'),
                             'possible_at': r.get('submitted_at')} for r in rows]}


def ensure_one_run(token: str, run_id: int) -> None:
    path = f'/actions/workflows/{WORKFLOW}/runs?per_page=100&branch={quote(BRANCH, safe="")}'
    for _ in range(12):
        runs = github_get(path, token).get('workflow_runs') or []
        if any(x.get('id') == run_id for x in runs):
            if any(type(x.get('id')) is int and x['id'] < run_id for x in runs):
                raise RuntimeError('Этот одноразовый workflow уже запускался')
            return
        time.sleep(2)
    raise RuntimeError('Не удалось подтвердить отсутствие повторного запуска')


def render(state: dict) -> str:
    samples = state.get('samples') or []
    q = state.get('quarantine_samples') or []
    start, complete = state.get('start') or {}, state.get('complete') or {}
    baseline = state.get('baseline_count')
    possible = start.get('possible_at')
    after = [s for s in samples if possible is not None and s.get('observed_at', 0) >= possible]
    last = after[-1]['count'] if after else None
    delta = last - baseline if type(last) is int and type(baseline) is int else None
    first = next((s for s in after if type(s.get('count')) is int and type(baseline) is int
                  and s['count'] > baseline), None)
    first_after = round(first['observed_at'] - possible, 1) if first else None
    show = lambda x: 'нет данных' if x is None else str(x)
    lines = [f'Ссылка: {TARGET}', f"Состояние: {state.get('phase', 'подготовка')}",
             f'Предыдущий запуск: https://github.com/{REPO}/actions/runs/{PRIOR_RUN}', WARNING,
             f'Карантин: {len(q)} замеров; первый {show(q[0]["count"] if q else None)}, '
             f'последний {show(q[-1]["count"] if q else None)}.',
             '', 'Boost-Like — бесплатная карточка подписчиков ВК (не платная корзина)',
             f'  Возможное действие до ввода: {show(start.get("possible_at"))}; '
             f'запрос запуска пропущен барьером: {show(start.get("request_released"))}; '
             f'HTTP-статус запуска: {show(start.get("http_status"))}.',
             f'  Возможное завершение: {show(complete.get("possible_at"))}; '
             f'запрос завершения пропущен барьером: {show(complete.get("request_released"))}; '
             f'HTTP-статус завершения: {show(complete.get("http_status"))}.',
             f'  Ответ страницы: {show(state.get("site_response"))}.',
             f'  Площадка показала подтверждение заказа: {show(state.get("provider_claimed_order"))}.',
             f'  Счётчик до ввода: {show(baseline)}; последний замер: {show(last)}; '
             f'чистое изменение: {show(delta)}.',
             f'  Первое обнаружение роста после возможного действия: {show(first_after)}' +
             (' с.' if first_after is not None else '.'),
             '  Доставка именно Boost-Like: неизвестна; рекламный объём не считается доставкой.']
    if state.get('reason'):
        lines.append('  Ограничение: ' + str(state['reason']))
    if state.get('counter_error'):
        lines.append('  Ошибка счётчика: ' + str(state['counter_error']))
    if state.get('observation'):
        status = {'working': 'обнаружен чистый рост счётчика',
                  'not_working': 'за окно чистый рост не обнаружен',
                  'unverified': 'не проверено', 'stopped': 'прервано'}.get(
                      state['observation'].get('status'), 'неизвестно')
        lines.append('  Окно после закрытия браузера: ' + str(state['observation'].get('window_sec')) + ' с; '
                     'состояние: ' + status + '.')
    if samples:
        lines.append('  Замеры (Unix-время: значение): ' + ', '.join(
            f"{int(x['observed_at'])}: {x['count']}" for x in samples))
    lines += ['', 'Неизвестная доставка не является нулём. Доставка может продолжаться после окна.']
    if state.get('actions_run_id'):
        lines.append(f"Запуск: https://github.com/{REPO}/actions/runs/{state['actions_run_id']}")
    return '\n'.join(lines) + '\n'


def publish(state: dict, publisher: GitHubProgress, out: Path, *, conclusion: str | None = None) -> None:
    state['updated_at'] = time.time()
    out.mkdir(parents=True, exist_ok=True)
    for name, contents in (('boost-trial.json', json.dumps(state, ensure_ascii=False, indent=2) + '\n'),
                           ('boost-trial.txt', render(state))):
        dest = out / name
        tmp = dest.with_name(dest.name + '.tmp')
        tmp.write_text(contents, encoding='utf-8')
        os.replace(tmp, dest)
    publisher.update(state, title='Бесплатная сессия и измерение VK; источник роста неизвестен',
                     conclusion=conclusion)


def pause(cancelled, seconds: float) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cancelled():
            return True
        time.sleep(min(1, max(.01, end - time.monotonic())))
    return cancelled()


def wait_quarantine(state: dict, read_count, save, cancelled, *, clock=time.time, sleep=time.sleep,
                    poll: float = POLL_SEC, max_wait: float = 7200) -> None:
    started = clock()
    stable_since = None
    last = None
    while clock() - started <= max_wait:
        if cancelled():
            raise RuntimeError('Остановлено в карантине, до заявки')
        try:
            count, route, source = read_count()
            if type(count) is not int or count < 0:
                raise ValueError('нет точного счётчика')
            at = clock()
            state['quarantine_samples'].append({'observed_at': at, 'count': count,
                                                 'route': route, 'source': source})
            state['counter_error'] = None
            if count != last:
                stable_since, last = at, count
            if at >= state['prior_ended_at'] + QUARANTINE_SEC and stable_since is not None and at - stable_since >= STABLE_SEC:
                save(state)
                return
        except Exception as exc:
            stable_since, last = None, None
            state['counter_error'] = 'Счётчик недоступен: ' + type(exc).__name__
        save(state)
        sleep(poll)
    raise RuntimeError('Счётчик не подтвердил карантин и стабильность; заявки не было')


def run(out: Path) -> int:
    required = {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'push', 'GITHUB_RUN_ATTEMPT': '1',
                'GITHUB_REPOSITORY': REPO, 'GITHUB_REF': 'refs/heads/' + BRANCH,
                'VKKK_LIVE_RUN': 'boost-free-once-after-resume'}
    if any(os.environ.get(k) != v for k, v in required.items()) or MARKER_FILE.read_text(encoding='utf-8') != MARKER_CONTENT:
        raise RuntimeError('Только одноразовое продолжение в разрешённой ветке')
    token, run_id = os.environ.get('VKKK_PROGRESS_TOKEN', ''), os.environ.get('GITHUB_RUN_ID', '')
    if not token or not run_id.isdigit():
        raise RuntimeError('Нет канала состояния')
    ensure_one_run(token, int(run_id))
    # The concurrency lock releases just as GitHub finishes indexing the prior run.
    # Retry read-only status briefly; never turn an incomplete run into permission.
    found = None
    for _ in range(24):
        prior_run = github_get(f'/actions/runs/{PRIOR_RUN}', token)
        if prior_run.get('status') == 'completed':
            found = validate_evidence(
                prior_run, github_get(f'/actions/jobs/{PRIOR_JOB}', token),
                github_get(f'/check-runs/{PRIOR_CHECK}', token),
                github_get(f'/actions/runs/{PREFLIGHT_RUN}', token),
                github_get(f'/check-runs/{PREFLIGHT_CHECK}', token))
            break
        time.sleep(5)
    if found is None:
        raise RuntimeError('Предыдущий запуск ещё не завершён; новая заявка запрещена')

    state = {'phase': 'карантин после предыдущей очереди', 'actions_run_id': run_id, 'target': TARGET,
             'prior_ended_at': found['prior_ended_at'], 'prior_sites': found['prior_sites'],
             'quarantine_samples': [], 'samples': [], 'baseline_count': None,
             'start': {'possible_at': None, 'request_released': False, 'http_status': None},
             'complete': {'possible_at': None, 'request_released': False, 'http_status': None},
             'provider_claimed_order': None, 'site_response': None, 'reason': None,
             'counter_error': None, 'observation': None, 'warning': WARNING}
    publisher = GitHubProgress(name=CHECK_NAME)
    publisher.create(state, title='Карантин перед бесплатной сессией Boost-Like')
    stop = [False]
    def request_stop(signum, frame):
        del signum, frame
        stop[0] = True
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    cancelled = lambda: stop[0]
    save = lambda s: publish(s, publisher, out)
    target = {'platform': 'vk', 'kind': 'community', 'url': TARGET}
    routes = [network.Endpoint(id='direct', kind='direct', label='Прямое соединение')]
    read = lambda: verifier._counter(target, routes)
    try:
        wait_quarantine(state, read, save, cancelled,
                        sleep=lambda sec: pause(cancelled, sec))
        if cancelled():
            raise RuntimeError('Остановлено до заявки')
        try:
            with stealth.open_page(headless=True, prefer='playwright') as (backend, page):
                if backend != 'playwright':
                    raise RuntimeError('Браузер не поддерживает сетевой барьер')
                attempt.run_trial(page, state, read, save, cancelled,
                                  expected_module_sha256=found['module_sha256'])
        except Exception as exc:
            state['reason'] = 'Браузерная попытка остановлена: ' + type(exc).__name__
        save(state)
        if cancelled():
            raise RuntimeError('Остановлено; возможное действие может продолжаться')
        if state['start'].get('possible_at') is not None:
            state['phase'] = 'наблюдение счётчика после закрытия браузера'
            save(state)
            last_meta = ['', '']
            def read_observation():
                count, route, source = read()
                last_meta[:] = [route, source]
                return count
            def sample(count, _at):
                state['samples'].append({'observed_at': time.time(), 'count': count,
                                         'route': last_meta[0], 'source': last_meta[1],
                                         'point': 'после закрытия браузера'})
                save(state)
            obs = verifier.observe_growth(read_observation, state['baseline_count'], time.monotonic(),
                                          timeout_sec=OBSERVE_SEC, poll_sec=POLL_SEC, settle_sec=120,
                                          wait=lambda sec: pause(cancelled, sec), on_sample=sample)
            state['observation'] = {'status': obs.status, 'window_sec': OBSERVE_SEC,
                                    'final_count': obs.final_count, 'net_change': obs.added_count,
                                    'reason': verifier._safe_detail(obs.reason)}
            if cancelled() or obs.status in ('stopped', 'unverified'):
                raise RuntimeError('Полное окно наблюдения не подтверждено')
        state['phase'] = 'завершено' if state['start'].get('possible_at') else 'завершено без заявки'
        publish(state, publisher, out, conclusion='success')
        return 0
    except Exception as exc:
        state['phase'] = 'остановлено'
        state['reason'] = verifier._safe_detail(str(exc) or type(exc).__name__)
        try:
            publish(state, publisher, out, conclusion='failure')
        except ProgressError:
            pass
        return 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    try:
        return run(args.output_dir)
    except Exception as exc:
        print('Boost trial blocked before action:', verifier._safe_detail(str(exc) or type(exc).__name__))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
