#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Inspect the free VK popup after closing only a verified paid advertisement.

No target is entered, no order button is clicked, and non-GET/HEAD requests
are blocked. Input/token contents are never recorded. This is not a delivery test.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import drivers, stealth, verifier  # noqa: E402
from tools.ci_progress import GitHubProgress, ProgressError  # noqa: E402
from tools.run_live_ci import notice  # noqa: E402

URL = 'https://smmnakrutka.ru/free-vk-followers'
PUBLIC_STRUCTURE = r'''() => {
  const vis=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);
    return r.width>4 && r.height>4 && s.display!=='none' && s.visibility!=='hidden';};
  return [...document.querySelectorAll('.t-popup.t-popup_show')].filter(vis).map(p => ({
    classes:String(p.className).slice(0,100), text:(p.innerText||'').replace(/\s+/g,' ').trim().slice(0,500),
    forms:[...p.querySelectorAll('form')].map(f=>({id:f.id, visible:vis(f),
      actionType:f.getAttribute('data-formactiontype')||'',
      fields:[...f.querySelectorAll('input,select')].map(e=>({name:e.name,type:e.type,
        visible:vis(e), present:!!String(e.value||'').trim(),
        publicValue:['service','url_page','quantity','quantity-text'].includes(e.name) ? e.value : undefined})).slice(0,25),
      buttons:[...f.querySelectorAll('button')].map(b=>({text:(b.innerText||'').trim().slice(0,100),
        type:b.type,visible:vis(b)})).slice(0,5)})).slice(0,3)})).slice(0,4);
}'''


def main() -> int:
    publisher = GitHubProgress(name='VK SMMnakrutka popup read-only')
    publisher.create({'phase': 'подготовка', 'orders_sent': 0})
    samples = []
    try:
        with stealth.open_page(headless=True, prefer='playwright') as (backend, page):
            if backend != 'playwright':
                raise RuntimeError('Playwright недоступен')
            page._p.route('**/*', lambda route: route.continue_()
                          if route.request.method in ('GET', 'HEAD') else route.abort())
            page.goto(URL, wait=1800)
            page.mouse_wiggle(2)
            page.scroll()
            if not drivers._same_service_location(URL, page.js('window.location.href') or ''):
                raise RuntimeError('Страница перенаправлена')
            close = drivers._smmnakrutka_close_paid_ad(page)
            if close == 'unsafe':
                raise RuntimeError('Рекламное окно не совпадает с проверенным')
            start = time.monotonic()
            for elapsed in range(0, 361, 20):
                if elapsed:
                    page.wait(20000)
                state = drivers._smmnakrutka_state(page, 'vk', 10).get('state') or 'unknown'
                sample = {'elapsed_sec': round(time.monotonic() - start, 1), 'state': state,
                          'popups': page._p.evaluate(PUBLIC_STRUCTURE)}
                samples.append(sample)
                publisher.update({'phase': 'наблюдение без ввода ссылки', 'orders_sent': 0,
                                  'ad_close': close, 'samples': samples}, title='Осмотр бесплатной формы')
                if state == 'ready':
                    break
        publisher.update({'phase': 'завершено', 'orders_sent': 0, 'ad_close': close, 'samples': samples},
                         title='Осмотр без заявок завершён', conclusion='success')
        notice('POPUP_READ_ONLY', {'orders_sent': 0, 'ad_close': close, 'last': samples[-1] if samples else None})
        return 0
    except Exception as exc:
        detail = verifier._safe_detail(str(exc) or type(exc).__name__)
        try:
            publisher.update({'phase': 'ошибка', 'orders_sent': 0, 'samples': samples, 'reason': detail},
                             title='Осмотр без заявок прерван', conclusion='failure')
        except ProgressError:
            pass
        notice('POPUP_BLOCKED', {'orders_sent': 0, 'reason': detail})
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
