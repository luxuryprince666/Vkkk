# -*- coding: utf-8 -*-
"""Первый запуск: программа сама ставит то, что нужно «роботу».

Что делает:
  * проверяет необязательные компоненты (patchright, ddddocr, curl_cffi, …);
  * чего не хватает — тихо ставит через pip;
  * один раз ставит браузер для робота (patchright install chromium);
  * пишет прогресс в журнал событий и в консоль.

Правила безопасности:
  * всё в фоновом потоке — интерфейс поднимается сразу и работает;
  * любая ошибка установки не ломает программу: ручной режим (assisted)
    работает на чистом Python без единой внешней библиотеки;
  * если компонент уже стоит — ничего не перекачиваем.

Все компоненты — БЕСПЛАТНЫЕ. Нет платных капча-ферм и прокси.
"""
from __future__ import annotations

import importlib
import subprocess
import sys
import threading
import time
from pathlib import Path

from core import db

# (имя модуля для проверки, имя пакета для pip)
PIP_PKGS = [
    ("patchright", "patchright"),
    ("ddddocr", "ddddocr"),
    ("curl_cffi", "curl_cffi"),
    ("playwright_stealth", "playwright-stealth"),
    ("proxybroker2", "proxybroker2"),
    ("cloudscraper", "cloudscraper"),
    ("stem", "stem"),
    ("torpy", "torpy"),
    ("xvfbwrapper", "xvfbwrapper"),
]
CHROMIUM_MARKER = Path(db.DATA) / "robot.ready"

_lock = threading.Lock()
_state: dict = {"running": False, "done": False, "installed": [], "errors": [], "checked_at": 0.0}


def _have(module: str) -> bool:
    try:
        importlib.import_module(module)
        return True
    except Exception:
        return False


def missing() -> list[tuple[str, str]]:
    return [(mod, pkg) for mod, pkg in PIP_PKGS if not _have(mod)]


def _pip(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", *args],
                          capture_output=True, text=True, timeout=1200)


def _job() -> None:
    with _lock:
        if _state["running"]:
            return
        _state["running"] = True
    try:
        need = missing()
        if need:
            db.log("info", "Первый запуск: устанавливаю компоненты робота: " + ", ".join(pkg for _, pkg in need))
            print("🔧 Устанавливаю компоненты робота: " + ", ".join(pkg for _, pkg in need), flush=True)
        for mod, pkg in need:
            try:
                res = _pip(pkg)
                if res.returncode == 0:
                    _state["installed"].append(pkg)
                    db.log("info", f"Установлено: {pkg}")
                    print(f"✅ Установлено: {pkg}", flush=True)
                else:
                    _state["errors"].append(pkg)
                    db.log("warn", f"{pkg}: pip вернул ошибку — робот может быть недоступен, "
                                   f"ручной режим работает как обычно")
                    print(f"⚠️  {pkg}: не удалось установить через pip", flush=True)
            except Exception as exc:
                _state["errors"].append(pkg)
                db.log("warn", f"{pkg}: не удалось установить ({str(exc)[:100]})")
                print(f"⚠️  {pkg}: ошибка установки", flush=True)

        # браузер робота: ставим один раз, если patchright есть, но браузер не скачан
        if _have("patchright") and not CHROMIUM_MARKER.exists():
            print("🌐 Браузер робота не найден, скачиваю Chromium для Patchright (однократно, ~1-3 мин)…", flush=True)
            db.log("info", "Браузер робота не найден, скачиваю Chromium для Patchright…")
            try:
                # выводим прогресс прямо в консоль, чтобы пользователь видел, что идет скачивание
                res = subprocess.run([sys.executable, "-m", "patchright", "install", "chromium"],
                                     timeout=1800)
                if res.returncode == 0:
                    CHROMIUM_MARKER.parent.mkdir(parents=True, exist_ok=True)
                    CHROMIUM_MARKER.write_text(str(int(time.time())))
                    db.log("info", "Браузер робота готов")
                    print("✅ Браузер робота готов", flush=True)
                else:
                    db.log("warn", "Браузер робота не поставился — ручной режим работает как обычно")
                    print("⚠️  Браузер робота не поставился автоматически", flush=True)
            except Exception as exc:
                db.log("warn", f"Браузер робота: {str(exc)[:100]}")
                print(f"⚠️  Браузер робота: ошибка — {str(exc)[:100]}", flush=True)
        elif _have("patchright") and CHROMIUM_MARKER.exists():
            print("✅ Браузер робота уже установлен", flush=True)

        if not need and _have("patchright"):
            _state.setdefault("quiet", True)

        # прокси-пул: набираем бесплатные публичные прокси (proxybroker2)
        try:
            from core import engine as _eng
            _eng.refresh_proxy_pool()
        except Exception as exc:
            db.log("warn", f"прокси-пул: не удалось собрать ({str(exc)[:100]})")

        print("🚀 Робот готов к работе", flush=True)
    finally:
        _state["running"] = False
        _state["done"] = True
        _state["checked_at"] = time.time()


def start_async() -> None:
    """Запустить проверку/установку в фоне. Никогда не бросает исключений."""
    try:
        threading.Thread(target=_job, daemon=True, name="bootstrap").start()
    except Exception:
        pass


def status() -> dict:
    return {k: (list(v) if isinstance(v, list) else v) for k, v in _state.items()}
