#!/usr/bin/env python3
"""Проверка нового UI в браузере: API кампаний подменён, настоящие заказы не создаются.

Нужны Playwright и браузер. Сервер должен быть запущен на временной базе:
    python tests/ui_verify_smoke.py http://127.0.0.1:8777
"""
import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import stealth

BASE = (sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:8777').rstrip('/')


def result(rid, name, status, added):
    return {'id': rid, 'site_name': name, 'service_label': 'Подписчики сообщества',
            'service_url': 'https://example.com/free',
            'baseline_count': 100, 'final_count': None if added is None else 100 + added, 'added_count': added,
            'time_to_growth_sec': 30 if added else None, 'status': status,
            'failure_reason': 'Нет данных о доставке' if added is None else (None if added else 'За пять минут нет прироста'),
            'samples': [] if added is None else [{'observed_at': 1700000030, 'count': 100 + added,
                         'source': 'виджет VK', 'route': 'direct'}],
            'attempts': [{'method': 'browser', 'route': 'direct', 'verdict': 'ok', 'detail': 'принято'}]}


def campaign(cid, status, name):
    return {'id': cid, 'status': status, 'channel_url': 'https://vk.ru/club243396555',
            'initial_count': 100, 'current_count': 105 if cid == 22 else 100,
            'total_added': 5 if cid == 22 else 0, 'growth_timeout_sec': 300, 'settle_sec': 90,
            'started_at': 1700000000, 'current_service': 'PRSkill' if status == 'running' else None,
            'current_phase': 'Ожидание фактического прироста' if status == 'running' else None,
            'results': [result(cid, name, 'working' if cid == 22 else 'not_working', 5 if cid == 22 else 0),
                        result(cid + 100, 'Неизвестная площадка', 'skipped', None)]}


def main():
    current, previous = campaign(22, 'running', 'Новая площадка'), campaign(10, 'completed', 'Предыдущая площадка')
    previous['results'][0]['service_url'] = 'javascript:alert(1)'  # не должна стать активной ссылкой
    state = {'started': False, 'active': None, 'stop_id': None, 'unresolved': 0, 'acknowledged': False}
    errors = []
    with sync_playwright() as p:
        options = {'headless': True, 'args': ['--no-sandbox']}
        exe = stealth._find_chrome()
        if exe:
            options['executable_path'] = exe
        browser = p.chromium.launch(**options)
        page = browser.new_page(viewport={'width': 1440, 'height': 950})
        page.on('pageerror', lambda exc: errors.append(str(exc)))
        page.on('dialog', lambda dialog: dialog.accept())

        def intercept(route):
            req = route.request
            url = urlsplit(req.url)
            if url.path == '/api/verify/start':
                payload = req.post_data_json
                assert payload['url'] == 'https://vk.ru/club243396555', payload
                state['started'] = True
                state['active'] = {'id': 22, 'status': 'running'}
                data = {'ok': True, 'campaign_id': 22}
            elif url.path == '/api/verify/stop':
                state['stop_id'] = req.post_data_json['campaign_id']
                assert state['stop_id'] == 22, state
                state['active'] = None
                state['unresolved'] = 1
                current['status'] = 'stopped'
                current['current_service'] = current['current_phase'] = None
                data = {'ok': True, 'campaign_id': 22, 'status': 'stopped'}
            elif url.path == '/api/verify/acknowledge':
                assert req.post_data_json == {'confirm': True}
                assert state['unresolved'] == 1
                state['unresolved'] = 0
                state['acknowledged'] = True
                data = {'ok': True, 'acknowledged': 1}
            elif url.path == '/api/verify/history':
                data = {'ok': True, 'campaigns': ([current, previous] if state['started'] else [previous])}
            elif url.path == '/api/verify/status':
                selected = (parse_qs(url.query).get('id') or [''])[0]
                if selected == '10':
                    item = previous
                elif selected == '22' or state['started']:
                    item = current
                else:
                    item = previous
                data = {'ok': True, 'campaign': item, 'active': state['active'],
                        'unresolved_count': state['unresolved']}
            else:
                raise AssertionError(req.url)
            route.fulfill(status=200, content_type='application/json', body=json.dumps(data, ensure_ascii=False))

        page.route('**/api/verify/**', intercept)
        page.route('**/api/network/test', lambda route: route.fulfill(
            status=200, content_type='application/json', body=json.dumps(
                {'ok': True, 'results': [{'ok': True, 'route': 'direct', 'count': 101, 'source': 'мобильная страница VK'}]},
                ensure_ascii=False)))
        page.goto(BASE, wait_until='domcontentloaded')
        page.wait_for_selector('#verify-url')
        page.wait_for_function('typeof ST !== "undefined" && ST !== null', timeout=10000)
        # Кнопка в шапке больше не открывает старый неподтверждаемый мастер.
        page.click('button[data-tab="settings"]')
        page.click('.btn-fullauto')
        assert page.locator('#tab-wizard').is_visible()
        assert page.locator('#verify-url').evaluate('(el) => document.activeElement === el')
        page.fill('#verify-url', 'https://vk.ru/club243396555')
        page.click('#verify-start')
        page.wait_for_selector('text=Новая площадка')
        assert page.locator('#verify-start').is_disabled()
        assert page.locator('#verify-stop').is_visible()
        assert '+5' in page.locator('#verify-result').inner_text()
        unknown = page.locator('#verify-result tr').filter(has_text='Неизвестная площадка')
        assert 'не проверено' in unknown.inner_text()
        assert unknown.locator('td').nth(2).inner_text() == '—'  # нет итогового счётчика
        assert unknown.locator('td').nth(3).inner_text() == '—'  # неизвестный прирост не равен нулю
        assert 'не доказывает источник подписок' in page.locator('#verify-result').inner_text()
        assert page.locator('#verify-result a[href="https://example.com/free"]').count() == 2
        page.locator('#verify-result details summary').first.click()
        page.wait_for_timeout(3300)
        assert page.locator('#verify-result details').first.get_attribute('open') is not None
        assert 'виджет VK' in page.locator('#verify-result details').first.inner_text()
        # После перезагрузки активная кампания восстанавливается из API.
        page.reload(wait_until='domcontentloaded')
        page.wait_for_selector('text=Новая площадка')
        assert page.locator('#verify-stop').is_visible()
        assert page.locator('#verify-start').is_disabled()
        page.wait_for_function("[...document.querySelector('#verify-history').options].some(o => o.value === '10')")
        page.select_option('#verify-history', '10')
        page.wait_for_selector('text=Предыдущая площадка')
        assert page.locator('#verify-result a[href^="javascript:"]').count() == 0
        assert page.locator('#verify-start').is_disabled()
        assert '#22' in page.locator('#verify-stop').inner_text()
        page.click('#verify-stop')
        page.wait_for_function("document.querySelector('#verify-stop').style.display === 'none'")
        assert state['stop_id'] == 22
        assert page.locator('#verify-start').is_enabled()
        page.wait_for_selector('#verify-risk button')
        assert 'непроверенные заявки: 1' in page.locator('#verify-risk').inner_text()
        page.click('#verify-risk button')
        page.wait_for_function("!document.querySelector('#verify-risk button')")
        assert state['acknowledged']
        # Настройки сети показывают источник, полученный из API.
        page.click('button[data-tab="settings"]')
        page.wait_for_function("document.querySelector('#counter-status').textContent.includes('мобильная страница')")
        page.click('button[onclick="networkTest()"]')
        page.wait_for_function("document.querySelector('#network-info').textContent.includes('мобильная страница VK')")
        page.click('button[data-tab="wizard"]')
        page.set_viewport_size({'width': 390, 'height': 844})
        page.wait_for_timeout(300)
        dims = page.evaluate('({view:innerWidth, doc:document.documentElement.scrollWidth})')
        assert dims['doc'] <= dims['view'] + 1, dims
        if os.environ.get('VKFREE_UI_SCREENSHOT'):
            page.screenshot(path=os.environ['VKFREE_UI_SCREENSHOT'], full_page=True)
        assert not errors, errors
        browser.close()
    print('UI проверен: запуск, опрос, детали, ссылки, перезагрузка, история, остановка, подтверждение риска, сеть и мобильная ширина.')
    print('API кампаний был подменён; настоящие площадки и заявки не использовались.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
