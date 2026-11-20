# -*- coding: utf-8 -*-
"""Offline tests for backing up read-only GitHub check snapshots."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import follow_live_ci as follow
from tools.ci_progress import encode_snapshot


class FollowLiveTests(unittest.TestCase):
    def test_completed_snapshot_keeps_unknown_result_unknown(self):
        snapshot = {'phase': 'completed', 'campaign': {'status': 'completed',
                    'channel_url': 'https://vk.ru/club123', 'initial_count': 1, 'current_count': 1,
                    'results': [{'site_name': 'Тест', 'status': 'skipped', 'added_count': None}]}}
        check = {'id': 123, 'name': follow.CHECK_NAME, 'status': 'completed', 'conclusion': 'success',
                 'output': {'summary': follow.MARKER + encode_snapshot(snapshot)}}
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(follow, 'github_api', return_value={'check_runs': [check]}):
            path = Path(folder) / 'report'
            self.assertEqual(follow.follow('owner/repo', 'a' * 40, 42, path, 30), 0)
            saved = json.loads(path.with_suffix('.json').read_text(encoding='utf-8'))
            text = path.with_suffix('.txt').read_text(encoding='utf-8')
        self.assertIsNone(saved['campaign']['results'][0]['added_count'])
        self.assertIn('Тест — не проверено', text)
        self.assertIn('наблюдаемый прирост: нет данных', text)

    def test_no_check_after_failed_workflow_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(follow, 'github_api',
             side_effect=[{'check_runs': []}, {'status': 'completed', 'conclusion': 'failure'}]):
            self.assertEqual(follow.follow('owner/repo', 'a' * 40, 42, Path(folder) / 'report', 30), 2)


if __name__ == '__main__':
    unittest.main()
