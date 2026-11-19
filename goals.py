# -*- coding: utf-8 -*-
"""Цели: «получи 100 лайков и 200 просмотров к 12.11» вместо «сделай 5 заходов».

Слой работает поверх обычных задач: цель считает ёмкость площадок, раскладывает заходы по
времени и создаёт те же строки в `tasks` (с `goal_id` и `metric`). Дальше всё как обычно —
планировщик, брейкеры, капча, восстановление после падения.

Три честных ответа, которые даёт preview:
    feasible  — с запасом, можно спокойно размазать по сроку;
    tight     — уложимся, но плотно, любая ошибка площадки может съесть запас;
    impossible— максимум меньше нужного: показываем максимум и что можно сделать.

Классы площадок берём из `site_profiles` (A/B/C): A и B считаются полной ёмкостью, C — половиной
(капча/iframe требует человека), площадки с общим пулом — третью и не больше самого пула.
"""
from __future__ import annotations

import json
import math
import random
import time

from core import db, engine, profiles

METRICS = {
    "likes":    ("лайки", "лайк", "лайка", "лайков"),
    "views":    ("просмотры", "просмотр", "просмотра", "просмотров"),
    "followers": ("подписчики", "подписчик", "подписчика", "подписчиков"),
    "friends":  ("друзья", "друг", "друга", "друзей"),
    "plays":    ("прослушивания", "прослушивание", "прослушивания", "прослушиваний"),
    "reposts":  ("репосты", "репост", "репоста", "репостов"),
    "comments": ("комментарии", "комментарий", "комментария", "комментариев"),
}

CLASS_WEIGHT = {"A": 1.0, "B": 1.0, "C": 0.5, "-": 0.0}
POOL_WEIGHT = 0.3
MAX_STEP_SEC = 3 * 3600           # не цедим мелочь сутками: максимум 3 часа между заходами
PACE_FILL = 0.7                   # раскладываем на 70% срока, остальное — запас на сбои


def metric_word(n: int, metric: str) -> str:
    """«100 лайков», «21 лайк», «5 просмотров» — без кривых окончаний."""
    forms = METRICS.get(metric)
    if not forms:
        return f"{n} {metric}"
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {forms[1]}"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} {forms[2]}"
    return f"{n} {forms[3]}"


# ------------------------------------------------------------------ рабочие часы и окна

def _work_window() -> tuple[int, int]:
    """(начало, конец) рабочих часов в минутах от полуночи, из настроек «8-23»."""
    raw = (db.get_setting("work_hours", "8-23") or "8-23")
    try:
        lo, hi = raw.split("-")
        return max(0, int(lo)) * 60, min(24, int(hi)) * 60
    except Exception:
        return 8 * 60, 23 * 60


def _minutes_in_window(start_ts: int, end_ts: int) -> int:
    """Сколько минут внутри рабочих часов между двумя моментами (по локальному времени)."""
    if end_ts <= start_ts:
        return 0
    lo, hi = _work_window()
    total = 0
    t = start_ts
    while t < end_ts:
        day = time.localtime(t)
        day_start_ts = int(time.mktime((day.tm_year, day.tm_mon, day.tm_mday, 0, 0, 0, 0, 0, -1)))
        w_start = day_start_ts + lo * 60
        w_end = day_start_ts + hi * 60
        seg_start = max(t, w_start)
        seg_end = min(end_ts, w_end)
        if seg_end > seg_start:
            total += (seg_end - seg_start) // 60
        t = day_start_ts + 86400
    return total


def _capabilities(max_claims: int) -> int:
    """Сколько заходов в день реально успеть (не больше одного в 15 минут на площадку)."""
    return max(1, min(int(max_claims), 96))


# ------------------------------------------------------------------ ёмкость

def _services_for(conn, target: dict, metric: str, profs: dict,
                  include_disabled: bool = False) -> list[dict]:
    """Услуги площадок, которыми можно закрыть метрику у этой ссылки."""
    kind = engine.detect_target_kind(target["platform"], target["url"]) \
        if not (target.get("kind") or "").strip() else target["kind"]
    rows = conn.execute("""
        SELECT v.*, s.name AS site_name, s.enabled AS site_enabled, s.mode AS site_mode
        FROM services v JOIN sites s ON s.id=v.site_id
        WHERE v.platform=? AND v.stype=? ORDER BY v.amount DESC""",
        (target["platform"], metric)).fetchall()
    out = []
    for r in rows:
        v = engine.apply_overrides(dict(r))
        if not include_disabled and (not v["enabled"] or not v["site_enabled"]):
            continue
        if not engine.subject_fits(v.get("subject"), kind):
            continue
        if kind == "post" and v["stype"] in ("followers", "friends"):
            continue
        prof = profs.get(v["site_id"]) or {}
        if prof.get("degraded") or prof.get("automation") == "-":
            continue
        v["_class"] = prof.get("automation") or "C"
        v["_hold"] = int(prof.get("hold_seconds") or 0)
        v["_pool"] = prof.get("pool_daily")
        out.append(v)
    return out


def _capacity_of(services: list[dict], start_ts: int, deadline_ts: int,
                 already_today: dict | None = None) -> dict[str, float]:
    """Сколько единиц метрики можно собрать за срок по каждой услуге."""
    minutes = _minutes_in_window(start_ts, deadline_ts)
    days = max(0.0, minutes / (60 * 15))            # «сутки» в масштабе 15-часового окна
    already_today = already_today or {}
    caps = {}
    for v in services:
        cool = max(1, int(v["cooldown_min"] or 0))
        by_time = math.floor(minutes / cool) if v["cooldown_min"] else int(minutes / 5)
        per_day = _capabilities(v["max_claims_day"] or 96)
        by_cap = math.floor(per_day * max(days, 15 / 60))     # хотя бы «сегодняшний остаток»
        used = int(already_today.get(v["id"], {}).get("claims", 0))
        claims = max(0, min(by_time, by_cap) - used)
        units = claims * int(v["amount"])
        weight = CLASS_WEIGHT.get(v["_class"], 0.5)
        if v.get("_pool"):
            units = min(units, int(v["_pool"]) * int(v["amount"]))
            weight *= POOL_WEIGHT
        caps[v["id"]] = units * weight
    return caps


# ------------------------------------------------------------------ раскладка по времени

def _gap_seconds(pace: str, rnd: random.Random) -> int:
    if pace == "max":
        return rnd.randint(20, 40)
    if pace == "quiet":
        return rnd.randint(600, 1500)               # не чаще 2–3 раз в час
    return rnd.randint(70, 200)                     # «по-человечески»: 1–3 минуты


def _plan_slots(services: list[dict], need: int, start_ts: int, deadline_ts: int,
                pace: str = "human", seed: int = 2026, already_today: dict | None = None) -> list[dict]:
    """Жадно раскладывает заходы: сначала класс A, потом B, потом C.

    Уважает: кулдаун услуги, суточный лимит заходов, рабочие часы, общий интервал между заходами,
    время на таймер площадки. Возвращает список {at, service, units}.
    """
    rnd = random.Random(seed)
    need = max(0, int(need))
    if not need or not services:
        return []
    pool = sorted(services, key=lambda v: (CLASS_WEIGHT.get(v["_class"], 0.5) < 1.0,
                                           -int(v["amount"])))
    next_ok = {v["id"]: start_ts for v in pool}
    used_today: dict[str, tuple[str, int]] = {}
    for sid, stat in (already_today or {}).items():
        used_today[sid] = (time.strftime("%Y-%m-%d", time.localtime(start_ts)), int(stat.get("claims", 0)))

    slots: list[dict] = []
    left = need
    t = engine.next_work_hour(start_ts) if not engine.in_work_hours(start_ts) else start_ts
    guard = 0
    sequential = db.get_setting('full_auto', '0') == '1'
    while left > 0 and t < deadline_ts and guard < 5000:
        guard += 1
        free = [v for v in pool if next_ok[v["id"]] <= t]
        if not free:
            nxt = min(next_ok[v["id"]] for v in pool)
            t = engine.next_work_hour(nxt) if not engine.in_work_hours(nxt) else nxt
            if t >= deadline_ts:
                break
            continue
        # суточные лимиты
        usable = []
        for v in free:
            day = time.strftime("%Y-%m-%d", time.localtime(t))
            used_day, used_n = used_today.get(v["id"], (day, 0))
            used_n = used_n if used_day == day else 0
            per_day = _capabilities(v["max_claims_day"] or 96)
            if used_n >= per_day:
                continue
            usable.append(v)
        if not usable:
            t = engine.next_work_hour(t + 86400) if not engine.in_work_hours(t + 86400) else t + 86400
            continue

        # В последовательном режиме нельзя заказать у сайта 20 и записать как 5.
        if sequential:
            usable = [v for v in usable if int(v['amount']) <= left]
            if not usable:
                future_fit = [v for v in pool if int(v['amount']) <= left and next_ok[v['id']] > t]
                if future_fit:
                    t = min(next_ok[v['id']] for v in future_fit)
                    continue
                break
        # класс важнее объёма: A и B раньше C
        usable.sort(key=lambda v: (CLASS_WEIGHT.get(v["_class"], 0.5) < 1.0, -int(v["amount"]), v["site_id"]))
        v = usable[0]
        units = min(int(v["amount"]), left)
        slots.append({"at": int(t), "service_id": v["id"], "site_id": v["site_id"],
                      "units": int(units), "amount": int(v["amount"]),
                      "site_name": v["site_name"], "label": v["label"], "class": v["_class"]})
        left -= units

        day = time.strftime("%Y-%m-%d", time.localtime(t))
        used_day, used_n = used_today.get(v["id"], (day, 0))
        used_today[v["id"]] = (day, (used_n if used_day == day else 0) + 1)

        cool = max(1, int(v["cooldown_min"] or 0)) * 60
        cool = int(cool * rnd.uniform(1.0, 1.15)) + int(v["_hold"] or 0)
        next_ok[v["id"]] = t + cool
        t = t + _gap_seconds(pace, rnd)
        if not engine.in_work_hours(t):
            t = engine.next_work_hour(t)
    return slots


def _stretch(slots: list[dict], start_ts: int, deadline_ts: int, seed: int = 7) -> list[dict]:
    """Растянуть готовый список на срок, чтобы не было «всё за пять минут»."""
    if len(slots) < 2:
        return slots
    rnd = random.Random(seed)
    window = max(60, deadline_ts - start_ts)
    step = min(window * PACE_FILL / len(slots), MAX_STEP_SEC)
    out, last_use = [], {}
    for i, s in enumerate(slots):
        target = start_ts + int(step * (i + rnd.uniform(0.2, 0.8)))
        prev = last_use.get(s["service_id"])
        if prev:
            target = max(target, prev + 300)
        if out:
            target = max(target, out[-1]["at"] + 60)
        if not engine.in_work_hours(target):
            target = engine.next_work_hour(target)
        last_use[s["service_id"]] = target
        out.append({**s, "at": int(target)})
    if out[-1]["at"] > deadline_ts:
        return slots                      # не влезает — оставляем плотный вариант
    return out


# ------------------------------------------------------------------ предпросмотр

def _target(target_id: int) -> dict:
    with db.rd() as conn:
        row = conn.execute("SELECT * FROM targets WHERE id=?", (int(target_id),)).fetchone()
    if not row:
        raise ValueError("Ссылка не найдена")
    return dict(row)


def _deadline(args: dict) -> int:
    now_ts = engine.now() if "engine" in globals() else int(time.time())
    if args.get("deadline_at"):
        return int(args["deadline_at"])
    if args.get("hours"):
        return now_ts + int(float(args["hours"]) * 3600)
    if args.get("days"):
        return now_ts + int(float(args["days"]) * 86400)
    raise ValueError("Не указан срок: передайте days, hours или deadline_at")


def preview(target_id: int, items: list[dict], deadline_at: int | None = None, *,
            days: float | None = None, hours: float | None = None, pace: str = "human") -> dict:
    """Что получится: по каждой метрике ёмкость, вердикт и (если можно) раскладка по времени."""
    now_ts = engine.now()
    deadline = deadline_at or _deadline({"days": days, "hours": hours})
    if deadline <= now_ts:
        raise ValueError("Срок уже прошёл — выберите время в будущем")
    target = _target(target_id)
    profs = profiles.all_profiles()
    since = engine.day_start(now_ts)
    with db.rd() as conn:
        stats = engine.claims_done_today(conn, list(engine.services_map().keys()), since)
    if not items:
        raise ValueError("Не указано, что накручиваем")

    out_items, suggestions, slots_all = [], [], []
    total_slots = 0
    for item in items:
        metric = (item.get("metric") or "").strip()
        if metric not in METRICS:
            raise ValueError(f"Неизвестная метрика: {metric}")
        need = max(1, int(item.get("need") or 0))
        have_raw = item.get("have_before")
        have = None if have_raw in (None, "") else max(0, int(have_raw))
        # Семантика 2026-11-12: «сколько» — сколько ДОБАВИТЬ к текущему значению,
        # «уже есть» — зафиксированный базлайн (текущее значение на объекте), для отчёта прироста.
        left = need
        services = _services_for_ro(target, metric, profs)
        caps = _capacity_of(services, now_ts, deadline, stats)
        per_site = {}
        for v in services:
            per_site[v["site_id"]] = per_site.get(v["site_id"], 0) + caps.get(v["id"], 0)
        capacity = int(sum(caps.values()))
        slots = _plan_slots(services, left, now_ts, deadline, pace, already_today=stats) if left else []
        slots = _stretch(slots, now_ts, deadline) if slots else []
        planned = sum(s["units"] for s in slots)
        if left <= 0:
            verdict = "done"
        elif planned >= left and capacity >= left * 2:
            verdict = "feasible"
        elif planned >= left:
            verdict = "tight"
        else:
            verdict = "impossible"
        reasons, sites_detail = [], []
        kind_now = target.get("kind") or engine.detect_target_kind(target["platform"], target["url"])
        if not services:
            if metric in ("followers", "friends") and kind_now == "post":
                reasons.append("подписчики начисляются на сообщество или личный профиль, а не на запись — "
                               "добавьте ссылку на группу/страницу и создайте цель для неё")
            elif metric in ("likes", "views", "plays", "reposts", "comments") and kind_now == "community":
                reasons.append("лайки и просмотры начисляются на конкретную запись/видео — "
                               "дайте ссылку вида vk.com/wall-… или vk.com/video-…")
            elif metric in ("plays",):
                reasons.append("прослушивания начисляются на аудиозапись или трек")
            else:
                reasons.append("на этой площадке нет бесплатной услуги под такую метрику")
        if verdict != "feasible":
            for v in sorted(services, key=lambda x: -caps.get(x["id"], 0))[:8]:
                cap = int(caps.get(v["id"], 0))
                why = []
                if v["_class"] == "C":
                    why.append("нужна капча/человек")
                if v.get("_pool"):
                    why.append("общий пул на всех")
                if v["max_claims_day"]:
                    why.append(f"{v['max_claims_day']} заходов/сутки")
                if v.get("_hold"):
                    why.append(f"таймер {v['_hold']}с")
                sites_detail.append({"site_id": v["site_id"], "site": v["site_name"],
                                     "per_visit": int(v["amount"]), "capacity": cap,
                                     "note": ", ".join(why) or "без ограничений"})
                if cap <= 0:
                    reasons.append(f"{v['site_name']}: сейчас недоступно")
        if verdict == "impossible":
            suggestions.append({"kind": "max", "units": planned,
                                "text": f"Взять максимум: {metric_word(planned, metric)} за этот срок"})
            for extra in (1, 2, 3, 7, 14):
                longer = _capacity_of(services, now_ts, deadline + extra * 86400, stats)
                if int(sum(longer.values())) >= left:
                    suggestions.append({"kind": "extend", "days": extra,
                                        "text": f"Продлить срок на {extra} дн. — этого хватит"})
                    break
        else:
            blocked = [p for p in profs.values() if p.get("degraded")]
            if blocked:
                suggestions.append({"kind": "check",
                                    "text": f"{len(blocked)} площадок не проверены — нажмите «Проверить» на экране «Площадки»"})
        if db.get_setting('full_auto', '0') == '1' and verdict in ('feasible', 'tight'):
            verdict = 'tight'  # скорость доставки неизвестна; обещать достижимость нельзя
            reasons.insert(0, 'Доставка идёт последовательно; время исполнения каждого заказа неизвестно')
        out_items.append({
            "metric": metric, "title": METRICS[metric][0], "need": need, "have_before": have,
            "left": left, "capacity": capacity, "planned": planned, "verdict": verdict,
            "sites_n": len({v["site_id"] for v in services}), "services_n": len(services),
            "visits": len(slots),
            "first_at": slots[0]["at"] if slots else None,
            "last_at": slots[-1]["at"] if slots else None,
            "reasons": reasons[:4], "sites": sites_detail,
        })
        slots_all.extend(slots)
        total_slots += len(slots)

    slots_all.sort(key=lambda s: s["at"])
    if db.get_setting('full_auto', '0') == '1':
        suggestions.insert(0, {"kind": "warning", "text": "Время плана приблизительное: следующий сайт запускается только после подтверждённого прироста; срок и точный объём не гарантированы."})
    head = None
    if slots_all:
        head = (f"{metric_word(out_items[0]['need'], out_items[0]['metric'])}"
                if len(out_items) == 1 else
                ", ".join(f"{metric_word(i['need'], i['metric'])}" for i in out_items))
        head += f" · {total_slots} заходов, с {engine.fmt(slots_all[0]['at'])} до {engine.fmt(slots_all[-1]['at'])}"
    return {"ok": True, "target": {"id": target["id"], "name": target["name"], "url": target["url"]},
            "deadline_at": deadline, "pace": pace, "items": out_items,
            "schedule": slots_all[:200], "visits": total_slots,
            "suggestions": suggestions, "headline": head,
            "feasible": all(i["verdict"] == "feasible" for i in out_items)}


def _services_for_ro(target: dict, metric: str, profs: dict) -> list[dict]:
    with db.rd() as conn:
        return _services_for(conn, target, metric, profs)


# ------------------------------------------------------------------ создание и ведение цели

def create(target_id: int, items: list[dict], deadline_at: int | None = None, *,
           days: float | None = None, hours: float | None = None, pace: str = "human",
           autostart: bool = True, title: str | None = None) -> dict:
    """Создать цель и её задачи. Возвращает {goal_id, tasks, ...}."""
    pv = preview(target_id, items, deadline_at, days=days, hours=hours, pace=pace)
    now_ts = engine.now()
    created = []
    # «уже есть»: если пользователь не указал значение — определяем актуальное сами
    # и фиксируем базлайн, относительно которого будет считаться прирост.
    target_url = (pv.get("target") or {}).get("url") or ""
    for item in pv["items"]:
        if item.get("have_before") is None:
            fixed = None
            try:
                from core import vkstats
                fixed = vkstats.count_for(target_url, item["metric"])
            except Exception:
                fixed = None
            item["have_before"] = int(fixed) if fixed is not None else 0
            item["baseline_source"] = "auto" if fixed is not None else "unavailable"
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO goals(target_id, title, deadline_at, pace, status, created_at, note)"
            " VALUES(?,?,?,?,'planned',?,?)",
            (int(target_id), (title or pv["target"]["name"])[:120], pv["deadline_at"], pace,
             now_ts, (pv.get("headline") or "")[:200]))
        goal_id = cur.lastrowid
        svcs = engine.services_map()
        for item in pv["items"]:
            conn.execute("INSERT INTO goal_items(goal_id, metric, need, have_before, planned, done, status)"
                         " VALUES(?,?,?,?,?,0,?)",
                         (goal_id, item["metric"], item["need"], item["have_before"],
                          item["planned"], "done" if item["left"] <= 0 else "open"))
        for slot in pv["schedule"]:
            svc = svcs.get(slot["service_id"]) or {}
            metric = svc.get("stype") or svc.get("type") or slot.get("metric")
            status = "ready" if slot["at"] <= now_ts + 2 and autostart else "queued"
            cur = conn.execute(
                "INSERT INTO tasks(service_id,target_id,status,planned_units,created_at,due_at,goal_id,metric)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (slot["service_id"], int(target_id), status, int(slot["units"]), now_ts,
                 int(slot["at"]), goal_id, metric))
            created.append({"id": cur.lastrowid, "service_id": slot["service_id"],
                            "site": slot["site_name"], "units": slot["units"], "due_at": slot["at"],
                            "metric": metric, "status": status})
    db.log("info", f"Цель #{goal_id}: {pv.get('headline') or 'создана'}")
    return {"ok": True, "goal_id": goal_id, "created": created, "preview": pv}


def _goal_row(goal_id: int):
    with db.rd() as conn:
        g = conn.execute("SELECT g.*, t.name AS target_name, t.url AS target_url, t.platform, t.kind"
                         " FROM goals g JOIN targets t ON t.id=g.target_id WHERE g.id=? AND g.status!='deleted'",
                         (int(goal_id),)).fetchone()
    if not g:
        raise ValueError("Цель не найдена")
    return dict(g)


def progress(goal_id: int) -> dict:
    """Прогресс цели: сколько сделано, сколько в очереди, успеваем ли, что мешает."""
    g = _goal_row(goal_id)
    with db.rd() as conn:
        items = [dict(r) for r in conn.execute(
            "SELECT * FROM goal_items WHERE goal_id=? ORDER BY id", (goal_id,)).fetchall()]
        agg = {r["metric"]: dict(r) for r in conn.execute("""
            SELECT COALESCE(t.metric, v.stype) AS metric,
                   SUM(CASE WHEN t.status='done' THEN COALESCE(t.claimed_units, t.planned_units) ELSE 0 END) AS done_units,
                   SUM(CASE WHEN t.status IN ('queued','ready','running','wait_confirm','captcha','waiting_delivery','delivery_uncertain')
                            THEN t.planned_units ELSE 0 END) AS open_units,
                   SUM(CASE WHEN t.status IN ('queued','ready','running','wait_confirm','captcha','waiting_delivery','delivery_uncertain') THEN 1 ELSE 0 END) AS open_n,
                   MAX(CASE WHEN t.status IN ('queued','ready','running','wait_confirm','captcha','waiting_delivery','delivery_uncertain') THEN t.due_at END) AS last_due,
                   MIN(CASE WHEN t.status IN ('queued','ready','running','wait_confirm','captcha','waiting_delivery','delivery_uncertain') THEN t.due_at END) AS next_due
            FROM tasks t JOIN services v ON v.id=t.service_id
            WHERE t.goal_id=? GROUP BY COALESCE(t.metric, v.stype)""", (goal_id,)).fetchall()}
        busy_sites = [dict(r) for r in conn.execute("""
            SELECT DISTINCT s.id, s.name, p.degraded, p.degraded_note, p.captcha
            FROM tasks t JOIN services v ON v.id=t.service_id JOIN sites s ON s.id=v.site_id
            LEFT JOIN site_profiles p ON p.site_id=s.id
            WHERE t.goal_id=? AND t.status IN ('queued','ready','running')""", (goal_id,)).fetchall()]
    mode = engine.captcha_mode()
    out, blockers, notes, all_done = [], [], [], True
    for it in items:
        a = agg.get(it["metric"], {})
        done = int(a.get("done_units") or 0)
        done_total = min(it["need"], done)
        open_units = int(a.get("open_units") or 0)
        left = max(0, it["need"] - done_total)
        all_done = all_done and left == 0
        out.append({"metric": it["metric"], "title": METRICS.get(it["metric"], (it["metric"],))[0],
                    "need": it["need"], "have_before": it["have_before"], "done": done_total,
                    "left": left, "open_units": open_units,
                    "percent": int(round(100 * done_total / it["need"])) if it["need"] else 100,
                    "next_at": a.get("next_due"), "last_at": a.get("last_due")})
    for s in busy_sites:
        if s.get("degraded"):
            blockers.append(f"{s['name']}: форма изменилась ({s.get('degraded_note') or 'нужна проверка'})")
        elif s.get("captcha"):
            try:
                caps_list = json.loads(s["captcha"]) if isinstance(s["captcha"], str) else list(s["captcha"])
            except Exception:
                caps_list = []
            if not caps_list:
                continue                     # капчи не знаем — не пугаем зря
            caps = ", ".join(caps_list)
            if mode == "nocaptcha":
                blockers.append(f"{s['name']}: капча ({caps}) — в режиме «Без капчи» заход пропустится")
            elif mode == "captcha":
                notes.append(f"{s['name']}: капча ({caps}) — при заходе откроется окно")
            else:
                notes.append(f"{s['name']}: капча ({caps}) — решается в браузере при заходе")
    status = g["status"]
    if all_done and status in ("planned", "running", "paused"):
        status = "done"
    # цель уже «идёт», если есть сделанные или стоящие в очереди заходы — так честнее в интерфейсе
    elif status == "planned" and (any(i["open_units"] for i in out) or any(i["done"] for i in out)):
        status = "running"
    eta = max([i["last_at"] or 0 for i in out]) or None
    behind = bool(eta and eta > g["deadline_at"] and not all_done)
    headline = ("Готово: " + ", ".join(f"{metric_word(i['done'], i['metric'])}" for i in out)) if all_done else \
        "Осталось " + ", ".join(f"{metric_word(i['left'], i['metric'])}" for i in out if i["left"]) + \
        (f" · успеваю к {engine.fmt(g['deadline_at'])}" if not behind else
         f" · не успеваю к {engine.fmt(g['deadline_at'])}")
    if not all_done and db.get_setting('full_auto', '0') == '1':
        headline = "Осталось " + ", ".join(metric_word(i['left'], i['metric']) for i in out if i['left']) + " · сроки исполнения площадок неизвестны"
    return {"ok": True, "id": g["id"], "title": g["title"], "status": status, "pace": g["pace"],
            "target": {"id": g["target_id"], "name": g["target_name"], "url": g["target_url"]},
            "deadline_at": g["deadline_at"], "created_at": g["created_at"],
            "items": out, "eta": eta, "behind": behind, "blockers": blockers[:6],
            "notes": notes[:5], "captcha_mode": mode,
            "headline": headline, "percent": min(100, int(round(sum(i["percent"] for i in out) / max(1, len(out))))),
            "tasks_open": sum(1 for i in out if i["open_units"])}


def list_goals() -> list[dict]:
    with db.rd() as conn:
        rows = conn.execute("SELECT id FROM goals WHERE status != 'deleted' ORDER BY"
                            " CASE status WHEN 'running' THEN 0 WHEN 'planned' THEN 1"
                            " WHEN 'paused' THEN 2 ELSE 3 END, id DESC LIMIT 30").fetchall()
    out = []
    for r in rows:
        try:
            out.append(progress(r["id"]))
        except Exception:
            continue
    return out


def pause(goal_id: int) -> dict:
    g = _goal_row(goal_id)
    if g["status"] in ("done",):
        raise ValueError("Цель уже выполнена")
    with db.tx() as conn:
        conn.execute("UPDATE goals SET status='paused' WHERE id=?", (goal_id,))
        moved = conn.execute("UPDATE tasks SET status='queued' WHERE goal_id=? AND status='ready'",
                             (goal_id,)).rowcount
    db.log("info", f"Цель #{goal_id} на паузе (снято с выполнения: {moved})")
    return {"ok": True, "paused": True, "moved": moved}


def resume(goal_id: int) -> dict:
    g = _goal_row(goal_id)
    if g["status"] == "done":
        raise ValueError("Цель уже выполнена")
    with db.tx() as conn:
        conn.execute("UPDATE goals SET status='planned' WHERE id=?", (goal_id,))
    db.log("info", f"Цель #{goal_id} снова в работе")
    return {"ok": True, "resumed": True}


def extend(goal_id: int, days: float) -> dict:
    g = _goal_row(goal_id)
    new_deadline = int(g["deadline_at"] + float(days) * 86400)
    if float(days) < 0:
        raise ValueError("Продлить можно только вперёд")
    with db.tx() as conn:
        conn.execute("UPDATE goals SET deadline_at=?, status='planned' WHERE id=?",
                     (new_deadline, goal_id))
    res = replan(goal_id)
    db.log("info", f"Цель #{goal_id}: срок продлён на {days} дн. до {engine.fmt(new_deadline)}")
    return {"ok": True, "deadline_at": new_deadline, **res}


def replan(goal_id: int) -> dict:
    """Пересчитать незакрытые заходы под текущую реальность (без дублей)."""
    g = _goal_row(goal_id)
    now_ts = engine.now()
    with db.tx() as conn:
        conn.execute("DELETE FROM deliveries WHERE task_id IN (SELECT id FROM tasks WHERE goal_id=? AND status IN ('queued','ready')) "
                     "AND state NOT IN ('reserved','submitting','waiting','uncertain')", (goal_id,))
        removed = conn.execute("DELETE FROM tasks WHERE goal_id=? AND status IN ('queued','ready') "
                               "AND id NOT IN (SELECT task_id FROM deliveries)", (goal_id,)).rowcount
    prog = progress(goal_id)
    items = [{"metric": i["metric"], "need": max(0, i["left"] - i["open_units"]),
              "have_before": 0} for i in prog["items"] if i["left"] > i["open_units"]]
    if not items:
        if all(i['left'] == 0 for i in prog['items']):
            with db.tx() as conn:
                conn.execute("UPDATE goals SET status='done', closed_at=? WHERE id=?", (now_ts, goal_id))
            return {"ok": True, "removed": removed, "created": [], "done": True}
        return {"ok": True, "removed": removed, "created": [], "done": False, "waiting": True}
    pv = preview(g["target_id"], items, g["deadline_at"], pace=g["pace"])
    created = []
    svcs = engine.services_map()
    with db.tx() as conn:
        for slot in pv["schedule"]:
            svc = svcs.get(slot["service_id"]) or {}
            status = "ready" if slot["at"] <= now_ts + 2 else "queued"
            cur = conn.execute(
                "INSERT INTO tasks(service_id,target_id,status,planned_units,created_at,due_at,goal_id,metric)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (slot["service_id"], g["target_id"], status, int(slot["units"]), now_ts,
                 int(slot["at"]), goal_id, svc.get("stype") or slot.get("metric")))
            created.append({"id": cur.lastrowid, "due_at": slot["at"], "units": slot["units"]})
        conn.execute("UPDATE goals SET status='planned' WHERE id=?", (goal_id,))
    db.log("info", f"Цель #{goal_id} пересчитана: снято {removed}, поставлено {len(created)} заходов")
    return {"ok": True, "removed": removed, "created": created, "preview": pv}


def remove(goal_id: int) -> dict:
    _goal_row(goal_id)
    with db.tx() as conn:
        if conn.execute("SELECT 1 FROM deliveries d JOIN tasks t ON t.id=d.task_id WHERE t.goal_id=? "
                        "AND d.state IN ('reserved','submitting','waiting','uncertain')", (goal_id,)).fetchone():
            raise ValueError("У цели есть незавершённый заказ; сначала проверьте его")
        conn.execute("DELETE FROM deliveries WHERE task_id IN (SELECT id FROM tasks WHERE goal_id=? AND status<>'done')", (goal_id,))
        n = conn.execute("DELETE FROM tasks WHERE goal_id=? AND status NOT IN ('done')", (goal_id,)).rowcount
        conn.execute("UPDATE goals SET status='deleted', closed_at=? WHERE id=?", (engine.now(), goal_id))
    db.log("info", f"Цель #{goal_id} удалена (снято заходов: {n})")
    return {"ok": True, "removed_tasks": n}


# ------------------------------------------------------------------ связь с задачами

def on_task_done(task_id: int) -> None:
    """Вызывается после закрытия захода: обновляем счётчики цели и, если нужно, пересчитываем."""
    with db.rd() as conn:
        row = conn.execute("SELECT goal_id, metric, status, claimed_units, planned_units FROM tasks WHERE id=?",
                           (task_id,)).fetchone()
    if not row or not row["goal_id"]:
        return
    goal_id = int(row["goal_id"])
    prog = progress(goal_id)
    with db.tx() as conn:
        for item in prog["items"]:
            conn.execute("UPDATE goal_items SET done=?, planned=? WHERE goal_id=? AND metric=?",
                         (item["done"] - 0, item["open_units"], goal_id, item["metric"]))
    if prog["status"] == "done":
        with db.tx() as conn:
            conn.execute("UPDATE goals SET status='done', closed_at=? WHERE id=?", (engine.now(), goal_id))
            conn.execute("UPDATE tasks SET status='skipped', note='цель достигнута по счётчику' "
                         "WHERE goal_id=? AND status IN ('queued','ready')", (goal_id,))
        db.log("info", f"Цель #{goal_id} выполнена: {prog['headline']}")
        return
    # Пауза не отменяет доставку, но подтверждение не должно возобновлять цель.
    if prog['status'] == 'paused':
        return
    # если по какой-то метрике в очереди не осталось заходов, а цель не закрыта — добираем
    short = [i for i in prog["items"] if i["left"] > 0 and i["open_units"] <= 0]
    if short:
        db.log("warn", f"Цель #{goal_id}: не хватает заходов на "
                       + ", ".join(metric_word(i["left"], i["metric"]) for i in short) + " — пересчитываю")
        try:
            replan(goal_id)
        except Exception as exc:
            db.log("error", f"Цель #{goal_id}: не удалось пересчитать ({exc})")
