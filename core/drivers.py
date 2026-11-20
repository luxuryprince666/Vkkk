# -*- coding: utf-8 -*-
"""Драйверы площадок: робот, который сам делает заход на панели.

Что делает драйвер:
    1. открывает страницу заказа нужной услуги;
    2. находит поле для ссылки (и поле количества, если есть);
    3. печатает ссылку «по-человечески» — с паузами, прокруткой и движениями мыши;
    4. если сайт показывает таймер/очередь — ждёт;
    5. нажимает кнопку заказа;
    6. читает ответ страницы и выносит вердикт.

Вердикты (`DriverResult.verdict`):
    ok          — заказ принят, заход засчитан;
    captcha     — нужна капча: отдаём человеку (или пропускаем в режиме «Без капчи»);
    quota       — площадка упёрлась в лимит: это не поломка, надо просто позже;
    bad_link    — ссылка не подходит (не тот объект, закрытый профиль, битая);
    need_human  — форма есть, но автоматически не разобрать (несколько шагов, SMS и т.п.);
    site_broken — страница не открылась или формы нет вовсе.

Найденные селекторы запоминаются в `site_profiles.profile_json['driver']`: со второго захода
робот не ищет форму заново, а идёт сразу к делу. Если селектор перестал работать — драйвер
ищет заново и обновляет память.
"""
from __future__ import annotations

import contextlib
import json
import random
import re
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser

from core import captcha, db, profiles, stealth

SHOTS = db.DATA / "logs" / "shots"

SUCCESS_HINTS = (
    "заказ принят", "заказ создан", "заказ успешно", "заявка принята", "заявка создана",
    "успешно отправлен", "успешно добавлен", "успешно создан", "заказ оформлен",
    "спасибо за заказ", "принят в работу", "order created", "order accepted", "order queued",
    "пробный заказ в работе", "мы запустили бесплатную услугу",
)
QUOTA_HINTS = (
    "лимит", "попробуйте позже", "попробуйте через", "исчерпан", "слишком часто",
    "не раньше", "подождите", "too many", "limit reached", "try again later", "rate limit",
    # «на сегодня всё» — тоже лимит, просто до завтра
    "использован", "использована", "доступен завтра", "доступна завтра", "возвращайтесь завтра",
    "завтра будет", "до завтра", "already used", "used today", "come back tomorrow",
    # семейство /testknop: «Вы уже воспользовались данной услугой сегодня»
    "уже воспользовались",
)
BAD_LINK_HINTS = (
    "неверная ссылка", "неверная сс", "некорректная ссылка", "проверьте ссылку",
    "ссылка не найдена", "не найдена", "не удалось найти", "invalid link", "invalid url",
    "закрытый профиль", "ссылка некорректна", "введите корректн",
)
CAPTCHA_HINTS = (
    "капч", "captcha", "я не робот", "подтвердите, что вы не робот", "verify you are human",
    "проверка браузера", "введите код с картинки", "не робот",
)
NEED_HUMAN_HINTS = (
    "войдите", "авторизуйтесь", "регистрация", "sms", "код из смс", "подтвердите номер",
    "введите телефон", "лимит на ip", "только для зарегистрированных",
)

TIMER_RE = re.compile(r"(?:через|осталось|подождите|таймер)[^\d]{0,12}(\d{1,3})"
                      r"(?:\s*(сек|секунд|мин|минут|м))", re.I)
CLOCK_RE = re.compile(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b")


@dataclass
class DriverResult:
    verdict: str
    detail: str
    units: int = 0
    waited: int = 0
    shot: str | None = None
    backend: str = ""
    learned: dict = field(default_factory=dict)
    steps: list = field(default_factory=list)
    submitted: bool = False  # была ли попытка отправки; не означает приёма или доставки

    def as_dict(self) -> dict:
        return {"verdict": self.verdict, "detail": self.detail, "units": self.units,
                "waited": self.waited, "shot": self.shot, "backend": self.backend,
                "learned": self.learned, "steps": self.steps, "submitted": self.submitted}

    @property
    def ok(self) -> bool:
        return self.verdict == "ok"


# ------------------------------------------------------------------ поиск формы в DOM

DISCOVER_JS = r"""
(() => {
  const out = {ok: true, fields: [], buttons: [], captcha: [], texts: [], timer: null, forms: 0};
  const visible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 4 && r.height > 4 && st.visibility !== 'hidden' && st.display !== 'none';
  };
  const css = (el) => {
    if (el.id && document.querySelectorAll('#' + CSS.escape(el.id)).length === 1)
      return '#' + CSS.escape(el.id);
    const name = el.getAttribute('name');
    if (name) {
      const sel = el.tagName.toLowerCase() + '[name="' + name.replace(/"/g, '\\"') + '"]';
      if (document.querySelectorAll(sel).length === 1) return sel;
    }
    const ph = el.getAttribute('placeholder');
    if (ph) {
      const sel = el.tagName.toLowerCase() + '[placeholder="' + ph.replace(/"/g, '\\"') + '"]';
      if (document.querySelectorAll(sel).length === 1) return sel;
    }
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && parts.length < 6) {
      let part = node.tagName.toLowerCase();
      const parent = node.parentNode;
      if (parent) {
        const same = [...parent.children].filter(c => c.tagName === node.tagName);
        if (same.length > 1) part += ':nth-of-type(' + (same.indexOf(node) + 1) + ')';
      }
      parts.unshift(part);
      node = node.parentNode;
    }
    return parts.join(' > ');
  };
  const attrs = (el) => [el.type, el.name, el.id, el.placeholder, el.getAttribute('aria-label'),
                         el.className, el.autocomplete].join(' ').toLowerCase();
  const labelOf = (el) => {
    let txt = '';
    if (el.id) {
      const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (l) txt += ' ' + l.innerText;
    }
    const wrap = el.closest('label');
    if (wrap) txt += ' ' + wrap.innerText;
    const box = el.closest('div, td, p, li');
    if (box) txt += ' ' + (box.innerText || '').slice(0, 160);
    return txt.toLowerCase();
  };

  // если открыто модальное окно — работаем внутри него, а не с формой «под» ним
  const dlgBoxes = [...document.querySelectorAll(
    "[role='dialog'], [aria-modal='true'], dialog[open], [class*='modal'], [class*='Modal']," +
    " [class*='popup'], [class*='Popup'], [class*='dialog'], [class*='Dialog']")].filter(visible);
  const dlg = dlgBoxes.length ? dlgBoxes[dlgBoxes.length - 1] : null;
  out.dialog = !!dlg;
  out.forms = document.querySelectorAll('form').length;
  const inputs = [...document.querySelectorAll('input, textarea')].filter(visible);
  for (const el of inputs) {
    const a = attrs(el), lab = attrs(el) + ' ' + labelOf(el);
    if (el.type === 'hidden') continue;
    out.fields.push({
      sel: css(el), type: (el.type || el.tagName).toLowerCase(),
      name: el.name || '', ph: el.getAttribute('placeholder') || '',
      label: labelOf(el).trim().slice(0, 90), attrs: a.slice(0, 160),
      value: el.value || '', readonly: !!el.readOnly, required: !!el.required,
      w: Math.round(el.getBoundingClientRect().width),
      dlg: dlg ? dlg.contains(el) : false
    });
  }
  const clickable = [...document.querySelectorAll('button, input[type=submit], input[type=button], a.btn, a.button, [role=button]')]
    .filter(visible);
  for (const el of clickable) {
    const t = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ');
    out.buttons.push({
      sel: css(el), text: t.slice(0, 60), disabled: !!el.disabled,
      attrs: ((el.id || '') + ' ' + (el.className || '') + ' ' + (el.getAttribute('name') || ''))
               .toLowerCase().slice(0, 120),
      nearForm: !!el.closest('form'),
      dlg: dlg ? dlg.contains(el) : false
    });
  }
  const capSelectors = ["iframe[src*='recaptcha']", "iframe[src*='hcaptcha']",
    "iframe[src*='turnstile']", "iframe[src*='cloudflare']", "iframe[src*='challenge']",
    "div.g-recaptcha", "div.h-captcha", "div.cf-turnstile", ".cf-turnstile",
    "[class*='captcha']", "[id*='captcha']", "[id*='turnstile']", "[class*='turnstile']",
    "[id*='challenge']", "[data-sitekey]", "input[name='cf-turnstile-response']",
    "input[name='g-recaptcha-response']", "input[name='h-captcha-response']"];
  for (const s of capSelectors) {
    document.querySelectorAll(s).forEach((el) => {
      if (!visible(el)) return;
      // пустой контейнер-заглушка (Turnstile рисуется позже, в том числе после клика)
      // — это ещё не капча: иначе робот уходит «в капчу» на ровном месте
      const looks = el.matches('iframe, img, canvas')
        || el.querySelector("iframe, img, canvas, [role='checkbox']")
        || el.querySelector("input:not([type=hidden])[name*='captcha']," +
                            " input:not([type=hidden])[name*='code']")
        || ((el.innerText || '').trim().length > 3);
      if (looks) out.captcha.push(s);
    });
  }
  const body = (document.body ? document.body.innerText : '').toLowerCase();
  const hints = ['заказ принят', 'заказ создан', 'успешно', 'ожидайте', 'в очереди', 'лимит',
    'попробуйте позже', 'неверная ссылка', 'капч', 'я не робот', 'войдите', 'регистрация',
    'осталось', 'через', 'подождите'];
  hints.forEach(h => { if (body.includes(h)) out.texts.push(h); });
  out.bodySample = body.slice(0, 1200);
  const tm = body.match(/(?:через|осталось|подождите|таймер)[^\d]{0,12}(\d{1,3})\s*(сек|секунд|мин|минут|м)/);
  if (tm) {
    let sec = parseInt(tm[1], 10);
    if (tm[2].startsWith('мин') || tm[2] === 'м') sec *= 60;
    out.timer = sec;
  }
  const clock = body.match(/\b(\d{1,2}):(\d{2})\b/);
  if (!out.timer && clock) out.timerClock = clock[0];
  return JSON.stringify(out);
})()
"""


TIMER_START_JS = r"""
(() => {
  const vis = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 20 && r.height > 14 && st.visibility !== 'hidden' && st.display !== 'none';
  };
  const css = (el) => {
    if (el.id && document.querySelectorAll('#' + CSS.escape(el.id)).length === 1)
      return '#' + CSS.escape(el.id);
    const cls = (el.className || '').toString().split(/\s+/).filter(Boolean)[0];
    if (cls) {
      const sel = '.' + CSS.escape(cls);
      if (document.querySelectorAll(sel).length === 1) return sel;
    }
    return null;
  };
  const WORDS = ['запустить таймер', 'нажмите на таймер', 'таймер', 'начать отсчёт', 'старт',
                 'start timer', 'start', 'получить бесплатно', 'get free', 'получить'];
  const BAD = ['купить', 'войти', 'регистрац', 'тариф', 'оплат', 'меню', 'кабинет', 'войти'];
  const cand = [...document.querySelectorAll("button, a, [role='button'], [class*='timer']," +
                " [class*='Timer'], div[onclick], span[onclick]")].filter(vis);
  const found = [];
  for (const el of cand) {
    const t = ((el.innerText || el.getAttribute('aria-label') || '') + ' ' + (el.className || ''))
                .toLowerCase().replace(/\s+/g, ' ').trim();
    if (!t || t.length > 90) continue;
    if (BAD.some((w) => t.includes(w))) continue;
    const prio = WORDS.findIndex((w) => t.includes(w));
    if (prio < 0) continue;
    const sel = css(el);
    if (!sel) continue;
    found.push({sel: sel, text: t.slice(0, 50), prio: prio});
  }
  found.sort((a, b) => a.prio - b.prio);
  return JSON.stringify(found.slice(0, 3));
})()
"""


TIMER_CLOCK_JS = r"""
(() => {
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 8 && r.height > 8 && st.visibility !== 'hidden' && st.display !== 'none';
  };
  const CTX = ['таймер', 'до бесплатного', 'осталось', 'до заказа', 'до конца', 'timer',
               'wait', 'минут', 'бесплатн'];
  const nodes = [...document.querySelectorAll('span, div, b, strong, time')].filter(vis);
  for (const el of nodes) {
    const t = (el.textContent || '').trim();
    if (!/^\d{1,2}:\d{2}(:\d{2})?$/.test(t)) continue;
    let ctx = '';
    let box = el;
    for (let i = 0; i < 3 && box; i++) { ctx += ' ' + (box.innerText || ''); box = box.parentElement; }
    ctx = ctx.toLowerCase().slice(0, 300);
    if (CTX.some((w) => ctx.includes(w))) return t;
  }
  return '';
})()
"""


def start_timer(page, state: dict, steps: list[str], max_wait: int = 150) -> tuple[dict, bool]:
    """Запустить таймер площадки и дождаться формы.

    У LikeBoom и подобных сначала надо нажать «таймер» вверху, и только через ~2 минуты
    появляется кнопка «Получить бесплатно» и поле для ссылки. Без этого робот видел
    «формы нет» и уходил ни с чем.
    """
    try:
        raw = page.js(TIMER_START_JS)
        cands = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except Exception:
        cands = []
    if not cands:
        # кнопки нет, но на странице тикает счётчик («01:56 До бесплатного заказа») —
        # значит отсчёт уже идёт и надо просто дождаться нуля
        try:
            clock = (page.js(TIMER_CLOCK_JS) or "").strip()
        except Exception:
            clock = ""
        if not clock:
            return state, False
        steps.append(f"на странице идёт отсчёт {clock} — жду")
        fin = time.time() + max(20, min(int(max_wait), 300))
        while time.time() < fin:
            page.wait(5000)
            new_state = discover(page)
            if new_state.get("fields"):
                steps.append("отсчёт закончился, форма появилась")
                return new_state, True
            btns = [b for b in (new_state.get("buttons") or [])
                    if not b.get("disabled") and any(
                        w in (b.get("text") or "").lower()
                        for w in ("получить бесплатно", "get free", "получить за", "забрать"))]
            if btns:
                page.click_human(btns[0]["sel"])
                steps.append(f"нажал «{btns[0]['text'][:40]}» после отсчёта")
                page.wait(random.randint(2500, 4000))
                new_state = discover(page)
                if new_state.get("fields"):
                    return new_state, True
        steps.append("отсчёт шёл, но форма так и не появилась")
        return state, True
    clicked = None
    for c in cands:
        if page.click_human(c["sel"]):
            clicked = c
            break
    if not clicked:
        return state, False
    steps.append(f"запустил таймер: «{clicked['text'][:40]}»")

    fin = time.time() + max(20, min(int(max_wait), 300))
    while time.time() < fin:
        page.wait(3000)
        new_state = discover(page)
        if new_state.get("fields"):
            steps.append(f"форма появилась через {int(max_wait - (fin - time.time()))} с после таймера")
            return new_state, True
        # таймер мог отрисовать кнопку, которую надо нажать ещё раз
        try:
            more = json.loads(page.js(TIMER_START_JS) or "[]")
        except Exception:
            more = []
        if more and more[0].get("text", "").startswith(("получить", "get free")):
            page.click_human(more[0]["sel"])
            steps.append("нажал «Получить бесплатно» после таймера")
    steps.append("ждал таймер, но форма так и не появилась")
    return state, True



PLATFORM_WORDS = {
    "vk": ("vk", "вконтакте", "вк"),
    "telegram": ("telegram", "телеграм", "тг", "tg"),
    "instagram": ("instagram", "инстаграм"),
    "tiktok": ("tiktok", "тикток"),
    "youtube": ("youtube", "ютуб"),
    "twitter": ("twitter", "x.com"),
    "twitch": ("twitch",),
}

PLATFORM_PICK_JS = r"""
(() => {
  const words = %(words)s;
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 12 && r.height > 10 && st.visibility !== 'hidden' && st.display !== 'none';
  };
  const txt = (el) => (el.innerText || el.textContent || '').trim().toLowerCase();
  const short = (el) => { const t = txt(el); return t.length > 0 && t.length <= 24; };
  const nodes = [...document.querySelectorAll(
    "button, li, a, [role='option'], [class*='option'], [class*='item'], [class*='tab']," +
    " [class*='dropdown'], [class*='select'], div, span")].filter(vis);
  // 1) опция уже видна в открытом списке
  const opt = nodes.find((el) => short(el) && words.includes(txt(el)) && el.children.length <= 2);
  if (opt) { opt.click(); return 'option'; }
  // 2) список закрыт — открываем по элементу с названием другой площадки
  const OTHERS = ['telegram', 'телеграм', 'max', 'tiktok', 'instagram', 'youtube', 'twitter',
                  'twitch', 'вконтакте'];
  const ctrl = nodes.find((el) => short(el) && OTHERS.includes(txt(el)) && el.children.length <= 2);
  if (ctrl) { ctrl.click(); return 'open'; }
  return '';
})()
"""


def choose_platform(page, platform: str, steps: list[str]) -> bool:
    """Выбрать нужную площадку в форме заказа.

    У LikeBoom в «Оформлении заказа» первой стоит Telegram: если её не переключить на VK,
    робот закажет подписчиков не туда. Работает только когда такая форма реально есть.
    """
    words = PLATFORM_WORDS.get((platform or "").lower())
    if not words:
        return False
    js = PLATFORM_PICK_JS % {"words": json.dumps(list(words))}
    try:
        res = page.js(js)
    except Exception:
        return False
    if res == "open":
        page.wait(random.randint(700, 1200))
        try:
            res = page.js(js)
        except Exception:
            return False
    if res == "option":
        steps.append(f"выбрал площадку «{platform.upper()}»")
        page.wait(random.randint(600, 1100))
        return True
    return False



TICK_JS = r"""
(() => {
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 6 && r.height > 6 && st.visibility !== 'hidden' && st.display !== 'none';
  };
  const OK = ['соглас', 'политик', 'персональн', 'условия', 'оферт', 'правил', 'принимаю',
              'i agree', 'terms', 'privacy', 'consent'];
  const NO = ['рассылк', 'newsletter', 'marketing', 'рекламн', 'sms', 'уведомл'];
  let n = 0;
  for (const box of [...document.querySelectorAll("input[type='checkbox']")].filter(vis)) {
    if (box.checked) continue;
    let ctx = ((box.closest('label') || {}).innerText || '') + ' ' +
              ((box.parentElement || {}).innerText || '');
    const box2 = box.closest('div, label, p, span');
    if (box2) ctx += ' ' + (box2.innerText || '');
    ctx = ctx.toLowerCase();
    if (NO.some((w) => ctx.includes(w))) continue;
    if (box.required || OK.some((w) => ctx.includes(w))) {
      box.click();
      n += 1;
    }
  }
  return n;
})()
"""


def tick_consents(page, steps: list[str]) -> int:
    """Отметить обязательные чекбоксы согласий (политика, персональные данные).

    Без них финальная кнопка на многих панелях не срабатывает. Рассылки и рекламу
    робот не отмечает — только то, что нужно для заказа.
    """
    try:
        n = int(page.js(TICK_JS) or 0)
    except Exception:
        return 0
    if n:
        steps.append(f"отметил обязательные согласия: {n}")
        page.wait(random.randint(300, 700))
    return n



def field_value(page, sel: str) -> str:
    """Что реально лежит в поле (по DOM, а не по нашим надеждам)."""
    try:
        return str(page.js("(() => {const e = document.querySelector(%r);"
                           " return e ? (e.value || '') : '';})()" % sel) or "")
    except Exception:
        return ""


def set_field(page, sel: str, value: str) -> bool:
    """Вписать значение и убедиться, что оно осталось в поле.

    React/Vue-формы иногда «съедают» обычный ввод: печатаем по-человечески, проверяем,
    и только если поле пустое — ставим значение через нативный сеттер с событиями.
    """
    try:
        page.type_human(sel, value)
    except Exception:
        pass
    if field_value(page, sel).strip() == value.strip():
        return True
    try:
        page.js("""(() => {
          const el = document.querySelector(%r);
          if (!el) return false;
          const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype
                                                  : HTMLInputElement.prototype;
          const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
          setter.call(el, %s);
          el.dispatchEvent(new Event('input', {bubbles: true}));
          el.dispatchEvent(new Event('change', {bubbles: true}));
          return true;
        })()""" % (sel, json.dumps(value)))
    except Exception:
        return False
    page.wait(200)
    return field_value(page, sel).strip() == value.strip()



def scope_state(state: dict) -> dict:
    """Оставить поля и кнопки открытого модального окна.

    Бесплатные панели часто показывают форму заказа «поверх» страницы, где под ней
    лежит ещё одна такая же форма. Без этого робот печатал e-mail в поле за модалкой
    и не находил финальную кнопку.
    """
    if not state:
        return state
    fields = state.get("fields") or []
    buttons = state.get("buttons") or []
    if not state.get("dialog"):
        return state
    in_dlg_fields = [f for f in fields if f.get("dlg")]
    in_dlg_buttons = [b for b in buttons if b.get("dlg")]
    out = dict(state)
    if in_dlg_fields:
        out["fields"] = in_dlg_fields
    if in_dlg_buttons:
        out["buttons"] = in_dlg_buttons
    return out



def _shot(page, path, focus: str | None = None) -> str | None:
    """Скриншот с прокруткой к нужному месту (движки без поддержки focus — просто снимок)."""
    try:
        return page.shot(path, focus=focus)
    except TypeError:
        return page.shot(path)


def discover(page: stealth.Page) -> dict:
    """Снимок формы: поля, кнопки, капча, подсказки, таймер."""
    raw = page.js(DISCOVER_JS)
    if not raw:
        return {"ok": False, "fields": [], "buttons": [], "captcha": [], "texts": []}
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return {"ok": False, "fields": [], "buttons": [], "captcha": [], "texts": []}


# На живых страницах рядом с бесплатной формой встречаются платная форма и
# форма отзывов. Отбор только по тексту кнопки может отправить не тот заказ.
SAME_FORM_JS = r"""((fieldSel, candidates) => {
  const one = sel => { try { const v = document.querySelectorAll(sel); return v.length === 1 ? v[0] : null; }
                       catch (_) { return null; } };
  const field = one(fieldSel), form = field && (field.form || field.closest('form'));
  if (!form) return JSON.stringify({ok:false});
  const owned = sel => {
    const el = one(sel);
    return !!el && (el.form || el.closest('form')) === form && !el.closest('a[href]');
  };
  return JSON.stringify({ok:true, fields:candidates.fields.filter(owned),
                         buttons:candidates.buttons.filter(owned)});
})"""

FREE_FORM_GUARD_JS = r"""((fieldSel, buttonSel, expectedValue, expectedUrl) => {
  const one = sel => { try { const v = document.querySelectorAll(sel); return v.length === 1 ? v[0] : null; }
                       catch (_) { return null; } };
  const field = one(fieldSel), button = one(buttonSel);
  const form = field && (field.form || field.closest('form'));
  if (!field || !button || !form || (button.form || button.closest('form')) !== form ||
      button.closest('a[href]') || !['BUTTON','INPUT'].includes(button.tagName)) return 'different-form';
  if (field.disabled || field.readOnly || (field.value || '').trim() !== expectedValue.trim()) return 'link-changed';
  if (button.disabled || button.getAttribute('aria-disabled') === 'true') return 'button-disabled';
  try {
    const wanted = new URL(expectedUrl), current = new URL(location.href);
    if (wanted.origin !== current.origin || wanted.pathname.replace(/\/$/,'') !== current.pathname.replace(/\/$/,''))
      return 'redirect';
    const action = new URL(button.getAttribute('formaction') || form.getAttribute('action') || location.href,
                           location.href);
    if (action.origin !== current.origin || !['http:', 'https:'].includes(action.protocol)) return 'external-action';
  } catch (_) { return 'redirect'; }
  const buttonText = (button.innerText || button.value || '').toLowerCase();
  const text = (buttonText + ' ' + (form.innerText || '')).toLowerCase();
  const knownFreeTest = ['avi1.ru', 'prtut.ru', 'pricesmm.com', 'doctorsmm.com']
    .includes(location.hostname.toLowerCase()) && form.id === 'get_free_form' && button.id === 'get_free_start';
  if (!knownFreeTest && !/(бесплатн|\bfree\b|без оплаты)/i.test(buttonText)) return 'free-not-proven';
  const paidText = text.match(/(?:к оплате|стоимость заказа|итого к оплате|total price)\s*[:\-]?\s*([0-9]+(?:[.,][0-9]+)?)/i);
  if (paidText && parseFloat(paidText[1].replace(',', '.')) > 0) return 'paid';
  for (const el of form.querySelectorAll('input, select, textarea')) {
    const key = ((el.name || '') + ' ' + (el.id || '')).toLowerCase();
    if (!/(payment|paymethod|price|cost|summa|оплат|стоимост)/i.test(key) || el.disabled ||
        (['radio','checkbox'].includes((el.type || '').toLowerCase()) && !el.checked)) continue;
    if (!/^(?:0(?:[.,]0+)?|free|бесплатно)$/i.test((el.value || '').trim())) return 'payment-field';
  }
  return 'ok';
})"""


def same_form_state(page, state: dict, field_sel: str) -> dict | None:
    """Ограничить эвристику элементами одной формы; неоднозначность означает пропуск."""
    candidates = {key: [x.get('sel') for x in (state.get(key) or []) if x.get('sel')]
                  for key in ('fields', 'buttons')}
    try:
        raw = page.js('(' + SAME_FORM_JS + ')(' + json.dumps(field_sel) + ',' +
                      json.dumps(candidates, ensure_ascii=False) + ')')
        scoped = json.loads(raw) if isinstance(raw, str) else raw
        if not scoped or not scoped.get('ok') or field_sel not in scoped.get('fields', []):
            return None
        result = dict(state)
        for key in ('fields', 'buttons'):
            allowed = set(scoped.get(key) or [])
            result[key] = [x for x in (state.get(key) or []) if x.get('sel') in allowed]
        return result
    except OperationCancelled:
        raise
    except Exception:
        return None


def free_form_guard(page, field_sel: str, button_sel: str, target_url: str, service_url: str) -> bool:
    """До и после измерения baseline подтвердить, что отправляется та же бесплатная форма."""
    args = ','.join(json.dumps(v, ensure_ascii=False) for v in
                    (field_sel, button_sel, target_url, service_url))
    try:
        return page.js('(' + FREE_FORM_GUARD_JS + ')(' + args + ')') == 'ok'
    except OperationCancelled:
        raise
    except Exception:
        return False


LINK_WORDS = ("link", "url", "ссылка", "ссылк", "href", "target", "profile", "vk.com", "адрес")
COUNT_WORDS = ("count", "amount", "quantity", "кол-во", "количество", "число")
EMAIL_WORDS = ("email", "mail", "почта", "e-mail")
SUBMIT_WORDS = ("отправить", "заказать", "заказ", "получить", "накрутить", "оформить",
                "начать", "продолжить", "бесплатно", "пуск",
                "submit", "send", "order", "start", "get ", "receive", "continue", "claim", "free")
# Нельзя случайно нажимать платные действия, авторизацию или выход из формы.
NON_SUBMIT_WORDS = ("отмена", "отменить", "закрыть", "назад", "войти", "регистрац", "купить",
                    "оплат", "пополн", "тариф", "корзин", "отзыв", "ещё больше", "еще больше",
                    "cancel", "close", "back", "sign in", "log in", "register", "payment",
                    "checkout", "purchase", "buy", "review")


def pick_link_field(fields: list[dict]) -> dict | None:
    """Ищем поле для ссылки: сначала по имени/подсказке, потом по «самому широкому текстовому»."""
    def score(f: dict) -> int:
        if f.get("readonly") or f.get("type") in ("checkbox", "radio", "submit", "button", "file"):
            return -99
        blob = f"{f.get('name','')} {f.get('ph','')} {f.get('label','')} {f.get('attrs','')}".lower()
        s = 0
        if any(w in blob for w in LINK_WORDS):
            s += 10
        if f.get("type") == "url":
            s += 6
        if "vk.com" in blob:
            s += 4
        if any(w in blob for w in EMAIL_WORDS):
            s -= 6
        if any(w in blob for w in COUNT_WORDS):
            s -= 4
        if f.get("type") in ("text", "url", "search", ""):
            s += 2
        if f.get("w", 0) > 200:
            s += 2
        return s

    best = None
    for f in fields:
        sc = score(f)
        if best is None or sc > best[0]:
            best = (sc, f)
    if best and best[0] >= 6:
        return best[1]
    # запасной путь: единственное широкое текстовое поле без подсказок
    def plain(f: dict) -> bool:
        blob = f"{f.get('name','')} {f.get('ph','')} {f.get('label','')} {f.get('attrs','')}".lower()
        return (f.get("type") in ("text", "url", "search", "")
                and not f.get("readonly") and f.get("w", 0) >= 200
                and not any(w in blob for w in EMAIL_WORDS + COUNT_WORDS))

    wide = [f for f in fields if plain(f)]
    return wide[0] if len(wide) == 1 else None


def pick_count_field(fields: list[dict], link_sel: str | None) -> dict | None:
    """Поле «сколько» — если площадка разрешает выбрать объём."""
    for f in fields:
        if f.get("sel") == link_sel:
            continue
        blob = f"{f.get('name','')} {f.get('ph','')} {f.get('label','')}".lower()
        if any(w in blob for w in COUNT_WORDS) and f.get("type") in ("text", "number", "tel", ""):
            return f
    return None


def pick_submit_button(buttons: list[dict]) -> dict | None:
    """Кнопка заказа. Выключенную тоже возвращаем — драйвер дождётся, когда её включат."""
    def score(b: dict) -> int:
        t = (b.get("text") or "").lower()
        attrs = (b.get("attrs") or "").lower()
        if any(w in t for w in NON_SUBMIT_WORDS):
            return -99
        sc = 0
        matched = False
        for i, w in enumerate(SUBMIT_WORDS):
            if w in t:
                sc += max(4, 12 - i)
                matched = True
                break
        if any(w in attrs for w in ("submit", "send", "order", "claim", "optin", "btn-get",
                                    "do-order", "get_free_start")):
            sc += 6
            matched = True
        if any(w in attrs for w in ("theme", "nav", "header", "footer", "cookie", "legal",
                                    "menu", "tab", "modal-close")):
            return -99
        if not matched:
            return -99
        if b.get("nearForm"):
            sc += 2
        return sc

    cands = [b for b in buttons if score(b) > 0]
    if not cands:
        return None
    # Предпочитаем активную подходящую кнопку, затем максимальную оценку.
    return max(cands, key=lambda b: (not b.get('disabled'), score(b)))


def has_email_field(fields: list[dict]) -> dict | None:
    """Поле e-mail: часть панелей просит его для отчёта о заказе."""
    for f in fields:
        blob = f"{f.get('name','')} {f.get('ph','')} {f.get('label','')} {f.get('attrs','')}".lower()
        if any(w in blob for w in EMAIL_WORDS) and f.get("type") in ("email", "text", ""):
            return f
    return None


READY_JS = r"""
(sel) => {
  const el = document.querySelector(sel);
  if (!el) return JSON.stringify({missing: true});
  const st = getComputedStyle(el);
  return JSON.stringify({
    disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true'
               || st.pointerEvents === 'none' || parseFloat(st.opacity) < 0.5
  });
}
"""


def _button_countdown(page: stealth.Page, sel: str) -> int:
    """Сколько секунд ещё сидит таймер НА КНОПКЕ заказа («Доступно через 6:47»).

    Движки /testknop (DoctorSMM, Avi1, Prtut, PriceSMM) показывают обратный отсчёт прямо
    в кнопке: нажимать раньше конца бесполезно — сервер молча игнорирует заявку.
    Возвращает 0, если таймера на кнопке нет.
    """
    try:
        raw = page.js(r"""((sel) => {
          const el = document.querySelector(sel);
          if (!el) return '';
          let t = (el.innerText || el.value || '') + ' ' + ((el.parentElement || {}).innerText || '');
          const m = t.match(/(?:ЧЕРЕЗ|через)\s*(\d{1,2}):(\d{2})/);
          if (!m) return '';
          return String(parseInt(m[1], 10) * 60 + parseInt(m[2], 10));
        })""" + "(" + repr(sel) + ")")
        return int(raw) if raw and str(raw).isdigit() else 0
    except Exception:
        return 0


def _wait_countdown(page: stealth.Page, sel: str, budget: int) -> int:
    """Дождаться окончания таймера на кнопке (не дольше budget). Возвращает число секунд ожидания."""
    spent = 0
    while spent < budget:
        left = _button_countdown(page, sel)
        if left <= 0:
            return spent
        chunk = min(20, left + 5, budget - spent)
        page.wait(chunk * 1000)
        spent += chunk
    return spent


def _wait_for_ready(page: stealth.Page, sel: str, max_sec: int = 45) -> tuple[int, bool]:
    """Ждёт, пока кнопка заказа станет живой. Возвращает (сколько ждали, дождались ли).

    Ждём именно кнопку, а не «любой текст про время»: на панелях висит подпись
    «следующий заказ через 5 минут» — это не повод стоять пять минут перед первым заходом.
    Если кнопка так и осталась выключенной, значит площадка действительно держит лимит —
    это вердикт «quota», а не повод ждать.
    """
    spent = 0
    while spent < max_sec:
        raw = page.js("(" + READY_JS.strip() + ")(" + repr(sel) + ")")
        try:
            st = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception:
            return spent, True                     # не смогли прочитать — пробуем нажать
        if st.get("missing"):
            return spent, True
        if not st.get("disabled"):
            return spent, True
        page.wait(1000)
        spent += 1
    return spent, False


# ------------------------------------------------------------------ чтение ответа страницы

def read_verdict(text: str, has_captcha: bool, captcha_known: bool) -> tuple[str, str]:
    """Ответ об отправке, не доказательство доставки. Ошибки имеют приоритет."""
    low = (text or "").lower()
    for h in BAD_LINK_HINTS:
        if h in low:
            return "bad_link", f"страница ругается на ссылку: «{h}»"
    for h in QUOTA_HINTS:
        if h in low:
            return "quota", f"лимит площадки: «{h}»"
    for h in ("заказ не принят", "заявка не принята", "не удалось создать", "заказ отклонен",
              "заказ отклонён", "order rejected", "order failed"):
        if h in low:
            return "need_human", f"площадка отклонила заказ: «{h}»"
    success = next((h for h in SUCCESS_HINTS if h in low), None)
    if has_captcha and not success:
        return "captcha", "на странице висит капча"
    if success:
        return "ok", f"страница подтвердила заказ: «{success}»"
    for h in CAPTCHA_HINTS:
        if h in low:
            return "captcha", f"похоже на капчу: «{h}»"
    for h in NEED_HUMAN_HINTS:
        if h in low:
            return "need_human", f"нужен человек: «{h}»"
    if captcha_known:
        return "captcha", "площадка известна капчей, а подтверждения заказа не видно"
    return "need_human", "ответ страницы непонятен — проверьте скриншот"


def retry_delay_sec(text: str) -> int:
    """Когда возвращаться после «лимита»: через час или завтра.

    «Бесплатная накрутка на сегодня использована — возвращайтесь завтра» — это не час,
    и гонять робота каждые 60 минут бессмысленно: площадка его просто отфутболит.
    """
    low = (text or "").lower()
    if any(w in low for w in ("завтра", "tomorrow", "come back", "used today")):
        now = time.localtime()
        left = 24 * 3600 - (now.tm_hour * 3600 + now.tm_min * 60 + now.tm_sec)
        return max(3600, left + 600)          # до полуночи плюс 10 минут на «раскачку»
    sec = parse_timer(text)
    if sec and sec > 0:
        return max(300, sec + 30)
    return 3600


def _text_diff(before: str, after: str) -> str:
    """Что появилось на странице после нажатия.

    Площадки держат на странице рекламный текст («накрутка идёт в общей очереди»),
    и он легко выдаёт себя за подтверждение заказа. Поэтому сравниваем текст до и после
    клика: сначала читаем вердикт по приросту, и только потом по всей странице.
    """
    before_low = (before or "").lower()
    out = []
    for line in (after or "").splitlines():
        t = line.strip()
        if t and t.lower() not in before_low:
            out.append(t)
    return "\n".join(out)[:2000]


def read_verdict_delta(before: str, after: str, has_captcha: bool,
                       captcha_known: bool) -> tuple[str, str]:
    """Учитывать только новый ответ, а не рекламные подсказки до клика.

    В частности, старая надпись «неверная ссылка» не доказывает отказ нового
    заказа: повтор с другим адресом мог бы создать дубликат.
    """
    delta = _text_diff(before, after)
    if delta:
        verdict, why = read_verdict(delta, has_captcha, False)
        if verdict in ("ok", "bad_link", "quota", "captcha") or why.startswith(
                ("нужен человек:", "площадка отклонила заказ:")):
            return verdict, why
    if has_captcha or captcha_known:
        return "captcha", "капча или подтверждение заказа не распознаны"
    return "need_human", "не видно нового подтверждения или отказа — проверьте скриншот"



def parse_timer(text: str) -> int:
    """Сколько секунд ждать по тексту страницы (таймер/очередь). 0 — ждать нечего."""
    low = (text or "").lower()
    m = TIMER_RE.search(low)
    if m:
        sec = int(m.group(1))
        unit = m.group(2)
        if unit.startswith("мин") or unit == "м":
            sec *= 60
        return max(0, min(900, sec))
    m = CLOCK_RE.search(low)
    if m and ("осталось" in low or "таймер" in low):
        mins, secs = int(m.group(1)), int(m.group(2))
        return max(0, min(900, mins * 60 + secs))
    return 0


# ------------------------------------------------------------------ память драйвера

# Сайты на движке /testknop (DoctorSMM, Avi1, Nakruti-клоны) принимают только ссылки
# вида vk.com/…: адрес vk.ru/… упирается в «Проверьте правильность ссылки».
VKCOM_SITES = {"prskill", "doctorsmm", "avi1", "nakruti", "lowcostsmm", "prtut", "pricesmm"}


def normalize_link(site_id: str, url: str) -> str:
    """Безопасно меняет только домен VK, не строку запроса и не посторонние адреса."""
    from urllib.parse import urlsplit, urlunsplit
    if site_id in VKCOM_SITES and isinstance(url, str):
        parts = urlsplit(url)
        if parts.hostname in ('vk.ru', 'm.vk.ru') and not parts.username and not parts.port:
            return urlunsplit((parts.scheme, parts.hostname.replace('vk.ru', 'vk.com'),
                               parts.path, parts.query, parts.fragment))
    return url


def _contact_email() -> str:
    """E-mail из настроек приложения (нужен площадкам, которые требуют контакты)."""
    try:
        return db.get_setting("order_email", "") or db.get_setting("contact_email", "") or ""
    except Exception:
        return ""


def _memory(site_id: str) -> dict:
    try:
        prof = profiles.get_profile(site_id) or {}
    except Exception:
        return {}
    op = prof.get("order_page") or {}
    return op.get("driver") or {}


def remember(site_id: str, data: dict) -> None:
    """Записать/обновить выученные селекторы площадки.

    Тихо пропускаем площадки, которых нет в каталоге (например, тестовый стенд): память драйвера —
    приятный бонус, а не повод падать.
    """
    with db.rd() as conn:
        if not conn.execute("SELECT 1 FROM sites WHERE id=?", (site_id,)).fetchone():
            return
    with db.tx() as conn:
        row = conn.execute("SELECT profile_json FROM site_profiles WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO site_profiles(site_id) VALUES(?)", (site_id,))
            base = {}
        else:
            try:
                base = json.loads(row["profile_json"] or "{}")
            except Exception:
                base = {}
        mem = base.get("driver") or {}
        mem.update(data)
        base["driver"] = mem
        conn.execute("UPDATE site_profiles SET profile_json=? WHERE site_id=?",
                     (json.dumps(base, ensure_ascii=False), site_id))
    profiles.reset_cache()


def forget(site_id: str) -> None:
    with db.rd() as conn:
        if not conn.execute("SELECT 1 FROM sites WHERE id=?", (site_id,)).fetchone():
            return
    with db.tx() as conn:
        row = conn.execute("SELECT profile_json FROM site_profiles WHERE site_id=?", (site_id,)).fetchone()
        if not row:
            return
        try:
            base = json.loads(row["profile_json"] or "{}")
        except Exception:
            base = {}
        base.pop("driver", None)
        conn.execute("UPDATE site_profiles SET profile_json=? WHERE site_id=?",
                     (json.dumps(base, ensure_ascii=False), site_id))
    profiles.reset_cache()


# ------------------------------------------------------------------ сам заход

@contextlib.contextmanager
def _open_smart(headless: bool, prefer: str | None, steps: list[str], proxy: str | None = None):
    """Открыть браузер, а если видимое окно не поднимается — молча уйти в фон.

    Бывает на сервере/в контейнере без экрана (нет DISPLAY): headed-запуск падает
    с «Target page, context or browser has been closed». Раньше из-за этого заход
    срывался целиком, хотя headless работал бы нормально.
    """
    with contextlib.ExitStack() as stack:
        val = None
        if not headless:
            try:
                val = stack.enter_context(stealth.open_page(headless=False, prefer=prefer, proxy=proxy))
            except Exception as exc:            # noqa: BLE001 — любую причину считаем «нет экрана»
                steps.append(f"видимое окно не открылось ({str(exc)[:70]}) — продолжаю в фоне")
        if val is None:
            val = stack.enter_context(stealth.open_page(headless=True, prefer=prefer, proxy=proxy))
        yield val


DIALOG_JS = r"""
(() => {
  const vis = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 30 && r.height > 20 && st.visibility !== 'hidden' && st.display !== 'none'
           && parseFloat(st.opacity || '1') > 0.3;
  };
  const boxes = [...document.querySelectorAll(
    "[role='dialog'], [aria-modal='true'], dialog[open], [class*='modal'], [class*='Modal']," +
    "[class*='popup'], [class*='Popup'], [class*='dialog'], [class*='Dialog'], [class*='overlay']," +
    "[class*='Overlay']")].filter(vis);
  const WORDS = ['ок, понятно', 'ок понятно', 'понятно', 'понял', 'got it', 'ok, got it',
                 'продолжить', 'принимаю', 'принять', 'согласен', 'соглашаюсь', 'accept',
                 'agree', 'i agree', 'close', 'закрыть', 'хорошо', 'ясно', 'ok', 'ок', 'да'];
  const BAD = ['купить', 'оплат', 'тариф', 'войти', 'login', 'sign up', 'регистрац', 'подписка на'];
  const found = [];
  for (const box of boxes) {
    const btns = [...box.querySelectorAll("button, a[role='button'], input[type='submit']," +
                                          " [class*='btn'], [class*='button']")].filter(vis);
    for (const b of btns) {
      const t = ((b.innerText || b.value || '').trim() || '').toLowerCase().slice(0, 40);
      if (!t) continue;
      if (BAD.some((w) => t.includes(w))) continue;
      const hit = WORDS.findIndex((w) => t === w || t.startsWith(w));
      if (hit < 0) continue;
      let sel = b.id ? '#' + CSS.escape(b.id) : '';
      if (!sel) {
        const cls = (b.className || '').toString().split(/\s+/).filter(Boolean)[0];
        sel = cls ? '.' + CSS.escape(cls) : '';
      }
      found.push({sel: sel, text: t, prio: hit});
    }
  }
  found.sort((a, b) => a.prio - b.prio);
  return JSON.stringify(found.slice(0, 3));
})()
"""


def _detail_with_captcha(detail: str, steps: list[str]) -> str:
    """Дописать в заметку, что капча была и её решили сами — это видно в журнале."""
    if any("капча решена" in st or "код капчи введён" in st for st in steps) and "капча" not in detail.lower():
        return detail + " · капча решена автоматически"
    return detail


def _autosolve(page, steps: list[str], *, allow_human: bool, kind_hint: str = "") -> bool:
    """Попробовать решить капчу самим. True — можно продолжать заход.

    Настройка «Решать капчи автоматически» выключается в интерфейсе: тогда поведение
    прежнее — сразу человек (или пропуск захода). Никаких платных сервисов.
    """
    if (db.get_setting("captcha_autosolve", "1") or "1") != "1":
        steps.append("автоматическое решение капчи выключено в настройках")
        return False
    try:
        res = captcha.solve(page)
    except Exception as exc:                       # OCR/движок не должен ломать заход
        steps.append(f"решатель капчи упал: {str(exc)[:120]}")
        return False
    if res.ok:
        label = "код капчи введён" if res.kind == "image" else "капча решена сама"
        steps.append(f"{label} ({res.kind}): {res.detail}")
        for st in res.steps:
            steps.append("· " + st)
        return True
    steps.append(f"капчу решить не удалось ({res.kind}): {res.detail}")
    for st in res.steps[-3:]:
        steps.append("· " + st)
    return False



def _solve_or_wait(page, steps: list[str], svc: dict, task_id: int,
                   headless: bool, allow_human: bool) -> bool:
    """Try automatic solving, then keep a visible task browser open for its operator."""
    if _autosolve(page, steps, allow_human=allow_human):
        return True
    if not allow_human or headless or any("продолжаю в фоне" in step for step in steps):
        return False
    # Для проверяемой кампании нет task_id: пользователь решает капчу в окне,
    # драйвер продолжает только после появления подтверждённого токена.
    if not task_id:
        try:
            wait_sec = max(0, min(int(db.get_setting('captcha_wait_sec', '300') or 0), 3600))
            steps.append('ожидаю ручного решения капчи в видимом окне')
            end = time.monotonic() + wait_sec
            while time.monotonic() < end:
                if captcha._tokens_present(page):
                    steps.append('капча подтверждена в окне')
                    return True
                page.wait(1000)
        except OperationCancelled:
            raise
        except Exception as exc:
            steps.append('ручная проверка недоступна: ' + str(exc)[:80])
        return False
    # Task and confirmation event belong to executor; import here avoids a module cycle.
    from core import executor
    try:
        wait_sec = max(0, min(int(db.get_setting("captcha_wait_sec", "300") or 0), 3600))
        if not wait_sec:
            return False
        event = executor.captcha_event(task_id)
        event.clear()
        executor._set_task(task_id, "captcha", "решите капчу в открытом окне и подтвердите в интерфейсе")
        steps.append("ожидаю ручного решения в открытом окне")
        end = time.monotonic() + wait_sec
        while time.monotonic() < end:
            if event.is_set() or captcha._tokens_present(page):
                executor._set_task(task_id, "running", "капча подтверждена, ожидаю ответ площадки")
                steps.append("капча подтверждена вручную; ожидаю ответ площадки")
                return True
            page.wait(1000)
    except Exception as exc:
        steps.append(f"не удалось дождаться ручного решения: {str(exc)[:100]}")
    return False


def dismiss_dialog(page, steps: list[str] | None = None, verbose: bool = True) -> str | None:
    """Закрыть всплывающее окно-«читалку» («Важно знать перед запуском» и подобные).

    Бесплатные панели любят вешать модалку с единственной кнопкой «Ок, понятно»:
    без этого клика заказ не уходит, а робот видит «непонятный ответ». Возвращает
    текст нажатой кнопки или None.
    """
    try:
        raw = page.js(DIALOG_JS)
        items = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except Exception:
        items = []
    # рекламные слои fancybox (SMMYT и родственные) перехватывают клики: закрываем раньше читалок
    try:
        if page.js("""(() => { const o=document.querySelector('#fancybox-overlay');
            if(!o||getComputedStyle(o).display==='none') return false;
            if(window.jQuery&&jQuery.fancybox){try{jQuery.fancybox.close();return true}catch(e){}}
            const c=document.querySelector('.fancybox-close,.fancybox-item,#fancybox-close');
            if(c){c.click();return true} o.click(); return true; })()"""):
            if steps is not None:
                steps.append("закрыл рекламный слой fancybox")
            page.wait(random.randint(400, 800))
    except Exception:
        pass
    if not items:
        return None
    btn = items[0]
    sel = btn.get("sel")
    clicked = bool(sel) and page.click_human(sel)
    if not clicked:
        label = (btn.get("text") or "").lower()
        page.js("""(() => {
          const vis = (el) => { const r = el.getBoundingClientRect();
            return r.width > 30 && r.height > 20 && getComputedStyle(el).display !== 'none'; };
          const b = [...document.querySelectorAll('button, a')].filter(vis)
            .find(e => ((e.innerText || '').trim() || '').toLowerCase().startsWith(%s));
          if (b) b.click();
        })()""" % json.dumps(label[:30]))
        clicked = True
    if clicked and steps is not None and verbose:
        steps.append(f"закрыл окно «{btn.get('text')}»")
    page.wait(random.randint(400, 800))
    return btn.get("text")



# ------------------------------------------------------------------ SMMCode: бонусная услуга 0 ₽

SMMCODE_FREE_VARIANT_JS = """(() => {
  const items = [...document.querySelectorAll('.form-service-item')];
  const free = items.find(e => parseFloat(e.dataset.price || '1') === 0);
  if (free) {
    free.click();
    return JSON.stringify({found: true, id: free.dataset.service_id, min: free.dataset.min,
                           max: free.dataset.max, name: free.dataset.name});
  }
  const fb = document.querySelector('#smm_free_boost_form, #free_boost');
  if (fb) return JSON.stringify({found: true, form: true});
  return JSON.stringify({found: false, prices: items.slice(0, 5).map(e => e.dataset.price)});
})()"""


def _run_smmcode(page, target_url: str, steps: list[str], *, dry_run: bool, on_submit=None) -> tuple[str, str, int, int]:
    """Бонус-услуга SMMCode (0 ₽, раз в 20 минут на каждый вид).

    Сайт периодически включает/выключает бонус-форму: если бесплатный вариант
    (data-price = 0) или форма #smm_free_boost_form на странице — подаём заявку,
    иначе честно сообщаем, что бонус сейчас не отрисовался (платное не трогаем).
    """
    started = time.time()
    try:
        info = json.loads(page.js(SMMCODE_FREE_VARIANT_JS) or "{}")
    except Exception:
        info = {}
    if not info.get("found"):
        prices = ", ".join(str(p) for p in (info.get("prices") or []))
        return ("need_human",
                f"бонус-форма SMMCode сейчас не отрисовалась (варианты на странице: {prices} ₽/шт) — "
                f"сайт временно выключил бонус; повторим при следующем заходе", 0, int(time.time() - started))
    steps.append("бонус-вариант найден" + (f": #{info.get('id')} «{str(info.get('name'))[:40]}»" if info.get("id") else ""))
    page.wait(random.randint(1200, 2000))
    # количество: минимум из data-min (иначе 10)
    try:
        qty = max(1, int(float(info.get("min") or 10)))
    except Exception:
        qty = 10
    page.js("(() => {const e=document.querySelector('#count_value'); if(e){e.value=%d; e.dispatchEvent(new Event('input',{bubbles:true})); e.dispatchEvent(new Event('change',{bubbles:true}))}})()" % qty)
    if not page.type_human("#link", target_url):
        page.js("(() => {const e=document.querySelector('#link'); if(e){e.value=%r; e.dispatchEvent(new Event('input',{bubbles:true}));return true} return false})()" % target_url)
    email = _contact_email()
    if email:
        if not page.type_human("#email", email):
            page.js("(() => {const e=document.querySelector('#email'); if(e){e.value=%r; e.dispatchEvent(new Event('input',{bubbles:true}));return true} return false})()" % email)
    steps.append(f"ссылка и количество {qty} заполнены")
    if dry_run:
        return "ok", "dry-run: бонус-форма заполнена, отправка не выполнялась", 0, int(time.time() - started)
    page.wait(random.randint(700, 1300))
    if on_submit:
        on_submit()
    clicked = page.click_human("button:has-text('Запустить накрутку')")
    if not clicked and on_submit:
        return "need_human", "SMMCode: результат клика неизвестен; повтор запрещён", 0, int(time.time() - started)
    if not clicked:
        clicked = page.js("""(() => {const b=[...document.querySelectorAll('button')]
              .find(x => (x.innerText||'').includes('Запустить накрутку') && x.offsetParent!==null);
            if(b){b.click();return 'ok'} return 'no'})()""") == 'ok'
    if not clicked:
        return "site_broken", "кнопка «Запустить накрутку» не найдена", 0, int(time.time() - started)
    page.wait(random.randint(5000, 8000))
    text = page.text() or ""
    low = text.lower()
    if "успешн" in low or "запущен" in low or "заказ" in low and "оформлен" in low:
        return "ok", f"бонус-заявка SMMCode отправлена ({qty} шт.)", qty, int(time.time() - started)
    if "20 минут" in low or "повтор" in low or "позже" in low:
        return "quota", "бонус этого вида уже использовали — раз в 20 минут", 0, int(time.time() - started)
    return "need_human", "SMMCode не подтвердил запуск: " + text[:160], 0, int(time.time() - started)


# ------------------------------------------------------------------ SMM8: бесплатные подписчики ВК без логина

SMM8_STATE_JS = r"""(() => {
  const tokEl = document.querySelector('[name="cf-turnstile-response"]');
  const tp = document.querySelector('.fsc-page.timer-page');
  const th = document.querySelector('.thanks-page');
  const tEl = document.querySelector('#timeTimer');
  const err = document.querySelector('.fsc-inline-error');
  const thVis = th && !th.classList.contains('hide');
  return JSON.stringify({
    tok: !!(tokEl && (tokEl.value || '').length > 10),
    timer_page: !!(tp && !tp.classList.contains('hide')),
    thanks: !!thVis,
    thanks_err: !!(thVis && th.classList.contains('fsc-error')),
    err_msg: thVis && th.classList.contains('fsc-error')
      ? ((th.querySelector('.thanks-page-error-message') || {}).textContent || '')
      : (err && !err.classList.contains('hide') ? err.textContent : ''),
    timer: tEl ? tEl.textContent : ''
  });
})()"""


def _smm8_turnstile_click(page, steps: list[str]) -> bool:
    """Клик по чекбоксу Turnstile, спрятанному в ЗАКРЫТОМ shadow root.

    document.querySelectorAll('iframe') его не видит (проверено), а фреймы
    Playwright/Patchright — видят. Клик в левую часть фрейма (сам чекбокс).
    """
    try:
        frames = [f for f in page._p.frames if "challenges.cloudflare.com" in f.url]
        if not frames:
            return False
        box = frames[0].frame_element().bounding_box()
        if not box or box.get("height", 0) < 5:
            return False
        page._p.mouse.click(box["x"] + 28, box["y"] + box["height"] / 2)
        steps.append("клик по чекбоксу Turnstile (shadow-DOM iframe)")
        return True
    except Exception:
        return False


# ------------------------------------------------------------------ антибот-шлюзы («Подтвердите, что вы человек»)

ANTIBOT_GATE_JS = r"""(() => {
  const t = (document.body ? document.body.innerText : '').toLowerCase();
  const gate = t.includes('подтвердите, что вы человек') || t.includes('похожий цвет') ||
               t.includes('антибот клау') || t.includes('нажмите на кнопку, если она появилась');
  if (!gate) return JSON.stringify({gate: false});
  let btn = null;
  for (const e of document.querySelectorAll('button, a, input[type=button], input[type=submit], [class*=btn]')) {
    if (!e.offsetParent) continue;
    const txt = (e.innerText || e.value || '').toLowerCase();
    if (/не робот|проверк|продолжить|подтверд|перейти/.test(txt)) { btn = e; break; }
  }
  return JSON.stringify({gate: true, btn: !!btn});
})()"""


def _antibot_gate(page) -> dict:
    try:
        return json.loads(page.js(ANTIBOT_GATE_JS) or "{}")
    except Exception:
        return {}


def _antibot_try_pass(page, steps: list[str]) -> bool:
    """Кликнуть кнопку шлюза, если она есть. True — шлюз пропустил."""
    try:
        clicked = page.js(r"""(() => {
          const els = [...document.querySelectorAll('button, a, input[type=button], input[type=submit], [class*=btn]')];
          const b = els.find(e => e.offsetParent && /не робот|проверк|продолжить|подтверд|перейти/i.test(e.innerText || e.value || ''));
          if (!b) return 'no-btn';
          b.click();
          return 'clicked';
        })()""")
        if clicked != "clicked":
            steps.append("кнопки шлюза нет на странице — проверка может пройти сама")
        page.wait(random.randint(6000, 9000))
    except Exception as exc:
        steps.append("клик по шлюзу не удался: " + str(exc)[:80])
        return False
    return not _antibot_gate(page).get("gate")


# ------------------------------------------------------------------ кнопка «Попробовать бесплатно»

FREE_BTN_JS = r"""(() => {
  const els = [...document.querySelectorAll('button, a, [class*=btn], [class*=free], [class*=trial]')];
  const b = els.find(e => e.offsetParent &&
      /попробовать бесплатно|попробуй.{0,4}бесплатно|бесплатный тест|тест бесплатно|получить бесплатно/i.test(e.innerText || ''));
  if (!b) return 'no';
  b.click();
  return 'clicked';
})()"""


def _run_smm8(page, target_url: str, steps: list[str], *, dry_run: bool,
              allow_captcha_wait: bool = False, task_id: int = 0,
              headless: bool = True, hold_budget: int = 2200, on_submit=None) -> tuple[str, str, int, int]:
    """SMM8 /free-vk-followers: 10 подписчиков ВК без логина, раз в 30 мин, до 3/сутки.

    Механика прочитана из виджета ravin.js (telegramadviser.com/api):
    «Get It Now» → Turnstile-токен → POST /api/start {code:'vkfollowers', link, token}
    → тикет → страница ДОЛЖНА оставаться открытой wait=1800 сек → POST /api {ticket}
    → заказ создан (thanks-page). Автоклик по чекбоксу с датацентрового IP Cloudflare
    отклоняет — тогда ждём человека в окне задачи, дальше всё доводим сами.
    """
    started = time.time()

    def state() -> dict:
        try:
            return json.loads(page.js(SMM8_STATE_JS) or "{}")
        except Exception:
            return {}

    # 1) ссылка
    if not page.type_human("#inputOptinLinkLoggedIn", target_url):
        ok = page.js("(() => {const e=document.getElementById('inputOptinLinkLoggedIn'); if(!e) return 'no';"
                     " const st=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
                     " st.call(e,%s); e.dispatchEvent(new Event('input',{bubbles:true}));"
                     " e.dispatchEvent(new Event('change',{bubbles:true})); return 'ok'})()" % json.dumps(target_url))
        if ok != "ok":
            return "site_broken", "SMM8: не нашёл поле ссылки free-vk-followers", 0, int(time.time() - started)
        steps.append("ссылка вставлена через события")
    else:
        steps.append("ссылка напечатана")
    if dry_run:
        return "ok", "dry-run: ссылка заполнена, заказ не отправлялся", 0, int(time.time() - started)

    # Первый клик может сразу создать заявку или запустить автоматическую
    # отправку после таймера. Зафиксировать риск до действия, не после thanks.
    if on_submit:
        on_submit()
    # Не нажимаем второй раз при неопределённом ответе: это может быть дубликат.
    began = time.time()
    st: dict = {}
    for attempt in range(1):
        steps.append("жму «Get It Now» (попытка %d)" % (attempt + 1))
        page.js("(() => {const b=document.getElementById('btnOptinLoggedIn'); if(b) b.click(); return 1})()")
        deadline = time.time() + 40
        clicked = False
        while time.time() < deadline:
            page.wait(4000)
            st = state()
            if st.get("timer_page") or st.get("err_msg"):
                break
            if (not clicked and time.time() - began > 6
                    and db.get_setting('captcha_autosolve', '1') == '1'):
                clicked = _smm8_turnstile_click(page, steps)
        st = state()
        if st.get("timer_page") or st.get("tok"):
            break

    # Явный отказ не надо ошибочно называть капчей или обходить сменой маршрута.
    error_message = str(st.get('err_msg') or '').strip()
    error_verdict, _ = read_verdict(error_message, False, False)
    if error_message and error_verdict in ('quota', 'bad_link'):
        return error_verdict, 'SMM8: ' + error_message[:180], 0, int(time.time() - started)

    # 3) Turnstile не решён сам — человек в видимом окне задачи.
    if not (st.get("timer_page") or st.get("tok")):
        solved = False
        visible = not headless and not any('продолжаю в фоне' in step for step in steps)
        if allow_captcha_wait and visible:
            from core import executor
            try:
                wait_sec = max(0, min(int(db.get_setting("captcha_wait_sec", "300") or 0), 3600))
                if wait_sec:
                    event = executor.captcha_event(task_id) if task_id else None
                    if event:
                        event.clear()
                    if task_id:
                        executor._set_task(task_id, "captcha",
                                           "SMM8: кликните чекбокс «Я человек» в открытом окне — дальше всё автоматически")
                    steps.append("ожидаю ручного клика по Turnstile-чекбоксу")
                    end = time.monotonic() + wait_sec
                    while time.monotonic() < end:
                        st = state()
                        if st.get("timer_page") or st.get("tok"):
                            solved = True
                            break
                        if event and event.is_set() and (st.get("tok") or st.get("timer_page")):
                            solved = True
                            break
                        page.wait(1000)
                    if solved:
                        if task_id:
                            executor._set_task(task_id, "running", "Turnstile решён — довожу заказ SMM8 до конца")
                        steps.append("Turnstile решён, продолжаю автоматически")
            except Exception as exc:
                steps.append("ручное ожидание не удалось: " + str(exc)[:90])
        if not solved:
            st = state()
            if not (st.get("timer_page") or st.get("tok")):
                return ("captcha",
                        "SMM8: проверка Turnstile не завершена; для ручного решения нужен видимый браузер"
                        + (". Ответ площадки: " + error_message[:140] if error_message else ""),
                        0, int(time.time() - started))

    # 4) заявка стартовала: держим страницу открытой до конца таймера (wait=1800 c)
    steps.append("заявка стартовала: страница остаётся открытой на всё время таймера")
    hold_end = time.time() + hold_budget
    while time.time() < hold_end:
        page.wait(20000)
        st = state()
        if st.get("thanks"):
            if st.get("thanks_err"):
                msg = (st.get("err_msg") or "").strip()
                low = msg.lower()
                if any(k in low for k in ("already", "cooldown", "daily", "limit",
                                          "active order", "try again later")):
                    return "quota", "SMM8: лимит («%s») — раз в 30 мин, до 3 заказов в сутки" % msg[:90], \
                        0, int(time.time() - started)
                return "need_human", "SMM8 отклонил финальную отправку: " + msg[:140], 0, int(time.time() - started)
            # SMM8 сам отправляет финальный заказ после длительного таймера;
            # риск был сохранён до первого клика.
            return ("ok",
                    "заказ SMM8 создан: 10 подписчиков (страница продержана на таймере, финальная "
                    "отправка выполнена)", 10, int(time.time() - started))
    return ("need_human",
            "SMM8: таймер 30 мин не дошёл до финала за время ожидания — страница была открыта, "
            "повторим при следующем заходе", 0, int(time.time() - started))


# ------------------------------------------------------------------ SMMPrime: бесплатные просмотры постов ВК

SMMPRIME_QUESTION_JS = r"""(() => {const l=document.getElementById('question');return l?l.innerText:'';})()"""


def _smmprime_frame(page):
    """Iframe free-api.smmprime.com — вся бесплатная секция живёт в нём."""
    try:
        for f in page._p.frames:
            if "free-api.smmprime" in f.url:
                return f
    except Exception:
        pass
    return None


def _run_smmprime(page, target_url: str, steps: list[str], *, dry_run: bool,
                  budget: int = 170, on_submit=None) -> tuple[str, str, int, int]:
    """SMMPrime /free: «ВК Просмотры 100 шт. · бесплатно», без регистрации.

    Механика (подтверждена боевой подачей 2026-11-12, заказ принят, пул 447→446):
    в iframe free-api.smmprime.com выбираем кнопку .service «ВК Просмотры», вводим
    ссылку на пост в #link, ждём личный таймер (~30 c, /api/visit available_at),
    получаем вопрос-проверку «Проверка: N + M = ?» (#question) → решаем →
    POST /api/order → {"status":"accepted"}. Пул общий на всех посетителей
    (~450/день), поэтому заказ может не пройти, когда пул исчерпан.
    """
    started = time.time()
    f = None
    for _ in range(20):
        page.wait(3000)
        f = _smmprime_frame(page)
        if f:
            break
    if f is None:
        return "site_broken", "SMMPrime: iframe free-api не загрузился", 0, int(time.time() - started)
    steps.append("iframe free-api.smmprime найден")
    try:
        sel = f.evaluate("""(() => {const bs=[...document.querySelectorAll('button.service')];"""
                         """const vk=bs.find(b=>/ВК/i.test(b.innerText));if(!vk)return 'NO_VK';"""
                         """vk.click();return 'OK';})()""")
    except Exception as exc:
        return "site_broken", "SMMPrime: не удалось выбрать услугу (" + str(exc)[:80] + ")", \
            0, int(time.time() - started)
    if sel != "OK":
        return "site_broken", "SMMPrime: кнопка «ВК Просмотры» не найдена — список услуг изменился", \
            0, int(time.time() - started)
    steps.append("услуга «ВК Просмотры 100 шт.» выбрана")
    page.wait(random.randint(800, 1600))
    f.evaluate("(() => {const i=document.getElementById('link');if(!i)return 'NO';i.focus();"
               "i.value=%s;i.dispatchEvent(new Event('input',{bubbles:true}));"
               "i.dispatchEvent(new Event('change',{bubbles:true}));return 'OK';})()" % json.dumps(target_url))
    steps.append("ссылка на пост введена")
    if dry_run:
        return "ok", "dry-run: услуга и ссылка заполнены, заказ не отправлялся", 0, int(time.time() - started)

    captured = {}

    def _on_resp(q):
        if "free-api.smmprime.com/api/order" in q.url:
            try:
                captured["body"] = q.text()[:400]
            except Exception:
                pass

    try:
        page._p.on("response", _on_resp)
    except Exception:
        pass

    deadline = time.time() + budget
    clicked = False
    while time.time() < deadline:
        page.wait(5000)
        try:
            q = f.evaluate(SMMPRIME_QUESTION_JS)
            btn = f.evaluate("(() => {const b=document.getElementById('order');"
                             "return b?{t:(b.innerText||'').slice(0,40),dis:!!b.disabled}:{t:'',dis:true};})()")
        except Exception:
            continue
        m = re.search(r"(\d+)\s*([+\-*x×])\s*(\d+)\s*=", q or "")
        if m and not btn.get("dis"):
            a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
            ans = a + b if op == "+" else (a - b if op == "-" else a * b)
            f.evaluate("(() => {const i=document.getElementById('answer');if(!i)return 'NO';i.focus();"
                       "i.value=%s;i.dispatchEvent(new Event('input',{bubbles:true}));return 'OK';})()"
                       % json.dumps(str(ans)))
            steps.append("вопрос-проверка решена: %s = %s" % ((q or "").strip()[:40], ans))
            page.wait(random.randint(600, 1200))
            if on_submit:
                on_submit()  # риск фиксируется до возможного заказа
            f.evaluate("(() => {const b=document.getElementById('order');if(b&&!b.disabled)b.click();return 1;})()")
            clicked = True
            steps.append("нажала «Получить бесплатно»")
            break
    if not clicked:
        return ("need_human",
                "SMMPrime: за время ожидания не пришёл вопрос-проверка (личный таймер ~30 c, "
                "общий пул мог быть занят) — повторим при следующем заходе",
                0, int(time.time() - started))
    for _ in range(8):
        page.wait(3000)
        if captured.get("body"):
            break
    body = (captured.get("body") or "").lower()
    if '"accepted"' in body:
        return ("ok",
                "SMMPrime: 100 просмотров ВК — заказ принят сервером" +
                (" (demo)" if '"demo": true' in body else ""), 100, int(time.time() - started))
    if any(k in body for k in ("limit", "pool", "законч", "исчерп", "try later", "available")):
        return "quota", "SMMPrime: общий бесплатный пул на сегодня исчерпан/занят — " + body[:110], \
            0, int(time.time() - started)
    if body:
        return "need_human", "SMMPrime не принял заказ: " + body[:140], 0, int(time.time() - started)
    return "need_human", "SMMPrime: ответ сервера не получен после нажатия — повторим при следующем заходе", \
        0, int(time.time() - started)


# ------------------------------------------------------------------ SMMnakrutka: форма после таймера

# PRSkill renders its paid order and the free giveaway next to each other.
# Select only the uniquely identified giveaway card, and never manufacture or
# solve its Turnstile token. An empty token means the free order is unavailable
# to this non-interactive runner, not that delivery failed.
PRSKILL_GIVEAWAY_JS = r"""(() => {
  const inputs = [...document.querySelectorAll('input[placeholder="https://vk.com/***"]')];
  const marker = document.querySelector('#giveaway-form');
  if (inputs.length !== 1 || !marker) return JSON.stringify({state:'unexpected'});
  const input = inputs[0], card = input.closest('div.form');
  if (!card || !(marker.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING))
    return JSON.stringify({state:'unexpected'});
  const text = (card.innerText || '').toLowerCase().replace(/\s+/g, ' ');
  if (!text.includes('накрутка бесплатно') || !text.includes('подписчиков вк') || !/\b10\b/.test(text))
    return JSON.stringify({state:'unexpected'});
  const buttons = [...card.querySelectorAll('button')].filter(b => (b.innerText || '').trim().toLowerCase() === 'заказать');
  if (buttons.length !== 1 || buttons[0].disabled || input.disabled || input.readOnly)
    return JSON.stringify({state:'unexpected'});
  const extra = [...card.querySelectorAll('input, select')].filter(e =>
    /(?:payment|paymethod|price|cost|summa|оплат|стоимост|email|password)/i.test(
      (e.name || '') + ' ' + (e.id || '') + ' ' + (e.type || '')) && !e.disabled);
  if (extra.length) return JSON.stringify({state:'unexpected'});
  const token = [...card.querySelectorAll('input[name="cf-turnstile-response"]')];
  if (token.length !== 1 || (token[0].value || '').length < 10) return JSON.stringify({state:'captcha'});
  const selector = el => {
    let node = el, parts = [];
    while (node && node.nodeType === 1 && node.tagName !== 'HTML') {
      const parent = node.parentElement;
      if (!parent) break;
      const siblings = [...parent.children].filter(x => x.tagName === node.tagName);
      parts.unshift(node.tagName.toLowerCase() + (siblings.length > 1 ? ':nth-of-type(' + (siblings.indexOf(node)+1) + ')' : ''));
      const s = parts.join(' > ');
      if (document.querySelectorAll(s).length === 1 && document.querySelector(s) === el) return s;
      node = parent;
    }
    return '';
  };
  const button = selector(buttons[0]);
  if (!button) return JSON.stringify({state:'unexpected'});
  return JSON.stringify({state:'ready', link:'input[placeholder="https://vk.com/***"]',
                         button, value:input.value || ''});
})()"""


def _prskill_giveaway_state(page) -> dict:
    try:
        raw = page.js(PRSKILL_GIVEAWAY_JS)
        return json.loads(raw) if isinstance(raw, str) else (raw or {})
    except OperationCancelled:
        raise
    except Exception:
        return {}


def _run_prskill(page, svc: dict, target_url: str, steps: list[str], *, dry_run: bool, on_submit=None) -> DriverResult:
    """Only the free VK giveaway; paid order fields and unsolved challenges are untouched."""
    started = time.monotonic()
    expected_url = svc.get('url') or ''
    state: dict = {}
    for _ in range(6):
        if not _same_service_location(expected_url, page.js('window.location.href') or ''):
            return DriverResult('site_broken', 'PRSkill: страница перенаправлена; заказ не отправлен', steps=steps)
        state = _prskill_giveaway_state(page)
        if state.get('state') != 'captcha':
            break
        page.wait(2000)  # only wait for the site's own passive challenge
    if state.get('state') == 'captcha':
        return DriverResult('captcha', 'PRSkill: бесплатная форма требует подтверждения Turnstile; '
                            'без человека заказ не отправлен', steps=steps)
    if state.get('state') != 'ready':
        return DriverResult('need_human', 'PRSkill: бесплатная форма изменилась или не найдена; '
                            'заказ не отправлен', steps=steps)
    if not set_field(page, state['link'], target_url):
        return DriverResult('site_broken', 'PRSkill: ссылка не сохранилась в бесплатной форме', steps=steps)
    ready = _prskill_giveaway_state(page)
    if (ready.get('state') != 'ready' or ready.get('link') != state['link']
            or ready.get('button') != state['button'] or ready.get('value', '').strip() != target_url.strip()
            or not _same_service_location(expected_url, page.js('window.location.href') or '')):
        return DriverResult('need_human', 'PRSkill: бесплатная форма изменилась; заказ не отправлен', steps=steps)
    if dry_run:
        return DriverResult('ok', 'dry-run: бесплатная форма найдена, заказ не отправлен', steps=steps)
    before = page.text() or ''
    if on_submit:
        on_submit()
    again = _prskill_giveaway_state(page)
    if (again.get('state') != 'ready' or again.get('link') != ready['link']
            or again.get('button') != ready['button'] or again.get('value', '').strip() != target_url.strip()
            or not _same_service_location(expected_url, page.js('window.location.href') or '')):
        return DriverResult('need_human', 'PRSkill: форма изменилась перед кликом; проверьте возможную отправку',
                            steps=steps, submitted=bool(on_submit))
    if not page.click_human(ready['button']):
        return DriverResult('need_human', 'PRSkill: результат клика неизвестен; повтор запрещён',
                            steps=steps, submitted=True)
    steps.append('нажата кнопка бесплатной формы PRSkill; повтор не выполняется')
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        page.wait(1000)
        verdict, detail = read_verdict_delta(before, page.text() or '', False, False)
        if verdict in ('ok', 'bad_link', 'quota'):
            return DriverResult(verdict, 'PRSkill: ' + detail, units=int(svc.get('amount') or 0),
                                waited=int(time.monotonic() - started), steps=steps, submitted=True)
    return DriverResult('need_human', 'PRSkill: ответ после отправки не распознан; повтор запрещён',
                        waited=int(time.monotonic() - started), steps=steps, submitted=True)


# У этой площадки основная видимая форма НЕ отправляет заказ. После 300 секунд
# сама страница открывает другую форму в .t-popup_show и асинхронно заполняет
# служебные поля. Селекторы и соответствие service/количества основаны на
# публичной разметке страниц VK/TG; при изменении разметки лучше не отправлять.
SMMNAKRUTKA_FORM_JS = r"""((platform, amount) => {
  const vis = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect(), st = getComputedStyle(el);
    return r.width > 4 && r.height > 4 && st.visibility !== 'hidden' && st.display !== 'none';
  };
  const pops = [...document.querySelectorAll('.t-popup.t-popup_show')].filter(vis);
  if (!pops.length) return JSON.stringify({state:'waiting'});
  if (pops.length !== 1) return JSON.stringify({state:'unexpected'});
  const pop = pops[0], forms = [...pop.querySelectorAll('form')].filter(vis);
  if (forms.length !== 1) return JSON.stringify({state:'unexpected'});
  const form = forms[0], id = form.id;
  if (!id || document.querySelectorAll('#' + CSS.escape(id)).length !== 1 ||
      (form.getAttribute('method') || '').toLowerCase() !== 'post' ||
      form.getAttribute('data-formactiontype') !== '2')
    return JSON.stringify({state:'unexpected'});
  const links = [...form.querySelectorAll('input[name="link"]')].filter(vis);
  const buttons = [...form.querySelectorAll('button[type="submit"]')].filter(vis);
  const choices = [...form.querySelectorAll('select[name="quantity-text"]')];
  const qtys = [...form.querySelectorAll('input[name="quantity"]')];
  const title = ((pop.querySelector('.t702__title') || {}).textContent || '').toLowerCase();
  const free = ((pop.querySelector('.t702__form-bottom-text') || {}).textContent || '')
    .trim().replace(/\s+/g, ' ').toLowerCase();
  const choice = choices.length === 1 ? (choices[0].value || '').toLowerCase() : '';
  const name = platform === 'vk' ? 'вк' : platform === 'telegram' ? 'телеграм' : '';
  const submitText = buttons.length === 1 ? (buttons[0].innerText || '').trim().toLowerCase() : '';
  if (!name || !title.includes('подписчик') || !title.includes(name) ||
      !choice.includes('подписчик') || !choice.includes(name) || free !== 'абсолютно бесплатно' ||
      links.length !== 1 || links[0].type !== 'url' || links[0].readOnly || links[0].disabled ||
      buttons.length !== 1 || submitText !== 'заказать' || qtys.length !== 1 ||
      qtys[0].value !== String(amount))
    return JSON.stringify({state:'unexpected'});
  const emails = [...form.querySelectorAll('input[type="email"]')].filter(vis);
  if (emails.length > 1) return JSON.stringify({state:'unexpected'});
  const requiredEmail = emails.length === 1 && (emails[0].required ||
    emails[0].getAttribute('aria-required') === 'true' || emails[0].getAttribute('data-tilda-req') === '1');
  const unchecked = [...form.querySelectorAll('input[type="checkbox"]')].some(e =>
    vis(e) && !e.checked && (e.required || e.getAttribute('aria-required') === 'true' ||
      e.getAttribute('data-tilda-req') === '1'));
  if (unchecked) return JSON.stringify({state:'consent'});
  const success = form.querySelector('.js-successbox');
  if (success && vis(success) && (success.innerText || '').trim())
    return JSON.stringify({state:'unexpected'});
  const required = ['formservices[]', 'rand', 'endtime', 'verify', 'url_page',
                    'sessionToken', 'permissionKey', 'service'];
  for (const n of required) {
    const nodes = [...form.querySelectorAll('input')].filter(e => e.name === n);
    if (nodes.length > 1) return JSON.stringify({state:'unexpected'});
    if (nodes.length !== 1 || !String(nodes[0].value || '').trim())
      return JSON.stringify({state:'waiting_tokens'});
  }
  const service = form.querySelector('input[name="service"]').value.trim();
  const expectedService = platform === 'vk' ? '179' : '649';
  if (service !== expectedService) return JSON.stringify({state:'service_mismatch'});
  const expectedPage = platform === 'vk' ? 'free-vk-followers' : 'free-telegram-followers';
  if (form.querySelector('input[name="url_page"]').value.trim() !== expectedPage)
    return JSON.stringify({state:'unexpected'});
  if (buttons[0].disabled) return JSON.stringify({state:'waiting_tokens'});
  const base = '#' + CSS.escape(id);
  return JSON.stringify({state:'ready', link:base + ' input[name="link"]',
    button:base + ' button[type="submit"]',
    email:requiredEmail ? base + ' input[type="email"]' : '',
    value:links[0].value || '', successUrl:form.getAttribute('data-success-url') || ''});
})"""

SMMNAKRUTKA_RESPONSE_JS = r"""((formSelector) => {
  const vis = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect(), st = getComputedStyle(el);
    return r.width > 4 && r.height > 4 && st.visibility !== 'hidden' && st.display !== 'none';
  };
  const form = document.querySelector(formSelector);
  const success = form && form.querySelector('.js-successbox');
  const errors = form ? [...form.querySelectorAll('.js-errorbox-all, .t-input-error')]
    .filter(vis).map(e => (e.innerText || '').trim()).filter(Boolean) : [];
  return JSON.stringify({url:window.location.href,
    success:success && vis(success) ? (success.innerText || '').trim().slice(0,300) : '',
    errors:errors.join(' ').slice(0,300)});
})"""


def _same_service_location(expected: str, actual: str, *, compare_query: bool = False) -> bool:
    """Никаких заказов или доверия подтверждению после перехода на другой сайт/путь."""
    from urllib.parse import urlsplit
    try:
        a, b = urlsplit(expected), urlsplit(actual)
        if a.scheme not in ('https', 'http') or a.scheme != b.scheme or not a.hostname or not b.hostname:
            return False
        if a.username or a.password or b.username or b.password:
            return False
        def port(u):
            return u.port or (443 if u.scheme == 'https' else 80)
        return (a.hostname.lower() == b.hostname.lower() and port(a) == port(b)
                and a.path.rstrip('/') == b.path.rstrip('/')
                and (not compare_query or a.query == b.query))
    except (ValueError, TypeError):
        return False


def _smmnakrutka_state(page, platform: str, amount: int) -> dict:
    try:
        raw = page.js('(' + SMMNAKRUTKA_FORM_JS + ')(' + json.dumps(platform) + ',' + str(amount) + ')')
        return json.loads(raw) if isinstance(raw, str) else (raw or {})
    except OperationCancelled:
        raise
    except Exception:
        return {}


def _smmnakrutka_response(page, form_selector: str) -> dict:
    try:
        raw = page.js('(' + SMMNAKRUTKA_RESPONSE_JS + ')(' + json.dumps(form_selector) + ')')
        return json.loads(raw) if isinstance(raw, str) else (raw or {})
    except OperationCancelled:
        raise
    except Exception:
        return {}


def _run_smmnakrutka(page, svc: dict, target_url: str, steps: list[str], *, dry_run: bool,
                      budget: int, on_submit=None) -> DriverResult:
    """Дождаться настоящей бесплатной формы и нажать её не более одного раза."""
    from urllib.parse import urljoin
    started = time.monotonic()
    platform = svc.get('platform') or ''
    amount = int(svc.get('amount') or 0)
    expected_url = svc.get('url') or ''

    def result(verdict, detail, *, submitted=False, units=0):
        return DriverResult(verdict, detail, units=units, waited=int(time.monotonic() - started),
                            steps=steps, submitted=submitted)

    steps.append('ожидаю открытия настоящей бесплатной формы SMMnakrutka после таймера')
    deadline = started + max(1, budget)
    state = {}
    while time.monotonic() < deadline:
        current = page.js('window.location.href') or ''
        if not _same_service_location(expected_url, current):
            return result('site_broken', 'SMMnakrutka: страница перенаправлена на другой адрес; заказ не отправлен')
        state = _smmnakrutka_state(page, platform, amount)
        kind = state.get('state')
        if kind == 'ready':
            break
        if kind in ('unexpected', 'consent', 'service_mismatch'):
            why = {'unexpected': 'форма или бесплатная услуга не совпадает с ожидаемой',
                   'consent': 'форма требует дополнительного согласия',
                   'service_mismatch': 'код услуги не соответствует выбранным подписчикам'}[kind]
            return result('need_human', 'SMMnakrutka: ' + why + '; заказ не отправлен')
        if discover(page).get('captcha') or captcha.visible_widgets(page):
            return result('captcha', 'SMMnakrutka: требуется проверка человека до заказа')
        page.wait(2000)
    else:
        why = ('поля проверки таймера не появились' if state.get('state') == 'waiting_tokens'
               else 'форма не открылась после таймера')
        return result('need_human', 'SMMnakrutka: ' + why + '; заказ не отправлен')

    steps.append('открылась форма подписчиков; обязательные поля проверки присутствуют')
    if not set_field(page, state['link'], target_url):
        return result('site_broken', 'SMMnakrutka: ссылка не сохранилась в форме; заказ не отправлен')
    if state.get('email'):
        email = _contact_email()
        if not email:
            return result('need_human', 'SMMnakrutka требует e-mail; укажите его в настройках')
        if not set_field(page, state['email'], email):
            return result('site_broken', 'SMMnakrutka: обязательный e-mail не сохранился')
    # Проверяем, что форма не подменилась во время ввода.
    ready = _smmnakrutka_state(page, platform, amount)
    if (ready.get('state') != 'ready' or ready.get('link') != state['link']
            or ready.get('button') != state['button'] or ready.get('value', '').strip() != target_url.strip()
            or not _same_service_location(expected_url, page.js('window.location.href') or '')):
        return result('need_human', 'SMMnakrutka: форма изменилась до отправки; заказ не отправлен')
    if discover(page).get('captcha') or captcha.visible_widgets(page):
        return result('captcha', 'SMMnakrutka: требуется проверка человека до заказа')
    form_selector = ready['button'].rsplit(' button', 1)[0]
    before_response = _smmnakrutka_response(page, form_selector)
    if not before_response or before_response.get('success') or before_response.get('errors'):
        return result('need_human', 'SMMnakrutka: состояние формы неясно до отправки; заказ не отправлен')
    if dry_run:
        return result('ok', 'dry-run: настоящая форма заполнена, заказ не отправлен')

    before_url = page.js('window.location.href') or ''
    success_url = urljoin(before_url, ready.get('successUrl') or '')
    if on_submit:
        on_submit()  # измерение и отметка риска должны завершиться ДО клика
    # Получение счётчика может занять время: ещё раз проверяем адрес и содержимое.
    after_callback = _smmnakrutka_state(page, platform, amount)
    latest_response = _smmnakrutka_response(page, form_selector)
    if (after_callback.get('state') != 'ready' or after_callback.get('link') != ready['link']
            or after_callback.get('button') != ready['button'] or after_callback.get('successUrl') != ready.get('successUrl')
            or after_callback.get('value', '').strip() != target_url.strip()
            or not latest_response or latest_response.get('errors') or latest_response.get('success')
            or not _same_service_location(before_url, page.js('window.location.href') or '', compare_query=True)
            or discover(page).get('captcha') or captcha.visible_widgets(page)):
        return result('need_human', 'SMMnakrutka: форма изменилась перед кликом; проверьте возможную отправку',
                      submitted=bool(on_submit))
    if not page.click_human(ready['button']):
        return result('need_human', 'SMMnakrutka: результат клика неизвестен; повтор запрещён', submitted=True)
    steps.append('нажата кнопка настоящей формы; повтор не выполняется')

    response_end = time.monotonic() + 20
    while time.monotonic() < response_end:
        page.wait(1000)
        response = _smmnakrutka_response(page, form_selector)
        current = response.get('url') or ''
        if current and not _same_service_location(expected_url, current):
            return result('need_human', 'SMMnakrutka: после клика открыт другой адрес; результат неизвестен',
                          submitted=True)
        error = str(response.get('errors') or '').strip()
        if error:
            verdict, why = read_verdict(error, False, False)
            if verdict in ('bad_link', 'quota', 'captcha'):
                return result(verdict, 'SMMnakrutka: ' + why, submitted=True)
            return result('need_human', 'SMMnakrutka: форма показала ошибку: ' + error[:160], submitted=True)
        if (current and not _same_service_location(before_url, current, compare_query=True)
                and _same_service_location(expected_url, success_url)
                and _same_service_location(success_url, current, compare_query=True)):
            return result('ok', 'SMMnakrutka: форма показала страницу подтверждения; доставка не подтверждена',
                          submitted=True, units=amount)
        message = str(response.get('success') or '')
        if message.lower().startswith('спасибо за доверие'):
            return result('ok', 'SMMnakrutka: форма показала подтверждение; доставка не подтверждена',
                          submitted=True, units=amount)
        if discover(page).get('captcha') or captcha.visible_widgets(page):
            return result('captcha', 'SMMnakrutka: после клика нужна проверка человека; повтор запрещён',
                          submitted=True)
    return result('need_human', 'SMMnakrutka: ответ после клика не получен; повтор запрещён', submitted=True)


# ------------------------------------------------------------------ LikeBoom: бесплатный заказ ≤15 ₽

LIKEBOOM_SELECT_CATEGORY_JS = """(() => {
  const sel = document.querySelector('#category-list');
  if (!sel) return 'no-category';
  sel.value = '5';
  sel.dispatchEvent(new Event('change', {bubbles: true}));
  return 'ok';
})()"""

LIKEBOOM_SERVICE_OPTIONS_JS = """(() => JSON.stringify([...document.querySelectorAll('#service-list option')]
  .map(o => ({v: o.value, t: (o.textContent || '').trim()}))))()"""

LIKEBOOM_SELECT_SERVICE_JS = """((v) => {
  const sel = document.querySelector('#service-list');
  if (!sel) return 'no-service';
  sel.value = v;
  sel.dispatchEvent(new Event('change', {bubbles: true}));
  return 'ok';
})"""

LIKEBOOM_TOTAL_PRICE_JS = r"""(() => {
  const form = document.querySelector('.lk-new-task') || document.body;
  const m = (form.innerText || '').match(/([0-9]+[.,][0-9]{2})\s*(?:₽|руб)/g) || [];
  return JSON.stringify(m);
})()"""

# У актуальной обычной формы выбранный radio и реально отправляемый
# OrderForm[paymentMethod] — разные поля. Одного radio недостаточно: скрытое
# поле могло остаться платным. Если формат бесплатной формы неизвестен,
# заказ останавливаем вместо предположения, что купон гарантирует 0 ₽.
LIKEBOOM_FREE_PAYMENT_JS = r"""(() => {
  const links = [...document.querySelectorAll('#link')];
  if (links.length !== 1 || !links[0].form) return false;
  const form = links[0].form;
  const selected = [...form.querySelectorAll(
    'input[name*=payment]:checked, select[name*=payment], '
    + 'input[name*=method]:checked, select[name*=method]')].filter(e => !e.disabled);
  const submitted = [...form.querySelectorAll('[name="OrderForm[paymentMethod]"]')]
    .filter(e => !e.disabled);
  const isFree = e => /^(free|бесплатно)$/i.test((e.value || '').trim());
  return selected.length === 1 && submitted.length === 1 &&
    isFree(selected[0]) && isFree(submitted[0]);
})()"""


def _likeboom_free_payment(page) -> bool:
    return page.js(LIKEBOOM_FREE_PAYMENT_JS) is True


def _ru_price(text: str) -> float | None:
    """«246,96 ₽» / «1\u00a0420,50 руб» -> float; иначе None."""
    m = re.search(r"([0-9]+(?:[ \u00a0][0-9]{3})*[.,][0-9]+)", (text or "").replace(",", "."))
    if not m:
        return None
    return float(m.group(1).replace(" ", "").replace("\u00a0", ""))


def _parse_option_price(text: str) -> tuple[float, float] | None:
    """«99,06 ₽ / 1000» -> (99.06, 1000)."""
    m = re.search(r"([0-9]+(?:[ \u00a0][0-9]{3})*[.,][0-9]+)\s*(?:₽|руб)\s*/\s*([0-9]{2,5})", text or "")
    if not m:
        return None
    return float(m.group(1).replace(" ", "").replace("\u00a0", "").replace(",", ".")), float(m.group(2))


def _option_per_unit(text: str) -> float | None:
    """Цена за штуку из текста опции: «99,06 ₽ / 1000 (1,486 ₽ / 100)» -> 0.9906."""
    t = text or ""
    for pat in (r"\(([0-9]+[.,][0-9]+)\s*(?:₽|руб)\s*/\s*(\d{2,5})\)",
                r"([0-9]+[.,][0-9]+)\s*(?:₽|руб)\s*/\s*(\d{2,5})\)"):
        m = re.search(pat, t)
        if m:
            price = float(m.group(1).replace(",", "."))
            base = float(m.group(2))
            if base > 0:
                return price / base
    m = re.search(r"([0-9]+[.,][0-9]+)\s*(?:₽|руб)\s*/\s*(\d{2,5})", t)
    if m:
        price = float(m.group(1).replace(",", "."))
        base = float(m.group(2))
        if base > 0:
            return price / base
    return None


def _likeboom_pick_option(options: list[dict]) -> tuple[dict | None, float]:
    """Дешёвая «Подписчики в паблик/группу» VK: сначала без «Высоких списаний»."""
    cand = [o for o in options if "подписчик" in (o.get("t") or "").lower()
            and ("паблик" in (o.get("t") or "").lower() or "групп" in (o.get("t") or "").lower())]
    if not cand:
        return None, 0.0
    def key(o):
        per = _option_per_unit(o.get("t") or "")
        waste = "высокие списания" in (o.get("t") or "").lower()
        return (0 if per is not None else 1, 1 if waste else 0, per if per is not None else 9e9)
    best = min(cand, key=key)
    return best, (_option_per_unit(best.get("t") or "") or 9e9)


def _run_likeboom(page, target_url: str, steps: list[str], *, dry_run: bool,
                  wait_button_sec: float = 170.0, on_submit=None) -> tuple[str, str, int, int]:
    """Мастер free-trial: таймер 2 минуты → «Получить бесплатно» → заказ ≤15 ₽.

    Возвращает (verdict, detail, units, waited). E-mail берётся из настроек приложения.
    """
    started = time.time()
    email = _contact_email()
    if not email:
        return "need_human", "для LikeBoom нужен e-mail: укажите его в Настройках («E-mail для заказов»)", 0, 0
    # В доступном free-trial.js площадки start() вызывается при загрузке,
    # хотя текст инструкции предлагает нажать таймер. Дополнительный клик
    # по неясному элементу здесь не делаем; ждём штатную кнопку.
    steps.append("ожидание бесплатного таймера LikeBoom (~2 мин)")
    deadline = time.time() + wait_button_sec
    while time.time() < deadline:
        try:
            if page.js("""(() => {const b=[...document.querySelectorAll('button')]
                   .find(x => (x.innerText||'').includes('Получить бесплатно') && x.offsetParent!==null);
                 return b ? b.innerText.trim() : ''})()"""):
                break
        except Exception:
            pass
        page.wait(4000)
    else:
        return "need_human", "кнопка «Получить бесплатно» не появилась за отведённое время", 0, int(time.time() - started)
    try:
        page.click_human("button:has-text('Получить бесплатно')") or \
            page.js("""(() => {const b=[...document.querySelectorAll('button')]
                    .find(x => (x.innerText||'').includes('Получить бесплатно'));
                  if(b){b.click();return 'ok'} return 'no'})()""")
    except Exception:
        pass
    page.wait(random.randint(1800, 2600))
    if page.js(LIKEBOOM_SELECT_CATEGORY_JS) != "ok":
        return "site_broken", "мастер LikeBoom не открылся (нет списка категорий)", 0, int(time.time() - started)
    # услуги подгружаются ajax-ом после смены категории — ждём появления «паблик/групп»
    options: list[dict] = []
    for _ in range(10):
        page.wait(random.randint(1200, 1800))
        try:
            options = json.loads(page.js(LIKEBOOM_SERVICE_OPTIONS_JS) or "[]")
        except Exception:
            options = []
        if _likeboom_pick_option(options)[0]:
            break
    opt, _ = _likeboom_pick_option(options)
    if not opt:
        return "site_broken", "в списке услуг VK LikeBoom нет подписчиков для паблика/группы", 0, int(time.time() - started)
    if page.js("%s(%r)" % (LIKEBOOM_SELECT_SERVICE_JS, opt["v"])) != "ok":
        return "site_broken", "не удалось выбрать услугу в мастере LikeBoom", 0, int(time.time() - started)
    page.wait(random.randint(2000, 2800))
    # держим суммарную стоимость в бесплатных 15 ₽: читаем цены формы и дробим количество
    qty = 100
    for _try in range(4):
        page.js("(() => {const e=document.querySelector('#amount'); if(e){e.value=%d; e.dispatchEvent(new Event('input',{bubbles:true})); e.dispatchEvent(new Event('change',{bubbles:true}))}})()" % qty)
        page.wait(random.randint(800, 1300))
        try:
            prices = json.loads(page.js(LIKEBOOM_TOTAL_PRICE_JS) or "[]")
        except Exception:
            prices = []
        total = None
        for p_txt in prices:
            val = _ru_price(p_txt)
            if val is not None:
                total = val          # первая цена на форме — сумма заказа
                break
        if total is None or total <= 15.0:
            break
        new_qty = max(10, int(15.0 / total * qty) // 10 * 10)
        if new_qty >= qty:
            new_qty = max(10, qty // 10 * 10 - 10)
        steps.append(f"сумма {total:.2f} ₽ выше бесплатного лимита — уменьшаю количество до {new_qty}")
        qty = new_qty
    if not page.type_human("#link", target_url):
        page.js("(() => {const e=document.querySelector('#link'); if(e){e.value=%r; e.dispatchEvent(new Event('input',{bubbles:true}));return true} return false})()" % target_url)
    if not page.type_human("#order-email", email):
        page.js("(() => {const e=document.querySelector('#order-email'); if(e){e.value=%r; e.dispatchEvent(new Event('input',{bubbles:true}));return true} return false})()" % email)
    page.wait(random.randint(900, 1500))
    try:
        prices = json.loads(page.js(LIKEBOOM_TOTAL_PRICE_JS) or "[]")
    except Exception:
        prices = []
    steps.append(f"выбрана услуга {opt['t'][:60]}, количество {qty}, цены на форме: {', '.join(prices[:3]) or 'нет'}")
    if dry_run:
        return "ok", "dry-run: форма LikeBoom заполнена, отправка не выполнялась", qty, int(time.time() - started)
    # Не создавать потенциально платный заказ, даже если его цена меньше рекламного лимита.
    # Проверяем и выбранный способ, и значение, которое фактически уйдёт в форме.
    if not _likeboom_free_payment(page):
        return "need_human", "LikeBoom: бесплатный способ оплаты не подтверждён — заказ не отправлен", 0, int(time.time() - started)
    page.wait(random.randint(600, 1200))
    if on_submit:
        on_submit()
    # Пока снимался новый счётчик, страница могла изменить способ оплаты.
    if not _likeboom_free_payment(page):
        return "need_human", "LikeBoom: способ оплаты изменился перед кликом; проверьте возможную отправку", 0, int(time.time() - started)
    clicked = page.click_human("button:has-text('Оформить заказ')")
    if not clicked and on_submit:
        return "need_human", "LikeBoom: результат клика неизвестен; повтор запрещён", 0, int(time.time() - started)
    if not clicked:
        clicked = page.js("""(() => {const b=[...document.querySelectorAll('button')]
              .find(x => (x.innerText||'').includes('Оформить заказ') && x.offsetParent!==null);
            if(b){b.click();return 'ok'} return 'no'})()""") == 'ok'
    if not clicked:
        return "site_broken", "кнопка «Оформить заказ» не найдена", 0, int(time.time() - started)
    page.wait(random.randint(6000, 9000))
    text = page.text() or ""
    m = re.search(r"Заказ №\s*(\d+)", text)
    if m:
        steps.append(f"заказ №{m.group(1)} оформлен")
        return "ok", f"заказ №{m.group(1)} оформлен бесплатно (метод FREE)", qty, int(time.time() - started)
    if "оформлен бесплатно" in text:
        return "ok", "заказ оформлен бесплатно (номер не распознан)", qty, int(time.time() - started)
    return "need_human", "после отправки LikeBoom не показал страницу заказа: " + text[:160], 0, int(time.time() - started)


class OperationCancelled(Exception):
    pass


class _InterruptiblePage:
    """Даёт кнопке «Остановить» прерывать длинные ожидания между действиями браузера."""
    def __init__(self, page, event):
        self._page, self._event = page, event

    def wait(self, ms):
        remaining = max(0, int(ms))
        while remaining:
            if self._event.is_set():
                raise OperationCancelled('Остановлено пользователем')
            chunk = min(remaining, 1000)
            self._page.wait(chunk)
            remaining -= chunk

    def __getattr__(self, name):
        attr = getattr(self._page, name)
        if name.startswith('_') or not callable(attr):
            return attr
        def guarded(*args, **kwargs):
            if self._event.is_set():
                raise OperationCancelled('Остановлено пользователем')
            return attr(*args, **kwargs)
        return guarded


def run(svc: dict, target_url: str, *, task_id: int = 0, dry_run: bool = False,
        prefer: str | None = None, headless: bool = True, allow_captcha_wait: bool = False,
        deadline_sec: int = 180, proxy: str | None = None, on_submit=None,
        cancel_event=None, normalize_target: bool = True) -> DriverResult:
    """Заход браузером. on_submit вызывается перед возможной отправкой заказа."""
    site_id = svc["site_id"]
    target_url = normalize_link(site_id, target_url) if normalize_target else target_url
    prof = profiles.get_profile(site_id) or {}
    mem = _memory(site_id)
    steps: list[str] = []
    focus_sel: str | None = None          # к чему прокручивать скриншот
    shot_path = SHOTS / f"{site_id}-{int(time.time())}-{task_id or 'check'}.png"

    started = time.time()
    try:
        with _open_smart(headless, prefer, steps, proxy=proxy) as (backend, page):
            if cancel_event is not None:
                page = _InterruptiblePage(page, cancel_event)
            steps.append(f"браузер: {backend}")
            page.goto(svc["url"], wait=random.randint(1200, 2200))
            page.mouse_wiggle(2)
            page.scroll()
            page.wait(random.randint(300, 900))

            # PRSkill показывает рядом платную и бесплатную формы. Обрабатывать
            # можно только отдельно проверенную бесплатную карточку.
            if (site_id == 'prskill' and svc.get('stype') == 'followers'
                    and svc.get('platform') == 'vk'):
                res = _run_prskill(page, svc, target_url, steps, dry_run=dry_run, on_submit=on_submit)
                res.backend = backend
                res.shot = _shot(page, shot_path)
                return res

            # В SMMnakrutka модальное окно и есть настоящая форма; его нельзя
            # закрывать общим обработчиком диалогов или отправлять ложную форму под ним.
            if (site_id == 'smmnakrutka' and svc.get('stype') == 'followers'
                    and svc.get('platform') in ('vk', 'telegram')):
                budget = max(int(deadline_sec), int(svc.get('hold_seconds') or 300) + 90)
                res = _run_smmnakrutka(page, svc, target_url, steps, dry_run=dry_run,
                                        budget=min(budget, 3600), on_submit=on_submit)
                res.backend = backend
                res.shot = _shot(page, shot_path)
                return res

            dismiss_dialog(page, steps)          # всплывающее «Важно знать…», cookie-плашки

            # отдельный сценарий: SMMCode — бонусная услуга 0 ₽ (форма появляется/исчезает на стороне сайта)
            if site_id == "smmcode":
                verdict, detail, units, waited2 = _run_smmcode(page, target_url, steps, dry_run=dry_run, on_submit=on_submit)
                shot = _shot(page, shot_path, "#link")
                return DriverResult(verdict, detail, units=units, waited=waited2,
                                    shot=shot, backend=backend, steps=steps)

            # отдельный сценарий: SMM8 — бесплатные подписчики ВК без логина
            # (Turnstile-чекбокс в закрытом shadow root + таймер 30 мин с открытой страницей)
            if site_id == "smm8":
                verdict, detail, units, waited2 = _run_smm8(
                    page, target_url, steps, dry_run=dry_run,
                    allow_captcha_wait=allow_captcha_wait, task_id=task_id,
                    headless=headless, hold_budget=int(svc.get("hold_seconds") or 2100) + 120,
                    on_submit=on_submit)
                shot = _shot(page, shot_path, "#inputOptinLinkLoggedIn")
                return DriverResult(verdict, detail, units=units, waited=waited2,
                                    shot=shot, backend=backend, steps=steps)

            # отдельный сценарий: SMMPrime — бесплатные просмотры постов ВК (iframe + вопрос)
            if site_id == "smmprime":
                verdict, detail, units, waited2 = _run_smmprime(page, target_url, steps, dry_run=dry_run, on_submit=on_submit)
                shot = _shot(page, shot_path, "#link")
                return DriverResult(verdict, detail, units=units, waited=waited2,
                                    shot=shot, backend=backend, steps=steps)


            # отдельный сценарий: LikeBoom — таймер + мастер заказа ≤15 ₽
            if site_id == "likeboom":
                verdict, detail, units, waited = _run_likeboom(page, target_url, steps, dry_run=dry_run, on_submit=on_submit)
                shot = _shot(page, shot_path, ".lk-new-task__submit")
                return DriverResult(verdict, detail, units=units, waited=waited,
                                    shot=shot, backend=backend, steps=steps)

            # 0) страница вообще та ли? 404 и «страница не найдена» — частая беда каталога
            head_text = (page.text() or "").lower()
            if any(m in head_text[:600] for m in ("404 - документ не найден", "404 not found",
                                                 "страница не найдена", "page not found",
                                                 "страница не существует")):
                shot = _shot(page, shot_path, focus_sel)
                return DriverResult("site_broken", "страница услуги не открылась (404) — "
                                    "в каталоге устарел адрес", shot=shot, backend=backend, steps=steps)

            # 0b) заглушка Cloudflare «Just a moment…»: стелс-движки проходят её сами
            if captcha.is_cloudflare_wall(page.text()):
                cf = captcha.solve_cloudflare(page, steps=steps)
                if cf.ok:
                    state = discover(page)
                else:
                    shot = _shot(page, shot_path, focus_sel)
                    why = "Cloudflare не пропустил автоматически"
                    if allow_captcha_wait:
                        return DriverResult("captcha", why + " — требуется ручная проверка",
                                            shot=shot, backend=backend, steps=steps)
                    return DriverResult("captcha", why, shot=shot, backend=backend, steps=steps)

            # 0c) антибот-шлюз «Подтвердите, что вы человек» (например, smmyt.ru): кнопка
            # кликается автоматически, цветовые загадки — человеком в окне задачи
            if _antibot_gate(page).get("gate"):
                if db.get_setting('captcha_autosolve', '1') != '1':
                    shot = _shot(page, shot_path, focus_sel)
                    return DriverResult('captcha', 'антибот-шлюз требует ручной проверки; '
                                        'автоматическое решение выключено',
                                        shot=shot, backend=backend, steps=steps)
                steps.append("антибот-шлюз «я не робот»: пробую пройти сам")
                if not _antibot_try_pass(page, steps):
                    if not _solve_or_wait(page, steps, svc, task_id, headless, allow_captcha_wait):
                        shot = _shot(page, shot_path, focus_sel)
                        return DriverResult("captcha", "антибот-шлюз «я не робот» не пройден — нужен клик человека",
                                            shot=shot, backend=backend, steps=steps)
                    if not _antibot_try_pass(page, steps):
                        shot = _shot(page, shot_path, focus_sel)
                        return DriverResult("captcha", "антибот-шлюз не пропустил после подтверждения человека",
                                            shot=shot, backend=backend, steps=steps)
                steps.append("антибот-шлюз пройден")


            state = discover(page)
            learned = {}

            # 1) капча до всякого ввода
            if state.get("captcha"):
                if _solve_or_wait(page, steps, svc, task_id, headless, allow_captcha_wait):
                    state = discover(page)          # после капчи страница могла перерисоваться
                else:
                    shot = _shot(page, shot_path, focus_sel)
                    why = "капча не пройдена автоматически"
                    if allow_captcha_wait:
                        return DriverResult("captcha", why + " — требуется ручная проверка",
                                            shot=shot, backend=backend, steps=steps)
                    return DriverResult("captcha", why + " (" + ", ".join(state["captcha"][:2]) + ")",
                                        shot=shot, backend=backend, steps=steps)

            # 2) поле ссылки: сначала из памяти, потом по-новому
            field = None
            if mem.get("link_sel"):
                if page.js(f"(() => {{const e=document.querySelector({mem['link_sel']!r});"
                           f" return !!e;}})())"):
                    field = {"sel": mem["link_sel"], "type": "text"}
                    steps.append("поле ссылки: из памяти")
            if field is None:
                field = pick_link_field(state.get("fields") or [])
            if field is None and not on_submit:
                # Эвристические клики по таймеру не подходят для проверяемой
                # кампании: заранее неизвестно, не создаёт ли клик заказ.
                state, tried = start_timer(page, state, steps,
                                           max_wait=min(int(svc.get("hold_seconds") or 150), 200))
                if tried:
                    field = pick_link_field(state.get("fields") or [])
            if field is None and not on_submit:
                try:
                    if page.js(FREE_BTN_JS) == "clicked":
                        steps.append("кликнула «Попробовать бесплатно» — форма раскрылась")
                        page.wait(random.randint(2500, 4000))
                        state = discover(page)
                        field = pick_link_field(state.get("fields") or [])
                except Exception:
                    pass
            if field is None:
                shot = _shot(page, shot_path, focus_sel)
                return DriverResult("site_broken", "не нашёл поле для ссылки — форма изменилась",
                                    shot=shot, backend=backend, steps=steps)
            learned["link_sel"] = field["sel"]
            focus_sel = field["sel"]           # до клика снимаем окно с формой
            steps.append(f"поле ссылки: {field['sel'][:60]}")
            if on_submit:
                scoped = same_form_state(page, state, field['sel'])
                if not scoped:
                    return DriverResult('need_human', 'бесплатная форма и поле ссылки не связаны однозначно; '
                                        'заказ не отправлен', backend=backend, steps=steps)
                state = scoped

            typed = page.type_human(field["sel"], target_url)
            if not typed:
                ok = page.js(f"""(() => {{
                    const el = document.querySelector({field['sel']!r});
                    if (!el) return false;
                    el.focus();
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value').set;
                    setter.call(el, {target_url!r});
                    el.dispatchEvent(new Event('input', {{bubbles: true}}));
                    el.dispatchEvent(new Event('change', {{bubbles: true}}));
                    return true;
                }})()""")
                steps.append("ссылка вставлена через события (поле не приняло печать)")
                if not ok:
                    shot = _shot(page, shot_path, focus_sel)
                    return DriverResult("site_broken", "поле ссылки не приняло текст",
                                        shot=shot, backend=backend, steps=steps)
            page.wait(random.randint(250, 700))

            # 2b) React-панели часто рисуют кнопку только после ввода ссылки — смотрим ещё раз
            refreshed = discover(page)
            if on_submit:
                refreshed = same_form_state(page, refreshed, field['sel'])
                if not refreshed:
                    return DriverResult('need_human', 'форма изменилась после ввода ссылки; заказ не отправлен',
                                        backend=backend, steps=steps)
            if refreshed.get("buttons"):
                state = {**state, "buttons": refreshed["buttons"], "fields": refreshed.get("fields") or state.get("fields")}

            # 3) поле количества — если площадка даёт выбрать объём
            count_field = pick_count_field(state.get("fields") or [], field["sel"])
            amount = int(svc.get("amount") or 0)
            if count_field and amount:
                if page.type_human(count_field["sel"], str(amount)):
                    learned["count_sel"] = count_field["sel"]
                    steps.append(f"количество: {amount}")

            # 4) почта, если её требует форма (берём из настроек, иначе не выдумываем)
            email_field = has_email_field(state.get("fields") or [])
            if email_field:
                email = db.get_setting("order_email", "") or ""
                if not email:
                    shot = _shot(page, shot_path, focus_sel)
                    return DriverResult("need_human", "площадка требует e-mail для заказа — "
                                        "впишите его в Настройках («E-mail для заказов»)",
                                        shot=shot, backend=backend, steps=steps)
                page.type_human(email_field["sel"], email)
                steps.append("e-mail из настроек подставлен")

            # 4b) форма заказа с выбором площадки: переключаем на нужную (иначе закажем «не туда»)
            if not on_submit and choose_platform(page, svc.get("platform") or "vk", steps):
                state = discover(page)

            # 5) таймер/очередь и «кнопка пока выключена» — ждём, но не дольше разумного
            waited = 0
            budget = max(20, deadline_sec - int(time.time() - started))
            button = None
            if mem.get("submit_sel") and (not on_submit or any(
                    b.get('sel') == mem['submit_sel'] for b in state.get('buttons') or [])):
                # из памяти берём, только если это по-прежнему похоже на кнопку заказа:
                # иначе застрявший селектор уводит робота на «Telegram» в выпадашке
                same = page.js("""(() => {
                  const el = document.querySelector(%r);
                  if (!el) return '';
                  const t = (el.innerText || el.value || '').trim().slice(0, 60);
                  const a = ((el.id || '') + ' ' + (el.className || '')).toLowerCase();
                  return el.tagName.toLowerCase() + '|' + (el.disabled ? '1' : '0') + '|' + t + '|' + a;
                })()""" % mem["submit_sel"])
                if same:
                    tag, dis, txt, attrs = (same.split("|") + ["", "", "", ""])[:4]
                    candidate = {"sel": mem["submit_sel"], "text": txt, "attrs": attrs,
                                 "disabled": dis == '1'}
                    looks = tag in ('button', 'input', 'a') and pick_submit_button([candidate]) is not None
                    if looks and not any(w in attrs for w in ("option", "dropdown", "tab", "select")):
                        button = candidate
                    else:
                        steps.append("селектор кнопки из памяти больше не подходит — ищу заново")
            if button is None:
                button = pick_submit_button(state.get("buttons") or [])
            if button is None and not dry_run:
                shot = _shot(page, shot_path, focus_sel)
                return DriverResult("need_human", "не нашёл кнопку заказа — форма нестандартная",
                                    shot=shot, backend=backend, steps=steps)

            if button is not None:
                # таймер прямо на кнопке (testknop-семейство): ждём до конца отсчёта
                cd = _wait_countdown(page, button["sel"], budget=max(0, budget - waited))
                if cd:
                    waited += cd
                    steps.append(f"ждал таймер на кнопке: {cd} с")
                if on_submit and _button_countdown(page, button['sel']) > 0:
                    return DriverResult('quota', 'таймер бесплатной формы ещё не закончился; '
                                        'заказ не отправлен', waited=waited, backend=backend, steps=steps)
                spent, ready = _wait_for_ready(page, button["sel"], max_sec=max(20, budget - waited))
                waited += spent
                if spent:
                    steps.append(f"ждал, пока кнопка станет доступной: {spent} с")
                if not ready:
                    shot = _shot(page, shot_path, focus_sel)
                    return DriverResult("quota", "кнопка заказа остаётся выключенной — "
                                        "площадка держит лимит, попробуем позже",
                                        waited=waited, shot=shot, backend=backend,
                                        learned=learned, steps=steps)

            if dry_run:
                shot = _shot(page, shot_path, focus_sel)
                btn = button or {}
                learned["submit_sel"] = btn.get("sel")
                remember(site_id, {**learned, "backend": backend, "dry_at": int(time.time()),
                                   "ok_n": mem.get("ok_n", 0), "fail_n": 0})
                return DriverResult("ok", "проверка прошла: поле найдено"
                                    + (f", кнопка «{btn['text']}»" if btn.get("text") else ", кнопку не увидел")
                                    + (f", ждал {waited} с" if waited else ""),
                                    waited=waited, shot=shot, backend=backend,
                                    learned=learned, steps=steps)

            # 6) кнопка заказа
            text_before = page.text() or ""
            focus_sel = button["sel"]
            learned["submit_sel"] = button["sel"]
            page.mouse_wiggle(2)
            if on_submit:
                if (discover(page).get('captcha') or captcha.visible_widgets(page)
                        or not free_form_guard(page, field['sel'], button['sel'], target_url, svc['url'])):
                    return DriverResult('need_human', 'бесплатность или состояние именно этой формы '
                                        'не подтверждено; заказ не отправлен', waited=waited,
                                        backend=backend, learned=learned, steps=steps)
                on_submit()
                if (discover(page).get('captcha') or captcha.visible_widgets(page)
                        or not free_form_guard(page, field['sel'], button['sel'], target_url, svc['url'])
                        or _button_countdown(page, button['sel']) > 0):
                    return DriverResult('need_human', 'форма изменилась перед кликом; '
                                        'проверьте возможную отправку', waited=waited,
                                        backend=backend, learned=learned, steps=steps, submitted=True)
            if not page.click_human(button["sel"]):
                if on_submit:
                    return DriverResult('need_human', 'результат клика неясен; повтор не выполняем',
                                        waited=waited, backend=backend, learned=learned,
                                        steps=steps, submitted=True)
                page.js(f"(() => {{const e=document.querySelector({button['sel']!r});"
                        f" if (e) e.click();}})())")
                steps.append("кнопка нажата через JS (обычный клик не прошёл)")
            steps.append(f"нажал «{str(button.get('text'))[:40]}»")
            page.wait(random.randint(1800, 3200))

            # 6b) вместо заказа мог выехать информационный попап — закрываем и жмём ещё раз
            if not on_submit and dismiss_dialog(page, steps):
                page.wait(random.randint(700, 1200))
                if page.click_human(button["sel"]):
                    steps.append(f"после окна нажал «{str(button.get('text'))[:40]}» ещё раз")
                    page.wait(random.randint(1500, 2500))

            # 6c) вместо заказа могла открыться форма-мастер (LikeBoom: «Заказать накрутку»).
            #     Заполняем по кругу: после каждого поля страница может дорисовать следующее.
            done_fields: set[str] = set()
            probe_w = scope_state(discover(page))
            probe_field = pick_link_field(probe_w.get("fields") or [])
            # мастер — это ЛИБО открывшееся окно, ЛИБО новое пустое поле ссылки, которого мы не
            # заполняли. Иначе после успешной отправки робот жал бы кнопку второй раз.
            wizard_needed = bool(probe_w.get("dialog")) or bool(
                probe_field and not (probe_field.get("value") or "").strip()
                and probe_field.get("sel") != learned.get("link_sel"))
            if wizard_needed and on_submit:
                return DriverResult('need_human', 'после первого клика открылась дополнительная форма; '
                                    'повторная отправка без проверки запрещена', waited=waited,
                                    backend=backend, learned=learned, steps=steps, submitted=True)
            for _pass in range(3 if wizard_needed else 0):
                state_w = scope_state(discover(page))
                wiz = pick_link_field(state_w.get("fields") or [])
                if not wiz or (wiz.get("value") or "").strip() or wiz["sel"] in done_fields:
                    wiz = None
                filled = False
                if wiz:
                    focus_sel = wiz["sel"]
                    done_fields.add(wiz["sel"])
                    set_field(page, wiz["sel"], target_url)
                    steps.append("мастер: ссылка заполнена")
                    filled = True
                em = has_email_field(state_w.get("fields") or [])
                email_now = db.get_setting("order_email", "") or ""
                if em and em["sel"] not in done_fields and not (em.get("value") or "").strip():
                    done_fields.add(em["sel"])
                    set_field(page, em["sel"], email_now)
                    steps.append("мастер: e-mail из настроек")
                    filled = True
                cnt = pick_count_field(state_w.get("fields") or [],
                                       (wiz or {}).get("sel") or (state_w.get("fields") or [{}])[0].get("sel"))
                if cnt and cnt["sel"] not in done_fields and int(svc.get("amount") or 0) \
                        and str(cnt.get("value") or "") != str(int(svc["amount"])):
                    done_fields.add(cnt["sel"])
                    set_field(page, cnt["sel"], str(int(svc["amount"])))
                    steps.append(f"мастер: количество {int(svc['amount'])}")
                    filled = True
                if tick_consents(page, steps):
                    filled = True
                if filled:
                    page.wait(random.randint(700, 1200))
                    continue
                b_w = pick_submit_button(state_w.get("buttons") or [])
                if b_w and not b_w.get("disabled"):
                    text_before = page.text() or ""
                    focus_sel = b_w["sel"]
                    if page.click_human(b_w["sel"]):
                        steps.append(f"мастер: нажал «{str(b_w.get('text'))[:40]}»")
                        page.wait(random.randint(2500, 4000))
                        dismiss_dialog(page, steps)
                break

            # 7) что ответила площадка: ждём до 15 с, потому что Turnstile/Cloudflare
            #    и «Checking…» приезжают с задержкой, а не в первую же секунду
            saw_captcha = False
            pending_seen = False
            verdict, why = "unknown", ""
            fin = time.time() + 15
            while True:
                after = discover(page)
                text = page.text()
                if after.get("captcha") or captcha.visible_widgets(page):
                    saw_captcha = True
                    break
                low = (text or "").lower()
                if any(w in low for w in ("checking", "проверя", "verifying", "please wait",
                                          "подождите", "just a moment")):
                    pending_seen = True
                verdict, why = read_verdict_delta(text_before, text, False, bool(prof.get("captcha")))
                # «Checking…» — площадка ещё думает: капча/Turnstile могут приехать позже,
                # поэтому по «непонятному ответу» не выходим, пока висит ожидание
                stop = verdict in ("ok", "bad_link", "quota") or (verdict == "need_human" and not pending_seen)
                if stop or time.time() > fin:
                    break
                page.wait(1500)

            if not saw_captcha and verdict not in ("ok", "bad_link", "quota", "need_human") \
                    and pending_seen:
                # площадка «думает» слишком долго и молчит — почти всегда это антибот-заслон
                shot = _shot(page, shot_path, focus_sel)
                return DriverResult("captcha", "площадка держит проверку «Checking…» — "
                                    "похоже на защиту от ботов, решите в окне",
                                    waited=waited, shot=shot, backend=backend, steps=steps)

            if saw_captcha:
                if _solve_or_wait(page, steps, svc, task_id, headless, allow_captcha_wait):
                    # капча пройдена — ждём, что ответит площадка, и читаем вердикт как обычно
                    for _ in range(4):
                        page.wait(1500)
                        text = page.text()
                        v2, why2 = read_verdict_delta(text_before, text, False, bool(prof.get("captcha")))
                        if v2 in ("ok", "bad_link", "quota"):
                            shot = _shot(page, shot_path, focus_sel)
                            if v2 == "ok":
                                remember(site_id, {**learned, "ok_n": int(mem.get("ok_n", 0)) + 1,
                                                   "learned_at": int(time.time()), "backend": backend})
                                return DriverResult("ok", _detail_with_captcha(why2, steps),
                                                    units=amount, waited=waited, shot=shot,
                                                    backend=backend, learned=learned, steps=steps)
                            if v2 == "quota":
                                learned["retry_sec"] = retry_delay_sec(text)
                            return DriverResult(v2, why2, waited=waited, shot=shot,
                                                backend=backend, learned=learned, steps=steps)
                elif allow_captcha_wait:
                    shot = _shot(page, shot_path, focus_sel)
                    return DriverResult("captcha", "после отправки появилась капча, автоматика не справилась — "
                                        "требуется ручная проверка",
                                        waited=waited, shot=shot, backend=backend, steps=steps)
                verdict, why = read_verdict_delta(text_before, text, True, False)
                shot = _shot(page, shot_path, focus_sel)
                if verdict == "ok":
                    remember(site_id, {**learned, "ok_n": int(mem.get("ok_n", 0)) + 1})
                    return DriverResult("ok", _detail_with_captcha(why, steps), units=amount,
                                        waited=waited, shot=shot, backend=backend,
                                        learned=learned, steps=steps)
                keep = {k: v for k, v in learned.items() if k != "submit_sel"}
                remember(site_id, {**keep, "fail_n": int(mem.get("fail_n", 0)) + 1})
                return DriverResult("captcha", why, waited=waited, shot=shot, backend=backend,
                                    learned=learned, steps=steps)

            # 8) ждём подтверждения: страница может обновиться не сразу
            verdict, why = read_verdict_delta(text_before, text, False, bool(prof.get("captcha")))
            for _ in range(4):
                if verdict in ("ok", "bad_link"):
                    break
                page.wait(1200)
                text = page.text()
                verdict, why = read_verdict_delta(text_before, text, False, bool(prof.get("captcha")))
                if verdict == "quota":
                    break

            shot = _shot(page, shot_path, focus_sel)
            if verdict == "ok":
                remember(site_id, {**learned, "ok_n": int(mem.get("ok_n", 0)) + 1,
                                   "learned_at": int(time.time()), "backend": backend})
                return DriverResult("ok", _detail_with_captcha(why, steps), units=amount,
                                    waited=waited, shot=shot, backend=backend,
                                    learned=learned, steps=steps)
            if verdict == "quota":
                pause = retry_delay_sec(text)
                learned["retry_sec"] = pause
                if pause > 3600:
                    why = why + " — судя по странице, до завтра"
                steps.append(f"следующая попытка через {pause // 3600} ч")
            keep = {k: v for k, v in learned.items() if k != "submit_sel"}   # чужую кнопку не запоминаем
            remember(site_id, {**keep, "fail_n": int(mem.get("fail_n", 0)) + 1,
                               "last_fail": verdict, "last_fail_at": int(time.time())})
            return DriverResult(verdict, why, waited=waited, shot=shot, backend=backend,
                                learned=learned, steps=steps)

    except OperationCancelled:
        return DriverResult("cancelled", "Остановлено пользователем; отправленный заказ отменить нельзя", steps=steps)
    except stealth.BrowserUnavailable as exc:
        return DriverResult("need_human", f"браузерный движок недоступен: {exc}",
                            backend="", steps=steps)
    except Exception as exc:
        return DriverResult("site_broken", f"сбой драйвера: {str(exc)[:180]}", steps=steps)


# ------------------------------------------------------------------ путь без браузера

class _FormParser(HTMLParser):
    """Вытаскивает из HTML первую форму с полями — без браузера и JS."""

    def __init__(self):
        super().__init__()
        self.forms: list[dict] = []
        self._cur: dict | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._cur = {"action": a.get("action") or "", "method": (a.get("method") or "post").lower(),
                         "fields": {}}
            self.forms.append(self._cur)
        elif self._cur is not None and tag in ("input", "textarea", "select"):
            name = a.get("name")
            if not name:
                return
            if (a.get("type") or "").lower() in ("checkbox", "radio"):
                self._cur["fields"][name] = a.get("value") or "on"
            else:
                self._cur["fields"][name] = a.get("value") or ""

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag):
        if tag == "form":
            self._cur = None


def nojs_capable(url: str, proxy: str | None = None) -> dict | None:
    """Можно ли сделать заход без браузера: обычная HTML-форма, без капчи и JS-зависимостей."""
    page = stealth.http_get(url, proxy=proxy)
    if not page or not page.get("status") or page.get("status") >= 400:
        return None
    html = page.get("text") or ""
    low = html.lower()
    if any(s in low for s in ("recaptcha", "hcaptcha", "turnstile", "cf-challenge", "challenge-platform")):
        return {"ok": False, "why": "на странице капча/антибот"}
    parser = _FormParser()
    try:
        parser.feed(html)
    except Exception:
        return None
    for form in parser.forms:
        names = list(form["fields"].keys())
        if not names:
            continue
        link_name = next((n for n in names
                          if any(w in n.lower() for w in ("link", "url", "ssylka", "ссылка"))), None)
        if link_name:
            return {"ok": True, "form": form, "link_field": link_name, "page": page}
    return {"ok": False, "why": "в HTML нет формы со ссылкой (форма рисуется скриптами)"}


def run_nojs(svc: dict, target_url: str, *, dry_run: bool = False, proxy: str | None = None,
             on_submit=None, cancel_event=None, normalize_target: bool = True) -> DriverResult:
    """Простая HTML-форма; после возможного POST никогда не повторять его без проверки."""
    if cancel_event is not None and cancel_event.is_set():
        return DriverResult('cancelled', 'Остановлено пользователем')
    if normalize_target:
        target_url = normalize_link(svc['site_id'], target_url)
    info = nojs_capable(svc["url"], proxy=proxy)
    if not info or not info.get("ok"):
        why = (info or {}).get("why") or "страница не открылась"
        return DriverResult("need_human", f"без браузера не выйдет: {why}", backend="curl_cffi")
    form = info["form"]
    data = dict(form["fields"])
    data[info["link_field"]] = target_url
    email_field = next((n for n in data if "mail" in n.lower()), None)
    if email_field:
        email = db.get_setting("order_email", "") or ""
        if not email:
            return DriverResult("need_human", "нужен e-mail для заказа — впишите в Настройках",
                                backend="curl_cffi")
        data[email_field] = email
    count_field = next((n for n in data if any(w in n.lower() for w in ("count", "amount", "quantity"))), None)
    if count_field and int(svc.get("amount") or 0):
        data[count_field] = str(int(svc["amount"]))
    if dry_run:
        return DriverResult("ok", f"форма разобрана без браузера: поля {list(data)[:6]}",
                            backend="curl_cffi", learned={"nojs": True})
    from urllib.parse import urljoin, urlsplit
    action = urljoin(svc['url'], form['action'] or svc['url'])
    # Не отправляем данные формы на посторонний домен и не преобразуем GET в POST.
    if form.get('method') != 'post' or urlsplit(action).hostname != urlsplit(svc['url']).hostname:
        return DriverResult('need_human', 'форма требует другой метод или внешний адрес; нужна ручная проверка',
                            backend='curl_cffi')
    if cancel_event is not None and cancel_event.is_set():
        return DriverResult('cancelled', 'Остановлено пользователем')
    if on_submit:
        on_submit()
    res = stealth.http_post(action, data, referer=svc["url"],
                            cookies=(info["page"] or {}).get("cookies"), proxy=proxy)
    if not res or not res.get("status"):
        return DriverResult("site_broken", f"POST не прошёл: {(res or {}).get('error', '')[:120]}",
                            backend="curl_cffi", submitted=True)
    verdict, why = read_verdict(res.get("text") or "", False, False)
    remember(svc["site_id"], {"nojs": True, "nojs_at": int(time.time()),
                              "ok_n": int(_memory(svc["site_id"]).get("ok_n", 0)) + (1 if verdict == "ok" else 0)})
    return DriverResult(verdict, why, units=int(svc.get("amount") or 0), backend="curl_cffi", submitted=True)


# ------------------------------------------------------------------ проверка площадки

def probe_site(site_id: str, service_id: str | None = None, *, prefer: str | None = None,
               headless: bool = True) -> dict:
    """Проверить площадку роботом: открыть форму, найти поля, остановиться до отправки.

    Ничего не отправляет — только смотрит, сможет ли робот пройти заход.
    """
    with db.rd() as conn:
        if service_id:
            row = conn.execute("SELECT v.*, s.name AS site_name FROM services v JOIN sites s ON s.id=v.site_id"
                               " WHERE v.id=?", (service_id,)).fetchone()
        else:
            row = conn.execute("SELECT v.*, s.name AS site_name FROM services v JOIN sites s ON s.id=v.site_id"
                               " WHERE v.site_id=? AND v.enabled=1 ORDER BY (v.platform='vk') DESC,"
                               " v.stype='followers' DESC LIMIT 1", (site_id,)).fetchone()
    if not row:
        raise ValueError("Не нашёл услугу для проверки")
    svc = dict(row)

    # ссылку берём из каталога услуг — подставляем «сухой» адрес, чтобы ничего не отправить
    demo = {"vk": "https://vk.com/club1", "telegram": "https://t.me/example",
            "instagram": "https://instagram.com/example", "youtube": "https://youtube.com/@example",
            "tiktok": "https://tiktok.com/@example", "twitter": "https://x.com/example"}.get(
        svc.get("platform") or "vk", "https://example.com/")
    res = run(svc, demo, dry_run=True, prefer=prefer, headless=headless)
    out = {"ok": res.verdict == "ok", "site_id": site_id, "service": svc.get("id"),
           "service_label": svc.get("label"), "url": svc.get("url"), **res.as_dict()}
    if res.verdict == "ok":
        old = profiles.get_profile(site_id) or {}
        cls = old.get("automation") or "C"
        # робот прошёл — площадку можно поднять в классе (C → B), если капчи не встретили
        if cls == "C" and not (old.get("captcha") or []):
            with db.tx() as conn:
                conn.execute("UPDATE site_profiles SET automation='B' WHERE site_id=?", (site_id,))
            profiles.reset_cache()
            out["upgraded"] = "C→B"
        db.log("info", f"{svc['site_name']}: робот проверил форму — {res.detail}", site_id)
    else:
        db.log("warn", f"{svc['site_name']}: робот не смог — {res.detail}", site_id)
    return out
