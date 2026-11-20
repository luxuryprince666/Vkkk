# -*- coding: utf-8 -*-
"""Tests of CI reporting only: no external requests and no real orders."""
import base64
import contextlib
import io
import json
import os
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools import run_live_ci
from tools.ci_progress import ProgressError


class LiveCIReportTests(unittest.TestCase):
    def test_real_runner_needs_explicit_ci_authorization(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS': '', 'VKKK_LIVE_RUN': ''}):
            with self.assertRaisesRegex(RuntimeError, 'одноразового'):
                run_live_ci.run('https://vk.ru/club1', Path('/unused'))

    def test_ci_rerun_attempt_cannot_repeat_the_campaign(self):
        env = {'GITHUB_ACTIONS': 'true', 'VKKK_LIVE_RUN': 'authorized-once',
               'GITHUB_RUN_ATTEMPT': '2', 'GITHUB_EVENT_NAME': 'push',
               'GITHUB_REPOSITORY': 'luxuryprince666/Vkkk',
               'GITHUB_REF': 'refs/heads/arena/e372c48b-vkkk'}
        with patch.dict(os.environ, env), patch.object(run_live_ci.db, 'init_db') as init:
            with self.assertRaisesRegex(RuntimeError, 'одноразового'):
                run_live_ci.run('https://vk.ru/club243396555', Path('/unused'))
        init.assert_not_called()

    def test_compact_result_keeps_unknown_distinct_from_zero(self):
        row = {'site_name': 'Site', 'baseline_count': 1, 'final_count': None,
               'added_count': None, 'attempts': []}
        out = run_live_ci.compact_result(row)
        self.assertEqual(out['before'], 1)
        self.assertIsNone(out['after'])
        self.assertIsNone(out['change'])

    def test_growth_event_ignores_measurements_before_possible_submission(self):
        row = {'baseline_count': 1, 'submitted_at': 20,
               'samples': [{'count': 2, 'observed_at': 10},
                           {'count': 1, 'observed_at': 21},
                           {'count': 3, 'observed_at': 30}]}
        self.assertEqual(run_live_ci.first_growth_sample(row), row['samples'][2])
        row['submitted_at'] = None
        self.assertIsNone(run_live_ci.first_growth_sample(row))
        row['submitted_at'] = 40
        self.assertIsNone(run_live_ci.first_growth_sample(row))

    def test_github_notice_escapes_percent_and_newlines(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            run_live_ci.notice('STATUS', {'detail': '100%\nне заказ'})
        text = out.getvalue()
        self.assertEqual(text.count('\n'), 1)
        self.assertIn('100%25', text)
        self.assertIn('\\nне заказ', text)

    def test_report_chunks_reassemble_exact_json(self):
        campaign = {'id': 1, 'status': 'failed', 'channel_url': 'https://vk.ru/club123',
                    'initial_count': 1, 'current_count': 1, 'error_reason': 'нет заказа',
                    'results': [{'site_name': 'Тест', 'status': 'skipped'}],
                    'large': ''.join(chr(32 + ((i * 73) % 90)) for i in range(10000))}
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(out):
            run_live_ci.emit_report(campaign, Path(folder))
            saved = json.loads((Path(folder) / 'vk-live-report.json').read_text(encoding='utf-8'))
            text = (Path(folder) / 'vk-live-report.txt').read_text(encoding='utf-8')
        payloads = []
        for line in out.getvalue().splitlines():
            if '::REPORT_CHUNK ' in line:
                payload = line.split('::REPORT_CHUNK ', 1)[1].replace('%25', '%')
                payloads.append(json.loads(payload))
        self.assertTrue(payloads)
        self.assertEqual([p['part'] for p in payloads], list(range(1, len(payloads) + 1)))
        self.assertTrue(all(p['total'] == len(payloads) for p in payloads))
        blob = ''.join(p['zlib_base64'] for p in payloads)
        restored = json.loads(zlib.decompress(base64.b64decode(blob)).decode('utf-8'))
        self.assertEqual(restored, campaign)
        self.assertEqual(saved, campaign)
        self.assertIn('Тест — не проверено', text)


class LiveCIFlowTests(unittest.TestCase):
    def test_one_shot_records_full_terminal_snapshot_and_uses_long_window(self):
        from contextlib import contextmanager
        @contextmanager
        def browser(**_kwargs):
            yield 'playwright', type('Page', (), {'js': lambda self, _: 2})()
        pub = MagicMock()
        pub.create.return_value = 42
        campaign = {'id': 1, 'status': 'completed', 'channel_url': 'https://vk.ru/club123',
                    'initial_count': 1, 'current_count': 1, 'growth_timeout_sec': 900,
                    'results': [], 'baseline_samples': []}
        env = {'GITHUB_ACTIONS': 'true', 'VKKK_LIVE_RUN': 'authorized-once',
               'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_EVENT_NAME': 'push',
               'GITHUB_REPOSITORY': 'luxuryprince666/Vkkk',
               'GITHUB_REF': 'refs/heads/arena/e372c48b-vkkk'}
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, env), \
             patch.object(run_live_ci.db, 'init_db'), patch.object(run_live_ci.db, 'set_setting') as setting, \
             patch.object(run_live_ci.stealth, 'backend_status',
                          return_value={'backends': [{'id': 'playwright', 'ready': True}]}), \
             patch.object(run_live_ci.stealth, 'open_page', browser), \
             patch.object(run_live_ci.verifier, '_counter', return_value=(1, 'direct', 'виджет VK')), \
             patch.object(run_live_ci.verifier, 'start', return_value={'campaign_id': 1}), \
             patch.object(run_live_ci.verifier, 'status', return_value=campaign), \
             patch.object(run_live_ci, 'GitHubProgress', return_value=pub), \
             patch.object(run_live_ci.time, 'sleep'), patch.object(run_live_ci.signal, 'signal'), \
             contextlib.redirect_stdout(io.StringIO()):
            code = run_live_ci.run('https://vk.ru/club243396555', Path(folder))
            saved = json.loads((Path(folder) / 'vk-live-report.json').read_text(encoding='utf-8'))
        self.assertEqual(code, 0)
        self.assertEqual(saved, campaign)
        setting.assert_any_call('verify_timeout_sec', '900')
        setting.assert_any_call('captcha_autosolve', '0')
        setting.assert_any_call('nojs_enabled', '0')
        self.assertEqual(pub.update.call_args.kwargs['conclusion'], 'success')
        self.assertEqual(pub.update.call_args.args[0]['campaign'], campaign)

    def test_no_progress_channel_means_no_order_is_started(self):
        from contextlib import contextmanager
        @contextmanager
        def browser(**_kwargs):
            yield 'playwright', type('Page', (), {'js': lambda self, _: 2})()
        env = {'GITHUB_ACTIONS': 'true', 'VKKK_LIVE_RUN': 'authorized-once',
               'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_EVENT_NAME': 'push',
               'GITHUB_REPOSITORY': 'luxuryprince666/Vkkk',
               'GITHUB_REF': 'refs/heads/arena/e372c48b-vkkk'}
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, env), \
             patch.object(run_live_ci.db, 'init_db'), patch.object(run_live_ci.db, 'set_setting'), \
             patch.object(run_live_ci.stealth, 'backend_status',
                          return_value={'backends': [{'id': 'playwright', 'ready': True}]}), \
             patch.object(run_live_ci.stealth, 'open_page', browser), \
             patch.object(run_live_ci.verifier, '_counter', return_value=(1, 'direct', 'виджет VK')), \
             patch.object(run_live_ci.verifier, 'start') as start, \
             patch.object(run_live_ci, 'GitHubProgress', side_effect=ProgressError('offline')), \
             patch.object(run_live_ci.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()):
            code = run_live_ci.run('https://vk.ru/club243396555', Path(folder))
        self.assertEqual(code, 2)
        start.assert_not_called()

    def test_lost_progress_channel_stops_campaign_and_keeps_unknowns(self):
        from contextlib import contextmanager
        @contextmanager
        def browser(**_kwargs):
            yield 'playwright', type('Page', (), {'js': lambda self, _: 2})()
        pub = MagicMock()
        pub.create.return_value = 42
        pub.update.side_effect = [ProgressError('offline'), None]
        running = {'id': 1, 'status': 'running', 'channel_url': 'https://vk.ru/club123',
                   'initial_count': 1, 'current_count': 1,
                   'results': [{'id': 2, 'site_name': 'Test', 'status': 'running',
                                'baseline_count': 1, 'added_count': None, 'samples': [], 'attempts': []}]}
        stopped = {**running, 'status': 'stopped', 'results': [{**running['results'][0], 'status': 'unverified'}]}
        env = {'GITHUB_ACTIONS': 'true', 'VKKK_LIVE_RUN': 'authorized-once',
               'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_EVENT_NAME': 'push',
               'GITHUB_REPOSITORY': 'luxuryprince666/Vkkk',
               'GITHUB_REF': 'refs/heads/arena/e372c48b-vkkk'}
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, env), \
             patch.object(run_live_ci.db, 'init_db'), patch.object(run_live_ci.db, 'set_setting'), \
             patch.object(run_live_ci.stealth, 'backend_status',
                          return_value={'backends': [{'id': 'playwright', 'ready': True}]}), \
             patch.object(run_live_ci.stealth, 'open_page', browser), \
             patch.object(run_live_ci.verifier, '_counter', return_value=(1, 'direct', 'виджет VK')), \
             patch.object(run_live_ci.verifier, 'start', return_value={'campaign_id': 1}), \
             patch.object(run_live_ci.verifier, 'status', side_effect=[running, stopped]), \
             patch.object(run_live_ci.verifier, 'stop') as stop, \
             patch.object(run_live_ci, 'GitHubProgress', return_value=pub), \
             patch.object(run_live_ci.time, 'sleep'), patch.object(run_live_ci.signal, 'signal'), \
             contextlib.redirect_stdout(io.StringIO()):
            code = run_live_ci.run('https://vk.ru/club243396555', Path(folder))
            saved = json.loads((Path(folder) / 'vk-live-report.json').read_text(encoding='utf-8'))
        self.assertEqual(code, 2)
        stop.assert_called_once_with(1)
        self.assertIsNone(saved['results'][0]['added_count'])
        self.assertEqual(pub.update.call_args.kwargs['conclusion'], 'failure')


if __name__ == '__main__':
    unittest.main()
