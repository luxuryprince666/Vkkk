# -*- coding: utf-8 -*-
"""Исполнитель задач.

Два режима:
  assisted (по умолчанию) — программа готовит всё, что можно автоматизировать: копирует
      вашу ссылку в буфер, открывает точную страницу с бесплатной кнопкой, держит таймер
      и записывает результат. Последний клик остаётся за вами: на всех этих сайтах стоят
      свои таймеры и проверки, обходить которые бессмысленно и небезопасно.

  auto (экспериментальный) — Playwright-робот: открывает страницу, ищет поле для ссылки,
      вставляет, жмёт кнопку и ждёт таймер. Требует `pip install playwright && playwright install chromium`.
      Селекторы эвристические, поэтому при 3 неудачах сайт автоматически отключается (breaker).
"""
from __future__ import annotations

import contextlib
import platform
import shutil
import subprocess
import threading
import time
import webbrowser
from pathlib import Path

from . import db, delivery, engine

SCREENS = db.BASE / "logs" / "screens"


# ------------------------------------------------------------------ буфер обмена
def copy_to_clipboard(text: str) -> str:
    """Возвращает способ, которым удалось скопировать (или 'file')."""
    sysname = platform.system()
    try:
        if sysname == "Windows":
            p = subprocess.Popen(["clip"], stdin=subprocess.PIPE, shell=True)
            p.communicate(text.encode("utf-16le"))
            return "clip"
        if sysname == "Darwin":
            p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
            p.communicate(text.encode("utf-8"))
            return "pbcopy"
        for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
            if shutil.which(cmd[0]):
                p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
                p.communicate(text.encode("utf-8"))
                return cmd[0]
    except Exception:
        pass
    # универсальный фолбэк: файл, который интерфейс показывает и даёт скопировать вручную
    path = db.DATA / "clipboard.txt"
    path.write_text(text, encoding="utf-8")
    return "file"


def open_url(url: str) -> bool:
    if db.get_setting("open_browser", "1") != "1" or db.get_setting("dry_run", "0") == "1":
        return False
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:
        return False


# ------------------------------------------------------------------ запуск задачи
def start_task(task_id: int) -> dict:
    """Переводит задачу в работу и готовит всё для пользователя/робота."""
    if AUTO_LOCK.locked():
        return {"ok": False, "task_id": task_id,
                "error": "Робот уже занят другой задачей — попробуйте позже"}
    now_ts = engine.now()
    block_note = ""
    reserved = False
    with db.tx() as conn:
        t = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not t:
            raise ValueError("Задача не найдена")
        reopen = t["status"] == "wait_confirm"
        if t["status"] not in ("ready", "queued", "failed", "wait_confirm"):
            raise ValueError(f"Задача в статусе {t['status']}, запускать нельзя")
        svc = conn.execute("SELECT v.*, s.mode AS site_mode, s.name AS site_name FROM services v"
                           " JOIN sites s ON s.id=v.site_id WHERE v.id=?", (t["service_id"],)).fetchone()
        tg = conn.execute("SELECT * FROM targets WHERE id=?", (t["target_id"],)).fetchone()
        if not tg:
            raise ValueError("У задачи нет привязанной ссылки")
        # Ссылку могли переклассифицировать уже после планирования — проверяем ещё раз,
        # иначе слот сгорит на услуге, которая этот объект не принимает.
        # Статус меняем ПОСЛЕ выхода из транзакции: иначе исключение откатит запись.
        if (db.get_setting("strict_subject", "1") or "1") == "1":
            kind, _ = engine.target_kind(tg)
            svc_subject = engine._field(svc, "subject", "any")
            if not engine.subject_fits(svc_subject, kind):
                block_note = (f"Услуга для «{engine.SUBJECT_SHORT.get(str(svc_subject), 'другой объект')}», "
                              f"а ссылка — «{engine.SUBJECT_SHORT.get(kind, 'не определён')}»")
        if block_note:
            pass                      # ничего не трогаем — разберёмся ниже, после коммита
        elif reopen:
            # задача уже открыта человеком — просто обновляем буфер и продлеваем время
            conn.execute("UPDATE tasks SET started_at=COALESCE(started_at,?) WHERE id=?", (now_ts, task_id))
        else:
            reserved = delivery.reserve(conn, t, svc, tg)
            cur = conn.execute(
                "UPDATE tasks SET status='running', started_at=?, attempts=attempts+1, note=NULL"
                " WHERE id=? AND status IN ('ready','queued','failed')", (now_ts, task_id))
            if cur.rowcount == 0:
                raise ValueError("Задачу уже забрал другой процесс")

    if block_note:
        _set_task(task_id, "failed", block_note)
        db.log("warn", f"Заход отменён: {block_note}", svc["site_id"], task_id)
        raise ValueError(block_note + " — поправьте тип ссылки в «Мои ссылки»")

    if reserved:
        try:
            delivery.set_baseline(task_id, dict(tg), svc['stype'])
        except delivery.CounterError as exc:
            delivery.release_before_submit(task_id, str(exc))
            with db.tx() as conn:
                conn.execute("UPDATE tasks SET status='queued', due_at=?, note=? WHERE id=?",
                             (engine.now() + 120, f'отправка остановлена: {exc}', task_id))
            db.log('warn', f'Заказ #{task_id} не отправлен: {exc}', task_id=task_id)
            return {"ok": False, "task_id": task_id, "error": str(exc)}

    try:
        return _prepare_after_claim(task_id, svc, tg, now_ts)
    except Exception:
        # что-то пошло не так уже после захвата — задача не должна остаться «выполняется» навсегда
        with contextlib.suppress(Exception):
            if not delivery.waiting(task_id, uncertain=True, note='сбой запуска; проверьте сайт'):
                _set_task(task_id, "ready", "сбой запуска — задача возвращена в очередь")
        raise


def robot_now(task_id: int) -> dict:
    """Запустить робота на конкретной задаче прямо сейчас, даже если площадка в режиме «assisted».

    Нужно для кнопки «выполнить роботом»: человек видит заход, который стоит в очереди,
    и хочет, чтобы его сделал робот, а не он сам.
    """
    with db.rd() as conn:
        t = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not t:
            raise ValueError("Задача не найдена")
        if t["status"] not in ("queued", "ready", "failed", "wait_confirm"):
            raise ValueError(f"Задача в статусе {t['status']} — робот за неё не возьмётся")
        svc = conn.execute("SELECT v.*, s.name AS site_name, s.mode AS site_mode FROM services v"
                           " JOIN sites s ON s.id=v.site_id WHERE v.id=?", (t["service_id"],)).fetchone()
        tg = conn.execute("SELECT * FROM targets WHERE id=?", (t["target_id"],)).fetchone()
    if not tg:
        raise ValueError("У задачи нет привязанной ссылки")
    if not engine.captcha_allowed(dict(svc)):
        raise ValueError(f"Услуга не входит в режим «{engine.CAPTCHA_MODES[engine.captcha_mode()]}»")
    # Блокировку здесь НЕ берём: её возьмёт _prepare_after_claim() после захвата задачи.
    # Иначе получается двойной захват, и робот отвечает «занят другой задачей» на первую же кнопку.
    if AUTO_LOCK.locked():
        return {"ok": False, "error": "Робот уже занят другой задачей — попробуйте через минуту"}
    _FORCE_AUTO.add(task_id)
    try:
        result = start_task(task_id)      # захват задачи, проверки объекта и запуск потока робота
        if not result.get("ok", False):
            _FORCE_AUTO.discard(task_id)
            return result
    except Exception:
        _FORCE_AUTO.discard(task_id)   # задача не стартовала — не тащим за собой флаг
        raise
    db.log("info", f"Робот запущен вручную: {svc['site_name']} / {svc['label']}", svc["site_id"], task_id)
    return {"ok": True, "pending": True, "task_id": task_id,
            "site": svc["site_name"], "label": svc["label"]}


def _obj_note(svc, tg) -> str:
    """Короткая подпись объекта, чтобы в журнале было видно: крутим сообщество или человека."""
    kind, _ = engine.target_kind(tg)
    svc_sub = engine.SUBJECT_SHORT.get(str(engine._field(svc, "subject", "any") or "any"), "любой объект")
    tg_sub = engine.SUBJECT_SHORT.get(kind, "не определён")
    return f"объект: {tg_sub} (услуга: {svc_sub})"


def _prepare_after_claim(task_id: int, svc, tg, now_ts: int) -> dict:
    force_auto = task_id in _FORCE_AUTO
    _FORCE_AUTO.discard(task_id)
    mode = svc["site_mode"] if svc["site_mode"] in ("assisted", "auto") else "assisted"
    if force_auto or (db.get_setting("full_auto", "0") == "1" and svc["site_mode"] != "manual"):
        mode = "auto"          # «выполнить роботом сейчас» — даже на assisted-площадке
    clip_method = copy_to_clipboard(tg["url"]) if mode != "auto" else ""
    opened = open_url(svc["url"]) if mode != "auto" else False
    deadline = now_ts + int(svc["hold_seconds"])

    if mode == "auto":
        if not engine.captcha_allowed(svc):
            _set_task(task_id, "ready", "услуга не входит в выбранный режим капчи")
            raise ValueError(f"Услуга не входит в режим «{engine.CAPTCHA_MODES[engine.captcha_mode()]}»")
        if not AUTO_LOCK.acquire(blocking=False):
            delivery.release_before_submit(task_id, "робот занят; заказ не отправлен")
            _set_task(task_id, "ready", "робот занят другой задачей")
            return {"ok": False, "mode": mode, "task_id": task_id,
                    "error": "Робот уже работает над другой задачей — вернул эту в очередь."}
        try:
            threading.Thread(target=_auto_worker, args=(task_id, engine.apply_overrides(dict(svc)), tg["url"]),
                             daemon=True, name=f"auto-{task_id}").start()
        except Exception as exc:
            AUTO_LOCK.release()
            _set_task(task_id, "failed", f"не удалось запустить робота: {exc}")
            raise
        db.log("info", f"Робот пошёл на {svc['site_name']} / {svc['label']} — {_obj_note(svc, tg)}",
               svc["site_id"], task_id)
        return {
            "ok": True,
            "mode": "auto",
            "pending": True,
            "task_id": task_id,
            "site": svc["site_name"],
            "label": svc["label"],
            "open_url": svc["url"],
            "target_url": tg["url"],
            "hold_seconds": int(svc["hold_seconds"]),
            "hint": "Робот работает в отдельном окне. Если появится капча — решите её там, "
                    "заход продолжится сам.",
        }

    with db.tx() as conn:
        conn.execute("UPDATE tasks SET status='wait_confirm', due_at=? WHERE id=?", (deadline, task_id))
    db.log("info", f"Открыл {svc['site_name']} / {svc['label']} — {_obj_note(svc, tg)}",
           svc["site_id"], task_id)
    return {
        "ok": True,
        "mode": "assisted",
        "task_id": task_id,
        "site": svc["site_name"],
        "label": svc["label"],
        "open_url": svc["url"],
        "target_url": tg["url"],
        "clipboard": clip_method,
        "browser_opened": opened,
        "hold_seconds": int(svc["hold_seconds"]),
        "deadline": deadline,
        "hint": (f"Ссылка уже в буфере ({clip_method}) — вставьте её на сайте, нажмите их кнопку "
                 f"и подождите {int(svc['hold_seconds'])} с, затем отметьте результат. "
                 f"{_obj_note(svc, tg)}."),
    }


def finish_task(task_id: int, ok: bool, note: str = "", units: int | None = None) -> dict:
    now_ts = engine.now()
    with db.rd() as conn:
        active = conn.execute("SELECT d.state,t.status FROM deliveries d JOIN tasks t ON t.id=d.task_id "
                              "WHERE d.task_id=? AND d.state IN ('reserved','submitting','waiting','uncertain')", (task_id,)).fetchone()
    if active and active['status'] == 'running':
        raise ValueError('Робот ещё работает; дождитесь результата перед ручным подтверждением')
    if delivery.waiting(task_id, uncertain=not ok, note=note):
        return {"ok": bool(ok), "status": "waiting_delivery" if ok else "delivery_uncertain", "units": 0}
    with db.tx() as conn:
        t = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not t:
            raise ValueError("Задача не найдена")
        if t["status"] in ("done", "missed", "skipped", "waiting_delivery", "delivery_uncertain"):
            # иначе двойной клик по «Готово» начислял бы единицы дважды и сбрасывал предохранитель сайта
            raise ValueError(f"Задача уже закрыта ({t['status']}) — повторно закрывать нельзя")
        svc = conn.execute("SELECT v.*, s.name AS site_name FROM services v JOIN sites s ON s.id=v.site_id"
                           " WHERE v.id=?", (t["service_id"],)).fetchone()
        if ok:
            done_units = int(units if units is not None else (t["planned_units"] or svc["amount"]))
            conn.execute(
                "UPDATE tasks SET status='done', finished_at=?, claimed_units=?, note=? WHERE id=?",
                (now_ts, done_units, note or None, task_id))
            # сбрасываем счётчик неудач сайта в ТОЙ ЖЕ транзакции (никаких вложенных begin — иначе дедлок)
            conn.execute("UPDATE sites SET breaker_fails=0 WHERE id=?", (svc["site_id"],))
        else:
            conn.execute("UPDATE tasks SET status='failed', finished_at=?, note=? WHERE id=?",
                         (now_ts, note or "не выполнено", task_id))
    if ok:
        # прогресс цели считаем ПОСЛЕ коммита: on_task_done сам открывает транзакции,
        # внутри чужой транзакции это дало бы 30-секундную блокировку базы
        try:
            from core import goals as _goals
            _goals.on_task_done(task_id)
        except Exception as exc:
            db.log("warn", f"Не удалось обновить прогресс цели по задаче {task_id}: {exc}")
        return {"ok": True, "status": "done", "units": done_units}
    mark_fail(svc["site_id"])
    db.log("warn", f"Заход не выполнен: {svc['site_name']} / {svc['label']} — {note}", svc["site_id"], task_id)
    return {"ok": False, "status": "failed"}


def cancel_task(task_id: int) -> dict:
    with db.tx() as conn:
        if conn.execute("SELECT 1 FROM deliveries WHERE task_id=? AND state IN ('reserved','submitting','waiting','uncertain')", (task_id,)).fetchone():
            raise ValueError("Заказ ещё может выполняться; сначала проверьте его и явно снимите ожидание")
        cur = conn.execute("UPDATE tasks SET status='skipped', finished_at=? WHERE id=? AND status IN"
                           " ('queued','ready','running','wait_confirm','captcha','failed')",
                           (engine.now(), task_id))
        if cur.rowcount == 0:
            row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            raise ValueError("Задача не найдена" if not row else f"Задача уже закрыта ({row['status']})")
    return {"ok": True, "status": "skipped"}


def retry_task(task_id: int) -> dict:
    with db.tx() as conn:
        cur = conn.execute(
            "UPDATE tasks SET status='ready', due_at=?, note=NULL, finished_at=NULL"
            " WHERE id=? AND status IN ('failed','missed','skipped')", (engine.now(), task_id))
        if cur.rowcount == 0:
            row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise ValueError("Задача не найдена")
            raise ValueError(f"Эту задачу нельзя вернуть в очередь: статус {row['status']}")
    return {"ok": True, "status": "ready"}


# ------------------------------------------------------------------ breaker
def mark_fail(site_id: str) -> None:
    """3 неудачи подряд -> сайт отключается на 6 часов (half-open после)."""
    with db.tx() as conn:
        row = conn.execute("SELECT breaker_fails FROM sites WHERE id=?", (site_id,)).fetchone()
        fails = (row["breaker_fails"] if row else 0) + 1
        if fails >= 3:
            conn.execute("UPDATE sites SET breaker_fails=0, enabled=0, breaker_until=? WHERE id=?",
                         (engine.now() + 6 * 3600, site_id))
            db.log("error", f"{site_id}: 3 неудачи подряд — сайт отключён на 6 часов", site_id)
        else:
            conn.execute("UPDATE sites SET breaker_fails=? WHERE id=?", (fails, site_id))


# ------------------------------------------------------------------ авто-режим


# ================================================================== авто-режим
# Робот работает в отдельном потоке: HTTP-запрос не ждёт, пока браузер пройдёт весь путь
# (а тем более пока человек решает капчу). Статус задачи виден в интерфейсе.
AUTO_LOCK = threading.Lock()          # одновременно — только один браузер
_FORCE_AUTO: set[int] = set()          # задачи, которые велено выполнить роботом принудительно
CAPTCHA_EVENTS: dict[int, threading.Event] = {}   # task_id -> «я решил капчу вручную»
CAPTCHA_EVENTS_LOCK = threading.Lock()

# Поле для ссылки: сначала точные признаки, потом общие.
LINK_SELECTORS = (
    "input[type=url]",
    "input[name*=link i]", "input[id*=link i]", "input[name*=url i]", "input[id*=url i]",
    "input[placeholder*=ссылк i]", "input[placeholder*=link i]", "input[placeholder*=url i]",
    "textarea[placeholder*=ссылк i]",
)
LINK_FALLBACK = ("form input[type=text]", "form textarea", "input[type=text]")

SUBMIT_HINTS = ("получить", "заказать", "отправить", "старт", "start", "get it", "submit",
                "бесплатн", "free", "отправ", "продолжить")

# Капча: сильные признаки (сама задача/картинка), затем слабые (виджет, поле).
CAPTCHA_STRONG = (
    "iframe[src*='recaptcha/api2/bframe']",
    "iframe[src*='hcaptcha'][title*='hallenge']",
    "iframe[title*='задач']",
    "iframe[title*='hallenge']",
    "input[name*='captcha' i]", "input[id*='captcha' i]",
    "img[src*='captcha' i]",
    "#captcha", ".captcha-image", "[class*='captcha-img']",
)
CAPTCHA_WEAK = (
    "iframe[src*='turnstile']",
    "div.g-recaptcha", "div.h-captcha", "[class*='captcha']",
)
CAPTCHA_TEXTS = ("я не робот", "подтвердите, что вы не робот", "введите код с картинки",
                 "проверка браузера", "проверяем ваш браузер", "verify you are human",
                 "i'm not a robot", "введите капчу", "не робот?")

SUCCESS_HINTS = ("заказ создан", "заказ принят", "заявка принята", "успешно", "готово",
                 "ожидайте", "в обработке", "выполняется", "спасибо", "order created",
                 "success", "queued", "in progress")


# ------------------------------------------------------------------ мелкие утилиты
def _visible(page, selector: str) -> bool:
    try:
        loc = page.locator(selector)
        return bool(loc.count() and loc.first.is_visible())
    except Exception:
        return False


def _body_text(page) -> str:
    try:
        return (page.inner_text("body") or "").lower()
    except Exception:
        try:
            return (page.content() or "").lower()
        except Exception:
            return ""


def _pause(page, ms: int) -> None:
    try:
        page.wait_for_timeout(ms)
    except Exception:
        time.sleep(ms / 1000)


def _screenshot(page, path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        SCREENS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path))
        return str(path)
    except Exception:
        return None


def _set_task(task_id: int, status: str, note: str | None = None) -> None:
    with db.tx() as conn:
        conn.execute("UPDATE tasks SET status=?, note=? WHERE id=?", (status, note, task_id))


# ------------------------------------------------------------------ распознавание
def detect_captcha(page) -> str | None:
    """Возвращает признак капчи (селектор или фразу) либо None."""
    for sel in CAPTCHA_STRONG:
        if _visible(page, sel):
            return sel
    for sel in CAPTCHA_WEAK:
        if _visible(page, sel):
            return sel
    text = _body_text(page)
    for phrase in CAPTCHA_TEXTS:
        if phrase in text:
            return phrase
    return None


def find_link_field(page) -> str | None:
    for sel in LINK_SELECTORS:
        if _visible(page, sel):
            return sel
    for sel in LINK_FALLBACK:
        if _visible(page, sel):
            return sel
    return None


def click_submit(page) -> bool:
    for txt in SUBMIT_HINTS:
        try:
            btn = page.get_by_role("button", name=txt).first
            if btn.count() and btn.is_visible():
                btn.click()
                return True
        except Exception:
            continue
    for sel in ("button[type=submit]", "input[type=submit]", "form button"):
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                loc.click()
                return True
        except Exception:
            continue
    return False


def looks_submitted(page) -> bool:
    text = _body_text(page)
    return any(h in text for h in SUCCESS_HINTS)


# ------------------------------------------------------------------ капча и человек
def mark_captcha(service_id: str, value: int, reason: str = "") -> None:
    """Запоминаем, есть ли на услуге капча (это влияет на режимы работы)."""
    with contextlib.suppress(Exception):
        db.set_service_override(service_id, captcha=value)
        tail = f" ({reason})" if reason else ""
        db.log("warn" if value else "info",
               f"Капча {'обнаружена' if value else 'не обнаружена'}: {service_id}{tail}")


def captcha_event(task_id: int) -> threading.Event:
    with CAPTCHA_EVENTS_LOCK:
        ev = CAPTCHA_EVENTS.get(task_id)
        if ev is None:
            ev = threading.Event()
            CAPTCHA_EVENTS[task_id] = ev
        return ev


def confirm_captcha_by_human(task_id: int) -> bool:
    """Кнопка «я решил капчу» в интерфейсе: будим робота, не дожидаясь авто-детекта."""
    captcha_event(task_id).set()
    db.log("info", "Капча подтверждена вручную", None, task_id)
    return True


def handle_captcha(page, svc: dict, task_id: int, headless: bool) -> tuple[bool, str]:
    """Отдаём капчу человеку. Возвращает (решено, пояснение).

    Окно браузера уже открыто и видно — человек решает капчу прямо в нём.
    Робот в это время ждёт: либо капча исчезнет с страницы, либо он получит
    подтверждение из интерфейса, либо истечёт captcha_wait_sec.
    """
    mark_captcha(svc["id"], 1, "робот увидел защиту")
    if headless:
        return False, ("капча, а окно браузера скрыто: включите видимое окно или режим «Все сайты», "
                       "чтобы решать капчу вручную")
    wait_sec = int(db.get_setting("captcha_wait_sec", "300") or 0)
    if wait_sec <= 0:
        return False, "капча не решена: ожидание отключено в настройках"

    ev = captcha_event(task_id)
    ev.clear()
    _set_task(task_id, "captcha", f"решите капчу в открытом окне — жду {wait_sec // 60} мин")
    db.log("warn", f"{svc['site_name']}: нужна капча — решите её в окне браузера",
           svc["site_id"], task_id)
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        if ev.is_set():
            _set_task(task_id, "running", "капча подтверждена вручную")
            return True, "капча подтверждена вручную"
        if detect_captcha(page) is None:
            _pause(page, 1200)          # капча исчезает не мгновенно — перепроверяем
            if detect_captcha(page) is None:
                _set_task(task_id, "running", "капча решена")
                db.log("info", f"{svc['site_name']}: капча решена, продолжаю", svc["site_id"], task_id)
                return True, "капча решена"
        time.sleep(1.5)
    return False, f"капча не решена за {wait_sec // 60} мин"


# ------------------------------------------------------------------ сценарий захода
def robot_flow(page, svc: dict, target_url: str, task_id: int, headless: bool) -> tuple[bool, str, str | None]:
    """Весь путь робота по странице. Вынесен отдельно — тестируется без браузера."""
    shot = SCREENS / f"{svc['site_id']}-{int(time.time())}-{task_id}.png"
    page.goto(svc["url"], wait_until="domcontentloaded")
    _pause(page, 1500)

    # 1) капча может встретиться ещё до ввода ссылки
    if detect_captcha(page):
        solved, why = handle_captcha(page, svc, task_id, headless)
        if not solved:
            return False, why, _screenshot(page, shot)

    # 2) поле для ссылки
    field = find_link_field(page)
    if not field:
        return False, "не нашёл поле для ссылки — проверьте страницу вручную или режим assisted", _screenshot(page, shot)
    page.locator(field).first.fill(target_url)

    # 3) кнопка
    if not click_submit(page):
        return False, "не нашёл кнопку отправки", _screenshot(page, shot)
    _pause(page, 2500)

    # 4) капча после отправки — самый частый случай
    if detect_captcha(page):
        solved, why = handle_captcha(page, svc, task_id, headless)
        if not solved:
            return False, why, _screenshot(page, shot)
        # после решения капчи форму часто нужно отправить повторно — но только если заказ ещё не принят
        if not looks_submitted(page):
            click_submit(page)
            _pause(page, 2000)

    # 5) таймер сайта
    _pause(page, int(svc["hold_seconds"]) * 1000)

    # 6) финальная проверка на защиту: если заявку уже приняли, капча на странице не отменяет заход
    if detect_captcha(page):
        solved, _why = handle_captcha(page, svc, task_id, headless)
        if not solved:
            if looks_submitted(page):
                return True, "заявка отправлена, но страница показывает защиту — проверьте вручную", _screenshot(page, shot)
            return False, "капча появилась в конце захода и осталась нерешённой", _screenshot(page, shot)

    ok = looks_submitted(page)
    mark = _screenshot(page, shot)
    if ok:
        return True, "робот отправил заявку", mark
    # заявку могли принять без явного текста — считаем успехом, если капчи нет и форма ушла
    return True, "робот завершил заход (успех не подтверждён текстом страницы)", mark


# ------------------------------------------------------------------ обвязка потока
def _headless_now() -> bool:
    """Нужно ли скрытое окно.

    В режиме «Без капчи» человек не участвует вообще — окно не показываем.
    В остальных режимах окно видимое (если не отключено в настройках), чтобы
    капчу можно было решить руками.
    """
    if engine.captcha_mode() == "nocaptcha":
        return True
    return db.get_setting("captcha_browser_visible", "1") != "1"


def _robot_settings() -> dict:
    """Настройки робота: движок, видимость окна, разрешено ли ждать таймер."""
    ps = engine.proxy_pool_settings()
    proxy = engine.next_proxy() if ps["enabled"] else None
    return {
        "prefer": db.get_setting("browser_backend", "auto") or "auto",
        "headless": _headless_now(),
        "allow_email": db.get_setting("order_email", "") or "",
        "nojs": (db.get_setting("nojs_enabled", "1") or "1") == "1",
        "dry_run": db.get_setting("dry_run", "0") == "1",
        "proxy": proxy,
        "proxy_enabled": ps["enabled"],
        "proxy_use_tor": ps["use_tor"],
    }


def _auto_worker(task_id: int, svc: dict, target_url: str) -> None:
    """Поток робота. Всегда освобождает блокировку и всегда оставляет задачу в понятном статусе."""
    try:
        from core import drivers          # noqa: F401 — проверяем, что модуль на месте
    except Exception as exc:
        with contextlib.suppress(RuntimeError):
            AUTO_LOCK.release()
        delivery.release_before_submit(task_id, "драйвер недоступен; заказ не отправлен")
        _after_auto(task_id, svc, False, f"драйверы недоступны: {exc}", None, count_fail=False)
        return

    cfg = _robot_settings()
    prefer = None if cfg["prefer"] in ("", "auto") else cfg["prefer"]
    allow_captcha = engine.captcha_mode() != "nocaptcha"
    res = None
    try:
        # быстрый путь: у площадки простая HTML-форма и робот уже доказал, что так работает
        mem = drivers._memory(svc["site_id"])
        if cfg["nojs"] and not cfg["dry_run"] and mem.get("nojs"):
            res = drivers.run_nojs(dict(svc), target_url)
            if res.verdict in ("bad_link", "quota", "ok"):
                pass
            elif res.verdict == "captcha" and db.get_setting("captcha_autosolve", "1") == "0":
                pass
            else:
                res = None           # капчу и другие проблемы пробуем в браузере
        if res is None:
            res = drivers.run(dict(svc), target_url, task_id=task_id, prefer=prefer,
                              dry_run=cfg["dry_run"], headless=cfg["headless"], allow_captcha_wait=allow_captcha,
                              deadline_sec=240, proxy=cfg.get("proxy"))
    except Exception as exc:
        res = None
        note = f"сбой драйвера: {str(exc)[:200]}"
    finally:
        with contextlib.suppress(RuntimeError):
            AUTO_LOCK.release()

    if res is None:
        if delivery.waiting(task_id, uncertain=True, note=note):
            return
        _after_auto(task_id, svc, False, note, None)
        return
    if cfg["dry_run"]:
        delivery.release_before_submit(task_id, "тестовый прогон: заказ не отправлен")
        _set_task(task_id, "failed", "тестовый прогон: заказ не отправлен и прирост не начислен")
        return
    _apply_driver_result(task_id, dict(svc), res, allow_captcha)


def _apply_driver_result(task_id: int, svc: dict, res, allow_captcha: bool) -> None:
    """Превращает вердикт драйвера в состояние задачи. Здесь вся «честность» отказов."""
    shot = res.shot
    detail = res.detail
    with db.rd() as conn:
        tracked = conn.execute("SELECT state FROM deliveries WHERE task_id=?", (task_id,)).fetchone()
    if tracked and tracked['state'] in ('waiting', 'uncertain', 'verified', 'abandoned'):
        return  # пользователь или монитор уже обработали эту заявку
    if res.ok:
        if delivery.waiting(task_id, note=detail):
            return
        _after_auto(task_id, svc, True, detail, shot)
        return
    # При неоднозначном ответе после клика заказ мог быть принят. Не запускаем другой сайт.
    clicked = any('нажал' in step or 'кнопка нажата' in step for step in (res.steps or []))
    if res.verdict not in ('quota', 'bad_link') and clicked:
        if delivery.waiting(task_id, uncertain=True, note=detail):
            return
    delivery.release_before_submit(task_id, detail)
    if res.verdict == "captcha":
        if allow_captcha:
            # Драйвер завершил сеанс: если ожидание не помогло, нужен новый заход.
            _set_task(task_id, "captcha", ("auto: " + detail)[:480] + (f" | скриншот: {shot}" if shot else ""))
            db.log("info", f"Капча: {svc['site_name']} / {svc['label']} — требуется ручной заход",
                   svc["site_id"], task_id)
            mark_captcha(svc["id"], 1, detail)
        else:
            _set_task(task_id, "failed", ("auto: капча в режиме «Без капчи» — " + detail)[:480])
            mark_captcha(svc["id"], 1, detail)
            db.log("info", f"Пропуск из-за капчи (режим «Без капчи»): {svc['site_name']}", svc["site_id"], task_id)
        return
    if res.verdict == "quota":
        # это не поломка: возвращаем задачу в очередь и откладываем площадку.
        # Драйвер может сказать точнее («завтра»), иначе — час.
        pause = int((res.learned or {}).get("retry_sec") or 3600)
        pause = max(300, min(pause, 26 * 3600))
        unit = "до завтра" if pause > 3600 else "на час"
        with db.tx() as conn:
            conn.execute("UPDATE tasks SET status='queued', due_at=?, started_at=NULL, note=? WHERE id=?",
                         (engine.now() + pause, ("auto: " + detail)[:400], task_id))
        db.log("info", f"Лимит площадки: {svc['site_name']} — заход отложен {unit}", svc["site_id"], task_id)
        return
    if res.verdict == "bad_link":
        _set_task(task_id, "failed", ("auto: " + detail)[:480] + (f" | скриншот: {shot}" if shot else ""))
        db.log("warn", f"Ссылка не подошла: {svc['site_name']} / {svc['label']} — {detail}",
               svc["site_id"], task_id)
        return
    if res.verdict == "site_broken":
        _set_task(task_id, "failed", ("auto: " + detail)[:480] + (f" | скриншот: {shot}" if shot else ""))
        mark_fail(svc["site_id"])
        db.log("warn", f"Площадка не отдала форму: {svc['site_name']} — {detail}", svc["site_id"], task_id)
        return
    # need_human и всё остальное
    _after_auto(task_id, svc, False, detail, shot, count_fail=False)


def _after_auto(task_id: int, svc: dict, ok: bool, note: str, shot: str | None,
                count_fail: bool = True) -> None:
    with CAPTCHA_EVENTS_LOCK:
        CAPTCHA_EVENTS.pop(task_id, None)
    try:
        if ok:
            db.log("info", f"Авто-заход выполнен: {svc['site_name']} / {svc['label']} — {note}",
                   svc["site_id"], task_id)
            finish_task(task_id, True, note="auto: " + note, units=int(svc["amount"]))
            if "капча" not in note:
                # капчи не встретили — помечаем услугу как безопасную для авто-режима
                with db.rd() as conn:
                    cur = conn.execute("SELECT captcha FROM services WHERE id=?", (svc["id"],)).fetchone()
                if cur is not None and int(cur["captcha"]) == -1:
                    mark_captcha(svc["id"], 0, "робот прошёл без защиты")
            return
        _set_task(task_id, "failed", ("auto: " + note)[:500] + (f" | скриншот: {shot}" if shot else ""))
        if count_fail:
            mark_fail(svc["site_id"])
        db.log("warn", f"Авто-заход не удался: {svc['site_name']} / {svc['label']} — {note}",
               svc["site_id"], task_id)
    except Exception as exc:
        with contextlib.suppress(Exception):
            _set_task(task_id, "failed", f"auto: сбой обработки результата — {exc}")


def auto_submit(svc, target_url: str, task_id: int = 0) -> tuple[bool, str, str | None]:
    """Синхронный одиночный прогон робота (для отладки, тестов и кнопки «роботом сейчас»)."""
    try:
        from core import drivers
    except Exception as exc:
        return False, f"драйверы недоступны: {exc}", None
    cfg = _robot_settings()
    prefer = None if cfg["prefer"] in ("", "auto") else cfg["prefer"]
    res = drivers.run(dict(svc), target_url, task_id=task_id, prefer=prefer,
                      headless=cfg["headless"],
                      allow_captcha_wait=engine.captcha_mode() != "nocaptcha", deadline_sec=240)
    return res.ok, f"[{res.verdict}] {res.detail}", res.shot
