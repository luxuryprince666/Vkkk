# -*- coding: utf-8 -*-
"""Тесты этапов M1 (профили площадок) и M2 (цели: «сколько и за какой срок»).

Запуск:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = tempfile.mkdtemp(prefix="vkfree-m12-")
os.environ.setdefault("VKFREE_DATA", TMP)
# модуль test_core тоже выставляет VKFREE_DATA — берём общий путь только если он уже задан
if os.environ.get("VKFREE_DATA"):
    TMP = os.environ["VKFREE_DATA"]

from core import db, engine, executor, goals, profiles  # noqa: E402

GOOD_SITES = ("doctorsmm", "smmnakrutka", "nakruti", "prskill", "doctorsmm", "likeboom")


def fresh_db() -> None:
    for f in Path(TMP).glob("app.db*"):
        f.unlink()
    db.DB_PATH = Path(TMP) / "app.db"
    db.DATA = Path(TMP)
    db.BACKUP_DIR = Path(TMP) / "backups"
    db.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    db.init_db()
    profiles.reset_cache()


def post_target(url: str = "https://vk.com/wall-1_2", kind: str = "post") -> int:
    with db.tx() as c:
        return c.execute("INSERT INTO targets(name,platform,url,kind,created_at) VALUES('Пост','vk',?,?,?)",
                         (url, kind, engine.now())).lastrowid


class ProfilesBase(unittest.TestCase):
    def setUp(self) -> None:
        fresh_db()


class TestProfilesSeed(ProfilesBase):
    def test_profiles_created_for_every_site(self):
        with db.rd() as c:
            sites = c.execute("SELECT COUNT(*) FROM sites").fetchone()[0]
        self.assertEqual(len(profiles.all_profiles()), sites)

    def test_classes_are_known(self):
        prof = profiles.all_profiles()
        for sid, p in prof.items():
            self.assertIn(p["automation"], ("A", "B", "C", "-"), f"{sid}: неизвестный класс")
            self.assertIsInstance(p["captcha"], list)

    def test_hold_ignores_delivery_time(self):
        """«выдача 3 мин – 1 час» — это доставка, а не ожидание: ждать не надо."""
        self.assertEqual(profiles._hold_of({"hold": "выдача 3 минуты"}), 0)
        self.assertEqual(profiles._hold_of({"hold": "мгновенно"}), 0)
        self.assertEqual(profiles._hold_of({"hold": "таймер 5 минут"}), 300)
        self.assertEqual(profiles._hold_of({"hold": "очередь через 37 секунд"}), 37)
        self.assertGreaterEqual(profiles._hold_of({"hold": "таймер на странице"}), 15)

    def test_hold_capped(self):
        self.assertLessEqual(profiles._hold_of({"hold": "таймер 999 минут"}), profiles.MAX_HOLD)

    def test_known_timer_extracted(self):
        prof = profiles.all_profiles()
        self.assertEqual(prof["smmnakrutka"]["hold_seconds"], 300)   # 5 минут
        self.assertEqual(prof["nakruti"]["hold_seconds"], 0)         # таймера нет

    def test_dead_sites_unusable(self):
        prof = profiles.all_profiles()
        # feijoa/bestsmm удалены из каталога; проверяем живую семантику is_usable
        self.assertTrue(profiles.is_usable("doctorsmm", prof))
        self.assertFalse(profiles.is_usable("net-takogo-saita", {"net-takogo-saita": {"automation": "-", "degraded": 0}}))
        self.assertFalse(profiles.is_usable("doctorsmm", {"doctorsmm": {"automation": "B", "degraded": 1}}))

    def test_reseed_keeps_manual_state(self):
        with db.tx() as c:
            c.execute("UPDATE site_profiles SET degraded=1, degraded_note='тест' WHERE site_id='doctorsmm'")
        profiles.reset_cache()
        profiles.seed_profiles()
        p = profiles.get_profile("doctorsmm")
        self.assertEqual(p["degraded"], 1)
        self.assertEqual(p["degraded_note"], "тест")


class TestProfilesHealth(ProfilesBase):
    def test_health_rows_and_summary(self):
        rows = profiles.health()
        self.assertGreaterEqual(len(rows), 8)
        for r in rows:
            self.assertIn("status", r)
            self.assertIn(r["status"], ("работает", "капча", "форма изменилась", "не работает", "выключена"))
        self.assertTrue(any(r["status"] == "капча" for r in rows))

    def test_check_site_marks_degraded_only_after_two_fails(self):
        orig = profiles.probe.probe
        try:
            profiles.probe.probe = lambda url: {"status": 200, "error": None, "has_link_field": False,
                                                "has_submit": False, "captcha": [], "fields_n": 0,
                                                "forms_n": 0, "spa": False}
            first = profiles.check_site("doctorsmm")
            self.assertEqual(first["verdict"], "changed")
            self.assertEqual(profiles.get_profile("doctorsmm")["degraded"], 0)   # один сбой — не приговор
            profiles.check_site("doctorsmm")
            self.assertEqual(profiles.get_profile("doctorsmm")["degraded"], 1)   # два подряд — выключаем
            self.assertFalse(profiles.is_usable("doctorsmm"))
        finally:
            profiles.probe.probe = orig

    def test_good_form_resets_degradation(self):
        orig = profiles.probe.probe
        try:
            profiles.probe.probe = lambda url: {"status": 200, "error": None, "has_link_field": False,
                                                "has_submit": False, "captcha": [], "fields_n": 0,
                                                "forms_n": 0, "spa": False}
            profiles.check_site("doctorsmm")
            profiles.check_site("doctorsmm")
            self.assertEqual(profiles.get_profile("doctorsmm")["degraded"], 1)
            profiles.probe.probe = lambda url: {"status": 200, "error": None, "has_link_field": True,
                                                "has_submit": True, "captcha": [], "fields_n": 3,
                                                "forms_n": 1, "spa": False}
            res = profiles.check_site("doctorsmm")
            self.assertEqual(res["verdict"], "ok")
            self.assertEqual(profiles.get_profile("doctorsmm")["degraded"], 0)
            self.assertTrue(profiles.is_usable("doctorsmm"))
        finally:
            profiles.probe.probe = orig

    def test_unreachable_site(self):
        orig = profiles.probe.probe
        try:
            profiles.probe.probe = lambda url: {"status": None, "error": "Name or service not known"}
            res = profiles.check_site("doctorsmm")
            self.assertEqual(res["verdict"], "dead")
        finally:
            profiles.probe.probe = orig

    def test_check_unknown_site_raises(self):
        with self.assertRaises(ValueError):
            profiles.check_site("нет-такой")

    def test_stale_sites_lists_enabled_unchecked(self):
        self.assertIn("doctorsmm", profiles.stale_sites(24))


class TestPlanRespectsProfiles(ProfilesBase):
    def test_plan_skips_degraded_site(self):
        tid = post_target()
        with db.tx() as c:
            c.execute("UPDATE site_profiles SET degraded=1, degraded_note='форма изменилась' WHERE site_id='doctorsmm'")
            c.execute("UPDATE site_profiles SET degraded=1 WHERE site_id='smmnakrutka'")
        profiles.reset_cache()
        svc = next((v for v in engine.services_map().values() if v["id"] == "doctorsmm-vk-likes"), None)
        self.assertIsNotNone(svc, "нужна услуга doctorsmm-vk-likes в каталоге")
        res = engine.plan(tid, [{"service_id": svc["id"], "claims": 1}], autostart=False)
        self.assertEqual(len(res["created"]), 0)
        self.assertTrue(any("заходы не ставлю" in w for w in res["warnings"]))

    def test_service_view_marks_broken_site(self):
        with db.tx() as c:
            c.execute("UPDATE site_profiles SET degraded=1 WHERE site_id='likeboom'")
        profiles.reset_cache()
        st = engine.state()
        broken = [s for s in st["services"] if s["site_broken"]]
        self.assertTrue(broken)
        for s in broken:
            self.assertFalse(s["ready"], "услуга сломанной площадки не может быть готова")


class TestMetricWords(unittest.TestCase):
    def test_plural_forms(self):
        self.assertEqual(goals.metric_word(1, "likes"), "1 лайк")
        self.assertEqual(goals.metric_word(3, "likes"), "3 лайка")
        self.assertEqual(goals.metric_word(11, "likes"), "11 лайков")
        self.assertEqual(goals.metric_word(21, "views"), "21 просмотр")
        self.assertEqual(goals.metric_word(100, "followers"), "100 подписчиков")
        self.assertEqual(goals.metric_word(0, "plays"), "0 прослушиваний")


class GoalsBase(ProfilesBase):
    def setUp(self) -> None:
        super().setUp()
        # планировщик живёт в рабочем окне «8-23»: ночью (вне окна) превью честно
        # даёт planned=0 и тест падал бы только ночью — расширяем окно на всю ночь
        db.set_setting("work_hours", "0-24")
        self.tid = post_target()
        self.cid = post_target("https://vk.com/club12345", "community")


class TestGoalPreview(GoalsBase):
    def test_feasible_small_goal(self):
        pv = goals.preview(self.tid, [{"metric": "likes", "need": 100},
                                      {"metric": "views", "need": 200}], days=2)
        self.assertEqual(pv["items"][0]["need"], 100)
        self.assertEqual(pv["items"][0]["verdict"], "feasible")
        self.assertGreater(pv["items"][0]["capacity"], 100)
        self.assertTrue(pv["feasible"])
        self.assertIn("заход", pv["headline"])

    def test_impossible_shows_real_max(self):
        pv = goals.preview(self.tid, [{"metric": "likes", "need": 5000}], hours=1)
        item = pv["items"][0]
        self.assertEqual(item["verdict"], "impossible")
        self.assertLess(item["planned"], 5000)
        self.assertGreater(item["planned"], 0)
        self.assertTrue(any(s["kind"] == "max" for s in pv["suggestions"]))
        self.assertTrue(any(s["kind"] == "extend" for s in pv["suggestions"]))

    def test_followers_not_for_post(self):
        pv = goals.preview(self.tid, [{"metric": "followers", "need": 100}], days=1)
        item = pv["items"][0]
        self.assertEqual(item["verdict"], "impossible")
        self.assertTrue(any("сообщество" in r for r in item["reasons"]))

    def test_followers_for_community(self):
        pv = goals.preview(self.cid, [{"metric": "followers", "need": 10}], days=1)
        self.assertGreater(pv["items"][0]["sites_n"], 0)

    def test_need_is_additive_over_baseline(self):
        # «сколько» — сколько добавить; «уже есть» — зафиксированный базлайн, не вычитается
        pv = goals.preview(self.tid, [{"metric": "likes", "need": 100, "have_before": 40}], days=1)
        self.assertEqual(pv["items"][0]["left"], 100)
        self.assertEqual(pv["items"][0]["have_before"], 40)

    def test_have_before_none_stays_none(self):
        # пустое «уже есть» — софт определит базлайн сам при создании цели
        pv = goals.preview(self.tid, [{"metric": "likes", "need": 100, "have_before": None}], days=1)
        self.assertIsNone(pv["items"][0]["have_before"])
        self.assertEqual(pv["items"][0]["left"], 100)

    def test_bad_args(self):
        with self.assertRaises(ValueError):
            goals.preview(self.tid, [], days=1)
        with self.assertRaises(ValueError):
            goals.preview(9999, [{"metric": "likes", "need": 10}], days=1)
        with self.assertRaises(ValueError):
            goals.preview(self.tid, [{"metric": "лайки", "need": 10}], days=1)
        with self.assertRaises(ValueError):
            goals.preview(self.tid, [{"metric": "likes", "need": 10}], days=-1)


class TestGoalSchedule(GoalsBase):
    def test_slots_inside_deadline_and_work_hours(self):
        now_ts = engine.now()
        pv = goals.preview(self.tid, [{"metric": "likes", "need": 300}], days=2)
        self.assertTrue(pv["schedule"])
        for slot in pv["schedule"]:
            self.assertGreaterEqual(slot["at"], now_ts - 5)
            self.assertLessEqual(slot["at"], pv["deadline_at"] + 5)
            self.assertTrue(engine.in_work_hours(slot["at"]), "заход должен попадать в рабочие часы")

    def test_cooldown_between_visits_of_same_service(self):
        pv = goals.preview(self.tid, [{"metric": "likes", "need": 500}], days=2, pace="max")
        svcs = engine.services_map()
        last = {}
        for slot in sorted(pv["schedule"], key=lambda s: s["at"]):
            sid = slot["service_id"]
            cool = int(svcs[sid]["cooldown_min"] or 0) * 60
            if sid in last:
                self.assertGreaterEqual(slot["at"] - last[sid], max(60, cool * 0.99),
                                        f"кулдаун услуги {sid} нарушен")
            last[sid] = slot["at"]

    def test_daily_claims_cap_respected(self):
        pv = goals.preview(self.tid, [{"metric": "likes", "need": 3000}], days=1, pace="max")
        svcs = engine.services_map()
        per_day = {}
        for slot in pv["schedule"]:
            day = time.strftime("%Y-%m-%d", time.localtime(slot["at"]))
            per_day.setdefault((slot["service_id"], day), 0)
            per_day[(slot["service_id"], day)] += 1
        for (sid, day), n in per_day.items():
            cap = int(svcs[sid]["max_claims_day"] or 96)
            self.assertLessEqual(n, cap, f"{sid} {day}: {n} заходов при лимите {cap}")

    def test_plan_not_bigger_than_need(self):
        pv = goals.preview(self.tid, [{"metric": "views", "need": 50}], days=1)
        self.assertLessEqual(sum(s["units"] for s in pv["schedule"]), 50)

    def test_no_network_calls_in_preview(self):
        """Расчёт не должен ходить в интернет: только база и каталог."""
        import socket
        orig = socket.socket

        def no(*a, **k):
            raise AssertionError("preview попытался открыть сокет")

        socket.socket = no
        try:
            goals.preview(self.tid, [{"metric": "likes", "need": 40}], days=1)
        finally:
            socket.socket = orig


class TestGoalLifecycle(GoalsBase):
    def test_create_makes_linked_tasks(self):
        res = goals.create(self.tid, [{"metric": "likes", "need": 100}], days=2)
        gid = res["goal_id"]
        self.assertGreater(len(res["created"]), 0)
        with db.rd() as c:
            rows = c.execute("SELECT goal_id, metric, planned_units, due_at FROM tasks").fetchall()
        self.assertTrue(all(r["goal_id"] == gid for r in rows))
        self.assertTrue(all(r["metric"] == "likes" for r in rows))
        self.assertLessEqual(sum(r["planned_units"] for r in rows), 100)

    def test_progress_grows_after_visit(self):
        res = goals.create(self.tid, [{"metric": "likes", "need": 100}], days=2)
        gid = res["goal_id"]
        with db.rd() as c:
            t = c.execute("SELECT id FROM tasks WHERE goal_id=? ORDER BY due_at LIMIT 1", (gid,)).fetchone()
        before = goals.progress(gid)["items"][0]["done"]
        executor.finish_task(t["id"], True, "тест")
        after = goals.progress(gid)["items"][0]
        self.assertGreater(after["done"], before)
        self.assertLess(after["left"], 100)

    def test_goal_marked_done_when_all_visits_closed(self):
        res = goals.create(self.tid, [{"metric": "views", "need": 100}], days=1)
        gid = res["goal_id"]
        for _ in range(12):
            with db.rd() as c:
                t = c.execute("SELECT id FROM tasks WHERE goal_id=? AND status IN ('queued','ready')"
                              " ORDER BY due_at LIMIT 1", (gid,)).fetchone()
            if not t:
                break
            executor.finish_task(t["id"], True, "тест")
        pr = goals.progress(gid)
        self.assertEqual(pr["status"], "done")
        self.assertEqual(pr["percent"], 100)

    def test_failed_visit_keeps_goal_open(self):
        res = goals.create(self.tid, [{"metric": "likes", "need": 100}], days=2)
        gid = res["goal_id"]
        with db.rd() as c:
            t = c.execute("SELECT id FROM tasks WHERE goal_id=? ORDER BY due_at LIMIT 1", (gid,)).fetchone()
        executor.finish_task(t["id"], False, "площадка не ответила")
        pr = goals.progress(gid)
        self.assertNotEqual(pr["status"], "done")
        self.assertGreater(pr["items"][0]["left"], 0)

    def test_pause_and_resume(self):
        res = goals.create(self.tid, [{"metric": "likes", "need": 100}], days=2)
        gid = res["goal_id"]
        self.assertTrue(goals.pause(gid)["paused"])
        self.assertEqual(goals.progress(gid)["status"], "paused")
        with db.rd() as c:
            live = c.execute("SELECT COUNT(*) FROM tasks WHERE goal_id=? AND status IN ('ready','running')",
                             (gid,)).fetchone()[0]
        self.assertEqual(live, 0)
        self.assertTrue(goals.resume(gid)["resumed"])

    def test_replan_removes_and_rebuilds(self):
        res = goals.create(self.tid, [{"metric": "likes", "need": 100}], days=2)
        gid = res["goal_id"]
        rep = goals.replan(gid)
        self.assertGreater(rep["removed"], 0)
        with db.rd() as c:
            open_n = c.execute("SELECT COUNT(*) FROM tasks WHERE goal_id=? AND status IN ('queued','ready')",
                               (gid,)).fetchone()[0]
        self.assertGreater(open_n, 0)

    def test_extend_moves_deadline(self):
        res = goals.create(self.tid, [{"metric": "likes", "need": 100}], days=1)
        gid = res["goal_id"]
        before = goals.progress(gid)["deadline_at"]
        goals.extend(gid, 2)
        self.assertGreater(goals.progress(gid)["deadline_at"], before)

    def test_remove_clears_tasks(self):
        res = goals.create(self.tid, [{"metric": "likes", "need": 100}], days=1)
        gid = res["goal_id"]
        out = goals.remove(gid)
        self.assertGreater(out["removed_tasks"], 0)
        with db.rd() as c:
            left = c.execute("SELECT COUNT(*) FROM tasks WHERE goal_id=?", (gid,)).fetchone()[0]
        self.assertEqual(left, 0)
        self.assertEqual(goals.list_goals(), [])

    def test_list_goals(self):
        goals.create(self.tid, [{"metric": "likes", "need": 20}], days=1)
        self.assertEqual(len(goals.list_goals()), 1)

    def test_degraded_site_is_blocker_in_progress(self):
        res = goals.create(self.tid, [{"metric": "likes", "need": 100}], days=2)
        gid = res["goal_id"]
        with db.rd() as c:
            sites = [r[0] for r in c.execute(
                "SELECT DISTINCT v.site_id FROM tasks t JOIN services v ON v.id=t.service_id WHERE t.goal_id=?",
                (gid,)).fetchall()]
        with db.tx() as c:
            c.execute("UPDATE site_profiles SET degraded=1, degraded_note='форма изменилась' WHERE site_id=?",
                      (sites[0],))
        profiles.reset_cache()
        self.assertTrue(any("форма изменилась" in b for b in goals.progress(gid)["blockers"]))


class TestGoalQuotaGuard(GoalsBase):
    def test_preview_counts_today_claims(self):
        """Сделанные сегодня заходы уменьшают ёмкость — план не должен их дублировать."""
        before = goals.preview(self.tid, [{"metric": "likes", "need": 100}], days=1)["items"][0]["planned"]
        pres = goals._services_for_ro({"platform": "vk", "url": "https://vk.com/wall-1_2", "kind": "post"},
                                      "likes", profiles.all_profiles())
        self.assertTrue(pres)
        # израсходуем все заходы по самой ёмкой услуге
        target_svc = max(pres, key=lambda v: int(v["amount"]))
        with db.tx() as c:
            for i in range(int(target_svc["max_claims_day"] or 1)):
                c.execute("INSERT INTO tasks(service_id,target_id,status,planned_units,claimed_units,created_at,"
                          "finished_at) VALUES(?,?,'done',?,?,?,?)",
                          (target_svc["id"], self.tid, int(target_svc["amount"]), int(target_svc["amount"]),
                           engine.now(), engine.now()))
        after = goals.preview(self.tid, [{"metric": "likes", "need": 100}], days=1)["items"][0]["planned"]
        self.assertLessEqual(after, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
