# -*- coding: utf-8 -*-
"""Проверки VK-источников: фикстуры и локальный HTTP, без отправки заявок."""
import json
import os
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from core import delivery, network, verifier

FIXTURES = Path(__file__).parent / 'fixtures'
GID = '243396555'
TARGET = {'platform': 'vk', 'url': f'https://vk.ru/club{GID}'}


def entry(count=1, gid=GID, screen_name=None):
    return {'method': 'groups.getById',
            'request': {'fields': 'is_member,is_closed,members_count,can_post', 'group_ids': gid},
            'version': '5.289',
            'response': {'groups': [{'id': int(gid), 'members_count': count,
                                     'screen_name': screen_name or f'club{gid}'}], 'profiles': []}}


def html_for(entries):
    data = json.dumps(entries, ensure_ascii=False, separators=(',', ':'))
    return ('<!doctype html><html><body><script>\nwindow.cur.apiPrefetchCache = '
            + data + ';\nwindow.apiPrefetchReadyResolve();\n</script></body></html>')


class WidgetSelectorTests(unittest.TestCase):
    def test_observed_widget_selector_and_owner(self):
        html = (FIXTURES / 'vk_widget_count.html').read_text(encoding='utf-8')
        self.assertEqual(delivery.parse_widget_count(html, GID), 1)
        with self.assertRaises(delivery.CounterError):
            delivery.parse_widget_count(html, '123')
        with self.assertRaises(delivery.CounterError):
            delivery.parse_widget_count(html + '<script>oid: -123</script>', GID)
        for suffix in ('e1', '.5', 'other'):
            spoofed = html.replace(f'oid: -{GID},', f'oid: -{GID}{suffix},')
            with self.subTest(suffix=suffix), self.assertRaises(delivery.CounterError):
                delivery.parse_widget_count(spoofed, GID)

    def test_widget_only_accepts_exact_grouped_numbers(self):
        def widget(value):
            return f'<script>oid: -{GID}</script><a id="members_count">{value}</a>'
        for text, value in [('1&nbsp;follower', 1), ('0 подписчиков', 0),
                            ('10 546 подписчиков', 10546), ('12,345 followers', 12345),
                            ('12\u202f345 участников', 12345), ('no followers', 0)]:
            with self.subTest(text=text):
                self.assertEqual(delivery.parse_widget_count(widget(text), GID), value)
        for text in ('1,2 тыс. подписчиков', '1.2K followers', '1,234 подписчиков',
                     '12 34 подписчика', '1000 100 участников', '12.345 followers',
                     '9223372036854775808 followers', '9' * 5000 + ' followers'):
            with self.subTest(text=text[:50]), self.assertRaises(delivery.CounterError):
                delivery.parse_widget_count(widget(text), GID)

    def test_widget_rejects_untrusted_final_url_and_partial_response(self):
        body = (FIXTURES / 'vk_widget_count.html').read_text(encoding='utf-8')
        bad_urls = ['http://vk.ru/widget_community.php', 'https://evil.example/widget_community.php',
                    'https://user@vk.ru/widget_community.php', 'https://vk.ru:444/widget_community.php',
                    'https://vk.ru/club243396555', '']
        for final in bad_urls:
            with self.subTest(url=final), patch.object(network, 'http_request', return_value={
                    'ok': True, 'status': 200, 'url': final, 'text': body}), \
                    self.assertRaises(delivery.CounterError):
                delivery.read_vk_widget(GID)
        for status in (None, 206, 304):
            with self.subTest(status=status), patch.object(network, 'http_request', return_value={
                    'ok': True, 'status': status, 'url': 'https://vk.ru/widget_community.php', 'text': body}), \
                    self.assertRaises(delivery.CounterError):
                delivery.read_vk_widget(GID)


class MobilePrefetchParserTests(unittest.TestCase):
    def test_observed_mobile_prefetch_is_structured_not_page_wide_regex(self):
        html = (FIXTURES / 'vk_mobile_prefetch.html').read_text(encoding='utf-8')
        self.assertEqual(delivery.parse_vk_page_count(html, GID), 1)
        # These other counts must not be picked up as subscriber counts.
        html = html.replace('<body>', '<body><div>123 456 followers</div>')
        html += '<script>var other = {"id":243396555,"members_count":999};</script>'
        self.assertEqual(delivery.parse_vk_page_count(html, GID), 1)
        # Отдельная предзагрузка другой группы не подменяет проверяемую.
        self.assertEqual(delivery.parse_vk_page_count(html_for([entry(999, '123'), entry(1)]), GID), 1)

    def test_exact_zero_and_large_integer_are_valid(self):
        for count in (0, 42, 123456789):
            with self.subTest(count=count):
                self.assertEqual(delivery.parse_vk_page_count(html_for([entry(count)]), GID), count)

    def test_rejects_rounded_nonnumeric_and_out_of_range_values(self):
        for count in (True, False, -1, 1.0, '1', '1.2K', None, 2**63):
            with self.subTest(count=count), self.assertRaises(delivery.CounterError):
                delivery.parse_vk_page_count(html_for([entry(count)]), GID)

    def test_rejects_wrong_request_response_or_ambiguous_groups(self):
        changes = []
        for requested in ('123', '243396555,123', '', True, None):
            rec = entry(); rec['request']['group_ids'] = requested; changes.append(rec)
        for fields in ('is_member', 'other_members_count', None):
            rec = entry(); rec['request']['fields'] = fields; changes.append(rec)
        for returned in (123, True, '243396555', 243396555.0, None):
            rec = entry(); rec['response']['groups'][0]['id'] = returned; changes.append(rec)
        rec = entry(); rec['response']['groups'] = []; changes.append(rec)
        rec = entry(); rec['response']['groups'].append({'id': 123, 'members_count': 99}); changes.append(rec)
        rec = entry(); rec['method'] = 'users.get'; changes.append(rec)
        for rec in changes:
            with self.subTest(record=rec), self.assertRaises(delivery.CounterError):
                delivery.parse_vk_page_count(html_for([rec]), GID)
        with self.assertRaises(delivery.CounterError):
            delivery.parse_vk_page_count(html_for([entry(), entry()]), GID)

    def test_rejects_missing_or_spoofed_scripts_and_duplicate_json_keys(self):
        valid = html_for([entry()])
        script = valid[valid.index('<script>'):valid.index('</script>') + len('</script>')]
        cases = [
            '<p>1 follower</p>',
            valid.replace('<script>', '<!--<script>').replace('</script>', '</script>-->'),
            valid.replace('window.cur.apiPrefetchCache = ', 'var fake = "window.cur.apiPrefetchCache = ";'),
            valid + script,
            valid.replace('window.apiPrefetchReadyResolve();', 'window.cur.apiPrefetchCache = [];'),
            valid.replace('"members_count":1', '"members_count":1,"members_count":999'),
            valid.replace('"members_count":1', '"members_count":NaN'),
        ]
        for html in cases:
            with self.subTest(html=html[:110]), self.assertRaises(delivery.CounterError):
                delivery.parse_vk_page_count(html, GID)


class MobileFallbackTests(unittest.TestCase):
    def test_api_and_widget_failure_fall_back_and_report_page_source(self):
        calls = []
        body = html_for([entry(47)])
        proxy = 'http://user:password@proxy.test:8080'

        def fake_request(url, **kwargs):
            calls.append((url, kwargs))
            if url.startswith('https://api.vk.com/'):
                return {'ok': True, 'text': json.dumps({'error': {'error_code': 5}})}
            if '/widget_community.php?' in url:
                return {'ok': True, 'status': 200, 'url': url, 'text': '<html>no counter</html>'}
            return {'ok': True, 'status': 200, 'url': url, 'text': body}

        source = []
        with patch.object(network, 'http_request', side_effect=fake_request), \
             patch.dict(os.environ, {'VKFREE_VK_TOKEN': 'secret-token'}):
            self.assertEqual(delivery.read_vk_count(TARGET, 'followers', proxy=proxy, source_out=source), 47)
        self.assertEqual(source, ['мобильная страница VK'])
        self.assertEqual(len(calls), 4)
        self.assertTrue(calls[0][0].startswith('https://api.vk.com/'))
        self.assertTrue(calls[1][0].startswith('https://vk.com/widget_community.php?'))
        self.assertTrue(calls[2][0].startswith('https://vk.ru/widget_community.php?'))
        self.assertTrue(calls[3][0].startswith(f'https://m.vk.ru/club{GID}?'))
        for url, kwargs in calls:
            self.assertEqual(kwargs['proxy'], proxy)
            self.assertNotIn('secret-token', url)
        self.assertNotIn('data', calls[3][1])
        self.assertIn('no-store', calls[3][1]['headers']['Cache-Control'])
        self.assertEqual(calls[3][1]['headers']['Pragma'], 'no-cache')

    def test_widget_success_does_not_use_page(self):
        source = []
        with patch.object(delivery, 'read_vk_widget', return_value=3), \
             patch.object(delivery, 'read_vk_page') as page, \
             patch.dict(os.environ, {'VKFREE_VK_TOKEN': ''}):
            self.assertEqual(delivery.read_vk_count(TARGET, 'followers', source_out=source), 3)
        page.assert_not_called()
        self.assertEqual(source, ['виджет VK'])

    def test_page_tries_another_mobile_host_after_invalid_response(self):
        body = html_for([entry(9)])
        calls = []
        def fake(url, **kwargs):
            calls.append((url, kwargs))
            if len(calls) == 1:
                return {'ok': True, 'status': 200, 'url': 'https://m.vk.ru/club999', 'text': body}
            return {'ok': True, 'status': 200, 'url': url, 'text': body}
        with patch.object(network, 'http_request', side_effect=fake):
            self.assertEqual(delivery.read_vk_page(GID, proxy='socks5h://127.0.0.1:1080'), 9)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1][0].startswith('https://m.vk.com/'))
        self.assertTrue(all(c[1]['proxy'] == 'socks5h://127.0.0.1:1080' for c in calls))

    def test_page_rejects_untrusted_redirects_and_accepts_confirmed_alias(self):
        body = html_for([entry(7, screen_name='our.community')])
        invalid = ['https://evil.example/our.community', 'https://m.vk.ru/club999',
                   'https://m.vk.ru/login', 'http://m.vk.ru/our.community',
                   'https://user@m.vk.ru/our.community', 'https://m.vk.ru:444/our.community',
                   'https://m.vk.ru/someone.else', 'https://m.vk.ru/our.community/other']
        for final in invalid:
            with self.subTest(final=final), \
                 patch.object(network, 'http_request', return_value={'ok': True, 'status': 200, 'url': final, 'text': body}), \
                 self.assertRaises(delivery.CounterError):
                delivery.read_vk_page(GID)
        with patch.object(network, 'http_request', return_value={
                'ok': True, 'status': 200, 'url': 'https://m.vk.ru/our.community', 'text': body}):
            self.assertEqual(delivery.read_vk_page(GID), 7)

    def test_page_requires_successful_complete_response_and_final_url(self):
        body = html_for([entry(7)])
        for status in (None, 206, 304):
            with self.subTest(status=status), patch.object(network, 'http_request', return_value={
                    'ok': True, 'status': status, 'url': f'https://m.vk.ru/club{GID}', 'text': body}), \
                    self.assertRaises(delivery.CounterError):
                delivery.read_vk_page(GID)
        with patch.object(network, 'http_request', return_value={'ok': True, 'status': 200, 'text': body}), \
                self.assertRaises(delivery.CounterError):
            delivery.read_vk_page(GID)
        for reader in (delivery.read_vk_page, delivery.read_vk_widget):
            for invalid in ('0', '0123', '1' * 16, '../feed', ''):
                with self.subTest(reader=reader.__name__, gid=invalid), \
                        patch.object(network, 'http_request') as request:
                    with self.assertRaises(delivery.CounterError):
                        reader(invalid)
                    request.assert_not_called()

    def test_all_sources_unavailable_does_not_invent_count(self):
        source = []
        with patch.object(network, 'http_request', return_value={'ok': False, 'error': 'offline'}), \
             patch.dict(os.environ, {'VKFREE_VK_TOKEN': ''}), \
             self.assertRaises(delivery.CounterError) as caught:
            delivery.read_vk_count(TARGET, 'followers', source_out=source)
        self.assertEqual(source, [])
        self.assertIn('виджет', str(caught.exception))
        self.assertIn('страница', str(caught.exception))

    def test_end_to_end_local_http_page_is_reloaded_on_each_measurement(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.server.requests.append((self.path, dict(self.headers)))
                if self.path.startswith('/widget_community.php?'):
                    data = b'<html>widget unavailable</html>'
                else:
                    self.server.measurements += 1
                    data = html_for([entry(1 if self.server.measurements == 1 else 4)]).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):
                pass

        srv = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        srv.requests, srv.measurements = [], 0
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        real_request = network.http_request

        def local_request(url, **kwargs):
            # Транспорт реальный, но к локальному серверу; подставляем исходный
            # публичный URL как конечный, чтобы проверить защиту от редиректа.
            parsed = urllib.parse.urlsplit(url)
            local = f'http://127.0.0.1:{srv.server_port}{parsed.path}?{parsed.query}'
            result = real_request(local, **kwargs)
            if result.get('ok') and result.get('url') == local:
                result['url'] = url
            return result

        try:
            route = network.Endpoint(id='direct', kind='direct', label='direct', proxy_url=None)
            with patch.object(network, 'http_request', side_effect=local_request), \
                 patch.dict(os.environ, {'VKFREE_VK_TOKEN': ''}):
                first = verifier._counter(TARGET, [route])
                second = verifier._counter(TARGET, [route])
            self.assertEqual(first, (1, 'direct', 'мобильная страница VK'))
            self.assertEqual(second, (4, 'direct', 'мобильная страница VK'))
            self.assertEqual(srv.measurements, 2)
            pages = [(path, headers) for path, headers in srv.requests if path.startswith('/club')]
            self.assertEqual(len(pages), 2)
            self.assertNotEqual(pages[0][0], pages[1][0])
            self.assertTrue(all('no-store' in h.get('Cache-Control', '') for _, h in pages))
            widgets = [(path, headers) for path, headers in srv.requests if path.startswith('/widget_community.php?')]
            self.assertEqual(len(widgets), 4)
            self.assertNotEqual(widgets[0][0], widgets[2][0])
            self.assertTrue(all('no-store' in h.get('Cache-Control', '') for _, h in widgets))
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
