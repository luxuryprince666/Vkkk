# -*- coding: utf-8 -*-
"""Слой хранения: SQLite с WAL, миграциями и ежедневным бэкапом.

Гарантии отказоустойчивости:
  * WAL + busy_timeout — параллельные чтения не блокируют запись;
  * все записи в транзакциях, состояние меняется только через атомарные UPDATE ... WHERE status=?;
  * схема создаётся идемпотентно, версия хранится в PRAGMA user_version;
  * при старте состояние «running» из прошлой сессии чинится (см. recover());
  * бэкап базы раз в сутки, хранится 5 последних копий.
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

BASE = Path(__file__).resolve().parent.parent
# DATA можно переопределить переменной окружения — удобно для тестов и портативных сборок
DATA = Path(os.environ.get("VKFREE_DATA") or (BASE / "data"))
LOGS = BASE / "logs"
DB_PATH = DATA / "app.db"
CATALOG_PATH = DATA / "catalog.json"
BACKUP_DIR = DATA / "backups"
if not CATALOG_PATH.exists():
    CATALOG_PATH = BASE / "data" / "catalog.json"

# Сайты, где бесплатный «слот» один на весь сайт (а не на каждую услугу отдельно).
# Обоснование: SMM.media прямо пишет «любая ОДНА услуга на выбор 1 раз в 20 минут».
SHARED_SLOT_SITES = {"likeboom"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    home            TEXT,
    free_page       TEXT,
    platforms       TEXT NOT NULL DEFAULT '[]',
    reg_required    INTEGER NOT NULL DEFAULT 0,
    verified        TEXT,
    mode            TEXT NOT NULL DEFAULT 'assisted',   -- assisted | auto | manual
    enabled         INTEGER NOT NULL DEFAULT 1,
    notes           TEXT,
    breaker_fails   INTEGER NOT NULL DEFAULT 0,
    breaker_until   INTEGER NOT NULL DEFAULT 0,
    updated_at      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS services (
    id              TEXT PRIMARY KEY,
    site_id         TEXT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    platform        TEXT NOT NULL,
    stype           TEXT NOT NULL,
    subject         TEXT NOT NULL DEFAULT 'any',   -- что накручиваем: community|person|post|any
    label           TEXT NOT NULL,
    amount          INTEGER NOT NULL,
    cooldown_min    INTEGER NOT NULL DEFAULT 0,
    max_claims_day  INTEGER,
    hold_seconds    INTEGER NOT NULL DEFAULT 60,
    url             TEXT NOT NULL,
    confirmed       INTEGER NOT NULL DEFAULT 1,
    enabled         INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_services_site ON services(site_id);
CREATE INDEX IF NOT EXISTS idx_services_platform ON services(platform, stype);

CREATE TABLE IF NOT EXISTS targets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    platform    TEXT NOT NULL,
    url         TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT '',          -- community|person|post|'' (не указано)
    created_at  INTEGER NOT NULL,
    UNIQUE(url, platform)
);

CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id      TEXT NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    target_id       INTEGER REFERENCES targets(id) ON DELETE SET NULL,
    status          TEXT NOT NULL DEFAULT 'queued',
    planned_units   INTEGER NOT NULL DEFAULT 0,
    claimed_units   INTEGER NOT NULL DEFAULT 0,
    attempts        INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL,
    started_at      INTEGER,
    due_at          INTEGER,
    finished_at     INTEGER,
    note            TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, due_at);
CREATE INDEX IF NOT EXISTS idx_tasks_service ON tasks(service_id, finished_at);

-- Незавершённый заказ блокирует следующий на тот же объект и метрику.
CREATE TABLE IF NOT EXISTS deliveries (
    task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE RESTRICT,
    target_id INTEGER NOT NULL REFERENCES targets(id) ON DELETE RESTRICT,
    target_key TEXT NOT NULL,
    metric TEXT NOT NULL,
    expected INTEGER NOT NULL,
    baseline INTEGER,
    last_count INTEGER,
    state TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    checked_at INTEGER,
    confirm_hits INTEGER NOT NULL DEFAULT 0,
    next_check_at INTEGER,
    note TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_active ON deliveries(target_key, metric)
    WHERE state IN ('reserved','submitting');

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    level   TEXT NOT NULL,
    site_id TEXT,
    task_id INTEGER,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------- площадки: что мы про них знаем
CREATE TABLE IF NOT EXISTS site_profiles (
    site_id       TEXT PRIMARY KEY REFERENCES sites(id) ON DELETE CASCADE,
    automation    TEXT NOT NULL DEFAULT 'C',        -- A | B | C
    captcha       TEXT NOT NULL DEFAULT '[]',       -- JSON: какие капчи встречены
    hold_seconds  INTEGER NOT NULL DEFAULT 0,       -- сколько ждать таймер/очередь перед заказом
    keep_open     INTEGER NOT NULL DEFAULT 0,       -- 1 = страницу нельзя закрывать до подтверждения
    pool_daily    INTEGER,                          -- суточный пул на всех посетителей (NULL = нет)
    profile_json  TEXT NOT NULL DEFAULT '{}',       -- селекторы, тексты успеха/провала, заметки
    degraded      INTEGER NOT NULL DEFAULT 0,       -- 1 = форма не совпала, задачи не планируем
    degraded_note TEXT,
    checked_at    INTEGER NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------- цели: «получи N единиц за срок»
CREATE TABLE IF NOT EXISTS goals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id    INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
    title        TEXT,
    deadline_at  INTEGER NOT NULL,
    pace         TEXT NOT NULL DEFAULT 'human',     -- human | max | quiet
    status       TEXT NOT NULL DEFAULT 'planned',   -- planned|running|paused|done|failed
    created_at   INTEGER NOT NULL,
    closed_at    INTEGER,
    note         TEXT
);
CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status, deadline_at);

CREATE TABLE IF NOT EXISTS goal_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id      INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    metric       TEXT NOT NULL,                     -- likes|views|followers|plays|reposts|comments|friends
    need         INTEGER NOT NULL,
    have_before  INTEGER NOT NULL DEFAULT 0,
    planned      INTEGER NOT NULL DEFAULT 0,        -- сколько единиц запланировано задачами
    done         INTEGER NOT NULL DEFAULT 0,        -- сколько подтверждено
    status       TEXT NOT NULL DEFAULT 'open'       -- open|done|short
);
CREATE INDEX IF NOT EXISTS idx_goal_items ON goal_items(goal_id);
"""

DEFAULT_SETTINGS = {
    "full_auto": "0",             # опциональный полностью автоматический последовательный режим
    "autopilot": "1",             # авто-исполнение задач по расписанию (запустил — и работает)
    "auto_open": "1",             # автопилоту разрешено самому открывать assisted-задачи
    "work_hours": "8-23",         # в какие часы разрешены заходы по местному времени
    "global_min_gap_sec": "20",   # минимальный интервал между любыми заходами
    "daily_total_limit": "0",     # 0 = без глобального лимита заходов в сутки
    "open_browser": "1",          # открывать страницу в браузере при запуске задачи
    "dry_run": "0",               # 1 = ничего не открывать/не отправлять, только логировать
    "captcha_mode": "all",        # all | nocaptcha | captcha — какие сайты вообще трогаем
    "captcha_wait_sec": "300",    # сколько ждём, пока вы решите капчу в открытом окне
    "captcha_browser_visible": "1",  # показывать окно браузера, чтобы можно было вмешаться
    "theme": "light",           # light | dark
    "strict_subject": "1",      # 1 = не крутить услугу на объекте, для которого она не предназначена
    # ---- робот (этап M3)
    "browser_backend": "auto",  # auto | patchright | camoufox | nodriver | playwright
    "nojs_enabled": "1",        # 1 = пробовать быстрый путь без браузера (простые формы)
    "order_email": "",          # e-mail для площадок, которые его требуют (не ВК!)
    # ---- капчи (решаем сами, без платных сервисов)
    "captcha_autosolve": "1",   # 1 = пробовать решать капчу автоматически (галочка/OCR)
    "captcha_ocr": "1",         # 1 = распознавать текстовые капчи с картинки (ddddocr)
}

# Колонки, добавленные после первой версии схемы (миграция выполняется на лету).
SERVICE_EXTRA_COLS = {
    "subject": "TEXT NOT NULL DEFAULT 'any'",    # что накручиваем: community|person|post|any
    "cooldown_override": "INTEGER",              # ручной кулдаун, минуты (NULL = из каталога)
    "amount_override": "INTEGER",                # ручной объём за заход
    "max_claims_override": "INTEGER",            # ручной лимит заходов в сутки
    "captcha": "INTEGER NOT NULL DEFAULT -1",    # -1 не проверено, 0 нет капчи, 1 есть капча
}
SITE_EXTRA_COLS = {
    "captcha": "INTEGER NOT NULL DEFAULT -1",
}
# Объект накрутки появился позже: у старых услуг он не определён ('any' = не разделяет).
TARGET_EXTRA_COLS = {
    "kind": "TEXT NOT NULL DEFAULT ''",
}
TASK_EXTRA_COLS = {
    "goal_id": "INTEGER",          # к какой цели относится заход (NULL = обычная накрутка)
    "metric": "TEXT",              # метрика цели: likes|views|followers|... (для быстрых отчётов)
}
SCHEMA_VERSION = 5


def connect() -> sqlite3.Connection:
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


_local = threading.local()   # соединение текущей транзакции в этом потоке


@contextlib.contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """Транзакция: commit при успехе, rollback при исключении.

    Активное соединение запоминается в thread-local, чтобы log() внутри транзакции
    писал через него же и никогда не вставал во вторую блокировку (иначе — дедлок на 30 с).
    """
    conn = connect()
    prev = getattr(_local, "conn", None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _local.conn = conn
        yield conn
        _local.conn = prev
        conn.execute("COMMIT")
    except Exception:
        _local.conn = prev
        with contextlib.suppress(Exception):
            conn.execute("ROLLBACK")
        raise
    finally:
        _local.conn = prev
        conn.close()


@contextlib.contextmanager
def rd() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def _ensure_columns(conn: sqlite3.Connection, table: str, cols: dict[str, str]) -> None:
    """Идемпотентная миграция: добавляет недостающие колонки, не трогая данные."""
    have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, decl in cols.items():
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def init_db() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    # executescript сам управляет транзакцией, поэтому схему создаём отдельным соединением
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        # Миграция: если индекс был создан со старым WHERE (включая 'waiting','uncertain'),
        # пересоздаём его с новым — блокируем только 'reserved' и 'submitting'.
        # Это нужно для FULL AUTO MODE: после принятия заказа (state='waiting')
        # следующая задача по той же метрике должна poter стартовать.
        try:
            rows = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='idx_delivery_active'"
            ).fetchall()
            for row in rows:
                sql = (row[0] or "").lower()
                if "waiting" in sql and "uncertain" in sql:
                    conn.execute("DROP INDEX IF EXISTS idx_delivery_active")
                    conn.execute("""CREATE UNIQUE INDEX idx_delivery_active
                                    ON deliveries(target_key, metric)
                                    WHERE state IN ('reserved','submitting')""")
                    break
        except Exception:
            pass
    finally:
        conn.close()
    with tx() as conn:
        _ensure_columns(conn, "services", SERVICE_EXTRA_COLS)
        _ensure_columns(conn, "sites", SITE_EXTRA_COLS)
        _ensure_columns(conn, "targets", TARGET_EXTRA_COLS)
        _ensure_columns(conn, "tasks", TASK_EXTRA_COLS)
        _ensure_columns(conn, "deliveries", {"confirm_hits": "INTEGER NOT NULL DEFAULT 0"})
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?,?)", (k, v))
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    seed_catalog()
    try:                       # профили площадок (research/sites.json); без него просто нет данных
        from core import profiles
        profiles.seed_profiles()
    except Exception:
        pass
    recover()
    backup_db()


def _insert_event(conn: sqlite3.Connection, now: int, level: str, message: str,
                  site_id: str | None, task_id: int | None) -> None:
    conn.execute("INSERT INTO events(ts, level, site_id, task_id, message) VALUES(?,?,?,?,?)",
                 (now, level, site_id, task_id, message[:2000]))
    # держим таблицу событий в разумных размерах
    conn.execute("DELETE FROM events WHERE id < (SELECT MAX(id) - 20000 FROM events)")


def log(level: str, message: str, site_id: str | None = None, task_id: int | None = None) -> None:
    """Журнал событий. Никогда не роняет приложение и никогда не ждёт чужую блокировку."""
    now = int(time.time())
    conn = getattr(_local, "conn", None)      # уже внутри транзакции — пишем её же соединением
    if conn is not None:
        try:
            _insert_event(conn, now, level, message, site_id, task_id)
            return
        except Exception:
            pass
    try:
        with tx() as c:
            _insert_event(c, now, level, message, site_id, task_id)
        return
    except Exception:
        pass
    # база недоступна — не теряем запись, кладём в файл
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        with open(LOGS / "events-fallback.log", "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {level.upper():5} {message}\n")
    except Exception:
        pass


def seed_catalog(force: bool = False) -> dict:
    """Загружает каталог в БД. Пользовательские настройки (enabled/mode) не перетираются."""
    if not CATALOG_PATH.exists():
        raise FileNotFoundError(f"Нет файла каталога: {CATALOG_PATH}")
    cat = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    now = int(time.time())
    sites_n = svcs_n = 0
    with tx() as conn:
        for s in cat["sites"]:
            row = conn.execute("SELECT id, enabled, mode FROM sites WHERE id=?", (s["id"],)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO sites(id,name,home,free_page,platforms,reg_required,verified,mode,enabled,notes,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (s["id"], s["name"], s.get("home"), s.get("free_page"),
                     json.dumps(s.get("platforms", []), ensure_ascii=False), int(bool(s.get("reg_required"))),
                     s.get("verified"), s.get("mode", "assisted"), int(bool(s.get("enabled", True))),
                     s.get("notes"), now),
                )
                sites_n += 1
            else:
                conn.execute(
                    "UPDATE sites SET name=?, home=?, free_page=?, platforms=?, reg_required=?, verified=?, notes=?, updated_at=?"
                    " WHERE id=?",
                    (s["name"], s.get("home"), s.get("free_page"),
                     json.dumps(s.get("platforms", []), ensure_ascii=False), int(bool(s.get("reg_required"))),
                     s.get("verified"), s.get("notes"), now, s["id"]),
                )
                if force:
                    conn.execute("UPDATE sites SET enabled=?, mode=? WHERE id=?",
                                 (int(bool(s.get("enabled", True))), s.get("mode", "assisted"), s["id"]))

            for v in s.get("services", []):
                conn.execute(
                    "INSERT INTO services(id,site_id,platform,stype,subject,label,amount,cooldown_min,max_claims_day,hold_seconds,url,confirmed)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(id) DO UPDATE SET site_id=excluded.site_id, platform=excluded.platform,"
                    " stype=excluded.stype, subject=excluded.subject, label=excluded.label, amount=excluded.amount,"
                    " cooldown_min=excluded.cooldown_min, max_claims_day=excluded.max_claims_day,"
                    " hold_seconds=excluded.hold_seconds, url=excluded.url, confirmed=excluded.confirmed",
                    (v["id"], s["id"], v["platform"], v["type"], v.get("subject") or "any",
                     v.get("label") or v["id"], int(v["amount"]),
                     int(v.get("cooldown_min", 0)), v.get("max_claims_day"), int(v.get("hold_seconds", 60)),
                     v["url"], int(bool(v.get("confirmed", True)))),
                )
                svcs_n += 1

        # Каталог — источник истины: чего в нём больше нет, того не должно быть и в базе.
        # Иначе убранный сайт продолжает висеть в интерфейсе и в очередь попадают мёртвые услуги.
        keep_sites = {s["id"] for s in cat["sites"]}
        keep_svcs = {v["id"] for s in cat["sites"] for v in s.get("services", [])}
        gone_svcs = [r[0] for r in conn.execute("SELECT id FROM services").fetchall() if r[0] not in keep_svcs]
        gone_sites = [r[0] for r in conn.execute("SELECT id FROM sites").fetchall() if r[0] not in keep_sites]
        for sid in gone_svcs:
            conn.execute("DELETE FROM services WHERE id=?", (sid,))     # задачи уходят каскадом
        for sid in gone_sites:
            conn.execute("DELETE FROM sites WHERE id=?", (sid,))
    removed = {"services": len(gone_svcs), "sites": len(gone_sites)}
    return {"sites": sites_n, "services": svcs_n, "version": cat.get("meta", {}).get("version"),
            **removed}


def recover() -> None:
    """Чинит состояние после аварийного завершения.

    * задачи, висевшие в running, возвращаются в ready (пользователь ничего не потерял);
    * задачи wait_confirm не трогаем — таймер считается по started_at;
    * снимаем «зависшие» breaker-ы, у которых истёк срок.
    """
    now = int(time.time())
    with tx() as conn:
        # Запуск мог отправить заявку до сбоя. Не повторяем без проверки человеком.
        conn.execute("UPDATE deliveries SET state='uncertain', note='перезапуск во время отправки', next_check_at=? "
                     "WHERE state='submitting'", (now,))
        conn.execute("UPDATE tasks SET status='delivery_uncertain', note='перезапуск во время заказа; проверьте площадку' "
                     "WHERE id IN (SELECT task_id FROM deliveries WHERE state='uncertain') AND status IN ('running','captcha','wait_confirm')")
        conn.execute("UPDATE deliveries SET state='released', note='перезапуск до отправки' WHERE state='reserved'")
        n = conn.execute("UPDATE tasks SET status='ready' WHERE status='running' AND id NOT IN "
                         "(SELECT task_id FROM deliveries WHERE state IN ('waiting','uncertain'))").rowcount
        conn.execute("UPDATE sites SET breaker_fails=0, breaker_until=0 WHERE breaker_until>0 AND breaker_until<?", (now,))
    if n:
        log("warn", f"После перезапуска восстановлено задач: {n}")


def backup_db(keep: int = 5) -> str | None:
    """Копия базы раз в сутки (имя по дате), старые чистятся."""
    if not DB_PATH.exists():
        return None
    stamp = datetime.now().strftime("%Y-%m-%d")
    dst = BACKUP_DIR / f"app-{stamp}.db"
    if dst.exists():
        return str(dst)
    try:
        src = connect()
        dst_conn = sqlite3.connect(str(dst))
        with dst_conn:
            src.backup(dst_conn)
        dst_conn.close()
        src.close()
        files = sorted(BACKUP_DIR.glob("app-*.db"))
        for old in files[:-keep]:
            old.unlink(missing_ok=True)
        return str(dst)
    except Exception as exc:  # бэкап не должен мешать запуску
        log("warn", f"Бэкап не удался: {exc}")
        return None


# ------------------------------------------------------------------ настройки
def get_setting(key: str, default: str = "") -> str:
    with rd() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def get_settings() -> dict[str, str]:
    with rd() as conn:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}


def set_setting(key: str, value: Any) -> None:
    with tx() as conn:
        conn.execute("INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, str(value)))


def export_csv() -> str:
    """Экспорт истории выполненных задач в CSV (для Excel)."""
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["id", "сайт", "услуга", "тип", "объект услуги", "площадка", "ссылка", "объект ссылки",
                "единиц", "статус", "создана", "завершена"])
    with rd() as conn:
        rows = conn.execute("""
            SELECT t.id, s.name AS site, v.label, v.stype, v.subject, v.platform, tg.url, tg.kind,
                   t.claimed_units, t.status, t.created_at, t.finished_at
            FROM tasks t JOIN services v ON v.id=t.service_id JOIN sites s ON s.id=v.site_id
            LEFT JOIN targets tg ON tg.id=t.target_id
            ORDER BY t.id DESC LIMIT 5000
        """).fetchall()
    subj = {"community": "сообщество", "person": "личная страница", "post": "запись", "any": "любой"}
    for r in rows:
        kind = (r["kind"] or "").strip()
        kind_txt = subj.get(kind, "") if kind else ""
        w.writerow([r["id"], r["site"], r["label"], r["stype"], subj.get(r["subject"] or "any", ""),
                    r["platform"], r["url"], kind_txt,
                    r["claimed_units"], r["status"],
                    datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d %H:%M") if r["created_at"] else "",
                    datetime.fromtimestamp(r["finished_at"]).strftime("%Y-%m-%d %H:%M") if r["finished_at"] else ""])
    path = BASE / "exports" / f"history-{datetime.now().strftime('%Y%m%d-%H%M')}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(buf.getvalue(), encoding="utf-8-sig")
    return str(path)


# ------------------------------------------------------------ ручные настройки
def set_service_override(service_id: str, cooldown_min: int | None = "keep",
                         amount: int | None = "keep", max_claims_day: int | None = "keep",
                         captcha: int | None = "keep") -> dict:
    """Правит ручные значения услуги. Значение None = «вернуть как в каталоге».

    «keep» (по умолчанию) — не трогать это поле.
    """
    fields, params = [], []
    mapping = (("cooldown_override", cooldown_min), ("amount_override", amount),
               ("max_claims_override", max_claims_day))
    for col, val in mapping:
        if val != "keep":
            fields.append(f"{col}=?")
            params.append(None if val is None else int(val))
    if captcha != "keep":
        fields.append("captcha=?")
        params.append(int(captcha))
    if not fields:
        return {"changed": 0}
    with tx() as conn:
        row = conn.execute("SELECT id FROM services WHERE id=?", (service_id,)).fetchone()
        if not row:
            raise ValueError("Услуга не найдена")
        conn.execute(f"UPDATE services SET {', '.join(fields)} WHERE id=?", (*params, service_id))
    return {"changed": 1}


def set_site_captcha(site_id: str, captcha: int) -> dict:
    """Помечает ВСЕ услуги сайта как «капча есть / нет / не проверено»."""
    with tx() as conn:
        cur = conn.execute("UPDATE services SET captcha=? WHERE site_id=?", (int(captcha), site_id))
        conn.execute("UPDATE sites SET captcha=? WHERE id=?", (int(captcha), site_id))
        return {"updated": cur.rowcount}


def set_site_cooldown(site_id: str, cooldown_min: int | None) -> dict:
    """Ставит один кулдаун на все услуги сайта (None — вернуть каталожные значения)."""
    with tx() as conn:
        cur = conn.execute("UPDATE services SET cooldown_override=? WHERE site_id=?",
                           (None if cooldown_min is None else int(cooldown_min), site_id))
        return {"updated": cur.rowcount}


def reset_overrides(site_id: str | None = None) -> dict:
    """Сбрасывает все ручные правки (по сайту или целиком)."""
    with tx() as conn:
        if site_id:
            cur = conn.execute(
                "UPDATE services SET cooldown_override=NULL, amount_override=NULL, max_claims_override=NULL"
                " WHERE site_id=?", (site_id,))
        else:
            cur = conn.execute(
                "UPDATE services SET cooldown_override=NULL, amount_override=NULL, max_claims_override=NULL")
        return {"updated": cur.rowcount}
