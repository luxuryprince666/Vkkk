# -*- coding: utf-8 -*-
"""A follow-up is allowed only for an earlier completed, unsent site."""
import copy
import os
import sys
import unittest
from unittest.mock import patch

from tools import run_unsent_followup_ci as followup
from tools.ci_progress import encode_snapshot


def documents():
    row = {'site_id': 'smmnakrutka', 'status': 'skipped', 'submitted_at': None,
           'attempts': [{'submitted_at': None, 'verdict': 'need_human'}]}
    snapshot = {'actions_run_id': str(followup.PRIOR_RUN), 'phase': 'completed',
                'campaign': {'status': 'completed', 'channel_url': followup.TARGET,
                             'results': [row, {'site_id': 'doctor', 'status': 'working', 'submitted_at': 99}]}}
    check = {'id': followup.PRIOR_CHECK, 'head_sha': followup.PRIOR_SHA,
             'name': 'VK sequential live verification snapshot', 'status': 'completed',
             'conclusion': 'success', 'output': {'summary': followup.MARKER + encode_snapshot(snapshot)}}
    workflow = {'id': followup.PRIOR_RUN, 'head_sha': followup.PRIOR_SHA, 'run_attempt': 1,
                'status': 'completed', 'conclusion': 'success'}
    return check, workflow, snapshot


def attach(check, snapshot):
    check['output']['summary'] = followup.MARKER + encode_snapshot(snapshot)


class UnsentFollowupTests(unittest.TestCase):
    def test_completed_unsent_site_can_be_selected(self):
        check, workflow, _ = documents()
        row = followup.validate_previous(check, workflow)
        self.assertEqual(row['site_id'], 'smmnakrutka')
        self.assertIsNone(row['submitted_at'])

    def test_in_progress_failed_or_rerun_workflow_is_blocked(self):
        for patch_values in ({'status': 'in_progress'}, {'conclusion': 'failure'}, {'run_attempt': 2}):
            check, workflow, _ = documents()
            workflow.update(patch_values)
            with self.subTest(patch_values=patch_values), self.assertRaises(ValueError):
                followup.validate_previous(check, workflow)

    def test_prior_possible_submission_never_retried(self):
        for where in ('result', 'attempt'):
            check, workflow, snapshot = documents()
            row = snapshot['campaign']['results'][0]
            if where == 'result':
                row['submitted_at'] = 123
            else:
                row['attempts'][0]['submitted_at'] = 123
            attach(check, snapshot)
            with self.subTest(where=where), self.assertRaises(ValueError):
                followup.validate_previous(check, workflow)

    def test_no_new_order_after_an_ambiguous_or_incomplete_prior_campaign(self):
        for status in ('running', 'pending', 'unverified'):
            check, workflow, snapshot = documents()
            snapshot['campaign']['results'][1]['status'] = status
            attach(check, snapshot)
            with self.subTest(status=status), self.assertRaises(ValueError):
                followup.validate_previous(check, workflow)

    def test_wrong_target_duplicate_site_or_missing_snapshot_are_blocked(self):
        check, workflow, snapshot = documents()
        snapshot['campaign']['channel_url'] = 'https://vk.ru/club123'
        attach(check, snapshot)
        with self.assertRaises(ValueError):
            followup.validate_previous(check, workflow)
        check, workflow, snapshot = documents()
        snapshot['campaign']['results'].append(copy.deepcopy(snapshot['campaign']['results'][0]))
        attach(check, snapshot)
        with self.assertRaises(ValueError):
            followup.validate_previous(check, workflow)
        check['output']['summary'] = ''
        with self.assertRaises(ValueError):
            followup.validate_previous(check, workflow)

    def test_outside_one_shot_ci_no_api_or_database_action_occurs(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS': '', 'VKKK_LIVE_RUN': ''}), \
             patch.object(sys, 'argv', ['run_unsent_followup_ci.py', '--output-dir', '/unused']), \
             patch.object(followup, 'github_get') as get, patch.object(followup.db, 'init_db') as init:
            with self.assertRaises(RuntimeError):
                followup.main()
        get.assert_not_called()
        init.assert_not_called()


if __name__ == '__main__':
    unittest.main()
