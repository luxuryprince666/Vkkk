# -*- coding: utf-8 -*-
"""Нативный пульт управления (tkinter, без внешних зависимостей).

Это дополнительное окно, а не основной интерфейс: полный дашборд открывается
в браузере. Пульт нужен, чтобы держать программу под рукой — видеть статус,
переключать режим капчи и автопилот, не открывая браузер.

Запуск:  python run.py --panel
Если tkinter недоступен (или нет дисплея) — программа просто откроет браузер.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
import webbrowser

# --------------------------------------------------------------------------- палитра
BG = "#f4f6fb"
CARD = "#ffffff"
LINE = "#e5eaf2"
TXT = "#0f1724"
DIM = "#66748b"
ACC = "#2563eb"
OK = "#0e9f6e"
WARN = "#b46a06"
ERR = "#d92d3c"
FONT = ("Segoe UI", 10)
FONT_B = ("Segoe UI", 10, "bold")
FONT_H = ("Segoe UI", 15, "bold")
FONT_NUM = ("Segoe UI", 20, "bold")


# --------------------------------------------------------------------------- сеть
def api(base: str, path: str, method: str = "GET", payload: dict | None = None,
        timeout: float = 5.0) -> dict:
    """Один запрос к локальному API. Никогда не бросает исключение наружу."""
    url = base.rstrip("/") + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
            return body if isinstance(body, dict) else {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": f"HTTP {exc.code}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# --------------------------------------------------------------------------- данные
def summary(state: dict) -> dict:
    """Выжимка из /api/state для пульта (чистая функция — её легко тестировать)."""
    if not state or not state.get("ok", True):
        return {"online": False, "line1": "нет связи с программой", "line2": "", "ready": 0,
                "today": 0, "mode": "all", "mode_label": "—", "autopilot": False,
                "captcha_wait": 0, "playwright": False}
    stats = state.get("stats", {})
    health = state.get("health", {})
    settings = state.get("settings", {})
    cap = stats.get("captcha", {})
    ready = sum(1 for s in state.get("services", []) if s.get("ready"))
    in_hours = stats.get("in_work_hours", True)
    gap = int(stats.get("gap_left", 0) or 0)
    if not in_hours:
        line1 = "вне рабочих часов"
    elif gap > 0:
        line1 = f"пауза между заходами · {gap} с"
    else:
        line1 = "заходы разрешены"
    return {
        "online": True,
        "line1": line1,
        "line2": f"готово к запуску: {ready} · заходов сегодня: {stats.get('total_claims_today', 0)}",
        "ready": ready,
        "today": int(stats.get("total_claims_today", 0) or 0),
        "mode": cap.get("mode", "all"),
        "mode_label": cap.get("mode_label", "Все сайты"),
        "autopilot": settings.get("autopilot") == "1",
        "captcha_wait": int(health.get("captcha_tasks", 0) or 0),
        "playwright": bool(health.get("playwright")),
        "captcha": cap,
    }


def events_lines(payload: dict, limit: int = 7) -> list[str]:
    """Строки журнала для пульта."""
    out = []
    for e in (payload.get("events") or [])[:limit]:
        level = {"info": "·", "warn": "!", "error": "×"}.get(e.get("level"), "·")
        out.append(f"{level} {e.get('message', '')[:70]}")
    return out


# --------------------------------------------------------------------------- окно
class Panel:
    """Окно-пульт. Всё, что может упасть, завёрнуто: пульт не должен ломать программу."""

    def __init__(self, base: str) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.base = base
        self.tk = tk
        self.ttk = ttk
        self._alive = True

        self.root = tk.Tk()
        self.root.title("VK Free Farm — пульт")
        self.root.configure(bg=BG)
        self.root.geometry("470x560")
        self.root.minsize(430, 480)

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("Vertical.TScrollbar", background=BG, troughcolor=BG, borderwidth=0)

        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._poll()

    # ---------------------------------------------------------------- разметка
    def _card(self, parent, **kw):
        tk = self.tk
        frame = tk.Frame(parent, bg=CARD, highlightbackground=LINE, highlightthickness=1, **kw)
        return frame

    def _build(self) -> None:
        tk = self.tk
        wrap = tk.Frame(self.root, bg=BG, padx=14, pady=14)
        wrap.pack(fill="both", expand=True)

        # заголовок
        head = tk.Frame(wrap, bg=BG)
        head.pack(fill="x")
        tk.Label(head, text="VK Free Farm", bg=BG, fg=TXT, font=FONT_H).pack(side="left")
        self.lbl_online = tk.Label(head, text="●", bg=BG, fg=OK, font=("Segoe UI", 13))
        self.lbl_online.pack(side="right")

        # статус
        card = self._card(wrap)
        card.pack(fill="x", pady=(10, 10))
        inner = tk.Frame(card, bg=CARD, padx=14, pady=12)
        inner.pack(fill="x")
        self.lbl_line1 = tk.Label(inner, text="подключаюсь…", bg=CARD, fg=TXT, font=FONT_B, anchor="w")
        self.lbl_line1.pack(fill="x")
        self.lbl_line2 = tk.Label(inner, text="", bg=CARD, fg=DIM, font=FONT, anchor="w")
        self.lbl_line2.pack(fill="x", pady=(2, 0))

        # цифры
        nums = tk.Frame(wrap, bg=BG)
        nums.pack(fill="x")
        self.lbl_ready = self._num(nums, "готово", 0)
        self.lbl_today = self._num(nums, "заходов сегодня", 0)

        # режим капчи
        tk.Label(wrap, text="Режим", bg=BG, fg=DIM, font=FONT).pack(anchor="w", pady=(14, 4))
        modes = tk.Frame(wrap, bg=BG)
        modes.pack(fill="x")
        self.mode_buttons = {}
        for key, label in (("all", "Все сайты"), ("nocaptcha", "Без капчи"), ("captcha", "С капчей")):
            b = tk.Button(modes, text=label, font=FONT, relief="flat", bd=0, padx=12, pady=7,
                          bg=CARD, fg=DIM, activebackground="#eef2f8", activeforeground=TXT,
                          highlightthickness=1, highlightbackground=LINE, cursor="hand2",
                          command=lambda k=key: self.set_mode(k))
            b.pack(side="left", padx=(0, 6))
            self.mode_buttons[key] = b

        # кнопки
        actions = tk.Frame(wrap, bg=BG)
        actions.pack(fill="x", pady=(14, 0))
        self.btn_auto = self._btn(actions, "Автопилот: —", self.toggle_autopilot, accent=True)
        self.btn_open = self._btn(actions, "Открыть интерфейс", self.open_ui)
        row2 = tk.Frame(wrap, bg=BG)
        row2.pack(fill="x", pady=(8, 0))
        self._btn(row2, "Экспорт CSV", self.export_csv)
        self._btn(row2, "Обновить каталог", self.reload_catalog)

        # журнал
        tk.Label(wrap, text="Последние события", bg=BG, fg=DIM, font=FONT).pack(anchor="w", pady=(14, 4))
        logcard = self._card(wrap)
        logcard.pack(fill="both", expand=True)
        self.log = tk.Text(logcard, bg=CARD, fg=TXT, font=("Consolas", 9), bd=0, padx=10, pady=8,
                           height=7, wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True)

        tk.Label(wrap, text=self.base, bg=BG, fg=DIM, font=("Segoe UI", 8)).pack(anchor="w", pady=(8, 0))

    def _num(self, parent, caption: str, value: int):
        tk = self.tk
        card = self._card(parent)
        card.pack(side="left", expand=True, fill="x", padx=(0, 8))
        inner = tk.Frame(card, bg=CARD, padx=14, pady=10)
        inner.pack(fill="x")
        tk.Label(inner, text=caption.upper(), bg=CARD, fg=DIM, font=("Segoe UI", 8, "bold")).pack(anchor="w")
        lbl = tk.Label(inner, text=str(value), bg=CARD, fg=TXT, font=FONT_NUM, anchor="w")
        lbl.pack(anchor="w")
        return lbl

    def _btn(self, parent, text: str, command, accent: bool = False):
        tk = self.tk
        b = tk.Button(parent, text=text, font=FONT, relief="flat", bd=0, padx=14, pady=8,
                      bg=ACC if accent else CARD, fg="#ffffff" if accent else TXT,
                      activebackground="#1d55d0" if accent else "#eef2f8",
                      activeforeground="#ffffff" if accent else TXT,
                      highlightthickness=0 if accent else 1, highlightbackground=LINE,
                      cursor="hand2", command=command)
        b.pack(side="left", padx=(0, 8))
        return b

    # ---------------------------------------------------------------- действия
    def set_mode(self, mode: str) -> None:
        self._safe(lambda: api(self.base, "/api/settings", "POST", {"captcha_mode": mode}))
        self.refresh()

    def toggle_autopilot(self) -> None:
        state = self._last if hasattr(self, "_last") else {}
        current = (state.get("settings") or {}).get("autopilot") == "1"
        self._safe(lambda: api(self.base, "/api/settings", "POST",
                               {"autopilot": "0" if current else "1"}))
        self.refresh()

    def open_ui(self) -> None:
        self._safe(lambda: webbrowser.open(self.base, new=2))

    def export_csv(self) -> None:
        self._safe(lambda: api(self.base, "/api/export", "GET"))

    def reload_catalog(self) -> None:
        self._safe(lambda: api(self.base, "/api/catalog/reload", "POST", {}))

    def _safe(self, fn) -> None:
        try:
            fn()
        except Exception:
            pass

    # ---------------------------------------------------------------- опрос
    def refresh(self) -> None:
        state = api(self.base, "/api/state")
        self._last = state
        s = summary(state)
        tk = self.tk
        self.lbl_online.configure(fg=OK if s["online"] else ERR)
        self.lbl_line1.configure(text=s["line1"])
        self.lbl_line2.configure(text=s["line2"])
        self.lbl_ready.configure(text=str(s["ready"]))
        self.lbl_today.configure(text=str(s["today"]))
        self.btn_auto.configure(text="Автопилот: " + ("вкл" if s["autopilot"] else "выкл"),
                                bg=OK if s["autopilot"] else CARD,
                                fg="#ffffff" if s["autopilot"] else TXT)
        for key, btn in self.mode_buttons.items():
            active = (key == s["mode"])
            btn.configure(bg=ACC if active else CARD, fg="#ffffff" if active else DIM,
                          highlightbackground=ACC if active else LINE)
        lines = events_lines(api(self.base, "/api/events?limit=7"))
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.insert("1.0", "\n".join(lines) if lines else "пока пусто")
        self.log.configure(state="disabled")

    def _poll(self) -> None:
        if not self._alive:
            return
        try:
            self.refresh()
        except Exception:
            pass
        try:
            self.root.after(3000, self._poll)
        except Exception:
            pass

    def close(self) -> None:
        self._alive = False
        self._safe(self.root.destroy)

    def run(self) -> None:
        self.root.mainloop()


def run(base: str) -> bool:
    """Открывает пульт. False — если окно создать не удалось (нет дисплея/tkinter)."""
    try:
        panel = Panel(base)
    except Exception as exc:                       # нет дисплея, нет tkinter и т.п.
        print(f"  Пульт недоступен ({exc}). Открываю интерфейс в браузере.")
        return False
    panel.run()
    return True


def available() -> bool:
    """Есть ли смысл пытаться открыть окно (tkinter + дисплей)."""
    try:
        import tkinter  # noqa: F401
    except Exception:
        return False
    try:
        root = tkinter.Tk()
        root.destroy()
        return True
    except Exception:
        return False
