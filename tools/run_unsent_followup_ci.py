#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One narrowly scoped follow-up for a site that the previous run never submitted.

The earlier run and its completed snapshot are checked before any browser or
order is opened. Never retry a site after a possible submission, and never start
this follow-up while an earlier campaign remains active or ambiguous.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import db  # noqa: E402
from tools.ci_progress import decode_snapshot  # noqa: E402
from tools.run_live_ci import notice, run  # noqa: E402

REPO = 'luxuryprince666/Vkkk'
TARGET = 'https://vk.ru/club243396555'
PRIOR_SHA = 'e191cfac05ae0fe5a50af1263985aba82e0a3b40'
PRIOR_RUN = 44764939761
PRIOR_CHECK = 136387388143
SITE = 'smmnakrutka'
MARKER = 'VKKK_SNAPSHOT_ZLIB_BASE64:'


def github_get(path: str, token: str) -> dict:
    req = urllib.request.Request('https://api.github.com/repos/' + REPO + path,
                                 headers={'Accept': 'application/vnd.github+json',
                                          'Authorization': 'Bearer ' + token,
                                          'User-Agent': 'vk-unsent-followup',
                                          'X-GitHub-Api-Version': '2022-11-28'})
    with urllib.request.urlopen(req, timeout=20) as resp:
        value = json.load(resp)
    if not isinstance(value, dict):
        raise ValueError('Некорректный ответ GitHub')
    return value


def validate_previous(check: dict, workflow: dict) -> dict:
    """Reject incomplete, ambiguous, wrong-target and already-submitted campaigns."""
    if (workflow.get('id') != PRIOR_RUN or workflow.get('head_sha') != PRIOR_SHA
            or workflow.get('status') != 'completed' or workflow.get('conclusion') != 'success'
            or workflow.get('run_attempt') != 1):
        raise ValueError('Предыдущий workflow ещё выполняется или не завершился успешно')
    if (check.get('id') != PRIOR_CHECK or check.get('head_sha') != PRIOR_SHA
            or check.get('name') != 'VK sequential live verification snapshot'
            or check.get('status') != 'completed' or check.get('conclusion') != 'success'):
        raise ValueError('Предыдущий снимок не подтверждён')
    summary = (check.get('output') or {}).get('summary') or ''
    if MARKER not in summary:
        raise ValueError('Предыдущий снимок отсутствует')
    snapshot = decode_snapshot(summary.split(MARKER, 1)[1].strip())
    campaign = snapshot.get('campaign') or {}
    rows = campaign.get('results') or []
    if (str(snapshot.get('actions_run_id')) != str(PRIOR_RUN) or snapshot.get('phase') != 'completed'
            or campaign.get('status') != 'completed' or campaign.get('channel_url') != TARGET
            or not isinstance(rows, list)):
        raise ValueError('Предыдущая кампания неполная или относится к другой ссылке')
    if any(r.get('status') in ('pending', 'running', 'unverified') for r in rows):
        raise ValueError('Предыдущая кампания содержит незавершённый или неизвестный результат')
    candidates = [r for r in rows if r.get('site_id') == SITE]
    if len(candidates) != 1:
        raise ValueError('Нет однозначной записи площадки')
    row = candidates[0]
    if (row.get('status') != 'skipped' or row.get('submitted_at') is not None
            or any(a.get('submitted_at') is not None for a in row.get('attempts', []))):
        raise ValueError('Возможная отправка уже была; повтор запрещён')
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description='Однократная проверка только ранее не отправленной площадки')
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    permitted = {'GITHUB_ACTIONS': 'true', 'VKKK_LIVE_RUN': 'authorized-once',
                 'VKKK_UNSENT_FOLLOWUP': 'smmnakrutka-only', 'GITHUB_RUN_ATTEMPT': '1',
                 'GITHUB_EVENT_NAME': 'push', 'GITHUB_REPOSITORY': REPO,
                 'GITHUB_REF': 'refs/heads/arena/e372c48b-vkkk'}
    if any(os.environ.get(k) != v for k, v in permitted.items()):
        raise RuntimeError('Одноразовая проверка не разрешена в этом окружении')
    token = os.environ.get('VKKK_PROGRESS_TOKEN', '')
    if not token:
        raise RuntimeError('Нет канала проверки состояния предыдущей кампании')
    check = github_get(f'/check-runs/{PRIOR_CHECK}', token)
    workflow = github_get(f'/actions/runs/{PRIOR_RUN}', token)
    row = validate_previous(check, workflow)
    notice('UNSENT_PREFLIGHT', {'prior_run': PRIOR_RUN, 'site': SITE,
                                'prior_status': row.get('status'), 'prior_submission': False})
    # The database is unique to this runner. seed_catalog preserves these flags
    # when run() subsequently initializes the same database again.
    db.init_db()
    with db.tx() as conn:
        if conn.execute('SELECT COUNT(*) FROM sites WHERE id=?', (SITE,)).fetchone()[0] != 1:
            raise RuntimeError('Площадка отсутствует в каталоге')
        conn.execute('UPDATE sites SET enabled=CASE WHEN id=? THEN 1 ELSE 0 END', (SITE,))
    return run(TARGET, args.output_dir, max_seconds=3600)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        notice('UNSENT_BLOCKED', {'reason': type(exc).__name__, 'detail': str(exc)[:200]})
        raise SystemExit(2)
