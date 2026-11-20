#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only preflight for the continuation: never fills or submits a form."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import db, network, stealth, verifier  # noqa: E402
from tools.ci_progress import GitHubProgress, ProgressError  # noqa: E402
from tools.run_continuation_ci import SITES, configure, validate_previous  # noqa: E402
from tools.run_live_ci import notice  # noqa: E402
from tools.run_unsent_followup_ci import PRIOR_CHECK, PRIOR_RUN, TARGET, github_get  # noqa: E402


def main() -> int:
    publisher = GitHubProgress(name='VK continuation preflight read-only')
    state = {'phase': 'подготовка', 'orders_sent': 0}
    publisher.create(state, title='Проверка без заявок')
    try:
        token = os.environ.get('VKKK_PROGRESS_TOKEN', '')
        prior = validate_previous(github_get(f'/check-runs/{PRIOR_CHECK}', token),
                                  github_get(f'/actions/runs/{PRIOR_RUN}', token))
        configure()
        with stealth.open_page(headless=True, prefer='playwright') as (backend, page):
            if backend != 'playwright' or page.js('1 + 1') != 2:
                raise RuntimeError('Браузер не запускается')
        target = {'platform': 'vk', 'kind': 'community', 'url': TARGET}
        route = network.Endpoint(id='direct', kind='direct', label='Прямое соединение')
        reads = []
        for _ in range(2):
            count, label, source = verifier._counter(target, [route])
            reads.append({'count': count, 'route': label, 'source': source})
            time.sleep(2)
        rows = []
        cfg = verifier._settings()
        for site in SITES:
            with db.tx() as conn:
                conn.execute('UPDATE sites SET enabled=CASE WHEN id=? THEN 1 ELSE 0 END', (site,))
                candidates = verifier._site_results(conn, 'vk', 'community', cfg)
            selected = [r for r in candidates if r['site_id'] == site]
            if len(selected) != 1:
                raise RuntimeError('Площадка отсутствует или дублируется: ' + site)
            rows.append({k: selected[0].get(k) for k in ('site_id', 'site_name', 'service_id',
                                                         'service_label', 'status', 'failure_reason')})
        state = {'phase': 'завершено', 'orders_sent': 0, 'prior': prior, 'counter_samples': reads,
                 'browser': backend, 'candidates': rows}
        publisher.update(state, title='Read-only preflight завершён', conclusion='success')
        notice('CONTINUATION_PREFLIGHT', {'orders_sent': 0, 'counts': [r['count'] for r in reads],
                                          'candidates': rows})
        return 0
    except Exception as exc:
        state['phase'] = 'ошибка'
        state['reason'] = verifier._safe_detail(str(exc) or type(exc).__name__)
        try:
            publisher.update(state, title='Preflight остановлен без заявок', conclusion='failure')
        except ProgressError:
            pass
        notice('CONTINUATION_PREFLIGHT_BLOCKED', {'orders_sent': 0, 'reason': state['reason']})
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
