"""Проверка HTTP-API на живом сервере.

    python tests/api_smoke.py http://127.0.0.1:8777

Скрипт ничего не требует от интерфейса: он дёргает маршруты так же, как это делает
браузер, и проверяет ответы. Все созданные им ссылки и задачи убирает за собой.
"""
import json
import sys
import urllib.error
import urllib.request

import atexit
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = (sys.argv[1] if len(sys.argv) > 1 else "").rstrip("/")
ok, bad = [], []


def check(name: str, cond: bool, extra: str = "") -> None:
    (ok if cond else bad).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f" — {extra}" if extra and not cond else ""))


def start_own_server() -> str:
    """Поднимает отдельный сервер на чистой базе — чтобы прогон был повторяемым."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    data = tempfile.mkdtemp(prefix="vkf-api-")
    env = {**os.environ, "VKFREE_DATA": data, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen([sys.executable, "run.py", "--host", "127.0.0.1",
                             "--port", str(port), "--no-browser"],
                            cwd=str(ROOT), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    atexit.register(lambda: (proc.terminate(), proc.wait(timeout=10)))
    base = f"http://127.0.0.1:{port}"
    for _ in range(80):
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=1) as r:
                if r.status == 200:
                    print(f"(поднял свой сервер на {base}, база: {data})\n")
                    return base
        except Exception:
            time.sleep(0.25)
    raise SystemExit("не удалось поднять тестовый сервер")


def call(path: str, payload=None, method: str | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method=method or ("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


def expect_400(name: str, path: str, payload) -> None:
    code, body = call(path, payload)
    check(name, code == 400 and body.get("ok") is False, f"{code} {body}")


def main() -> int:
    global BASE
    if not BASE:
        BASE = start_own_server()
    print(f"Проверяю API на {BASE}\n")

    print("Служебное")
    code, health = call("/api/health")
    check("GET /api/health → 200", code == 200 and health.get("ok") is True)
    check("в health есть состояние планировщика", "health" in health)
    code, st = call("/api/state")
    check("GET /api/state → 200", code == 200 and st.get("ok") is True)
    for key in ("targets", "tasks", "sites", "services", "settings", "stats"):
        check(f"в state есть «{key}»", key in st, str(list(st))[:160])
    code, _ = call("/api/nope")
    check("неизвестный маршрут → 404", code == 404, str(code))

    print("\nСсылки")
    # если прошлый прогон упал, за собой не убрал — прибираемся
    test_url = "https://vk.com/club555000111"
    for existing in call("/api/state")[1].get("targets", []):
        if existing["url"] == test_url:
            call(f"/api/targets/{existing['id']}", method="DELETE")
    code, t = call("/api/targets", {"name": "API-тест сообщество", "platform": "vk",
                                    "url": test_url})
    tid = t.get("id") if code == 200 else None
    check("POST /api/targets создаёт ссылку", code == 200 and tid, str(t))
    check("тип ссылки определён как сообщество", t.get("kind") == "community", str(t))
    code, t2 = call("/api/targets", {"name": "Дубль", "platform": "vk",
                                     "url": "https://vk.com/club555000111"})
    check("повторная ссылка отклоняется", code == 400, str(t2)[:120])
    expect_400("ссылка не на vk.com отклоняется",
               "/api/targets", {"name": "Чужое", "platform": "vk", "url": "https://example.com/x"})
    code, d = call("/api/detect", {"url": "https://vk.com/id555000111"})
    check("POST /api/detect → person", code == 200 and d.get("kind") == "person", str(d))
    code, up = call(f"/api/targets/{tid}", {"name": "API-тест переименован"})
    check("POST /api/targets/<id> обновляет ссылку", code == 200 and up.get("ok"), str(up))

    print("\nУслуги под ссылку")
    code, sv = call(f"/api/targets/{tid}/services")
    keys = [g["key"] for g in sv.get("groups", [])]
    check("GET /api/targets/<id>/services → 200", code == 200 and sv.get("ok") is True)
    check("для сообщества есть подписчики", any(k.startswith("followers") for k in keys), str(keys))
    check("нет услуг «на личную страницу» у сообщества",
          not any(k == "followers:person" for k in keys), str(keys))
    items = [i for g in sv.get("groups", []) for i in g["items"]]
    check("у каждой услуги есть подпись «+N …»", all(i["gives"].startswith("+") for i in items))
    check("у каждой услуги есть режим захода", all(i.get("mode_hint") for i in items))
    ready = [i for i in items if not i.get("blocked")]
    check("есть хотя бы одна доступная услуга", bool(ready), f"всего {len(items)}")
    if not ready:                      # все окна закрыты — задачи проверить не на чем
        print("\n(все услуги на паузе — блок задач пропущен)")
        print(f"\nИтого: {len(ok)} OK, {len(bad)} провал(ов)")
        return 0 if not bad else 1

    print("\nПлан и задачи")
    pick = ready[0]["id"]
    code, plan = call("/api/plan", {"target_id": tid,
                                    "items": [{"service_id": pick, "claims": 1}],
                                    "autostart": False})
    created = plan.get("created") or []
    check("POST /api/plan ставит задачу", code == 200 and created, str(plan)[:160])
    task_id = created[0]["id"] if created else None
    code, started = call(f"/api/tasks/{task_id}/start", {}, "POST")
    check("POST /api/tasks/<id>/start запускает заход", code == 200 and started.get("ok"), str(started)[:160])
    check("в запуске есть инструкция и срок",
          "instruction" in started or "steps" in started or "deadline" in started, str(list(started))[:160])
    code, fin = call(f"/api/tasks/{task_id}/finish", {"ok": True, "note": "проверка"})
    check("POST /api/tasks/<id>/finish закрывает задачу", code == 200 and fin.get("ok"), str(fin)[:160])
    code, again = call(f"/api/tasks/{task_id}/finish", {"ok": True})
    check("повторное закрытие отклоняется", code == 400, str(again)[:120])

    # для отмены берём другую услугу: первая только что ушла на кулдаун
    pick2 = ready[1]["id"] if len(ready) > 1 else pick
    code, plan2 = call("/api/plan", {"target_id": tid,
                                     "items": [{"service_id": pick2, "claims": 1}],
                                     "autostart": False})
    t2 = (plan2.get("created") or [{}])[0].get("id")
    check("вторая задача создаётся", bool(t2), str(plan2)[:160])
    code, canc = call(f"/api/tasks/{t2}/cancel", {})
    check("POST /api/tasks/<id>/cancel отменяет", code == 200, str(canc)[:120])
    code, retry = call(f"/api/tasks/{t2}/retry", {})
    check("POST /api/tasks/<id>/retry возвращает в очередь", code == 200, str(retry)[:120])

    print("\nСайты и услуги")
    code, mod = call("/api/sites_all/mode", {"mode": "assisted"})
    check("режим «Все сайты» переключается", code == 200 and mod.get("ok"), str(mod)[:120])
    expect_400("неизвестный режим отклоняется", "/api/sites_all/mode", {"mode": "турбо"})
    code, site = call("/api/sites/doctorsmm", {"cooldown_min": 45})
    check("кулдаун сайта меняется", code == 200 and site.get("ok"), str(site)[:120])
    code, svc = call(f"/api/services/{pick}", {"amount": 7})
    check("объём одной услуги меняется", code == 200 and svc.get("ok"), str(svc)[:120])
    code, res = call(f"/api/services/{pick}", {"reset": True})
    check("ручные значения услуги сбрасываются", code == 200, str(res)[:120])

    print("\nНастройки, журнал, экспорт")
    code, sett = call("/api/settings", {"autopilot": "0"})
    check("настройки сохраняются", code == 200 and sett.get("ok"), str(sett)[:120])
    code, ev = call("/api/events?limit=5")
    check("журнал событий отдаётся", code == 200 and isinstance(ev.get("events"), list))
    code, tick = call("/api/tick", {})
    check("POST /api/tick проходит", code == 200, str(tick)[:120])
    req = urllib.request.Request(BASE + "/api/export")
    with urllib.request.urlopen(req, timeout=20) as r:
        csv = r.read().decode("utf-8")
    check("CSV-экспорт отдаётся", r.status == 200 and ";" in csv.split("\n")[0] or "," in csv.split("\n")[0])

    print("\nПлощадки (M1)")
    code, h = call("/api/sites/health")
    # Каталог меняется; проверяем полноту ответа относительно текущего состояния,
    # а не устаревшее число площадок из прежней версии.
    expected_ids = {site['id'] for site in st.get('sites', [])}
    actual_ids = {site.get('site_id') or site.get('id') for site in h.get('sites', [])}
    check("GET /api/sites/health отдаёт площадки", code == 200 and bool(expected_ids) and actual_ids == expected_ids,
          str(h)[:140])
    check("у площадок есть статус и класс",
          all(r.get("status") and r.get("automation") is not None for r in h.get("sites", [])))
    check("в сводке есть счётчики", set(h.get("summary", {})) >= {"total", "working", "captcha"})
    code, chk = call("/api/sites/check", {"site_id": "nakruti"})
    check("POST /api/sites/check проверяет площадку", code == 200 and chk.get("verdict") in
          ("ok", "js", "changed", "dead"), str(chk)[:140])
    expect_400("проверка без site_id отклоняется", "/api/sites/check", {})

    print("\nЦели (M2)")
    code, post = call("/api/targets", {"name": "Цель: пост", "platform": "vk",
                                      "url": "https://vk.com/wall-987_654", "kind": "post"})
    post_id = post.get("id") if code == 200 else tid
    check("ссылка на пост для целей создана", code == 200 and post_id, str(post)[:120])
    items = [{"metric": "likes", "need": 100}, {"metric": "views", "need": 200}]
    code, pv = call("/api/goals/preview", {"target_id": post_id, "items": items, "days": 2})
    check("POST /api/goals/preview считает план", code == 200 and pv.get("items"), str(pv)[:140])
    check("предпросмотр даёт вердикт и раскладку",
          pv.get("items", [{}])[0].get("verdict") in ("feasible", "tight", "impossible", "done")
          and "schedule" in pv)
    code, big = call("/api/goals/preview", {"target_id": post_id, "items": [{"metric": "likes", "need": 9000}],
                                            "hours": 1})
    check("невыполнимая цель честно помечается",
          code == 200 and big["items"][0]["verdict"] == "impossible"
          and big["items"][0]["planned"] < 9000, str(big)[:140])
    expect_400("цель без позиций отклоняется", "/api/goals/preview", {"target_id": post_id, "items": [], "days": 1})
    expect_400("цель без срока отклоняется", "/api/goals/preview", {"target_id": post_id, "items": items})
    expect_400("цель без ссылки отклоняется", "/api/goals/preview", {"items": items, "days": 1})

    code, goal = call("/api/goals", {"target_id": post_id, "items": items, "days": 2})
    gid = goal.get("goal_id")
    check("цель создаётся и ставит заходы", code == 200 and gid and goal.get("created_n", 0) > 0,
          str(goal)[:160])
    code, gl = call("/api/goals")
    check("GET /api/goals отдаёт список", code == 200 and any(g["id"] == gid for g in gl.get("goals", [])))
    code, prog = call(f"/api/goals/{gid}")
    check("прогресс цели считается", code == 200 and prog.get("percent") is not None
          and prog.get("items"), str(prog)[:140])
    code, paused = call(f"/api/goals/{gid}/pause", {})
    check("цель ставится на паузу", code == 200 and paused.get("paused"))
    code, resumed = call(f"/api/goals/{gid}/resume", {})
    check("цель возобновляется", code == 200 and resumed.get("resumed"))
    code, rep = call(f"/api/goals/{gid}/replan", {})
    check("цель пересчитывается", code == 200 and rep.get("removed", 0) > 0, str(rep)[:140])
    code, ext = call(f"/api/goals/{gid}/extend", {"days": 1})
    check("срок цели продлевается", code == 200 and ext.get("deadline_at"), str(ext)[:140])
    code, dead = call(f"/api/goals/{gid}", method="DELETE")
    check("цель удаляется", code == 200 and dead.get("ok"), str(dead)[:140])
    code, gone = call(f"/api/goals/{gid}")
    check("удалённой цели нет", code == 400 and gone.get("ok") is False, f"{code} {gone}")
    call(f"/api/targets/{post_id}", method="DELETE")

    print("\nРобот (M3)")
    code, br = call("/api/browsers")
    check("GET /api/browsers отдаёт движки", code == 200 and len(br.get("backends", [])) == 4, str(br)[:140])
    check("у движков есть команды установки",
          all(b.get("pip") for b in br.get("backends", [])))
    check("быстрый путь curl_cffi учтён", "curl" in br)
    code, hint = call("/api/browsers/install_hint", {"backend": "patchright"})
    check("подсказка по установке движка", code == 200 and hint.get("commands"), str(hint)[:140])
    expect_400("неизвестный движок отклоняется", "/api/browsers/install_hint", {"backend": "magic"})
    expect_400("робот без задачи отклоняется", "/api/tasks/99999/robot", {})
    code, rc = call("/api/sites/no-such-site/robot_check", {"headless": True})
    check("робот на несуществующей площадке отвечает ошибкой", code == 400, f"{code} {rc}")
    code, sett = call("/api/settings", {"browser_backend": "patchright"})
    check("движок сохраняется в настройках", code == 200 and sett.get("settings", {}).get("browser_backend") == "patchright")
    expect_400("неизвестный движок в настройках отклоняется", "/api/settings", {"browser_backend": "телепорт"})
    code, sett2 = call("/api/settings", {"order_email": "robot@example.com", "nojs_enabled": "0"})
    check("e-mail для заказов сохраняется", code == 200 and sett2.get("settings", {}).get("order_email") == "robot@example.com")
    expect_400("кривой e-mail отклоняется", "/api/settings", {"order_email": "не-почта"})
    call("/api/settings", {"browser_backend": "auto", "order_email": "", "nojs_enabled": "1"})

    print("\nУборка")
    check("ссылка удаляется", call(f"/api/targets/{tid}", method="DELETE")[0] == 200)

    print(f"\nИтого: {len(ok)} OK, {len(bad)} провал(ов)")
    if bad:
        print("Провалы: " + ", ".join(bad))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
