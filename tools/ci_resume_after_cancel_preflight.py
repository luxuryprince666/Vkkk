#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only preflight for one quarantined continuation; sends no orders."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import db, drivers, network, stealth, verifier  # noqa: E402
from tools import run_resume_after_cancel_ci as resume  # noqa: E402
from tools.ci_extra_sites_readonly import STRUCTURE  # noqa: E402
from tools.ci_progress import GitHubProgress  # noqa: E402
from tools.run_live_ci import notice  # noqa: E402


def main() -> int:
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != resume.REPO
            or os.environ.get('GITHUB_REF') != 'refs/heads/' + resume.BRANCH):
        raise RuntimeError('Preflight разрешён только на нужной ветке CI')
    token = os.environ.get('VKKK_PROGRESS_TOKEN', '')
    if not token:
        raise RuntimeError('Нет канала отчёта')
    publisher = GitHubProgress(name='VK safe resume preflight (read only)')
    state = {'phase': 'подготовка', 'orders_sent': 0}
    publisher.create(state, title='Проверка без заявок')
    try:
        info = resume.evidence(token)
        catalog = json.loads((ROOT / 'data/catalog.json').read_text(encoding='utf-8'))
        resume.validate_catalog(catalog)
        db.init_db()
        inspected = []
        with stealth.open_page(headless=True, prefer='playwright') as (backend, page):
            if backend != 'playwright' or page.js('1+1') != 2:
                raise RuntimeError('Браузер не готов')
            # Ни ссылку, ни другие значения не вводим, ни по каким кнопкам не
            # кликаем. Мутирующие запросы браузера при осмотре блокируются.
            blocked = [0]
            def readonly(route):
                if route.request.method in ('GET', 'HEAD'):
                    route.continue_()
                else:
                    blocked[0] += 1
                    route.abort()
            page._p.context.route('**/*', readonly)
            for name, url in (
                ('PriceSMM', resume.EXPECTED_URLS['pricesmm']),
                ('SprinterSMM', resume.EXPECTED_URLS['sprintersmm']),
            ):
                entry = {'site': name, 'orders_sent': 0}
                try:
                    page.goto(url, wait=2500)
                    structure = page._p.evaluate(STRUCTURE)
                    discovered = drivers.discover(page)
                    field = drivers.pick_link_field(discovered.get('fields') or [])
                    scoped = drivers.same_form_state(page, discovered, field['sel']) if field else None
                    button = drivers.pick_submit_button(scoped.get('buttons') or []) if scoped else None
                    entry.update({'structure': structure, 'field_selector': field.get('sel') if field else None,
                                  'button_selector': button.get('sel') if button else None,
                                  'free_guard_on_empty_field': bool(field and button and drivers.free_form_guard(
                                      page, field['sel'], button['sel'], '', url)),
                                  'countdown_sec': drivers._button_countdown(page, button['sel']) if button else None})
                except Exception as exc:
                    entry['error'] = verifier._safe_detail(str(exc) or type(exc).__name__)
                inspected.append(entry)
            state['blocked_mutating_requests'] = blocked[0]
            state['inspected_forms_without_input'] = inspected
        vk = {'platform': 'vk', 'kind': 'community', 'url': resume.TARGET}
        route = network.Endpoint(id='direct', kind='direct', label='Прямое соединение')
        samples = []
        for _ in range(2):
            count, label, source = verifier._counter(vk, [route])
            samples.append({'observed_at': time.time(), 'count': count, 'route': label, 'source': source})
            time.sleep(2)
        state.update({'phase': 'завершено', 'target': resume.TARGET, 'orders_sent': 0,
                      'prior_cancel_run': resume.CANCEL_RUN, 'prior_runner_ended_at': info['ended_at'],
                      'earliest_after_one_hour': info['ended_at'] + 3600,
                      'prior_recovery_check': resume.RECOVERY_CHECK, 'extra_check': resume.EXTRA_CHECK,
                      'excluded_uncertain': ['doctorsmm', 'avi1', 'nakruti', 'prtut'],
                      'queue': list(resume.QUEUE), 'counter_samples': samples})
        publisher.update(state, title='Проверка доказательств и счётчика без заявок завершена', conclusion='success')
        notice('RESUME_PREFLIGHT', {'orders_sent': 0, 'counter': [s['count'] for s in samples],
                                    'sites': list(resume.QUEUE)})
        return 0
    except Exception as exc:
        state.update({'phase': 'ошибка', 'reason': verifier._safe_detail(str(exc) or type(exc).__name__)})
        publisher.update(state, title='Preflight остановлен, заявок нет', conclusion='failure')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
