# -*- coding: utf-8 -*-
"""Браузерный слой: какой движок взять и как выглядеть человеком.

Зачем отдельный модуль. Обычный Playwright оставляет следы (navigator.webdriver, CDP-флаги),
и часть панелей это видит. Есть бесплатные решения, которые убирают эти «утечки»:

    Patchright  — патченный Playwright, те же команды, но без следов. Приоритет №1.
    Camoufox    — Firefox с подменой отпечатка (canvas, шрифты, WebGL). Приоритет №2.
    Nodriver    — Chrome через CDP без WebDriver. Приоритет №3.
    Playwright  — обычный, если ничего другого нет. Приоритет №4.

Всё это опционально: софт работает и без них (ручной путь), а при наличии — сам берёт лучший.
Модуль отдаёт «адаптер страницы» с коротким набором команд (открыть, выполнить JS, текст,
скриншот, пауза), поэтому логика драйверов площадок не зависит от движка.
"""
from __future__ import annotations

import contextlib
import importlib
import os
import platform
import random
import shutil
import time
from pathlib import Path

# Xvfb — виртуальный дисплей, чтобы браузер и человекоподобный ввод работали
# на сервере без монитора (нет $DISPLAY).
_XVFB = None


def _start_xvfb() -> bool:
    """Поднимает виртуальный дисплей Xvfb, если нет $DISPLAY и есть xvfbwrapper."""
    global _XVFB
    if os.environ.get("DISPLAY"):
        return True
    try:
        from xvfbwrapper import Xvfb  # type: ignore
        _XVFB = Xvfb(width=1366, height=860, colordepth=24)
        _XVFB.start()
        os.environ["DISPLAY"] = f":{_XVFB.new_display}"
        return True
    except Exception:
        return False

# ------------------------------------------------------------------ реестр движков

BACKENDS = [
    {
        "id": "patchright",
        "title": "Patchright",
        "why": "патченный Playwright: те же команды, но без следов автоматизации",
        "pip": "pip install patchright",
        "post": "python -m patchright install chromium",
        "module": "patchright.sync_api",
        "priority": 1,
    },
    {
        "id": "camoufox",
        "title": "Camoufox",
        "why": "Firefox с подменой отпечатка: canvas, шрифты, WebGL, гео",
        "pip": "pip install camoufox[geoip]",
        "post": "python -m camoufox fetch",
        "module": "camoufox.sync_api",
        "priority": 2,
    },
    {
        "id": "nodriver",
        "title": "Nodriver",
        "why": "управление Chrome через CDP без WebDriver — самый незаметный",
        "pip": "pip install nodriver",
        "post": "",
        "module": "nodriver",
        "priority": 3,
    },
    {
        "id": "playwright",
        "title": "Playwright",
        "why": "обычный браузерный движок — основа, работает всегда",
        "pip": "pip install playwright",
        "post": "python -m playwright install chromium",
        "module": "playwright.sync_api",
        "priority": 4,
    },
]

CURL_ID = "curl_cffi"

UA_CHROME = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
UA_FIREFOX = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:143.0) Gecko/20100101 Firefox/143.0")

# Аргументы запуска: убираем самые заметные признаки автоматизации
CHROME_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
]
CHROME_ARGS_HEADLESS = CHROME_ARGS + ["--headless=new"]

# Что подставляем в страницу до её скриптов (маленький, но действенный набор)
STEALTH_INIT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU', 'ru', 'en-US', 'en']});
Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
if (!window.chrome) { window.chrome = {runtime: {}, app: {isInstalled: false}}; }
const _query = navigator.permissions && navigator.permissions.query;
if (_query) {
  navigator.permissions.query = (p) => (p && p.name === 'notifications')
    ? Promise.resolve({state: Notification.permission}) : _query.call(navigator.permissions, p);
}
const _getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function (p) {
  if (p === 37445) return 'Intel Inc.';
  if (p === 37446) return 'Intel Iris OpenGL Engine';
  return _getParameter.call(this, p);
};
if (!navigator.plugins || !navigator.plugins.length) {
  Object.defineProperty(navigator, 'plugins', {get: () => [{name: 'Chrome PDF Plugin'}, {name: 'Chrome PDF Viewer'}]});
}
"""


class BrowserUnavailable(RuntimeError):
    """Ни один браузерный движок недоступен."""

    def __init__(self, tried: list[tuple[str, str]]):
        self.tried = tried
        lines = [f"{name}: {why}" for name, why in tried]
        super().__init__("нет доступного браузера (" + "; ".join(lines) + ")")


def _module_ok(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except Exception:
        return False


def backend_status() -> dict:
    """Кто установлен, кто нет, и что доустановить. Для интерфейса и диагностики."""
    rows, best = [], None
    for b in sorted(BACKENDS, key=lambda x: x["priority"]):
        ok = _module_ok(b["module"])
        browsers = _browsers_present(b["id"])
        row = {**b, "installed": bool(ok and browsers), "module_ok": ok,
               "browsers_ok": browsers, "ready": bool(ok and browsers)}
        if row["ready"] and best is None:
            best = b["id"]
        rows.append(row)
    return {"backends": rows, "best": best, "curl": _module_ok(CURL_ID),
            "recommend": (best or None)}


def _browsers_present(backend_id: str) -> bool:
    """Есть ли скачанный браузер под этот движок.

    Playwright-family держит сборки в ~/.cache/ms-playwright, но на Windows
    может быть и в %LOCALAPPDATA%/ms-playwright или %USERPROFILE%/AppData/Local/ms-playwright.
    Если проверить не удалось — считаем «есть», чтобы не мешать запуску.
    """
    try:
        if backend_id in ("playwright", "patchright"):
            # _open_playwright_like умеет запускать системный Chrome/Chromium;
            # не требуем кэша Playwright, когда этот браузер уже найден.
            if _find_chrome():
                return True
            candidates = [
                Path.home() / ".cache" / "ms-playwright",
                Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright",
                Path(os.environ.get("USERPROFILE", "")) / "AppData" / "Local" / "ms-playwright",
            ]
            for cache in candidates:
                if not cache or not cache.exists():
                    continue
                if any(p.is_dir() and p.name.startswith("chromium") for p in cache.iterdir()):
                    return True
            return False
        if backend_id == "camoufox":
            cache = Path.home() / ".cache" / "camoufox"
            return cache.exists() and any(cache.iterdir())
        if backend_id == "nodriver":
            if _find_chrome():
                return True
            cache = Path.home() / ".cache" / "ms-playwright"
            return cache.exists()
    except Exception:
        return True
    return True


def _find_chrome() -> str | None:
    """Ищем локальный Chrome/Chromium для nodriver."""
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    cache = Path.home() / ".cache" / "ms-playwright"
    if cache.exists():
        for d in sorted(cache.glob("chromium-*")):
            for sub in ("chrome-linux/chrome", "chrome-linux64/chrome", "chrome-mac/Chromium.app/Contents/MacOS/Chromium"):
                p = d / sub
                if p.exists():
                    return str(p)
    return None


LAST_TRIED: list[dict] = []


def last_tried() -> list[dict]:
    """Что пробовали при последнем запуске: для диагностики в интерфейсе."""
    return list(LAST_TRIED)


def _note_try(name: str, ok: bool, why: str = "") -> None:
    LAST_TRIED.append({"backend": name, "ok": ok, "why": why[:200]})
    del LAST_TRIED[:-8]


def preferred(prefer: str | None = None) -> list[str]:
    """Порядок движков: сначала выбранный вручную, потом по приоритету."""
    order = [b["id"] for b in sorted(BACKENDS, key=lambda x: x["priority"])]
    if prefer and prefer in order:
        order.remove(prefer)
        order.insert(0, prefer)
    return order


# ------------------------------------------------------------------ адаптеры страницы

class Page:
    """Общий интерфейс: драйверу площадки всё равно, какой движок внутри."""

    backend = "?"

    def goto(self, url: str, wait: int = 1500) -> None: ...
    def js(self, script: str):
        raise NotImplementedError
    def text(self) -> str:
        return str(self.js("document.body ? document.body.innerText : ''") or "")
    def html(self) -> str:
        return str(self.js("document.documentElement ? document.documentElement.outerHTML : ''") or "")
    def shot(self, path: str | Path) -> str | None:
        return None
    def wait(self, ms: int) -> None:
        time.sleep(max(0, ms) / 1000)
    def close(self) -> None: ...

    # «Человеческие» действия — там, где движок умеет
    def type_human(self, selector: str, text: str) -> bool:
        return False

    def click_human(self, selector: str) -> bool:
        return False

    def mouse_wiggle(self, n: int = 3) -> None:
        pass

    def scroll(self) -> None:
        self.js("window.scrollBy(0, 180 + Math.random() * 240)")

    # Геометрия и клик по координатам — нужны для капч внутри iframe (Turnstile, reCAPTCHA),
    # куда обычным селектором из основного документа не достать.
    def box(self, selector: str) -> dict | None:
        """Положение элемента на экране: {x, y, w, h} в координатах окна."""
        try:
            rect = self.js("(() => {const e = document.querySelector(%r); if (!e) return null;"
                           " e.scrollIntoView({block: 'center'});"
                           " const r = e.getBoundingClientRect();"
                           " return {x: r.x, y: r.y, w: r.width, h: r.height};})()" % selector)
        except Exception:
            return None
        if not rect:
            return None
        self.wait(250)
        try:                       # перечитываем после прокрутки
            rect = self.js("(() => {const e = document.querySelector(%r); if (!e) return null;"
                           " const r = e.getBoundingClientRect();"
                           " return {x: r.x, y: r.y, w: r.width, h: r.height};})()" % selector) or rect
        except Exception:
            pass
        return rect if isinstance(rect, dict) else None

    def click_at(self, x: float, y: float) -> bool:
        """Клик мышью по координатам окна."""
        return False


def _jitter(rnd: random.Random, lo: int, hi: int) -> int:
    return rnd.randint(lo, hi)


class PlaywrightPage(Page):
    """Общий адаптер для playwright / patchright / camoufox: у них одинаковый API."""

    def __init__(self, page, backend: str):
        self._p = page
        self.backend = backend

    def goto(self, url: str, wait: int = 1500) -> None:
        self._p.goto(url, wait_until="domcontentloaded", timeout=90000)
        self.wait(wait)

    def js(self, script: str):
        try:
            return self._p.evaluate(script)
        except Exception:
            return None

    def shot(self, path, focus: str | None = None) -> str | None:
        """Скриншот окна. Если передан focus — сначала прокручиваем к этому элементу.

        Полностраничные снимки бесплатных панелей получаются на несколько экранов,
        и разобрать на них ответ площадки невозможно. Поэтому снимаем окно вокруг формы.
        """
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            if focus:
                try:
                    self.js(f"(() => {{const e=document.querySelector({focus!r});"
                            " if (e) e.scrollIntoView({block: 'center'});}})()")
                    time.sleep(0.35)
                except Exception:
                    pass
            self._p.screenshot(path=str(path))
            return str(path)
        except Exception:
            return None

    def wait(self, ms: int) -> None:
        try:
            self._p.wait_for_timeout(int(ms))
        except Exception:
            time.sleep(max(0, ms) / 1000)

    def type_human(self, selector: str, text: str) -> bool:
        """Печатаем по-человечески: паузы между символами плюс осмысленные опечатки не делаем."""
        try:
            loc = self._p.locator(selector).first
            loc.click(timeout=8000)
            self.wait(_jitter(random.Random(), 120, 380))
            loc.fill("")
            for ch in text:
                self._p.keyboard.type(ch, delay=_jitter(random.Random(), 45, 160))
                if random.random() < 0.06:
                    self.wait(_jitter(random.Random(), 180, 520))
            self.wait(_jitter(random.Random(), 150, 400))
            return True
        except Exception:
            return False

    def click_human(self, selector: str) -> bool:
        try:
            loc = self._p.locator(selector).first
            loc.scroll_into_view_if_needed(timeout=5000)
            self.wait(_jitter(random.Random(), 150, 450))
            loc.click(timeout=10000)
            return True
        except Exception:
            return False

    def mouse_wiggle(self, n: int = 3) -> None:
        try:
            for _ in range(n):
                self._p.mouse.move(_jitter(random.Random(), 120, 900),
                                   _jitter(random.Random(), 100, 620),
                                   steps=_jitter(random.Random(), 4, 12))
                self.wait(_jitter(random.Random(), 60, 220))
        except Exception:
            pass

    def click_at(self, x: float, y: float) -> bool:
        """Клик мышью по координатам — так нажимаются чекбоксы капч внутри iframe."""
        try:
            rnd = random.Random()
            self._p.mouse.move(x - _jitter(rnd, 40, 160), y - _jitter(rnd, 20, 90),
                               steps=_jitter(rnd, 5, 12))
            self.wait(_jitter(rnd, 90, 260))
            self._p.mouse.move(x, y, steps=_jitter(rnd, 6, 14))
            self.wait(_jitter(rnd, 80, 220))
            self._p.mouse.down()
            self.wait(_jitter(rnd, 40, 140))
            self._p.mouse.up()
            return True
        except Exception:
            return False

    def captcha_frames(self) -> list[dict]:
        """Visible challenge frames, including frames hosted in shadow roots."""
        result = []
        for frame in self._p.frames:
            try:
                url = (frame.url or "").lower()
                if "challenges.cloudflare" in url or "turnstile" in url:
                    kind = "turnstile"
                elif "hcaptcha" in url:
                    kind = "hcaptcha"
                elif "recaptcha" in url:
                    kind = "recaptcha"
                else:
                    continue
                el = frame.frame_element()
                box = el.bounding_box()
                if box and box["width"] > 20 and box["height"] > 12 and el.is_visible():
                    result.append({"kind": kind, "x": box["x"], "y": box["y"],
                                   "w": box["width"], "h": box["height"]})
            except Exception:
                continue
        return result

    def frames_with(self, url_part: str) -> list:
        """Фреймы, в адресе которых есть строка (например, challenges.cloudflare.com)."""
        out = []
        try:
            for f in self._p.frames:
                with contextlib.suppress(Exception):
                    if url_part in (f.url or ""):
                        out.append(f)
        except Exception:
            return []
        return out

    def close(self) -> None:
        for attr in ("close",):
            with contextlib.suppress(Exception):
                getattr(self._p, attr)()


class NodriverPage(Page):
    """Адаптер для nodriver: он асинхронный, поэтому держим свой цикл в отдельном потоке."""

    backend = "nodriver"

    def __init__(self, loop, browser, tab):
        self._loop, self._browser, self._tab = loop, browser, tab

    def _run(self, coro, timeout: float = 60.0):
        return self._loop.run(self._await(coro), timeout)

    async def _await(self, coro):
        return await coro

    def goto(self, url: str, wait: int = 1500) -> None:
        self._run(self._tab.get(url))
        self.wait(wait)

    def js(self, script: str):
        try:
            return self._run(self._tab.evaluate(script, await_promise=False))
        except Exception:
            return None

    def shot(self, path, focus: str | None = None) -> str | None:
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            if focus:
                try:
                    self.js(f"(() => {{const e=document.querySelector({focus!r});"
                            " if (e) e.scrollIntoView({block: 'center'});}})()")
                    time.sleep(0.35)
                except Exception:
                    pass
            self._run(self._tab.save_screenshot(str(path)))
            return str(path)
        except Exception:
            return None

    def click_human(self, selector: str) -> bool:
        try:
            self.js(f"""(() => {{
                const el = document.querySelector({selector!r});
                if (!el) return false;
                el.scrollIntoView({{block: 'center'}});
                el.click();
                return true;
            }})()""")
            return True
        except Exception:
            return False

    def close(self) -> None:
        with contextlib.suppress(Exception):
            res = self._browser.stop()
            if hasattr(res, "__await__"):
                self._run(res)


class _LoopThread:
    """Отдельный asyncio-цикл: nodriver весь асинхронный, а планировщик у нас синхронный."""

    def __init__(self):
        import asyncio
        import threading
        self._asyncio = asyncio
        self.loop = asyncio.new_event_loop()
        self._t = threading.Thread(target=self._run_forever, daemon=True)
        self._t.start()

    def _run_forever(self) -> None:
        self._asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coro, timeout: float = 60.0):
        fut = self._asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout=timeout)

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            self.loop.call_soon_threadsafe(self.loop.stop)


# ------------------------------------------------------------------ запуск

@contextlib.contextmanager
def open_page(headless: bool = True, prefer: str | None = None, profile_dir: str | None = None, proxy: str | None = None):
    """Открывает страницу лучшим доступным движком. Отдаёт (имя движка, Page)."""
    _start_xvfb()
    tried: list[tuple[str, str]] = []
    LAST_TRIED.clear()
    for name in preferred(prefer):
        if name == "nodriver":
            if not _module_ok("nodriver"):
                tried.append((name, "не установлен"))
                continue
            try:
                page, closer = _open_nodriver(headless, proxy)
            except Exception as exc:
                tried.append((name, str(exc)[:140]))
                _note_try(name, False, str(exc))
                continue
            _note_try(name, True)
            try:
                yield name, page
            finally:
                with contextlib.suppress(Exception):
                    closer()
            return

        module = next(b["module"] for b in BACKENDS if b["id"] == name)
        if not _module_ok(module):
            tried.append((name, "не установлен"))
            continue
        if not _browsers_present(name):
            tried.append((name, "браузер не скачан"))
            continue
        try:
            page, closer = _open_playwright_like(name, headless, profile_dir, proxy)
        except Exception as exc:
            tried.append((name, str(exc)[:140]))
            _note_try(name, False, str(exc))
            continue
        _note_try(name, True)
        try:
            yield name, page
        finally:
            with contextlib.suppress(Exception):
                closer()
        return
    raise BrowserUnavailable(tried)


def _proxy_kwargs(proxy: str | None) -> dict:
    """Proxy для playwright/camoufox: http://host:port или socks5://host:port."""
    if not proxy:
        return {}
    from urllib.parse import unquote, urlsplit
    parsed = urlsplit(proxy)
    if parsed.scheme not in ('http', 'https', 'socks5', 'socks5h') or not parsed.hostname or not parsed.port:
        raise ValueError('Некорректный прокси')
    host = f'[{parsed.hostname}]' if ':' in parsed.hostname else parsed.hostname
    scheme = 'socks5' if parsed.scheme == 'socks5h' else parsed.scheme
    value = {'server': f'{scheme}://{host}:{parsed.port}'}
    if parsed.username is not None:
        value['username'] = unquote(parsed.username)
    if parsed.password is not None:
        value['password'] = unquote(parsed.password)
    return {'proxy': value}


def _open_playwright_like(name: str, headless: bool, profile_dir: str | None, proxy: str | None = None):
    """playwright / patchright / camoufox — у всех одинаковый sync API."""
    if name == "camoufox":
        from camoufox.sync_api import Camoufox  # type: ignore

        cm = Camoufox(headless=headless, locale="ru-RU", humanize=True,
                      os=["windows"], geoip=False, i_know_what_im_doing=True,
                      **_proxy_kwargs(proxy))
        browser = cm.__enter__()
        ctx = browser.new_context(locale="ru-RU", viewport={"width": 1366, "height": 860})
        page = ctx.new_page()
        page.set_default_timeout(25000)

        def closer():
            with contextlib.suppress(Exception):
                browser.close()
            with contextlib.suppress(Exception):
                cm.__exit__(None, None, None)

        return PlaywrightPage(page, "camoufox"), closer

    if name == "patchright":
        from patchright.sync_api import sync_playwright  # type: ignore
    else:
        from playwright.sync_api import sync_playwright  # type: ignore

    pw = sync_playwright().start()
    try:
        args = CHROME_ARGS_HEADLESS if headless else CHROME_ARGS
        # У playwright и patchright разные версии Chromium: подставляем тот, что реально скачан,
        # иначе движок ищет свой headless-shell и падает с «Executable doesn't exist».
        exe = _find_chrome()
        launch_kwargs = {"headless": headless, "args": args}
        if exe:
            launch_kwargs["executable_path"] = exe
        launch_kwargs.update(_proxy_kwargs(proxy))
        browser = pw.chromium.launch(**launch_kwargs)
        ctx_args = {
            "locale": "ru-RU",
            "viewport": {"width": 1366, "height": 860},
            "timezone_id": "Europe/Moscow",
            "user_agent": UA_CHROME,
            "device_scale_factor": 1,
        }
        if name == "playwright":
            ctx = browser.new_context(**ctx_args)
            with contextlib.suppress(Exception):
                ctx.add_init_script(STEALTH_INIT)
        else:
            # patchright сам убирает следы; лишние подмены ему только мешают
            ctx = browser.new_context(**ctx_args)
        page = ctx.new_page()
        if name == 'playwright':
            # Обрабатывать можно только уже созданную страницу.
            with contextlib.suppress(Exception):
                from playwright_stealth.sync_api import stealth as _sync_stealth  # type: ignore
                _sync_stealth.sync_stealth(page)
        page.set_default_timeout(25000)
    except Exception:
        with contextlib.suppress(Exception):
            pw.stop()
        raise

    def closer():
        with contextlib.suppress(Exception):
            browser.close()
        with contextlib.suppress(Exception):
            pw.stop()

    return PlaywrightPage(page, name), closer


def _open_nodriver(headless: bool, proxy: str | None = None):
    """nodriver через свой цикл. Прокси никогда не игнорируется молча."""
    import nodriver as nd  # type: ignore

    proxy_args = []
    if proxy:
        conf = _proxy_kwargs(proxy)['proxy']
        if conf.get('username') or conf.get('password'):
            raise ValueError('nodriver не поддерживает прокси с авторизацией; выберите другой движок')
        proxy_args = ['--proxy-server=' + conf['server']]
    loop = _LoopThread()

    async def boot():
        kwargs = {"headless": headless, "sandbox": False,
                  "browser_args": CHROME_ARGS + proxy_args + (["--headless=new"] if headless else [])}
        chrome = _find_chrome()
        if chrome and "ms-playwright" in chrome:
            kwargs["browser_executable_path"] = chrome
        browser = await nd.start(**kwargs)
        tab = browser.main_tab or await browser.get("about:blank")
        return browser, tab

    try:
        browser, tab = loop.run(boot(), timeout=90)
    except Exception:
        loop.stop()
        raise
    page = NodriverPage(loop, browser, tab)

    def closer():
        page.close()
        loop.stop()

    return page, closer


# ------------------------------------------------------------------ дешёвый HTTP-путь

def curl_available() -> bool:
    return _module_ok(CURL_ID)


def http_get(url: str, timeout: float = 20.0, proxy: str | None = None) -> dict | None:
    """Быстрый GET; при прокси не допускаем незаметного прямого соединения."""
    if not curl_available():
        return None
    try:
        from curl_cffi import requests as creq  # type: ignore

        r = creq.get(url, impersonate="chrome", timeout=timeout, allow_redirects=True,
                     proxies={'http': proxy, 'https': proxy} if proxy else None)
        return {"status": r.status_code, "text": r.text, "url": str(r.url),
                "cookies": dict(r.cookies), "backend": "curl_cffi"}
    except Exception as exc:
        return {"status": None, "text": "", "error": str(exc)[:200], "backend": "curl_cffi"}


def http_post(url: str, data: dict, referer: str | None = None, timeout: float = 20.0,
              cookies: dict | None = None, proxy: str | None = None) -> dict | None:
    """POST формы без браузера — для площадок, которые отдают обычный HTML-форму."""
    if not curl_available():
        return None
    try:
        from curl_cffi import requests as creq  # type: ignore

        headers = {"Referer": referer} if referer else {}
        r = creq.post(url, data=data, impersonate="chrome", timeout=timeout,
                      headers=headers, allow_redirects=True, cookies=cookies or {},
                      proxies={'http': proxy, 'https': proxy} if proxy else None)
        return {"status": r.status_code, "text": r.text, "url": str(r.url), "backend": "curl_cffi"}
    except Exception as exc:
        return {"status": None, "text": "", "error": str(exc)[:200], "backend": "curl_cffi"}


def human_delay(lo: float = 0.6, hi: float = 1.8) -> None:
    """Пауза «по-человечески» — между шагами, а не в одном темпе."""
    time.sleep(random.uniform(lo, hi))


def platform_hint() -> str:
    return f"{platform.system()} {platform.release()}"
