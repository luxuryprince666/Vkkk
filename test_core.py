# -*- coding: utf-8 -*-
"""Тесты ядра: лимиты, кулдауны, breaker, восстановление после падения.

Запуск:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = tempfile.mkdtemp(prefix="vkfree-test-")
os.environ["VKFREE_DATA"] = TMP

from core import db, engine, executor  # noqa: E402
from core.server import validate_target  # noqa: E402


def fresh_db() -> None:
    for f in Path(TMP).glob("app.db*"):
        f.unlink()
    db.DB_PATH = Path(TMP) / "app.db"
    db.DATA = Path(TMP)
    db.BACKUP_DIR = Path(TMP) / "backups"
    db.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    db.init_db()


class Base(unittest.TestCase):
    def setUp(self) -> None:
        fresh_db()
        with db.tx() as c:
            self.tid = c.execute(
                "INSERT INTO targets(name,platform,url,created_at) VALUES('t','vk','https://vk.com/test',0)"
            ).lastrowid

    def svc(self, sid: str) -> dict:
        return engine.services_map()[sid]


class TestPlan(Base):
    def test_plan_respects_requested_claims(self):
        r = engine.plan(self.tid, [{"service_id": "nakruti-vk-followers", "claims": 3}])
        self.assertEqual(len(r["created"]), 3)

    def test_plan_caps_by_daily_limit(self):
        # у nakruti-vk-followers суточный лимит 3 захода
        r = engine.plan(self.tid, [{"service_id": "nakruti-vk-followers", "claims": 10}])
        self.assertEqual(len(r["created"]), 3)
        self.assertTrue(r["warnings"])

    def test_cooldown_spacing(self):
        r = engine.plan(self.tid, [{"service_id": "smmnakrutka-vk-followers", "claims": 4}])
        dues = [c["due_at"] for c in r["created"]]
        step = 300 * 60
        for a, b in zip(dues, dues[1:]):
            self.assertEqual(b - a, step, "шаг должен равняться кулдауну услуги")

    def test_shared_slot_site(self):
        """LikeBoom: один бесплатный заказ на сайт — вторая услуга не должна получить то же время."""
        r = engine.plan(self.tid, [{"service_id": "likeboom-vk-followers", "claims": 1},
                                   {"service_id": "likeboom-vk-likes", "claims": 1}])
        self.assertEqual(len(r["created"]), 2)
        dues = sorted(c["due_at"] for c in r["created"])
        self.assertGreaterEqual(dues[1] - dues[0], 20 * 60)

    def test_wrong_platform_skipped(self):
        with db.tx() as c:
            tg = c.execute("INSERT INTO targets(name,platform,url,created_at) VALUES"
                           "('ig','instagram','https://instagram.com/x',0)").lastrowid
        r = engine.plan(tg, [{"service_id": "doctorsmm-vk-followers", "claims": 1}])
        self.assertEqual(len(r["created"]), 0)
        self.assertTrue(any("платформ" in w for w in r["warnings"]))

    def test_after_done_limit_shrinks(self):
        engine.plan(self.tid, [{"service_id": "nakruti-vk-followers", "claims": 1}])
        with db.tx() as c:
            tid_task = c.execute("SELECT id FROM tasks LIMIT 1").fetchone()["id"]
        executor.finish_task(tid_task, True)
        r = engine.plan(self.tid, [{"service_id": "nakruti-vk-followers", "claims": 5}])
        self.assertEqual(len(r["created"]), 2, "осталось 2 из 3 суточных заходов")


class TestState(Base):
    def test_state_shape(self):
        st = engine.state()
        for key in ("sites", "services", "targets", "tasks", "stats", "settings", "health"):
            self.assertIn(key, st)
        self.assertGreater(len(st["services"]), 20)

    def test_vk_followers_cap_positive(self):
        st = engine.state()
        followers = [b for b in st["stats"]["by_type"] if b["platform"] == "vk" and b["type"] == "followers"]
        self.assertTrue(followers)
        self.assertGreater(followers[0]["left_units"], 300)

    def test_claimed_units_counted(self):
        engine.plan(self.tid, [{"service_id": "doctorsmm-vk-followers", "claims": 1}])
        with db.tx() as c:
            tid_task = c.execute("SELECT id FROM tasks LIMIT 1").fetchone()["id"]
        executor.finish_task(tid_task, True)
        st = engine.state()
        svc = [s for s in st["services"] if s["id"] == "doctorsmm-vk-followers"][0]
        self.assertEqual(svc["claims_today"], 1)
        self.assertEqual(svc["units_today"], 50)


class TestBreaker(Base):
    def test_site_disabled_after_three_failures(self):
        engine.plan(self.tid, [{"service_id": "doctorsmm-vk-followers", "claims": 1}])
        with db.tx() as c:
            tid_task = c.execute("SELECT id FROM tasks LIMIT 1").fetchone()["id"]
        for _ in range(3):
            executor.start_task(tid_task)
            executor.finish_task(tid_task, False, "тест")
            executor.retry_task(tid_task)
        with db.rd() as c:
            site = c.execute("SELECT enabled, breaker_until FROM sites WHERE id='doctorsmm'").fetchone()
        self.assertEqual(site["enabled"], 0)
        self.assertGreater(site["breaker_until"], engine.now())

    def test_success_resets_failures(self):
        executor.mark_fail("doctorsmm")
        executor.mark_fail("doctorsmm")
        engine.plan(self.tid, [{"service_id": "doctorsmm-vk-followers", "claims": 1}])
        with db.tx() as c:
            tid_task = c.execute("SELECT id FROM tasks LIMIT 1").fetchone()["id"]
        executor.finish_task(tid_task, True)
        with db.rd() as c:
            self.assertEqual(c.execute("SELECT breaker_fails FROM sites WHERE id='doctorsmm'").fetchone()[0], 0)


class TestRecovery(Base):
    def test_running_tasks_restored(self):
        engine.plan(self.tid, [{"service_id": "doctorsmm-vk-followers", "claims": 1}])
        with db.tx() as c:
            c.execute("UPDATE tasks SET status='running'")
        db.recover()
        with db.rd() as c:
            self.assertEqual(c.execute("SELECT status FROM tasks").fetchone()[0], "ready")

    def test_reopen_extends_deadline(self):
        """Повторный клик по открытой задаче не создаёт второй заход, а продлевает время."""
        engine.plan(self.tid, [{"service_id": "doctorsmm-vk-followers", "claims": 1}])
        with db.tx() as c:
            tid_task = c.execute("SELECT id FROM tasks LIMIT 1").fetchone()["id"]
        first = executor.start_task(tid_task)
        second = executor.start_task(tid_task)
        self.assertEqual(second["mode"], "assisted")
        self.assertGreaterEqual(second["deadline"], first["deadline"])
        with db.rd() as c:
            row = c.execute("SELECT status, attempts FROM tasks WHERE id=?", (tid_task,)).fetchone()
        self.assertEqual(row["status"], "wait_confirm")
        self.assertEqual(row["attempts"], 1, "переоткрытие не считается новым заходом")

    def test_done_task_cannot_start(self):
        engine.plan(self.tid, [{"service_id": "doctorsmm-vk-followers", "claims": 1}])
        with db.tx() as c:
            tid_task = c.execute("SELECT id FROM tasks LIMIT 1").fetchone()["id"]
        executor.finish_task(tid_task, True)
        with self.assertRaises(ValueError):
            executor.start_task(tid_task)

    def test_stale_confirm_returns_to_queue(self):
        """Человек отошёл и не подтвердил — задача не залипает навсегда."""
        engine.plan(self.tid, [{"service_id": "doctorsmm-vk-followers", "claims": 1}])
        with db.tx() as c:
            tid_task = c.execute("SELECT id FROM tasks LIMIT 1").fetchone()["id"]
        executor.start_task(tid_task)
        with db.tx() as c:
            c.execute("UPDATE tasks SET due_at=0 WHERE id=?", (tid_task,))
        self.assertEqual(engine.reap_stale(), 1)
        with db.rd() as c:
            self.assertEqual(c.execute("SELECT status FROM tasks WHERE id=?", (tid_task,)).fetchone()[0], "queued")

    def test_stale_confirm_gives_up_after_three(self):
        engine.plan(self.tid, [{"service_id": "doctorsmm-vk-followers", "claims": 1}])
        with db.tx() as c:
            tid_task = c.execute("SELECT id FROM tasks LIMIT 1").fetchone()["id"]
        executor.start_task(tid_task)
        with db.tx() as c:
            c.execute("UPDATE tasks SET due_at=0, attempts=3 WHERE id=?", (tid_task,))
        engine.reap_stale()
        with db.rd() as c:
            self.assertEqual(c.execute("SELECT status FROM tasks WHERE id=?", (tid_task,)).fetchone()[0], "missed")


class TestValidation(unittest.TestCase):
    def test_good(self):
        self.assertEqual(validate_target("vk", "https://vk.com/club1"), "https://vk.com/club1")

    def test_bad_domain(self):
        with self.assertRaises(ValueError):
            validate_target("vk", "https://evil.example.com/x")

    def test_bad_scheme(self):
        with self.assertRaises(ValueError):
            validate_target("vk", "vk.com/club1")

    def test_spaces(self):
        with self.assertRaises(ValueError):
            validate_target("vk", "https://vk.com/a b")


class TestSettings(Base):
    def test_work_hours_window(self):
        db.set_setting("work_hours", "0-24")
        self.assertTrue(engine.in_work_hours())
        db.set_setting("work_hours", "3-4")
        # вне окна 3–4 часа должно быть False (кроме случая, когда сейчас ровно 3 часа)
        from datetime import datetime
        hour = datetime.now().hour
        self.assertEqual(engine.in_work_hours(), hour == 3)

    def test_global_gap_guard(self):
        db.set_setting("work_hours", "0-24")
        db.set_setting("global_min_gap_sec", "0")
        self.assertTrue(engine.can_start_now()[0])
        db.set_setting("global_min_gap_sec", "600")
        with db.tx() as c:
            c.execute("INSERT INTO tasks(service_id,target_id,status,created_at,started_at) VALUES"
                      "('doctorsmm-vk-followers',?,'wait_confirm',0,?)", (self.tid, engine.now()))
        ok, why = engine.can_start_now()
        self.assertFalse(ok)
        self.assertIn("интервал", why)

    def test_daily_total_limit_guard(self):
        db.set_setting("work_hours", "0-24")
        db.set_setting("global_min_gap_sec", "0")
        db.set_setting("daily_total_limit", "1")
        with db.tx() as c:
            c.execute("INSERT INTO tasks(service_id,target_id,status,created_at,finished_at) VALUES"
                      "('doctorsmm-vk-followers',?,'done',0,?)", (self.tid, engine.now()))
        ok, why = engine.can_start_now()
        self.assertFalse(ok)
        self.assertIn("лимит", why)

    def test_dry_run_blocks_browser(self):
        db.set_setting("dry_run", "1")
        self.assertFalse(executor.open_url("https://example.com"))


class TestManualSettings(Base):
    """Ручная настройка кулдаунов/объёмов и режимы капчи."""

    def test_override_cooldown(self):
        db.set_service_override("doctorsmm-vk-followers", cooldown_min=7)
        svc = engine.services_map()["doctorsmm-vk-followers"]
        self.assertEqual(svc["cooldown_min"], 7)
        self.assertEqual(svc["cooldown_min_catalog"], 1440)
        self.assertTrue(svc["cooldown_min_manual"])
        view = [x for x in engine.state()["services"] if x["id"] == "doctorsmm-vk-followers"][0]
        self.assertEqual(view["cooldown_min"], 7)
        self.assertEqual(view["cooldown_catalog"], 1440)

    def test_reset_overrides_back_to_catalog(self):
        db.set_service_override("doctorsmm-vk-followers", cooldown_min=7, amount=99)
        db.set_service_override("doctorsmm-vk-followers", cooldown_min=None, amount=None)
        svc = engine.services_map()["doctorsmm-vk-followers"]
        self.assertEqual(svc["cooldown_min"], 1440)
        self.assertEqual(svc["amount"], 50)
        self.assertFalse(svc["cooldown_min_manual"])

    def test_site_wide_cooldown(self):
        db.set_site_cooldown("doctorsmm", 5)
        svcs = [v for v in engine.services_map().values() if v["site_id"] == "doctorsmm"]
        self.assertTrue(svcs)
        self.assertTrue(all(v["cooldown_min"] == 5 for v in svcs))

    def test_catalog_reload_keeps_manual_values(self):
        db.set_service_override("doctorsmm-vk-followers", cooldown_min=7)
        db.seed_catalog()          # имитация кнопки «перезагрузить каталог»
        self.assertEqual(engine.services_map()["doctorsmm-vk-followers"]["cooldown_min"], 7)

    def test_reschedule_uses_new_cooldown(self):
        db.set_setting("work_hours", "0-24")
        r = engine.plan(self.tid, [{"service_id": "smmnakrutka-vk-followers", "claims": 3}])
        dues_before = [c["due_at"] for c in r["created"]]
        self.assertEqual(dues_before[1] - dues_before[0], 300 * 60)
        db.set_service_override("smmnakrutka-vk-followers", cooldown_min=2)
        engine.reschedule()
        with db.rd() as conn:
            dues = [row["due_at"] for row in conn.execute(
                "SELECT due_at FROM tasks WHERE status IN ('queued','ready') ORDER BY due_at")]
        self.assertEqual(len(dues), 3)
        self.assertEqual(dues[1] - dues[0], 2 * 60, "очередь должна пересчитаться под новый кулдаун")


class TestCaptchaModes(Base):
    def test_nocaptcha_mode_allows_unknown(self):
        """Непроверенный сайт в режиме «без капчи» работает — иначе режим бесполезен в первый день."""
        db.set_setting("captcha_mode", "nocaptcha")
        with db.tx() as c:
            c.execute("UPDATE services SET captcha=-1")
        r = engine.plan(self.tid, [{"service_id": "smmnakrutka-vk-followers", "claims": 2}])
        self.assertEqual(len(r["created"]), 2)

    def test_nocaptcha_mode_excludes_known_captcha(self):
        db.set_setting("captcha_mode", "nocaptcha")
        db.set_service_override("doctorsmm-vk-followers", captcha=1)
        r = engine.plan(self.tid, [{"service_id": "doctorsmm-vk-followers", "claims": 2}])
        self.assertEqual(len(r["created"]), 0)
        self.assertTrue(any("режим" in w for w in r["warnings"]))

    def test_nocaptcha_mode_allows_known_clean(self):
        db.set_setting("captcha_mode", "nocaptcha")
        db.set_service_override("smmnakrutka-vk-followers", captcha=0)
        r = engine.plan(self.tid, [{"service_id": "smmnakrutka-vk-followers", "claims": 2}])
        self.assertEqual(len(r["created"]), 2)

    def test_captcha_mode_only_captcha_sites(self):
        db.set_setting("captcha_mode", "captcha")
        db.set_service_override("doctorsmm-vk-followers", captcha=0)
        db.set_service_override("doctorsmm-vk-likes", captcha=1)
        r = engine.plan(self.tid, [{"service_id": "doctorsmm-vk-followers", "claims": 1},
                                   {"service_id": "doctorsmm-vk-likes", "claims": 1}])
        self.assertEqual([c["service_id"] for c in r["created"]], ["doctorsmm-vk-likes"])

    def test_next_open_task_respects_mode(self):
        db.set_setting("work_hours", "0-24")
        db.set_setting("autopilot", "1")
        db.set_service_override("doctorsmm-vk-followers", captcha=1)
        engine.plan(self.tid, [{"service_id": "doctorsmm-vk-followers", "claims": 1}])
        db.set_setting("captcha_mode", "nocaptcha")
        self.assertIsNone(engine.next_open_task(), "в режиме «без капчи» задача с капчей не должна браться")
        db.set_setting("captcha_mode", "captcha")
        self.assertIsNotNone(engine.next_open_task())
        db.set_setting("captcha_mode", "all")
        self.assertIsNotNone(engine.next_open_task())

    def test_site_captcha_bulk(self):
        db.set_site_captcha("doctorsmm", 0)
        svcs = [v for v in engine.services_map().values() if v["site_id"] == "doctorsmm"]
        self.assertTrue(all(v["captcha"] == 0 for v in svcs))

    def test_captcha_stat_in_state(self):
        db.set_service_override("doctorsmm-vk-followers", captcha=0)
        st = engine.state()
        cap = st["stats"]["captcha"]
        self.assertEqual(cap["none"], 1)
        self.assertEqual(cap["mode"], "all")
        self.assertEqual(cap["allowed"], len(st["services"]))


class TestRobustness(Base):
    """Регрессии, найденные живыми прогонами."""

    def test_captcha_allowed_accepts_sqlite_row(self):
        with db.rd() as conn:
            row = conn.execute("SELECT * FROM services WHERE id='doctorsmm-vk-followers'").fetchone()
        self.assertTrue(engine.captcha_allowed(row, "all"))
        self.assertTrue(engine.captcha_allowed(row, "nocaptcha"))

    def test_failed_start_returns_task_to_queue(self):
        """Если запуск сорвался, задача не должна остаться «выполняется» навсегда."""
        engine.plan(self.tid, [{"service_id": "doctorsmm-vk-followers", "claims": 1}])
        # уже после планирования включаем режим, в который задача не входит
        db.set_setting("captcha_mode", "nocaptcha")
        db.set_service_override("doctorsmm-vk-followers", captcha=1)
        with db.tx() as c:
            c.execute("UPDATE sites SET mode='auto' WHERE id='doctorsmm'")
        with db.tx() as c:
            tid_task = c.execute("SELECT id FROM tasks LIMIT 1").fetchone()["id"]
        with self.assertRaises(ValueError):
            executor.start_task(tid_task)
        with db.rd() as c:
            row = c.execute("SELECT status FROM tasks WHERE id=?", (tid_task,)).fetchone()
        self.assertEqual(row["status"], "ready")


class TestBulkSiteMode(Base):
    def test_all_sites_mode_endpoint_logic(self):
        with db.tx() as c:
            c.execute("UPDATE sites SET mode='assisted'")
        with db.tx() as c:
            cur = c.execute("UPDATE sites SET mode='auto' WHERE enabled=1")
        with db.rd() as c:
            enabled_n = c.execute("SELECT COUNT(*) FROM sites WHERE enabled=1").fetchone()[0]
        # проверяем, что массовый режим задел ВСЕ включённые площадки (их число меняется:
        # мёртвые домены софт выключает сам, см. research/free-nakrutka-2026.md)
        self.assertEqual(cur.rowcount, enabled_n)
        self.assertGreaterEqual(enabled_n, 8)
        with db.rd() as c:
            modes = {row[0] for row in c.execute("SELECT DISTINCT mode FROM sites WHERE enabled=1")}
        self.assertEqual(modes, {"auto"})


class TestServerStructure(unittest.TestCase):
    """Сторож: маршруты обязаны быть методами класса Handler."""

    def test_handler_has_routes(self):
        from core.server import Handler
        for method in ("do_GET", "do_POST", "do_DELETE", "_route"):
            self.assertTrue(callable(getattr(Handler, method, None)),
                            f"Handler.{method} отсутствует — сломана структура класса")

    def test_routes_not_leaked_to_module(self):
        from core import server
        self.assertFalse(hasattr(server, "do_POST"), "маршруты не должны оказаться на уровне модуля")
        self.assertTrue(callable(server._opt_int))


class TestServerValidation(unittest.TestCase):
    def test_opt_int_keep_and_reset(self):
        from core.server import _opt_int
        self.assertEqual(_opt_int({}, "cooldown_min", 0, 100, "x"), "keep")
        self.assertIsNone(_opt_int({"cooldown_min": None}, "cooldown_min", 0, 100, "x"))
        self.assertIsNone(_opt_int({"cooldown_min": ""}, "cooldown_min", 0, 100, "x"))
        self.assertEqual(_opt_int({"cooldown_min": 15}, "cooldown_min", 0, 100, "x"), 15)

    def test_opt_int_out_of_range(self):
        from core.server import _opt_int
        with self.assertRaises(ValueError):
            _opt_int({"cooldown_min": 99999}, "cooldown_min", 0, 10080, "Кулдаун")
        with self.assertRaises(ValueError):
            _opt_int({"cooldown_min": "abc"}, "cooldown_min", 0, 10080, "Кулдаун")

class RobotNowLockTest(unittest.TestCase):
    """Кнопка «выполнить роботом» не должна сама занимать AUTO_LOCK.

    Иначе _prepare_after_claim() не может его взять и задача мгновенно уезжает
    в «робот занят другой задачей» — именно так и было.
    """

    def setUp(self):
        fresh_db()
        self.ex = executor

    def test_lock_not_taken_before_start(self):
        with db.tx() as conn:
            tg_id = conn.execute("INSERT INTO targets (name, platform, url, created_at) VALUES (?,?,?,?)",
                                 ("Т", "vk", "https://vk.ru/club1", 0)).lastrowid
            conn.execute("INSERT INTO tasks (service_id, target_id, status, due_at, created_at)"
                         " VALUES (?,?,?,?,?)", ("doctorsmm-vk-followers", tg_id, "queued", 0, 0))
            tid = conn.execute("SELECT id FROM tasks ORDER BY id DESC LIMIT 1").fetchone()[0]
        seen = {}

        def fake_start(task_id):
            seen["locked"] = executor.AUTO_LOCK.locked()
            seen["task"] = task_id
            raise RuntimeError("остановлено в тесте")

        orig = executor.start_task
        executor.start_task = fake_start
        try:
            with self.assertRaises(RuntimeError):
                executor.robot_now(tid)
        finally:
            executor.start_task = orig
        self.assertFalse(seen.get("locked"), "AUTO_LOCK был занят ещё до старта задачи")
        self.assertNotIn(tid, executor._FORCE_AUTO, "флаг «принудительно роботом» остался висеть")

    def test_refuses_when_robot_busy(self):
        with db.tx() as conn:
            tg_id = conn.execute("INSERT INTO targets (name, platform, url, created_at) VALUES (?,?,?,?)",
                                 ("Т2", "vk", "https://vk.ru/club2", 0)).lastrowid
            conn.execute("INSERT INTO tasks (service_id, target_id, status, due_at, created_at)"
                         " VALUES (?,?,?,?,?)", ("doctorsmm-vk-followers", tg_id, "queued", 0, 0))
            tid = conn.execute("SELECT id FROM tasks ORDER BY id DESC LIMIT 1").fetchone()[0]
        executor.AUTO_LOCK.acquire()
        try:
            res = executor.robot_now(tid)
        finally:
            executor.AUTO_LOCK.release()
        self.assertFalse(res["ok"])
        self.assertIn("занят", res["error"])

if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestSubjectSplit(Base):
    """«Подписчики» — это два разных объекта: сообщество и личная страница."""

    def test_detect_kind_from_url(self):
        cases = {
            "https://vk.com/club12345": "community",
            "https://vk.com/public987": "community",
            "https://vk.com/event42": "community",
            "https://vk.com/id777": "person",
            "https://vk.com/wall-123_456": "post",
            "https://vk.com/photo-1_2": "post",
            "https://vk.com/video-1_2": "post",
            "https://vk.com/clip-1_2": "post",
            "https://vk.com/away.php?to=https%3A%2F%2Fx.ru": "",
            "https://vk.com/moj_pablik": "",       # короткое имя — не угадываем
            "https://vk.ru/club5": "community",
        }
        for url, want in cases.items():
            self.assertEqual(engine.detect_target_kind("vk", url), want, url)

    def test_subject_fits(self):
        self.assertTrue(engine.subject_fits("community", "community"))
        self.assertTrue(engine.subject_fits("any", "person"))
        self.assertTrue(engine.subject_fits("person", ""))         # тип не указан — не мешаем
        self.assertFalse(engine.subject_fits("person", "community"))
        self.assertFalse(engine.subject_fits("post", "community"))

    def test_catalog_has_both_follower_kinds(self):
        svcs = engine.services_map().values()
        vk_fol = [s for s in svcs if s["platform"] == "vk" and s["stype"] == "followers"]
        kinds = {s.get("subject") for s in vk_fol}
        self.assertIn("community", kinds)
        self.assertIn("page", kinds)   # подписчики на личную страницу (prtut/pricesmm)
        # у «друзей» объект всегда личная страница
        for s in svcs:
            if s["platform"] == "vk" and s["stype"] == "friends":
                self.assertEqual(s["subject"], "person")

    def test_plan_blocks_wrong_object(self):
        with db.tx() as c:
            c.execute("UPDATE targets SET kind='community', url='https://vk.com/club1' WHERE id=?", (self.tid,))
        res = engine.plan(self.tid, [{"service_id": "prtut-vk-page-followers", "claims": 1}], autostart=False)
        self.assertEqual(len(res["created"]), 0)
        self.assertTrue(any("личная страница" in w for w in res["warnings"]), res["warnings"])

    def test_plan_allows_matching_object(self):
        with db.tx() as c:
            c.execute("UPDATE targets SET kind='community', url='https://vk.com/club1' WHERE id=?", (self.tid,))
        res = engine.plan(self.tid, [{"service_id": "prskill-vk-followers", "claims": 1}], autostart=False)
        self.assertEqual(len(res["created"]), 1, res["warnings"])

    def test_plan_respects_auto_detect(self):
        # kind не указан, но ссылка — на личную страницу: услуга «в группу» должна отсечься
        with db.tx() as c:
            c.execute("UPDATE targets SET kind='', url='https://vk.com/id555' WHERE id=?", (self.tid,))
        res = engine.plan(self.tid, [{"service_id": "prskill-vk-followers", "claims": 1},
                                     {"service_id": "prtut-vk-page-followers", "claims": 1}], autostart=False)
        created = {c["service_id"] for c in res["created"]}
        self.assertIn("prtut-vk-page-followers", created)
        self.assertNotIn("prskill-vk-followers", created)

    def test_unknown_kind_warns_but_works(self):
        with db.tx() as c:
            c.execute("UPDATE targets SET kind='', url='https://vk.com/korotkoe_imya' WHERE id=?", (self.tid,))
        res = engine.plan(self.tid, [{"service_id": "prskill-vk-followers", "claims": 1}], autostart=False)
        self.assertEqual(len(res["created"]), 1)
        self.assertTrue(any("не указано" in w for w in res["warnings"]), res["warnings"])

    def test_strict_subject_off(self):
        db.set_setting("strict_subject", "0")
        with db.tx() as c:
            c.execute("UPDATE targets SET kind='community', url='https://vk.com/club1' WHERE id=?", (self.tid,))
        res = engine.plan(self.tid, [{"service_id": "prtut-vk-page-followers", "claims": 1}], autostart=False)
        self.assertEqual(len(res["created"]), 1)
        db.set_setting("strict_subject", "1")

    def test_state_has_subject_split(self):
        st = engine.state()
        self.assertIn("subjects", st["stats"])
        rows = [r for r in st["stats"]["by_subject"] if r["platform"] == "vk" and r["type"] == "followers"]
        subjects = {r["subject"] for r in rows}
        self.assertEqual(subjects, {"community", "person"})
        for r in rows:
            self.assertIn("left_units", r)
        tg = st["targets"][0]
        self.assertIn("kind", tg)
        self.assertIn("kind_auto", tg)

    def test_target_kind_declared_wins_over_detect(self):
        with db.tx() as c:
            c.execute("UPDATE targets SET kind='person', url='https://vk.com/club1' WHERE id=?", (self.tid,))
        st = engine.state()
        self.assertEqual(st["targets"][0]["kind"], "person")
        self.assertFalse(st["targets"][0]["kind_auto"])


class TestTargetKindApi(unittest.TestCase):
    def test_kind_arg_validation(self):
        from core.server import _target_kind_arg
        self.assertEqual(_target_kind_arg({"kind": "community"}, "vk", "u"), "community")
        self.assertEqual(_target_kind_arg({"kind": "auto"}, "vk", "u"), "")
        self.assertEqual(_target_kind_arg({}, "vk", "u", current="person"), "person")
        with self.assertRaises(ValueError):
            _target_kind_arg({"kind": "group"}, "vk", "u")


class TestHardening(Base):
    """Проверки на реальные дефекты, найденные при прогоне: домены, настройки, уборка каталога."""

    def test_domain_check_rejects_lookalike(self):
        from core.server import validate_target
        for bad in ("https://vk.com.evil.example/x", "https://notvk.com/x",
                    "https://vk.com@evil.example/x", "https://evil.example/?vk.com"):
            with self.assertRaises(ValueError, msg=bad):
                validate_target("vk", bad)
        for good in ("https://vk.com/moj_pablik", "https://m.vk.com/x",
                     "https://vk.ru/id1", "https://vk.com:443/x"):
            self.assertEqual(validate_target("vk", good), good)

    def test_settings_are_validated(self):
        from core.server import _clean_settings
        self.assertEqual(_clean_settings({"autopilot": "true"})["autopilot"], "1")
        self.assertEqual(_clean_settings({"dry_run": False})["dry_run"], "0")
        self.assertEqual(_clean_settings({"work_hours": "9-22"})["work_hours"], "9-22")
        self.assertEqual(_clean_settings({"theme": "dark"})["theme"], "dark")
        for bad in ({"theme": "purple"}, {"autopilot": "может быть"}, {"work_hours": "круглосуточно"},
                    {"captcha_wait_sec": 5}, {"daily_total_limit": -1}, {"неизвестно": "1"}, {}):
            with self.assertRaises(ValueError, msg=str(bad)):
                _clean_settings(bad)

    def test_plan_rejects_garbage_items(self):
        with self.assertRaises(ValueError):
            engine.plan(self.tid, "not-a-list", autostart=False)
        with self.assertRaises(ValueError):
            engine.plan(self.tid, [], autostart=False)
        with self.assertRaises(ValueError):
            engine.plan(None, [{"service_id": "doctorsmm-vk-followers", "claims": 1}])

    def test_removed_site_disappears_from_db(self):
        import json as _json
        cat = _json.loads(Path(db.CATALOG_PATH).read_text(encoding="utf-8"))
        cat["sites"] = [x for x in cat["sites"] if x["id"] != "doctorsmm"]
        mini = Path(TMP) / "catalog-mini.json"
        mini.write_text(_json.dumps(cat, ensure_ascii=False), encoding="utf-8")
        old = db.CATALOG_PATH
        db.CATALOG_PATH = mini
        try:
            info = db.seed_catalog()
        finally:
            db.CATALOG_PATH = old
        self.assertGreaterEqual(info["sites"], 1)
        self.assertGreaterEqual(info["services"], 1)
        with db.rd() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) n FROM sites WHERE id='doctorsmm'").fetchone()["n"], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) n FROM services WHERE site_id='doctorsmm'").fetchone()["n"], 0)

    def test_start_task_blocks_wrong_object(self):
        with db.tx() as c:
            c.execute("UPDATE targets SET kind='community' WHERE id=?", (self.tid,))
            c.execute("INSERT INTO tasks(service_id,target_id,status,planned_units,created_at,due_at)"
                      " VALUES('prtut-vk-page-followers',?,'ready',20,0,0)", (self.tid,))
            tid = c.execute("SELECT MAX(id) m FROM tasks").fetchone()["m"]
        with self.assertRaises(ValueError):
            executor.start_task(tid)
        with db.rd() as c:
            row = c.execute("SELECT status, note FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertTrue("другой объект" in row["note"] or "личная страница" in row["note"])

    def test_csv_export_has_object_columns(self):
        with db.tx() as c:
            c.execute("UPDATE targets SET kind='community' WHERE id=?", (self.tid,))
        engine.plan(self.tid, [{"service_id": "prskill-vk-followers", "claims": 1}], autostart=False)
        path = db.export_csv()
        text = Path(path).read_text(encoding="utf-8-sig")
        head = text.splitlines()[0]
        self.assertIn("объект услуги", head)
        self.assertIn("объект ссылки", head)
        self.assertIn("сообщество", text)

    def test_catalog_no_smmchik(self):
        import json as _json
        cat = _json.loads(Path(db.CATALOG_PATH).read_text(encoding="utf-8"))
        ids = {x["id"] for x in cat["sites"]}
        self.assertNotIn("smmchik", ids)
        self.assertEqual(len(ids), 13)
        self.assertGreaterEqual(sum(len(x["services"]) for x in cat["sites"]), 12)


class TestWizard(unittest.TestCase):
    """Мастер «Накрутить»: что показывается для конкретной ссылки."""

    def setUp(self) -> None:
        fresh_db()
        with db.tx() as c:
            self.tid = c.execute(
                "INSERT INTO targets(name,platform,url,kind,created_at)"
                " VALUES('паблик','vk','https://vk.com/club900','community',0)").lastrowid
            self.tid_person = c.execute(
                "INSERT INTO targets(name,platform,url,kind,created_at)"
                " VALUES('страница','vk','https://vk.com/id900','person',0)").lastrowid
            self.tid_post = c.execute(
                "INSERT INTO targets(name,platform,url,kind,created_at)"
                " VALUES('запись','vk','https://vk.com/wall-1_2','post',0)").lastrowid

    def test_groups_for_community(self):
        r = engine.services_for_target(self.tid)
        titles = [g["title"] for g in r["groups"]]
        self.assertIn("Подписчики в сообщество", titles)
        self.assertNotIn("Подписчики на личную страницу", titles)
        ids = {i["id"] for g in r["groups"] for i in g["items"]}
        self.assertNotIn("prtut-vk-page-followers", ids)     # только для страниц
        self.assertIn("prskill-vk-followers", ids)           # только для групп
        self.assertTrue(all(i["gives"].startswith("+") for g in r["groups"] for i in g["items"]))

    def test_groups_for_person(self):
        r = engine.services_for_target(self.tid_person)
        titles = [g["title"] for g in r["groups"]]
        self.assertIn("Подписчики на личную страницу", titles)
        self.assertIn("Друзья на личную страницу", titles)
        ids = {i["id"] for g in r["groups"] for i in g["items"]}
        self.assertNotIn("prskill-vk-followers", ids)
        self.assertIn("prtut-vk-page-followers", ids)

    def test_post_target_has_no_followers(self):
        r = engine.services_for_target(self.tid_post)
        for g in r["groups"]:
            self.assertNotIn("followers", g["key"])
            self.assertNotIn("friends", g["key"])
        self.assertTrue(r["groups"], "для записи должны быть лайки/просмотры")

    def test_ready_first_and_human_text(self):
        r = engine.services_for_target(self.tid)
        items = [i for g in r["groups"] for i in g["items"]]
        self.assertTrue(items)
        for i in items:
            self.assertIn("mode_hint", i)
            self.assertIsInstance(i["blocked"], str)
            self.assertRegex(i["gives"], r"^\+\d+ \w+")
        # внутри группы: сначала без блокировки, потом с ней
        for g in r["groups"]:
            seen_blocked = False
            for i in g["items"]:
                if i["blocked"]:
                    seen_blocked = True
                elif seen_blocked:
                    self.fail("доступные услуги должны идти раньше недоступных")

    def test_unknown_target(self):
        with self.assertRaises(ValueError):
            engine.services_for_target(999999)

    def test_plural_and_gives(self):
        self.assertEqual(engine.plural_ru(1, ("лайк", "лайка", "лайков")), "лайк")
        self.assertEqual(engine.plural_ru(3, ("лайк", "лайка", "лайков")), "лайка")
        self.assertEqual(engine.plural_ru(11, ("лайк", "лайка", "лайков")), "лайков")
        self.assertEqual(engine.gives_text({"amount": 10, "type": "followers"}), "+10 подписчиков")
        self.assertEqual(engine.gives_text({"amount": 1, "type": "views"}), "+1 просмотр")


class TestBodyDrain(unittest.TestCase):
    """Тело POST должно дочитываться всегда — иначе следующий запрос ломается."""

    def setUp(self) -> None:
        fresh_db()

    def test_server_reads_body_on_start_route(self):
        import http.client
        import threading
        from core.server import QuietServer, Handler

        srv = QuietServer(("127.0.0.1", 0), Handler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            # POST с телом на маршрут, который тело не разбирает
            conn.request("POST", "/api/tick", body="{}", headers={"Content-Type": "application/json"})
            r1 = conn.getresponse(); r1.read()
            self.assertLess(r1.status, 500)
            # тем же соединением — обычный GET: он должен пройти, а не 501
            conn.request("GET", "/api/health")
            r2 = conn.getresponse(); body = r2.read()
            self.assertEqual(r2.status, 200, body[:120])
            conn.close()
        finally:
            srv.shutdown()
            srv.server_close()

    def test_two_posts_same_connection_do_not_share_body(self):
        """Кэш тела не должен переезжать на следующий запрос того же соединения."""
        import http.client
        import json
        import threading
        from core.server import QuietServer, Handler

        srv = QuietServer(("127.0.0.1", 0), Handler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            # первое тело — большое и «запоминающееся»
            conn.request("POST", "/api/detect", body='{"url": "https://vk.com/club123456"}',
                         headers={"Content-Type": "application/json"})
            r1 = conn.getresponse(); first = json.loads(r1.read())
            self.assertEqual(r1.status, 200, first)
            # второе тело — другое; если кэш не сброшен, разберётся первое
            conn.request("POST", "/api/detect", body='{"url": "https://vk.com/person_ivan_01"}',
                         headers={"Content-Type": "application/json"})
            r2 = conn.getresponse(); second = json.loads(r2.read())
            self.assertEqual(r2.status, 200, second)
            self.assertNotEqual(first.get("kind"), second.get("kind"))
            # и следом обычный GET по тому же сокету — не 501
            conn.request("GET", "/api/health")
            r3 = conn.getresponse(); r3.read()
            self.assertEqual(r3.status, 200)
            conn.close()
        finally:
            srv.shutdown()
            srv.server_close()
