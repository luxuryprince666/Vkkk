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
from unittest.mock import patch

from tools import run_live_ci


class LiveCIReportTests(unittest.TestCase):
    def test_real_runner_needs_explicit_ci_authorization(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS': '', 'VKKK_LIVE_RUN': ''}):
            with self.assertRaisesRegex(RuntimeError, 'одноразового'):
                run_live_ci.run('https://vk.ru/club1', Path('/unused'))

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


if __name__ == '__main__':
    unittest.main()
