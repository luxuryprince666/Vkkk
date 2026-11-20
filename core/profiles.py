# -*- coding: utf-8 -*-
"""Профили площадок: что софт знает о каждой и что с этим делать.

Профиль хранится в БД (`site_profiles`) и подтягивается из `research/sites.json` — файла,
который собирается живым обходом площадок (`tools/probe_sites.py`). Здесь же — самопроверка:
регулярно открываем страницу заказа и смотрим, на месте ли поле ссылки, кнопка и не появилась
ли капча. Если форма изменилась — площадка помечается `degraded`, и планировщик её не трогает.

Классы автоматизации:
    A — форма без капчи и без таймера: можно вести роботом сразу
    B — без капчи, но есть таймер/очередь: робот ждёт на странице
    C — капча или сложная механика (общий пул, iframe): нужен человек рядом
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from core import db, probe

ROOT = Path(__file__).resolve().parent.parent
PROFILES_PATH = ROOT / "research" / "sites.json"

CLASS_LABELS = {
    "A": "полный автопилот",
    "B": "автопилот с ожиданием",
    "C": "нужен браузер/человек",
    "-": "не работает",
}

# Сколько ждать таймер/очередь по умолчанию, если он есть, но длительность неизвестна
DEFAULT_HOLD = 120
MAX_HOLD = 360


def _class_of(entry: dict) -> str:
    cls = (entry.get("automation_class") or "").strip().upper()
    if cls in ("A", "B", "C"):
        return cls
    return "C" if entry.get("alive") else "-"


def _hold_of(entry: dict) -> int:
    """Сколько ждать перед отправкой формы (таймер/очередь).

    Важно: в профилях текст вида «выдача 3 мин – 1 час» описывает ДОСТАВКУ, а не ожидание.
    Ждём мы только там, где есть таймер или очередь, — иначе робот простоит лишние минуты.
    """
    note = (entry.get("hold") or "").lower()
    if not note or note.strip() in ("—", "-", "мгновенно"):
        return 0
    if not any(w in note for w in ("таймер", "очеред", "ожидан", "подожд", "сек")):
        return 0
    num = ""
    for ch in note:
        if ch.isdigit():
            num += ch
        elif num:
            break
    if not num:
        return DEFAULT_HOLD                      # таймер есть, длительность неизвестна
    value = int(num)
    if "мин" in note:
        value *= 60
    return max(15, min(MAX_HOLD, value))


def seed_profiles(path: Path | None = None) -> dict:
    """Подтянуть профили в БД. Повторный вызов ничего не ломает (upsert).

    Ручные пометки (degraded) и время последней проверки НЕ перетираются — они про реальность,
    а не про каталог.
    """
    src = Path(path) if path else PROFILES_PATH
    entries = json.loads(src.read_text(encoding="utf-8")) if src.exists() else []
    n = 0
    with db.tx() as conn:
        for e in entries:
            sid = e.get("id")
            exists = conn.execute("SELECT 1 FROM sites WHERE id=?", (sid,)).fetchone()
            if not exists:
                continue
            order_page = {
                "hold_label": (e.get("hold") or "").strip()[:80],
                "link_selector": "",       # заполняется, если знаем точно; иначе работает эвристика
                "submit_selector": "",
                "success_text": ["заказ принят", "успешно", "ожидайте", "в очереди", "принят"],
                "fail_text": ["лимит", "попробуйте позже", "неверная ссылка", "уже был", "недоступно"],
            }
            conn.execute(
                "INSERT INTO site_profiles(site_id, automation, captcha, hold_seconds, keep_open,"
                " pool_daily, profile_json, degraded, degraded_note, checked_at)"
                " VALUES(?,?,?,?,?,?,?,0,'',?)"
                " ON CONFLICT(site_id) DO UPDATE SET automation=excluded.automation,"
                " captcha=excluded.captcha, hold_seconds=excluded.hold_seconds,"
                " keep_open=excluded.keep_open, pool_daily=excluded.pool_daily,"
                " profile_json=excluded.profile_json",
                (sid, _class_of(e), json.dumps(e.get("captcha") or [], ensure_ascii=False),
                 int(_hold_of(e)), 1 if e.get("page_must_stay_open") else 0,
                 e.get("pool_daily"), json.dumps(order_page, ensure_ascii=False),
                 int(e.get("checked_at_ts") or 0)))
            n += 1
        # Для новых площадок без отдельного исследования создаём консервативный
        # профиль: требуется человек, а не выдуманный успешный автопилот.
        conn.execute("INSERT OR IGNORE INTO site_profiles(site_id, automation, degraded_note) "
                     "SELECT id, 'C', 'Профиль ещё не исследован' FROM sites")
    reset_cache()
    return {"profiles": n}


def _row_to_profile(row) -> dict:
    d = dict(row)
    try:
        d["captcha"] = json.loads(d.get("captcha") or "[]")
    except Exception:
        d["captcha"] = []
    try:
        d["order_page"] = json.loads(d.get("profile_json") or "{}")
    except Exception:
        d["order_page"] = {}
    d["class_label"] = CLASS_LABELS.get(d.get("automation") or "-", "неизвестно")
    return d


def get_profile(site_id: str) -> dict | None:
    with db.rd() as conn:
        row = conn.execute("SELECT * FROM site_profiles WHERE site_id=?", (site_id,)).fetchone()
    return _row_to_profile(row) if row else None


_CACHE: dict = {"ts": 0.0, "data": {}}
_CACHE_TTL = 10.0


def reset_cache() -> None:
    _CACHE["ts"] = 0.0
    _CACHE["data"] = {}


def all_profiles(ttl: float | None = None) -> dict[str, dict]:
    """Профили всех площадок. Кэш на 10 секунд: этим пользуется и рендер услуг, и планировщик."""
    ttl = _CACHE_TTL if ttl is None else ttl
    if _CACHE["data"] and (time.time() - _CACHE["ts"]) < ttl:
        return _CACHE["data"]
    with db.rd() as conn:
        rows = conn.execute("SELECT p.*, s.name AS site_name, s.enabled, s.mode, s.breaker_until"
                            " FROM site_profiles p JOIN sites s ON s.id=p.site_id").fetchall()
    data = {r["site_id"]: {**_row_to_profile(r), "site_name": r["site_name"],
                           "enabled": bool(r["enabled"]), "mode": r["mode"],
                           "breaker_until": r["breaker_until"]} for r in rows}
    _CACHE.update(ts=time.time(), data=data)
    return data


def is_usable(site_id: str, profiles: dict | None = None) -> bool:
    """Можно ли планировать заходы на площадку прямо сейчас."""
    p = (profiles or all_profiles()).get(site_id)
    if not p:
        return True                       # профиля нет — работаем по общим правилам каталога
    if p["automation"] == "-" or p["degraded"]:
        return False
    return True


def health() -> list[dict]:
    """Сводка «как дела у площадок» для интерфейса."""
    now_ts = int(time.time())
    with db.rd() as conn:
        rows = conn.execute("""
            SELECT p.*, s.name, s.enabled, s.mode, s.free_page, s.breaker_until, s.reg_required,
                   (SELECT COUNT(*) FROM services v WHERE v.site_id=s.id) AS services_n,
                   (SELECT COUNT(*) FROM tasks t JOIN services v ON v.id=t.service_id
                     WHERE v.site_id=s.id AND t.status IN ('queued','ready','running')) AS tasks_open
            FROM site_profiles p JOIN sites s ON s.id=p.site_id
            ORDER BY s.name""").fetchall()
        orphan = conn.execute("""
            SELECT s.id, s.name, s.enabled, s.mode, s.free_page, s.breaker_until, s.reg_required,
                   (SELECT COUNT(*) FROM services v WHERE v.site_id=s.id) AS services_n,
                   (SELECT COUNT(*) FROM tasks t JOIN services v ON v.id=t.service_id
                     WHERE v.site_id=s.id AND t.status IN ('queued','ready','running')) AS tasks_open
            FROM sites s WHERE s.id NOT IN (SELECT site_id FROM site_profiles) ORDER BY s.name""").fetchall()
    out = []
    for r in list(rows) + list(orphan):
        d = dict(r)
        try:
            d["captcha"] = json.loads(d.get("captcha") or "[]")
        except Exception:
            d["captcha"] = []
        d["automation"] = d.get("automation") or "-"
        d["class_label"] = CLASS_LABELS.get(d["automation"], "не проверялась")
        try:
            d["hold_label"] = (json.loads(d.get("profile_json") or "{}") or {}).get("hold_label") or ""
        except Exception:
            d["hold_label"] = ""
        d["status"] = ("выключена" if not d["enabled"] else
                       "не работает" if d["automation"] == "-" else
                       "форма изменилась" if d["degraded"] else
                       "капча" if d["captcha"] else
                       "работает")
        d["breaker_left"] = max(0, int(d.get("breaker_until") or 0) - now_ts)
        d.pop("profile_json", None)
        out.append(d)
    return out


def check_site(site_id: str, order_url: str | None = None) -> dict:
    """Открыть страницу заказа и убедиться, что форма жива. Обновляет профиль в БД.

    Проверяем три вещи: страница доступна, есть поле для ссылки, есть кнопка отправки.
    Капчу не считаем ошибкой — это свойство площадки (класс C), но запоминаем её вид.
    """
    with db.rd() as conn:
        site = conn.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone()
        if not site:
            raise ValueError("Площадка не найдена")
        if not order_url:
            svc = conn.execute("SELECT url FROM services WHERE site_id=? AND enabled=1"
                               " ORDER BY platform='vk' DESC LIMIT 1", (site_id,)).fetchone()
            order_url = svc["url"] if svc else site["free_page"] or site["home"]
    res = probe.probe(order_url)
    now_ts = int(time.time())

    reachable = bool(res.get("status") and not res.get("error"))
    blocked = bool(res.get("blocked"))
    link_ok = bool(res.get("has_link_field"))
    submit_ok = bool(res.get("has_submit"))
    captcha = res.get("captcha") or []

    # «Форма не найдена» — это не приговор, если сайт рисует форму на JS (SPA/iframe).
    spa_form = bool(res.get("spa"))         # форму рисует JS-фреймворк: в HTML её просто нет
    if not reachable or blocked:
        verdict, note = "dead", "страница не открывается" if not reachable else "антибот-защита"
    elif link_ok and submit_ok:
        verdict, note = "ok", "поле ссылки и кнопка на месте"
    elif link_ok or submit_ok or spa_form or res.get("forms_n") or res.get("fields_n") \
            or res.get("iframes_n"):
        # часть элементов рисуется скриптами — это норма, окончательно проверяем браузером при заходе
        missing = ("форма в отдельном окне" if res.get("iframes_n") and not (link_ok or submit_ok)
                   else "кнопка заказа" if link_ok else ("поле ссылки" if submit_ok else "форма целиком"))
        verdict, note = "js", f"в HTML не видно ({missing}) — проверим браузером"
    else:
        if res.get("antibot"):
            # антибот-шлюз («Подтвердите, что вы человек») — это не поломка: человек
            # кликает проверку при заходе, дальше сайт работает как обычно
            verdict, note = "js", "антибот-шлюз «я не робот» — человек кликает при заходе, дальше софт сам"
        else:
            verdict, note = "changed", "не нашёл ни поля ссылки, ни кнопки заказа"

    last_check = {"verdict": verdict, "url": order_url, "status": res.get("status"),
                  "fields": res.get("fields_n"), "has_link": link_ok,
                  "has_submit": submit_ok, "sec": res.get("sec"), "checked_at": now_ts}
    with db.tx() as conn:
        row = conn.execute("SELECT profile_json FROM site_profiles WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO site_profiles(site_id) VALUES(?)", (site_id,))
            base = {}
        else:
            try:
                base = json.loads(row["profile_json"] or "{}")
            except Exception:
                base = {}
        bad = verdict in ("dead", "changed")
        fails = int(base.get("fail_n") or 0)
        fails = fails + 1 if bad else 0
        base["fail_n"] = fails
        base["last_check"] = last_check
        # выключаем площадку только после двух провалов подряд: один сетевой сбой — не приговор
        degraded = 1 if fails >= 2 else 0
        if fails == 1:
            note += " (первый раз — перепроверю)"
        conn.execute("UPDATE site_profiles SET degraded=?, degraded_note=?, checked_at=?, captcha=?,"
                     " profile_json=? WHERE site_id=?",
                     (degraded, note, now_ts, json.dumps(captcha, ensure_ascii=False),
                      json.dumps(base, ensure_ascii=False), site_id))
    reset_cache()
    if verdict == "dead":
        db.log("warn", f"{site['name']}: страница заказа недоступна — площадка выключена из планов", site_id)
    elif verdict == "changed":
        db.log("warn", f"{site['name']}: форма заказа изменилась — нужна проверка", site_id)
    return {"site_id": site_id, "verdict": verdict, "note": note, "url": order_url,
            "status": res.get("status"), "captcha": captcha, "has_link_field": link_ok,
            "has_submit": submit_ok, "fields_n": res.get("fields_n"), "sec": res.get("sec")}


def check_all(limit: int | None = None) -> list[dict]:
    """Проверить все включённые площадки (для ежедневной самопроверки)."""
    with db.rd() as conn:
        rows = conn.execute("SELECT id, name FROM sites WHERE enabled=1 ORDER BY name").fetchall()
    out = []
    for r in (rows[:limit] if limit else rows):
        try:
            out.append(check_site(r["id"]))
        except Exception as exc:                     # площадка не должна ронять весь обход
            out.append({"site_id": r["id"], "verdict": "error", "note": str(exc)[:120]})
    good = sum(1 for x in out if x["verdict"] in ("ok", "js"))
    db.log("info", f"Самопроверка площадок: {good} из {len(out)} в порядке")
    return out


def stale_sites(max_age_hours: int = 24) -> list[str]:
    """Площадки, которые давно не проверяли (или не проверяли вовсе)."""
    limit = int(time.time()) - max_age_hours * 3600
    with db.rd() as conn:
        rows = conn.execute(
            "SELECT p.site_id FROM site_profiles p JOIN sites s ON s.id=p.site_id"
            " WHERE s.enabled=1 AND p.checked_at < ? AND s.reg_required=0", (limit,)).fetchall()
    return [r["site_id"] for r in rows]
