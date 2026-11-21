#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Inspect only public SMMnakrutka HTML/scripts; no browser and no orders.

The output contains sanitized public script paths and allowlisted code words.
Quoted values, query strings, cookies, page contents and tokens are not saved.
This cannot establish whether a later form or order will work.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import network, verifier  # noqa: E402
from tools.ci_progress import GitHubProgress, ProgressError  # noqa: E402
from tools.run_unsent_followup_ci import REPO  # noqa: E402

URL = 'https://smmnakrutka.ru/free-vk-followers'
CHECK_NAME = 'SMMnakrutka public source inspection (no order)'
MARKERS = ('countTimePermission', 'permissionKey', 'sessionToken', 'endtime', 'verify')
SAFE_WORDS = {'GET', 'POST', 'get', 'post', 'json', 'success', 'error', 'rand', 'endtime',
              'verify', 'url_page', 'sessionToken', 'permissionKey', 'service', 'quantity',
              'countTimePermission', 'function', 'fetch', 'ajax', 'data', 'headers', 'form',
              'response', 'request', 'body', 'url', 'method', 'type', 'async', 'await', 'then',
              'return', 'true', 'false', 'setTimeout', 'setInterval', 'setItem', 'getItem',
              'const', 'let', 'var', 'if', 'else', 'catch', 'finally', 'JSON', 'stringify',
              'localStorage', 'window', 'document', 'Math', 'random', 'Date', 'now',
              'querySelector', 'value', 'status', 'ok', 'null', 'length', 'createElement'}


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []
        self.inline = []
        self._active = False
        self._external = False
        self._pieces = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != 'script':
            return
        self._active = True
        attrs = dict(attrs)
        src = attrs.get('src') or ''
        self._external = bool(src)
        self._pieces = []
        if src:
            self.urls.append(src)

    def handle_data(self, data):
        if self._active and not self._external:
            self._pieces.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == 'script' and self._active:
            if not self._external:
                self.inline.append(''.join(self._pieces))
            self._active = False
            self._pieces = []


def public_script_url(value: str) -> dict | None:
    try:
        u = urlsplit(urljoin(URL, value))
        host = (u.hostname or '').lower()
        if u.scheme != 'https' or not host or u.username or u.password or u.port not in (None, 443):
            return None
        # A path is public, but a dynamic segment could contain a token.
        path = re.sub(r'/[^/]{32,}', '/[redacted]', u.path)[:180]
        return {'host': host, 'path': path}
    except ValueError:
        return None


def safe_structure(text: str) -> str:
    """Show only allowlisted words and punctuation, never untrusted literals."""
    tokens = re.findall(r'[A-Za-z_$][A-Za-z0-9_$]*|[0-9]+|===|!==|=>|&&|\|\||[{}\[\]():;.,=+!?<>/|-]', text)
    result = []
    for token in tokens[:200]:
        if token in SAFE_WORDS:
            result.append(token)
        elif token[0].isalpha() or token[0] in '_$':
            result.append('_')
        elif token[0].isdigit():
            result.append('#')
        else:
            result.append(token)
    return ' '.join(result)[:1200]


def public_endpoints(text: str) -> list[dict]:
    """Only explicitly named provider hosts and simple public PHP paths."""
    found = []
    for match in re.findall(r'https://(?:smmbakparse\.ru|smmnakrutka\.ru)/php/[A-Za-z0-9_.-]+\.php', text):
        item = public_script_url(match)
        if item and item not in found:
            found.append(item)
    return found[:20]


def analyze(html: str) -> dict:
    scripts = Scripts()
    scripts.feed(html)
    sources = []
    for value in scripts.urls:
        item = public_script_url(value)
        if item and item not in sources:
            sources.append(item)
    matches = []
    for index, text in enumerate(scripts.inline):
        for marker in MARKERS:
            at = text.find(marker)
            if at < 0:
                continue
            near = text[max(0, at - 500):min(len(text), at + 600)]
            # Publish ONLY fixed, non-secret code words, never arbitrary source
            # fragments or values, even when a string crosses a slice boundary.
            words = [word for word in re.findall(r'[A-Za-z_$][A-Za-z0-9_$]*', near)
                     if word in SAFE_WORDS]
            matches.append({'script_index': index, 'marker': marker,
                            'sha256': hashlib.sha256(text.encode('utf8')).hexdigest(),
                            'nearby_code_words': words[:80], 'nearby_structure': safe_structure(near),
                            'public_endpoints': public_endpoints(text)})
    return {'script_sources': sources[:100], 'inline_script_count': len(scripts.inline),
            'marker_matches': matches[:25]}


def run() -> int:
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_EVENT_NAME') != 'push'
            or os.environ.get('GITHUB_RUN_ATTEMPT') != '1' or os.environ.get('GITHUB_REPOSITORY') != REPO
            or os.environ.get('GITHUB_REF') != 'refs/heads/arena/e372c48b-vkkk'):
        raise RuntimeError('Только read-only диагностика в разрешённой ветке')
    state = {'phase': 'подготовка', 'actions_run_id': os.environ.get('GITHUB_RUN_ID'),
             'orders_sent': 0, 'target_entered': False, 'clicked': False, 'changing_requests': 0}
    publisher = GitHubProgress(name=CHECK_NAME)
    publisher.create(state, title='Публичный HTML без браузера и заявок')
    try:
        response = network.http_request(URL, method='GET', timeout=25)
        final = urlsplit(response.get('url') or '')
        if (not response.get('ok') or response.get('status') != 200 or final.scheme != 'https'
                or final.hostname != 'smmnakrutka.ru' or final.path.rstrip('/') != '/free-vk-followers'):
            raise RuntimeError('Публичная страница недоступна или перенаправлена')
        html = response.get('text') or ''
        if not html or len(html) > 5_000_000:
            raise RuntimeError('Неподходящий объём публичной страницы')
        state.update(analyze(html), phase='завершено')
        publisher.update(state, title='Публичные скрипты изучены без заявки', conclusion='success')
        return 0
    except Exception as exc:
        state.update(phase='остановлено', reason=verifier._safe_detail(str(exc) or type(exc).__name__))
        try:
            publisher.update(state, title='Осмотр остановлен без заявки', conclusion='failure')
        except ProgressError:
            pass
        return 2


if __name__ == '__main__':
    try:
        raise SystemExit(run())
    except Exception as exc:
        print('No-order source inspection blocked:', verifier._safe_detail(str(exc) or type(exc).__name__))
        raise SystemExit(2)
