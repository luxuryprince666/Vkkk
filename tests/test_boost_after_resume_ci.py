# -*- coding: utf-8 -*-
"""Offline evidence, quarantine, and reporting tests. No real orders."""
import copy
import unittest
from datetime import datetime, timezone

from tools import run_boost_after_resume_ci as run
from tools.ci_progress import encode_snapshot
from tools.run_unsent_followup_ci import MARKER, TARGET


def completed(id_, sha):
    return {'id': id_, 'head_sha': sha, 'status': 'completed', 'conclusion': 'success', 'run_attempt': 1}


def check(id_, sha, name, state):
    return {'id': id_, 'head_sha': sha, 'name': name, 'status': 'completed', 'conclusion': 'success',
            'output': {'summary': MARKER + encode_snapshot(state)}}


def fixture():
    ended = 1_005_000.0
    rows = []
    for site in run.resume.QUEUE:
        rows.append({'site_id': site, 'verifier_status': 'skipped', 'campaign_status': 'completed',
                     'submitted_at': None, 'final_count': None, 'attempts': [], 'samples': []})
    rows[0].update(verifier_status='unverified', campaign_status='failed', submitted_at=1_003_000.0,
                   final_count=1,
                   attempts=[{'submitted_at': 1_003_000.0, 'observation_window_sec': 900,
                              'late_recorded': False}],
                   samples=[{'observed_at': 1_004_000.0, 'count': 1}])
    state = {'actions_run_id': str(run.PRIOR_RUN), 'target': TARGET, 'phase': 'завершено',
             'error': None, 'current': None, 'queue': list(run.resume.QUEUE), 'results': rows}
    inspected = {'phase': 'завершено', 'orders_sent': 0, 'target_entered': False,
                 'clicked': False, 'changing_requests_allowed': False, 'initial_card': {'valid': True},
                 'module_path': run.attempt.MODULE, 'module_sha256': 'a' * 64}
    return [completed(run.PRIOR_RUN, run.PRIOR_SHA),
            {'id': run.PRIOR_JOB, 'run_id': run.PRIOR_RUN, 'status': 'completed', 'conclusion': 'success',
             'completed_at': datetime.fromtimestamp(ended, timezone.utc).isoformat()},
            check(run.PRIOR_CHECK, run.PRIOR_SHA, run.resume.CHECK_NAME, state),
            completed(run.PREFLIGHT_RUN, run.PREFLIGHT_SHA),
            check(run.PREFLIGHT_CHECK, run.PREFLIGHT_SHA, run.preflight.CHECK, inspected)]


def replace_snapshot(item, fn):
    from tools.ci_progress import decode_snapshot
    state = decode_snapshot(item['output']['summary'].split(MARKER, 1)[1])
    fn(state)
    item['output']['summary'] = MARKER + encode_snapshot(state)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.args = fixture()

    def validate(self):
        return run.validate_evidence(*self.args, now=1_005_100.0)

    def test_exact_completed_runs_and_full_window_are_accepted(self):
        result = self.validate()
        self.assertEqual(result['prior_ended_at'], 1_005_000.0)
        self.assertEqual(result['module_sha256'], 'a' * 64)
        self.assertEqual(len(result['prior_sites']), 7)

    def test_incomplete_cancelled_or_duplicate_evidence_is_rejected(self):
        for change in ('workflow', 'job', 'queue', 'target', 'preflight'):
            with self.subTest(change=change):
                self.args = fixture()
                if change == 'workflow': self.args[0]['conclusion'] = 'cancelled'
                if change == 'job': self.args[1]['status'] = 'in_progress'
                if change == 'queue': replace_snapshot(self.args[2], lambda s: s['results'].pop())
                if change == 'target': replace_snapshot(self.args[2], lambda s: s.update(target='https://vk.ru/club999'))
                if change == 'preflight': replace_snapshot(self.args[4], lambda s: s.update(target_entered=True))
                with self.assertRaises(ValueError): self.validate()

    def test_smm8_requires_an_hour_of_observation(self):
        def modify(s):
            row = s['results'][-1]
            row.update(verifier_status='unverified', campaign_status='failed', submitted_at=1_000_000.0,
                       final_count=1, attempts=[{'submitted_at': 1_000_000.0, 'observation_window_sec': 900}],
                       samples=[{'observed_at': 1_004_000.0, 'count': 1}])
        replace_snapshot(self.args[2], modify)
        with self.assertRaises(ValueError): self.validate()

    def test_short_window_and_late_recorded_submission_are_rejected(self):
        for change in ('short', 'late', 'duplicate'):
            with self.subTest(change=change):
                self.args = fixture()
                def modify(s):
                    row = s['results'][0]
                    if change == 'short': row['attempts'][0]['observation_window_sec'] = 30
                    if change == 'late': row['attempts'][0]['late_recorded'] = True
                    if change == 'duplicate': row['attempts'].append(copy.deepcopy(row['attempts'][0]))
                replace_snapshot(self.args[2], modify)
                with self.assertRaises(ValueError): self.validate()


class QuarantineTests(unittest.TestCase):
    def test_waits_hour_after_prior_and_two_minutes_stable(self):
        t = [4600.0]
        state = {'prior_ended_at': 1000.0, 'quarantine_samples': []}
        published = []
        run.wait_quarantine(state, lambda: (1, 'direct', 'mock'),
                            lambda s: published.append(len(s['quarantine_samples'])), lambda: False,
                            clock=lambda: t[0], sleep=lambda n: t.__setitem__(0, t[0]+n), poll=30)
        self.assertGreaterEqual(t[0], 4720.0)
        self.assertGreaterEqual(len(state['quarantine_samples']), 5)
        self.assertTrue(published)

    def test_change_resets_stability_and_error_never_becomes_zero(self):
        t, reads = [5000.0], [1, 1, 2, 2, 2, 2, 2]
        state = {'prior_ended_at': 1000.0, 'quarantine_samples': []}
        def count():
            return reads.pop(0), 'direct', 'mock'
        run.wait_quarantine(state, count, lambda _: None, lambda: False,
                            clock=lambda: t[0], sleep=lambda n: t.__setitem__(0, t[0]+n), poll=30)
        self.assertEqual(t[0], 5180.0)
        state = {'prior_ended_at': 1000.0, 'quarantine_samples': []}
        def unavailable():
            raise OSError('mock network failure')
        with self.assertRaises(RuntimeError):
            run.wait_quarantine(state, unavailable, lambda _: None, lambda: False,
                                clock=lambda: t[0], sleep=lambda n: t.__setitem__(0, t[0]+n),
                                poll=30, max_wait=60)
        self.assertEqual(state['quarantine_samples'], [])
        self.assertIsNotNone(state['counter_error'])

    def test_report_does_not_turn_baseline_into_post_action_zero(self):
        state = {'start': {'possible_at': 101}, 'complete': {}, 'baseline_count': 1,
                 'samples': [{'observed_at': 100, 'count': 1}], 'quarantine_samples': []}
        text = run.render(state)
        self.assertIn('последний замер: нет данных; чистое изменение: нет данных', text)
        state['samples'].append({'observed_at': 102, 'count': 1})
        self.assertIn('последний замер: 1; чистое изменение: 0', run.render(state))

    def test_first_growth_after_completion_has_its_own_elapsed_time(self):
        state = {'start': {'possible_at': 100}, 'complete': {'possible_at': 200}, 'baseline_count': 1,
                 'samples': [{'observed_at': 100, 'count': 1}, {'observed_at': 235.7, 'count': 10},
                             {'observed_at': 425, 'count': 11}], 'quarantine_samples': []}
        text = run.render(state)
        self.assertIn('роста после возможного действия: 135.7 с', text)
        self.assertIn('роста после возможного завершения: 35.7 с', text)
        self.assertIn('последний замер: 11; чистое изменение: 10', text)
