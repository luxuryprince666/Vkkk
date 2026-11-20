# -*- coding: utf-8 -*-
"""Зонд страниц площадок: тянем HTML и вытаскиваем факты о форме заказа.

Один и тот же код используют:
  * `tools/probe_sites.py` — разовый обход всего каталога (исследование);
  * `core/profiles.check_site()` — ежедневная самопроверка площадок внутри софта.

Зависимостей нет: только стандартная библиотека (urllib). Браузер для этого не нужен.
"""
from __future__ import annotations

import gzip
import re
import ssl
import time
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")

CAPTCHA_SIGNS = {
    "recaptcha": [r"google\.com/recaptcha", r"g-recaptcha", r"grecaptcha"],
    "hcaptcha": [r"hcaptcha\.com", r"h-captcha"],
    "turnstile": [r"challenges\.cloudflare\.com/turnstile", r"cf-turnstile"],
    "smartcaptcha": [r"smartcaptcha", r"captcha\.yandexcloud"],
    "image_captcha": [r"captcha\.php", r"captcha\.png", r"captcha\.jpg", r"/captcha/", r"секретный код"],
    "cloudflare": [r"cf-browser-verification", r"__cf_chl", r"just a moment"],
}

BLOCK_SIGNS = [r"just a moment", r"attention required", r"доступ ограничен", r"проверка браузера"]

# Антибот-шлюзы вида «Подтвердите, что вы человек» (например, «Антибот Клауд» на smmyt.ru).
# Это НЕ поломка: человек кликает проверку при заходе, дальше сайт работает как обычно.
ANTIBOT_SIGNS = [r"подтвердите, что вы человек", r"антибот клау", r"похожий цвет",
                 r"нажмите на кнопку, если она появилась", r"вы не робот"]

# как выглядит поле для ссылки: подсказки в placeholder/name/id
LINK_HINTS = ("ссыл", "url", "link", "http", "профил", "запис", "пост", "видео")
# как выглядит кнопка заказа
SUBMIT_HINTS = ("заказать", "получить", "отправить", "крутить", "накрутить", "запустить", "отправ")


def _maybe_gunzip(raw: bytes) -> bytes:
    """Некоторые серверы (антибот-шлюзы) отдают gzip без заголовка Content-Encoding.

    Смотрим на магию 1f 8b: если тело — gzip, распаковываем независимо от заголовков.
    """
    if raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw)
        except Exception:
            pass
    return raw


def fetch(url: str, timeout: int = 20) -> dict:
    """GET с браузерным UA. Возвращает {status, url, html, sec, bytes|error}."""
    t0 = time.time()
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip",
    })
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            raw = _maybe_gunzip(r.read())
            return {"status": r.status, "url": r.geturl(), "html": raw.decode("utf-8", "replace"),
                    "sec": round(time.time() - t0, 1), "bytes": len(raw)}
    except urllib.error.HTTPError as e:
        raw = _maybe_gunzip(e.read())
        body = raw.decode("utf-8", "replace")
        return {"status": e.code, "url": url, "html": body, "sec": round(time.time() - t0, 1),
                "bytes": len(body), "http_error": True}
    except Exception as exc:
        return {"status": None, "url": url, "html": "", "sec": round(time.time() - t0, 1),
                "error": f"{type(exc).__name__}: {exc}"[:160]}


def text_of(html: str) -> str:
    """Видимый текст страницы (грубо, без парсера — для эвристик этого хватает)."""
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def fields_of(html: str, limit: int = 40) -> list[dict]:
    """Поля ввода страницы: name / placeholder / type."""
    out = []
    for tag in re.findall(r"<input[^>]+>", html, re.I)[:limit]:
        name = re.search(r'name="([^"]*)"', tag, re.I)
        ph = re.search(r'placeholder="([^"]*)"', tag, re.I)
        typ = re.search(r'type="([^"]*)"', tag, re.I)
        out.append({"name": (name.group(1) if name else ""),
                    "ph": (ph.group(1) if ph else ""),
                    "type": (typ.group(1) if typ else "text")})
    return out


def buttons_of(html: str, limit: int = 20) -> list[str]:
    """Тексты кнопок: <button>, <a class="btn">, <input type=submit>."""
    texts = []
    for m in re.findall(r"<button[^>]*>(.*?)</button>", html, re.S | re.I):
        t = re.sub(r"<[^>]+>", " ", m)
        t = re.sub(r"\s+", " ", t).strip()
        if t:
            texts.append(t[:40])
    for m in re.findall(r'<input[^>]+type="submit"[^>]*>', html, re.I):
        v = re.search(r'value="([^"]*)"', m, re.I)
        if v:
            texts.append(v.group(1)[:40])
    return texts[:limit]


def has_link_field(fields: list[dict]) -> bool:
    for f in fields:
        blob = (f.get("name", "") + " " + f.get("ph", "")).lower()
        if any(h in blob for h in LINK_HINTS) and f.get("type", "text") in ("text", "url", "search", ""):
            return True
    return False


def has_submit_button(buttons: list[str]) -> bool:
    return any(any(h in b.lower() for h in SUBMIT_HINTS) for b in buttons)


def analyse(url: str, html: str) -> dict:
    """Факты о странице: капча, поля, кнопки, таймеры, тексты отказа."""
    low = html.lower()
    text = text_of(html)
    low_text = text.lower()
    fields = fields_of(html)
    buttons = buttons_of(html)
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    return {
        "url": url,
        "title": (title.group(1).strip()[:110] if title else ""),
        "captcha": [k for k, signs in CAPTCHA_SIGNS.items() if any(re.search(s, low) for s in signs)],
        "blocked": any(re.search(s, low) for s in BLOCK_SIGNS),
        "antibot": any(re.search(s, low) for s in ANTIBOT_SIGNS),
        "fields": fields[:10],
        "fields_n": len(fields),
        "has_link_field": has_link_field(fields),
        "buttons": buttons,
        "has_submit": has_submit_button(buttons),
        "forms_n": len(re.findall(r"<form", html, re.I)),
        # форма может жить в iframe (у части панелей бесплатный виджет вынесен на отдельный домен)
        "iframes": re.findall(r'<iframe[^>]+src="([^"]+)"', html, re.I)[:5],
        "iframes_n": len(re.findall(r"<iframe", html, re.I)),
        "spa": bool(re.search(r'id="root"|id="app"|__next_data__|nuxt|vue', low)),
        "says_free": "бесплат" in low_text or "free" in low,
        "mentions_vk": any(w in low_text for w in ("вконтакте", "vk.com", "вк ")),
        "hold_hints": sorted({w for w in ("не закрывайте", "не закрывать", "дождитесь", "таймер",
                                          "осталось", "очередь", "подождите", "сек", "минут")
                              if w in low_text}),
        "fail_texts": sorted({w for w in ("лимит", "попробуйте позже", "неверная ссылка", "уже был",
                                          "недоступно", "повторите") if w in low_text}),
        "timers": re.findall(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", text)[:6],
        "about": text[:400],
    }


def probe(url: str, timeout: int = 20) -> dict:
    """Загрузить и разобрать страницу одним вызовом."""
    page = fetch(url, timeout=timeout)
    res = analyse(page.get("url") or url, page.get("html") or "")
    res.update({"status": page.get("status"), "sec": page.get("sec"),
                "bytes": page.get("bytes"), "error": page.get("error")})
    return res
