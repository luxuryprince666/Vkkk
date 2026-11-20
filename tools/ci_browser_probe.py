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
from tools.ci_progress import GitHubProgress, ProgressError  # noqa: E402
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
        publisher = GitHubProgress(name='VK read-only readiness snapshot')
        check_id = publisher.create({'phase': 'подготовка', 'count': count, 'orders_sent': 0})
        notice('PREP_CHECK', {'check_id': check_id})
    except ProgressError as exc:
        notice('PREP_BLOCKED', {'reason': 'Не удалось открыть канал отчёта; заявок нет', 'detail': str(exc)})
        return 2
    observations = []
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
                    observations.append({'site': site['name'], 'visited': False, 'orders_sent': 0})
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
                    # Check form scoping with the existing empty field only; no target is entered.
                    scoped = drivers.same_form_state(page, state, field['sel']) if field else None
                    selected = drivers.pick_submit_button(scoped.get('buttons') or []) if scoped else None
                    out['verified_scope'] = {
                        'candidate': {k: selected.get(k) for k in ('sel', 'text', 'disabled')} if selected else None,
                        'free_guard_for_existing_value': bool(selected and drivers.free_form_guard(
                            page, field['sel'], selected['sel'], drivers.field_value(page, field['sel']), service['url'])),
                        'countdown_sec': drivers._button_countdown(page, selected['sel']) if selected else None,
                    }
                    if site['id'] == 'prskill':
                        special = drivers._prskill_giveaway_state(page)
                        out['prskill_state'] = special.get('state')
                    if site['id'] == 'smmnakrutka':
                        out['smmnakrutka_state'] = drivers._smmnakrutka_state(page, 'vk', 10).get('state')
                        # A visible popup may be an advert or a countdown, not the free form.
                        # Capture only structure, never input/token values.
                        out['smmnakrutka_popups'] = page._p.evaluate('''() => {
                          const vis = e => { const r=e.getBoundingClientRect(),s=getComputedStyle(e);
                            return r.width>4 && r.height>4 && s.display!=='none' && s.visibility!=='hidden'; };
                          return [...document.querySelectorAll('.t-popup.t-popup_show')].filter(vis).map(p => ({
                            id:p.id, classes:String(p.className).slice(0,100),
                            text:(p.innerText || '').trim().replace(/\\s+/g,' ').slice(0,400),
                            closers:[...p.querySelectorAll('[class*=close],button,[role=button]')].filter(e =>
                              /close|закрыть/i.test(String(e.className)+' '+(e.getAttribute('aria-label')||'')))
                              .map(e=>({tag:e.tagName,id:e.id,classes:String(e.className).slice(0,120),
                                aria:e.getAttribute('aria-label')||'',visible:vis(e),inForm:!!e.closest('form')})).slice(0,8),
                            forms:[...p.querySelectorAll('form')].map(f=>({id:f.id,visible:vis(f),
                              method:f.getAttribute('method')||'',actionType:f.getAttribute('data-formactiontype')||'',
                              fields:[...f.querySelectorAll('input,select')].map(e=>({name:e.name,id:e.id,
                                type:e.type,visible:vis(e)})).slice(0,25),
                              buttons:[...f.querySelectorAll('button')].map(e=>({text:(e.innerText||'').trim(),
                                type:e.type,visible:vis(e)})).slice(0,5)})).slice(0,5)})).slice(0,5);
                        }''')
                    if site['id'] in ('doctorsmm', 'smm8'):
                        # Only public structure: never collect input values, cookies or tokens.
                        out['extra_structure'] = page._p.evaluate('''() => {
                          const visible = e => !!(e.getBoundingClientRect().width &&
                              e.getBoundingClientRect().height && getComputedStyle(e).visibility !== 'hidden');
                          const fields = [...document.querySelectorAll('input, select, textarea')]
                            .filter(e => /free|optin|link|captcha|turnstile/i.test(e.id + ' ' + e.name + ' ' + e.placeholder))
                            .map(e => ({tag:e.tagName,id:e.id,name:e.name,type:e.type,ph:e.placeholder,
                              visible:visible(e)})).slice(0,30);
                          const buttons = [...document.querySelectorAll('button, a, input[type=submit]')]
                            .filter(e => /free|optin|get it now|попробовать|бесплатно/i.test(
                              e.id + ' ' + e.className + ' ' + (e.innerText || e.value || '')))
                            .map(e => ({tag:e.tagName,id:e.id,classes:String(e.className).slice(0,90),
                              text:(e.innerText || e.value || '').trim().slice(0,100),visible:visible(e),
                              href:e.tagName === 'A' ? (e.getAttribute('href') || '').split('?')[0] : ''})).slice(0,25);
                          const forms = [...document.querySelectorAll('form')].map(e => ({id:e.id,
                            method:e.getAttribute('method') || '', action:(e.getAttribute('action') || '').split('?')[0],
                            visible:visible(e)})).slice(0,20);
                          const frames = [...document.querySelectorAll('iframe')].map(e => ({id:e.id,
                            src:(e.getAttribute('src') || '').split('?')[0],visible:visible(e)})).slice(0,20);
                          const scripts = [...document.querySelectorAll('script[src]')].map(e =>
                            (e.getAttribute('src') || '').split('?')[0]).slice(0,30);
                          return {fields, buttons, forms, frames, scripts};
                        }''')
                    # Describe the target form and the explicitly identified free
                    # section without collecting field values, cookies or tokens.
                    out['form_scope'] = page._p.evaluate('''(sel) => {
                      const field = sel ? document.querySelector(sel) : null;
                      const form = field && (field.form || field.closest('form'));
                      const free = document.querySelector('#giveaway-form');
                      const describe = root => root ? {
                        tag: root.tagName, id: root.id || '',
                        action: (root.getAttribute('action') || '').split('?')[0],
                        method: root.getAttribute('method') || '',
                        hidden_names: [...root.querySelectorAll('input[type=hidden]')].map(e=>e.name).slice(0,20),
                        fields: [...root.querySelectorAll('input, textarea')].filter(e => e.type !== 'hidden')
                          .map(e => ({id:e.id, name:e.name, ph:e.placeholder, type:e.type})).slice(0,12),
                        buttons: [...root.querySelectorAll('button, input[type=submit], a')]
                          .map(e => ({tag:e.tagName, id:e.id, text:(e.innerText||e.value||'').trim().slice(0,75)}))
                          .filter(e => e.text).slice(0,15)
                      } : null;
                      return {form:describe(form), free_section:describe(free)};
                    }''', field.get('sel') if field else '')
                    if site['id'] == 'prskill':
                        out['prskill_giveaway'] = page._p.evaluate('''() => {
                          const i = [...document.querySelectorAll('input')].find(e =>
                            (e.placeholder || '') === 'https://vk.com/***');
                          if (!i) return null;
                          const chain = [];
                          for (let e = i, depth = 0; e && depth < 6; e = e.parentElement, depth++) {
                            chain.push({tag:e.tagName, id:e.id, classes:String(e.className || '').slice(0,120),
                              text:(e.innerText || '').trim().replace(/\\s+/g,' ').slice(0,900),
                              fields:[...e.querySelectorAll('input')].map(x => ({id:x.id, ph:x.placeholder,
                                type:x.type, name:x.name})).slice(0,12),
                              buttons:[...e.querySelectorAll('button')].map(x => ({id:x.id,
                                text:(x.innerText||'').trim().slice(0,80)})).slice(0,10)});
                          }
                          return {chain, widgetCount:document.querySelectorAll('[data-sitekey],iframe[src*=turnstile]').length,
                            tokenInputs:[...document.querySelectorAll('input[name*=turnstile],input[name*=captcha]')]
                              .map(x=>({name:x.name, nonempty:!!x.value}))};
                        }''')
                except Exception as exc:
                    out['error'] = verifier._safe_detail(str(exc))
                observations.append(out)
                publisher.update({'phase': 'осмотр без ввода ссылки', 'count': count,
                                  'sites': observations, 'orders_sent': 0}, title='Read-only проверка браузера')
    except Exception as exc:
        notice('PREP_BLOCKED', {'reason': 'Браузер/канал отчёта недоступен; заявок нет',
                                'detail': verifier._safe_detail(str(exc))})
        try:
            publisher.update({'phase': 'ошибка', 'count': count, 'sites': observations, 'orders_sent': 0},
                             title='Read-only проверка прервана', conclusion='failure')
        except ProgressError:
            pass
        return 2
    publisher.update({'phase': 'завершено', 'count': count, 'sites': observations, 'orders_sent': 0},
                     title='Read-only проверка завершена', conclusion='success')
    notice('PREP_SITES', {'sites': observations})
    notice('PREP_FINISHED', {'orders_sent': 0, 'note': 'read-only; доставка не проверена'})
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
