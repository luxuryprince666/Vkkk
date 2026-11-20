#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Inspect additional free VK pages without entering a target or placing orders.

All non-GET/HEAD browser traffic is aborted. The collected structure omits input
values, tokens, request bodies and URL query strings, except that a numeric
public free-service selector is recorded for comparison. This is not a delivery test.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import network, stealth, verifier  # noqa: E402
from tools.ci_progress import GitHubProgress, ProgressError  # noqa: E402
from tools.run_live_ci import notice  # noqa: E402

SITES = {
    'Boost-Like': 'https://boost-like.ru/vk/besplatnaya-nakrutka-podpischikov-v-vk',
    'LowcostSMM': 'https://lowcostsmm.com/ru/free/nakrutka-podpischikov-v-gruppu-vk-besplatno/',
    'MartiniSMM': 'https://martinismm.com/social-media/nakrutka-podpischikov-v-gruppu-vk-besplatno/',
    'CapitanSMM': 'https://capitansmm.com/ru/smm/nakrutka-podpischikov-v-gruppu-vk-besplatno/',
    'CaptainSMM': 'https://captainsmm.com/ru/nakrutka/nakrutka-podpischikov-v-gruppu-vk-besplatno/',
    'SprinterSMM': 'https://sprintersmm.com/raskrutka/',
}
STRUCTURE = r'''() => {
  const vis=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);
    return r.width>4 && r.height>4 && s.display!=='none' && s.visibility!=='hidden';};
  return {title:document.title,
    mainText:(document.body?.innerText||'').replace(/\s+/g,' ').trim().slice(0,1300),
    forms:[...document.querySelectorAll('form')].slice(0,25).map(f=>({
      id:f.id||'', classes:String(f.className||'').slice(0,150), method:f.method,
      actionPath:(()=>{try{const x=new URL(f.action||'',location.href);return x.origin+x.pathname}catch{return ''}})(),
      visible:vis(f), text:(f.innerText||'').replace(/\s+/g,' ').trim().slice(0,500),
      fields:[...f.querySelectorAll('input,select,textarea')].slice(0,35).map(e=>({
        id:e.id||'', name:e.name||'', type:e.type||e.tagName.toLowerCase(), visible:vis(e),
        required:!!e.required, ariaRequired:e.getAttribute('aria-required')||'',
        placeholder:e.getAttribute('placeholder')||'', hasValue:!!String(e.value||'').trim(),
        options:e.tagName==='SELECT'?[...e.options].slice(0,20).map(o=>({text:(o.textContent||'').trim().slice(0,100)})):undefined
      })),
      buttons:[...f.querySelectorAll('button,input[type=submit],a[role=button]')].slice(0,15).map(e=>({
        id:e.id||'',type:e.type||'',text:(e.innerText||e.value||'').trim().slice(0,120),visible:vis(e),disabled:!!e.disabled
      }))
    })),
    looseControls:[...document.querySelectorAll('button,input[type=submit]')].filter(e=>!e.closest('form') && vis(e))
      .slice(0,15).map(e=>({id:e.id||'',text:(e.innerText||e.value||'').trim().slice(0,120)})),
    selectedPublicService:(()=>{const e=document.querySelector('select#get_free_id');
      return e && /^\d+$/.test(e.value||'') ? {id:e.value, text:(e.selectedOptions[0]?.textContent||'').trim()} : null})(),
    scripts:[...document.querySelectorAll('script[src]')].slice(0,30).map(e=>{
      try{const u=new URL(e.src);return u.origin+u.pathname}catch{return ''}})
  };
}'''


def main() -> int:
    publisher = GitHubProgress(name='VK additional free sites read-only')
    state = {'phase': 'подготовка', 'orders_sent': 0, 'sites': []}
    publisher.create(state, title='Осмотр дополнительных сайтов без заявок')
    try:
        target = {'platform': 'vk', 'kind': 'community', 'url': 'https://vk.ru/club243396555'}
        route = network.Endpoint(id='direct', kind='direct', label='Прямое соединение')
        count, label, source = verifier._counter(target, [route])
        state['counter'] = {'count': count, 'route': label, 'source': source}
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            opts = {'headless': True, 'args': ['--no-sandbox', '--disable-dev-shm-usage']}
            chrome = stealth._find_chrome()
            if chrome:
                opts['executable_path'] = chrome
            browser = playwright.chromium.launch(**opts)
            context = browser.new_context(service_workers='block', user_agent=stealth.UA_CHROME)
            blocked = []
            def readonly_route(route):
                request = route.request
                if request.method in ('GET', 'HEAD'):
                    return route.continue_()
                parsed = urlsplit(request.url)
                entry = {'method': request.method, 'host': parsed.hostname or '', 'path': parsed.path}
                raw = request.post_data or ''
                try:
                    if raw.lstrip().startswith('{'):
                        data = json.loads(raw)
                        keys = list(data) if isinstance(data, dict) else []
                    else:
                        keys = [k for k, _ in parse_qsl(raw, keep_blank_values=True, max_num_fields=60)]
                    entry['body_keys'] = sorted({k for k in keys if re.fullmatch(r'[\w.\[\]-]{1,60}', k)})[:25]
                except (ValueError, TypeError, json.JSONDecodeError):
                    entry['body_keys'] = []
                if entry not in blocked and len(blocked) < 30:
                    blocked.append(entry)
                return route.abort()
            context.route('**/*', readonly_route)
            page = context.new_page()
            for name, url in SITES.items():
                info = {'site': name, 'url': url, 'orders_sent': 0}
                try:
                    page.goto(url, wait_until='domcontentloaded', timeout=45000)
                    page.wait_for_timeout(2500)
                    final = urlsplit(page.url)
                    info['final'] = final.scheme + '://' + (final.hostname or '') + final.path
                    info['structure'] = page.evaluate(STRUCTURE)
                except Exception as exc:
                    info['error'] = verifier._safe_detail(str(exc) or type(exc).__name__)
                state['sites'].append(info)
                state['blocked_requests'] = blocked
                state['phase'] = 'осмотр без ввода ссылки и без заказов'
                publisher.update(state, title='Осмотр без заявок продолжается')
            browser.close()
        state['phase'] = 'завершено'
        publisher.update(state, title='Осмотр без заявок завершён', conclusion='success')
        notice('EXTRA_READ_ONLY', {'orders_sent': 0, 'sites': [x['site'] for x in state['sites']],
                                   'counter': count})
        return 0
    except Exception as exc:
        state['phase'] = 'ошибка'
        state['reason'] = verifier._safe_detail(str(exc) or type(exc).__name__)
        try:
            publisher.update(state, title='Осмотр без заявок прерван', conclusion='failure')
        except ProgressError:
            pass
        notice('EXTRA_READ_ONLY_BLOCKED', {'orders_sent': 0, 'reason': state['reason']})
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
