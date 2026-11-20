# -*- coding: utf-8 -*-
"""CI check publisher tests with no network or real orders."""
import os
import random
import string
import unittest
from unittest.mock import patch

from tools.ci_progress import GitHubProgress, ProgressError, decode_snapshot, encode_snapshot


class ProgressTests(unittest.TestCase):
    def test_snapshot_preserves_unicode_null_and_measurements(self):
        data = {'status': 'running', 'results': [{'site': 'Тест', 'before': 1,
                                                   'after': None, 'change': None,
                                                   'samples': [1, 1, 2]}]}
        self.assertEqual(decode_snapshot(encode_snapshot(data)), data)

    def test_no_repository_sha_or_token_means_no_requests(self):
        with patch.dict(os.environ, {'GITHUB_REPOSITORY': '', 'GITHUB_SHA': '',
                                     'VKKK_PROGRESS_TOKEN': ''}):
            with self.assertRaises(ProgressError):
                GitHubProgress(name='probe')

    def test_create_and_complete_use_the_same_check_id(self):
        env = {'GITHUB_REPOSITORY': 'owner/repo', 'GITHUB_SHA': 'a' * 40,
               'VKKK_PROGRESS_TOKEN': 'local-test-only'}
        with patch.dict(os.environ, env), patch.object(GitHubProgress, '_request',
                                                       side_effect=[{'id': 42}, {}]) as request:
            pub = GitHubProgress(name='probe')
            self.assertEqual(pub.create({'count': 1}), 42)
            pub.update({'count': 2}, conclusion='success')
        self.assertEqual(request.call_args_list[0].args[0:2], ('POST', '/check-runs'))
        self.assertEqual(request.call_args_list[1].args[0:2], ('PATCH', '/check-runs/42'))
        self.assertEqual(request.call_args_list[1].args[2]['conclusion'], 'success')

    def test_oversized_snapshot_is_rejected_instead_of_truncated(self):
        rng = random.Random(42)
        text = ''.join(rng.choice(string.ascii_letters + string.digits) for _ in range(80000))
        with self.assertRaisesRegex(ProgressError, 'слишком велик'):
            GitHubProgress._output({'data': text}, 'title')


if __name__ == '__main__':
    unittest.main()
