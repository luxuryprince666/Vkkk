#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Publish a saved final report from a fresh Actions step's annotation budget."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.run_live_ci import emit_report, notice  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print('Usage: ci_emit_report.py PATH', file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    if not path.is_file():
        notice('NO_FINAL_REPORT', {'reason': 'Итоговый файл не создан; смотрите последний снимок check-run'})
        return 0
    emit_report(json.loads(path.read_text(encoding='utf-8')), path.parent)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
