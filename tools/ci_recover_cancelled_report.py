#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Recover the final report artifact from one cancelled run; never opens sites.

The sandbox cannot reach the Actions artifact storage host directly, but a
separate Actions runner can. This tool reads only a fixed completed run and
publishes its sanitized report to a check on this commit. No target or order is
submitted. No download URL, authorization header or token is logged.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.ci_progress import GitHubProgress  # noqa: E402
from tools.run_unsent_followup_ci import REPO, TARGET, github_get  # noqa: E402

RUN_ID = 44771191791
ARTIFACT_ID = 14651164651
ARTIFACT_NAME = 'vk-continuation-report'
SHA = '3709851d4c4831c24f9b2d57eb78ee8f3af8767e'
CHECK_NAME = 'VK cancelled continuation artifact recovery (read only)'


def main() -> int:
    token = os.environ.get('VKKK_PROGRESS_TOKEN', '')
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != REPO
            or os.environ.get('GITHUB_REF') != 'refs/heads/arena/e372c48b-vkkk' or not token):
        raise RuntimeError('Recovery permitted only in the scoped CI environment')
    run = github_get(f'/actions/runs/{RUN_ID}', token)
    if (run.get('id') != RUN_ID or run.get('head_sha') != SHA or run.get('run_attempt') != 1
            or run.get('status') != 'completed' or run.get('conclusion') != 'cancelled'):
        raise ValueError('Expected cancelled run was not found')
    artifacts = github_get(f'/actions/runs/{RUN_ID}/artifacts', token).get('artifacts') or []
    found = [a for a in artifacts if a.get('id') == ARTIFACT_ID and a.get('name') == ARTIFACT_NAME
             and not a.get('expired')]
    if len(found) != 1 or found[0].get('size_in_bytes', 10**8) > 2_000_000:
        raise ValueError('Expected small report artifact not found')
    with tempfile.TemporaryDirectory(prefix='vkkk-recovery-') as folder:
        env = {**os.environ, 'GH_TOKEN': token}
        result = subprocess.run(['gh', 'run', 'download', str(RUN_ID), '--repo', REPO,
                                 '--name', ARTIFACT_NAME, '--dir', folder], env=env,
                                capture_output=True, timeout=90, check=False)
        if result.returncode:
            # gh may include a signed artifact URL in stderr. Never log it.
            raise RuntimeError('Could not download report artifact from Actions storage')
        path = Path(folder) / 'vk-continuation.json'
        if not path.is_file() or path.stat().st_size > 2_000_000:
            raise ValueError('Artifact report is missing or too large')
        report = json.loads(path.read_text(encoding='utf-8'))
        if str(report.get('actions_run_id')) != str(RUN_ID) or report.get('target') != TARGET:
            raise ValueError('Artifact report belongs to another run or target')
        report['artifact_recovery'] = {'source_run': RUN_ID, 'artifact_id': ARTIFACT_ID,
                                       'workflow_conclusion': 'cancelled', 'orders_sent': 0}
        publisher = GitHubProgress(name=CHECK_NAME)
        check = publisher.create(report, title='Восстановлен отчёт отменённого запуска без заявок')
        publisher.update(report, title='Отчёт восстановлен, новые заявки не отправлялись', conclusion='success')
        print('Recovered read-only report check:', check, flush=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('Recovery failed:', type(exc).__name__, flush=True)
        raise SystemExit(2)
