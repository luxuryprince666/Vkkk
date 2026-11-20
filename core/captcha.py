# -*- coding: utf-8 -*-
"""Бесплатные попытки решения некоторых капч без гарантии результата.

Текстовые изображения распознаются локальным ddddocr. Результат считается
только попыткой до проверки площадкой. Для видимых виджетов можно дождаться
токена и попробовать чекбокс; сложные задания остаются ручными. Cloudscraper
— отдельный вспомогательный клиент, не подключённый к автоматическому заходу.
"""
from __future__ import annotations

import base64
import contextlib
import io
import json
import random
import re
import time
import tempfile
from pathlib import Path
from dataclasses import dataclass, field

# ------------------------------------------------------------------ состояние решателей

OCR_MODULE = "ddddocr"
CF_MODULE = "cloudscraper"
PYP_PASSER_MODULE = "pypasser"


def _have(module: str) -> bool:
    try:
        __import__(module)
        return True
    except Exception:
        return False


_OCR_ENGINE = None
_OCR_FAILED = False


def ocr_engine():
    """Ленивая загрузка ddddocr: 0.1 с, никакой сети, одна модель на процесс."""
    global _OCR_ENGINE, _OCR_FAILED
    if _OCR_ENGINE is not None or _OCR_FAILED:
        return _OCR_ENGINE
    try:
        import ddddocr  # noqa: WPS433 — тяжёлый импорт, только по требованию
        _OCR_ENGINE = ddddocr.DdddOcr(show_ad=False)
    except Exception:
        _OCR_FAILED = True
    return _OCR_ENGINE


def solvers_status() -> list[dict]:
    """Что из «капча-арсенала» готово на этой машине — для интерфейса и диагностики."""
    ocr_ok = _have(OCR_MODULE)
    cf_ok = _have(CF_MODULE)
    pp_ok = _have(PYP_PASSER_MODULE)
    return [
        {"id": "click", "title": "Галочка «я не робот»",
         "why": "Turnstile, reCAPTCHA, hCaptcha: ждём автопроход и кликаем чекбокс мышью",
         "pip": "", "ready": True},
        {"id": "ocr", "title": "Текстовая капча (OCR)",
         "why": "локальное распознавание картинки, офлайн и бесплатно",
         "pip": "pip install ddddocr", "ready": ocr_ok},
        {"id": "cloudflare", "title": "Cloudflare «Just a moment…»",
         "why": "ожидание проверки; cloudscraper — обход IUAM и выдача cf_clearance",
         "pip": "pip install cloudscraper", "ready": cf_ok},
        {"id": "recaptcha_v3", "title": "reCAPTCHA v3 (аудио)",
         "why": "pypasser — обход невидимой reCAPTCHA v3 через аудио-челлендж, без участия человека",
         "pip": "pip install pypasser", "ready": pp_ok},
        {"id": "human", "title": "Человек в окне",
         "why": "при видимом окне задачи можно решить капчу и подтвердить в интерфейсе",
         "pip": "", "ready": True},
    ]


# ------------------------------------------------------------------ что вообще на странице

WIDGETS_JS = r"""
(() => {
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 20 && r.height > 12 && st.visibility !== 'hidden' && st.display !== 'none'
           && parseFloat(st.opacity || '1') > 0.2;
  };
  const out = [];
  for (const f of document.querySelectorAll('iframe')) {
    if (!vis(f)) continue;
    const s = ((f.src || '') + ' ' + (f.getAttribute('title') || '')).toLowerCase();
    let kind = '';
    if (s.includes('turnstile') || s.includes('challenges.cloudflare')) kind = 'turnstile';
    else if (s.includes('hcaptcha')) kind = 'hcaptcha';
    else if (s.includes('recaptcha')) kind = 'recaptcha';
    if (!kind) continue;
    const r = f.getBoundingClientRect();
    out.push({kind: kind, x: Math.round(r.x), y: Math.round(r.y),
              w: Math.round(r.width), h: Math.round(r.height), src: s.slice(0, 100)});
  }
  return JSON.stringify(out);
})()
"""

HOLDERS_JS = r"""
(() => {
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 10 && r.height > 8 && st.visibility !== 'hidden' && st.display !== 'none';
  };
  const SEL = "[id*='captcha'], [class*='captcha'], [id^='cf-chl-widget'], [class*='turnstile']," +
              " [class*='h-captcha'], [class*='g-recaptcha'], [data-sitekey]";
  const out = [];
  for (const el of document.querySelectorAll(SEL)) {
    if (!vis(el)) continue;
    const inner = el.querySelector("input[name*='turnstile'], input[name*='recaptcha']," +
                                   " input[name*='hcaptcha'], iframe, [role='checkbox']");
    const r = el.getBoundingClientRect();
    out.push({x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height),
              hasInner: !!inner, id: (el.id || '').slice(0, 40)});
  }
  // самый «капчевый» контейнер: с полем токена внутри и разумного размера
  out.sort((a, b) => (b.hasInner ? 1 : 0) - (a.hasInner ? 1 : 0) || (a.w * a.h) - (b.w * b.h));
  return JSON.stringify(out.slice(0, 3));
})()
"""

TOKEN_JS = r"""
(() => {
  const names = ['cf-turnstile-response', 'g-recaptcha-response', 'h-captcha-response'];
  for (const n of names) {
    const el = document.querySelector('[name="' + n + '"]');
    if (el && (el.value || '').length > 10) return n;
  }
  for (const n of names) {
    const els = [...document.querySelectorAll('#' + n)];
    for (const el of els) if ((el.value || el.textContent || '').length > 10) return n;
  }
  return '';
})()
"""

IMAGE_JS = r"""
(() => {
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 24 && r.height > 14 && st.visibility !== 'hidden' && st.display !== 'none';
  };
  const WORDS = ['captcha', 'капч', 'код', 'code', 'verify', 'secur', 'protect', 'check'];
  const imgs = [...document.querySelectorAll('img, canvas')].filter(vis);
  const scored = [];
  for (const el of imgs) {
    const src = (el.currentSrc || el.src || '').toLowerCase();
    let blob = src + ' ' + ((el.id || '') + ' ' + (el.className || '') + ' ' +
                            (el.alt || '') + ' ' + (el.getAttribute('name') || '')).toLowerCase();
    let box = el.parentElement;
    for (let i = 0; i < 2 && box; i++) { blob += ' ' + ((box.className || '') + ' ' + (box.id || '')).toLowerCase(); box = box.parentElement; }
    let score = 0;
    if (WORDS.some((w) => blob.includes(w))) score += 5;
    const r = el.getBoundingClientRect();
    if (r.width >= 60 && r.width <= 320 && r.height >= 20 && r.height <= 110) score += 3;
    if (el.tagName === 'CANVAS') score += 1;
    if (score >= 5) scored.push({el: el, score: score});
  }
  if (!scored.length) return '[]';
  // селектор для лучшего кандидата
  const css = (el) => {
    if (el.id && document.querySelectorAll('#' + CSS.escape(el.id)).length === 1)
      return '#' + CSS.escape(el.id);
    const parent = el.parentElement;
    if (parent) {
      const same = [...parent.children].filter((c) => c.tagName === el.tagName);
      if (same.length === 1 && parent.id) return '#' + CSS.escape(parent.id) + ' ' + el.tagName.toLowerCase();
    }
    const name = el.getAttribute('name');
    if (name) return el.tagName.toLowerCase() + '[name="' + name + '"]';
    let path = [];
    let node = el;
    while (node && node.nodeType === 1 && path.length < 6) {
      let part = node.tagName.toLowerCase();
      const p = node.parentNode;
      if (p) {
        const same = [...p.children].filter((c) => c.tagName === node.tagName);
        if (same.length > 1) part += ':nth-of-type(' + (same.indexOf(node) + 1) + ')';
      }
      path.unshift(part);
      node = node.parentNode;
    }
    return path.join(' > ');
  };
  scored.sort((a, b) => b.score - a.score);
  const best = scored[0].el;
  if (!best) return '[]';
  return JSON.stringify([{sel: css(best), src: (best.currentSrc || best.src || '').slice(0, 200),
                          tag: best.tagName}]);
})()
"""

INPUT_FOR_CODE_JS = r"""
(() => {
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 40 && r.height > 12 && getComputedStyle(el).visibility !== 'hidden';
  };
  const WORDS = ['captcha', 'капч', 'код', 'code', 'verify', 'pin'];
  const inputs = [...document.querySelectorAll('input[type=text], input:not([type])')].filter(vis);
  for (const el of inputs) {
    const blob = ((el.name || '') + ' ' + (el.id || '') + ' ' + (el.placeholder || '') + ' ' +
                  (el.className || '')).toLowerCase();
    if (WORDS.some((w) => blob.includes(w))) {
      if (el.id && document.querySelectorAll('#' + CSS.escape(el.id)).length === 1)
        return '#' + CSS.escape(el.id);
      if (el.name) {
        const sel = 'input[name="' + CSS.escape(el.name) + '"]';
        if (document.querySelectorAll(sel).length === 1) return sel;
      }
    }
  }
  // Не подставляем код в произвольное поле заказа.
  return '';
})()
"""


# ------------------------------------------------------------------ результат

@dataclass
class Solved:
    ok: bool
    kind: str = ""
    detail: str = ""
    steps: list = field(default_factory=list)


# ------------------------------------------------------------------ 1) галочка «я не робот»

def visible_widgets(page) -> list[dict]:
    """Visible iframes including those nested inside shadow DOM, when supported."""
    try:
        found = json.loads(page.js(WIDGETS_JS) or "[]")
    except Exception:
        found = []
    getter = getattr(page, "captcha_frames", None)
    if getter:
        try:
            for widget in getter():
                if not any(abs(w["x"] - widget["x"]) < 3 and
                           abs(w["y"] - widget["y"]) < 3 for w in found):
                    found.append(widget)
        except Exception:
            pass
    return found


# Панельные капчи-«подарки» (SMMYT и подобные) часто лежат в display:none блоке:
# пока блок не показать, iframe существует с нулевой геометрией и кликнуть в него нельзя.
_FORCE_SHOW_CAPTCHA_JS = """(() => {
  let touched = 0;
  document.querySelectorAll('#captcha_block_id, .captcha_block, .captcha_block2').forEach(b => {
    if (getComputedStyle(b).display === 'none' || b.getBoundingClientRect().width === 0) {
      b.style.display = 'block'; b.style.opacity = '1'; b.style.position = 'relative';
      b.style.visibility = 'visible'; touched += 1;
    }
  });
  return touched;
})()"""


def force_show_hidden_captcha(page) -> int:
    """Раскрыть скрытые контейнеры капчи; вернуть число изменённых блоков."""
    try:
        return int(page.js(_FORCE_SHOW_CAPTCHA_JS) or 0)
    except Exception:
        return 0


COLOR_GATE_JS = """(() => {
  const box = document.querySelector('#content');
  if (!box) return null;
  const ask = [...box.querySelectorAll('p')].find(e => (e.innerText || '').includes('похожий цвет'));
  if (!ask) return null;
  const opts = [...box.querySelectorAll('[onclick]')].filter(e => e.getClientRects().length);
  if (!opts.length) return null;
  const rgb = (s) => (String(s).match(/\\d+/g) || []).slice(0, 3).map(Number);
  const goal = rgb(getComputedStyle(ask.previousElementSibling).backgroundColor);
  if (goal.length < 3) return null;
  let best = null, bd = Infinity;
  for (const e of opts) {
    const c = rgb(getComputedStyle(e).backgroundColor);
    if (c.length < 3) continue;
    const d = (c[0]-goal[0])*(c[0]-goal[0]) + (c[1]-goal[1])*(c[1]-goal[1]) + (c[2]-goal[2])*(c[2]-goal[2]);
    if (d < bd) { bd = d; best = e; }
  }
  if (!best) return null;
  best.click();
  return bd;
})()"""


def color_gate_present(text: str) -> bool:
    """SMMYT-стиль «Антибот Клауд»: выбрать похожий цвет/картинку из плиток."""
    low = (text or '').lower()
    return 'похожий цвет' in low or 'похожую картинку' in low


def solve_color_gate(page, *, tries: int = 4, steps: list | None = None) -> Solved:
    """Кликнуть по плитке с цветом, ближайшим к эталонному (проверка на человечность)."""
    steps = steps if steps is not None else []
    for attempt in range(tries):
        try:
            dist = page.js(COLOR_GATE_JS)
        except Exception:
            dist = None
        page.wait(1500)
        if dist is not None and not color_gate_present((page.text() or '')[:400]):
            steps.append(f"цветовая проверка решена (расхождение RGB {dist})")
            return Solved(True, "color_gate", "проверка «похожий цвет» пройдена автоматически", steps)
        page.wait(900)
    steps.append("цветовая проверка не решена")
    return Solved(False, "color_gate", "цветовая проверка не пройдена", steps)


def _tokens_present(page) -> str:
    try:
        return str(page.js(TOKEN_JS) or "")
    except Exception:
        return ""


def _scroll_to_captcha(page) -> dict | None:
    """Прокрутить страницу к капче и вернуть её габариты.

    Turnstile в «невидимом» режиме (например, prskill) вообще не рисует iframe, пока
    контейнер не попадёт в видимую область. Поэтому сначала подводим экран к капче,
    а уже потом ищем виджет и ждём токен.
    """
    try:
        holders = json.loads(page.js(HOLDERS_JS) or "[]")
    except Exception:
        return None
    if not holders:
        return None
    page.js("""(() => {
      const SEL = "[id*='captcha'], [class*='captcha'], [id^='cf-chl-widget'], [class*='turnstile']," +
                  " [class*='h-captcha'], [class*='g-recaptcha'], [data-sitekey]";
      const el = [...document.querySelectorAll(SEL)].find((e) => {
        const r = e.getBoundingClientRect();
        return r.width > 10 && r.height > 8;
      });
      if (el) el.scrollIntoView({block: 'center'});
    })()""")
    page.wait(900)
    try:
        again = json.loads(page.js(HOLDERS_JS) or "[]")
    except Exception:
        again = holders
    return (again or holders)[0]


def _wait_token(page, seconds: float) -> str:
    fin = time.time() + max(1.0, seconds)
    while time.time() < fin:
        token = _tokens_present(page)
        if token:
            return token
        page.wait(500)
    return ""


def _click_checkbox_in_frame(page, widget: dict) -> bool:
    """Точный клик по чекбоксу внутри iframe (там, где движок даёт доступ к фреймам)."""
    getter = getattr(page, "frames_with", None)
    if not getter:
        return False
    part = {"turnstile": "challenges.cloudflare", "hcaptcha": "hcaptcha",
            "recaptcha": "recaptcha"}.get(widget.get("kind") or "", "")
    if not part:
        return False
    for frame in getter(part):
        for sel in ("#checkbox", "input[type=checkbox]", "label#checkbox",
                    "div#checkbox", "[role=checkbox]", "label"):
            try:
                loc = frame.locator(sel).first
                if loc.count() == 0:
                    continue
                loc.click(timeout=4000)
                return True
            except Exception:
                continue
    return False


def solve_checkbox(page, *, auto_wait: float = 6.0, tries: int = 2, steps: list | None = None) -> Solved:
    """Turnstile / reCAPTCHA / hCaptcha: сначала ждём автопроход, потом жмём чекбокс."""
    steps = steps if steps is not None else []

    token = _wait_token(page, auto_wait)
    if token:
        steps.append(f"капча прошла сама (токен {token}) — ничего не нажимал")
        return Solved(True, "checkbox", "капча прошла автоматически, токен получен", steps)

    for attempt in range(tries):
        widgets = visible_widgets(page)
        if not widgets:
            token = _wait_token(page, 2.0)
            if token:
                steps.append("токен появился без виджета — капчи уже нет")
                return Solved(True, "checkbox", "капча прошла сама", steps)
            holder = _scroll_to_captcha(page)
            if holder:
                widgets = [{"kind": "turnstile", "x": holder["x"], "y": holder["y"],
                            "w": max(60, holder["w"]), "h": max(40, holder["h"]),
                            "src": "контейнер " + (holder.get("id") or "")}]
                steps.append("виджет без iframe — работаю по контейнеру капчи")
            else:
                return Solved(False, "checkbox", "виджет капчи не найден", steps)

        widget = widgets[0]
        steps.append(f"виджет {widget['kind']}: {widget['w']}×{widget['h']} в ({widget['x']}, {widget['y']})")

        # 1) настоящая мышь по левому краю виджета — там чекбокс
        page.wait(random.randint(400, 900))
        x = widget["x"] + 28
        y = widget["y"] + max(18, widget["h"] // 2)
        clicked = page.click_at(x, y)
        if clicked:
            steps.append("кликнул по чекбоксу мышью")
            token = _wait_token(page, 5.0)
            if token:
                return Solved(True, "checkbox", f"капча решена кликом (токен {token})", steps)

        # 1b) SMMYT-стиль: чекбокс reCAPTCHA существует отдельно от виджета-обёртки —
        # пробуем точную геометрию якоря по координатам
        anchor = None
        try:
            anchor = page.box('iframe[title="reCAPTCHA"]')
        except Exception:
            anchor = None
        if anchor and anchor.get("w", 0) > 20:
            page.wait(random.randint(300, 700))
            if page.click_at(anchor["x"] + 28, anchor["y"] + max(18, anchor["h"] // 2)):
                steps.append("кликнул по чекбоксу reCAPTCHA по координатам якоря")
                token = _wait_token(page, 12.0)
                if token:
                    return Solved(True, "checkbox", "reCAPTCHA решена кликом по якорю", steps)

        # 2) точный клик внутри фрейма (если движок даёт доступ)
        if _click_checkbox_in_frame(page, widget):
            steps.append("кликнул по чекбоксу внутри фрейма")
            token = _wait_token(page, 5.0)
            if token:
                return Solved(True, "checkbox", "капча решена кликом внутри фрейма", steps)

        steps.append(f"попытка {attempt + 1}: токена нет")
        page.wait(random.randint(900, 1800))

    return Solved(False, "checkbox", "капча не пройдена автоматически", steps)


# ------------------------------------------------------------------ 2) текстовая капча (OCR)

def _img_bytes(page, sel: str, src: str) -> bytes | None:
    """Байты картинки: сначала напрямую, затем снимком с экрана и обрезкой."""
    if src.startswith("data:image"):
        try:
            return base64.b64decode(src.split(",", 1)[1])
        except Exception:
            pass
    if src:
        data_url = page.js("""(async () => {
          try {
            const el = document.querySelector(%r);
            const url = el ? (el.currentSrc || el.src) : %r;
            const r = await fetch(url, {credentials: 'include'});
            const b = await r.blob();
            return await new Promise((res) => {const fr = new FileReader();
              fr.onload = () => res(fr.result); fr.onerror = () => res(''); fr.readAsDataURL(b);});
          } catch (e) { return ''; }
        })()""" % (sel, src))
        if isinstance(data_url, str) and data_url.startswith("data:image"):
            try:
                return base64.b64decode(data_url.split(",", 1)[1])
            except Exception:
                pass

    # запасной путь: снимок окна и обрезка по координатам картинки
    rect = page.box(sel)
    if not rect:
        return None
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp:
        temp_path = temp.name
    shot = page.shot(temp_path)
    if not shot:
        Path(temp_path).unlink(missing_ok=True)
        return None
    try:
        from PIL import Image
        img = Image.open(shot)
        scale = img.width / float(page.js("window.innerWidth") or img.width)
        left = int(rect["x"] * scale)
        top = int(rect["y"] * scale)
        right = int((rect["x"] + rect["w"]) * scale)
        bottom = int((rect["y"] + rect["h"]) * scale)
        crop = img.crop((max(0, left), max(0, top), min(img.width, right), min(img.height, bottom)))
        buf = io.BytesIO()
        crop.save(buf, "PNG")
        return buf.getvalue()
    except Exception:
        return None
    finally:
        Path(temp_path).unlink(missing_ok=True)


def _ocr_guess(png: bytes) -> str:
    engine = ocr_engine()
    if not engine or not png:
        return ""
    variants = [png]
    try:                                     # второй заход — увеличенная и обесцвеченная картинка
        from PIL import Image
        img = Image.open(io.BytesIO(png)).convert("L")
        img = img.resize((img.width * 2, img.height * 2))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        variants.append(buf.getvalue())
    except Exception:
        pass
    for data in variants:
        try:
            guess = str(engine.classification(data) or "")
        except Exception:
            continue
        guess = re.sub(r"[^0-9A-Za-zА-Яа-яЁё]", "", guess).strip()
        if 2 <= len(guess) <= 12:
            return guess
    return ""


def solve_image(page, *, tries: int = 3, steps: list | None = None) -> Solved:
    """Текстовая капча: распознаём локально и вписываем. Не вышло — просим новую картинку."""
    steps = steps if steps is not None else []
    if not ocr_engine():
        return Solved(False, "image", "нет движка распознавания (pip install ddddocr)", steps)

    for attempt in range(tries):
        try:
            found = json.loads(page.js(IMAGE_JS) or "[]")
        except Exception:
            found = []
        if not found:
            return Solved(False, "image", "картинку капчи не нашёл", steps)
        target = found[0]
        sel = target.get("sel") or ""
        if not sel:
            return Solved(False, "image", "не смог построить селектор картинки", steps)

        png = _img_bytes(page, sel, target.get("src") or "")
        if not png:
            return Solved(False, "image", "картинку капчи не удалось забрать", steps)
        guess = _ocr_guess(png)
        if not guess:
            steps.append(f"попытка {attempt + 1}: OCR не разобрал картинку")
        else:
            field = page.js(INPUT_FOR_CODE_JS) or ""
            if not field:
                return Solved(False, "image", "не нашёл поле для кода с картинки", steps)
            from core import drivers          # локальный импорт: drivers сам зовёт captcha
            filled = drivers.set_field(page, field, guess)
            if not filled:
                return Solved(False, "image", "не удалось заполнить поле капчи", steps)
            steps.append(f"распознал код и заполнил поле {field}; правильность проверит сайт")
            return Solved(True, "image", "код введён, ожидается проверка площадкой", steps)

        # просим другую картинку: обычно достаточно кликнуть по ней
        with contextlib.suppress(Exception):
            page.click_human(sel)
        page.wait(random.randint(700, 1500))

    return Solved(False, "image", "капчу с картинки распознать не удалось", steps)


# ------------------------------------------------------------------ 3) Cloudflare «Just a moment…»

CF_TEXT = ("just a moment", "checking your browser", "проверка браузера", "проверяем ваш браузер",
           "ddos protection", "enable javascript and cookies", "attention required")


def is_cloudflare_wall(text: str) -> bool:
    low = (text or "").lower()
    return any(w in low for w in CF_TEXT)


def solve_cloudflare(page, *, wait_sec: float = 25.0, steps: list | None = None) -> Solved:
    """Страница-заглушка Cloudflare: чаще всего стелс-движок проходит её сам за 5–20 секунд."""
    steps = steps if steps is not None else []
    if not is_cloudflare_wall(page.text()):
        return Solved(False, "cloudflare", "заглушки Cloudflare нет", steps)

    steps.append("страница Cloudflare «Just a moment…» — жду автопроход")
    fin = time.time() + max(5.0, wait_sec)
    while time.time() < fin:
        page.wait(1500)
        if not is_cloudflare_wall(page.text()):
            steps.append(f"челлендж пройден за {int(wait_sec - (fin - time.time()))} с")
            return Solved(True, "cloudflare", "Cloudflare пропустил после челленджа", steps)
        token = _tokens_present(page)
        if token:
            steps.append("появился токен — челлендж идёт")
    return Solved(False, "cloudflare", "Cloudflare не пропустил за отведённое время", steps)


def cf_clearance(url: str, timeout: float = 25.0) -> dict | None:
    """Заход без браузера через cloudscraper: получаем cf_clearance и HTML страницы."""
    if not _have(CF_MODULE):
        return None
    try:
        import cloudscraper
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False})
        res = scraper.get(url, timeout=timeout)
        cookies = {c.name: c.value for c in scraper.cookies}
        return {"status": res.status_code, "text": res.text, "cookies": cookies}
    except Exception:
        return None


def solve_recaptcha_v3(page, *, timeout: float = 20.0, steps: list | None = None) -> Solved:
    """reCAPTCHA v3: pypasser — обход через аудио-челлендж, без участия человека.

    Невидимая капча (нет чекбокса, нет картинок): pypasser пытается пройти
    аудио-верификацию и получить валидный токен g-recaptcha-response.
    """
    steps = steps if steps is not None else []
    if not _have(PYP_PASSER_MODULE):
        return Solved(False, "recaptcha_v3", "pypasser не установлен", steps)
    try:
        import pypasser  # type: ignore
        # pypasser нужен доступ к странице; пробуем через API библиотеки
        result = pypasser.solve(page, timeout=timeout)
        if result and result.get("token"):
            steps.append("reCAPTCHA v3 пройдена через аудио (pypasser)")
            return Solved(True, "recaptcha_v3", "токен получен через аудио-челлендж", steps)
        steps.append("pypasser не вернул токен")
    except Exception as exc:
        steps.append(f"pypasser: {str(exc)[:120]}")
    return Solved(False, "recaptcha_v3", "reCAPTCHA v3 не пройдена автоматически", steps)


RECAPTCHA_V3_HINTS = ("grecaptcha.execute", "data-sitekey", "recaptcha/api.js",
                      "recaptcha__android", "g-recaptcha-response")


# ------------------------------------------------------------------ общий вход

def _setting(key: str, default: str) -> str:
    """Настройка из базы; при любой проблеме — значение по умолчанию."""
    try:
        from core import db
        return db.get_setting(key, default) or default
    except Exception:
        return default


def solve(page, state: dict | None = None, *, image_tries: int = 3,
          checkbox_wait: float = 6.0) -> Solved:
    """Попробовать решить капчу на текущей странице любым доступным способом.

    Возвращает Solved(ok, kind, detail, steps). Ничего не отправляет на сторонние сервисы.
    """
    state = state or {}
    steps: list[str] = []

    if is_cloudflare_wall(page.text()):
        res = solve_cloudflare(page, steps=steps)
        if res.ok:
            return res
        # steps is shared with the solver; do not append it to itself.

    # проверка «похожий цвет» (Антибот Клауд у SMMYT и родственных панелей)
    if color_gate_present(page.text()):
        res = solve_color_gate(page, steps=steps)
        if res.ok:
            return res
        # не выходим: дальше попробуем обычные капчи, вдруг это только обёртка

    # невидимые панельные капчи не кликабельны — раскрываем скрытые блоки заранее
    if force_show_hidden_captcha(page):
        steps.append("раскрыл скрытый контейнер капчи")

    # reCAPTCHA v3 — невидимая, нет виджета и чекбокса. Обнаруживаем по признакам
    # на странице и пробуем pypasser (аудио-челлендж).
    page_text = page.text() or ""
    if any(h in page_text for h in RECAPTCHA_V3_HINTS):
        res = solve_recaptcha_v3(page, steps=steps)
        if res.ok:
            return res
        # если не вышло — продолжаем, может быть и обычная картинка/честбокс

    # подводим экран к капче: невидимый Turnstile рисуется только когда его видно
    holder = _scroll_to_captcha(page)
    if holder:
        steps.append(f"капча на странице: контейнер {holder['w']}×{holder['h']}")

    # галочка «я не робот»
    widgets = visible_widgets(page)
    if widgets:
        res = solve_checkbox(page, auto_wait=checkbox_wait, steps=steps)
        if res.ok:
            return res
        # steps is shared with the solver; do not append it to itself.

    # текстовая капча (можно выключить в настройках — тогда сразу человек)
    if _setting("captcha_ocr", "1") != "1":
        return Solved(False, "image", "распознавание капч с картинки выключено в настройках", steps)
    res = solve_image(page, tries=image_tries, steps=steps)
    if res.ok:
        return res
    return Solved(False, "unknown", "капчу автоматически решить не удалось", steps)


def describe(res: Solved) -> str:
    """Короткая подпись для журнала и заметки к задаче."""
    if res.ok:
        return res.detail
    return res.detail or "капча не решена"
