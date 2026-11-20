# -*- coding: utf-8 -*-
"""Offline-only tests for the explicitly scoped continuation; no real orders."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core import db, delivery, drivers, verifier
from tools import run_continuation_ci as continuation
from tools.ci_progress import encode_snapshot


def previous_documents():
    submitted = 1000
    doctor = {'site_id': 'doctorsmm', 'site_name': 'DoctorSMM', 'status': 'unverified',
              'baseline_count': 1, 'final_count': 1, 'submitted_at': submitted,
              'failure_reason': 'неизвестно',
              'samples': [{'observed_at': 1901, 'count': 1}],
              'attempts': [{'submitted_at': submitted, 'observation_window_sec': 900}]}
    rows = [{'site_id': x, 'site_name': x, 'status': 'skipped', 'submitted_at': None,
             'attempts': []} for x in continuation.SITES] + [doctor]
    snap = {'actions_run_id': str(continuation.PRIOR_RUN), 'phase': 'failed',
            'campaign': {'status': 'failed', 'channel_url': continuation.TARGET,
                         'initial_count': 1, 'current_count': 1, 'results': rows}}
    check = {'id': continuation.PRIOR_CHECK, 'head_sha': continuation.PRIOR_SHA,
             'name': 'VK sequential live verification snapshot', 'status': 'completed',
             'conclusion': 'failure', 'output': {'summary': continuation.MARKER + encode_snapshot(snap)}}
    workflow = {'id': continuation.PRIOR_RUN, 'head_sha': continuation.PRIOR_SHA, 'run_attempt': 1,
                'status': 'completed', 'conclusion': 'failure'}
    return check, workflow, snap


def attach(check, snap):
    check['output']['summary'] = continuation.MARKER + encode_snapshot(snap)


class PriorGuardTests(unittest.TestCase):
    def test_exact_completed_failed_campaign_can_select_only_previously_unsent(self):
        check, workflow, snap = previous_documents()
        value = continuation.validate_previous(check, workflow)
        self.assertEqual(value['doctor']['status'], 'unverified')
        self.assertEqual(value['doctor']['baseline_count'], 1)
        self.assertNotIn('doctorsmm', continuation.SITES)
        self.assertEqual(len(continuation.SITES), len(set(continuation.SITES)))
        self.assertEqual(len(snap['campaign']['results']), 9)

    def test_running_rerun_wrong_id_sha_or_check_never_accepted(self):
        cases = [{'status': 'in_progress'}, {'conclusion': 'success'}, {'run_attempt': 2},
                 {'head_sha': '0' * 40}, {'id': 7}]
        for changes in cases:
            check, workflow, _ = previous_documents()
            workflow.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                continuation.validate_previous(check, workflow)
        for changes in ({'status': 'in_progress'}, {'conclusion': 'success'}, {'id': 7}):
            check, workflow, _ = previous_documents()
            check.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                continuation.validate_previous(check, workflow)

    def test_other_possible_submission_or_non_skipped_site_blocks_all_orders(self):
        for field in ('row', 'attempt', 'status'):
            check, workflow, snap = previous_documents()
            row = snap['campaign']['results'][0]
            if field == 'row':
                row['submitted_at'] = 100
            elif field == 'attempt':
                row['attempts'] = [{'submitted_at': 100}]
            else:
                row['status'] = 'unverified'
            attach(check, snap)
            with self.subTest(field=field), self.assertRaises(ValueError):
                continuation.validate_previous(check, workflow)

    def test_no_last_counter_sample_no_queue(self):
        check, workflow, snap = previous_documents()
        snap['campaign']['results'][-1]['samples'][-1]['observed_at'] = 1899
        attach(check, snap)
        with self.assertRaises(ValueError):
            continuation.validate_previous(check, workflow)
        check, workflow, snap = previous_documents()
        snap['campaign']['results'][-1]['final_count'] = 2
        attach(check, snap)
        with self.assertRaises(ValueError):
            continuation.validate_previous(check, workflow)

    def test_wrong_target_duplicate_missing_or_ambiguous_snapshot_blocks(self):
        for change in ('target', 'duplicate', 'missing', 'running'):
            check, workflow, snap = previous_documents()
            if change == 'target':
                snap['campaign']['channel_url'] = 'https://vk.ru/club123'
            elif change == 'duplicate':
                snap['campaign']['results'].append(copy.deepcopy(snap['campaign']['results'][0]))
            elif change == 'missing':
                snap['campaign']['results'].pop(0)
            else:
                snap['campaign']['results'][0]['status'] = 'running'
            attach(check, snap)
            with self.subTest(change=change), self.assertRaises(ValueError):
                continuation.validate_previous(check, workflow)

    def test_outside_authorized_ci_is_blocked_before_api_or_db(self):
        with patch.dict('os.environ', {'GITHUB_ACTIONS': '', 'VKKK_LIVE_RUN': ''}), \
             patch.object(continuation, 'github_get') as api, \
             patch.object(continuation.db, 'init_db') as init:
            with self.assertRaises(RuntimeError):
                continuation.run(Path('/unused'))
        api.assert_not_called()
        init.assert_not_called()


class MeasurementTests(unittest.TestCase):
    def test_unknown_delivery_is_not_converted_to_zero(self):
        row = {'site_id': 'x', 'status': 'skipped', 'baseline_count': 1, 'final_count': None,
               'submitted_at': None, 'samples': [{'observed_at': 1, 'count': 1}]}
        result = continuation.measured_row(row, 'completed')
        self.assertIsNone(result['observed_counter_change'])
        self.assertIsNone(result['attributed_delivery_count'])
        self.assertIsNone(result['first_observed_increase_sec'])

    def test_measured_zero_and_uncertain_delivery_remain_distinct(self):
        row = {'site_id': 'x', 'status': 'unverified', 'baseline_count': 1, 'final_count': 1,
               'submitted_at': 10, 'samples': [{'observed_at': 11, 'count': 1}]}
        result = continuation.measured_row(row, 'failed')
        self.assertEqual(result['observed_counter_change'], 0)
        self.assertIsNone(result['attributed_delivery_count'])
        state = {'prior': {}, 'phase': 'завершено', 'results': [result]}
        text = continuation.render_report(state)
        self.assertIn('изменение публичного счётчика: 0', text)
        self.assertIn('Доставка именно этой площадкой: неизвестна', text)
        self.assertIn('Нулевое изменение счётчика относится только к указанному интервалу', text)

    def test_short_interrupted_window_reports_measurement_without_claiming_delivery(self):
        row = {'site_id': 'avi1', 'status': 'skipped', 'baseline_count': 1, 'final_count': None,
               'submitted_at': 10, 'samples': [{'observed_at': 10, 'count': 1},
                                                {'observed_at': 11.7, 'count': 1}]}
        result = continuation.measured_row(row, 'completed')
        self.assertEqual(result['final_count'], 1)
        self.assertEqual(result['observed_counter_change'], 0)
        self.assertIsNone(result['attributed_delivery_count'])

    def test_growth_time_uses_first_post_submit_sample_but_never_claims_source(self):
        row = {'site_id': 'x', 'status': 'working', 'baseline_count': 1, 'final_count': 3,
               'submitted_at': 10, 'samples': [{'observed_at': 9, 'count': 4},
                                                {'observed_at': 11, 'count': 1},
                                                {'observed_at': 25, 'count': 2},
                                                {'observed_at': 40, 'count': 3}]}
        result = continuation.measured_row(row, 'completed')
        self.assertEqual(result['observed_counter_change'], 2)
        self.assertEqual(result['first_observed_increase_sec'], 15)
        self.assertIsNone(result['attributed_delivery_count'])
        self.assertIn('запоздалая доставка', result['attribution_warning'])

    def test_stopped_missing_counter_short_or_stale_watch_blocks_next_site(self):
        row = {'status': 'unverified', 'submitted_at': 1000, 'final_count': 1,
               'attempts': [{'submitted_at': 1000, 'observation_window_sec': 900}],
               'samples': [{'observed_at': 1901, 'count': 1}]}
        self.assertTrue(continuation.can_continue({'status': 'failed'}, row, now=1910)[0])
        self.assertFalse(continuation.can_continue({'status': 'stopped'}, row, now=1910)[0])
        self.assertFalse(continuation.can_continue({'status': 'failed'}, row, now=2200)[0])
        short = copy.deepcopy(row)
        short['samples'][-1]['observed_at'] = 1800
        self.assertFalse(continuation.can_continue({'status': 'failed'}, short, now=1810)[0])
        short = copy.deepcopy(row)
        short['submitted_at'] = None
        self.assertFalse(continuation.can_continue({'status': 'failed'}, short, now=1910)[0])
        short = copy.deepcopy(row)
        short['final_count'] = None
        self.assertFalse(continuation.can_continue({'status': 'failed'}, short, now=1910)[0])
        self.assertFalse(continuation.can_continue({'status': 'failed'}, {'status': 'skipped'}, now=1910)[0])

    def test_completed_skipped_after_possible_click_or_short_watch_blocks_next(self):
        row = {'site_id': 'avi1', 'status': 'skipped', 'submitted_at': 1000, 'final_count': None,
               'attempts': [{'submitted_at': 1000, 'observation_window_sec': 900}],
               'samples': [{'observed_at': 1002, 'count': 1}]}
        self.assertFalse(continuation.can_continue({'status': 'completed'}, row, now=1003)[0])
        row['status'], row['final_count'] = 'not_working', 1
        self.assertFalse(continuation.can_continue({'status': 'completed'}, row, now=1003)[0])
        row['samples'][-1]['observed_at'] = 1901
        self.assertTrue(continuation.can_continue({'status': 'completed'}, row, now=1910)[0])
        row['site_id'] = 'smm8'  # здесь даже 15 минут не покрывают заявленный час
        self.assertFalse(continuation.can_continue({'status': 'completed'}, row, now=1910)[0])
        row['attempts'][0]['observation_window_sec'] = 3600
        row['samples'][-1]['observed_at'] = 4601
        self.assertTrue(continuation.can_continue({'status': 'completed'}, row, now=4610)[0])
        row['attempts'][0]['late_recorded'] = True
        self.assertFalse(continuation.can_continue({'status': 'completed'}, row, now=4610)[0])

    def test_initial_counter_stable_after_one_hour_not_before(self):
        now = [1000.0]
        def clock():
            return now[0]
        def sleep(seconds):
            now[0] += seconds
        state = {'prior': {'doctor': {'submitted_at': 1000.0}}, 'quarantine_samples': []}
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(continuation, 'PRIOR_WAIT_SECONDS', 180), \
             patch.object(continuation, 'STABLE_SECONDS', 60), \
             patch.object(continuation, 'POLL_SECONDS', 30):
            continuation.wait_for_prior(state, MagicMock(), Path(folder), [False],
                                        read_count=lambda: (1, 'direct', 'виджет VK'),
                                        clock=clock, sleep=sleep)
            self.assertGreaterEqual(now[0], 1180)
            self.assertEqual(state['quarantine_samples'][-1]['count'], 1)
            data = json.loads((Path(folder) / 'vk-continuation.json').read_text())
            self.assertEqual(data['quarantine_samples'][0]['count'], 1)

    def test_counter_failure_breaks_stability_and_prevents_orders(self):
        now = [1000.0]
        def clock():
            return now[0]
        def sleep(seconds):
            now[0] += seconds
        def no_count():
            raise delivery.CounterError('offline')
        state = {'prior': {'doctor': {'submitted_at': 1000.0}}, 'quarantine_samples': []}
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(continuation, 'PRIOR_WAIT_SECONDS', 180), \
             patch.object(continuation, 'POLL_SECONDS', 30):
            with self.assertRaisesRegex(RuntimeError, 'Счётчик недоступен'):
                continuation.wait_for_prior(state, MagicMock(), Path(folder), [False],
                                            read_count=no_count, clock=clock, sleep=sleep)
        self.assertEqual(state['quarantine_samples'], [])


class LocalDuplicateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = (db.DATA, db.DB_PATH, db.BACKUP_DIR)
        db.DATA = Path(self.tmp.name)
        db.DB_PATH = db.DATA / 'test.sqlite'
        db.BACKUP_DIR = db.DATA / 'backups'
        db.init_db()

    def tearDown(self):
        db.DATA, db.DB_PATH, db.BACKUP_DIR = self.original
        self.tmp.cleanup()

    def test_no_duplicate_candidate_even_without_a_previous_click(self):
        with self.assertRaises(RuntimeError):
            continuation.ensure_not_repeated('smmnakrutka', ['smmnakrutka'])
        with self.assertRaises(RuntimeError):
            continuation.ensure_not_repeated('doctorsmm', [])
        continuation.ensure_not_repeated('smmnakrutka', [])

    def test_two_isolated_campaigns_run_serially_and_record_only_measured_counts(self):
        continuation.configure()
        db.set_setting('global_min_gap_sec', '0')
        events = []
        def fake_driver(svc, target, **kwargs):
            events.append('start:' + svc['site_id'])
            if svc['site_id'] == 'smmnakrutka':
                return drivers.DriverResult('need_human', 'форма не готова')
            kwargs['on_submit']()
            # К моменту, когда драйверу разрешён клик, снимок уже записан.
            snap = json.loads((Path(folder) / 'vk-continuation.json').read_text())
            if snap['current']['row']['submitted_at'] is not None:
                events.append('durable')
            events.append('submit:' + svc['site_id'])
            return drivers.DriverResult('ok', 'форма принята')
        def fake_observe(read, baseline, started, **kwargs):
            kwargs['on_sample'](read(), started + 1)
            events.append('observe')
            return verifier.Observation('not_working', baseline, 0)
        state = {'prior': {}, 'results': [], 'phase': 'подготовка', 'current': None}
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(verifier, '_counter', return_value=(1, 'direct', 'виджет VK')), \
             patch.object(drivers, 'run', side_effect=fake_driver), \
             patch.object(verifier, 'observe_growth', side_effect=fake_observe):
            first, row1 = continuation.run_site('smmnakrutka', state, MagicMock(), Path(folder), [False],
                                                 continuation.time.monotonic() + 60)
            state['results'].append(continuation.measured_row(row1, first['status']))
            self.assertTrue(continuation.can_continue(first, row1)[0])
            second, row2 = continuation.run_site('avi1', state, MagicMock(), Path(folder), [False],
                                                  continuation.time.monotonic() + 60)
        self.assertEqual([event for event in events if event != 'observe'],
                         ['start:smmnakrutka', 'start:avi1', 'durable', 'submit:avi1'])
        self.assertIn('observe', events)  # наблюдение может начаться параллельно с записью снимка
        self.assertEqual(first['status'], 'completed')
        self.assertEqual(second['status'], 'completed')
        result = continuation.measured_row(row2, second['status'])
        self.assertEqual(result['observed_counter_change'], 0)
        self.assertIsNone(result['attributed_delivery_count'])
        with self.assertRaises(RuntimeError):
            continuation.ensure_not_repeated('avi1', ['smmnakrutka'])


if __name__ == '__main__':
    unittest.main()
