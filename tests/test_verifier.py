# -*- coding: utf-8 -*-
"""Проверка наблюдателя и транзакционного запуска БЕЗ реальных заявок."""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db, delivery, drivers, engine, executor, network, verifier


class Clock:
    def __init__(self):
        self.t = 0.0
    def now(self):
        return self.t
    def wait(self, seconds):
        self.t += seconds
        return False


class GrowthTests(unittest.TestCase):
    def test_configured_smm8_window_is_site_specific(self):
        cfg = {'timeout': 900, 'smm8_timeout': 3600}
        self.assertEqual(verifier._observation_window(cfg, {'site_id': 'smm8'}), 3600)
        self.assertEqual(verifier._observation_window(cfg, {'site_id': 'nakruti'}), 900)
        self.assertEqual(verifier._observation_window({'timeout': 300}, {'site_id': 'smm8'}), 300)

    def observe(self, reader, **kwargs):
        clock = Clock()
        samples = []
        out = verifier.observe_growth(lambda: reader(clock.t), 100, 0, clock=clock.now, wait=clock.wait,
                                      on_sample=lambda count, at: samples.append((at, count)),
                                      timeout_sec=300, poll_sec=15, settle_sec=90, **kwargs)
        return out, samples, clock.t

    def test_formal_success_without_growth_is_not_working_after_full_five_minutes(self):
        out, samples, elapsed = self.observe(lambda t: 100)
        self.assertEqual((out.status, out.final_count, out.added_count), ('not_working', 100, 0))
        self.assertEqual(elapsed, 300)
        self.assertEqual(samples[-1], (300, 100))
        self.assertIsNone(out.time_to_growth_sec)

    def test_growth_must_be_confirmed_and_stable_before_next_service(self):
        def reader(t):
            if t < 30: return 100
            if t < 60: return 102
            return 105
        out, samples, elapsed = self.observe(reader)
        self.assertEqual((out.status, out.final_count, out.added_count), ('working', 105, 5))
        self.assertEqual(out.time_to_growth_sec, 30)
        self.assertEqual(elapsed, 300)  # даже ранний рост наблюдается все пять минут
        self.assertGreaterEqual(sum(n > 100 for _, n in samples), 2)

    def test_growth_at_deadline_gets_a_second_sample(self):
        out, _samples, elapsed = self.observe(lambda t: 101 if t >= 300 else 100)
        self.assertEqual(out.status, 'working')
        self.assertEqual(out.added_count, 1)
        self.assertEqual(out.time_to_growth_sec, 300)
        self.assertEqual(elapsed, 390)

    def test_single_transient_increase_is_not_a_success(self):
        out, _samples, _ = self.observe(lambda t: 101 if t == 30 else 100)
        self.assertEqual(out.status, 'not_working')
        self.assertEqual(out.added_count, 0)

    def test_counter_decrease_is_a_negative_net_change_not_zero(self):
        out, _samples, elapsed = self.observe(lambda t: 98)
        self.assertEqual(elapsed, 300)
        self.assertEqual((out.status, out.final_count, out.added_count), ('not_working', 98, -2))

    def test_delayed_order_stays_under_observation_during_and_after_driver(self):
        clock = Clock()
        out = verifier.observe_growth(lambda: 103 if clock.t >= 630 else 100, 100, 0,
                                      timeout_sec=300, poll_sec=15, settle_sec=90,
                                      clock=clock.now, wait=clock.wait,
                                      delayed_until=lambda: 600 if clock.t >= 600 else None)
        self.assertEqual((out.status, out.added_count, out.time_to_growth_sec), ('working', 3, 630))
        self.assertEqual(clock.t, 900)  # пять минут после последнего возможного действия драйвера

    def test_lost_counter_is_unknown_not_zero(self):
        def reader(t):
            if t >= 300:
                raise delivery.CounterError('offline')
            return 100
        out, _samples, _ = self.observe(reader)
        self.assertEqual(out.status, 'unverified')
        self.assertIsNone(out.added_count)
        self.assertIn('счётчика', out.reason)

    def test_counter_loss_during_growth_stops_without_advancing(self):
        def reader(t):
            if t >= 45:
                raise delivery.CounterError('offline')
            return 102 if t >= 15 else 100
        out, _samples, _ = self.observe(reader)
        self.assertEqual(out.status, 'unverified')
        self.assertIsNone(out.added_count)
        self.assertIn('стабилизации', out.reason)

    def test_continuous_growth_is_not_declared_stable(self):
        out, _samples, _ = self.observe(lambda t: 101 + int(t // 15), max_observe_sec=390)
        self.assertEqual(out.status, 'unverified')
        self.assertIsNone(out.added_count)

    def test_stop_does_not_mark_zero_or_working(self):
        clock = Clock()
        out = verifier.observe_growth(lambda: 100, 100, 0, timeout_sec=300, poll_sec=15,
                                      clock=clock.now, wait=lambda n: True)
        self.assertEqual(out.status, 'stopped')
        self.assertIsNone(out.added_count)


class CounterTests(unittest.TestCase):
    def test_vk_widget_zero_singular_plural_and_rounding(self):
        def widget(value):
            return f'<script>oid: -243396555</script><a id="members_count">{value}</a>'
        for text, expected in [('0 followers', 0), ('1 follower', 1), ('12 345 подписчиков', 12345),
                               ('Нет подписчиков', 0), ('2 участника', 2)]:
            self.assertEqual(delivery.parse_widget_count(widget(text), '243396555'), expected)
        with self.assertRaises(delivery.CounterError):
            delivery.parse_widget_count(widget('1,2 тыс. подписчиков'), '243396555')
        with self.assertRaises(delivery.CounterError):
            delivery.parse_widget_count(widget('1 follower'), '999')
        with self.assertRaises(delivery.CounterError):
            delivery.parse_widget_count('<script>oid:-243396555</script><p>нет подписчиков</p>', '243396555')

    def test_telegram_exact_english_russian_and_rounded(self):
        def page(value):
            return f'<div class="tgme_page_title"><span>Test</span></div><div class="tgme_page_extra">{value}</div>'
        for text, expected in [('1 subscriber', 1), ('12 345 subscribers', 12345),
                               ('12,345 members, 70 online', 12345), ('1 234 участника', 1234),
                               ('0 subscribers', 0)]:
            self.assertEqual(delivery.parse_telegram_count(page(text), 'testchannel'), expected)
        for text in ('1.2K subscribers', '12,3K subscribers', '2.5 тыс. подписчиков'):
            with self.assertRaises(delivery.CounterError):
                delivery.parse_telegram_count(page(text), 'testchannel')
        with self.assertRaises(delivery.CounterError):
            delivery.parse_telegram_count('<div class="tgme_page_extra">5 subscribers</div>', 'testchannel')

    def test_telegram_redirect_to_another_channel_is_rejected(self):
        page = '<div class="tgme_page_title">Test</div><div class="tgme_page_extra">12 subscribers</div>'
        with patch.object(network, 'http_request', return_value={'ok': True, 'url': 'https://t.me/otherchannel', 'text': page}), \
             patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': ''}):
            with self.assertRaises(delivery.CounterError):
                delivery.read_telegram_count('testchannel')

    def test_vk_api_failure_falls_back_to_widget_and_records_actual_source(self):
        target = {'platform': 'vk', 'url': 'https://vk.ru/club243396555'}
        api_error = {'ok': True, 'text': json.dumps({'error': {'error_code': 5}})}
        source = []
        with patch.object(network, 'http_request', return_value=api_error), \
             patch.object(delivery, 'read_vk_widget', return_value=41) as widget, \
             patch.dict(os.environ, {'VKFREE_VK_TOKEN': 'invalid'}):
            self.assertEqual(delivery.read_vk_count(target, 'followers', source_out=source), 41)
        widget.assert_called_once_with('243396555', proxy=None)
        self.assertEqual(source, ['виджет VK'])

    def test_vk_api_and_widget_both_unavailable_do_not_invent_a_count(self):
        target = {'platform': 'vk', 'url': 'https://vk.ru/club243396555'}
        with patch.object(network, 'http_request', return_value={'ok': False}), \
             patch.object(delivery, 'read_vk_widget', side_effect=delivery.CounterError('offline')), \
             patch.dict(os.environ, {'VKFREE_VK_TOKEN': 'test-secret'}):
            with self.assertRaises(delivery.CounterError) as caught:
                delivery.read_vk_count(target, 'followers')
        self.assertIn('VK API недоступен', str(caught.exception))
        self.assertIn('offline', str(caught.exception))
        self.assertNotIn('test-secret', str(caught.exception))

    def test_widget_tries_other_vk_host_after_unusable_html(self):
        responses = [{'ok': True, 'status': 200, 'url': 'https://vk.com/widget_community.php', 'text': 'нет счётчика'},
                     {'ok': True, 'status': 200, 'url': 'https://vk.ru/widget_community.php',
                      'text': '<script>oid:-243396555</script><a id="members_count">1 follower</a>'}]
        with patch.object(network, 'http_request', side_effect=responses) as request:
            self.assertEqual(delivery.read_vk_widget('243396555'), 1)
        self.assertEqual(request.call_count, 2)

    def test_telegram_reports_fallback_source_instead_of_guessing_from_token(self):
        page = '<div class="tgme_page_title">Test</div><div class="tgme_page_extra">12 subscribers</div>'
        responses = [{'ok': False}, {'ok': True, 'url': 'https://t.me/testchannel', 'text': page}]
        source = []
        with patch.object(network, 'http_request', side_effect=responses), \
             patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'invalid'}):
            self.assertEqual(delivery.read_telegram_count('testchannel', source_out=source), 12)
        self.assertEqual(source, ['t.me'])

    def test_url_validation_never_accepts_fake_hosts_or_post_links(self):
        for value in ('https://vk.ru.evil.example/club123', 'https://user@vk.ru/club123',
                      'https://t.me/+invite', 'https://t.me/channelname/123', 'https://t.me/s/channelname/123'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                delivery.normalize_channel_input(value)
        self.assertEqual(delivery.normalize_channel_input('@testchannel'), ('telegram', 'community', 'https://t.me/testchannel'))
        self.assertEqual(delivery.identity({'platform': 'vk', 'url': 'https://vk.ru/club243396555'}, 'followers')[1], '243396555')

    def test_system_chromium_is_available_to_playwright_without_downloaded_cache(self):
        from core import stealth
        with patch.object(stealth, '_find_chrome', return_value='/usr/bin/chromium'):
            self.assertTrue(stealth._browsers_present('playwright'))
            self.assertTrue(stealth._browsers_present('patchright'))

    def test_explicit_failures_precede_generic_success(self):
        for text, expected in [('Заказ принят. Лимит исчерпан', 'quota'),
                               ('Ваш заказ не принят', 'need_human'),
                               ('Успешно. Неверная ссылка', 'bad_link')]:
            self.assertEqual(drivers.read_verdict(text, False, False)[0], expected)
        self.assertEqual(drivers.read_verdict('успешно', False, False)[0], 'need_human')
        self.assertIsNone(drivers.pick_submit_button([{'text': 'Отмена', 'nearForm': True}]))
        self.assertIsNone(drivers.pick_submit_button([{'text': 'Купить заказ', 'attrs': 'order'}]))


class Smm8SafetyTests(unittest.TestCase):
    def test_smm8_uses_documented_vk_com_link(self):
        self.assertEqual(drivers.normalize_link('smm8', 'https://vk.ru/club243396555'),
                         'https://vk.com/club243396555')

    def test_smm8_never_clicks_if_free_widget_changes_during_baseline(self):
        events = []
        class Page:
            def type_human(self, selector, value): return True
            def wait(self, ms): pass
            def js(self, script):
                if "btnOptinLoggedIn" in script and '.click()' in script:
                    events.append('click')
                return '1'
        with patch.object(drivers, '_smm8_free_guard', side_effect=[True, False]):
            result = drivers._run_smm8(Page(), 'https://vk.com/club123', [], dry_run=False,
                                       on_submit=lambda: events.append('record'))
        self.assertEqual(result[0], 'need_human')
        self.assertEqual(events, ['record'])

    @patch.object(drivers, '_smm8_free_guard', return_value=True)
    def test_possible_submission_is_recorded_before_first_click_and_not_repeated(self, _guard):
        events = []
        class Page:
            def type_human(self, selector, value): return True
            def wait(self, ms): pass
            def js(self, script):
                if script == drivers.SMM8_STATE_JS:
                    return json.dumps({'err_msg': 'captcha'})
                if "btnOptinLoggedIn" in script and '.click()' in script:
                    events.append('click')
                return '1'
        result = drivers._run_smm8(Page(), 'https://vk.ru/club123', [], dry_run=False,
                                   on_submit=lambda: events.append('record'))
        self.assertEqual(result[0], 'captcha')
        self.assertEqual(events, ['record', 'click'])

    @patch.object(drivers, '_smm8_free_guard', return_value=True)
    def test_headless_fallback_does_not_wait_for_an_invisible_human(self, _guard):
        class Page:
            def type_human(self, selector, value): return True
            def wait(self, ms): pass
            def js(self, script):
                return json.dumps({'err_msg': 'captcha'}) if script == drivers.SMM8_STATE_JS else '1'
        steps = ['видимое окно не открылось — продолжаю в фоне']
        with patch.object(db, 'get_setting') as setting:
            result = drivers._run_smm8(Page(), 'https://vk.ru/club123', steps, dry_run=False,
                                       allow_captcha_wait=True, headless=False, on_submit=lambda: None)
        self.assertEqual(result[0], 'captcha')
        setting.assert_not_called()

    @patch.object(drivers, '_smm8_free_guard', return_value=True)
    def test_smm8_respects_disabled_automatic_captcha(self, _guard):
        import itertools
        class Page:
            def type_human(self, selector, value): return True
            def wait(self, ms): pass
            def js(self, script):
                return '{}' if script == drivers.SMM8_STATE_JS else '1'
        clock = itertools.count()
        with patch.object(drivers.time, 'time', side_effect=lambda: next(clock)), \
             patch.object(db, 'get_setting', return_value='0'), \
             patch.object(drivers, '_smm8_turnstile_click') as solve:
            result = drivers._run_smm8(Page(), 'https://vk.ru/club123', [], dry_run=False,
                                       on_submit=lambda: None)
        self.assertEqual(result[0], 'captcha')
        solve.assert_not_called()

    def test_antibot_gate_respects_disabled_automatic_captcha(self):
        from contextlib import contextmanager
        class Page:
            def goto(self, *args, **kwargs): pass
            def mouse_wiggle(self, *args): pass
            def scroll(self, *args): pass
            def wait(self, *args): pass
            def text(self): return ''
        @contextmanager
        def browser(*args, **kwargs):
            yield 'fake', Page()
        with patch.object(drivers, '_open_smart', browser), \
             patch.object(drivers.profiles, 'get_profile', return_value={}), \
             patch.object(drivers, '_memory', return_value={}), \
             patch.object(drivers, '_shot', return_value=None), \
             patch.object(drivers, '_antibot_gate', return_value={'gate': True}), \
             patch.object(drivers, '_antibot_try_pass') as solve, \
             patch.object(db, 'get_setting', return_value='0'):
            result = drivers.run({'site_id': 'example', 'url': 'https://example.com'},
                                 'https://vk.ru/club123', normalize_target=False)
        self.assertEqual(result.verdict, 'captcha')
        solve.assert_not_called()

    @patch.object(drivers, '_smm8_free_guard', return_value=True)
    def test_explicit_smm8_limit_is_not_misreported_as_captcha(self, _guard):
        class Page:
            def type_human(self, selector, value): return True
            def wait(self, ms): pass
            def js(self, script):
                return json.dumps({'err_msg': 'limit reached'}) if script == drivers.SMM8_STATE_JS else '1'
        result = drivers._run_smm8(Page(), 'https://vk.ru/club123', [], dry_run=False,
                                   on_submit=lambda: None)
        self.assertEqual(result[0], 'quota')
        self.assertIn('limit reached', result[1])

    def test_smmprime_records_risk_before_order_click_in_legacy_queue(self):
        events = []
        class Frame:
            url = 'https://free-api.smmprime.com/free'
            def evaluate(self, script):
                if script == drivers.SMMPRIME_QUESTION_JS:
                    return '2 + 3 = ?'
                if "return b?{t:" in script:
                    return {'dis': False}
                if "if(b&&!b.disabled)b.click()" in script:
                    events.append('click')
                return 'OK'
        class RawPage:
            frames = [Frame()]
            def on(self, event, callback): pass
        class Page:
            _p = RawPage()
            def wait(self, ms): pass
        result = drivers._run_smmprime(Page(), 'https://vk.ru/wall1_2', [], dry_run=False,
                                       on_submit=lambda: events.append('record'))
        self.assertEqual(result[0], 'need_human')  # поддельная страница не возвращает ответ
        self.assertEqual(events, ['record', 'click'])


class DatabaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='vkfree-verify-')
        self.old = (db.DATA, db.DB_PATH, db.BACKUP_DIR)
        db.DATA = Path(self.tmp.name)
        db.DB_PATH = db.DATA / 'app.db'
        db.BACKUP_DIR = db.DATA / 'backups'
        db.init_db()
        db.set_setting('global_min_gap_sec', '0')
        db.set_setting('proxy_rotation', '0')
        with db.tx() as conn:
            conn.execute("UPDATE sites SET enabled=CASE WHEN id IN ('prskill','smmnakrutka') THEN 1 ELSE 0 END")

    def tearDown(self):
        db.DATA, db.DB_PATH, db.BACKUP_DIR = self.old
        self.tmp.cleanup()

    def test_two_services_never_overlap_and_only_measured_growth_is_reported(self):
        counter = {'value': 100}
        events = []
        def driver(svc, link, **kw):
            events.append('submit:' + svc['site_id'])
            kw['on_submit']()
            return drivers.DriverResult('ok', 'заказ принят', units=50, backend='fake')
        def observe(read, baseline, started, **kw):
            site = events[-1].split(':')[1]
            events.append('watch:' + site)
            if site == 'smmnakrutka':
                counter['value'] = 105
                kw['on_sample'](read(), time.monotonic())
                kw['on_sample'](read(), time.monotonic())
                result = verifier.Observation('working', 105, 5, 30)
            else:
                kw['on_sample'](read(), time.monotonic())
                result = verifier.Observation('not_working', 100, 0, reason='5 минут без прироста')
            events.append('settled:' + site)
            return result
        with patch.object(delivery, 'read_target_count', side_effect=lambda *a, **k: counter['value']), \
             patch.object(drivers, 'run', side_effect=driver), patch.object(verifier, 'observe_growth', side_effect=observe):
            cid = verifier.start('https://vk.ru/club243396555', background=False)['campaign_id']
        c = verifier.status(cid)
        by_site = {r['site_id']: r for r in c['results']}
        self.assertEqual(c['status'], 'completed')
        self.assertEqual(c['initial_count'], 100)
        self.assertEqual(c['total_added'], 5)
        self.assertEqual((by_site['prskill']['status'], by_site['prskill']['added_count']), ('not_working', 0))
        self.assertEqual((by_site['smmnakrutka']['status'], by_site['smmnakrutka']['added_count']), ('working', 5))
        self.assertEqual(events, ['submit:prskill', 'watch:prskill', 'settled:prskill',
                                  'submit:smmnakrutka', 'watch:smmnakrutka', 'settled:smmnakrutka'])
        self.assertEqual(by_site['smmnakrutka']['baseline_count'], 100)
        self.assertTrue(by_site['smmnakrutka']['service_url'].startswith('https://'))
        self.assertEqual(len(by_site['smmnakrutka']['samples']), 4)  # до формы, перед отправкой и два подтверждения
        self.assertEqual(by_site['smmprime']['status'], 'skipped')
        self.assertIsNone(by_site['smmprime']['added_count'])
        report = verifier.format_report(c)
        self.assertIn('ссылка:', report.lower())
        self.assertIn('наблюдаемый прирост: 5', report)
        self.assertIn('Страница площадки: https://', report)
        self.assertIn('Начало проверки (UTC):', report)
        self.assertIn('SMMPrime — не проверено', report)
        self.assertIn('ожидание до этого действия не входит', report)

    def test_counter_unavailable_never_submits_any_site(self):
        with patch.object(delivery, 'read_target_count', side_effect=delivery.CounterError('нет сети')), \
             patch.object(drivers, 'run') as submit:
            cid = verifier.start('https://vk.ru/club243396555', background=False)['campaign_id']
        submit.assert_not_called()
        c = verifier.status(cid)
        self.assertEqual(c['status'], 'failed')
        self.assertIsNone(c['initial_count'])
        self.assertTrue(all(r['status'] == 'skipped' and r['added_count'] is None for r in c['results']))

    def test_fresh_baseline_is_taken_immediately_before_delayed_submission(self):
        with db.tx() as conn:
            conn.execute("UPDATE sites SET enabled=0 WHERE id='smmnakrutka'")
        counter = {'value': 100}
        actions = []
        def driver(svc, link, **kw):
            counter['value'] = 104  # рост пока страница ждала свой таймер
            kw['on_submit']()
            actions.append('click')
            return drivers.DriverResult('ok', 'принято')
        def observe(read, baseline, started, **kw):
            self.assertEqual(baseline, 104)
            counter['value'] = 107
            kw['on_sample'](read(), time.monotonic())
            return verifier.Observation('working', 107, 3, 15)
        with patch.object(delivery, 'read_target_count', side_effect=lambda *a, **k: counter['value']), \
             patch.object(drivers, 'run', side_effect=driver), \
             patch.object(verifier, 'observe_growth', side_effect=observe):
            cid = verifier.start('https://vk.ru/club243396555', background=False)['campaign_id']
        result = next(r for r in verifier.status(cid)['results'] if r['site_id'] == 'prskill')
        self.assertEqual(actions, ['click'])
        self.assertEqual((result['baseline_count'], result['final_count'], result['added_count']), (104, 107, 3))
        self.assertEqual(result['attempts'][0]['baseline_count'], 104)
        self.assertEqual([s['count'] for s in result['samples']], [100, 104, 107])
        self.assertLessEqual(result['samples'][1]['observed_at'], result['submitted_at'])

    def test_lost_counter_before_submission_blocks_click_and_all_other_sites(self):
        calls = []
        clicks = []
        def count(*args, **kwargs):
            calls.append(1)
            if len(calls) > 2:
                raise delivery.CounterError('счётчик пропал во время таймера')
            return 100
        def driver(svc, link, **kw):
            kw['on_submit']()
            clicks.append(svc['site_id'])
            return drivers.DriverResult('ok', 'принято')
        with patch.object(delivery, 'read_target_count', side_effect=count), \
             patch.object(drivers, 'run', side_effect=driver):
            cid = verifier.start('https://vk.ru/club243396555', background=False)['campaign_id']
        campaign = verifier.status(cid)
        result = next(r for r in campaign['results'] if r['site_id'] == 'prskill')
        self.assertEqual(clicks, [])
        self.assertEqual(campaign['status'], 'failed')
        self.assertEqual(result['status'], 'unverified')
        self.assertIn('перед отправкой', result['failure_reason'])
        self.assertIsNone(result['submitted_at'])
        self.assertIsNone(result['added_count'])
        self.assertEqual(verifier.unresolved_count(), 0)
        self.assertEqual(next(r for r in campaign['results'] if r['site_id'] == 'smmnakrutka')['status'], 'skipped')

    def test_stop_while_refreshing_baseline_prevents_click(self):
        checking, release = threading.Event(), threading.Event()
        reads, clicks = [], []
        def count(*args, **kwargs):
            reads.append(1)
            if len(reads) == 3:
                checking.set()
                release.wait(5)
            return 100
        def driver(svc, link, **kw):
            kw['on_submit']()
            clicks.append(svc['site_id'])
            return drivers.DriverResult('ok', 'принято')
        with patch.object(delivery, 'read_target_count', side_effect=count), \
             patch.object(drivers, 'run', side_effect=driver):
            cid = verifier.start('https://vk.ru/club243396555')['campaign_id']
            try:
                self.assertTrue(checking.wait(4))
                verifier.stop(cid)
            finally:
                release.set()
            for _ in range(100):
                campaign = verifier.status(cid)
                if campaign['status'] not in ('running', 'stopping'):
                    break
                time.sleep(.02)
        result = next(r for r in campaign['results'] if r['site_id'] == 'prskill')
        self.assertEqual(campaign['status'], 'stopped')
        self.assertEqual(clicks, [])
        self.assertIsNone(result['submitted_at'])
        self.assertEqual(verifier.unresolved_count(), 0)

    def test_callback_never_permits_two_submissions(self):
        clicks = []
        def driver(svc, link, **kw):
            kw['on_submit']()
            clicks.append('first')
            kw['on_submit']()
            clicks.append('second')
            return drivers.DriverResult('ok', 'принято')
        with patch.object(delivery, 'read_target_count', return_value=100), \
             patch.object(drivers, 'run', side_effect=driver), \
             patch.object(verifier, 'observe_growth', return_value=verifier.Observation('not_working', 100, 0)):
            cid = verifier.start('https://vk.ru/club243396555', background=False)['campaign_id']
        result = next(r for r in verifier.status(cid)['results'] if r['site_id'] == 'prskill')
        self.assertEqual(clicks, ['first'])
        self.assertEqual(result['status'], 'unverified')
        self.assertIsNotNone(result['submitted_at'])
        self.assertEqual(verifier.unresolved_count(), 1)

    def test_before_submit_hook_runs_after_record_and_before_click(self):
        with db.tx() as conn:
            conn.execute("UPDATE sites SET enabled=0 WHERE id='smmnakrutka'")
        events = []
        def driver(svc, link, **kw):
            kw['on_submit']()
            events.append('click')
            return drivers.DriverResult('ok', 'принято')
        def durable(campaign_id, result_id):
            campaign = verifier.status(campaign_id)
            row = next(r for r in campaign['results'] if r['id'] == result_id)
            self.assertEqual(row['baseline_count'], 100)
            self.assertIsNotNone(row['submitted_at'])
            events.append('durable')
        with patch.object(delivery, 'read_target_count', return_value=100), \
             patch.object(drivers, 'run', side_effect=driver), \
             patch.object(verifier, 'observe_growth', return_value=verifier.Observation('not_working', 100, 0)):
            verifier.start('https://vk.ru/club243396555', background=False, before_submit=durable)
        self.assertEqual(events, ['durable', 'click'])

    def test_failed_before_submit_hook_prevents_click_and_blocks_next(self):
        clicks = []
        def driver(svc, link, **kw):
            kw['on_submit']()
            clicks.append(svc['site_id'])
            return drivers.DriverResult('ok', 'принято')
        def fail(_campaign_id, _result_id):
            raise RuntimeError('publication unavailable')
        with patch.object(delivery, 'read_target_count', return_value=100), \
             patch.object(drivers, 'run', side_effect=driver), \
             patch.object(verifier, 'observe_growth', return_value=verifier.Observation('not_working', 100, 0)):
            cid = verifier.start('https://vk.ru/club243396555', background=False, before_submit=fail)['campaign_id']
        campaign = verifier.status(cid)
        row = next(r for r in campaign['results'] if r['site_id'] == 'prskill')
        self.assertEqual(clicks, [])
        self.assertEqual(campaign['status'], 'failed')
        self.assertEqual(row['status'], 'unverified')
        self.assertIsNotNone(row['submitted_at'])

    def test_campaign_saves_actual_mobile_fallback_source(self):
        with db.tx() as conn:
            conn.execute('UPDATE sites SET enabled=0')  # никаких настоящих драйверов
        mobile = (Path(__file__).parent / 'fixtures' / 'vk_mobile_prefetch.html').read_text(encoding='utf-8')
        def request(url, **_kwargs):
            if '/widget_community.php?' in url:
                return {'ok': False, 'error': 'widget offline'}
            return {'ok': True, 'status': 200, 'url': url, 'text': mobile}
        with patch.dict(os.environ, {'VKFREE_VK_TOKEN': ''}), \
             patch.object(network, 'http_request', side_effect=request), \
             patch.object(drivers, 'run') as submit:
            cid = verifier.start('https://vk.ru/club243396555', background=False)['campaign_id']
        submit.assert_not_called()
        campaign = verifier.status(cid)
        self.assertEqual(campaign['initial_count'], 1)
        self.assertEqual(campaign['baseline_samples'][0]['source'], 'мобильная страница VK')
        self.assertEqual(campaign['baseline_samples'][0]['count'], 1)
        self.assertIsNone(campaign['total_added'])

    def test_alternative_proxy_is_used_after_pre_submit_network_failure(self):
        with db.tx() as conn:
            conn.execute("UPDATE sites SET enabled=0 WHERE id='smmnakrutka'")
        routes = [network.Endpoint('direct', 'direct', 'прямой'),
                  network.Endpoint('p1', 'http', 'доверенный прокси', 'http://proxy.test:8888')]
        calls = []
        def driver(svc, link, **kw):
            calls.append(kw['proxy'])
            if not kw['proxy']:
                return drivers.DriverResult('site_broken', 'нет соединения')
            kw['on_submit']()
            return drivers.DriverResult('ok', 'принято')
        def observe(read, baseline, started, **kw):
            return verifier.Observation('working', baseline + 2, 2, 15)
        with patch.object(verifier, '_routes', return_value=routes), \
             patch.object(delivery, 'read_target_count', return_value=100), \
             patch.object(drivers, 'run', side_effect=driver), patch.object(verifier, 'observe_growth', side_effect=observe):
            cid = verifier.start('https://vk.ru/club243396555', background=False)['campaign_id']
        r = next(r for r in verifier.status(cid)['results'] if r['site_id'] == 'prskill')
        self.assertEqual(calls, [None, 'http://proxy.test:8888'])
        self.assertEqual(r['status'], 'working')
        self.assertEqual(len(r['attempts']), 2)

    def test_explicit_limit_is_not_bypassed_by_proxy_rotation(self):
        with db.tx() as conn:
            conn.execute("UPDATE sites SET enabled=0 WHERE id='smmnakrutka'")
        routes = [network.Endpoint('direct', 'direct', 'прямой'),
                  network.Endpoint('p1', 'http', 'доверенный прокси', 'http://proxy.test:8888')]
        calls = []
        def driver(svc, link, **kw):
            calls.append(kw['proxy'])
            kw['on_submit']()
            return drivers.DriverResult('quota', 'Лимит исчерпан')
        with patch.object(verifier, '_routes', return_value=routes), \
             patch.object(delivery, 'read_target_count', return_value=100), \
             patch.object(drivers, 'run', side_effect=driver), \
             patch.object(verifier, 'observe_growth', return_value=verifier.Observation('not_working', 100, 0)) as observe:
            cid = verifier.start('https://vk.ru/club243396555', background=False)['campaign_id']
        result = next(r for r in verifier.status(cid)['results'] if r['site_id'] == 'prskill')
        self.assertEqual(calls, [None])
        self.assertEqual(result['status'], 'unverified')
        self.assertIsNone(result['added_count'])
        self.assertIn('Лимит', result['failure_reason'])
        self.assertNotIn('rejected', observe.call_args.kwargs)  # не прерываем полное окно

    def test_ambiguous_submission_stops_without_retry_or_route_rotation(self):
        routes = [network.Endpoint('direct', 'direct', 'прямой'),
                  network.Endpoint('p1', 'http', 'доверенный прокси', 'http://proxy.test:8888')]
        calls = []
        def driver(svc, link, **kw):
            calls.append((svc['site_id'], kw['proxy']))
            kw['on_submit']()
            return drivers.DriverResult('need_human', 'Ответ после клика неизвестен')
        with patch.object(verifier, '_routes', return_value=routes), \
             patch.object(delivery, 'read_target_count', return_value=100), \
             patch.object(drivers, 'run', side_effect=driver), \
             patch.object(verifier, 'observe_growth', return_value=verifier.Observation('not_working', 100, 0)):
            cid = verifier.start('https://vk.ru/club243396555', background=False)['campaign_id']
        campaign = verifier.status(cid)
        result = next(r for r in campaign['results'] if r['site_id'] == 'prskill')
        self.assertEqual(calls, [('prskill', None)])
        self.assertEqual(campaign['status'], 'failed')
        self.assertEqual(result['status'], 'unverified')
        self.assertIsNone(result['added_count'])
        self.assertEqual(verifier.unresolved_count(), 1)

    def test_bad_link_after_possible_click_is_observed_and_never_retried(self):
        with db.tx() as conn:
            conn.execute("UPDATE sites SET enabled=0 WHERE id='smmnakrutka'")
        links = []
        def driver(svc, link, **kw):
            links.append(link)
            kw['on_submit']()
            return drivers.DriverResult('bad_link', 'неверная ссылка')
        with patch.object(delivery, 'read_target_count', return_value=100), patch.object(drivers, 'run', side_effect=driver), \
             patch.object(verifier, 'observe_growth', return_value=verifier.Observation('not_working', 100, 0)) as observe:
            cid = verifier.start('https://vk.ru/club243396555', background=False)['campaign_id']
        r = next(r for r in verifier.status(cid)['results'] if r['site_id'] == 'prskill')
        self.assertEqual(links, ['https://vk.com/club243396555'])
        self.assertEqual(r['status'], 'unverified')
        self.assertIsNone(r['added_count'])
        self.assertEqual(len(r['attempts']), 1)
        self.assertNotIn('rejected', observe.call_args.kwargs)

    def test_bad_link_before_any_submit_can_try_vk_domain_alternative(self):
        with db.tx() as conn:
            conn.execute("UPDATE sites SET enabled=0 WHERE id='smmnakrutka'")
        links = []
        def driver(svc, link, **kw):
            links.append(link)
            if len(links) == 1:
                return drivers.DriverResult('bad_link', 'ссылка не подошла до отправки')
            kw['on_submit']()
            return drivers.DriverResult('ok', 'принято')
        with patch.object(delivery, 'read_target_count', return_value=100), patch.object(drivers, 'run', side_effect=driver), \
             patch.object(verifier, 'observe_growth', return_value=verifier.Observation('working', 103, 3, 20)):
            cid = verifier.start('https://vk.ru/club243396555', background=False)['campaign_id']
        r = next(r for r in verifier.status(cid)['results'] if r['site_id'] == 'prskill')
        self.assertEqual(links, ['https://vk.com/club243396555', 'https://vk.ru/club243396555'])
        self.assertEqual(r['added_count'], 3)
        self.assertEqual(len(r['attempts']), 2)

    def test_stop_keeps_unverified_order_and_requires_explicit_risk_acknowledgement(self):
        launched = threading.Event()
        def driver(svc, link, **kw):
            kw['on_submit']()
            launched.set()
            kw['cancel_event'].wait(5)
            return drivers.DriverResult('cancelled', 'остановлено')
        def observe(read, baseline, started, **kw):
            kw['wait'](5)
            return verifier.Observation('stopped', baseline, None, reason='остановлено')
        with patch.object(delivery, 'read_target_count', return_value=100), patch.object(drivers, 'run', side_effect=driver), \
             patch.object(verifier, 'observe_growth', side_effect=observe):
            cid = verifier.start('https://vk.ru/club243396555')['campaign_id']
            self.assertTrue(launched.wait(4))
            verifier.stop(cid)
            for _ in range(100):
                c = verifier.status(cid)
                if c['status'] not in ('running', 'stopping'): break
                time.sleep(0.02)
            self.assertEqual(c['status'], 'stopped')
            r = next(r for r in c['results'] if r['site_id'] == 'prskill')
            self.assertEqual(r['status'], 'unverified')
            self.assertIsNone(r['added_count'])
            with self.assertRaisesRegex(ValueError, 'риск повторного запуска'):
                verifier.start('https://vk.ru/club243396555')
            # Не запускаем второй набор заявок — проверяем флаг через заранее недоступный счётчик.
            with patch.object(delivery, 'read_target_count', side_effect=delivery.CounterError('offline')):
                second = verifier.start('https://vk.ru/club243396555', acknowledge_risk=True, background=False)
            self.assertEqual(verifier.status(second['campaign_id'])['status'], 'failed')

    def test_late_submission_after_stop_is_recorded_and_blocks_repeat(self):
        began = threading.Event()
        def legacy_driver(svc, link, **kw):
            # Старый драйвер не вызвал callback, но сообщает об отправке только
            # после того, как пользователь запросил остановку.
            began.set()
            kw['cancel_event'].wait(4)
            return drivers.DriverResult('ok', 'заказ принят', submitted=True)
        with patch.object(delivery, 'read_target_count', return_value=100), \
             patch.object(drivers, 'run', side_effect=legacy_driver):
            cid = verifier.start('https://vk.ru/club243396555')['campaign_id']
            try:
                self.assertTrue(began.wait(4))
            finally:
                verifier.stop(cid)
            for _ in range(100):
                c = verifier.status(cid)
                # Финальный статус записывается перед освобождением блокировки.
                if c['status'] not in ('running', 'stopping') and not executor.AUTO_LOCK.locked():
                    break
                time.sleep(.02)
            self.assertEqual(c['status'], 'stopped')
            self.assertFalse(executor.AUTO_LOCK.locked())
            result = next(r for r in c['results'] if r['site_id'] == 'prskill')
            self.assertEqual(result['status'], 'unverified')
            self.assertIsNotNone(result['submitted_at'])
            self.assertIsNone(result['added_count'])
            self.assertEqual(verifier.unresolved_count(), 1)
            with self.assertRaisesRegex(ValueError, 'риск повторного запуска'):
                verifier.start('https://vk.ru/club243396555')

    def test_legacy_task_and_new_campaign_cannot_start_in_parallel(self):
        launched = threading.Event()
        def driver(svc, link, **kw):
            launched.set()
            kw['cancel_event'].wait(5)
            return drivers.DriverResult('cancelled', 'остановлено')
        with patch.object(delivery, 'read_target_count', return_value=100), patch.object(drivers, 'run', side_effect=driver):
            cid = verifier.start('https://vk.ru/club243396555')['campaign_id']
            self.assertTrue(launched.wait(4))
            with db.tx() as conn:
                target = conn.execute('SELECT id FROM targets LIMIT 1').fetchone()[0]
                conn.execute("INSERT INTO tasks(service_id,target_id,status,created_at) VALUES('prskill-vk-followers',?,'ready',?)",
                             (target, int(time.time())))
                tid = conn.execute('SELECT max(id) FROM tasks').fetchone()[0]
            result = executor.start_task(tid)
            self.assertFalse(result['ok'])
            verifier.stop(cid)
            for _ in range(100):
                if verifier.status(cid)['status'] not in ('running', 'stopping'): break
                time.sleep(0.02)

    def test_stopping_a_hung_driver_keeps_barrier_until_it_really_exits(self):
        started, release = threading.Event(), threading.Event()
        calls = []
        def uncooperative_driver(svc, link, **kw):
            calls.append(svc['site_id'])
            kw['on_submit']()
            started.set()
            release.wait(5)  # не реагирует на cancel_event
            return drivers.DriverResult('cancelled', 'stopped')
        try:
            with patch.object(delivery, 'read_target_count', return_value=100), \
                 patch.object(drivers, 'run', side_effect=uncooperative_driver):
                cid = verifier.start('https://vk.ru/club243396555')['campaign_id']
                self.assertTrue(started.wait(3))
                verifier.stop(cid)
                time.sleep(0.3)
                self.assertEqual(verifier.status(cid)['status'], 'stopping')
                with self.assertRaises(ValueError):
                    verifier.start('https://vk.ru/club243396555')
                self.assertEqual(calls, ['prskill'])
                release.set()
                for _ in range(100):
                    if verifier.status(cid)['status'] not in ('running', 'stopping'): break
                    time.sleep(.02)
                self.assertEqual(verifier.status(cid)['status'], 'stopped')
        finally:
            release.set()

    def test_recover_only_dead_owner_and_pid_reuse(self):
        with db.tx() as conn:
            conn.execute("INSERT INTO campaigns(channel_url,platform,status,started_at,owner_pid,owner_birth) "
                         "VALUES('https://vk.ru/club1','vk','running',1,?,?)",
                         (os.getpid(), db._process_birth(os.getpid())))
            cid = conn.execute('SELECT max(id) FROM campaigns').fetchone()[0]
            conn.execute("INSERT INTO campaign_results(campaign_id,seq_no,site_id,site_name,service_id,service_label,"
                         "channel_url,status,added_count,submitted_at) VALUES(?,1,'s','Site','v','Svc','url','running',0,1)", (cid,))
            conn.execute("INSERT INTO campaign_results(campaign_id,seq_no,site_id,site_name,service_id,service_label,"
                         "channel_url,status) VALUES(?,2,'s2','Site 2','v','Svc','url','pending')", (cid,))
        db.recover()
        self.assertEqual(verifier.status(cid)['status'], 'running')
        # Такой же PID, но идентификатор запуска не совпадает: это уже другой процесс.
        if db._process_birth(os.getpid()) is None:
            self.skipTest('/proc недоступен')
        with db.tx() as conn:
            conn.execute("UPDATE campaigns SET owner_birth='expired-process' WHERE id=?", (cid,))
        db.recover()
        c = verifier.status(cid)
        self.assertEqual(c['status'], 'interrupted')
        self.assertEqual(c['results'][0]['status'], 'unverified')
        self.assertIsNone(c['results'][0]['added_count'])
        self.assertEqual(c['results'][1]['status'], 'skipped')
        with self.assertRaisesRegex(ValueError, 'риск повторного запуска'):
            verifier.start('https://vk.ru/club243396555')

    def test_old_not_null_migration_preserves_results_samples_indexes_and_triggers(self):
        import re
        conn = db.connect()
        try:
            ddl = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='campaign_results'").fetchone()[0]
            old = re.sub(r'(added_count\s+INTEGER)\s*,', r'\1 NOT NULL DEFAULT 0,', ddl, count=1)
            old = re.sub(r'(?m)^\s*service_url\s+TEXT[^\n]*\n', '', old, count=1)
            self.assertNotEqual(ddl, old)
            self.assertNotIn('service_url', old)
            conn.execute('PRAGMA foreign_keys=OFF')
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('DROP TABLE campaign_results')
            conn.execute(old)
            conn.execute('CREATE INDEX idx_campaign_results_cid ON campaign_results(campaign_id,seq_no)')
            conn.execute('CREATE INDEX custom_results_site ON campaign_results(site_id)')
            conn.execute("CREATE TRIGGER custom_results_update AFTER UPDATE ON campaign_results BEGIN "
                         "INSERT INTO events(ts,level,message) VALUES(1,'info','trigger'); END")
            conn.execute("INSERT INTO campaigns(channel_url,platform,status,started_at,owner_pid) "
                         "VALUES('https://vk.ru/club1','vk','running',1,99999999)")
            cid = conn.execute('SELECT max(id) FROM campaigns').fetchone()[0]
            conn.execute("INSERT INTO campaign_results(campaign_id,seq_no,site_id,site_name,service_id,service_label,"
                         "channel_url,status,added_count,submitted_at) VALUES(?,1,'s','Site','v','Svc','url','running',0,1)", (cid,))
            rid = conn.execute('SELECT max(id) FROM campaign_results').fetchone()[0]
            conn.execute("INSERT INTO campaign_samples(campaign_id,result_id,observed_at,count,source) "
                         "VALUES(?,?,1,42,'fixture')", (cid, rid))
            conn.execute('COMMIT')
        finally:
            conn.close()
        db.init_db()
        with db.rd() as conn:
            info = {r[1]: r for r in conn.execute('PRAGMA table_info(campaign_results)')}
            self.assertEqual(info['added_count'][3:5], (0, None))
            self.assertIn('service_url', info)
            result = conn.execute('SELECT status,added_count,service_url FROM campaign_results WHERE id=?', (rid,)).fetchone()
            self.assertEqual(tuple(result), ('unverified', None, ''))
            self.assertEqual(conn.execute('SELECT result_id,count FROM campaign_samples').fetchone()[:], (rid, 42))
            objects = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE tbl_name='campaign_results'")}
            self.assertTrue({'custom_results_site', 'custom_results_update', 'idx_campaign_results_cid'} <= objects)
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], db.SCHEMA_VERSION)
        old_report = verifier.status(cid)
        self.assertIsNone(old_report['total_added'])
        self.assertIn('Страница площадки: нет данных', verifier.format_report(old_report))

    def test_unverified_order_blocks_legacy_queue_until_explicit_acknowledgement(self):
        with db.tx() as conn:
            tid = conn.execute("INSERT INTO targets(name,platform,url,kind,created_at) "
                               "VALUES('test','vk','https://vk.ru/club123','community',1)").lastrowid
            task = conn.execute("INSERT INTO tasks(service_id,target_id,status,created_at,due_at) "
                                "VALUES('prskill-vk-followers',?,'ready',1,1)", (tid,)).lastrowid
            cid = conn.execute("INSERT INTO campaigns(target_id,channel_url,platform,status,started_at) "
                               "VALUES(?,'https://vk.ru/club123','vk','interrupted',1)", (tid,)).lastrowid
            conn.execute("INSERT INTO campaign_results(campaign_id,seq_no,site_id,site_name,service_id,service_label,"
                         "channel_url,status,submitted_at) VALUES(?,1,'s','Site','v','Svc','url','unverified',1)", (cid,))
        self.assertEqual(verifier.unresolved_count(), 1)
        self.assertIsNone(engine.next_open_task())
        with self.assertRaisesRegex(ValueError, 'непроверенная заявка'):
            executor.start_task(task)
        with self.assertRaises(ValueError):
            verifier.acknowledge_risk(False)
        self.assertEqual(verifier.acknowledge_risk(True)['acknowledged'], 1)
        self.assertEqual(verifier.unresolved_count(), 0)
        self.assertEqual(engine.next_open_task()['id'], task)

    def test_migration_preserves_active_barrier(self):
        with db.tx() as conn:
            conn.execute('DROP INDEX idx_delivery_active')
            conn.execute("CREATE UNIQUE INDEX idx_delivery_active ON deliveries(target_key,metric) "
                         "WHERE state IN ('reserved','submitting')")
        db.init_db()
        with db.rd() as conn:
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='idx_delivery_active'").fetchone()[0]
        self.assertIn('waiting', sql)
        self.assertIn('uncertain', sql)


if __name__ == '__main__':
    unittest.main()
