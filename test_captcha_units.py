"""Проверки отказоустойчивости решателя без доступа к внешним площадкам."""
import unittest
from unittest.mock import patch
from core import captcha, drivers


class StubPage:
    def js(self, script):
        if script == captcha.IMAGE_JS:
            return '[{"sel":"#img", "src":""}]'
        if script == captcha.INPUT_FOR_CODE_JS:
            return '#captcha-code'
        return ''
    def wait(self, ms): pass
    def click_human(self, sel): return True


class CaptchaSafetyTests(unittest.TestCase):
    def test_fill_failure_is_not_success(self):
        with patch.object(captcha, 'ocr_engine', return_value=object()), \
             patch.object(captcha, '_img_bytes', return_value=b'image'), \
             patch.object(captcha, '_ocr_guess', return_value='AB12'), \
             patch.object(drivers, 'set_field', return_value=False):
            result = captcha.solve_image(StubPage())
        self.assertFalse(result.ok)
        self.assertIn('не удалось', result.detail)

    def test_image_guess_is_only_candidate_not_confirmation(self):
        with patch.object(captcha, 'ocr_engine', return_value=object()), \
             patch.object(captcha, '_img_bytes', return_value=b'image'), \
             patch.object(captcha, '_ocr_guess', return_value='AB12'), \
             patch.object(drivers, 'set_field', return_value=True):
            result = captcha.solve_image(StubPage())
        self.assertTrue(result.ok)
        self.assertIn('ожидается проверка', result.detail)
        self.assertFalse(any('AB12' in line for line in result.steps))

    def test_widget_frame_discovery_even_without_dom_iframe(self):
        class Page(StubPage):
            def captcha_frames(self):
                return [{'kind': 'turnstile', 'x': 100, 'y': 100, 'w': 300, 'h': 65}]
        self.assertEqual(captcha.visible_widgets(Page())[0]['kind'], 'turnstile')

    def test_manual_confirmation_while_browser_is_open(self):
        from core import executor
        class Event:
            def clear(self): pass
            def is_set(self): return True
        with patch.object(drivers, '_autosolve', return_value=False), \
             patch.object(drivers.db, 'get_setting', return_value='30'), \
             patch.object(executor, 'captcha_event', return_value=Event()), \
             patch.object(executor, '_set_task') as set_task:
            result = drivers._solve_or_wait(StubPage(), [], {}, 42, False, True)
        self.assertTrue(result)
        self.assertEqual([call.args[1] for call in set_task.call_args_list], ['captcha', 'running'])

    def test_no_manual_wait_without_a_live_task(self):
        with patch.object(drivers, '_autosolve', return_value=False):
            self.assertFalse(drivers._solve_or_wait(StubPage(), [], {}, 0, False, True))
