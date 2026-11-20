#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One-shot, externally hosted live verification with recoverable check annotations.

This can place real orders. It is enabled only by the explicit CI environment and
uses the same sequential verifier, fresh counters and duplicate-order barriers as
the application. Never infer delivery from a successful HTTP response or form.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import signal
import sys
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import db, delivery, network, stealth, verifier  # noqa: E402


TERMINAL = {'working', 'not_working', 'unverified', 'skipped', 'rejected'}


def notice(kind: str, payload: dict) -> None:
    """Put structured, secret-free status into GitHub check annotations."""
    data = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    text = (kind + ' ' + data).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
    print('::notice title=VK live verification::' + text, flush=True)


def first_growth_sample(row: dict) -> dict | None:
    """Only count measurements made after the possible submission, not setup samples."""
    submitted, baseline = row.get('submitted_at'), row.get('baseline_count')
    if submitted is None or baseline is None:
        return None
    return next((s for s in row.get('samples', [])
                 if s['observed_at'] >= submitted and s['count'] > baseline), None)


def compact_result(row: dict) -> dict:
    return {
        'site': row.get('site_name'), 'site_id': row.get('site_id'),
        'service_url': row.get('service_url'), 'status': row.get('status'),
        'before': row.get('baseline_count'), 'after': row.get('final_count'),
        'change': row.get('added_count'), 'seconds_to_growth': row.get('time_to_growth_sec'),
        'possible_submit_at': row.get('submitted_at'), 'reason': row.get('failure_reason'),
        'attempts': [{k: a.get(k) for k in ('method', 'route', 'verdict', 'detail', 'submitted_at')}
                     for a in row.get('attempts', [])],
    }


def emit_report(campaign: dict, output_dir: Path) -> None:
    """Write the report and send compressed chunks to GitHub's check API.

    The separate artifact/log storage host is not necessarily reachable from the
    controller. Chunks contain exactly the report JSON, not the SQLite database.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    data = json.dumps(campaign, ensure_ascii=False, indent=2).encode('utf-8')
    (output_dir / 'vk-live-report.json').write_bytes(data + b'\n')
    (output_dir / 'vk-live-report.txt').write_text(verifier.format_report(campaign) + '\n', encoding='utf-8')
    encoded = base64.b64encode(zlib.compress(data, level=9)).decode('ascii')
    chunks = [encoded[i:i + 3500] for i in range(0, len(encoded), 3500)] or ['']
    for i, chunk in enumerate(chunks, 1):
        notice('REPORT_CHUNK', {'part': i, 'total': len(chunks), 'zlib_base64': chunk})
    notice('FINISHED', {'status': campaign.get('status'), 'initial': campaign.get('initial_count'),
                        'last': campaign.get('current_count'), 'error': campaign.get('error_reason'),
                        'sites': len(campaign.get('results') or [])})


def run(target: str, output_dir: Path, *, max_seconds: int = 18000, poll_seconds: float = 5) -> int:
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('VKKK_LIVE_RUN') != 'authorized-once':
        raise RuntimeError('Запуск реальных заявок разрешён только из одноразового CI-задания')
    db.init_db()
    # CI has no interactive screen; do not wait for an invisible human or invent an email.
    db.set_setting('captcha_browser_visible', '0')
    db.set_setting('browser_backend', 'playwright')
    db.set_setting('captcha_mode', 'all')
    db.set_setting('captcha_autosolve', '0')
    db.set_setting('captcha_ocr', '0')
    db.set_setting('nojs_enabled', '0')
    db.set_setting('order_email', '')
    available = stealth.backend_status()
    if not any(b.get('id') == 'playwright' and b.get('ready') for b in available['backends']):
        notice('BLOCKED', {'reason': 'Playwright/Chromium недоступен; заявок нет'})
        return 2
    # Module discovery alone does not prove that Chromium actually starts.
    try:
        with stealth.open_page(headless=True, prefer='playwright') as (backend, page):
            if backend != 'playwright' or page.js('1 + 1') != 2:
                raise RuntimeError('Браузер не прошёл проверку запуска')
    except Exception as exc:
        notice('BLOCKED', {'reason': 'Браузер не запускается; заявок нет',
                           'detail': verifier._safe_detail(str(exc))})
        return 2

    platform, kind, normalized = delivery.normalize_channel_input(target)
    if platform != 'vk' or kind != 'community':
        raise ValueError('Для этого CI-запуска нужна ссылка на сообщество ВК')
    vk = {'platform': platform, 'kind': kind, 'url': normalized}
    route = network.Endpoint(id='direct', kind='direct', label='Прямое соединение')
    try:
        first, _, first_source = verifier._counter(vk, [route])
        time.sleep(2)
        second, _, second_source = verifier._counter(vk, [route])
    except delivery.CounterError as exc:
        notice('BLOCKED', {'reason': 'Точный счётчик недоступен; заявок нет',
                           'detail': verifier._safe_detail(str(exc))})
        return 2
    notice('PREFLIGHT', {'url': normalized, 'readings': [first, second],
                          'sources': [first_source, second_source], 'orders_sent': 0})

    started = verifier.start(normalized)
    cid = started['campaign_id']
    begin = time.monotonic()
    requested_stop = [False]
    def request_stop(signum, frame):
        del signum, frame
        requested_stop[0] = True
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, request_stop)

    initial_reported = False
    result_reported: set[int] = set()
    submission_reported: set[int] = set()
    first_growth_reported: set[int] = set()
    last_phase = None
    stop_sent = False
    while True:
        c = verifier.status(cid)
        if not c:
            notice('BLOCKED', {'reason': 'Кампания исчезла из базы; результат неизвестен'})
            return 2
        if c.get('initial_count') is not None and not initial_reported:
            initial_reported = True
            notice('BASELINE', {'count': c['initial_count'], 'samples': c.get('baseline_samples')})
        phase = (c.get('current_service'), c.get('current_phase'))
        if phase != last_phase and phase[0]:
            notice('PHASE', {'site': phase[0], 'phase': phase[1]})
            last_phase = phase
        for row in c.get('results', []):
            rid = row['id']
            if row.get('submitted_at') is not None and rid not in submission_reported:
                submission_reported.add(rid)
                notice('POSSIBLE_SUBMISSION', {'site': row['site_name'],
                                               'before': row.get('baseline_count'),
                                               'at': row['submitted_at']})
            if rid not in first_growth_reported:
                positive = first_growth_sample(row)
                if positive:
                    first_growth_reported.add(rid)
                    notice('GROWTH_SAMPLE', {'site': row['site_name'], 'before': row['baseline_count'],
                                             'count': positive['count'], 'at': positive['observed_at'],
                                             'note': 'первое измерение, ещё не подтверждение доставки'})
            if row.get('status') in TERMINAL and row.get('finished_at') is not None and rid not in result_reported:
                result_reported.add(rid)
                notice('SITE_RESULT', compact_result(row))
        if c['status'] not in ('running', 'stopping'):
            # Initially skipped services may not have been emitted until the worker closed them.
            for row in c.get('results', []):
                if row['id'] not in result_reported:
                    notice('SITE_RESULT', compact_result(row))
            emit_report(c, output_dir)
            return 0 if c['status'] == 'completed' else 2
        if (requested_stop[0] or time.monotonic() - begin >= max_seconds) and not stop_sent:
            stop_sent = True
            notice('STOP_REQUESTED', {'reason': 'остановка по сигналу или временному бюджету; '
                                              'предыдущая заявка может продолжаться'})
            verifier.stop(cid)
        time.sleep(max(.1, poll_seconds))


def main() -> int:
    parser = argparse.ArgumentParser(description='Одноразовая проверка реальных заявок в CI')
    parser.add_argument('--target', required=True)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--max-seconds', type=int, default=18000)
    args = parser.parse_args()
    try:
        return run(args.target, args.output_dir, max_seconds=args.max_seconds)
    except Exception as exc:
        notice('ERROR', {'type': type(exc).__name__, 'detail': verifier._safe_detail(str(exc))})
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
