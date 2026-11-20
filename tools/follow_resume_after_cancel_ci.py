#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Save read-only snapshots of the quarantined VK remainder run."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools import follow_continuation_ci as follower  # noqa: E402
from tools.run_resume_after_cancel_ci import CHECK_NAME  # noqa: E402

if __name__ == '__main__':
    follower.CHECK_NAME = CHECK_NAME
    raise SystemExit(follower.main())
