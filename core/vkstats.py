# -*- coding: utf-8 -*-
"""Актуальные счётчики ВК для поля «уже есть» в целях.

Что умеет и чем:
  * подписчики сообщества  — публичный виджет ВК или VK API (delivery.read_vk_count);
  * лайки/просмотры поста  — мобильная страница поста (m.vk.ru) в браузере:
    анонимный HTTP-запрос получает только оболочку приложения, а браузер видит счётчики;
  * друзья/прослушивания   — публичного счётчика нет (None).

Модуль только ЧИТАЕТ публичные числа. Логина и пароля от ВК не требуется.
"""
from __future__ import annotations

import re
import time
import urllib.parse

from core import delivery

_POST_RE = re.compile(r"(?:wall|video)(-?\d+_\d+)")
_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 60.0


def normalize_post_url(url: str) -> str | None:
    """Любую ссылку vk.com/vk.ru/m.… на пост привести к https://m.vk.ru/wall-…."""
    m = _POST_RE.search(urllib.parse.urlsplit(str(url)).path or "")
    if not m:
        return None
    return "https://m.vk.ru/" + m.group(0)


def group_followers(url: str) -> int | None:
    """Точные подписчики сообщества ВК или публичного Telegram-канала. None — ссылка не та или сбой."""
    try:
        return int(delivery.read_target_count({"url": url}, "followers", allow_resolve=True))
    except Exception:
        return None


POST_COUNT_JS = r"""
(() => {
  const num = (s) => {
    if (!s) return null;
    const m = String(s).match(/(\d[\d\s\u00a0\u202f]{0,11})/);
    return m ? parseInt(m[1].replace(/[\s\u00a0\u202f]/g, ""), 10) : null;
  };
  const out = {likes: null, views: null, reposts: null, comments: null};
  const likeWrap = document.querySelector('[class*="like_wrap"], [class*="LikeWrap"]');
  if (likeWrap && /\bis_empty\b/.test(likeWrap.className || "")) out.likes = 0;
  if (out.likes === null) {
    const c = document.querySelector('[class*="Reaction"] [class*="count"], [class*="like"] [class*="count"]');
    out.likes = num(c ? c.textContent : null);
  }
  if (out.likes === null) {
    const b = document.querySelector('[class*="PostBottomButtonReaction"]');
    if (b) {
      const aria = b.getAttribute("aria-label") || "";
      out.likes = /\d/.test(aria) ? num(aria) : null;
    }
  }
  const viewsEl = document.querySelector('[class*="views"]:not([class*="review"]), [class*="Views"]');
  out.views = num(viewsEl ? viewsEl.textContent : null);
  if (out.views === null) {
    const m = (document.body.innerText || "").match(/(\d[\d\s\u00a0\u202f]{0,11})\s*просмотр/);
    out.views = m ? parseInt(m[1].replace(/[\s\u00a0\u202f]/g, ""), 10) : null;
  }
  const share = document.querySelector('[class*="share"] [class*="count"], [class*="Share"] [class*="count"]');
  out.reposts = num(share ? share.textContent : null);
  const comm = document.querySelector('[class*="comment"] [class*="count"], [class*="Comment"] [class*="count"]');
  out.comments = num(comm ? comm.textContent : null);
  return JSON.stringify(out);
})()
"""


def post_stats(url: str, *, prefer_backend: str | None = None) -> dict:
    """Лайки/просмотры (и если повезёт — репосты/комментарии) со страницы поста.

    Возвращает только то, что реально увидели; чего не увидели — None.
    """
    from core import stealth                       # тяжёлый импорт — только по делу
    murl = normalize_post_url(url)
    if not murl:
        return {}
    try:
        with stealth.open_page(headless=True, prefer=prefer_backend or "patchright") as (_, p):
            p.goto(murl, wait=9000)
            p.wait(5000)
            raw = p.js(POST_COUNT_JS) or "{}"
            data = json_loads(raw)
    except Exception:
        return {}
    return {k: int(v) for k, v in (data or {}).items() if isinstance(v, int) and v >= 0}


def json_loads(raw: str) -> dict:
    import json
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {}


def snapshot(url: str, *, prefer_backend: str | None = None) -> dict:
    """Все счётчики, которые удалось определить для ссылки (с кэшем на минуту)."""
    key = str(url).strip()
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return dict(hit[1])
    out: dict = {}
    followers = group_followers(url)
    if followers is not None:
        out["followers"] = followers
    stats = post_stats(url, prefer_backend=prefer_backend)
    out.update(stats)
    _CACHE[key] = (now, dict(out))
    return out


def count_for(url: str, metric: str, *, prefer_backend: str | None = None) -> int | None:
    """Актуальное значение метрики для «уже есть». None — определить не удалось."""
    metric = (metric or "").strip()
    if metric in ("followers",):
        return group_followers(url)
    if metric in ("friends", "plays"):
        return None                       # публичного счётчика нет
    stats = post_stats(url, prefer_backend=prefer_backend)
    if metric in stats:
        return stats[metric]
    return None
