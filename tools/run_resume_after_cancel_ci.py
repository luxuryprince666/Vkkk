#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Resume one VK experiment after a cancelled driver, without retrying it.

The prior runner may have acted after its last snapshot. Prtut, Avi1, Nakruti
and DoctorSMM are never re-ordered. We wait an hour after the prior runner
finished, require a stable exact counter, then try only specifically selected
previously unsent sites one by one. A counter change is not proof of causality.
"""
from __future__ import annotations

import copy
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import db, network, stealth, verifier  # noqa: E402
from tools import run_continuation_ci as continuation  # noqa: E402
from tools.ci_progress import GitHubProgress, ProgressError, decode_snapshot  # noqa: E402
from tools.run_live_ci import notice  # noqa: E402
from tools.run_unsent_followup_ci import (  # noqa: E402
    MARKER, PRIOR_CHECK, PRIOR_RUN, REPO, TARGET, github_get,
)

CANCEL_RUN = 44771191791
CANCEL_SHA = '3709851d4c4831c24f9b2d57eb78ee8f3af8767e'
RECOVERY_RUN = 44782989466
RECOVERY_CHECK = 136450360836
RECOVERY_SHA = 'fff7ab682877db80a4c9456d8c95f053389fa2ab'
RECOVERY_ARTIFACT = 14651164651
EXTRA_RUN = 44785954124
EXTRA_CHECK = 136460832549
EXTRA_SHA = '115b02ebb6cc72bf27bd369e63a024248fe887aa'
PREFLIGHT_RUN = 44789182092
PREFLIGHT_CHECK = 136472080961
PREFLIGHT_SHA = 'd79aa803978a7cdf2d72b05cfd4e9c9ff3a59882'
CHECK_NAME = 'VK resumed sequential measurement snapshot'
WORKFLOW = 'vk-resume-after-cancel.yml'
BRANCH = 'arena/e372c48b-vkkk'
QUEUE = ('pricesmm', 'sprintersmm', 'lowcostsmm', 'martinismm', 'capitansmm', 'captainsmm', 'smm8')
EXTRA_SITES = {'LowcostSMM': 'lowcostsmm.com', 'MartiniSMM': 'martinismm.com',
               'CapitanSMM': 'capitansmm.com', 'CaptainSMM': 'captainsmm.com'}
EXPECTED_URLS = {
    'pricesmm': 'https://pricesmm.com/prodvizhenie/nakrutka-podpischikov-v-gruppu-vk-besplatno/',
    'sprintersmm': 'https://sprintersmm.com/raskrutka/nakrutka-podpischikov-v-gruppu-vk-besplatno/',
    'lowcostsmm': 'https://lowcostsmm.com/ru/free/nakrutka-podpischikov-v-gruppu-vk-besplatno/',
    'martinismm': 'https://martinismm.com/social-media/nakrutka-podpischikov-v-gruppu-vk-besplatno/',
    'capitansmm': 'https://capitansmm.com/ru/smm/nakrutka-podpischikov-v-gruppu-vk-besplatno/',
    'captainsmm': 'https://captainsmm.com/ru/nakrutka/nakrutka-podpischikov-v-gruppu-vk-besplatno/',
    'smm8': 'https://smm8.com/free-vk-followers',
}
WARNING = ('Возможна запоздалая доставка DoctorSMM, Avi1, Nakruti.net или прерванного Prtut; '
           'публичный счётчик не позволяет достоверно установить источник роста.')


def _check_snapshot(check: dict, check_id: int, sha: str, name: str) -> dict:
    if (check.get('id') != check_id or check.get('head_sha') != sha or check.get('name') != name
            or check.get('status') != 'completed' or check.get('conclusion') != 'success'):
        raise ValueError('Не найден ожидаемый проверенный снимок')
    summary = (check.get('output') or {}).get('summary') or ''
    if MARKER not in summary:
        raise ValueError('В проверенном снимке нет данных')
    return decode_snapshot(summary.split(MARKER, 1)[1].strip())


def _completed(workflow: dict, run_id: int, sha: str, conclusion: str) -> None:
    if (workflow.get('id') != run_id or workflow.get('head_sha') != sha
            or workflow.get('status') != 'completed' or workflow.get('conclusion') != conclusion
            or workflow.get('run_attempt') != 1):
        raise ValueError('Предыдущий workflow не совпадает с ожидаемым')


def validate_evidence(cancel_run: dict, recovery_run: dict, recovery_check: dict,
                      first_run: dict, first_check: dict, extra_run: dict, extra_check: dict,
                      preflight_run: dict, preflight_check: dict, *, now: float | None = None) -> dict:
    """Fail closed unless all previous actions and newly inspected forms match."""
    now = time.time() if now is None else now
    _completed(cancel_run, CANCEL_RUN, CANCEL_SHA, 'cancelled')
    _completed(recovery_run, RECOVERY_RUN, RECOVERY_SHA, 'success')
    _completed(extra_run, EXTRA_RUN, EXTRA_SHA, 'success')
    _completed(preflight_run, PREFLIGHT_RUN, PREFLIGHT_SHA, 'success')
    prior = continuation.validate_previous(first_check, first_run)
    recovered = _check_snapshot(recovery_check, RECOVERY_CHECK, RECOVERY_SHA,
                                'VK cancelled continuation artifact recovery (read only)')
    metadata = recovered.get('artifact_recovery') or {}
    if (str(recovered.get('actions_run_id')) != str(CANCEL_RUN) or recovered.get('target') != TARGET
            or metadata.get('source_run') != CANCEL_RUN or metadata.get('artifact_id') != RECOVERY_ARTIFACT
            or metadata.get('workflow_conclusion') != 'cancelled' or metadata.get('orders_sent') != 0
            or (recovered.get('prior') or {}).get('doctor') != prior.get('doctor')):
        raise ValueError('Отчёт отменённой кампании неполный или относится к другой ссылке')
    try:
        ended_at = datetime.fromisoformat(cancel_run['updated_at'].replace('Z', '+00:00')).timestamp()
        updated_at = float(recovered['updated_at'])
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError('Неизвестно время остановки предыдущего процесса') from exc
    if ended_at < updated_at or ended_at > now + 60 or ended_at <= 0:
        raise ValueError('Некорректный интервал остановки предыдущего процесса')
    results = recovered.get('results')
    expected = ('smmnakrutka', 'likeboom', 'avi1', 'nakruti', 'mrpopular')
    if not isinstance(results, list) or tuple(r.get('site_id') for r in results) != expected:
        raise ValueError('Неожиданный список предыдущих площадок')
    for row in results:
        attempts = row.get('attempts') or []
        possible = [a for a in attempts if a.get('submitted_at') is not None]
        if row.get('site_id') in ('avi1', 'nakruti'):
            if len(possible) != 1 or row.get('submitted_at') != possible[0].get('submitted_at'):
                raise ValueError('Не сохранилась возможная предыдущая отправка')
        elif row.get('submitted_at') is not None or possible or row.get('verifier_status') != 'skipped':
            raise ValueError('Неожиданная возможная предыдущая отправка')
    current = recovered.get('current') or {}
    current_row = current.get('row') or {}
    if (current.get('site_id') != 'prtut' or current_row.get('site_id') != 'prtut'
            or current_row.get('verifier_status') not in ('running', 'pending')):
        raise ValueError('Прерванная площадка не установлена')
    # Даже если submitted_at пуст, текущий Prtut мог действовать между снимком
    # и отменой. При этом зафиксированный SHA старого run_site атомарно пишет
    # локальный JSON с именем СЛЕДУЮЩЕГО сайта до verifier.start; известный
    # артефакт загружен уже после остановки шага и всё ещё показывает Prtut.
    # Поэтому PriceSMM и SMM8 не были начаты, а сам Prtut считать возможной
    # отправкой и не повторять. Статический список queue сам по себе ничего
    # не доказывает; здесь важны также current и происхождение артефакта.
    previous_sites = {r['site_id'] for r in results} | {'prtut', 'doctorsmm'}
    if set(QUEUE) & previous_sites or len(set(QUEUE)) != len(QUEUE):
        raise ValueError('Очередь содержит ранее возможно отправленную площадку')
    if not all(s in (recovered.get('queue') or []) for s in ('pricesmm', 'smm8')):
        raise ValueError('Не подтверждены ранее не начатые PriceSMM и SMM8')

    inspected = _check_snapshot(extra_check, EXTRA_CHECK, EXTRA_SHA, 'VK additional free sites read-only')
    if inspected.get('orders_sent') != 0 or inspected.get('phase') != 'завершено':
        raise ValueError('Дополнительные формы не прошли осмотр без заявок')
    by_name = {x.get('site'): x for x in inspected.get('sites') or []}
    for name, host in EXTRA_SITES.items():
        row = by_name.get(name) or {}
        structure = row.get('structure') or {}
        candidate = row.get('candidate_check') or {}
        if (row.get('orders_sent') != 0 or not (row.get('final') or '').startswith('https://' + host + '/')
                or (structure.get('selectedPublicService') or {}).get('id') != '8559'
                or candidate.get('free_guard_on_empty_field') is not True
                or not isinstance(candidate.get('visible_countdown_sec'), int)
                or candidate['visible_countdown_sec'] <= 0):
            raise ValueError('Бесплатная форма не подтверждена read-only осмотром: ' + name)

    preflight = _check_snapshot(preflight_check, PREFLIGHT_CHECK, PREFLIGHT_SHA,
                                'VK safe resume preflight (read only)')
    if (preflight.get('phase') != 'завершено' or preflight.get('orders_sent') != 0
            or preflight.get('target') != TARGET or preflight.get('prior_cancel_run') != CANCEL_RUN
            or preflight.get('prior_recovery_check') != RECOVERY_CHECK or preflight.get('extra_check') != EXTRA_CHECK
            or tuple(preflight.get('queue') or []) != QUEUE):
        raise ValueError('Preflight без заявок не соответствует продолжению')
    inspected2 = {x.get('site'): x for x in preflight.get('inspected_forms_without_input') or []}
    for name in ('PriceSMM', 'SprinterSMM'):
        row = inspected2.get(name) or {}
        if (row.get('orders_sent') != 0 or row.get('free_guard_on_empty_field') is not True
                or (row.get('structure', {}).get('selectedPublicService') or {}).get('id') != '8559'
                or not isinstance(row.get('countdown_sec'), int) or row['countdown_sec'] <= 0):
            raise ValueError('Не подтверждён предварительный осмотр бесплатной формы: ' + name)
    counts = preflight.get('counter_samples') or []
    if (len(counts) < 2 or any(type(x.get('count')) is not int or x['count'] < 0
                               or isinstance(x.get('observed_at'), bool)
                               or not isinstance(x.get('observed_at'), (int, float))
                               or not ended_at <= x['observed_at'] <= now + 60 for x in counts)
            or counts[-1]['observed_at'] < counts[0]['observed_at']):
        raise ValueError('В preflight нет точных публичных счётчиков после отмены')
    return {'prior': prior, 'recovered': recovered, 'ended_at': ended_at,
            'last_snapshot_at': updated_at, 'inspected_count': counts[-1]['count']}


def validate_catalog(catalog: dict) -> None:
    sites = catalog.get('sites') or []
    for site_id, expected_url in EXPECTED_URLS.items():
        rows = [x for x in sites if x.get('id') == site_id]
        if len(rows) != 1 or rows[0].get('reg_required'):
            raise ValueError('Площадка отсутствует, дублируется или требует вход: ' + site_id)
        if site_id in {'lowcostsmm', 'martinismm', 'capitansmm', 'captainsmm', 'sprintersmm'} and rows[0].get('enabled'):
            raise ValueError('Новая непроверенная площадка должна быть отключена по умолчанию')
        services = [s for s in rows[0].get('services') or [] if s.get('platform') == 'vk'
                    and s.get('type') == 'followers' and s.get('subject') in ('community', 'any')]
        if (len(services) != 1 or services[0].get('url') != expected_url
                or services[0].get('id') != site_id + '-vk-followers'):
            raise ValueError('Услуга не совпадает с проверенной бесплатной страницей: ' + site_id)


def evidence(token: str) -> dict:
    return validate_evidence(
        github_get(f'/actions/runs/{CANCEL_RUN}', token),
        github_get(f'/actions/runs/{RECOVERY_RUN}', token),
        github_get(f'/check-runs/{RECOVERY_CHECK}', token),
        github_get(f'/actions/runs/{PRIOR_RUN}', token),
        github_get(f'/check-runs/{PRIOR_CHECK}', token),
        github_get(f'/actions/runs/{EXTRA_RUN}', token),
        github_get(f'/check-runs/{EXTRA_CHECK}', token),
        github_get(f'/actions/runs/{PREFLIGHT_RUN}', token),
        github_get(f'/check-runs/{PREFLIGHT_CHECK}', token))


def ensure_one_run(token: str, current_id: int, *, attempts: int = 10, delay: float = 2) -> None:
    """Reject a rerun or a second push-triggered run, even after the first ends."""
    path = f'/actions/workflows/{WORKFLOW}/runs?per_page=100&branch={quote(BRANCH, safe="")}'
    for _ in range(attempts):
        data = github_get(path, token)
        runs = data.get('workflow_runs') or []
        if any(r.get('id') == current_id for r in runs):
            earlier = [r for r in runs if isinstance(r.get('id'), int) and r['id'] < current_id]
            if earlier:
                raise RuntimeError('Для этого одноразового workflow уже существует предыдущий запуск')
            return
        time.sleep(delay)  # GitHub's workflow-run index may lag its run-id endpoint.
    raise RuntimeError('Не удалось проверить отсутствие повторного запуска')


def run(output: Path) -> int:
    permitted = {'GITHUB_ACTIONS': 'true', 'VKKK_LIVE_RUN': 'authorized-once',
                 'VKKK_RESUME_AFTER_CANCEL': 'quarantine-and-unattempted-only',
                 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_EVENT_NAME': 'push',
                 'GITHUB_REPOSITORY': REPO, 'GITHUB_REF': 'refs/heads/' + BRANCH}
    if any(os.environ.get(k) != v for k, v in permitted.items()):
        raise RuntimeError('Продолжение разрешено только для одноразового CI-запуска')
    token, run_id = os.environ.get('VKKK_PROGRESS_TOKEN', ''), os.environ.get('GITHUB_RUN_ID', '')
    if not token or not run_id.isdigit():
        raise RuntimeError('Нет канала проверяемого состояния')
    ensure_one_run(token, int(run_id))
    found = evidence(token)
    catalog = json.loads((ROOT / 'data/catalog.json').read_text(encoding='utf-8'))
    validate_catalog(catalog)
    previous = found['recovered']
    state = {'phase': 'подготовка', 'actions_run_id': run_id, 'target': TARGET,
             'prior': found['prior'], 'prior_continuation': {
                 'run_url': f'https://github.com/{REPO}/actions/runs/{CANCEL_RUN}',
                 'ended_at': found['ended_at'], 'last_snapshot_at': found['last_snapshot_at'],
                 'results': copy.deepcopy(previous['results']),
                 'interrupted_current': copy.deepcopy(previous['current'])},
             'queue': list(QUEUE), 'quarantine_samples': [], 'results': [], 'current': None,
             'quarantine_label': 'Наблюдение после остановки Prtut и предыдущих возможных заявок',
             'attribution_warning': WARNING, 'error': None}
    publisher = GitHubProgress(name=CHECK_NAME)
    publisher.create(state, title='Проверяем безопасное продолжение; происхождение роста неизвестно')
    cancelled = [False]
    def request_stop(signum, frame):
        del signum, frame
        cancelled[0] = True
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    deadline = time.monotonic() + continuation.MAX_RUN_SECONDS
    try:
        continuation.configure()
        with db.rd() as conn:
            known = {r['id'] for r in conn.execute('SELECT id FROM sites').fetchall()}
            for table in ('campaigns', 'campaign_results', 'tasks', 'deliveries'):
                if conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]:
                    raise RuntimeError('Временная база не пуста; новые заявки запрещены')
            for site_id, url in EXPECTED_URLS.items():
                rows = conn.execute("SELECT url FROM services WHERE site_id=? AND platform='vk' "
                                    "AND stype='followers' AND subject IN ('community','any')", (site_id,)).fetchall()
                if len(rows) != 1 or rows[0]['url'] != url:
                    raise RuntimeError('Временная база содержит другую услугу: ' + site_id)
        if not set(QUEUE) <= known:
            raise RuntimeError('Не все площадки очереди есть в каталоге')
        settings = verifier._settings()
        if settings['timeout'] < 900 or settings['smm8_timeout'] < 3600:
            raise RuntimeError('Окно наблюдения настроено слишком коротко')
        available = stealth.backend_status()
        if not any(x.get('id') == 'playwright' and x.get('ready') for x in available['backends']):
            raise RuntimeError('Playwright/Chromium недоступен; новых заявок нет')
        with stealth.open_page(headless=True, prefer='playwright') as (backend, page):
            if backend != 'playwright' or page.js('1 + 1') != 2:
                raise RuntimeError('Браузер не прошёл проверку; новых заявок нет')
        target = {'platform': 'vk', 'kind': 'community', 'url': TARGET}
        route = network.Endpoint(id='direct', kind='direct', label='Прямое соединение')
        continuation.wait_for_prior(
            state, publisher, output, cancelled, read_count=lambda: verifier._counter(target, [route]),
            after_timestamp=found['ended_at'], observation_phase='наблюдение после отменённого запуска')
        for site in QUEUE:
            if cancelled[0] or time.monotonic() >= deadline:
                raise RuntimeError('Время истекло или получена остановка; новые заявки не запускаются')
            campaign, row = continuation.run_site(site, state, publisher, output, cancelled, deadline, QUEUE)
            state['results'].append(continuation.measured_row(row, campaign['status']))
            state['current'] = None
            continuation.publish(publisher, state, output)
            okay, reason = continuation.can_continue(campaign, row)
            if not okay:
                raise RuntimeError(reason)
        state['phase'] = 'завершено'
        continuation.publish(publisher, state, output, conclusion='success')
        notice('RESUME_FINISHED', {'sites_attempted': len(state['results']), 'attribution': 'не установлена'})
        return 0
    except Exception as exc:
        state['phase'] = 'остановлено'
        state['error'] = verifier._safe_detail(str(exc) or type(exc).__name__)
        continuation.write_snapshot(state, output)
        try:
            continuation.publish(publisher, state, output, conclusion='failure')
        except ProgressError:
            pass
        notice('RESUME_STOPPED', {'reason': state['error'], 'sites_attempted': len(state['results'])})
        return 2


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description='Однократно проверить ранее не начатые VK-площадки')
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    try:
        return run(args.output_dir)
    except Exception as exc:
        notice('RESUME_BLOCKED', {'reason': verifier._safe_detail(str(exc) or type(exc).__name__)})
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
