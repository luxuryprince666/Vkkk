#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read two public VK counters after the completed Boost experiment.

This diagnostic uses only GETs for the known community and GitHub evidence.
It opens no provider page, enters no target into a form and sends no order.
The two VK sources are reported independently; an unavailable source or a
mismatch must never be represented as a consensus or as a zero count.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import delivery, verifier  # noqa: E402
from tools import run_boost_after_resume_ci as boost, run_resume_after_cancel_ci as resume  # noqa: E402
from tools.ci_progress import GitHubProgress, ProgressError  # noqa: E402
from tools.run_unsent_followup_ci import REPO, TARGET, github_get  # noqa: E402

GROUP_ID = '243396555'
BOOST_RUN = 44821581807
BOOST_CHECK = 136654434098
BOOST_SHA = '521d9e1caf12d53cf25ae0c38193a096035a8609'
BRANCH = 'arena/e372c48b-vkkk'
CHECK_NAME = 'VK post-Boost counter check (read only)'


def validate_previous(workflow: dict, check: dict) -> dict:
    """Use the exact completed Boost snapshot, not a claim from a live feed."""
    resume._completed(workflow, BOOST_RUN, BOOST_SHA, 'success')
    state = resume._check_snapshot(check, BOOST_CHECK, BOOST_SHA, boost.CHECK_NAME)
    start = state.get('start') or {}
    complete = state.get('complete') or {}
    observation = state.get('observation') or {}
    samples = state.get('samples') or []
    baseline, final = state.get('baseline_count'), observation.get('final_count')
    started_at, completed_at = start.get('possible_at'), complete.get('possible_at')
    last_at = samples[-1].get('observed_at') if samples and isinstance(samples[-1], dict) else None
    if (str(state.get('actions_run_id')) != str(BOOST_RUN) or state.get('target') != TARGET
            or state.get('phase') != 'завершено' or state.get('provider_claimed_order') is not True
            or start.get('request_released') is not True or complete.get('request_released') is not True
            or start.get('http_status') != 200 or complete.get('http_status') != 200
            or type(baseline) is not int or type(final) is not int or final <= baseline
            or observation.get('status') != 'working' or observation.get('net_change') != final - baseline
            or type(observation.get('window_sec')) not in (int, float) or observation['window_sec'] < 3600
            or any(type(t) not in (int, float) for t in (started_at, completed_at, last_at))
            or not 0 < started_at <= completed_at <= last_at
            or last_at - completed_at < 3600
            or not samples or samples[-1].get('count') != final):
        raise ValueError('Предыдущий измеренный результат Boost не подтверждён')
    return {'run_id': BOOST_RUN, 'check_id': BOOST_CHECK, 'baseline': baseline,
            'final_count': final, 'last_sample_at': samples[-1]['observed_at'],
            'possible_start_at': start.get('possible_at'), 'possible_complete_at': complete.get('possible_at'),
            'provider_claimed_order': True}


def collect(readers, *, clock=time.time) -> dict:
    """Read sources independently and do not invent a consensus on failure."""
    samples = []
    for source, reader in readers:
        try:
            count = reader(GROUP_ID)
            if type(count) is not int or count < 0:
                raise ValueError('Некорректный счётчик')
            samples.append({'source': source, 'observed_at': clock(), 'count': count, 'error': None})
        except Exception as exc:
            # No exception body, URL, credentials or response page goes into a public check.
            samples.append({'source': source, 'observed_at': clock(), 'count': None,
                            'error': type(exc).__name__})
    known = [s['count'] for s in samples if s['count'] is not None]
    agrees = (len(set(known)) == 1) if len(known) >= 2 else None
    return {'samples': samples, 'sources_agree': agrees,
            'consensus_count': known[0] if agrees is True else None}


def render(state: dict) -> str:
    prior = state.get('prior') or {}
    show = lambda value: 'нет данных' if value is None else str(value)
    lines = [f'Ссылка: {TARGET}', f"Состояние: {state.get('phase', 'подготовка')}",
             'Только чтение публичных страниц VK; новые заявки не отправлялись.',
             f"Предыдущий подтверждённый страницей заказ: https://github.com/{REPO}/actions/runs/{BOOST_RUN}",
             f"Предыдущее окно: {show(prior.get('baseline'))} → {show(prior.get('final_count'))}; "
             'источник роста счётчиком не доказан.', '']
    for sample in state.get('samples') or []:
        line = f"{sample['source']}: {show(sample.get('count'))}; Unix-время {show(sample.get('observed_at'))}"
        if sample.get('error'):
            line += '; ошибка чтения: ' + str(sample['error'])
        lines.append(line + '.')
    agreed = state.get('sources_agree')
    lines.append('Согласованное число: ' + show(state.get('consensus_count')) +
                 (' (оба источника совпали).' if agreed is True else
                  ' (источники расходятся).' if agreed is False else ' (недостаточно двух источников).'))
    if state.get('reason'):
        lines.append('Ограничение: ' + str(state['reason']))
    if state.get('actions_run_id'):
        lines.append(f"Проверка: https://github.com/{REPO}/actions/runs/{state['actions_run_id']}")
    return '\n'.join(lines) + '\n'


def save(state: dict, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for name, value in (('vk-counter.json', json.dumps(state, ensure_ascii=False, indent=2) + '\n'),
                        ('vk-counter.txt', render(state))):
        dest = out / name
        tmp = dest.with_name(dest.name + '.tmp')
        tmp.write_text(value, encoding='utf-8')
        os.replace(tmp, dest)


def run(out: Path) -> int:
    required = {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'push', 'GITHUB_RUN_ATTEMPT': '1',
                'GITHUB_REPOSITORY': REPO, 'GITHUB_REF': 'refs/heads/' + BRANCH}
    if any(os.environ.get(k) != v for k, v in required.items()):
        raise RuntimeError('Диагностика разрешена только в указанной ветке Actions')
    token, run_id = os.environ.get('VKKK_PROGRESS_TOKEN', ''), os.environ.get('GITHUB_RUN_ID', '')
    if not token or not run_id.isdigit():
        raise RuntimeError('Нет канала проверяемого состояния')
    state = {'phase': 'подготовка', 'actions_run_id': run_id, 'target': TARGET,
             'orders_sent': 0, 'samples': [], 'sources_agree': None, 'consensus_count': None}
    publisher = GitHubProgress(name=CHECK_NAME)
    publisher.create(state, title='Проверка публичного счётчика без заявок')
    try:
        state['prior'] = validate_previous(
            github_get(f'/actions/runs/{BOOST_RUN}', token), github_get(f'/check-runs/{BOOST_CHECK}', token))
        state.update(collect((('виджет VK', delivery.read_vk_widget),
                              ('мобильная страница VK', delivery.read_vk_page))))
        if all(s['count'] is None for s in state['samples']):
            raise RuntimeError('Оба публичных источника счётчика недоступны')
        state['phase'] = 'завершено'
        save(state, out)
        publisher.update(state, title='Источники VK проверены без заявок', conclusion='success')
        return 0
    except Exception as exc:
        state.update(phase='остановлено', reason=verifier._safe_detail(str(exc) or type(exc).__name__))
        save(state, out)
        try:
            publisher.update(state, title='Диагностика остановлена без заявок', conclusion='failure')
        except ProgressError:
            pass
        return 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, required=True)
    try:
        return run(parser.parse_args().output_dir)
    except Exception as exc:
        print('Read-only counter blocked:', verifier._safe_detail(str(exc) or type(exc).__name__))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
