# -*- coding: utf-8 -*-
"""HTTP-сервер на стандартной библиотеке (без Flask и pip).

Почему так: приложение должно запускаться на любой машине одной командой, без
установки зависимостей и без проблем «а у меня другая версия пакета».

Отказоустойчивость:
  * ThreadingHTTPServer — зависший запрос не блокирует остальные;
  * любое исключение обработчика превращается в JSON-ошибку 500, сервер живёт дальше;
  * фоновый поток-планировщик защищён от падений и пишет причину в лог;
  * все ответы с no-store, чтобы UI не показывал устаревшие лимиты.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import db, delivery, drivers, engine, executor, goals, profiles, stealth, vkstats

WEB = db.BASE / "web"
MAX_BODY = 1_000_000

DOMAIN_RULES = {
    "vk": ("vk.com", "vk.ru", "vk.me"),
    "telegram": ("t.me", "telegram.me", "telegram.org"),
    "instagram": ("instagram.com", "instagr.am"),
    "tiktok": ("tiktok.com",),
    "youtube": ("youtube.com", "youtu.be"),
    "twitch": ("twitch.tv",),
    "twitter": ("twitter.com", "x.com"),
    "facebook": ("facebook.com", "fb.com"),
    "ok": ("ok.ru", "odnoklassniki.ru"),
    "max": ("max.ru", "oneme.ru"),
    "rutube": ("rutube.ru",),
    "dzen": ("dzen.ru", "zen.yandex.ru"),
    "likee": ("likee.video", "likee.com"),
    "threads": ("threads.net",),
    "soundcloud": ("soundcloud.com",),
    "traffic": ("http",),
}


def validate_target(platform: str, url: str) -> str:
    url = (url or "").strip()
    if not re.match(r"^https?://", url):
        raise ValueError("Ссылка должна начинаться с http:// или https://")
    if " " in url or len(url) > 500:
        raise ValueError("В ссылке есть пробелы или она слишком длинная")
    host = (urlparse(url).hostname or "").lower()
    if not host:
        raise ValueError("В ссылке нет домена")
    allowed = DOMAIN_RULES.get(platform)
    # строго: домен совпадает целиком или является поддоменом. Никаких startswith —
    # иначе https://vk.com.evil.example/ проходил бы как «свой».
    if allowed and not any(host == d or host.endswith("." + d) for d in allowed):
        raise ValueError(f"Для площадки «{platform}» ожидается домен {', '.join(allowed[:2])}…")
    return url


def _goal_items(b: dict) -> list[dict]:
    """Позиции цели из запроса: [{metric, need, have_before}]."""
    raw = b.get("items")
    if not raw or not isinstance(raw, list):
        raise ValueError("Не указано, что накручиваем: нужен список items")
    out = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        metric = (it.get("metric") or "").strip()
        if not metric:
            continue
        have_raw = it.get("have_before", it.get("have"))
        have = None if have_raw in (None, "") else int(have_raw)
        out.append({"metric": metric, "need": int(it.get("need") or 0), "have_before": have})
    if not out:
        raise ValueError("Не указано, что накручиваем: нужен хотя бы один пункт")
    return out


def _req_int(body: dict, key: str, human: str = "ID") -> int:
    """Целое из тела запроса с человеческой ошибкой (а не 500 из-за int(None))."""
    val = body.get(key)
    if val is None or val == "":
        raise ValueError(f"{human}: не передано")
    try:
        return int(val)
    except (TypeError, ValueError):
        raise ValueError(f"{human}: нужно целое число")


def _target_kind_arg(body: dict, platform: str, url: str, current: str = "") -> str:
    """Разбирает поле kind из запроса. '' = определить автоматически."""
    if "kind" not in body:
        return (current or "").strip()
    kind = (body.get("kind") or "").strip()
    if kind in ("", "auto"):
        return ""
    if kind not in engine.TARGET_KINDS:
        raise ValueError("Объект ссылки: community (сообщество), person (личная страница), "
                         "post (запись) или auto")
    return kind


def _opt_int(body: dict, key: str, lo: int, hi: int, human: str) -> int | None:
    """Достаёт из тела целое число в диапазоне. None/пусто => None (сброс к каталогу)."""
    if key not in body:
        return "keep"           # не трогать это поле
    val = body[key]
    if val is None or val == "":
        return None             # явный сброс
    try:
        num = int(val)
    except (TypeError, ValueError):
        raise ValueError(f"{human}: нужно целое число")
    if not (lo <= num <= hi):
        raise ValueError(f"{human}: допустимо от {lo} до {hi}")
    return num


BOOL_SETTINGS = ("autopilot", "auto_open", "open_browser", "dry_run",
                 "captcha_browser_visible", "strict_subject", "nojs_enabled",
                 "captcha_autosolve", "captcha_ocr", "full_auto")
BACKEND_CHOICES = ("auto", "patchright", "camoufox", "nodriver", "playwright")
NUM_SETTINGS = {"global_min_gap_sec": (0, 3600), "daily_total_limit": (0, 100000),
                "captcha_wait_sec": (30, 3600)}


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение: 1 сайт, 2 сайта, 5 сайтов."""
    n = abs(int(n))
    a, b = n % 100, n % 10
    if 10 < a < 20:
        return many
    if 1 < b < 5:
        return few
    if b == 1:
        return one
    return many


def _clean_settings(body: dict) -> dict:
    """Проверяет настройки перед записью: только известные ключи и осмысленные значения.

    Без этого один опечатанный запрос мог положить в базу мусор, который потом
    роняет интерфейс (например, theme='purple' или autopilot='может быть').
    """
    if not isinstance(body, dict):
        raise ValueError("Ожидался объект с настройками")
    clean: dict[str, str] = {}
    for key, val in body.items():
        if key not in db.DEFAULT_SETTINGS:
            raise ValueError(f"Неизвестная настройка: {key}")
        if key in BOOL_SETTINGS:
            if isinstance(val, str):
                low = val.strip().lower()
                if low in ("1", "true", "yes", "on", "да"):
                    clean[key] = "1"
                elif low in ("0", "false", "no", "off", "нет", ""):
                    clean[key] = "0"
                else:
                    raise ValueError(f"{key}: ожидается 0 или 1")
            else:
                clean[key] = "1" if val else "0"
        elif key in NUM_SETTINGS:
            lo, hi = NUM_SETTINGS[key]
            try:
                num = int(val)
            except (TypeError, ValueError):
                raise ValueError(f"{key}: нужно целое число")
            if not (lo <= num <= hi):
                raise ValueError(f"{key}: допустимо от {lo} до {hi}")
            clean[key] = str(num)
        elif key == "captcha_mode":
            if val not in engine.CAPTCHA_MODES:
                raise ValueError("Режим капчи: all, nocaptcha или captcha")
            clean[key] = str(val)
        elif key == "theme":
            if val not in ("light", "dark"):
                raise ValueError("Тема: light или dark")
            clean[key] = str(val)
        elif key == "browser_backend":
            if val not in BACKEND_CHOICES:
                raise ValueError("Движок: auto, patchright, camoufox, nodriver или playwright")
            clean[key] = str(val)
        elif key == "order_email":
            email = str(val or "").strip()
            if email and ("@" not in email or " " in email or len(email) > 120):
                raise ValueError("E-mail для заказов: похоже на опечатку")
            clean[key] = email
        elif key == "work_hours":
            spec = str(val or "").strip()
            try:
                a, b_ = [int(x) for x in spec.split("-")]
            except Exception:
                raise ValueError("Рабочие часы: формат 8-23")
            if not (0 <= a <= 24 and 0 <= b_ <= 24):
                raise ValueError("Рабочие часы: часы от 0 до 24")
            clean[key] = f"{a}-{b_}"
        else:
            clean[key] = str(val)
    if not clean:
        raise ValueError("Пустой запрос: нечего сохранять")
    return clean


class Handler(BaseHTTPRequestHandler):
    server_version = "VKFree/1.0"
    protocol_version = "HTTP/1.1"

    # ---------------------------------------------------------------- утилиты
    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8",
              extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"))

    def _error(self, msg: str, code: int = 400) -> None:
        self._json({"ok": False, "error": msg}, code)

    def _body(self) -> dict:
        """Тело запроса. Читается ровно один раз и кэшируется.

        Важно: тело нужно дочитать даже маршрутам, которым оно не нужно. Иначе его остаток
        остаётся в сокете и следующий запрос по тому же соединению превращается в мусор
        вида «{}GET /api/state» — браузер получает 501 и решает, что связь потеряна.
        """
        cached = getattr(self, "_cached_body", None)
        if cached is not None:
            return cached
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._cached_body = {}
            return self._cached_body
        if length > MAX_BODY:
            self._cached_body = {}
            raise ValueError("Слишком большое тело запроса")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            value = None
            err = exc
        if value is None:
            self._cached_body = {}
            raise ValueError(f"Некорректный JSON: {err}")
        if not isinstance(value, dict):
            self._cached_body = {}
            raise ValueError("Тело запроса должно быть объектом JSON")
        self._cached_body = value
        return value

    def log_message(self, fmt, *args):  # тише в консоли, но с записью в файл
        return

    # ---------------------------------------------------------------- роутинг
    def handle_one_request(self) -> None:
        """Сбрасываем кэш тела перед каждым запросом.

        Экземпляр Handler живёт всё время keep-alive соединения, поэтому без сброса
        второй POST на том же сокете получал тело первого, а своё оставлял в буфере.
        """
        self._cached_body = None
        super().handle_one_request()

    def do_GET(self):
        try:
            self._route("GET")
        except ValueError as exc:
            # «не найдено», «неверный параметр» — это 400, а не падение сервера
            self._error(str(exc), 400)
        except Exception:
            traceback.print_exc()
            self._error("Внутренняя ошибка сервера", 500)

    def do_POST(self):
        try:
            self._body()          # дочитываем тело, даже если маршрут его не использует
            self._route("POST")
        except ValueError as exc:
            self._error(str(exc), 400)
        except Exception:
            traceback.print_exc()
            self._error("Внутренняя ошибка сервера", 500)

    def do_HEAD(self):
        """Некоторые прокси и браузеры проверяют доступность страницы HEAD-запросом."""
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
        except Exception:
            pass

    def do_DELETE(self):
        try:
            self._body()
            self._route("DELETE")
        except ValueError as exc:
            self._error(str(exc), 400)
        except Exception:
            traceback.print_exc()
            self._error("Внутренняя ошибка сервера", 500)

    def _route(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        # ---------- статика
        if method == "GET" and (path == "/" or path.startswith("/static")):
            rel = "index.html" if path == "/" else path[len("/static/"):]
            f = (WEB / rel).resolve()
            root = str(WEB.resolve()) + os.sep
            if not (str(f) + os.sep).startswith(root) or not f.is_file():
                return self._error("Файл не найден", 404)
            ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
            return self._send(200, f.read_bytes(), ctype + ("; charset=utf-8" if "text" in ctype or "javascript" in ctype else ""))

        if method == "GET" and path in ("/favicon.ico", "/favicon.svg"):
            icon = (b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
                    b"<rect width='64' height='64' rx='14' fill='#2f7fd8'/>"
                    b"<path d='M18 40l9-16 8 12 5-7 6 11z' fill='white'/></svg>")
            return self._send(200, icon, "image/svg+xml")

        # ---------- API
        if method == "GET" and path == "/api/counter":
            return self._json({"ok": True, "vk_token_configured": bool(os.environ.get("VKFREE_VK_TOKEN", "").strip()),
                               "supported": "vk community followers", "source": "VK API" if os.environ.get("VKFREE_VK_TOKEN", "").strip() else "виджет ВК"})

        # ---------- актуальные счётчики ВК для поля «уже есть»
        if method == "GET" and path == "/api/vkstats":
            link = (query.get("link") or [""])[0].strip()
            if not link:
                tid = (query.get("target_id") or [""])[0]
                if tid.isdigit():
                    with db.rd() as conn:
                        row = conn.execute("SELECT url FROM targets WHERE id=?", (int(tid),)).fetchone()
                    link = (row["url"] if row else "")
            if not link:
                raise ValueError("Укажите link или target_id")
            counts = vkstats.snapshot(link)
            return self._json({"ok": True, "link": link, "counts": counts,
                               "note": "значения сняты с публичной страницы ВК; прослушивания и друзья не определяются"})

        m_delivery = re.match(r"^/api/tasks/(\d+)/delivery/(check|abandon|confirm)$", path)
        if method == "POST" and m_delivery:
            task_id, action = int(m_delivery.group(1)), m_delivery.group(2)
            body = self._body()
            if action == 'confirm':
                if body.get('confirm') is not True:
                    raise ValueError('подтвердите, что площадка приняла заказ')
                delivery.confirm_accepted(task_id)
                return self._json({"ok": True})
            if action == 'abandon':
                if body.get('confirm') is not True:
                    raise ValueError('подтвердите риск параллельного заказа')
                delivery.abandon(task_id)
                return self._json({"ok": True})
            return self._json({"ok": True, "completed": delivery.check_one(task_id)})

        if method == "GET" and path == "/api/state":
            return self._json({"ok": True, **engine.state()})

        if method == "GET" and path == "/api/goals":
            return self._json({"ok": True, "goals": goals.list_goals(),
                               "metrics": [{"key": k, "title": v[0]} for k, v in goals.METRICS.items()]})

        m = re.match(r"^/api/goals/(\d+)$", path)
        if method == "GET" and m:
            return self._json(goals.progress(int(m.group(1))))

        if method == "GET" and path == "/api/browsers":
            st = stealth.backend_status()
            from core import captcha
            return self._json({"ok": True, **st,
                               "chosen": db.get_setting("browser_backend", "auto") or "auto",
                               "solvers": captcha.solvers_status(),
                               "autosolve": (db.get_setting("captcha_autosolve", "1") or "1") == "1",
                               "platform": stealth.platform_hint()})

        if method == "GET" and path == "/api/sites/health":
            rows = profiles.health()
            good = sum(1 for r in rows if r["status"] == "работает")
            return self._json({"ok": True, "sites": rows,
                               "summary": {"total": len(rows), "working": good,
                                           "captcha": sum(1 for r in rows if r["status"] == "капча"),
                                           "broken": sum(1 for r in rows if r["status"] in ("не работает", "форма изменилась")),
                                           "off": sum(1 for r in rows if r["status"] == "выключена")}})

        if method == "GET" and path == "/api/health":
            return self._json({"ok": True, "health": engine.health(), "settings": db.get_settings()})

        if method == "GET" and path == "/api/events":
            limit = min(500, int((query.get("limit") or ["200"])[0]))
            with db.rd() as conn:
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
            return self._json({"ok": True, "events": rows})

        if method == "GET" and path == "/api/export":
            try:
                p = db.export_csv()
                data = Path(p).read_bytes()
                return self._send(200, data, "text/csv; charset=utf-8",
                                  {"Content-Disposition": f'attachment; filename="{Path(p).name}"'})
            except Exception as exc:
                return self._error(f"Экспорт не удался: {exc}", 500)

        if method == "POST" and path == "/api/catalog/reload":
            info = db.seed_catalog(force=False)
            with db.rd() as conn:
                sites_n = conn.execute("SELECT COUNT(*) n FROM sites").fetchone()["n"]
                svcs_n = conn.execute("SELECT COUNT(*) n FROM services").fetchone()["n"]
            msg = (f"Каталог перезагружен: {sites_n} {_plural(sites_n, 'сайт', 'сайта', 'сайтов')}, "
                   f"{svcs_n} {_plural(svcs_n, 'услуга', 'услуги', 'услуг')}")
            if info.get("sites") or info.get("services"):
                gone_s, gone_v = info["sites"], info["services"]
                msg += (f" · убрано из базы: {gone_s} {_plural(gone_s, 'сайт', 'сайта', 'сайтов')}, "
                        f"{gone_v} {_plural(gone_v, 'услуга', 'услуги', 'услуг')} — их больше нет в каталоге")
            db.log("info", msg)
            return self._json({"ok": True, "sites_total": sites_n, "services_total": svcs_n, **info})

        if method == "POST" and path == "/api/tick":
            n = engine.promote_due()
            return self._json({"ok": True, "promoted": n})

        # ---------- определение объекта по ссылке (для подсказки в форме)
        if method == "POST" and path == "/api/detect":
            b = self._body()
            platform = (b.get("platform") or "vk").strip()
            url = (b.get("url") or "").strip()
            kind = engine.detect_target_kind(platform, url)
            return self._json({"ok": True, "kind": kind,
                               "label": engine.SUBJECTS.get(kind, "не определён"),
                               "short": engine.SUBJECT_SHORT.get(kind, "не определён")})

        # ---------- цели (этап 6): «сколько и за какой срок»
        if method == "POST" and path == "/api/goals/preview":
            b = self._body()
            return self._json(goals.preview(
                _req_int(b, "target_id", "Ссылка"), _goal_items(b),
                b.get("deadline_at"), days=b.get("days"), hours=b.get("hours"),
                pace=b.get("pace") or "human"))

        if method == "POST" and path == "/api/goals":
            b = self._body()
            res = goals.create(_req_int(b, "target_id", "Ссылка"), _goal_items(b),
                               b.get("deadline_at"), days=b.get("days"), hours=b.get("hours"),
                               pace=b.get("pace") or "human",
                               autostart=bool(b.get("autostart", True)),
                               title=b.get("title"))
            return self._json({"ok": True, "goal_id": res["goal_id"],
                               "created_n": len(res["created"]), "goal": goals.progress(res["goal_id"])})

        m = re.match(r"^/api/goals/(\d+)/(pause|resume|replan|extend)$", path)
        if method == "POST" and m:
            gid, act = int(m.group(1)), m.group(2)
            b = self._body()
            if act == "pause":
                return self._json(goals.pause(gid))
            if act == "resume":
                return self._json(goals.resume(gid))
            if act == "replan":
                return self._json(goals.replan(gid))
            return self._json(goals.extend(gid, float(b.get("days") or 1)))

        # ---------- ссылки-цели
        if method == "POST" and path == "/api/targets":
            b = self._body()
            name = (b.get("name") or "").strip() or "Без названия"
            platform = (b.get("platform") or "vk").strip()
            url = validate_target(platform, b.get("url", ""))
            kind = _target_kind_arg(b, platform, url)
            with db.tx() as conn:
                try:
                    cur = conn.execute("INSERT INTO targets(name, platform, url, kind, created_at) VALUES(?,?,?,?,?)",
                                       (name[:80], platform, url, kind, engine.now()))
                except Exception:
                    raise ValueError("Такая ссылка уже добавлена")
            effective = kind or engine.detect_target_kind(platform, url)
            db.log("info", f"Добавлена ссылка: {name} ({platform}, "
                           f"{engine.SUBJECT_SHORT.get(effective, 'объект не определён')})")
            return self._json({"ok": True, "id": cur.lastrowid, "kind": effective})

        # ---------- что можно накрутить на эту ссылку (мастер «Накрутить»)
        m = re.match(r"^/api/targets/(\d+)/services$", path)
        if method == "GET" and m:
            return self._json({"ok": True, **engine.services_for_target(int(m.group(1)))})

        m = re.match(r"^/api/targets/(\d+)$", path)
        if method == "POST" and m:
            b = self._body()
            tid = int(m.group(1))
            with db.rd() as conn:
                row = conn.execute("SELECT * FROM targets WHERE id=?", (tid,)).fetchone()
            if not row:
                raise ValueError("Ссылка не найдена")
            platform = b.get("platform") or row["platform"]
            url = validate_target(platform, b.get("url") or row["url"])
            name = (b.get("name") or row["name"]).strip()[:80] or row["name"]
            kind = _target_kind_arg(b, platform, url, current=engine._field(row, "kind", ""))
            with db.tx() as conn:
                latest = conn.execute("SELECT platform,url FROM targets WHERE id=?", (tid,)).fetchone()
                if not latest:
                    raise ValueError("Ссылка не найдена")
                if (platform != latest['platform'] or url != latest['url']) and conn.execute(
                        "SELECT 1 FROM deliveries WHERE target_id=? AND state IN ('reserved','submitting','waiting','uncertain')", (tid,)).fetchone():
                    raise ValueError("Нельзя менять ссылку, пока по ней выполняется заказ")
                conn.execute("UPDATE targets SET name=?, platform=?, url=?, kind=? WHERE id=?",
                             (name, platform, url, kind, tid))
            db.log("info", f"Ссылка «{name}»: объект — "
                           f"{engine.SUBJECT_SHORT.get(kind or engine.detect_target_kind(platform, url), 'не определён')}")
            return self._json({"ok": True, "id": tid, "kind": kind})

        m_goal = re.match(r"^/api/goals/(\d+)$", path)
        if method == "DELETE" and m_goal:
            return self._json(goals.remove(int(m_goal.group(1))))

        if method == "DELETE" and m:
            with db.tx() as conn:
                tid = int(m.group(1))
                if conn.execute("SELECT 1 FROM deliveries WHERE target_id=? AND state IN "
                                "('reserved','submitting','waiting','uncertain')", (tid,)).fetchone():
                    raise ValueError("У ссылки есть незавершённый заказ; сначала проверьте его")
                conn.execute("DELETE FROM deliveries WHERE target_id=?", (tid,))
                # сначала задачи (иначе останутся «сироты» с target_id=NULL), потом саму ссылку
                conn.execute("DELETE FROM tasks WHERE target_id=? AND status IN ('queued','ready','wait_confirm','failed')", (tid,))
                conn.execute("DELETE FROM targets WHERE id=?", (tid,))
            return self._json({"ok": True})

        # ---------- план
        if method == "POST" and path == "/api/plan":
            b = self._body()
            res = engine.plan(_req_int(b, "target_id", "Ссылка"), b.get("items") or [],
                              autostart=bool(b.get("autostart", True)))
            return self._json({"ok": True, **res})

        # ---------- задачи
        m = re.match(r"^/api/tasks/(\d+)/(start|finish|cancel|retry)$", path)
        if method == "POST" and m:
            tid, action = int(m.group(1)), m.group(2)
            if action == "start":
                return self._json({"ok": True, **executor.start_task(tid)})
            b = self._body()
            if action == "finish":
                ok = bool(b.get("ok"))
                units = b.get("units")
                return self._json({"ok": True, **executor.finish_task(tid, ok, b.get("note", ""),
                                                                     int(units) if units else None)})
            if action == "cancel":
                return self._json(executor.cancel_task(tid))
            if action == "retry":
                return self._json(executor.retry_task(tid))

        # ---------- все сайты разом: перевести в один режим исполнения
        m = re.match(r"^/api/tasks/(\d+)/robot$", path)
        if method == "POST" and m:
            return self._json(executor.robot_now(int(m.group(1))))

        if method == "POST" and path == "/api/sites_all/mode":
            b = self._body()
            mode = b.get("mode")
            if mode not in ("assisted", "auto", "manual"):
                raise ValueError("Режим должен быть assisted, auto или manual")
            with db.tx() as conn:
                cur = conn.execute("UPDATE sites SET mode=? WHERE enabled=1", (mode,))
            db.log("info", f"Все включённые сайты переведены в режим {mode}: "
                           f"{cur.rowcount} {_plural(cur.rowcount, 'сайт', 'сайта', 'сайтов')}")
            return self._json({"ok": True, "updated": cur.rowcount, "mode": mode})

        # ---------- проверка площадок (обязательно раньше, чем /api/sites/<id>: иначе «check_all»
        #            улетит в общий обработчик и будет воспринят как id площадки)
        if method == "POST" and path == "/api/sites/check_all":
            b = self._body()
            limit = _req_int(b, "limit", "Лимит") if b.get("limit") else None
            rows = profiles.check_all(limit=limit)
            return self._json({"ok": True, "checked": len(rows), "results": rows})

        m = re.match(r"^/api/sites/([\w\-]+)/robot_check$", path)
        if method == "POST" and m:
            b = self._body()
            prefer = b.get("backend") or None
            if prefer in ("", "auto"):
                prefer = None
            res = drivers.probe_site(m.group(1), b.get("service_id") or None,
                                     prefer=prefer, headless=bool(b.get("headless", True)))
            return self._json({"ok": True, **res})

        if method == "POST" and path == "/api/browsers/install_hint":
            b = self._body()
            name = (b.get("backend") or "").strip()
            from core import captcha
            row = next((x for x in stealth.BACKENDS if x["id"] == name), None)
            if not row:
                row = next((x for x in captcha.solvers_status() if x["id"] == name), None)
                if row:
                    return self._json({"ok": True, "backend": name,
                                       "commands": [row["pip"]] if row.get("pip") else [],
                                       "why": row["why"]})
                raise ValueError("Неизвестный движок")
            return self._json({"ok": True, "backend": name, "commands": [row["pip"], row["post"]],
                               "why": row["why"]})

        if method == "POST" and path == "/api/sites/check":
            b = self._body()
            sid = (b.get("site_id") or "").strip()
            if not sid:
                raise ValueError("Не указана площадка")
            return self._json({"ok": True, **profiles.check_site(sid, b.get("url") or None)})

        # ---------- сайты
        m = re.match(r"^/api/sites/([\w\-]+)$", path)
        if method == "POST" and m:
            sid = m.group(1)
            b = self._body()
            with db.tx() as conn:
                if "enabled" in b:
                    conn.execute("UPDATE sites SET enabled=?, breaker_fails=0, breaker_until=0 WHERE id=?",
                                 (1 if b["enabled"] else 0, sid))
                if "mode" in b:
                    if b["mode"] not in ("assisted", "auto", "manual"):
                        raise ValueError("Режим должен быть assisted, auto или manual")
                    conn.execute("UPDATE sites SET mode=? WHERE id=?", (b["mode"], sid))
            # капча и кулдаун на весь сайт — отдельными помощниками (они пишут в services)
            messages = []
            if "captcha" in b:
                cap = _req_int(b, "captcha", "Капча")
                if cap not in (-1, 0, 1):
                    raise ValueError("Капча: -1 не проверено, 0 нет, 1 есть")
                res = db.set_site_captcha(sid, cap)
                messages.append(f"капча у {res['updated']} услуг")
            if "cooldown_min" in b:
                cd = _opt_int(b, "cooldown_min", 0, 10080, "Кулдаун")
                res = db.set_site_cooldown(sid, cd)
                messages.append(f"кулдаун у {res['updated']} услуг")
                engine.reschedule(sid)
            if b.get("reset_overrides"):
                res = db.reset_overrides(sid)
                engine.reschedule(sid)
                messages.append(f"сброшено ручных настроек: {res['updated']}")
            db.log("info", f"Сайт {sid}: " + (", ".join(messages) if messages else "настройки обновлены"), sid)
            return self._json({"ok": True, "changed": messages})

        # ---------- ручная настройка одной услуги
        m = re.match(r"^/api/services/([\w\-.]+)$", path)
        if method == "POST" and m:
            svc_id = m.group(1)
            b = self._body()
            if b.get("reset"):
                db.set_service_override(svc_id, cooldown_min=None, amount=None, max_claims_day=None)
                engine.reschedule()
                db.log("info", f"Услуга {svc_id}: ручные значения сброшены к каталогу")
                return self._json({"ok": True, "reset": True, "service": engine.services_map().get(svc_id)})
            cooldown = _opt_int(b, "cooldown_min", 0, 10080, "Кулдаун (мин)")
            amount = _opt_int(b, "amount", 1, 100000, "Объём за заход")
            max_claims = _opt_int(b, "max_claims_day", 1, 1000, "Заходов в сутки")
            captcha = "keep"
            if "captcha" in b:
                captcha = _req_int(b, "captcha", "Капча")
                if captcha not in (-1, 0, 1):
                    raise ValueError("Капча: -1 не проверено, 0 нет, 1 есть")
            db.set_service_override(svc_id, cooldown_min=cooldown, amount=amount,
                                    max_claims_day=max_claims, captcha=captcha)
            if b.get("site_wide") and cooldown != "keep":
                svc = engine.services_map().get(svc_id)
                if svc:
                    db.set_site_cooldown(svc["site_id"], None if cooldown is None else int(cooldown))
            if cooldown != "keep":
                engine.reschedule()
            db.log("info", f"Услуга {svc_id}: ручные настройки обновлены")
            return self._json({"ok": True, "service": engine.services_map().get(svc_id)})

        # ---------- пересчитать очередь по новым кулдаунам
        if method == "POST" and path == "/api/reschedule":
            b = self._body()
            return self._json({"ok": True, **engine.reschedule(b.get("site_id"))})

        # ---------- «я решил капчу» для задачи
        m = re.match(r"^/api/tasks/(\d+)/captcha_done$", path)
        if method == "POST" and m:
            return self._json({"ok": executor.confirm_captcha_by_human(int(m.group(1)))})

        # ---------- настройки
        if method == "POST" and path == "/api/settings":
            b = self._body()
            clean = _clean_settings(b)
            with db.tx() as conn:
                current = conn.execute("SELECT value FROM settings WHERE key='full_auto'").fetchone()
                if clean.get('full_auto') == '1' and (not current or current[0] != '1'):
                    active = conn.execute("SELECT id FROM tasks WHERE status IN ('running','wait_confirm','captcha') LIMIT 1").fetchone()
                    if active:
                        raise ValueError(f"Сначала завершите открытую задачу #{active[0]}: нельзя безопасно определить её доставку задним числом")
                for k, v in clean.items():
                    conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))
            db.log("info", f"Настройки обновлены: {', '.join(clean.keys())}")
            return self._json({"ok": True, "settings": db.get_settings()})

        return self._error(f"Неизвестный маршрут {method} {path}", 404)


class QuietServer(ThreadingHTTPServer):
    """HTTP-сервер, который не сыплет трейсбеками, если браузер закрыл соединение."""
    daemon_threads = True

    def handle_error(self, request, client_address):
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError, ConnectionAbortedError)):
            return          # браузер/прокси закрыл сокет — это норма, не ошибка
        super().handle_error(request, client_address)


class SiteChecker(threading.Thread):
    """Тихая самопроверка площадок: раз в час берёт одну «просроченную» и смотрит форму заказа.

    Специально медленно и по одной — чтобы не выглядеть сканером и не мешать планировщику.
    """

    daemon = True

    def __init__(self, period_sec: int = 600) -> None:
        super().__init__(name="site-checker")
        self.period = period_sec
        self._stop = threading.Event()
        self.last: dict | None = None

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                # по две просроченные за цикл: статусы в таблице «Площадки» не протухают
                for sid in profiles.stale_sites(max_age_hours=12)[:2]:
                    res = profiles.check_site(sid)
                    self.last = res
                    db.log("info", f"Самопроверка «{sid}»: {res['note']}")
            except Exception as exc:
                db.log("warn", f"Самопроверка площадок: {exc}")
            self._stop.wait(self.period)

    def stop(self) -> None:
        self._stop.set()


# ------------------------------------------------------------------ планировщик
class Scheduler(threading.Thread):
    daemon = True

    def __init__(self) -> None:
        super().__init__(name="scheduler")
        self._stop = threading.Event()
        self.last_error = ""
        self._delivery_lock = threading.Lock()

    def _poll_deliveries(self) -> None:
        try:
            delivery.poll_due()
        finally:
            self._delivery_lock.release()

    def run(self) -> None:
        current_task_id = None
        while not self._stop.is_set():
            try:
                if self._delivery_lock.acquire(blocking=False):
                    threading.Thread(target=self._poll_deliveries, daemon=True, name="delivery-poll").start()
                engine.promote_due()
                engine.reap_stale()

                # Если есть текущая задача в работе — ждём её завершения перед следующей
                if current_task_id is not None:
                    with db.rd() as conn:
                        r = conn.execute("SELECT status FROM tasks WHERE id=?", (current_task_id,)).fetchone()
                    if r and r["status"] == "running":
                        self._stop.wait(3.0)
                        continue
                    current_task_id = None  # задача завершилась, переходим к следующей

                ok, _reason = engine.can_start_now()
                if db.get_setting("autopilot", "0") == "1" and ok:
                    t = engine.next_open_task()
                    if t:
                        # auto — робот делает всё сам; assisted — открываем страницу и буфер,
                        # человеку остаётся нажать «подтвердить» (если включено auto_open)
                        if (t.get("site_mode") == "auto" or db.get_setting("auto_open", "1") == "1"
                                or db.get_setting("full_auto", "0") == "1"):
                            try:
                                result = executor.start_task(int(t["id"]))
                                if result.get("mode") == "auto" and result.get("pending"):
                                    current_task_id = int(t["id"])  # ждём завершения авто-воркера
                            except delivery.CounterError as exc:
                                executor._set_task(int(t["id"]), "failed", f"полный авто: {exc}")
            except Exception as exc:
                self.last_error = str(exc)
                db.log("error", f"Планировщик: {exc}")
            self._stop.wait(3.0)

    def stop(self) -> None:
        self._stop.set()


def serve(host: str = "0.0.0.0", port: int = 8777) -> None:
    db.init_db()
    sched = Scheduler()
    sched.start()
    checker = SiteChecker()
    checker.start()
    httpd = QuietServer((host, port), Handler)
    db.log("info", f"Сервер запущен на http://{host}:{port}")
    print(f"\n  VK Free Farm — интерфейс: http://localhost:{port}\n  Ctrl+C — остановить\n")
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nОстанавливаюсь…")
    finally:
        sched.stop()
        checker.stop()
        httpd.server_close()
        db.log("info", "Сервер остановлен")
