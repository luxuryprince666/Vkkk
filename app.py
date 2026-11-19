# -*- coding: utf-8 -*-
"""CLI-хелпер. Основная точка входа — run.py; этот файл нужен для привычки `python app.py`."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
