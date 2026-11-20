#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Publish recoverable live state to a dedicated GitHub check run.

The Actions runner limits workflow-command notices to ten per step. A separate
check's output can instead be updated throughout a long-running sequential test.
The short-lived GitHub token is read only from the environment and never logged.
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
import zlib


class ProgressError(RuntimeError):
    pass


def encode_snapshot(data: dict) -> str:
    raw = json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return base64.b64encode(zlib.compress(raw, 9)).decode('ascii')


def decode_snapshot(encoded: str) -> dict:
    return json.loads(zlib.decompress(base64.b64decode(encoded, validate=True)).decode('utf-8'))


class GitHubProgress:
    def __init__(self, *, name: str):
        self.repo = os.environ.get('GITHUB_REPOSITORY', '')
        self.sha = os.environ.get('GITHUB_SHA', '')
        self.token = os.environ.get('VKKK_PROGRESS_TOKEN', '')
        if not re.fullmatch(r'[\w.-]+/[\w.-]+', self.repo) or not re.fullmatch(r'[0-9a-f]{40}', self.sha):
            raise ProgressError('Нет корректного репозитория или SHA для check-run')
        if not self.token:
            raise ProgressError('Нет токена для публикации состояния проверки')
        self.name = name[:100]
        self.id: int | None = None

    def _request(self, method: str, path: str, payload: dict) -> dict:
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        req = urllib.request.Request('https://api.github.com/repos/' + self.repo + path,
                                     data=data, method=method,
                                     headers={'Accept': 'application/vnd.github+json',
                                              'Authorization': 'Bearer ' + self.token,
                                              'X-GitHub-Api-Version': '2022-11-28',
                                              'Content-Type': 'application/json',
                                              'User-Agent': 'vk-sequential-verifier'})
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                body = response.read(1024 * 1024)
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError('not an object')
            return value
        except urllib.error.HTTPError as exc:
            raise ProgressError(f'GitHub check API вернул HTTP {exc.code}') from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise ProgressError('Не удалось обновить GitHub check: ' + type(exc).__name__) from exc

    @staticmethod
    def _output(state: dict, title: str) -> dict:
        encoded = encode_snapshot(state)
        summary = ('Актуальный структурированный снимок ниже. Неизвестное не является нулём; '
                   'рост публичного счётчика сам по себе не доказывает источник.\n\n'
                   'VKKK_SNAPSHOT_ZLIB_BASE64:' + encoded)
        # GitHub limits output.summary to 65535 characters. Never truncate a snapshot.
        if len(summary) > 65000:
            raise ProgressError('Снимок слишком велик для безопасной публикации в GitHub check')
        return {'title': title[:255], 'summary': summary}

    def create(self, state: dict, *, title: str = 'Проверка готовится') -> int:
        obj = self._request('POST', '/check-runs', {
            'name': self.name, 'head_sha': self.sha, 'status': 'in_progress',
            'output': self._output(state, title),
        })
        check_id = obj.get('id')
        if type(check_id) is not int or check_id <= 0:
            raise ProgressError('GitHub не вернул ID check-run')
        self.id = check_id
        return check_id

    def update(self, state: dict, *, title: str = 'Проверка выполняется', conclusion: str | None = None) -> None:
        if self.id is None:
            raise ProgressError('Check-run ещё не создан')
        obj = {'status': 'completed' if conclusion else 'in_progress',
               'output': self._output(state, title)}
        if conclusion:
            if conclusion not in ('success', 'failure', 'neutral', 'cancelled'):
                raise ValueError('Недопустимое завершение check-run')
            obj['conclusion'] = conclusion
        self._request('PATCH', '/check-runs/' + str(self.id), obj)
