#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Inspect Boost-Like's trial UI without entering a link or sending requests.

Initial page load is GET/HEAD only. After inspecting the one labelled free
trial control, ALL browser requests are blocked before opening its UI. The
browser has no account, saved cookies or supplied target. No form is submitted.
Request records contain method, hostname and pathname only, not query/body.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import stealth, verifier  # noqa: E402
from tools.ci_extra_sites_readonly import STRUCTURE  # noqa: E402
from tools.ci_progress import GitHubProgress, ProgressError  # noqa: E402
from tools.run_unsent_followup_ci import REPO  # noqa: E402

URL = 'https://boost-like.ru/vk/besplatnaya-nakrutka-podpischikov-v-vk'
CHECK = 'Boost-Like free modal inspection (no target or requests after click)'

CANDIDATE = r'''() => {
  const visible = e => { const r=e.getBoundingClientRect(), s=getComputedStyle(e);
    return r.width>4 && r.height>4 && s.visibility!=='hidden' && s.display!=='none'; };
  const buttons=[...document.querySelectorAll('button')].filter(e => visible(e) &&
    (e.innerText||'').trim().toLowerCase()==='получить');
  if (buttons.length!==1) return {unique:false,count:buttons.length};
  const b=buttons[0];
  let p=b, context='';
  for (let i=0;i<8 && p;i++,p=p.parentElement) {
    const t=(p.innerText||'').replace(/\s+/g,' ').trim();
    if (t.length<1200 && /бесплатно/i.test(t) && /подписчик/i.test(t) && /\+\s*10/.test(t)) {
      context=t; break;
    }
  }
  return {unique:true,context,insideForm:!!b.closest('form'),insideLink:!!b.closest('a[href]'),
          enabled:!b.disabled && b.getAttribute('aria-disabled')!=='true'};
}'''


def main() -> int:
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != REPO
            or os.environ.get('GITHUB_REF') != 'refs/heads/arena/e372c48b-vkkk'):
        raise RuntimeError('Scoped read-only CI only')
    state = {'phase': 'подготовка', 'orders_sent': 0, 'target_entered': False,
             'network_blocked_before_activation': False, 'blocked_after_activation': []}
    publisher = GitHubProgress(name=CHECK)
    publisher.create(state, title='Осмотр без ссылки и без заявок')
    try:
        blocked = []
        with stealth.open_page(headless=True, prefer='playwright') as (backend, page):
            if backend != 'playwright':
                raise RuntimeError('Требуется перехватываемый Playwright')
            raw = page._p
            raw.add_init_script("""(() => {
              const no = function(){throw new Error('disabled in read-only inspection')};
              Object.defineProperty(window,'WebSocket',{value:no,configurable:false});
              Object.defineProperty(window,'EventSource',{value:no,configurable:false});
              if (window.WebTransport) Object.defineProperty(window,'WebTransport',{value:no,configurable:false});
              if (window.RTCPeerConnection) Object.defineProperty(window,'RTCPeerConnection',{value:no,configurable:false});
              Object.defineProperty(navigator,'sendBeacon',{value:()=>false,configurable:false});
            })()""")
            block_all = [False]
            def readonly(route):
                request = route.request
                if not block_all[0] and request.method in ('GET', 'HEAD'):
                    route.continue_()
                else:
                    if block_all[0] and len(blocked) < 25:
                        u = urlsplit(request.url)
                        blocked.append({'method': request.method, 'host': u.hostname or '', 'path': u.path})
                    route.abort()
            raw.context.route('**/*', readonly)
            page.goto(URL, wait=3000)
            state['before'] = raw.evaluate(STRUCTURE)
            state['candidate'] = raw.evaluate(CANDIDATE)
            c = state['candidate']
            if not (c.get('unique') and c.get('context') and not c.get('insideForm')
                    and not c.get('insideLink') and c.get('enabled')):
                state.update(phase='завершено без нажатия', reason='Однозначная бесплатная кнопка не найдена')
            else:
                block_all[0] = True
                state['network_blocked_before_activation'] = True
                raw.get_by_role('button', name='Получить', exact=True).click(timeout=5000)
                page.wait(2000)
                state['after'] = raw.evaluate(STRUCTURE)
                state['blocked_after_activation'] = blocked
                state['phase'] = 'завершено: открытие без ссылки и сети'
        publisher.update(state, title='Осмотр без заявки завершён', conclusion='success')
        print('Read-only Boost-Like UI inspection completed; no target was entered and no order was sent.')
        return 0
    except Exception as exc:
        state.update(phase='ошибка', reason=verifier._safe_detail(str(exc) or type(exc).__name__))
        try:
            publisher.update(state, title='Осмотр без заявки прерван', conclusion='failure')
        except ProgressError:
            pass
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
