# -*- coding: utf-8 -*-
"""Offline guard tests: no external pages or real orders."""
import copy
import json
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from contextlib import contextmanager

from tools import ci_resume_after_cancel_preflight as preflight
from tools import run_resume_after_cancel_ci as resume
from tools.ci_progress import encode_snapshot
from tools.run_unsent_followup_ci import MARKER

ROOT = Path(__file__).resolve().parent.parent


def workflow(run_id, sha, conclusion='success', updated='2026-11-20T17:22:31Z'):
    return {'id': run_id, 'head_sha': sha, 'status': 'completed', 'conclusion': conclusion,
            'run_attempt': 1, 'updated_at': updated}


def check(check_id, sha, name, snapshot):
    return {'id': check_id, 'head_sha': sha, 'name': name, 'status': 'completed',
            'conclusion': 'success', 'output': {'summary': MARKER + encode_snapshot(snapshot)}}


class ResumeEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.recovered = json.loads((ROOT / 'reports/vk-continuation-44771191791.json').read_text(encoding='utf-8'))
        self.extra = json.loads((ROOT / 'reports/vk-extra-readonly-44785954124.json').read_text(encoding='utf-8'))
        self.cancel = workflow(resume.CANCEL_RUN, resume.CANCEL_SHA, 'cancelled')
        self.recovery = workflow(resume.RECOVERY_RUN, resume.RECOVERY_SHA)
        self.extra_run = workflow(resume.EXTRA_RUN, resume.EXTRA_SHA)

    def validate(self):
        rc = check(resume.RECOVERY_CHECK, resume.RECOVERY_SHA,
                   'VK cancelled continuation artifact recovery (read only)', self.recovered)
        ec = check(resume.EXTRA_CHECK, resume.EXTRA_SHA, 'VK additional free sites read-only', self.extra)
        with patch.object(resume.continuation, 'validate_previous', return_value=self.recovered['prior']):
            return resume.validate_evidence(self.cancel, self.recovery, rc, {}, {}, self.extra_run, ec,
                                            now=1795197000)

    def test_exact_evidence_waits_from_runner_completion_and_never_retries_interrupted_sites(self):
        result = self.validate()
        self.assertEqual(result['ended_at'], 1795195351)  # 17:22:31 UTC
        self.assertGreater(result['ended_at'], result['last_snapshot_at'])
        self.assertEqual(result['inspected_count'], 1)
        self.assertTrue({'pricesmm', 'smm8', 'sprintersmm', 'lowcostsmm', 'martinismm', 'capitansmm', 'captainsmm'}
                        <= set(resume.QUEUE))
        self.assertFalse({'doctorsmm', 'avi1', 'nakruti', 'prtut'} & set(resume.QUEUE))

    def test_running_or_rerun_prior_and_wrong_target_fail_closed(self):
        self.cancel['status'] = 'in_progress'
        with self.assertRaises(ValueError):
            self.validate()
        self.cancel['status'], self.cancel['run_attempt'] = 'completed', 2
        with self.assertRaises(ValueError):
            self.validate()
        self.cancel['run_attempt'] = 1
        self.recovered['target'] = 'https://vk.ru/club999'
        with self.assertRaises(ValueError):
            self.validate()

    def test_interrupted_site_and_unsaved_time_are_never_treated_as_unsent(self):
        self.recovered['current']['site_id'] = 'other'
        with self.assertRaises(ValueError):
            self.validate()
        self.recovered['current']['site_id'] = 'prtut'
        with patch.object(resume, 'QUEUE', resume.QUEUE + ('prtut',)):
            with self.assertRaises(ValueError):
                self.validate()
        self.cancel['updated_at'] = '2026-11-20T17:21:00Z'
        with self.assertRaises(ValueError):
            self.validate()

    def test_recovery_and_previous_submissions_must_match(self):
        self.recovered['artifact_recovery']['orders_sent'] = 1
        with self.assertRaises(ValueError):
            self.validate()
        self.recovered['artifact_recovery']['orders_sent'] = 0
        self.recovered['results'][0]['submitted_at'] = 1795193000
        with self.assertRaises(ValueError):
            self.validate()
        self.recovered['results'][0]['submitted_at'] = None
        self.recovered['results'][2]['attempts'][0]['submitted_at'] = None
        with self.assertRaises(ValueError):
            self.validate()

    def test_each_extra_free_form_needs_specific_readonly_guard_and_service(self):
        for name in resume.EXTRA_SITES:
            with self.subTest(site=name):
                old = copy.deepcopy(self.extra)
                row = next(x for x in self.extra['sites'] if x['site'] == name)
                row['candidate_check']['free_guard_on_empty_field'] = False
                with self.assertRaises(ValueError):
                    self.validate()
                self.extra = old
        row = next(x for x in self.extra['sites'] if x['site'] == 'MartiniSMM')
        row['structure']['selectedPublicService']['id'] = '123'
        with self.assertRaises(ValueError):
            self.validate()

    def test_catalog_requires_exact_reviewed_community_urls(self):
        catalog = json.loads((ROOT / 'data/catalog.json').read_text(encoding='utf-8'))
        resume.validate_catalog(catalog)
        site = next(x for x in catalog['sites'] if x['id'] == 'pricesmm')
        site['services'][0]['url'] = 'https://pricesmm.com/paid-order/'
        with self.assertRaises(ValueError):
            resume.validate_catalog(catalog)
        catalog = json.loads((ROOT / 'data/catalog.json').read_text(encoding='utf-8'))
        next(x for x in catalog['sites'] if x['id'] == 'lowcostsmm')['enabled'] = True
        with self.assertRaises(ValueError):
            resume.validate_catalog(catalog)

    def test_second_workflow_run_never_allowed(self):
        with patch.object(resume, 'github_get', return_value={'workflow_runs': [{'id': 100}]}):
            resume.ensure_one_run('not-a-real-token', 100, attempts=1, delay=0)
        with patch.object(resume, 'github_get', return_value={'workflow_runs': [{'id': 99}, {'id': 100}]}):
            with self.assertRaises(RuntimeError):
                resume.ensure_one_run('not-a-real-token', 100, attempts=1, delay=0)
        with patch.object(resume, 'github_get', return_value={'workflow_runs': []}), \
             patch.object(resume.time, 'sleep'):
            with self.assertRaises(RuntimeError):
                resume.ensure_one_run('not-a-real-token', 100, attempts=2, delay=0)

    def test_no_live_action_outside_scoped_ci(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(resume, 'evidence') as load:
            with self.assertRaises(RuntimeError):
                resume.run(Path('/nonexistent'))
        load.assert_not_called()

    def test_readonly_preflight_keeps_evidence_when_examining_forms(self):
        page = MagicMock()
        page.js.return_value = 2
        page._p.evaluate.return_value = {'title': 'synthetic form'}
        @contextmanager
        def browser(**kwargs):
            yield 'playwright', page
        env = {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': resume.REPO,
               'GITHUB_REF': 'refs/heads/' + resume.BRANCH, 'VKKK_PROGRESS_TOKEN': 'fake'}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(preflight, 'GitHubProgress') as progress, \
             patch.object(resume, 'evidence', return_value={'ended_at': 1795195351}), \
             patch.object(preflight.db, 'init_db'), patch.object(preflight.stealth, 'open_page', browser), \
             patch.object(preflight.drivers, 'discover', return_value={'fields': []}), \
             patch.object(preflight.verifier, '_counter', return_value=(1, 'direct', 'synthetic')), \
             patch.object(preflight.time, 'sleep'):
            self.assertEqual(preflight.main(), 0)
        state = progress.return_value.update.call_args.args[0]
        self.assertEqual(state['prior_runner_ended_at'], 1795195351)
        self.assertEqual(state['orders_sent'], 0)
        self.assertEqual(len(state['inspected_forms_without_input']), 2)
        self.assertEqual(progress.return_value.update.call_args.kwargs['conclusion'], 'success')


if __name__ == '__main__':
    unittest.main()
