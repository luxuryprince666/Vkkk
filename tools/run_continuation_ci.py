#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Continue the user's sequential experiment only on previously unsent sites.

This is an explicitly scoped one-shot CI experiment, not a general override of
application safety. A previous DoctorSMM click was ambiguous: we never retry it,
observe the public counter for at least an hour after that click, and prominently
retain the risk of late delivery. Later observed counter changes are *not*
claimed to be caused by a particular provider.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import db, delivery, executor, network, stealth, verifier  # noqa: E402
from tools.ci_progress import GitHubProgress, ProgressError, decode_snapshot  # noqa: E402
from tools.run_live_ci import notice  # noqa: E402
from tools.run_unsent_followup_ci import (  # noqa: E402
    MARKER, PRIOR_CHECK, PRIOR_RUN, PRIOR_SHA, REPO, TARGET, github_get,
)

CHECK_NAME = 'VK continuation sequential measurement snapshot'
SITES = ('smmnakrutka', 'likeboom', 'avi1', 'nakruti', 'mrpopular', 'prtut', 'pricesmm', 'smm8')
PRIOR_WAIT_SECONDS = 3600
STABLE_SECONDS = 120
POLL_SECONDS = 30
MAX_RUN_SECONDS = 21000
UNCERTAINTY = ('Возможна запоздалая доставка DoctorSMM или предыдущих площадок; '
               'публичный счётчик не позволяет достоверно установить источник прироста.')


def validate_previous(check: dict, workflow: dict) -> dict:
    """Accept precisely the known failed run, and only never-submitted sites."""
    if (workflow.get('id') != PRIOR_RUN or workflow.get('head_sha') != PRIOR_SHA
            or workflow.get('status') != 'completed' or workflow.get('conclusion') != 'failure'
            or workflow.get('run_attempt') != 1):
        raise ValueError('Предыдущий workflow не является ожидаемым завершённым запуском')
    if (check.get('id') != PRIOR_CHECK or check.get('head_sha') != PRIOR_SHA
            or check.get('name') != 'VK sequential live verification snapshot'
            or check.get('status') != 'completed' or check.get('conclusion') != 'failure'):
        raise ValueError('Не найден завершённый снимок предыдущего запуска')
    summary = (check.get('output') or {}).get('summary') or ''
    if MARKER not in summary:
        raise ValueError('В снимке нет структурированных данных')
    snap = decode_snapshot(summary.split(MARKER, 1)[1].strip())
    campaign = snap.get('campaign') or {}
    rows = campaign.get('results')
    if (str(snap.get('actions_run_id')) != str(PRIOR_RUN) or snap.get('phase') != 'failed'
            or campaign.get('status') != 'failed' or campaign.get('channel_url') != TARGET
            or not isinstance(rows, list)):
        raise ValueError('Предыдущая кампания неполная или относится к другой ссылке')
    by_id = {}
    for row in rows:
        site = row.get('site_id')
        if not site or site in by_id or row.get('status') in ('running', 'pending'):
            raise ValueError('Дублирующаяся или незавершённая запись площадки')
        if not isinstance(row.get('attempts', []), list):
            raise ValueError('Некорректный список попыток')
        by_id[site] = row
    doctor = by_id.get('doctorsmm') or {}
    submitted = doctor.get('submitted_at')
    samples = doctor.get('samples') or []
    possible_attempts = [a for a in doctor.get('attempts', []) if a.get('submitted_at') is not None]
    if (doctor.get('status') != 'unverified' or isinstance(submitted, bool)
            or not isinstance(submitted, (int, float)) or submitted <= 0
            or not samples or samples[-1].get('observed_at', 0) < submitted + 900
            or samples[-1].get('count') != doctor.get('final_count')
            or len(possible_attempts) != 1 or possible_attempts[0].get('submitted_at') != submitted
            or (possible_attempts[0].get('observation_window_sec') or 0) < 900):
        raise ValueError('Не подтверждено наблюдение за неоднозначной попыткой DoctorSMM')
    if submitted > time.time() + 60:
        raise ValueError('Время предыдущей попытки находится в будущем')
    for site, row in by_id.items():
        possible = row.get('submitted_at') is not None or any(
            a.get('submitted_at') is not None for a in row.get('attempts', []))
        if site != 'doctorsmm' and possible:
            raise ValueError('Неожиданная возможная отправка в предыдущем запуске: ' + site)
        if site != 'doctorsmm' and row.get('status') == 'unverified':
            raise ValueError('Найдена дополнительная неоднозначная предыдущая попытка')
    for site in SITES:
        if site not in by_id or by_id[site].get('status') != 'skipped':
            raise ValueError('Площадка не подтверждена как ранее не отправленная: ' + site)
    return {'run_id': PRIOR_RUN, 'check_id': PRIOR_CHECK, 'sha': PRIOR_SHA,
            'campaign_status': campaign['status'], 'initial_count': campaign.get('initial_count'),
            'last_count': campaign.get('current_count'),
            'doctor': {'site_id': 'doctorsmm', 'submitted_at': submitted,
                       'baseline_count': doctor.get('baseline_count'),
                       'final_count': doctor.get('final_count'), 'status': doctor['status'],
                       'last_sample_at': samples[-1]['observed_at'],
                       'reason': doctor.get('failure_reason')},
            'other_results': [{'site_id': r.get('site_id'), 'site_name': r.get('site_name'),
                               'status': r.get('status'), 'reason': r.get('failure_reason')}
                              for r in rows if r.get('site_id') not in SITES and r.get('site_id') != 'doctorsmm']}


def measured_row(row: dict, campaign_status: str) -> dict:
    """Keep measured net changes separate from unknown provider attribution."""
    submitted = row.get('submitted_at')
    baseline = row.get('baseline_count')
    samples = row.get('samples') or []
    after = [s for s in samples if submitted is not None and s.get('observed_at', 0) >= submitted]
    latest = after[-1] if after else None
    last_count = row.get('final_count')
    if campaign_status in ('running', 'stopping') and latest is not None:
        last_count = latest.get('count')
    numeric = lambda x: isinstance(x, int) and not isinstance(x, bool) and x >= 0
    observed = (last_count - baseline if latest is not None and numeric(last_count) and numeric(baseline)
                else None)
    first = next((s for s in after if numeric(s.get('count')) and numeric(baseline)
                  and s['count'] > baseline), None)
    elapsed = round(first['observed_at'] - submitted, 1) if first is not None else None
    return {
        'site_id': row.get('site_id'), 'site_name': row.get('site_name'),
        'service_url': row.get('service_url'), 'service_label': row.get('service_label'),
        'verifier_status': row.get('status'), 'campaign_status': campaign_status,
        'baseline_count': baseline, 'final_count': last_count,
        'observed_counter_change': observed, 'first_observed_increase_sec': elapsed,
        'attributed_delivery_count': None, 'delivery_source': 'не установлена',
        'submitted_at': submitted, 'finished_at': row.get('finished_at'),
        'failure_reason': row.get('failure_reason'), 'attempts': copy.deepcopy(row.get('attempts') or []),
        'samples': copy.deepcopy(samples), 'attribution_warning': UNCERTAINTY,
    }


def can_continue(campaign: dict, row: dict, now: float | None = None) -> tuple[bool, str]:
    """No later site if the counter or the previous driver remains unsafe."""
    now = time.time() if now is None else now
    if campaign.get('status') == 'stopped':
        return False, 'Предыдущая кампания остановлена'
    if campaign.get('status') == 'completed':
        if row.get('status') in ('working', 'not_working', 'skipped', 'rejected'):
            return True, ''
        return False, 'Некорректный итог предыдущей площадки'
    if campaign.get('status') != 'failed' or row.get('status') != 'unverified':
        return False, 'Кампания завершилась без безопасного результата'
    submitted = row.get('submitted_at')
    attempts = row.get('attempts') or []
    samples = row.get('samples') or []
    if submitted is None or not samples:
        return False, 'Нет точного счётчика или записи возможной отправки'
    windows = [a.get('observation_window_sec') or 0 for a in attempts if a.get('submitted_at') is not None]
    if not windows or max(windows) < 900:
        return False, 'Наблюдение после возможной отправки недостаточно долгое'
    last = samples[-1]
    observed_at = last.get('observed_at', 0)
    if (observed_at < submitted + max(windows) - 5 or observed_at > now + 60
            or now - observed_at > 120 or last.get('count') != row.get('final_count')):
        return False, 'Нет свежего достоверного измерения в конце окна наблюдения'
    return True, ''


def _number(x) -> str:
    return 'нет данных' if x is None else str(x)


def render_report(state: dict) -> str:
    prior = state.get('prior') or {}
    doctor = prior.get('doctor') or {}
    lines = [f"Ссылка: {TARGET}", f"Состояние продолжения: {state.get('phase', 'подготовка')}",
             f"Предыдущий запуск: https://github.com/{REPO}/actions/runs/{PRIOR_RUN}",
             f"Исходный счётчик первого запуска: {_number(prior.get('initial_count'))}",
             'DoctorSMM: возможная отправка; повтор не выполняется.',
             f"  До: {_number(doctor.get('baseline_count'))}; после первого окна: {_number(doctor.get('final_count'))}; "
             'доставка неизвестна.', UNCERTAINTY]
    if doctor.get('submitted_at') is not None and doctor.get('last_sample_at') is not None:
        lines.append(f"  Возможное действие Unix: {doctor['submitted_at']:.0f}; последний замер первого окна: "
                     f"{doctor['last_sample_at']:.0f} (через {doctor['last_sample_at'] - doctor['submitted_at']:.0f} с).")
    for other in prior.get('other_results') or []:
        lines.append(f"{other.get('site_name') or other.get('site_id')}: {other.get('reason') or other.get('status')}")
    qs = state.get('quarantine_samples') or []
    if qs:
        lines += ['', f"Наблюдение после DoctorSMM: {len(qs)} замеров; "
                      f"первый {_number(qs[0].get('count'))}; последний {_number(qs[-1].get('count'))}.",
                  f"  Первый замер Unix: {qs[0].get('observed_at')}; последний: {qs[-1].get('observed_at')}."]
    if state.get('quarantine_deadline'):
        lines.append(f"  Не переходить к заявкам раньше Unix {state['quarantine_deadline']:.0f}; "
                     f"дополнительно нужно {STABLE_SECONDS} с стабильного счётчика.")
    rows = list(state.get('results') or [])
    current = state.get('current') or {}
    if current.get('row'):
        rows.append(current['row'])
    elif current.get('site_id'):
        lines += ['', 'Сейчас: ' + str(current['site_id']) + ' — ' + str(current.get('phase') or 'подготовка')]
    labels = {'working': 'наблюдалось увеличение счётчика',
              'not_working': 'за окно не обнаружен чистый прирост',
              'unverified': 'результат заявки неизвестен', 'skipped': 'не проверено',
              'rejected': 'заявка отклонена площадкой', 'running': 'выполняется',
              'pending': 'ожидает'}
    for row in rows:
        change = row.get('observed_counter_change')
        label = labels.get(row.get('verifier_status'), row.get('verifier_status') or 'подготовка')
        lines += ['', f"{row.get('site_name') or row.get('site_id')} — {label}",
                  f"  Страница: {row.get('service_url') or 'нет данных'}",
                  f"  Было: {_number(row.get('baseline_count'))}; стало: {_number(row.get('final_count'))}; "
                  f"изменение публичного счётчика: {_number(change)}.",
                  f"  Первое обнаруженное увеличение счётчика: {_number(row.get('first_observed_increase_sec'))}" +
                  (' с после возможного действия.' if row.get('first_observed_increase_sec') is not None else '.'),
                  '  Доставка именно этой площадкой: неизвестна; источник роста не установлен.']
        if row.get('failure_reason'):
            lines.append('  Причина/ограничение: ' + str(row['failure_reason']))
        for attempt in row.get('attempts') or []:
            lines.append(f"  Попытка: {attempt.get('method')} / {attempt.get('route')}: "
                         f"{attempt.get('verdict')} — {attempt.get('detail') or ''}")
            if attempt.get('observation_window_sec') is not None:
                lines.append(f"  Окно наблюдения после драйвера: {attempt['observation_window_sec']} с.")
        samples = row.get('samples') or []
        if samples:
            lines.append('  Измерения (Unix-время: значение): ' + ', '.join(
                f"{int(x['observed_at'])}: {x['count']}" for x in samples))
    finished = {r.get('site_id') for r in state.get('results') or []}
    in_progress = (state.get('current') or {}).get('site_id')
    remaining = [site for site in state.get('queue') or [] if site not in finished and site != in_progress]
    if remaining:
        lines += ['', 'Ещё не проверены: ' + ', '.join(remaining) + '.']
    if state.get('error'):
        lines += ['', 'Проверка остановлена: ' + str(state['error'])]
    lines += ['', 'Отсутствие данных о доставке не является нулём. '
                  'Нулевое изменение счётчика относится только к указанному интервалу наблюдения.',
              'Время обнаружения включает задержку опроса и не доказывает время начала доставки.']
    if state.get('actions_run_id'):
        lines += ['', f"Продолжение: https://github.com/{REPO}/actions/runs/{state['actions_run_id']}"]
    return '\n'.join(lines) + '\n'


def write_snapshot(state: dict, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for name, text in (('vk-continuation.json', json.dumps(state, ensure_ascii=False, indent=2) + '\n'),
                       ('vk-continuation.txt', render_report(state))):
        path = out / name
        temp = path.with_name(path.name + '.tmp')
        temp.write_text(text, encoding='utf-8')
        os.replace(temp, path)


def publish(publisher: GitHubProgress, state: dict, out: Path, *, conclusion: str | None = None) -> None:
    state['updated_at'] = time.time()
    write_snapshot(state, out)
    publisher.update(state, title='Последовательные измерения: доставка не приписывается без доказательств',
                     conclusion=conclusion)


def configure() -> None:
    db.init_db()
    for key, value in {'captcha_browser_visible': '0', 'browser_backend': 'playwright',
                       'captcha_mode': 'all', 'captcha_autosolve': '0', 'captcha_ocr': '0',
                       'nojs_enabled': '0', 'order_email': '', 'proxy_rotation': '0',
                       'verify_timeout_sec': '900', 'verify_smm8_timeout_sec': '3600',
                       'verify_poll_sec': '15'}.items():
        db.set_setting(key, value)
    with db.tx() as conn:
        conn.execute('UPDATE sites SET enabled=0')


def wait_for_prior(state: dict, publisher: GitHubProgress, out: Path, cancelled: list[bool], *,
                   read_count, clock=time.time, sleep=time.sleep) -> None:
    submitted = state['prior']['doctor']['submitted_at']
    deadline = submitted + PRIOR_WAIT_SECONDS
    state['quarantine_deadline'] = deadline
    state['phase'] = 'наблюдение после возможной отправки DoctorSMM'
    stable_since = None
    last_count = None
    error_since = None
    limit = max(deadline + 1800, clock() + 1800)
    while True:
        now = clock()
        if cancelled[0]:
            raise RuntimeError('Получен сигнал остановки; новых заявок не будет')
        if now > limit:
            raise RuntimeError('Счётчик не стабилизировался в допустимое время; новых заявок нет')
        try:
            count, label, source = read_count()
            now = clock()
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise delivery.CounterError('Некорректный счётчик')
            if error_since is not None or last_count != count:
                stable_since = now
            last_count = count
            error_since = None
            state['quarantine_samples'].append({'observed_at': now, 'count': count,
                                                'source': source, 'route': label})
            state['counter_error'] = None
        except delivery.CounterError as exc:
            now = clock()
            stable_since = None
            error_since = error_since or now
            state['counter_error'] = verifier._safe_detail(str(exc))
            if now - error_since >= 180:
                raise RuntimeError('Счётчик недоступен; новых заявок нет') from exc
        publish(publisher, state, out)
        if (now >= deadline and error_since is None and stable_since is not None
                and now - stable_since >= STABLE_SECONDS):
            return
        sleep(POLL_SECONDS)


def ensure_not_repeated(site: str, done_sites: list[str]) -> None:
    if site in done_sites or site not in SITES:
        raise RuntimeError('Повторная или незапланированная площадка запрещена')
    with db.rd() as conn:
        rows = conn.execute('SELECT site_id, submitted_at, attempts_json FROM campaign_results').fetchall()
    for row in rows:
        attempts = json.loads(row['attempts_json'] or '[]')
        possible = row['submitted_at'] is not None or any(a.get('submitted_at') is not None for a in attempts)
        if possible and row['site_id'] == site:
            raise RuntimeError('Для этой площадки уже была возможная отправка')
        if possible and row['site_id'] not in done_sites:
            raise RuntimeError('Обнаружена возможная отправка вне завершённой очереди')


def run_site(site: str, state: dict, publisher: GitHubProgress, out: Path,
             cancelled: list[bool], deadline: float) -> tuple[dict, dict]:
    ensure_not_repeated(site, [r['site_id'] for r in state['results']])
    if cancelled[0] or time.monotonic() >= deadline:
        raise RuntimeError('Получена остановка; новая площадка не запускается')
    with db.tx() as conn:
        if conn.execute('SELECT COUNT(*) FROM sites WHERE id=?', (site,)).fetchone()[0] != 1:
            raise RuntimeError('Площадка отсутствует в каталоге: ' + site)
        conn.execute('UPDATE sites SET enabled=CASE WHEN id=? THEN 1 ELSE 0 END', (site,))
    state['current'] = {'site_id': site, 'phase': 'снимаем исходный счётчик'}
    publish(publisher, state, out)  # never open the next site without a recoverable state
    if cancelled[0] or time.monotonic() >= deadline:
        raise RuntimeError('Получена остановка; новая площадка не запускается')
    # This acknowledgement only affects this isolated temporary CI database.
    # Earlier possible orders remain visible as uncertainties in every report.
    started = verifier.start(TARGET, acknowledge_risk=True)
    cid = started['campaign_id']
    stop_sent = False
    last_published = 0.0
    try:
        while True:
            campaign = verifier.status(cid)
            if not campaign:
                raise RuntimeError('Кампания исчезла из временной базы')
            matching = [r for r in campaign.get('results', []) if r.get('site_id') == site]
            if len(matching) != 1:
                raise RuntimeError('Не удалось однозначно определить текущую площадку')
            row = matching[0]
            state['phase'] = 'последовательная проверка'
            state['current'] = {'site_id': site, 'phase': campaign.get('current_phase'),
                                'campaign_id': cid, 'row': measured_row(row, campaign['status'])}
            if time.monotonic() - last_published >= 20 or campaign['status'] not in ('running', 'stopping'):
                publish(publisher, state, out)
                last_published = time.monotonic()
            if campaign['status'] not in ('running', 'stopping'):
                # _worker records terminal status immediately before releasing AUTO_LOCK.
                until = time.monotonic() + 30
                while executor.AUTO_LOCK.locked() and time.monotonic() < until:
                    time.sleep(.2)
                if executor.AUTO_LOCK.locked():
                    raise RuntimeError('Драйвер предыдущей площадки ещё не завершён')
                return campaign, row
            if (cancelled[0] or time.monotonic() >= deadline) and not stop_sent:
                stop_sent = True
                verifier.stop(cid)
            time.sleep(3)
    except BaseException:
        # Publication, signal and runtime errors must never leave another site able to start.
        try:
            current = verifier.status(cid)
            if current and current.get('status') in ('running', 'stopping'):
                verifier.stop(cid)
        except Exception:
            pass
        raise


def run(output_dir: Path) -> int:
    permitted = {'GITHUB_ACTIONS': 'true', 'VKKK_LIVE_RUN': 'authorized-once',
                 'VKKK_CONTINUATION': 'previously-unsent-with-attribution-warning',
                 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_EVENT_NAME': 'push',
                 'GITHUB_REPOSITORY': REPO, 'GITHUB_REF': 'refs/heads/arena/e372c48b-vkkk'}
    if any(os.environ.get(k) != v for k, v in permitted.items()):
        raise RuntimeError('Продолжение разрешено только из одноразового CI-задания')
    token = os.environ.get('VKKK_PROGRESS_TOKEN', '')
    run_id = os.environ.get('GITHUB_RUN_ID', '')
    if not token or not run_id.isdigit():
        raise RuntimeError('Нет канала проверяемого состояния')
    previous_check = github_get(f'/check-runs/{PRIOR_CHECK}', token)
    previous_workflow = github_get(f'/actions/runs/{PRIOR_RUN}', token)
    prior = validate_previous(previous_check, previous_workflow)
    state = {'phase': 'подготовка', 'actions_run_id': run_id, 'target': TARGET, 'prior': prior,
             'queue': list(SITES), 'quarantine_samples': [], 'results': [], 'current': None,
             'attribution_warning': UNCERTAINTY, 'error': None}
    publisher = GitHubProgress(name=CHECK_NAME)
    publisher.create(state, title='Продолжение готовится; прежняя доставка неизвестна')
    cancelled = [False]
    def request_stop(signum, frame):
        del signum, frame
        cancelled[0] = True
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    deadline = time.monotonic() + MAX_RUN_SECONDS
    try:
        configure()
        available = stealth.backend_status()
        if not any(x.get('id') == 'playwright' and x.get('ready') for x in available['backends']):
            raise RuntimeError('Playwright/Chromium недоступен; новых заявок нет')
        with stealth.open_page(headless=True, prefer='playwright') as (backend, page):
            if backend != 'playwright' or page.js('1 + 1') != 2:
                raise RuntimeError('Браузер не прошёл проверку; новых заявок нет')
        vk = {'platform': 'vk', 'kind': 'community', 'url': TARGET}
        route = network.Endpoint(id='direct', kind='direct', label='Прямое соединение')
        wait_for_prior(state, publisher, output_dir, cancelled,
                       read_count=lambda: verifier._counter(vk, [route]))
        for site in SITES:
            if cancelled[0] or time.monotonic() >= deadline:
                raise RuntimeError('Исчерпан бюджет времени; новые заявки не запускаются')
            campaign, row = run_site(site, state, publisher, output_dir, cancelled, deadline)
            state['results'].append(measured_row(row, campaign['status']))
            state['current'] = None
            publish(publisher, state, output_dir)
            okay, reason = can_continue(campaign, row)
            if not okay:
                raise RuntimeError(reason)
        state['phase'] = 'завершено'
        publish(publisher, state, output_dir, conclusion='success')
        notice('CONTINUATION_FINISHED', {'sites_attempted': len(state['results']),
                                         'attribution': 'не установлена'})
        return 0
    except Exception as exc:
        state['phase'] = 'остановлено'
        state['error'] = verifier._safe_detail(str(exc) or type(exc).__name__)
        write_snapshot(state, output_dir)
        try:
            publish(publisher, state, output_dir, conclusion='failure')
        except ProgressError:
            pass
        notice('CONTINUATION_STOPPED', {'reason': state['error'], 'sites_attempted': len(state['results'])})
        return 2


def main() -> int:
    parser = argparse.ArgumentParser(description='Продолжить проверку только ранее не отправленных площадок')
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    try:
        return run(args.output_dir)
    except Exception as exc:
        notice('CONTINUATION_BLOCKED', {'reason': verifier._safe_detail(str(exc) or type(exc).__name__)})
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
