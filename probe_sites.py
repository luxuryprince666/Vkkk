# -*- coding: utf-8 -*-
"""Зонд площадок: обойти каталог и снять факты со страниц бесплатной накрутки.

Логика разбора живёт в `core/probe.py` — тот же код, что софт использует для ежедневной
самопроверки площадок. Здесь только обход каталога и отчёт.

    python tools/probe_sites.py            # все сайты из data/catalog.json
    python tools/probe_sites.py smm8 avi1  # только эти
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import probe  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "research"


def main(ids: list[str]) -> int:
    raw = json.loads((ROOT / "data" / "catalog.json").read_text(encoding="utf-8"))
    sites = raw["sites"] if isinstance(raw, dict) else raw
    if ids:
        sites = [s for s in sites if s["id"] in ids]
    OUT.mkdir(exist_ok=True)
    results = []
    for site in sites:
        url = site.get("free_page") or site.get("home")
        r = probe.probe(url)
        r["id"] = site["id"]
        r["name"] = site["name"]
        results.append(r)
        cap = ",".join(r["captcha"]) or "—"
        print(f"{site['id']:14} {str(r['status']):>4} {str(r['sec']):>5}s "
              f"{str(r['bytes'] or 0):>7}b полей={r['fields_n']:>2} ссылка={r['has_link_field']!s:5} "
              f"капча={cap:22} спа={r['spa']!s:5} {r['title'][:38]}", flush=True)
    (OUT / "probe.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nсохранено: {OUT / 'probe.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
