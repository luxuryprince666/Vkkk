# -*- coding: utf-8 -*-
"""Движок лимитов и планировщик задач.

Ключевые правила (все спорные места решены консервативно — «лучше недокрутить»):
  * сутки считаются по ЛОКАЛЬНОЙ полуночи, а не по UTC;
  * кулдаун отсчитывается от момента ЗАВЕРШЕНИЯ захода, а не от запуска;
  * для сайтов из SHARED_SLOT_SITES кулдаун общий на весь сайт (один слот — одна услуга);
  * если у услуги не указан max_claims_day, суточный лимит не ограничивается,
    но работает глобальный предохранитель daily_total_limit;
  * рабочее окно (work_hours) и минимальный гэп между заходами учитываются при планировании.
"""
from __future__ import annotations

import random
import time
from datetime import datetime, timedelta
from typing import Iterable

from . import db

# ------------------------------------------------------------------ прокси-ротация (бесплатно)
# Бесплатные источники чистых IP: публичные прокси (proxybroker2) и Tor (stem).
# Публичные прокси через браузер часто не тянут, но для прямых HTTP-запросов
# (prtut, pricesmm, doctorsmm API) могут подойти. Tor даёт новый IP на каждую цепочку.


def _have(module: str) -> bool:
    try:
        __import__(module)
        return True
    except Exception:
        return False


_PROXY_POOL: list[str] = []
_PROXY_INDEX = 0


def proxy_pool_settings() -> dict:
    """Настройки прокси / VLESS / Amnezia из БД."""
    raw = db.get_setting("proxy_list", "") or ""
    return {
        "enabled": (db.get_setting("proxy_rotation", "0") or "0") == "1",
        "manual_raw": raw,
        "manual": [x.strip() for x in raw.replace("\n", ",").split(",") if x.strip()],
        "subscription_url": (db.get_setting("proxy_subscription_url", "") or "").strip(),
        "autodetect_local": (db.get_setting("proxy_autodetect_local", "1") or "1") == "1",
        "use_tor": (db.get_setting("proxy_use_tor", "0") or "0") == "1",
    }


def refresh_proxy_pool() -> int:
    """Обновляет пул маршрутов (пользовательские HTTP/SOCKS5/VLESS, подписка, локальный Amnezia/Tor)."""
    global _PROXY_POOL, _PROXY_INDEX
    from core import network

    ps = proxy_pool_settings()
    eps = network.collect_endpoints(
        manual_raw=ps["manual_raw"],
        autodetect_local=ps["autodetect_local"],
        include_direct=False,
        include_tor=ps['use_tor'],
    )
    if ps["subscription_url"]:
        sub_eps, sub_note = network.fetch_subscription(ps["subscription_url"])
        if sub_note:
            db.log("info", sub_note)
        eps.extend([e for e in sub_eps if e.ready and e.proxy_url])
    found = [e.proxy_url for e in eps if e.ready and e.proxy_url]
    _PROXY_POOL = list(dict.fromkeys(found))
    _PROXY_INDEX = 0
    db.log("info", f"Прокси/VLESS пул: {len(_PROXY_POOL)} активных маршрутов")
    return len(_PROXY_POOL)


def next_proxy(site_id: str | None = None) -> str | None:
    """Возвращает следующий рабочий прокси из пула (с учётом блокировок площадки) или None."""
    global _PROXY_INDEX
    if site_id:
        from core import network
        ps = proxy_pool_settings()
        eps = [e for e in network.endpoints_for_site(
            site_id, manual_raw=ps["manual_raw"], autodetect_local=ps["autodetect_local"], prefer_proxy=True,
            include_tor=ps['use_tor']
        ) if e.proxy_url]
        if eps:
            return eps[0].proxy_url
    if not _PROXY_POOL:
        refresh_proxy_pool()
    if not _PROXY_POOL:
        return None
    proxy = _PROXY_POOL[_PROXY_INDEX % len(_PROXY_POOL)]
    _PROXY_INDEX += 1
    return proxy


# --------------------------------------------------------------------- время
def now() -> int:
    return int(time.time())


def day_start(ts: int | None = None) -> int:
    ts = ts or now()
    d = datetime.fromtimestamp(ts)
    return int(d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def fmt(ts: int | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")


def human_left(sec: int) -> str:
    sec = max(0, int(sec))
    if sec < 60:
        return f"{sec} сек"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m} мин {s:02d} с"
    h, m = divmod(m, 60)
    return f"{h} ч {m:02d} мин"


def in_work_hours(ts: int | None = None) -> bool:
    """Проверка рабочего окна вида '8-23' (по местному времени)."""
    spec = db.get_setting("work_hours", "8-23")
    try:
        a, b = [int(x) for x in spec.split("-")]
    except Exception:
        return True
    h = datetime.fromtimestamp(ts or now()).hour
    if a <= b:
        return a <= h < b
    return h >= a or h < b  # окно через полночь


def next_work_hour(ts: int | None = None) -> int:
    spec = db.get_setting("work_hours", "8-23")
    try:
        a = int(spec.split("-")[0])
    except Exception:
        return now()
    d = datetime.fromtimestamp(ts or now())
    target = d.replace(hour=a, minute=0, second=0, microsecond=0)
    if target <= d:
        target += timedelta(days=1)
    return int(target.timestamp())


# ------------------------------------------------------------------ выборки
CAPTCHA_MODES = {
    "all": "Все сайты",
    "nocaptcha": "Без капчи",
    "captcha": "С капчей",
}
CAPTCHA_LABELS = {-1: "не проверено", 0: "нет капчи", 1: "есть капча"}


def captcha_mode() -> str:
    mode = db.get_setting("captcha_mode", "all") or "all"
    return mode if mode in CAPTCHA_MODES else "all"


def apply_overrides(row: dict) -> dict:
    """Накладывает ручные значения на каталожные. Каталожные остаются рядом — для отката."""
    d = dict(row)
    for field, ocol in (("cooldown_min", "cooldown_override"),
                        ("amount", "amount_override"),
                        ("max_claims_day", "max_claims_override")):
        catalog = d.get(field)
        manual = d.get(ocol)
        d[field + "_catalog"] = catalog
        d[field + "_manual"] = manual is not None
        if manual is not None:
            d[field] = int(manual)
    cap = d.get("captcha")
    d["captcha"] = int(cap) if cap is not None else -1
    return d


def _field(row, key: str, default=None):
    """Достаёт поле и из dict, и из sqlite3.Row (у Row нет .get)."""
    try:
        val = row[key]
    except (KeyError, IndexError, TypeError):
        try:
            val = row.get(key, default)          # type: ignore[union-attr]
        except Exception:
            return default
    return default if val is None else val



# ---------------------------------------------------- объект накрутки (что именно крутим)
# Одна и та же «услуга подписчиков» бывает про РАЗНОЕ: вступить в сообщество (группу/паблик)
# или подписаться на личную страницу человека. Это разные вещи, и сервисы их не смешивают,
# поэтому в каталоге у услуги есть subject, а у ссылки — kind, и мы их сверяем.
SUBJECTS = {
    "community": "Сообщество / группа / паблик",
    "person": "Личная страница человека",
    "post": "Запись, фото, видео, трек",
    "any": "Сервис не разделяет — принимает любой объект",
}
SUBJECT_SHORT = {
    "community": "сообщество",
    "person": "личная страница",
    "page": "личная страница",
    "post": "запись",
    "any": "любой объект",
}
TARGET_KINDS = ("community", "person", "post")   # что можно указать у ссылки


SUBJECT_SYNONYMS = {"page": "person"}   # «страница» и «личная страница» — одно и то же


def subject_fits(subject: str | None, kind: str | None) -> bool:
    """Подходит ли услуга к объекту ссылки. Неизвестность трактуется мягко (пропускаем)."""
    subject = (subject or "any").strip() or "any"
    kind = (kind or "").strip()
    if subject in ("", "any") or kind in ("", "any"):
        return True
    subject = SUBJECT_SYNONYMS.get(subject, subject)
    kind = SUBJECT_SYNONYMS.get(kind, kind)
    return subject == kind


def detect_target_kind(platform: str, url: str) -> str:
    """Пытается понять по ссылке, что это: сообщество, личная страница или запись.

    Возвращает '' если однозначно сказать нельзя (например, короткое имя vk.com/ivan —
    это может быть и группа, и человек: такие случаи решает пользователь).
    """
    try:
        from urllib.parse import urlparse, parse_qs
        u = urlparse((url or "").strip())
        host = u.netloc.lower().split(":")[0]
        path = u.path.strip("/").lower()
        query = parse_qs(u.query or "")
    except Exception:
        return ""

    if platform == "vk" or host.endswith(("vk.com", "vk.ru")):
        q = " ".join(f"{k}={v[0]}" for k, v in query.items()).lower()
        seg = path.split("/")[0] if path else ""
        # ссылки на контент: стена, фото, видео, клип, аудио, документ, отметки «на стене»
        if ("wall" in seg or "photo" in seg or "video" in seg or "clip" in seg
                or "audio" in seg or "doc" in seg or "story" in seg):
            return "post"
        if any(m in q for m in ("w=wall", "z=video", "z=photo", "w=photo", "reply=")):
            return "post"
        if seg.startswith(("club", "public", "event", "group")):
            return "community"
        if seg.startswith("id") and seg[2:].isdigit():
            return "person"
        if seg in ("away", "feed", "im", "friends", "audio", "video", "photos", "board", "market", "app"):
            return ""
        if seg and all(c.isalpha() or c.isdigit() or c in "._-" for c in seg):
            # короткое имя — может быть и сообществом, и человеком: не угадываем
            return ""
        return ""

    if platform == "telegram":
        return "community" if path else ""

    return ""


def target_kind(tg) -> tuple[str, bool]:
    """(тип объекта, определён ли автоматически). Явно указанный пользователем важнее."""
    kind = str(_field(tg, "kind", "") or "").strip()
    if kind in TARGET_KINDS:
        return kind, False
    auto = detect_target_kind(str(_field(tg, "platform", "") or ""), str(_field(tg, "url", "") or ""))
    return auto, bool(auto)



def captcha_allowed(svc: dict, mode: str | None = None) -> bool:
    """Входит ли услуга в выбранный режим.

    «Без капчи» — оптимистично: сайт, который ещё не проверяли, считается безопасным,
    пока робот не встретит на нём защиту — тогда услуга запоминается как «с капчей»
    и навсегда выпадает из этого режима. Так режим работает без участия человека
    с первого дня и сам себя уточняет.
    """
    mode = mode or captcha_mode()
    cap = _field(svc, "captcha", -1)
    if mode == "nocaptcha":
        return cap != 1
    if mode == "captcha":
        return cap == 1
    return True


def services_map() -> dict[str, dict]:
    with db.rd() as conn:
        rows = conn.execute("""
            SELECT v.*, s.name AS site_name, s.enabled AS site_enabled, s.breaker_until, s.mode AS site_mode,
                   s.reg_required, s.verified, s.notes AS site_notes, s.free_page, s.home
            FROM services v JOIN sites s ON s.id = v.site_id
        """).fetchall()
    return {r["id"]: apply_overrides(dict(r)) for r in rows}


def claims_done_today(conn, service_ids: Iterable[str], since: int) -> dict[str, dict]:
    """Сколько заходов и единиц уже выполнено сегодня по каждой услуге."""
    ids = list(service_ids)
    out = {i: {"claims": 0, "units": 0, "last_finished": 0} for i in ids}
    if not ids:
        return out
    q = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""SELECT service_id, COUNT(*) AS c, COALESCE(SUM(claimed_units),0) AS u, MAX(finished_at) AS last
            FROM tasks WHERE service_id IN ({q}) AND status='done' AND finished_at>=?
            GROUP BY service_id""",
        (*ids, since),
    ).fetchall()
    for r in rows:
        out[r["service_id"]] = {"claims": r["c"], "units": r["u"], "last_finished": r["last"] or 0}
    return out


def last_claim_any(conn, site_id: str | None = None) -> int:
    """Время последнего завершённого захода (для общего кулдауна сайта и глобального гэпа)."""
    if site_id:
        row = conn.execute("""
            SELECT MAX(t.finished_at) AS last FROM tasks t
            JOIN services v ON v.id = t.service_id
            WHERE t.status='done' AND v.site_id=?""", (site_id,)).fetchone()
    else:
        row = conn.execute("SELECT MAX(finished_at) AS last FROM tasks WHERE status='done'").fetchone()
    return (row["last"] or 0) if row else 0


def total_claims_today(conn, since: int) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM tasks WHERE status='done' AND finished_at>=?", (since,)).fetchone()
    return row["c"] or 0


# --------------------------------------------------------------- готовность
def build_service_view(svc: dict, stat: dict, last_site: int, open_tasks: dict[str, int],
                       global_ok: bool, now_ts: int, mode: str | None = None) -> dict:
    """Считает всё, что нужно показать в интерфейсе по одной услуге."""
    mode = mode or captcha_mode()
    cooldown = int(svc["cooldown_min"]) * 60
    shared = svc["site_id"] in db.SHARED_SLOT_SITES
    last = last_site if shared else stat["last_finished"]
    next_at = (last + cooldown) if last else now_ts
    claims = stat["claims"]
    max_claims = svc["max_claims_day"]
    left_today = None if max_claims is None else max(0, int(max_claims) - claims)
    mode_ok = captcha_allowed(svc, mode)
    from core import profiles as _profiles
    prof = _profiles.all_profiles().get(svc["site_id"]) or {}
    site_broken = bool(prof.get("degraded")) or prof.get("automation") == "-"
    site_note = (prof.get("degraded_note") or "") if site_broken else ""
    ready = (
        bool(svc["enabled"]) and bool(svc["site_enabled"]) and global_ok and mode_ok and not site_broken
        and now_ts >= next_at and (left_today is None or left_today > 0)
    )
    if left_today is None:
        # без заявленного лимита: показываем теоретический максимум за сутки по кулдауну
        per_day = int(86400 / cooldown) if cooldown > 0 else 1
    else:
        per_day = min(int(max_claims), int(86400 / cooldown) if cooldown > 0 else int(max_claims))
    return {
        "id": svc["id"],
        "site_id": svc["site_id"],
        "site_name": svc["site_name"],
        "platform": svc["platform"],
        "type": svc["stype"],
        "subject": (svc.get("subject") or "any"),
        "subject_label": SUBJECTS.get(svc.get("subject") or "any", SUBJECTS["any"]),
        "subject_short": SUBJECT_SHORT.get(svc.get("subject") or "any", SUBJECT_SHORT["any"]),
        "label": svc["label"],
        "amount": int(svc["amount"]),
        "cooldown_min": int(svc["cooldown_min"]),
        "max_claims_day": max_claims,
        # ручные значения: что стоит сейчас, что в каталоге и правилось ли вручную
        "cooldown_catalog": int(svc.get("cooldown_min_catalog") or 0),
        "amount_catalog": int(svc.get("amount_catalog") or 0),
        "max_claims_catalog": svc.get("max_claims_day_catalog"),
        "cooldown_manual": bool(svc.get("cooldown_min_manual")),
        "amount_manual": bool(svc.get("amount_manual")),
        "max_claims_manual": bool(svc.get("max_claims_day_manual")),
        "captcha": int(svc.get("captcha", -1)),
        "captcha_label": CAPTCHA_LABELS.get(int(svc.get("captcha", -1)), "не проверено"),
        "mode_ok": mode_ok,
        "hold_seconds": int(svc["hold_seconds"]),
        "url": svc["url"],
        "confirmed": bool(svc["confirmed"]),
        "reg_required": bool(svc["reg_required"]),
        "verified": svc["verified"],
        "notes": svc["site_notes"],
        "mode": svc["site_mode"],
        "automation": prof.get("automation") or "",
        "site_broken": site_broken,
        "site_note": site_note,
        "shared_slot": shared,
        "enabled": bool(svc["enabled"]),
        "site_enabled": bool(svc["site_enabled"]),
        "breaker_until": int(svc["breaker_until"] or 0),
        "claims_today": claims,
        "units_today": stat["units"],
        "claims_left_today": left_today,
        "per_day_theoretical": per_day,
        "next_at": next_at,
        "seconds_left": max(0, next_at - now_ts),
        "ready": ready,
        "open_tasks": open_tasks.get(svc["id"], 0),
        "daily_cap_units": (left_today * int(svc["amount"])) if left_today is not None else per_day * int(svc["amount"]),
    }


def state() -> dict:
    """Полное состояние для интерфейса: сайты, услуги, задачи, статистика."""
    now_ts = now()
    since = day_start(now_ts)
    with db.rd() as conn:
        sites = [dict(r) for r in conn.execute("SELECT * FROM sites ORDER BY name").fetchall()]
        svcs = {r["id"]: apply_overrides(dict(r)) for r in conn.execute("""
            SELECT v.*, s.name AS site_name, s.enabled AS site_enabled, s.breaker_until, s.mode AS site_mode,
                   s.reg_required, s.verified, s.notes AS site_notes
            FROM services v JOIN sites s ON s.id=v.site_id""").fetchall()}
        stats = claims_done_today(conn, svcs.keys(), since)
        open_tasks_rows = conn.execute(
            "SELECT service_id, COUNT(*) AS c FROM tasks WHERE status IN ('queued','ready','running','wait_confirm','captcha','waiting_delivery','delivery_uncertain')"
            " GROUP BY service_id").fetchall()
        open_tasks = {r["service_id"]: r["c"] for r in open_tasks_rows}
        last_by_site = {r["site_id"]: (r["last"] or 0) for r in conn.execute("""
            SELECT v.site_id, MAX(t.finished_at) AS last FROM tasks t
            JOIN services v ON v.id=t.service_id WHERE t.status='done' GROUP BY v.site_id""").fetchall()}
        global_last = last_claim_any(conn)
        total_today = total_claims_today(conn, since)
        targets = []
        for r in conn.execute("SELECT * FROM targets ORDER BY id DESC").fetchall():
            t = dict(r)
            k, auto = target_kind(r)
            t["kind"] = k
            t["kind_auto"] = auto
            t["kind_label"] = SUBJECTS.get(k, "не определён")
            t["kind_short"] = SUBJECT_SHORT.get(k, "не определён")
            targets.append(t)
        tasks = []
        for r in conn.execute("""
            SELECT t.*, v.label, v.stype, v.platform, v.amount, v.hold_seconds, v.url AS service_url,
                   s.name AS site_name, s.mode AS site_mode, tg.name AS target_name, tg.url AS target_url,
                   tg.kind AS target_kind_declared, v.subject
            FROM tasks t JOIN services v ON v.id=t.service_id JOIN sites s ON s.id=v.site_id
            LEFT JOIN targets tg ON tg.id=t.target_id
            WHERE t.status IN ('queued','ready','running','wait_confirm','captcha','waiting_delivery','delivery_uncertain','failed','submitted_unverified')
            ORDER BY COALESCE(t.due_at, t.created_at) LIMIT 300""").fetchall():
            t = dict(r)
            k = (t.get("target_kind_declared") or "").strip()
            if k not in TARGET_KINDS:
                k = detect_target_kind(t.get("platform") or "", t.get("target_url") or "")
            t["target_kind"] = k
            t["target_kind_short"] = SUBJECT_SHORT.get(k, "не определён")
            t["subject_label"] = SUBJECTS.get(t.get("subject") or "any", SUBJECTS["any"])
            tasks.append(t)

    gap = int(db.get_setting("global_min_gap_sec", "20") or 0)
    gap_left = max(0, global_last + gap - now_ts)
    daily_limit = int(db.get_setting("daily_total_limit", "0") or 0)
    limit_ok = (daily_limit <= 0) or (total_today < daily_limit)
    work_ok = in_work_hours(now_ts)
    global_ok = limit_ok and work_ok and gap_left == 0

    mode = captcha_mode()
    items = []
    for svc in svcs.values():
        view = build_service_view(svc, stats[svc["id"]], last_by_site.get(svc["site_id"], 0),
                                  open_tasks, global_ok, now_ts, mode)
        items.append(view)
    items.sort(key=lambda x: (x["site_name"].lower(), x["platform"], x["type"], x["amount"]))

    # ---- аналитика «сколько можно взять сегодня»
    by_type: dict[tuple[str, str], dict] = {}
    for it in items:
        key = (it["platform"], it["type"])
        b = by_type.setdefault(key, {"platform": it["platform"], "type": it["type"],
                                     "done_units": 0, "left_units": 0, "sites": 0})
        if it["site_enabled"]:
            b["done_units"] += it["units_today"]
            b["left_units"] += max(0, it["daily_cap_units"])
            b["sites"] += 1

    # «Подписчики» делятся на сообщества и личные страницы — считаем их отдельно,
    # причём универсальные услуги (subject=any) входят в обе строки: они годятся и туда, и туда.
    by_subject: list[dict] = []
    groups: dict[tuple[str, str], list[dict]] = {}
    for it in items:
        groups.setdefault((it["platform"], it["type"]), []).append(it)
    for (platform, stype), group in groups.items():
        subjects = {g["subject"] for g in group}
        if subjects & {"community", "person"}:
            for kind in ("community", "person"):
                bucket = [g for g in group if subject_fits(g["subject"], kind)]
                if bucket:
                    by_subject.append({
                        "platform": platform, "type": stype, "subject": kind,
                        "label": _types().get(stype, stype) + " · " + SUBJECT_SHORT[kind],
                        "done_units": sum(g["units_today"] for g in bucket if g["site_enabled"]),
                        "left_units": sum(max(0, g["daily_cap_units"]) for g in bucket if g["site_enabled"]),
                        "sites": sum(1 for g in bucket if g["site_enabled"]),
                    })
        else:
            enabled = [g for g in group if g["site_enabled"]]
            # если все услуги группы рассчитаны на один и тот же объект — так и пишем (например, «Лайки · запись»)
            same = {g["subject"] for g in group}
            uniform = same.pop() if len(same) == 1 else ""
            by_subject.append({
                "platform": platform, "type": stype,
                "subject": "" if uniform in ("", "any") else uniform,
                "label": _types().get(stype, stype),
                "done_units": sum(g["units_today"] for g in enabled),
                "left_units": sum(max(0, g["daily_cap_units"]) for g in enabled),
                "sites": len(enabled),
            })

    captcha_stat = {"mode": mode, "mode_label": CAPTCHA_MODES[mode],
                    "none": 0, "yes": 0, "unknown": 0, "allowed": 0}
    for it in items:
        key = {0: "none", 1: "yes"}.get(it["captcha"], "unknown")
        captcha_stat[key] += 1
        if it["mode_ok"]:
            captcha_stat["allowed"] += 1

    return {
        "now": now_ts,
        "server_time": fmt(now_ts),
        "day_start": since,
        "sites": [
            {**s, "platforms": s["platforms"], "breaker_active": (s["breaker_until"] or 0) > now_ts,
             "services_count": sum(1 for i in items if i["site_id"] == s["id"])}
            for s in sites
        ],
        "services": items,
        "targets": targets,
        "tasks": tasks,
        "stats": {
            "total_claims_today": total_today,
            "daily_total_limit": daily_limit,
            "gap_left": gap_left,
            "in_work_hours": work_ok,
            "global_ok": global_ok,
            "by_type": sorted(by_type.values(), key=lambda x: -x["left_units"]),
            "by_subject": sorted(by_subject, key=lambda x: -x["left_units"]),
            "subjects": SUBJECTS,
            "subject_short": SUBJECT_SHORT,
            "captcha": captcha_stat,
            "types": _types(),
            "platforms": _platforms(),
        },
        "settings": db.get_settings(),
        "health": health(),
    }



# --------------------------------------------- «что можно накрутить на эту ссылку»
# Слова для человеческого текста: «+10 подписчиков», а не «10 units».
UNIT_WORDS = {
    "followers": ("подписчик", "подписчика", "подписчиков"),
    "friends": ("друг", "друга", "друзей"),
    "likes": ("лайк", "лайка", "лайков"),
    "views": ("просмотр", "просмотра", "просмотров"),
    "plays": ("прослушивание", "прослушивания", "прослушиваний"),
    "reposts": ("репост", "репоста", "репостов"),
    "comments": ("комментарий", "комментария", "комментариев"),
    "reactions": ("реакция", "реакции", "реакций"),
    "traffic": ("переход", "перехода", "переходов"),
}
# Заголовки групп: «Подписчики в сообщество» — понятно без знания терминов.
GROUP_TITLES = {
    ("followers", "community"): "Подписчики в сообщество",
    ("followers", "person"): "Подписчики на личную страницу",
    ("followers", "any"): "Подписчики (сайт не уточняет объект)",
    ("followers", ""): "Подписчики",
    ("friends", "person"): "Друзья на личную страницу",
    ("likes", "post"): "Лайки на запись",
    ("views", "post"): "Просмотры записи или видео",
    ("plays", "post"): "Прослушивания",
    ("reposts", "post"): "Репосты",
    ("comments", "post"): "Комментарии",
}


def plural_ru(n: int, forms: tuple[str, str, str]) -> str:
    """1 подписчик, 2 подписчика, 5 подписчиков."""
    n = abs(int(n))
    a, b = n % 100, n % 10
    if 10 < a < 20:
        return forms[2]
    if 1 < b < 5:
        return forms[1]
    if b == 1:
        return forms[0]
    return forms[2]


def gives_text(svc: dict) -> str:
    """«+10 подписчиков» — то, что человек получит за один заход."""
    amount = int(svc.get("amount") or 0)
    return f"+{amount} {plural_ru(amount, UNIT_WORDS.get(svc.get('type'), ('единица', 'единицы', 'единиц')))}"


def _blocked_reason(it: dict, now_ts: int) -> str:
    """Почему сейчас нельзя нажать «Накрутить» — простыми словами."""
    if not it.get("site_enabled"):
        return "сайт выключен в настройках"
    if not it.get("enabled"):
        return "услуга выключена"
    if int(it.get("breaker_until") or 0) > now_ts:
        return "сайт на паузе после ошибок — вернётся в " + fmt(it["breaker_until"])
    if not it.get("mode_ok"):
        return f"не входит в режим «{CAPTCHA_MODES[captcha_mode()]}»"
    if it.get("claims_left_today") == 0:
        return "на сегодня лимит исчерпан — приходите завтра"
    if not it.get("ready"):
        if it.get("seconds_left"):
            return "окно откроется через " + human_left(it["seconds_left"])
        return "сейчас недоступно"
    return ""


def services_for_target(target_id: int) -> dict:
    """Что можно накрутить на конкретную ссылку прямо сейчас.

    Возвращает услуги, сгруппированные по смыслу («Подписчики в сообщество», «Лайки на запись»),
    отсортированные так, чтобы готовые к запуску были сверху. Неподходящие по объекту
    услуги в список не попадают вовсе — новичку не нужно в этом разбираться.
    """
    now_ts = now()
    st = state()
    target = next((t for t in st["targets"] if int(t["id"]) == int(target_id)), None)
    if target is None:
        raise ValueError("Ссылка не найдена")
    kind = target.get("kind") or ""
    strict = (db.get_setting("strict_subject", "1") or "1") == "1"
    types = _types()

    groups: dict[tuple[str, str], dict] = {}
    for it in st["services"]:
        if it["platform"] != target["platform"]:
            continue
        if strict and not subject_fits(it["subject"], kind):
            continue
        # на ссылку-запись подписчики и друзья не накручиваются — это разные объекты
        if kind == "post" and it["type"] in ("followers", "friends"):
            continue
        subject = it["subject"] if it["subject"] in ("community", "person", "post") else ""
        # для универсальных услуг "сообщества"/"личной страницы" группу выбираем по самой ссылке
        if not subject and it["type"] in ("followers", "friends"):
            subject = kind if kind in ("community", "person") else "any"
        key = (it["type"], subject)
        g = groups.setdefault(key, {
            "key": key[0] + ":" + key[1],
            "title": GROUP_TITLES.get(key) or (types.get(it["type"], it["type"]) +
                                               (" · " + SUBJECT_SHORT[subject] if subject else "")),
            "items": [], "ready": 0,
        })
        item = dict(it)
        item["gives"] = gives_text(it)
        item["blocked"] = _blocked_reason(it, now_ts)
        item["manual"] = it.get("mode") != "auto"      # робот сделает сам или открываем вам
        item["mode_hint"] = "робот сделает сам" if it.get("mode") == "auto" else "откроем страницу — нажмёте сами"
        g["items"].append(item)
        if not item["blocked"]:
            g["ready"] += 1

    order = ["followers", "friends", "likes", "views", "plays", "reposts", "comments",
             "reactions", "traffic"]
    out_groups = sorted(groups.values(),
                        key=lambda g: (order.index(g["key"].split(":")[0]) if g["key"].split(":")[0] in order else 99,
                                       g["key"]))
    for g in out_groups:
        g["items"].sort(key=lambda x: (bool(x["blocked"]), -int(x["amount"])))
    return {
        "target": target,
        "kind": kind,
        "kind_label": SUBJECTS.get(kind, "не определён"),
        "kind_short": SUBJECT_SHORT.get(kind, ""),
        "kind_auto": bool(target.get("kind_auto")),
        "strict": strict,
        "groups": out_groups,
        "ready_total": sum(g["ready"] for g in out_groups),
        "work_hours_ok": bool(st["stats"]["in_work_hours"]),
        "work_hours": db.get_setting("work_hours", "8-23"),
        "mode": st["stats"]["captcha"]["mode"],
        "mode_label": st["stats"]["captcha"]["mode_label"],
        "now": now_ts,
    }


def _types() -> dict:
    try:
        import json
        return json.loads(db.CATALOG_PATH.read_text(encoding="utf-8"))["types"]
    except Exception:
        return {}


def _platforms() -> dict:
    try:
        import json
        return json.loads(db.CATALOG_PATH.read_text(encoding="utf-8"))["platforms"]
    except Exception:
        return {}


# ---------------------------------------------------------------- планирование
def plan(target_id: int, items: list[dict], autostart: bool = True) -> dict:
    """Создаёт задачи с расписанием.

    items: [{"service_id": str, "claims": int}]
    Возвращает созданные задачи и предупреждения (что не поместилось в лимиты).
    """
    now_ts = now()
    since = day_start(now_ts)
    created, warnings = [], []
    svcs = services_map()
    if not isinstance(items, list):
        raise ValueError("items: ожидался список услуг")
    items = [it for it in items if isinstance(it, dict)]
    if not items:
        raise ValueError("Не выбрано ни одной услуги")

    with db.rd() as conn:
        tg = conn.execute("SELECT * FROM targets WHERE id=?", (target_id,)).fetchone()
        if not tg:
            raise ValueError("Ссылка не найдена")
        kind, kind_auto = target_kind(tg)
        stats = claims_done_today(conn, svcs.keys(), since)
        last_by_site = {r["site_id"]: (r["last"] or 0) for r in conn.execute("""
            SELECT v.site_id, MAX(t.finished_at) AS last FROM tasks t
            JOIN services v ON v.id=t.service_id WHERE t.status='done' GROUP BY v.site_id""").fetchall()}
        last_by_service = {r["service_id"]: (r["last"] or 0) for r in conn.execute("""
            SELECT service_id, MAX(finished_at) AS last FROM tasks
            WHERE status='done' GROUP BY service_id""").fetchall()}
        planned_by_site = {r["site_id"]: (r["last"] or 0) for r in conn.execute("""
            SELECT v.site_id, MAX(t.due_at) AS last FROM tasks t JOIN services v ON v.id=t.service_id
            WHERE t.status IN ('queued','ready','running') GROUP BY v.site_id""").fetchall()}
        planned_by_service = {r["service_id"]: (r["last"] or 0) for r in conn.execute("""
            SELECT service_id, MAX(due_at) AS last FROM tasks
            WHERE status IN ('queued','ready','running') GROUP BY service_id""").fetchall()}
        # COOLDOWN сайта (для сайтов с общим слотом берём самый длинный из его услуг)
        site_cool = {}
        for v in svcs.values():
            site_cool[v["site_id"]] = max(site_cool.get(v["site_id"], 0), int(v["cooldown_min"]))
        pending_seen: dict[str, int] = {}   # локальный счётчик запланированного в этом же плане

    from core import profiles as _profiles      # локальный импорт: profiles сам зависит от engine
    try:
        profs = _profiles.all_profiles()
    except Exception:
        profs = {}

    strict = (db.get_setting("strict_subject", "1") or "1") == "1"
    if not kind:
        warnings.append("У ссылки не указано, что это — сообщество, личная страница или запись. "
                        "Откройте «Мои ссылки» и выберите тип: без него нельзя проверить, "
                        "подходит ли услуга к объекту.")

    with db.tx() as conn:
        for it in items:
            sid = it.get("service_id")
            want = max(1, int(it.get("claims", 1)))
            svc = svcs.get(sid)
            if not svc:
                warnings.append(f"Услуга {sid} не найдена — пропущена")
                continue
            if not svc["enabled"] or not svc["site_enabled"]:
                warnings.append(f"{svc['site_name']}: сайт/услуга отключены")
                continue
            if tg["platform"] not in json_platforms(svc):
                warnings.append(f"{svc['site_name']}: услуга не для платформы «{tg['platform']}»")
                continue
            if strict and not subject_fits(svc.get("subject"), kind):
                svc_sub = SUBJECT_SHORT.get(svc.get("subject") or "any", "любой объект")
                warnings.append(
                    f"{svc['site_name']} · {svc['label']}: услуга рассчитана на «{svc_sub}», "
                    f"а ссылка — «{SUBJECT_SHORT.get(kind, kind)}»")
                continue
            if not _profiles.is_usable(svc["site_id"], profs):
                note = (profs.get(svc["site_id"]) or {}).get("degraded_note") or "площадка помечена как нерабочая"
                warnings.append(f"{svc['site_name']}: {note} — заходы не ставлю. "
                                f"Проверьте площадку на экране «Площадки».")
                continue
            if not captcha_allowed(svc):
                warnings.append(
                    f"{svc['site_name']} · {svc['label']}: не входит в режим «{CAPTCHA_MODES[captcha_mode()]}»"
                    f" (капча: {CAPTCHA_LABELS.get(int(svc.get('captcha', -1)), 'не проверено')})")
                continue

            st = stats[sid]
            max_claims = svc["max_claims_day"]
            used = st["claims"] + pending_seen.get(sid, 0)
            # сколько заходов ещё влезает в суточный лимит (None = лимит не заявлен)
            free_today = None if max_claims is None else max(0, int(max_claims) - used)
            allowed = want if free_today is None else min(want, free_today)
            if allowed < want:
                warnings.append(
                    f"{svc['site_name']} · {svc['label']}: доступно {allowed} из {want} заходов (суточный лимит)")
            if allowed <= 0:
                continue

            cooldown = int(svc["cooldown_min"]) * 60
            shared = svc["site_id"] in db.SHARED_SLOT_SITES
            step = max(cooldown, site_cool.get(svc["site_id"], 0) * 60) if shared else cooldown
            if shared:
                # у сайта один бесплатный слот: учитываем и уже сделанные, и уже запланированные заходы
                base_last = max(last_by_site.get(svc["site_id"], 0),
                                planned_by_site.get(svc["site_id"], 0),
                                st["last_finished"] or 0)
            else:
                base_last = max(st["last_finished"] or 0, last_by_service.get(sid, 0),
                                planned_by_service.get(sid, 0))
            slot = max(now_ts, (base_last + step) if base_last else now_ts)

            for i in range(allowed):
                due = slot + i * step
                status = "ready" if due <= now_ts + 2 and autostart else "queued"
                cur = conn.execute(
                    "INSERT INTO tasks(service_id,target_id,status,planned_units,created_at,due_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (sid, target_id, status, int(svc["amount"]), now_ts, due))
                created.append({"id": cur.lastrowid, "service_id": sid, "site": svc["site_name"],
                                "label": svc["label"], "due_at": due, "amount": int(svc["amount"]),
                                "subject": svc.get("subject") or "any",
                                "target_kind": kind})
            pending_seen[sid] = pending_seen.get(sid, 0) + allowed
            # следующий заход по этому же сайту/услуге в этом плане должен уважать тот же шаг
            last_due = slot + (allowed - 1) * step
            planned_by_site[svc["site_id"]] = last_due
            planned_by_service[sid] = last_due

    db.log("info", f"Запланировано задач: {len(created)} на «{tg['name']}»"
                   + (f" (объект: {SUBJECT_SHORT.get(kind, 'не определён')})" if kind else ""))
    return {"created": created, "warnings": warnings,
            "summary": _plan_summary(created)}


def json_platforms(svc: dict) -> set[str]:
    return {svc["platform"]}


def _plan_summary(created: list[dict]) -> list[dict]:
    agg: dict[str, dict] = {}
    for c in created:
        a = agg.setdefault(c["service_id"], {"site": c["site"], "label": c["label"], "claims": 0, "units": 0})
        a["claims"] += 1
        a["units"] += c["amount"]
    return sorted(agg.values(), key=lambda x: -x["units"])


# --------------------------------------------------------------- продвижение
def promote_due() -> int:
    """queued -> ready, когда подошло время. Вызывается планировщиком."""
    now_ts = now()
    with db.tx() as conn:
        cur = conn.execute(
            "UPDATE tasks SET status='ready' WHERE status='queued' AND due_at<=?", (now_ts,))
        return cur.rowcount


def reap_stale(grace_min: int = 15) -> int:
    """Сторож: задачи, которые человек открыл и не подтвердил.

    Если дедлайн прошёл больше grace_min назад — возвращаем в очередь (повторим позже),
    а после 3 таких промахов закрываем как 'missed', чтобы не крутить одно и то же бесконечно.
    """
    now_ts = now()
    moved = 0
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT id, attempts FROM tasks WHERE status='wait_confirm' AND due_at IS NOT NULL AND due_at < ? AND id NOT IN (SELECT task_id FROM deliveries WHERE state IN ('reserved','submitting','waiting','uncertain'))",
            (now_ts - grace_min * 60,)).fetchall()
        for r in rows:
            if int(r["attempts"] or 0) >= 3:
                conn.execute("UPDATE tasks SET status='missed', note='не подтверждено за отведённое время'"
                             " WHERE id=?", (r["id"],))
                db.log("warn", f"Задача #{r['id']} закрыта как пропущенная (3 промаха)", None, int(r["id"]))
            else:
                conn.execute("UPDATE tasks SET status='queued', due_at=? WHERE id=?",
                             (now_ts + 300, r["id"]))
            moved += 1
    return moved


def can_start_now() -> tuple[bool, str]:
    """Можно ли прямо сейчас начинать новый заход.

    Проверяет то, что нельзя нарушать ни при каких обстоятельствах:
    рабочие часы, минимальный интервал между любыми заходами и общий суточный лимит.
    """
    if not in_work_hours():
        return False, "вне рабочих часов"
    now_ts = now()
    with db.rd() as conn:
        last = conn.execute("SELECT MAX(started_at) m FROM tasks WHERE started_at IS NOT NULL").fetchone()["m"] or 0
        gap = int(db.get_setting("global_min_gap_sec", "20") or 0)
        if gap > 0 and now_ts - last < gap:
            return False, f"минимальный интервал {gap} с"
        limit = int(db.get_setting("daily_total_limit", "0") or 0)
        if limit > 0:
            done = conn.execute("SELECT COUNT(*) c FROM tasks WHERE finished_at IS NOT NULL AND finished_at >= ?",
                                (day_start(now_ts),)).fetchone()["c"]
            if done >= limit:
                return False, f"достигнут общий лимит {limit} заходов в сутки"
    return True, ""


def next_open_task() -> dict | None:
    """Pick an unblocked task; an active delivery for the same metric is a hard barrier."""
    from core import delivery
    mode = captcha_mode()
    extra = " AND v.captcha<>1" if mode == "nocaptcha" else (" AND v.captcha=1" if mode == "captcha" else "")
    with db.rd() as conn:
        if db.unresolved_order_count(conn):
            return None
        active = {(r['target_key'], r['metric']) for r in conn.execute(
            "SELECT target_key,metric FROM deliveries WHERE state IN ('reserved','submitting','waiting','uncertain')")}
        rows = conn.execute(f"""
            SELECT t.*, v.url AS service_url, v.amount, v.hold_seconds, v.captcha, v.stype,
                   s.name AS site_name, s.mode AS site_mode,
                   tg.url AS target_url, tg.name AS target_name, tg.platform AS target_platform
            FROM tasks t JOIN services v ON v.id=t.service_id JOIN sites s ON s.id=v.site_id
            LEFT JOIN targets tg ON tg.id=t.target_id LEFT JOIN goals g ON g.id=t.goal_id
            WHERE t.status='ready' AND s.enabled=1 AND s.breaker_until<=?{extra}
              AND (t.goal_id IS NULL OR g.status IN ('planned','running'))
            ORDER BY t.due_at, t.id LIMIT 500""", (now(),)).fetchall()
    full = db.get_setting('full_auto', '0') == '1'
    for row in rows:
        t = dict(row)
        if full and t['site_mode'] == 'manual':
            continue
        try:
            key, _ = delivery.identity({'url': t['target_url'], 'platform': t['target_platform']}, t['stype'])
        except (delivery.CounterError, ValueError, TypeError):
            key = f"target:{t['target_id']}"
        if (key, t['stype']) not in active:
            return t
    return None


def reschedule(site_id: str | None = None) -> dict:
    """Пересчитывает время уже стоящих в очереди задач после ручной правки кулдаунов."""
    now_ts = now()
    svcs = services_map()
    with db.rd() as conn:
        last_done_site = {r["site_id"]: (r["last"] or 0) for r in conn.execute("""
            SELECT v.site_id, MAX(t.finished_at) AS last FROM tasks t
            JOIN services v ON v.id=t.service_id WHERE t.status='done' GROUP BY v.site_id""").fetchall()}
        last_done_svc = {r["service_id"]: (r["last"] or 0) for r in conn.execute("""
            SELECT service_id, MAX(finished_at) AS last FROM tasks
            WHERE status='done' GROUP BY service_id""").fetchall()}
    updated = 0
    with db.tx() as conn:
        rows = conn.execute("""
            SELECT t.id, t.service_id, t.due_at, t.created_at FROM tasks t
            WHERE t.status IN ('queued','ready') ORDER BY COALESCE(t.due_at, t.created_at), t.id""").fetchall()
        next_free: dict[tuple[str, str], int] = {}
        for r in rows:
            svc = svcs.get(r["service_id"])
            if not svc or (site_id and svc["site_id"] != site_id):
                continue
            cooldown = int(svc["cooldown_min"]) * 60
            shared = svc["site_id"] in db.SHARED_SLOT_SITES
            key = ("site", svc["site_id"]) if shared else ("svc", r["service_id"])
            if key not in next_free:
                base = last_done_site.get(svc["site_id"], 0) if shared else last_done_svc.get(r["service_id"], 0)
                next_free[key] = max(now_ts, (base + cooldown) if base else now_ts)
            due = next_free[key]
            next_free[key] = due + cooldown
            status = "ready" if due <= now_ts + 2 else "queued"
            conn.execute("UPDATE tasks SET due_at=?, status=? WHERE id=?", (due, status, r["id"]))
            updated += 1
    if updated:
        db.log("info", f"Очередь пересчитана по новым кулдаунам: {updated} задач")
    return {"updated": updated}


def _has_playwright() -> bool:
    """Есть ли Playwright — от этого зависит, может ли робот работать вообще."""
    import importlib.util
    return importlib.util.find_spec("playwright") is not None


def health() -> dict:
    with db.rd() as conn:
        q = conn.execute("SELECT COUNT(*) c FROM tasks WHERE status IN ('ready','wait_confirm','captcha','running')").fetchone()["c"]
        cap = conn.execute("SELECT COUNT(*) c FROM tasks WHERE status='captcha'").fetchone()["c"]
        failed = conn.execute("SELECT COUNT(*) c FROM tasks WHERE status='failed'").fetchone()["c"]
        return {"ready_tasks": q, "failed_tasks": failed, "captcha_tasks": cap,
                "playwright": _has_playwright(), "db": str(db.DB_PATH)}
