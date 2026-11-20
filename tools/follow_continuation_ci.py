#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only local backup of a continuing VK measurement check."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.ci_progress import decode_snapshot  # noqa: E402
from tools.follow_live_ci import atomic_text, github_api  # noqa: E402
from tools.run_continuation_ci import CHECK_NAME, MARKER, render_report  # noqa: E402


def follow(repo: str, sha: str, run_id: int, out: Path, interval: int) -> int:
    if '/' not in repo or len(sha) != 40 or not all(c in '0123456789abcdef' for c in sha):
        raise ValueError('Expected owner/repository and full lowercase commit SHA')
    last_signature = None
    found = False
    while True:
        try:
            checks = github_api(f'repos/{repo}/commits/{sha}/check-runs').get('check_runs', [])
            matches = [x for x in checks if x.get('name') == CHECK_NAME]
            if len(matches) > 1:
                raise ValueError('Multiple continuation checks: refusing to guess which one')
            if matches:
                check = matches[0]
                summary = (check.get('output') or {}).get('summary') or ''
                if MARKER not in summary:
                    raise ValueError('Continuation check has no complete snapshot')
                snapshot = decode_snapshot(summary.split(MARKER, 1)[1].strip())
                if str(snapshot.get('actions_run_id')) != str(run_id):
                    raise ValueError('Snapshot belongs to another run')
                found = True
                snapshot.update({'check_id': check['id'], 'check_status': check.get('status'),
                                 'check_conclusion': check.get('conclusion'),
                                 'actions_run_url': f'https://github.com/{repo}/actions/runs/{run_id}'})
                atomic_text(out.with_suffix('.json'), json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n')
                atomic_text(out.with_suffix('.txt'), render_report(snapshot))
                current = snapshot.get('current') or {}
                row = current.get('row') or {}
                samples = snapshot.get('quarantine_samples') or []
                signature = (snapshot.get('phase'), current.get('site_id'), current.get('phase'),
                             row.get('submitted_at'), row.get('final_count'),
                             len(snapshot.get('results') or []), samples[-1].get('count') if samples else None)
                if signature != last_signature:
                    print('Продолжение:', signature, flush=True)
                    last_signature = signature
                if check.get('status') == 'completed':
                    return 0 if check.get('conclusion') == 'success' else 2
            run = github_api(f'repos/{repo}/actions/runs/{run_id}')
            if run.get('status') == 'completed':
                print('Workflow завершён:', run.get('conclusion'),
                      '(последний снимок сохранён)' if found else '(снимок не создан)', flush=True)
                return 0 if run.get('conclusion') == 'success' and found else 2
        except (subprocess.SubprocessError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            print('Не удалось прочитать снимок, повторю:', type(exc).__name__, flush=True)
        time.sleep(max(10, interval))


def main() -> int:
    parser = argparse.ArgumentParser(description='Сохранять снимки продолжения из GitHub Actions')
    parser.add_argument('--repo', required=True)
    parser.add_argument('--sha', required=True)
    parser.add_argument('--run-id', required=True, type=int)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--interval', type=int, default=30)
    args = parser.parse_args()
    return follow(args.repo, args.sha, args.run_id, args.output, args.interval)


if __name__ == '__main__':
    raise SystemExit(main())
