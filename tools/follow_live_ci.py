#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Back up an in-progress or completed Actions verification check locally.

This performs read-only GitHub API queries. It never triggers workflows, changes
orders, or logs credentials. Unknown and pending results remain unknown.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core.verifier import format_report  # noqa: E402
from tools.ci_progress import decode_snapshot  # noqa: E402

CHECK_NAME = 'VK sequential live verification snapshot'
MARKER = 'VKKK_SNAPSHOT_ZLIB_BASE64:'


def github_api(path: str) -> dict:
    proc = subprocess.run(['gh', 'api', path], capture_output=True, text=True, timeout=45, check=True)
    value = json.loads(proc.stdout)
    if not isinstance(value, dict):
        raise ValueError('GitHub returned a non-object')
    return value


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(text, encoding='utf-8')
    os.replace(tmp, path)


def follow(repo: str, sha: str, run_id: int, out: Path, interval: int) -> int:
    if '/' not in repo or len(sha) != 40 or not all(c in '0123456789abcdef' for c in sha):
        raise ValueError('Expected owner/repository and a full lowercase commit SHA')
    last_signature = None
    found = False
    while True:
        try:
            checks = github_api(f'repos/{repo}/commits/{sha}/check-runs').get('check_runs', [])
            matches = [x for x in checks if x.get('name') == CHECK_NAME]
            if len(matches) > 1:
                raise ValueError('More than one live check; refusing to guess which one to report')
            if matches:
                found = True
                check = matches[0]
                summary = (check.get('output') or {}).get('summary') or ''
                if MARKER not in summary:
                    raise ValueError('Live check has no complete snapshot marker')
                snapshot = decode_snapshot(summary.split(MARKER, 1)[1].strip())
                snapshot['check_id'] = check['id']
                snapshot['check_status'] = check.get('status')
                snapshot['check_conclusion'] = check.get('conclusion')
                snapshot['actions_run_id'] = run_id
                snapshot['actions_run_url'] = f'https://github.com/{repo}/actions/runs/{run_id}'
                atomic_text(out.with_suffix('.json'), json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n')
                campaign = snapshot.get('campaign')
                if campaign:
                    text = format_report(campaign)
                    signature = (snapshot.get('phase'), campaign.get('current_service'),
                                 campaign.get('current_phase'), campaign.get('current_count'),
                                 tuple((r.get('site_id'), r.get('status'), r.get('submitted_at'),
                                        r.get('final_count')) for r in campaign.get('results', [])))
                else:
                    preflight = snapshot.get('preflight') or {}
                    text = 'Исходная проверка: ' + json.dumps(preflight, ensure_ascii=False)
                    signature = (snapshot.get('phase'), None)
                text += '\n\nИсточник: ' + snapshot['actions_run_url'] + '\n'
                atomic_text(out.with_suffix('.txt'), text)
                if signature != last_signature:
                    print('Снимок сохранён:', signature[:4], flush=True)
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
    parser = argparse.ArgumentParser(description='Сохранить снимки последовательной проверки из GitHub Actions')
    parser.add_argument('--repo', required=True)
    parser.add_argument('--sha', required=True)
    parser.add_argument('--run-id', required=True, type=int)
    parser.add_argument('--output', required=True, type=Path, help='путь без расширения .json/.txt')
    parser.add_argument('--interval', type=int, default=30)
    args = parser.parse_args()
    return follow(args.repo, args.sha, args.run_id, args.output, args.interval)


if __name__ == '__main__':
    raise SystemExit(main())
