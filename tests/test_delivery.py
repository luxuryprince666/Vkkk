# -*- coding: utf-8 -*-
"""Проверки последовательных заказов без реальных запросов к накрутчикам."""
import json
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from core import db, delivery, engine, executor, goals


class DeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='vkfree-delivery-')
        cls.old = (db.DATA, db.DB_PATH, db.BACKUP_DIR)
        db.DATA = Path(cls.tmp.name)
        db.DB_PATH = db.DATA / 'app.db'
        db.BACKUP_DIR = db.DATA / 'backups'
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        db.DATA, db.DB_PATH, db.BACKUP_DIR = cls.old
        cls.tmp.cleanup()

    def setUp(self):
        with db.tx() as c:
            c.execute('DELETE FROM deliveries')
            c.execute('DELETE FROM tasks')
            c.execute('DELETE FROM goals')
            c.execute('DELETE FROM targets')
            c.execute("INSERT INTO targets(id,name,platform,url,kind,created_at) VALUES(1,'test','vk','https://vk.ru/club243157309','community',1)")
            for i in range(5):
                site = f'test-delivery-{i}'
                c.execute("INSERT OR IGNORE INTO sites(id,name,mode,enabled) VALUES(?,?, 'assisted',1)", (site, site))
                c.execute("INSERT OR REPLACE INTO services(id,site_id,platform,stype,label,amount,cooldown_min,hold_seconds,url,subject) VALUES(?,?, 'vk','followers','test',20,0,0,'https://example.com','community')", (site,site))
                c.execute("INSERT INTO tasks(id,service_id,target_id,status,planned_units,created_at,due_at) VALUES(?,?,1,'ready',20,1,1)", (i+1,site))
            c.execute("UPDATE settings SET value='1' WHERE key='full_auto'")
            c.execute("UPDATE settings SET value='0' WHERE key='dry_run'")

    def status(self, i):
        with db.rd() as c:
            return c.execute('SELECT status FROM tasks WHERE id=?', (i,)).fetchone()[0]

    def start(self, i, n):
        with patch.object(delivery, 'read_vk_count', return_value=n), \
             patch.object(executor, '_prepare_after_claim', return_value={'ok': True}):
            return executor.start_task(i)

    def test_five_orders_sequentially_only_after_twenty_real_growth(self):
        count = 100
        for i in range(1, 6):
            self.start(i, count)
            self.assertTrue(delivery.waiting(i, note='принят'))
            if i < 5:
                with self.assertRaisesRegex(ValueError, 'предыдущего заказа'):
                    self.start(i+1, count)
            with patch.object(delivery, 'read_vk_count', return_value=count+19):
                self.assertFalse(delivery.check_one(i))
            self.assertEqual(self.status(i), 'waiting_delivery')
            with patch.object(delivery, 'read_vk_count', return_value=count+20):
                self.assertFalse(delivery.check_one(i))
                self.assertTrue(delivery.check_one(i))
            self.assertEqual(self.status(i), 'done')
            count += 20
        with db.rd() as c:
            self.assertEqual(c.execute("SELECT SUM(claimed_units) FROM tasks WHERE status='done'").fetchone()[0], 100)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM deliveries WHERE state='verified'").fetchone()[0], 5)

    def test_missing_counter_never_submits(self):
        with patch.object(delivery, 'read_vk_count', side_effect=delivery.CounterError('no token')), \
             patch.object(executor, '_prepare_after_claim') as submit:
            result = executor.start_task(1)
        self.assertFalse(result['ok'])
        submit.assert_not_called()
        self.assertEqual(self.status(1), 'queued')
        with db.rd() as c:
            self.assertEqual(c.execute('SELECT state FROM deliveries WHERE task_id=1').fetchone()[0], 'released')

    def test_changed_target_cannot_confirm_old_order(self):
        self.start(1, 100)
        delivery.waiting(1)
        with db.tx() as c:
            c.execute("UPDATE targets SET url='https://vk.ru/club123456' WHERE id=1")
        with patch.object(delivery, 'read_vk_count', return_value=1000) as read:
            self.assertFalse(delivery.check_one(1))
            read.assert_not_called()
        self.assertEqual(self.status(1), 'waiting_delivery')

    def test_counter_failure_keeps_barrier(self):
        self.start(1, 100)
        delivery.waiting(1)
        with patch.object(delivery, 'read_vk_count', side_effect=delivery.CounterError('offline')):
            self.assertFalse(delivery.check_one(1))
        with self.assertRaisesRegex(ValueError, 'предыдущего заказа'):
            self.start(2, 100)

    def test_uncertain_is_never_auto_unlocked(self):
        self.start(1, 100)
        delivery.waiting(1, uncertain=True)
        with patch.object(delivery, 'read_vk_count', return_value=140):
            self.assertFalse(delivery.check_one(1))
        self.assertEqual(self.status(1), 'delivery_uncertain')
        with self.assertRaises(ValueError):
            self.start(2, 140)
        delivery.abandon(1)
        self.assertEqual(self.status(1), 'failed')
        self.start(2, 140)

    def test_manual_confirmation_still_requires_two_count_samples(self):
        self.start(1, 100)
        delivery.waiting(1, uncertain=True)
        delivery.confirm_accepted(1)
        self.assertEqual(self.status(1), 'waiting_delivery')
        with patch.object(delivery, 'read_vk_count', return_value=120):
            self.assertFalse(delivery.check_one(1))
            self.assertTrue(delivery.check_one(1))

    def test_manual_finish_cannot_race_running_robot(self):
        self.start(1, 100)
        with self.assertRaises(ValueError):
            executor.finish_task(1, True)
        self.assertEqual(self.status(1), 'running')

    def test_crash_after_baseline_is_uncertain_not_retried(self):
        self.start(1, 100)
        with db.tx() as conn:
            conn.execute('UPDATE tasks SET owner_pid=? WHERE id=1', (99999999,))
        db.recover()
        self.assertEqual(self.status(1), 'delivery_uncertain')
        with self.assertRaises(ValueError):
            self.start(2, 100)

    def test_completed_order_counted_only_after_verified(self):
        self.start(1, 10)
        with db.tx() as c:
            c.execute("UPDATE tasks SET status='wait_confirm' WHERE id=1")
        out = executor.finish_task(1, True)
        self.assertEqual(out['status'], 'waiting_delivery')
        with db.rd() as c:
            self.assertEqual(c.execute('SELECT claimed_units FROM tasks WHERE id=1').fetchone()[0], 0)
        with patch.object(delivery, 'read_vk_count', return_value=30):
            delivery.check_one(1)
            delivery.check_one(1)
        with db.rd() as c:
            self.assertEqual(c.execute('SELECT claimed_units FROM tasks WHERE id=1').fetchone()[0], 20)

    def test_concurrent_starts_only_one_submits(self):
        barrier = threading.Barrier(2)
        results = []
        def run(i):
            barrier.wait()
            try:
                executor.start_task(i)
                results.append('started')
            except ValueError:
                results.append('blocked')
        ts = [threading.Thread(target=run,args=(i,)) for i in (1,2)]
        with patch.object(delivery, 'read_vk_count', return_value=100), \
             patch.object(executor, '_prepare_after_claim', return_value={'ok': True}):
            for t in ts: t.start()
            for t in ts: t.join()
        self.assertCountEqual(results, ['started', 'blocked'])

    def test_alias_and_disabled_setting_still_respect_barrier(self):
        self.start(1, 100)
        delivery.waiting(1)
        db.set_setting('full_auto', '0')
        with db.tx() as c:
            c.execute("INSERT INTO targets(id,name,platform,url,kind,created_at) VALUES(2,'alias','vk','https://vk.com/public243157309','community',1)")
            c.execute('UPDATE tasks SET target_id=2 WHERE id=2')
        with self.assertRaises(ValueError):
            self.start(2, 100)

    def test_ready_picker_skips_blocked_target(self):
        self.start(1, 100)
        delivery.waiting(1)
        self.assertIsNone(engine.next_open_task())

    def test_goal_counts_actual_growth_and_refuses_overshoot(self):
        with db.tx() as c:
            c.execute("INSERT INTO goals(id,target_id,title,deadline_at,status,created_at) VALUES(1,1,'g',9999999999,'planned',1)")
            c.execute("INSERT INTO goal_items(goal_id,metric,need,have_before) VALUES(1,'followers',35,0)")
            c.execute("UPDATE tasks SET goal_id=1,metric='followers' WHERE id IN (1,2)")
            c.execute("UPDATE tasks SET status='skipped' WHERE id IN (3,4,5)")
        self.start(1, 100)
        delivery.waiting(1)
        with patch.object(delivery, 'read_vk_count', return_value=130):
            delivery.check_one(1)
            self.assertTrue(delivery.check_one(1))
        self.assertEqual(goals.progress(1)['items'][0]['done'], 30)
        with self.assertRaisesRegex(delivery.CounterError, 'осталось 5'):
            self.start(2, 130)
        self.assertEqual(self.status(2), 'ready')

    def test_successful_driver_result_only_starts_waiting(self):
        self.start(1, 100)
        with db.rd() as c:
            svc = dict(c.execute("SELECT v.*,s.name site_name FROM services v JOIN sites s ON s.id=v.site_id WHERE v.id='test-delivery-0'").fetchone())
        result = SimpleNamespace(ok=True, verdict='ok', detail='заказ принят', shot=None, steps=['нажал кнопку'], learned={})
        executor._apply_driver_result(1, svc, result, True)
        self.assertEqual(self.status(1), 'waiting_delivery')
        with self.assertRaises(ValueError): self.start(2, 100)

    def test_ambiguous_result_after_click_stays_locked(self):
        self.start(1, 100)
        with db.rd() as c:
            svc = dict(c.execute("SELECT v.*,s.name site_name FROM services v JOIN sites s ON s.id=v.site_id WHERE v.id='test-delivery-0'").fetchone())
        result = SimpleNamespace(ok=False, verdict='need_human', detail='ответ неясен', shot=None, steps=['нажал кнопку'], learned={})
        executor._apply_driver_result(1, svc, result, True)
        self.assertEqual(self.status(1), 'delivery_uncertain')
        with self.assertRaises(ValueError): self.start(2, 100)

    def test_goal_planner_does_not_pretend_20_is_5(self):
        svc = {'id':'sample', 'site_id':'sample', 'site_name':'s', 'label':'s',
               '_class':'A', '_hold':0, 'amount':20, 'cooldown_min':1, 'max_claims_day':96}
        with patch.object(engine, 'in_work_hours', return_value=True):
            slots = goals._plan_slots([svc], 35, 1000, 10000)
        self.assertEqual([slot['units'] for slot in slots], [20])

    def test_verification_does_not_resume_paused_goal(self):
        with db.tx() as c:
            c.execute("INSERT INTO goals(id,target_id,title,deadline_at,status,created_at) VALUES(1,1,'g',9999999999,'planned',1)")
            c.execute("INSERT INTO goal_items(goal_id,metric,need,have_before) VALUES(1,'followers',100,0)")
            c.execute("UPDATE tasks SET goal_id=1,metric='followers' WHERE id=1")
            c.execute("UPDATE tasks SET status='skipped' WHERE id>1")
        self.start(1, 100)
        delivery.waiting(1)
        goals.pause(1)
        with patch.object(delivery, 'read_vk_count', return_value=120):
            delivery.check_one(1); delivery.check_one(1)
        with db.rd() as c:
            self.assertEqual(c.execute('SELECT status FROM goals WHERE id=1').fetchone()[0], 'paused')
            self.assertEqual(c.execute("SELECT COUNT(*) FROM tasks WHERE goal_id=1 AND status IN ('queued','ready')").fetchone()[0], 0)

    def test_goal_paused_is_not_selected(self):
        with db.tx() as c:
            c.execute("INSERT INTO goals(id,target_id,title,deadline_at,status,created_at) VALUES(1,1,'g',9999999999,'paused',1)")
            c.execute("UPDATE tasks SET goal_id=1 WHERE id=1")
            c.execute("UPDATE tasks SET status='queued' WHERE id>1")
        self.assertIsNone(engine.next_open_task())

    def test_widget_parser_exact_and_rejects_wrong_or_rounded(self):
        good = '<a id="members_count">10<span class="num_delim"> </span>546&nbsp;подписчиков</a><script>oid: -243157309,</script>'
        self.assertEqual(delivery.parse_widget_count(good, '243157309'), 10546)
        with self.assertRaises(delivery.CounterError):
            delivery.parse_widget_count(good, '123')
        with self.assertRaises(delivery.CounterError):
            delivery.parse_widget_count('<a id="members_count">1,2 тыс подписчиков</a><script>oid: -243157309</script>', '243157309')

    def test_threshold_must_be_confirmed_twice(self):
        self.start(1, 100)
        delivery.waiting(1)
        for count in (120, 119, 120):
            with patch.object(delivery, 'read_vk_count', return_value=count):
                self.assertFalse(delivery.check_one(1))
        with patch.object(delivery, 'read_vk_count', return_value=120):
            self.assertTrue(delivery.check_one(1))

    def test_vk_api_count_and_response_identity(self):
        response = {'ok': True, 'text': json.dumps({'response': {'groups':[{'id':243157309,'members_count':42}]}})}
        with patch.dict(os.environ, {'VKFREE_VK_TOKEN':'test-token'}), \
             patch.object(delivery.network, 'http_request', return_value=response) as request:
            source = []
            self.assertEqual(delivery.read_vk_count({'platform':'vk','url':'https://vk.ru/club243157309'},
                                                    'followers', source_out=source), 42)
            self.assertEqual(source, ['VK API'])
            self.assertEqual(request.call_args.kwargs['headers']['Content-Type'],
                             'application/x-www-form-urlencoded')

    def test_cancel_cannot_remove_active_order(self):
        self.start(1, 100)
        delivery.waiting(1)
        with self.assertRaises(ValueError):
            executor.cancel_task(1)


if __name__ == '__main__':
    unittest.main()
