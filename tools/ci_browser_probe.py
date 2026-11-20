#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only CI readiness check: no target is entered and no buttons are clicked.

All non-GET/HEAD browser requests are blocked. This does not test delivery and
must not be represented as a completed order.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import delivery, drivers, network, stealth, verifier  # noqa: E402
from tools.run_live_ci import notice  # noqa: E402


TARGET = {'platform': 'vk', 'kind': 'community', 'url': 'https://vk.ru/club243396555'}


def main() -> int:
    route = network.Endpoint(id='direct', kind='direct', label='Прямое соединение')
    try:
        count, label, source = verifier._counter(TARGET, [route])
    except delivery.CounterError as exc:
        notice('PREP_BLOCKED', {'reason': 'Не удалось прочитать счётчик',
                                'detail': verifier._safe_detail(str(exc))})
        return 2
    status = stealth.backend_status()
    notice('PREP_COUNTER', {'count': count, 'source': source, 'route': label,
                            'playwright_ready': any(b['id'] == 'playwright' and b['ready']
                                                    for b in status['backends']),
                            'orders_sent': 0})
    catalog = json.loads((ROOT / 'data' / 'catalog.json').read_text(encoding='utf-8'))
    try:
        with stealth.open_page(headless=True, prefer='playwright') as (backend, page):
            if backend != 'playwright' or page.js('1+1') != 2:
                raise RuntimeError('Chromium не прошёл проверку')
            page._p.route('**/*', lambda route: route.continue_()
                          if route.request.method in ('GET', 'HEAD') else route.abort())
            notice('PREP_BROWSER', {'backend': backend, 'started': True})
            for site in catalog['sites']:
                services = [s for s in site.get('services', []) if s.get('platform') == 'vk'
                            and s.get('type') == 'followers' and s.get('subject', 'any') in ('any', 'community')]
                if site.get('reg_required') or not services or site['id'] == 'likeboom':
                    # LikeBoom starts a free-trial countdown on load; don't consume a trial during setup.
                    notice('PREP_SITE', {'site': site['name'], 'visited': False, 'orders_sent': 0})
                    continue
                service = services[0]
                out = {'site': site['name'], 'visited': True, 'orders_sent': 0}
                try:
                    response = page._p.goto(service['url'], wait_until='domcontentloaded', timeout=30000)
                    page.wait(1800)
                    state = drivers.discover(page)
                    field = drivers.pick_link_field(state.get('fields') or [])
                    button = drivers.pick_submit_button(state.get('buttons') or [])
                    out.update({
                        'http_status': response.status if response else None,
                        'final_host': urlsplit(page._p.url).hostname,
                        'link_field': {k: field.get(k) for k in ('sel', 'name', 'ph', 'label')} if field else None,
                        'submit_candidate': {k: button.get(k) for k in ('sel', 'text', 'nearForm', 'disabled')} if button else None,
                        'captcha_visible': bool(state.get('captcha')),
                        'fields': [{k: f.get(k) for k in ('sel', 'name', 'ph', 'label')}
                                   for f in (state.get('fields') or [])[:5]],
                        'buttons': [{k: b.get(k) for k in ('sel', 'text', 'nearForm', 'disabled')}
                                    for b in (state.get('buttons') or [])[:10]],
                    })
                except Exception as exc:
                    out['error'] = verifier._safe_detail(str(exc))
                notice('PREP_SITE', out)
    except Exception as exc:
        notice('PREP_BLOCKED', {'reason': 'Браузер недоступен; заявок нет',
                                'detail': verifier._safe_detail(str(exc))})
        return 2
    notice('PREP_FINISHED', {'orders_sent': 0, 'note': 'read-only; доставка не проверена'})
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
