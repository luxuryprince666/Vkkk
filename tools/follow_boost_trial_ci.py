#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Save read-only snapshots of the queued Boost-Like trial check."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools import follow_continuation_ci as follower  # noqa: E402
from tools.run_boost_after_resume_ci import CHECK_NAME, render  # noqa: E402

if __name__ == '__main__':
    follower.CHECK_NAME = CHECK_NAME
    follower.render_report = render
    raise SystemExit(follower.main())
