#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only test of the Boost-Like trial allowlist; never enters a target."""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import stealth, verifier  # noqa: E402
from tools import boost_like_trial as trial  # noqa: E402
from tools.ci_progress import GitHubProgress, ProgressError  # noqa: E402
from tools.run_unsent_followup_ci import REPO  # noqa: E402

CHECK = 'Boost-Like strict allowlist preflight (no target or order)'
MODULE = '/_nuxt/CCrX1cDL.js'


def main() -> int:
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != REPO
            or os.environ.get('GITHUB_REF') != 'refs/heads/arena/e372c48b-vkkk'):
        raise RuntimeError('Scoped read-only CI only')
    state = {'phase': 'подготовка', 'orders_sent': 0, 'target_entered': False,
             'changing_requests_allowed': False, 'clicked': False}
    publisher = GitHubProgress(name=CHECK)
    publisher.create(state, title='Проверка сетевого барьера без заявки')
    try:
        with stealth.open_page(headless=True, prefer='playwright') as (backend, page):
            if backend != 'playwright':
                raise RuntimeError('Требуется перехватываемый Playwright')
            raw = page._p
            fence = trial.TrialFence()
            fence.attach(raw)
            try:
                page.goto(trial.PAGE_URL, wait=5000)
                # Public static JS only, no page input, no clicks and no POST.
                module = raw.evaluate('''async (path) => {
                  const response=await fetch(path, {cache:'no-store'});
                  if (!response.ok) throw new Error('public module unavailable');
                  return await response.text();
                }''', MODULE)
                state['module_path'] = MODULE
                state['module_sha256'] = hashlib.sha256(module.encode('utf-8')).hexdigest()
                state['initial_card'] = trial.initial_state(raw)
                state['allowed_requests'] = fence.allowed
                state['blocked_requests'] = fence.blocked
                if not state['initial_card'].get('valid'):
                    raise RuntimeError('Бесплатная карточка не прошла строгую проверку')
                if fence.start_seen or fence.complete_seen or fence.stage != 'loading':
                    raise RuntimeError('Неожиданное состояние сетевого барьера')
                state['phase'] = 'завершено'
            finally:
                fence.close()
        publisher.update(state, title='Барьер проверен без ввода ссылки и заявки', conclusion='success')
        print('Boost strict preflight completed without a target, click or order.')
        return 0
    except Exception as exc:
        state.update(phase='остановлено', reason=verifier._safe_detail(str(exc) or type(exc).__name__))
        try:
            publisher.update(state, title='Проверка без заявки остановлена', conclusion='failure')
        except ProgressError:
            pass
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
