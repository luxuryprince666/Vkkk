"""Живая проверка интерфейса через Playwright.

Запуск (сервер должен быть уже поднят):

    python run.py                      # в одном окне
    python tests/ui_smoke.py http://127.0.0.1:8777

Скрипт проходит путь новичка: «Накрутить» → выбрать ссылку → выбрать услугу →
заход с таймером → «Готово». Плюс проверяет остальные вкладки, API и то, что в
консоли браузера нет ошибок (в том числе старого 501 «{}GET»).
"""
import asyncio
import json
import sys
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8777").rstrip("/")
ok, bad = [], []


def check(name: str, cond: bool, extra: str = "") -> None:
    (ok if cond else bad).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f" — {extra}" if extra and not cond else ""))


def api(path: str, payload=None, method: str | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method=method or ("POST" if data else "GET"))
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def ensure_target(kind_url: str, name: str) -> int:
    """Возвращает id ссылки — существующей или только что добавленной."""
    for t in api("/api/state")["targets"]:
        if t["url"] == kind_url:
            return int(t["id"])
    return int(api("/api/targets", {"name": name, "platform": "vk", "url": kind_url})["id"])


async def main() -> int:
    print(f"Проверяю интерфейс на {BASE}\n")
    community_id = ensure_target("https://vk.com/club228822", "Тестовое сообщество")

    # ---------------------------------------------------------------- API
    print("API")
    st = api("/api/state")
    check("GET /api/state отвечает", st.get("ok") is True)
    svc = api("/api/targets/%d/services" % community_id)
    groups = [g["key"] for g in svc["groups"]]
    check("услуги для сообщества сгруппированы", bool(groups), str(groups))
    check("есть группа «подписчики в сообщество»",
          any(g.startswith("followers") for g in groups), str(groups))
    check("у услуг есть человеческая подпись «+N …»",
          all(i["gives"].startswith("+") for g in svc["groups"] for i in g["items"]))
    det = api("/api/detect", {"url": "https://vk.com/id998877"})
    check("тип ссылки определяется по адресу (личная страница)", det["kind"] == "person", str(det))
    det2 = api("/api/detect", {"url": "https://vk.com/club998877"})
    check("тип ссылки определяется по адресу (сообщество)", det2["kind"] == "community", str(det2))

    # ---------------------------------------------------------------- интерфейс
    async with async_playwright() as p:
        # путь к скачанному Chromium подставляем явно: у разных версий Playwright
        # свои сборки, и без этого падает «Executable doesn't exist»
        from core import stealth
        launch = {"args": ["--no-sandbox"]}
        exe = stealth._find_chrome()
        if exe:
            launch["executable_path"] = exe
        browser = await p.chromium.launch(**launch)
        page = await browser.new_page(viewport={"width": 1440, "height": 950})
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))

        await page.goto(BASE + "/", wait_until="load")
        await page.wait_for_timeout(1500)

        print("\nЭкран «Накрутить»")
        check("вкладка «Накрутить» открыта по умолчанию",
              await page.eval_on_selector("nav button[data-tab='wizard']", "el => el.classList.contains('active')"))
        check("навигация: «Накрутить», «Цели», «Очередь»",
              (await page.eval_on_selector_all("nav button.hot", "els => els.map(e => e.dataset.tab).join(',')")) == "wizard,goals,queue")
        await page.wait_for_selector("#wz-pick .pickcard", timeout=15000)
        cards = await page.eval_on_selector_all("#wz-pick .pickcard", "els => els.length")
        check("видны карточки ссылок", cards >= 1, f"карточек: {cards}")
        check("шаг 1 подписан по-человечески",
              "Куда накручиваем" in await page.inner_text("#wz-step-1 .wz-title"))
        sections = await page.evaluate(
            "() => [...document.querySelectorAll('[id^=tab-]')].filter(e => e.offsetParent !== null).map(e => e.id)")
        check("на экране ровно одна вкладка", sections == ["tab-wizard"], str(sections))

        print("\nДобавление ссылки с автодетектом")
        await page.evaluate("wzToggleAdd()")
        await page.fill("#wz-url", "https://vk.com/id771122")
        await page.wait_for_timeout(900)
        hint = await page.inner_text("#wz-kind-hint")
        check("подсказка про личную страницу появилась", "личн" in hint.lower(), hint)
        check("площадка выбрана — ВКонтакте",
              (await page.eval_on_selector("#wz-platform", "el => el.value")) == "vk")

        print("\nШаг 2: выбор услуги")
        await page.evaluate("document.querySelectorAll('#wz-pick .pickcard')[0].click()")
        await page.wait_for_selector("#wz-groups .svccard", timeout=15000)
        gcount = await page.eval_on_selector_all("#wz-groups .wzgroup", "els => els.length")
        scount = await page.eval_on_selector_all("#wz-groups .svccard", "els => els.length")
        check("группы услуг отрисованы", gcount >= 1, f"групп: {gcount}")
        check("карточки услуг отрисованы", scount >= 1, f"карточек: {scount}")
        check("у карточки есть кнопка «Накрутить»",
              (await page.eval_on_selector_all("#wz-groups .svccard button",
                                               "els => els.some(b => b.textContent.includes('Накрутить'))")) is True)
        check("фильтр «только доступные сейчас» есть",
              await page.eval_on_selector_all("#wz-groups input[type=checkbox]", "els => els.length>0"))

        print("\nШаг 3: заход с инструкцией")
        clicked = await page.evaluate("""() => {
            const b = [...document.querySelectorAll('#wz-groups .svccard button')]
                        .find(x => x.textContent.includes('Накрутить'));
            if (!b) return false; b.click(); return true; }""")
        check("кнопка «Накрутить» нажимается", clicked)
        await page.wait_for_selector("#wz-step-3", state="visible", timeout=20000)
        text = await page.inner_text("#wz-run")
        check("шаг 3 показан", True)
        check("в инструкции есть ссылка на объект", "vk.com" in text, text[:120])
        check("в инструкции сказано про буфер обмена", "буфер" in text.lower(), text[:160])
        check("есть таймер следующего захода", ":" in text and ("таймер" in text.lower() or "мин" in text.lower()))
        check("есть кнопка «Готово»", "Готово" in text)

        await page.screenshot(path="shot-wizard-run.png", full_page=True)
        await page.evaluate("document.querySelector('#wz-run button.primary').click()")
        await page.wait_for_timeout(2500)
        check("после «Готово» шаг 3 скрылся",
              await page.eval_on_selector("#wz-step-3", "el => el.style.display === 'none'"))
        await page.wait_for_timeout(800)
        await page.screenshot(path="shot-wizard.png", full_page=True)

        print("\nПлощадки (M1)")
        await page.evaluate("switchTab('platforms')")
        await page.wait_for_timeout(1200)
        rows = await page.eval_on_selector_all("#pf-list tbody tr", "els => els.length")
        check("таблица площадок отрисована", rows >= 20, f"строк: {rows}")
        summary = await page.inner_text("#pf-summary")
        check("сводка по площадкам видна", "работают" in summary, summary)
        classes = await page.eval_on_selector_all("#pf-list tbody tr td:nth-child(2)", "els => els.map(e => e.textContent.trim()).filter(Boolean)")
        check("у площадок есть класс автопилота", len(classes) >= 20, f"классов: {len(classes)}")
        check("есть кнопка «Проверить»", await page.eval_on_selector("#pf-list", "el => !!el.querySelector('button')"))
        check("есть кнопка «Робот»", await page.eval_on_selector("#pf-list", "el => !!el.querySelector('button')"))
        btns = await page.eval_on_selector_all("#pf-list tbody tr:first-child button", "els => els.map(e => e.textContent.trim())")
        check("у площадки есть «Робот» и «Форма»", "Робот" in btns and "Форма" in btns, str(btns))
        banner = await page.inner_text("#pf-browsers")
        check("плашка движка робота видна", len(banner) > 10, banner[:120])
        await page.screenshot(path="shot-platforms.png", full_page=False)

        print("\nНастройки робота")
        await page.evaluate("switchTab('settings')")
        await page.wait_for_timeout(1200)
        check("выбор движка есть",
              await page.eval_on_selector("#st-browser_backend", "el => el.options.length >= 4"))
        check("поле e-mail для заказов есть", await page.eval_on_selector("#st-order_email", "el => !!el"))
        st_browsers = await page.inner_text("#st-browsers")
        check("список движков отрисован", ("Patchright" in st_browsers or "Playwright" in st_browsers), st_browsers[:140])
        await page.screenshot(path="shot-settings-robot.png", full_page=True)

        print("\nЦели (M2)")
        await page.evaluate("switchTab('goals')")
        await page.wait_for_timeout(1200)
        check("вкладка целей открывается",
              await page.eval_on_selector("#tab-goals", "el => el.offsetParent !== null"))
        check("форма цели на месте", await page.eval_on_selector("#gl-metrics .gl-metric", "el => !!el"))
        await page.evaluate("""
            (async () => {
              const r = await api('/api/targets', 'POST',
                {name: 'UI-пост', platform: 'vk', url: 'https://vk.com/wall-555_1', kind: 'post'});
              return r;
            })()
        """)
        await page.evaluate("goalsLoad()")
        await page.wait_for_timeout(900)
        picked = await page.evaluate("""() => {
            const sel = document.getElementById('gl-target');
            const opt = [...sel.options].find(o => o.textContent.includes('UI-пост'));
            if (opt) sel.value = opt.value;
            return sel.value;
        }""")
        check("цель можно привязать к ссылке на пост", bool(picked), str(picked))
        check("ссылки подставились в выбор цели",
              await page.eval_on_selector("#gl-target", "el => el.options.length >= 1"))
        await page.evaluate("glReset(); glAddRow('likes', 100); glPreview()")
        await page.wait_for_timeout(2000)
        result = await page.inner_text("#gl-result")
        check("расчёт цели показывает вердикт",
              ("выполнимо" in result or "впритык" in result or "невыполнимо" in result), result[:120])
        check("расчёт цели показывает заходы", "заход" in result, result[:160])
        check("есть кнопка запуска цели",
              await page.eval_on_selector("#gl-result", "el => !!el.querySelector('button.primary')"))
        await page.screenshot(path="shot-goals.png", full_page=False)
        await page.evaluate("glStart()")
        await page.wait_for_timeout(1800)
        cards = await page.eval_on_selector_all("#gl-list .gl-card", "els => els.length")
        check("карточка цели появилась", cards >= 1, f"карточек: {cards}")
        bars = await page.eval_on_selector_all("#gl-list .gl-bar > i", "els => els.length")
        check("прогресс цели нарисован полосками", bars >= 1, f"полосок: {bars}")
        await page.screenshot(path="shot-goals-list.png", full_page=False)

        print("\nОстальные вкладки")
        for tab, marker in [("queue", "Очередь"), ("dash", "Сводка"), ("targets", "Ссылки"),
                            ("catalog", "Все услуги"), ("log", "Журнал"), ("settings", "Настройки")]:
            await page.evaluate(f"switchTab('{tab}')")
            await page.wait_for_timeout(700)
            visible = await page.eval_on_selector(f"#tab-{tab}", "el => el.offsetParent !== null")
            check(f"вкладка «{marker}» открывается", visible)

        print("\nКонсоль браузера")
        real = [e for e in errors if "favicon" not in e.lower()]
        check("ошибок в консоли нет", not real, "; ".join(real[:3]))
        check("нет старой ошибки 501", not any("501" in e for e in real), "; ".join(real[:3]))

        await browser.close()

    print(f"\nИтого: {len(ok)} OK, {len(bad)} провал(ов)")
    if bad:
        print("Провалы: " + ", ".join(bad))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
