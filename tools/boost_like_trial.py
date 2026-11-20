#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Narrow network and UI guards for Boost-Like's publicly advertised VK trial.

A free trial is distinct from the paid cart on the same page. The browser is
allowed one validated free-session start, ordinary status/heartbeats, and at
most one completion after the site's own button becomes ready. All other
requests, including paid-order and bonus requests, are blocked. No tokens,
query strings, request bodies or cookies are returned in diagnostic output.

This is an allowlist, not proof that arbitrary third-party JS is safe. It fails
closed if the site changes its page, service slug, API or timer behaviour.
"""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, unquote, urlsplit

HOST = 'boost-like.ru'
SERVICE_SLUG = 'besplatnaya-nakrutka-podpischikov-v-vk'
PAGE_PATH = '/vk/' + SERVICE_SLUG
PAGE_URL = 'https://' + HOST + PAGE_PATH
TARGET = 'https://vk.com/club243396555'
SERVICE_CHECK = '/api/v1/free/services/check/vk/' + SERVICE_SLUG
STATIC = re.compile(r'^/_nuxt/[A-Za-z0-9_.-]+\.(?:js|css|woff2?|ttf|svg|png|webp)$')
BUILD_META = re.compile(r'^/_nuxt/builds/meta/[A-Za-z0-9_-]{1,80}\.json$')
ASSET = re.compile(r'^/(?:images|storage)/[A-Za-z0-9_./-]+\.(?:svg|png|jpe?g|webp|gif|ico|woff2?)$')


class TrialFence:
    """One-use protocol allowlist. It intentionally never logs URLs or bodies."""
    def __init__(self, target: str = TARGET):
        if target != TARGET:
            raise ValueError('Boost trial is scoped to the approved VK community')
        self.target = target
        self.stage = 'loading'
        self.token: str | None = None
        self.start_seen = False
        self.complete_seen = False
        self.input_violation = False
        self.early_complete_blocked = False
        self.allowed = 0
        self.blocked = 0
        self.blocked_paths: list[dict] = []  # method + same-origin path only; no query or body
        self.handler = self._route
        self.raw = None

    @staticmethod
    def _body(data: str | None) -> dict | None:
        def unique_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('duplicate key')
                result[key] = value
            return result
        try:
            obj = json.loads(data or '', object_pairs_hook=unique_pairs)
            return obj if isinstance(obj, dict) else None
        except (ValueError, TypeError):
            return None

    def _allowed(self, method: str, address: str, body: str | None) -> bool:
        try:
            u = urlsplit(address)
            if (u.scheme != 'https' or u.hostname != HOST or u.username is not None
                    or u.password is not None or u.port not in (None, 443) or u.fragment):
                return False
            path, query = u.path, u.query
            if any(segment in ('.', '..') for segment in path.split('/')):
                return False
            if method in ('GET', 'HEAD'):
                if not query and (path in (PAGE_PATH, '/favicon.ico', '/api/v1/website',
                                           '/api/v1/platforms', SERVICE_CHECK,
                                           '/api/v1/free/check')
                                  or STATIC.fullmatch(path) or BUILD_META.fullmatch(path) or ASSET.fullmatch(path)):
                    return True
                if path == '/api/v1/free/activity' and parse_qs(query, keep_blank_values=True) == {'limit': ['5']}:
                    return True
                if (path == '/api/v1/free/status' and self.token and
                        self.stage in ('session', 'complete_armed', 'done') and
                        parse_qs(query, keep_blank_values=True) == {'token': [self.token]}):
                    return True
                return False
            if method != 'POST' or query:
                return False
            payload = self._body(body)
            if (path == '/api/v1/free/start' and self.stage == 'start_armed'
                    and not self.start_seen and payload == {'link': self.target, 'service_slug': SERVICE_SLUG}):
                self.start_seen = True
                self.stage = 'session'
                return True
            if (path == '/api/v1/free/heartbeat' and self.stage in ('session', 'complete_armed')
                    and self.token and payload == {'token': self.token}):
                return True
            if (path == '/api/v1/free/complete' and self.stage == 'complete_armed'
                    and self.token and not self.complete_seen and payload == {'token': self.token}):
                self.complete_seen = True
                self.stage = 'done'
                return True
            if path == '/api/v1/free/complete' and self.stage in ('input', 'start_armed', 'session'):
                self.early_complete_blocked = True
            return False
        except (ValueError, TypeError):
            return False

    def _route(self, route) -> None:
        request = route.request
        address, method = request.url, request.method.upper()
        try:
            body = request.post_data
        except Exception:
            body = None
        if self._allowed(method, address, body):
            self.allowed += 1
            route.continue_()
            return
        self.blocked += 1
        try:
            u = urlsplit(address)
            if u.hostname == HOST and len(self.blocked_paths) < 30:
                path = re.sub(r'/[^/]{40,}', '/[redacted]', u.path)[:160]
                self.blocked_paths.append({'method': method, 'path': path})
        except ValueError:
            pass
        if self.stage == 'input':
            # Any unexpected changing request or any GET containing the target
            # while filling is a reason not to proceed to the explicit click.
            decoded = address
            for _ in range(2):
                decoded = unquote(decoded)
            if method not in ('GET', 'HEAD') or 'club243396555' in decoded.lower():
                self.input_violation = True
        route.abort()

    def attach(self, raw) -> None:
        self.raw = raw
        raw.add_init_script("""(() => {
          const no = function(){throw new Error('disabled for guarded trial')};
          Object.defineProperty(window,'WebSocket',{value:no,configurable:false});
          Object.defineProperty(window,'EventSource',{value:no,configurable:false});
          if (window.WebTransport) Object.defineProperty(window,'WebTransport',{value:no,configurable:false});
          if (window.RTCPeerConnection) Object.defineProperty(window,'RTCPeerConnection',{value:no,configurable:false});
          Object.defineProperty(navigator,'sendBeacon',{value:()=>false,configurable:false});
        })()""")
        raw.context.route('**/*', self.handler)
        try:
            # Page-level routes have priority; cover both the page and popups.
            raw.route('**/*', self.handler)
        except Exception:
            raw.context.unroute('**/*', self.handler)
            raise

    def begin_input(self) -> None:
        if self.stage != 'loading':
            raise RuntimeError('Unexpected trial stage')
        self.stage = 'input'

    def arm_start(self) -> None:
        if self.stage != 'input' or self.input_violation or self.start_seen:
            raise RuntimeError('Start cannot be safely armed')
        self.stage = 'start_armed'

    def set_token(self, token: object) -> None:
        if (self.stage != 'session' or not self.start_seen or not isinstance(token, str)
                or not 1 <= len(token) <= 1024 or self.token is not None):
            raise RuntimeError('Free session is not confirmed')
        self.token = token  # memory only; never put it into a snapshot or log

    def arm_complete(self) -> None:
        if (self.stage != 'session' or not self.start_seen or not self.token
                or self.complete_seen or self.early_complete_blocked):
            raise RuntimeError('Completion cannot be safely armed')
        self.stage = 'complete_armed'

    def close(self) -> None:
        if self.raw is not None:
            try:
                self.raw.unroute('**/*', self.handler)
            finally:
                self.raw.context.unroute('**/*', self.handler)
            self.raw = None
        self.token = None


INITIAL_STATE = r'''(expected) => {
  const vis = e => { const r=e.getBoundingClientRect(), s=getComputedStyle(e);
    return r.width>4 && r.height>4 && s.visibility!=='hidden' && s.display!=='none'; };
  const cards=[...document.querySelectorAll('.free-form')].filter(vis);
  if(cards.length!==1) return {valid:false, reason:'free_card_count'};
  const card=cards[0], text=(card.innerText||'').replace(/\s+/g,' ').trim();
  const fields=[...card.querySelectorAll('input,textarea,select')].filter(vis);
  const buttons=[...card.querySelectorAll('button.free-form__submit')].filter(vis);
  const match=text.match(/осталось\s+(\d+)\s+попыт/i);
  const cap=[...card.querySelectorAll('iframe[src*="captcha"],iframe[src*="turnstile"],.cf-turnstile,.g-recaptcha,.h-captcha')].some(vis);
  const f=fields[0], b=buttons[0];
  const correct=fields.length===1 && f.tagName==='INPUT' && f.type==='url' &&
    f.classList.contains('free-form__input') && !f.disabled && !f.readOnly &&
    !f.closest('form') && String(f.value||'').trim()===expected;
  const ready=buttons.length===1 && !b.disabled && b.getAttribute('aria-disabled')!=='true' &&
    !b.closest('form') && !b.closest('a[href]') && (b.innerText||'').trim().toLowerCase()==='получить';
  const valid=!!(correct && ready && /бесплатно/i.test(text) && /подписчик/i.test(text) &&
    /(?:\bvk\b|вк)/i.test(text) && /\+\s*10\b/.test(text) && match && Number(match[1])>0 && !cap);
  return {valid, attempts_left:match ? Number(match[1]) : null, field_count:fields.length,
    button_count:buttons.length, field_matches:!!correct, button_ready:!!ready, captcha:cap};
}'''

SESSION_STATE = r'''() => {
  const vis = e => {const r=e.getBoundingClientRect(), s=getComputedStyle(e);
    return r.width>4 && r.height>4 && s.visibility!=='hidden' && s.display!=='none';};
  const cards=[...document.querySelectorAll('.free-form')].filter(vis);
  if(cards.length!==1) return {stage:'other', error:''};
  const card=cards[0], results=[...card.querySelectorAll('.free-result')].filter(vis);
  if(results.length===1) return {stage:'result', text:(results[0].innerText||'').replace(/\s+/g,' ').trim().slice(0,250)};
  const timers=[...card.querySelectorAll('.free-session__timer-value')].filter(vis);
  const buttons=[...card.querySelectorAll('button.free-session__complete-btn')].filter(vis);
  if(timers.length!==1 || buttons.length!==1) {
    const errors=[...card.querySelectorAll('.free-form__error')].filter(vis);
    return {stage:'other', error:errors.length===1 ? (errors[0].innerText||'').trim().slice(0,200) : ''};
  }
  const b=buttons[0], text=(b.innerText||'').trim();
  return {stage:'session', timer:(timers[0].innerText||'').trim().slice(0,30),
    ready:!b.disabled && b.getAttribute('aria-disabled')!=='true' && !b.closest('form') &&
      !b.closest('a[href]') && text.toLowerCase()==='получить', button_text:text.slice(0,40)};
}'''


def initial_state(raw, expected: str = '') -> dict:
    if raw.url != PAGE_URL:
        return {'valid': False, 'reason': 'unexpected_page'}
    return raw.evaluate(INITIAL_STATE, expected)


def session_state(raw) -> dict:
    return raw.evaluate(SESSION_STATE)
