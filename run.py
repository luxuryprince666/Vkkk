#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VK Free Farm — точка входа.

Запуск:
    python run.py                 # интерфейс на http://localhost:8777 (откроется браузер)
    python run.py --panel         # нативное окно-пульт (tkinter) + браузер по кнопке
    python run.py --port 9000     # другой порт
    python run.py --no-browser    # не открывать браузер автоматически
    python run.py --export        # выгрузить историю в CSV и выйти
    python run.py --reset         # удалить базу (осторожно: история потеряется)

Зависимостей нет: только стандартная библиотека Python 3.9+.
Для режима auto (робот) дополнительно нужен Playwright — см. README.
"""
from __future__ import annotations

import argparse
import socket
import sys
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import db, server  # noqa: E402


def port_open(host: str, port: int, timeout: float = 10.0) -> bool:
    """Ждём, пока сервер начнёт принимать соединения."""
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((probe_host, port), timeout=0.4):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="VK Free Farm — пульт бесплатной накрутки")
    ap.add_argument("--host", default="127.0.0.1", help="адрес (0.0.0.0 — доступ из сети)")
    ap.add_argument("--port", type=int, default=8777, help="порт")
    ap.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    ap.add_argument("--panel", action="store_true", help="нативное окно-пульт вместо браузера")
    ap.add_argument("--reset", action="store_true", help="удалить базу и начать заново")
    ap.add_argument("--export", action="store_true", help="экспорт истории в CSV и выход")
    args = ap.parse_args()

    if args.reset:
        for f in (db.DB_PATH, db.DB_PATH.with_suffix(".db-wal"), db.DB_PATH.with_suffix(".db-shm")):
            Path(f).unlink(missing_ok=True)
        print("База удалена, создаю новую.")

    if args.export:
        db.init_db()
        print("CSV:", db.export_csv())
        return 0

    url = f"http://localhost:{args.port}"

    # ---------- «скачал и запустил»: фон сам ставит то, что нужно роботу
    try:
        from core import bootstrap
        bootstrap.start_async()
    except Exception:
        pass

    # ---------- нативный пульт: сервер поднимается в фоне, окно остаётся главным
    if args.panel:
        import threading

        threading.Thread(target=server.serve, kwargs={"host": args.host, "port": args.port},
                         daemon=True, name="http").start()
        if not port_open(args.host, args.port):
            print("Сервер не поднялся. Попробуйте другой порт: python run.py --port 8778")
            return 1
        import panel
        if panel.run(url):
            return 0
        # окно не открылось (нет дисплея) — работаем как обычно, через браузер
        if not args.no_browser:
            webbrowser.open(url)
        print(f"\n  Пульт недоступен, но всё работает: {url}\n  Ctrl+C — остановить\n")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nОстанавливаюсь…")
        return 0

    # ---------- обычный запуск: браузер + сервер
    if not args.no_browser:
        import threading

        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    try:
        server.serve(host=args.host, port=args.port)
    except OSError as exc:
        print(f"\nНе удалось занять порт {args.port}: {exc}\nПопробуйте: python run.py --port {args.port + 1}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
