# -*- coding: utf-8 -*-
"""HTTP API проверяемой кампании на локальном сервере и поддельном счётчике."""
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from core import db, delivery, drivers, server


class VerifyApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='vkfree-api-verify-')
        self.old = db.DATA, db.DB_PATH, db.BACKUP_DIR
        db.DATA = Path(self.temp.name)
        db.DB_PATH = db.DATA / 'app.db'
        db.BACKUP_DIR = db.DATA / 'backups'
        db.init_db()
        db.set_setting('global_min_gap_sec', '0')
        with db.tx() as conn:
            conn.execute("UPDATE sites SET enabled=CASE WHEN id='prskill' THEN 1 ELSE 0 END")
        self.http = server.QuietServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:%d' % self.http.server_address[1]
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        db.DATA, db.DB_PATH, db.BACKUP_DIR = self.old
        self.temp.cleanup()

    def request(self, path, payload=None, method=None, headers=None, raw=None):
        body = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
        headers = {'Content-Type': 'application/json', **(headers or {})}
        req = urllib.request.Request(self.base + path, data=body, headers=headers,
                                     method=method or ('POST' if body is not None else 'GET'))
        try:
            with self.opener.open(req, timeout=8) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def wait_status(self, cid, terminal=True):
        for _ in range(200):
            code, result = self.request('/api/verify/status?id=%d' % cid)
            self.assertEqual(code, 200)
            campaign = result['campaign']
            if not terminal or campaign['status'] not in ('running', 'stopping'):
                return result
            time.sleep(.02)
        self.fail('campaign did not finish')

    def test_baseline_unavailable_returns_unknown_without_order(self):
        with patch.object(delivery, 'read_target_count', side_effect=delivery.CounterError('offline')), \
             patch.object(drivers, 'run') as submit:
            code, started = self.request('/api/verify/start', {'url': 'https://vk.ru/club243396555'})
            self.assertEqual(code, 200)
            result = self.wait_status(started['campaign_id'])
        submit.assert_not_called()
        self.assertIsNone(result['active'])
        c = result['campaign']
        self.assertEqual(c['status'], 'failed')
        self.assertIsNone(c['initial_count'])
        self.assertIsNone(c['total_added'])
        self.assertTrue(all(r['added_count'] is None and r['status'] == 'skipped' for r in c['results']))
        code, history = self.request('/api/verify/history')
        self.assertEqual(code, 200)
        self.assertEqual(history['campaigns'][0]['id'], c['id'])
        self.assertIsNone(history['campaigns'][0]['total_added'])
        code, missing = self.request('/api/verify/status?id=999999')
        self.assertEqual(code, 404)
        self.assertFalse(missing['ok'])

    def test_active_campaign_stop_and_concurrent_start(self):
        began = threading.Event()
        def submit(svc, link, **kwargs):
            kwargs['on_submit']()
            began.set()
            kwargs['cancel_event'].wait(4)
            return drivers.DriverResult('cancelled', 'отменено')
        with patch.object(delivery, 'read_target_count', return_value=100), \
             patch.object(drivers, 'run', side_effect=submit):
            code, started = self.request('/api/verify/start', {'url': 'https://vk.ru/club243396555'})
            self.assertEqual(code, 200)
            cid = started['campaign_id']
            try:
                self.assertTrue(began.wait(4))
                info = self.wait_status(cid, terminal=False)
                self.assertEqual(info['active']['id'], cid)
                code, denied = self.request('/api/verify/start', {'url': 'https://vk.ru/club243396555'})
                self.assertEqual(code, 400)
                self.assertFalse(denied['ok'])
            finally:
                self.request('/api/verify/stop', {'campaign_id': cid})
            info = self.wait_status(cid)
        self.assertEqual(info['campaign']['status'], 'stopped')
        r = next(r for r in info['campaign']['results'] if r['site_id'] == 'prskill')
        self.assertEqual(r['status'], 'unverified')
        self.assertIsNone(r['added_count'])
        self.assertIsNone(info['active'])
        self.assertEqual(info['unresolved_count'], 1)
        code, _ = self.request('/api/verify/acknowledge', {'confirm': False})
        self.assertEqual(code, 400)
        code, result = self.request('/api/verify/acknowledge', {'confirm': True})
        self.assertEqual((code, result['acknowledged']), (200, 1))
        self.assertEqual(self.request('/api/verify/status')[1]['unresolved_count'], 0)

    def test_network_test_returns_real_source_not_configured_guess(self):
        for actual in ('виджет VK', 'мобильная страница VK'):
            def count(*args, **kwargs):
                kwargs['source_out'].append(actual)
                return 42
            with self.subTest(actual=actual), patch.object(delivery, 'read_target_count', side_effect=count):
                code, result = self.request('/api/network/test', {'url': 'https://vk.ru/club243396555'})
                self.assertEqual(code, 200)
                self.assertEqual(result['results'][0]['count'], 42)
                self.assertEqual(result['results'][0]['source'], actual)

    def test_input_and_settings_validation(self):
        for body in ({'url': 'https://evil.example/vk.ru/club1'}, {'url': 'https://t.me/channelname/123'}):
            code, result = self.request('/api/verify/start', body)
            self.assertEqual(code, 400)
            self.assertFalse(result['ok'])
        code, _ = self.request('/api/settings', {'verify_timeout_sec': '30'})
        self.assertEqual(code, 400)
        code, _ = self.request('/api/verify/status?id=nope')
        self.assertEqual(code, 400)
        code, _ = self.request('/api/verify/stop', {})
        self.assertEqual(code, 400)
        code, _ = self.request('/api/settings', raw=b'null')
        self.assertEqual(code, 400)

    def test_cross_origin_mutations_and_non_json_are_rejected(self):
        code, _ = self.request('/api/settings', {'autopilot': '0'}, headers={'Origin': 'https://evil.example'})
        self.assertEqual(code, 400)
        self.assertEqual(db.get_setting('autopilot'), '1')
        code, _ = self.request('/api/settings', raw=b'{"autopilot":"0"}', headers={'Content-Type': 'text/plain'})
        self.assertEqual(code, 400)
        code, _ = self.request('/api/settings', {'autopilot': '0'}, headers={'Origin': self.base})
        self.assertEqual(code, 200)
        self.assertEqual(db.get_setting('autopilot'), '0')


if __name__ == '__main__':
    unittest.main()
