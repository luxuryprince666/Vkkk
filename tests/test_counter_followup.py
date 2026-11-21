# -*- coding: utf-8 -*-
"""No-order counter diagnostic: all network and clocks are mocked."""
import copy
import unittest

from tools import ci_vk_counter_followup as counter
from tools.ci_progress import encode_snapshot
from tools.run_unsent_followup_ci import MARKER, TARGET


def previous():
    state = {'phase': 'завершено', 'actions_run_id': str(counter.BOOST_RUN), 'target': TARGET,
             'baseline_count': 1, 'provider_claimed_order': True,
             'start': {'possible_at': 10.0, 'request_released': True, 'http_status': 200},
             'complete': {'possible_at': 20.0, 'request_released': True, 'http_status': 200},
             'observation': {'status': 'working', 'net_change': 10, 'final_count': 11, 'window_sec': 3600},
             'samples': [{'count': 1, 'observed_at': 9.0}, {'count': 11, 'observed_at': 3630.0}]}
    workflow = {'id': counter.BOOST_RUN, 'head_sha': counter.BOOST_SHA,
                'status': 'completed', 'conclusion': 'success', 'run_attempt': 1}
    check = {'id': counter.BOOST_CHECK, 'head_sha': counter.BOOST_SHA, 'name': counter.boost.CHECK_NAME,
             'status': 'completed', 'conclusion': 'success',
             'output': {'summary': MARKER + encode_snapshot(state)}}
    return workflow, check


class PreviousEvidenceTests(unittest.TestCase):
    def test_valid_exact_previous_snapshot(self):
        result = counter.validate_previous(*previous())
        self.assertEqual((result['baseline'], result['final_count']), (1, 11))

    def test_other_or_incomplete_run_is_rejected(self):
        for field, value in (('run_attempt', 2), ('status', 'in_progress'),
                             ('conclusion', 'failure'), ('head_sha', 'a' * 40)):
            with self.subTest(field=field):
                workflow, check = previous()
                workflow[field] = value
                with self.assertRaises(ValueError):
                    counter.validate_previous(workflow, check)

    def test_order_and_observation_must_both_be_confirmed(self):
        workflow, check = previous()
        from tools.ci_progress import decode_snapshot
        for change in ('claim', 'completion', 'count', 'target', 'window', 'clock'):
            with self.subTest(change=change):
                check2 = copy.deepcopy(check)
                state = decode_snapshot(check2['output']['summary'].split(MARKER, 1)[1])
                if change == 'claim': state['provider_claimed_order'] = False
                if change == 'completion': state['complete']['request_released'] = False
                if change == 'count': state['observation']['final_count'] = 1
                if change == 'target': state['target'] = 'https://vk.ru/club999'
                if change == 'window': state['observation']['window_sec'] = 30
                if change == 'clock': state['samples'][-1]['observed_at'] = 30
                check2['output']['summary'] = MARKER + encode_snapshot(state)
                with self.assertRaises(ValueError):
                    counter.validate_previous(workflow, check2)


class CounterTests(unittest.TestCase):
    def test_two_agreeing_sources_are_reported_separately(self):
        calls = []
        def read(gid):
            calls.append(gid)
            return 11
        data = counter.collect((('widget', read), ('mobile', read)), clock=lambda: 100.0)
        self.assertEqual(calls, [counter.GROUP_ID, counter.GROUP_ID])
        self.assertEqual([s['count'] for s in data['samples']], [11, 11])
        self.assertTrue(data['sources_agree'])
        self.assertEqual(data['consensus_count'], 11)

    def test_one_source_unavailable_never_becomes_zero_or_consensus(self):
        def fail(_):
            raise OSError('secret or page contents should not be recorded')
        data = counter.collect((('widget', lambda _: 11), ('mobile', fail)), clock=lambda: 10.0)
        self.assertIsNone(data['samples'][1]['count'])
        self.assertEqual(data['samples'][1]['error'], 'OSError')
        self.assertIsNone(data['sources_agree'])
        self.assertIsNone(data['consensus_count'])
        self.assertNotIn('secret', counter.render(data))

    def test_disagreement_has_no_made_up_single_count(self):
        data = counter.collect((('widget', lambda _: 10), ('mobile', lambda _: 11)), clock=lambda: 10.0)
        self.assertFalse(data['sources_agree'])
        self.assertIsNone(data['consensus_count'])
        self.assertIn('источники расходятся', counter.render(data))

    def test_boolean_count_is_rejected(self):
        data = counter.collect((('widget', lambda _: True), ('mobile', lambda _: 11)), clock=lambda: 10.0)
        self.assertIsNone(data['samples'][0]['count'])
        self.assertIsNone(data['consensus_count'])


if __name__ == '__main__':
    unittest.main()
