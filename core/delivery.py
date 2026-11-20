# -*- coding: utf-8 -*-
"""Последовательные заказы: бронирование метрики и проверка фактического прироста.

Поддерживает точное чтение публичных счётчиков подписчиков:
  * сообщества ВКонтакте (vk.ru/club..., vk.com/public..., event..., а также короткие имена);
  * публичные Telegram-каналы и группы (https://t.me/<канал>, https://t.me/s/<канал>, @канал).

Неподтверждённый заказ в очереди удерживает блокировку даже после перезапуска.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from core import db, network

ACTIVE = ('reserved', 'submitting', 'waiting', 'uncertain')
POLL_SECONDS = 60

VK_HOSTS = ('vk.com', 'vk.ru', 'm.vk.com', 'm.vk.ru')
TG_HOSTS = ('t.me', 'telegram.me')
_TG_RESERVED = {'joinchat', 'addstickers', 'addemoji', 'proxy', 'socks', 'share', 'iv', 'login'}
_SCREEN_CACHE: dict[str, str] = {}


class CounterError(ValueError):
    pass


def normalize_channel_input(raw_url: str) -> tuple[str, str, str]:
    """Определяет (platform, kind, normalized_url) по пользовательскому вводу ссылки/@юзернейма."""
    s = (raw_url or '').strip()
    if not s:
        raise ValueError('Укажите ссылку на канал или сообщество')
    if s.startswith('@') and re.fullmatch(r'@[A-Za-z0-9_]{4,64}', s):
        s = 'https://t.me/' + s[1:]
    elif not re.match(r'^https?://', s, re.I):
        low = s.lower()
        if low.startswith(('vk.com/', 'vk.ru/', 'm.vk.com/', 'm.vk.ru/', 't.me/', 'telegram.me/')):
            s = 'https://' + s
    try:
        sp = urllib.parse.urlsplit(s)
        host = (sp.hostname or '').lower()
        if (sp.scheme.lower() not in ('http', 'https') or sp.username or sp.password
                or sp.port not in (None, 80, 443) or any(c.isspace() for c in s) or len(s) > 500):
            raise ValueError('Некорректный адрес канала')
    except ValueError as exc:
        raise ValueError('Некорректный адрес канала') from exc
    parts = [p for p in sp.path.strip('/').split('/') if p]
    if host in TG_HOSTS:
        if len(parts) == 2 and parts[0].lower() == 's':
            slug = parts[1]
        elif len(parts) == 1:
            slug = parts[0]
        else:
            raise ValueError('Укажите публичный канал Telegram, а не отдельный пост')
        if slug.startswith('+') or slug.lower() in _TG_RESERVED:
            raise ValueError('Приватная ссылка Telegram не имеет публичного счётчика подписчиков')
        if not re.fullmatch(r'[A-Za-z0-9_]{4,64}', slug):
            raise ValueError('Некорректное имя Telegram-канала')
        return 'telegram', 'community', f'https://t.me/{slug}'
    if host in VK_HOSTS:
        if len(parts) != 1 or not re.fullmatch(r'[A-Za-z0-9_.]{1,100}', parts[0]):
            raise ValueError('Укажите ссылку на сообщество ВК, а не отдельную страницу или запись')
        seg = parts[0].lower()
        kind = ('person' if re.fullmatch(r'id[0-9]+', seg) else
                'post' if seg.startswith(('wall', 'photo', 'video', 'clip')) else 'community')
        return 'vk', kind, f'https://{host}/{seg}'
    raise ValueError('Поддерживаются сообщества ВК (vk.ru/club...) и публичные Telegram-каналы (t.me/...)')


def _resolve_vk_screen_name(screen_name: str, proxy: str | None = None) -> str:
    """Определяет числовой ID группы ВК по короткому адресу (через API или публичную страницу)."""
    slug = screen_name.strip().lower()
    if slug in _SCREEN_CACHE:
        return _SCREEN_CACHE[slug]
    token = os.environ.get('VKFREE_VK_TOKEN', '').strip()
    if token:
        data = urllib.parse.urlencode({
            'group_id': slug, 'fields': 'members_count', 'v': '5.199', 'access_token': token
        }).encode()
        res = network.http_request('https://api.vk.com/method/groups.getById',
                                   method='POST', data=data,
                                   headers={'Content-Type': 'application/x-www-form-urlencoded'},
                                   proxy=proxy, timeout=12)
        if res.get('ok'):
            try:
                obj = json.loads(res['text'])
                resp = obj.get('response')
                if isinstance(resp, dict):
                    resp = resp.get('groups') or resp.get('items')
                if (isinstance(resp, list) and len(resp) == 1 and isinstance(resp[0], dict)
                        and str(resp[0].get('screen_name', '')).lower() == slug
                        and str(resp[0].get('id', '')).isdigit()):
                    gid = str(resp[0]['id'])
                    _SCREEN_CACHE[slug] = gid
                    return gid
            except Exception:
                pass
    for base in ('https://vk.com/', 'https://vk.ru/', 'https://m.vk.ru/'):
        res = network.http_request(base + urllib.parse.quote(slug), proxy=proxy, timeout=12)
        if not res.get('ok'):
            continue
        final = urllib.parse.urlsplit(str(res.get('url') or ''))
        if final.hostname not in VK_HOSTS:
            continue
        # Надёжный вариант: VK перенаправил короткое имя на числовой адрес.
        m = re.fullmatch(r'/(?:club|public|event)([0-9]{1,15})/?', final.path, re.I)
        if m and m.group(1).lstrip('0'):
            gid = m.group(1).lstrip('0')
            _SCREEN_CACHE[slug] = gid
            return gid
        # Считаем только если страница короткого имени явно содержит один oid группы.
        html = res.get('text') or ''
        ids = {x.lstrip('0') for x in re.findall(r'\boid\s*:\s*-([0-9]{1,15})\b', html) if x.lstrip('0')}
        if final.path.strip('/').lower() == slug and len(ids) == 1:
            gid = ids.pop()
            _SCREEN_CACHE[slug] = gid
            return gid
    raise CounterError('для проверки укажите числовую ссылку сообщества ВК: vk.ru/club… или public… '
                       '(короткое имя не удалось разрешить в ID группы)')


def identity(target: dict, metric: str, *, allow_resolve: bool = False, proxy: str | None = None) -> tuple[str, str]:
    """Нормализует целевой объект в (уникальный_ключ, идентификатор) для ВК и Telegram."""
    raw_url = str(target.get('url') or '').strip()
    url = urllib.parse.urlsplit(raw_url)
    host = (url.hostname or '').lower()
    platform = (target.get('platform') or '').strip().lower()
    if not platform:
        if host in VK_HOSTS:
            platform = 'vk'
        elif host in TG_HOSTS:
            platform = 'telegram'

    if metric != 'followers':
        raise CounterError('автоматический счётчик прироста доступен только для подписчиков (followers)')

    if platform == 'telegram' and host in TG_HOSTS:
        parts = [p for p in url.path.strip('/').split('/') if p]
        if parts and parts[0].lower() == 's' and len(parts) >= 2:
            slug = parts[1]
        elif parts:
            slug = parts[0]
        else:
            raise CounterError('в ссылке Telegram не указан канал')
        if slug.startswith('+') or slug.lower() in _TG_RESERVED:
            raise CounterError('приватная ссылка Telegram не имеет публичного счётчика подписчиков')
        if not re.fullmatch(r'[A-Za-z0-9_]{4,64}', slug):
            raise CounterError('некорректное имя публичного Telegram-канала')
        return 'tg:channel:' + slug.lower(), slug

    if platform != 'vk' or host not in VK_HOSTS:
        raise CounterError('счётчик доступен только для подписчиков сообщества ВК или публичного Telegram-канала')

    segment = url.path.strip('/').split('/')[0].lower() if url.path.strip('/') else ''
    match = re.fullmatch(r'(?:club|public|event)([0-9]+)', segment)
    if match:
        gid = match.group(1).lstrip('0') or '0'
        if gid == '0':
            raise CounterError('некорректный номер сообщества')
        return 'vk:group:' + gid, gid

    if allow_resolve and segment and not re.fullmatch(r'id[0-9]+', segment) and not segment.startswith(('wall', 'photo', 'video')):
        gid = _resolve_vk_screen_name(segment, proxy=proxy)
        return 'vk:group:' + gid, gid

    raise CounterError('для проверки укажите числовую ссылку сообщества ВК: vk.ru/club… или public…')


class _CountParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.matches: list[str] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if self.depth:
            self.depth += 1
        elif dict(attrs).get('id') == 'members_count' and tag in ('a', 'div', 'span'):
            self.depth = 1
            self.parts = []

    def handle_endtag(self, tag):
        if self.depth:
            self.depth -= 1
            if not self.depth:
                self.matches.append(''.join(self.parts))

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)


def parse_widget_count(html: str, group_id: str) -> int:
    """Селектор виджета — #members_count; ID и число должны быть однозначными."""
    ids = set(re.findall(r'\boid\s*:\s*-([0-9]+)\s*(?=[,};<])', html))
    if ids != {group_id}:
        raise CounterError('виджет ВК не подтвердил идентификатор сообщества')
    parser = _CountParser()
    parser.feed(html)
    if len(parser.matches) != 1:
        # Любой похожий текст вне счётчика может относиться к другому блоку страницы.
        raise CounterError('виджет ВК не вернул однозначный счётчик подписчиков')
    text = parser.matches[0].strip()
    if re.fullmatch(r'(?:нет|пока\s+нет)[\s\u00a0\u202f]+(?:подписчиков|участников)|no[\s\u00a0\u202f]+(?:followers|members|subscribers)', text, re.I):
        return 0
    # Строгая группировка тысяч; округлённые 1,2 тыс. и 1.2K не принимаются.
    m = re.fullmatch(r'([0-9]+|[0-9]{1,3}(?:[ ,\s\u00a0\u202f][0-9]{3})+)'
                     r'[\s\u00a0\u202f]+(подписчик(?:а|ов)?|участник(?:а|ов)?|followers?|members?|subscribers?)', text, re.I)
    if not m or (',' in m.group(1) and not re.fullmatch(r'followers?|members?|subscribers?', m.group(2), re.I)):
        raise CounterError('виджет ВК не вернул точное неокруглённое число подписчиков')
    digits = re.sub(r'[\s,\u00a0\u202f]+', '', m.group(1))
    if len(digits) > 19 or int(digits) > 2**63 - 1:
        raise CounterError('число подписчиков виджета ВК вне допустимого диапазона')
    return int(digits)


class _VKInlineScripts(HTMLParser):
    """Собирает только содержимое inline script, не текст страницы или атрибутов."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts: list[str] = []
        self._parts: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == 'script' and not dict(attrs).get('src'):
            self._parts = []

    def handle_data(self, data):
        if self._parts is not None:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self._parts is not None:
            self.scripts.append(''.join(self._parts))
            self._parts = None


def _unique_json_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError('повторяющееся поле JSON')
        obj[key] = value
    return obj


def _reject_json_constant(_value):
    raise ValueError('не JSON')


def _vk_page_prefetch(html: str, group_id: str) -> tuple[int, str]:
    """Только JSON предзагрузки groups.getById мобильной страницы, без исполнения JS.

    VK помещает на страницу window.cur.apiPrefetchCache. Не ищем members_count
    глобальным regex: похожие поля могут относиться к другому сообществу.
    """
    parser = _VKInlineScripts()
    parser.feed(html or '')
    pattern = re.compile(r'\A\s*window\.cur\.apiPrefetchCache\s*=\s*')
    assignments = [(s, m) for s in parser.scripts if (m := pattern.match(s))]
    if len(assignments) != 1:
        raise CounterError('страница ВК не вернула однозначную предзагрузку сообщества')
    script, match = assignments[0]
    try:
        decoder = json.JSONDecoder(object_pairs_hook=_unique_json_object, parse_constant=_reject_json_constant)
        payload, end = decoder.raw_decode(script, match.end())
    except (ValueError, RecursionError) as exc:
        raise CounterError('страница ВК вернула некорректные данные сообщества') from exc
    if not re.fullmatch(r'\s*;\s*(?:window\.apiPrefetchReadyResolve\s*\(\s*\)\s*;?\s*)?', script[end:]):
        raise CounterError('страница ВК вернула неоднозначные данные сообщества')
    if not isinstance(payload, list):
        raise CounterError('страница ВК не вернула список данных сообщества')
    entries = []
    for item in payload:
        if not isinstance(item, dict) or item.get('method') != 'groups.getById':
            continue
        request = item.get('request')
        requested = request.get('group_ids') if isinstance(request, dict) else None
        if type(requested) in (str, int) and str(requested) == group_id:
            entries.append(item)
    if len(entries) != 1:
        raise CounterError('страница ВК не вернула однозначный запрос этого сообщества')
    entry = entries[0]
    request, response = entry['request'], entry.get('response')
    if not isinstance(response, dict):
        raise CounterError('страница ВК не подтвердила ответ сообщества')
    if (not isinstance(request.get('fields'), str)
            or 'members_count' not in {x.strip() for x in request['fields'].split(',')}):
        raise CounterError('страница ВК не подтвердила поле счётчика')
    groups = response.get('groups')
    if not isinstance(groups, list) or len(groups) != 1 or not isinstance(groups[0], dict):
        raise CounterError('страница ВК не вернула данные одного сообщества')
    group = groups[0]
    if type(group.get('id')) is not int or str(group['id']) != group_id:
        raise CounterError('страница ВК вернула другое сообщество')
    value = group.get('members_count')
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise CounterError('страница ВК не вернула точное число участников')
    name = group.get('screen_name')
    return value, name.lower() if isinstance(name, str) else ''


def parse_vk_page_count(html: str, group_id: str) -> int:
    """Точное значение и совпадающий ID из предзагруженного ответа мобильной страницы."""
    return _vk_page_prefetch(html, group_id)[0]


class _TgExtraParser(HTMLParser):
    """Извлекает <div class="tgme_page_extra"> и <div class="tgme_page_title"> со страницы t.me/<канал>."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._mode = ''
        self._depth = 0
        self._buf: list[str] = []
        self.extras: list[str] = []
        self.titles: list[str] = []

    def handle_starttag(self, tag, attrs):
        if self._depth:
            self._depth += 1
            return
        cls = set((dict(attrs).get('class') or '').split())
        if 'tgme_page_extra' in cls:
            self._mode = 'extra'
            self._depth = 1
            self._buf = []
        elif 'tgme_page_title' in cls:
            self._mode = 'title'
            self._depth = 1
            self._buf = []

    def handle_endtag(self, tag):
        if self._depth:
            self._depth -= 1
            if not self._depth:
                val = ''.join(self._buf).strip()
                if self._mode == 'extra':
                    self.extras.append(val)
                elif self._mode == 'title':
                    self.titles.append(val)
                self._mode = ''

    def handle_data(self, data):
        if self._depth:
            self._buf.append(data)


def parse_telegram_count(html: str, slug: str) -> int:
    """Точное число подписчиков публичного Telegram-канала или супергруппы со страницы t.me/<slug>."""
    p = _TgExtraParser()
    p.feed(html or '')
    if not p.titles:
        raise CounterError(f'Telegram-канал @{slug} не найден или страница не загрузилась')
    if len(p.extras) != 1:
        raise CounterError(f'Telegram не вернул однозначный счётчик для @{slug}')
    txt = p.extras[0].strip()
    if re.fullmatch(r'(?:no\s+(?:subscribers?|members?)|нет\s+(?:подписчиков|участников))', txt, re.I):
        return 0
    # Допускаем строго группированные разряды, включая английскую запятую, и необязательный online.
    m = re.fullmatch(
        r'([0-9]+|[0-9]{1,3}(?:[ ,\s\u00a0\u202f][0-9]{3})+)[\s\u00a0\u202f]+'
        r'(?:subscribers?|members?|подписчик(?:а|ов)?|участник(?:а|ов)?)'
        r'(?:\s*,\s*[0-9][0-9\s,\u00a0\u202f]*\s+online)?', txt, re.I)
    if m:
        return int(re.sub(r'[\s,\u00a0\u202f]+', '', m.group(1)))
    raise CounterError(f'Telegram не показал точное число подписчиков для @{slug}: «{txt[:80]}»')


def read_vk_widget(group_id: str, proxy: str | None = None) -> int:
    if not isinstance(group_id, str) or not re.fullmatch(r'[1-9][0-9]{0,14}', group_id):
        raise CounterError('некорректный номер сообщества ВК')
    params = urllib.parse.urlencode({'app': '0', 'width': '300px', 'mode': '0',
                                     'gid': group_id, '_': str(time.time_ns())})
    last_err: Exception | None = None
    for host in ('vk.com', 'vk.ru'):
        url = f'https://{host}/widget_community.php?' + params
        res = network.http_request(url, proxy=proxy, timeout=12,
                                   headers={'Cache-Control': 'no-cache, no-store, max-age=0',
                                            'Pragma': 'no-cache'})
        if not res.get('ok') or res.get('status') != 200:
            last_err = CounterError(res.get('error') or f'виджет ВК ({host}) вернул HTTP {res.get("status")}')
            continue
        try:
            final = urllib.parse.urlsplit(str(res.get('url') or ''))
            if (final.scheme.lower() != 'https' or (final.hostname or '').lower() not in ('vk.com', 'vk.ru')
                    or final.username or final.password or final.port not in (None, 443)
                    or final.path != '/widget_community.php'):
                raise CounterError('виджет ВК перенаправил на другой сайт или адрес')
            return parse_widget_count(res.get('text') or '', group_id)
        except (CounterError, ValueError) as exc:
            last_err = exc  # другая публичная точка может вернуть полноценный виджет
    raise CounterError(f'виджет ВК недоступен: {last_err}')


def read_vk_page(group_id: str, proxy: str | None = None) -> int:
    """Резерв: запрашиваем мобильную страницу заново, не используем приблизительный текст или снимок."""
    if not isinstance(group_id, str) or not re.fullmatch(r'[1-9][0-9]{0,14}', group_id):
        raise CounterError('некорректный номер сообщества ВК')
    last_err: Exception | None = None
    for host in ('m.vk.ru', 'm.vk.com'):
        url = f'https://{host}/club{group_id}?' + urllib.parse.urlencode({'_': str(time.time_ns())})
        res = network.http_request(url, proxy=proxy, timeout=12,
                                   headers={'Cache-Control': 'no-cache, no-store, max-age=0',
                                            'Pragma': 'no-cache'})
        if not res.get('ok') or res.get('status') != 200:
            last_err = CounterError(res.get('error') or f'страница ВК ({host}) вернула HTTP {res.get("status")}')
            continue
        try:
            final = urllib.parse.urlsplit(str(res.get('url') or ''))
            path = re.fullmatch(r'/([A-Za-z0-9_.]{1,100})/?', final.path)
            if (final.scheme.lower() != 'https' or (final.hostname or '').lower() not in ('m.vk.ru', 'm.vk.com')
                    or final.username or final.password or final.port not in (None, 443) or not path):
                raise CounterError('страница ВК перенаправила на другой сайт или адрес')
            value, screen_name = _vk_page_prefetch(res.get('text') or '', group_id)
            segment = path.group(1).lower()
            numeric = re.fullmatch(r'(?:club|public|event)([0-9]+)', segment)
            if numeric:
                correct_page = numeric.group(1).lstrip('0') == group_id
            else:
                correct_page = (segment == screen_name and not re.fullmatch(r'id[0-9]+', segment)
                                and segment not in {'login', 'join', 'feed', 'away.php'})
            if not correct_page:
                raise CounterError('страница ВК перенаправила на другое сообщество')
            return value
        except (CounterError, ValueError) as exc:
            last_err = exc
    raise CounterError(f'страница ВК недоступна: {last_err}')


def _read_vk_public(group_id: str, proxy: str | None, source_out: list[str] | None) -> int:
    try:
        value = read_vk_widget(group_id, proxy=proxy)
        source = 'виджет VK'
    except CounterError as widget_error:
        try:
            value = read_vk_page(group_id, proxy=proxy)
        except CounterError as page_error:
            raise CounterError(f'{widget_error}; резервная {page_error}') from page_error
        source = 'мобильная страница VK'
    if source_out is not None:
        source_out.append(source)
    return value


def read_vk_count(target: dict, metric: str, *, proxy: str | None = None, allow_resolve: bool = False,
                  source_out: list[str] | None = None) -> int:
    """Точный счётчик: VK API → виджет → предзагрузка мобильной страницы."""
    _key, group_id = identity(target, metric, allow_resolve=allow_resolve, proxy=proxy)
    token = os.environ.get('VKFREE_VK_TOKEN', '').strip()
    if not token:
        return _read_vk_public(group_id, proxy, source_out)
    try:
        data = urllib.parse.urlencode({'group_id': group_id, 'fields': 'members_count',
                                      'v': '5.199', 'access_token': token}).encode()
        res = network.http_request('https://api.vk.com/method/groups.getById',
                                   method='POST', data=data,
                                   headers={'Content-Type': 'application/x-www-form-urlencoded'},
                                   proxy=proxy, timeout=12)
        if not res.get('ok'):
            raise CounterError('VK API недоступен или вернул некорректный ответ')
        try:
            obj = json.loads(res.get('text') or '{}')
        except (TypeError, ValueError) as exc:
            raise CounterError('VK API вернул некорректный JSON') from exc
        if not isinstance(obj, dict):
            raise CounterError('VK API вернул некорректный ответ')
        if 'error' in obj:
            code = obj['error'].get('error_code', '?') if isinstance(obj['error'], dict) else '?'
            raise CounterError(f'VK API отклонил запрос (код {code}); проверьте ключ и права')
        response = obj.get('response')
        if isinstance(response, dict):
            response = response.get('groups') or response.get('items')
        if not isinstance(response, list) or len(response) != 1 or not isinstance(response[0], dict):
            raise CounterError('VK API не вернул данные одного сообщества')
        group = response[0]
        if str(group.get('id', group.get('gid', ''))) != group_id:
            raise CounterError('VK API вернул другое сообщество')
        value = group.get('members_count')
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise CounterError('VK API не вернул точное число участников')
        if source_out is not None:
            source_out.append('VK API')
        return value
    except CounterError as api_error:
        try:
            return _read_vk_public(group_id, proxy, source_out)
        except CounterError as public_error:
            raise CounterError(f'{api_error}; резервные источники: {public_error}') from public_error


def read_telegram_count(slug: str, proxy: str | None = None, *, source_out: list[str] | None = None) -> int:
    """Точное число подписчиков публичного Telegram-канала через Bot API или страницу t.me/<slug>."""
    clean_slug = slug.lstrip('@').strip()
    bot_token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    if bot_token:
        api_url = (f'https://api.telegram.org/bot{bot_token}/getChatMemberCount?'
                   + urllib.parse.urlencode({'chat_id': '@' + clean_slug}))
        res = network.http_request(api_url, proxy=proxy, timeout=12)
        if res.get('ok'):
            try:
                obj = json.loads(res.get('text') or '{}')
                value = obj.get('result')
                if obj.get('ok') and isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    if source_out is not None:
                        source_out.append('Telegram Bot API')
                    return value
            except Exception:
                pass
    url = f'https://t.me/{urllib.parse.quote(clean_slug)}'
    res = network.http_request(url, proxy=proxy, timeout=12)
    if not res.get('ok'):
        raise CounterError(f'страница Telegram недоступна ({res.get("error") or ("HTTP " + str(res.get("status")))})')
    final = urllib.parse.urlsplit(str(res.get('url') or url))
    if (final.hostname or '').lower() not in TG_HOSTS or final.path.strip('/').lower() != clean_slug.lower():
        raise CounterError('Telegram перенаправил на другой канал или сайт')
    count = parse_telegram_count(res.get('text') or '', clean_slug)
    if source_out is not None:
        source_out.append('t.me')
    return count


def read_target_count(target: dict, metric: str = 'followers', *, proxy: str | None = None,
                      allow_resolve: bool = True, source_out: list[str] | None = None) -> int:
    """Точный счётчик; source_out при необходимости получает фактический источник."""
    key, ident = identity(target, metric, allow_resolve=allow_resolve, proxy=proxy)
    if key.startswith('tg:channel:'):
        return read_telegram_count(ident, proxy=proxy, source_out=source_out)
    return read_vk_count(target, metric, proxy=proxy, allow_resolve=allow_resolve, source_out=source_out)


def reserve(conn, task, service, target) -> bool:
    """Вызывается внутри BEGIN IMMEDIATE до любого запуска площадки."""
    metric = service['stype']
    # Уже идущие заказы блокируют запуск даже после выключения режима.
    try:
        key, _ = identity(dict(target), metric)
    except CounterError:
        key = f"target:{target['id']}"
    conflict = conn.execute("SELECT task_id FROM deliveries WHERE target_key=? AND metric=? "
                            "AND state IN ('reserved','submitting','waiting','uncertain')",
                            (key, metric)).fetchone()
    if conflict:
        raise ValueError(f'ждём завершения предыдущего заказа #{conflict[0]} по этой метрике')
    enabled = conn.execute("SELECT value FROM settings WHERE key='full_auto'").fetchone()
    if not enabled or enabled[0] != '1':
        return False
    # Другие активные задачи по той же цели могли начаться до включения нового режима.
    active = conn.execute("SELECT t.id,t.target_id,tg.url,tg.platform FROM tasks t "
                          "JOIN services v ON v.id=t.service_id LEFT JOIN targets tg ON tg.id=t.target_id "
                          "WHERE v.stype=? AND t.id<>? "
                          "AND t.status IN ('running','wait_confirm')",
                          (metric, task['id'])).fetchall()
    for other in active:
        try:
            other_key, _ = identity({'platform': other['platform'], 'url': other['url']}, metric)
        except (CounterError, TypeError, ValueError):
            other_key = f"target:{other['target_id']}"
        if other_key == key or other['target_id'] == target['id']:
            raise ValueError(f'есть незавершённая задача #{other["id"]} по этой метрике')
    key, _ = identity(dict(target), metric)  # fail closed для неподдерживаемой метрики
    expected = int(service['amount_override'] if service['amount_override'] is not None else service['amount'])
    if expected <= 0:
        raise CounterError('площадка не указала положительный объём заказа')
    if task['goal_id']:
        item = conn.execute("SELECT need,have_before FROM goal_items WHERE goal_id=? AND metric=?",
                            (task['goal_id'], metric)).fetchone()
        done = conn.execute("SELECT COALESCE(SUM(t.claimed_units),0) AS n FROM tasks t JOIN services v ON v.id=t.service_id "
                            "WHERE t.goal_id=? AND COALESCE(t.metric,v.stype)=? AND t.status='done'",
                            (task['goal_id'], metric)).fetchone()['n']
        if not item:
            raise CounterError('метрика отсутствует в цели')
        remaining = max(0, int(item['need']) - int(done))
        if expected > remaining:
            raise CounterError(f'осталось {remaining}, а площадка добавляет {expected}; пересчитайте цель без превышения')
    conn.execute("DELETE FROM deliveries WHERE task_id=? AND state NOT IN ('reserved','submitting','waiting','uncertain')", (task['id'],))
    conn.execute("INSERT INTO deliveries(task_id,target_id,target_key,metric,expected,state,created_at) "
                 "VALUES(?,?,?,?,?,'reserved',?)", (task['id'], target['id'], key, metric, expected, int(time.time())))
    return True


def _read_for_row(target_dict: dict, metric: str) -> int:
    if (target_dict.get('platform') or '').lower() == 'telegram':
        return read_target_count(target_dict, metric, allow_resolve=False)
    return read_vk_count(target_dict, metric)


def set_baseline(task_id: int, target: dict, metric: str) -> int:
    count = _read_for_row(target, metric)
    with db.tx() as c:
        changed = c.execute("UPDATE deliveries SET baseline=?, last_count=?, state='submitting', checked_at=? "
                            "WHERE task_id=? AND state='reserved'", (count, count, int(time.time()), task_id)).rowcount
        if not changed:
            raise CounterError('резерв заказа больше не активен')
    return count


def release_before_submit(task_id: int, reason: str) -> None:
    with db.tx() as c:
        c.execute("UPDATE deliveries SET state='released', note=? WHERE task_id=? AND state IN ('reserved','submitting')",
                  (reason[:400], task_id))


def waiting(task_id: int, uncertain: bool = False, note: str = '') -> bool:
    """Площадка приняла заказ или после отправки получен неоднозначный ответ."""
    now = int(time.time())
    with db.tx() as c:
        row = c.execute("SELECT * FROM deliveries WHERE task_id=? AND state='submitting'", (task_id,)).fetchone()
        if not row:
            return False
        state = 'uncertain' if uncertain else 'waiting'
        status = 'delivery_uncertain' if uncertain else 'waiting_delivery'
        detail = ('ответ заказа неясен' if uncertain else 'заказ принят') + \
                 f"; отсчёт {row['baseline']} → {row['baseline'] + row['expected']}"
        if note:
            detail += '; ' + note[:200]
        c.execute("UPDATE deliveries SET state=?, note=?, next_check_at=? WHERE task_id=?",
                  (state, note[:400], now, task_id))
        c.execute("UPDATE tasks SET status=?, note=? WHERE id=?", (status, detail[:500], task_id))
    db.log('info', f'Заказ #{task_id}: {detail}', task_id=task_id)
    return True


def check_one(task_id: int) -> bool:
    """Проверить текущий счётчик. Только подтверждённый заказ завершается автоматически."""
    with db.rd() as c:
        row = c.execute("SELECT d.*,t.url,t.platform FROM deliveries d JOIN targets t ON t.id=d.target_id "
                        "WHERE d.task_id=? AND d.state IN ('waiting','uncertain')", (task_id,)).fetchone()
    if not row:
        return False
    now = int(time.time())
    try:
        current_key, _ = identity(dict(row), row['metric'])
        if current_key != row['target_key']:
            raise CounterError('ссылка изменилась после отправки; проверка остановлена')
        count = _read_for_row(dict(row), row['metric'])
    except CounterError as exc:
        with db.tx() as c:
            c.execute("UPDATE deliveries SET note=?, next_check_at=? WHERE task_id=? AND state IN ('waiting','uncertain')",
                      (str(exc)[:400], now + POLL_SECONDS, task_id))
            c.execute("UPDATE tasks SET note=? WHERE id=? AND status IN ('waiting_delivery','delivery_uncertain')",
                      (f'жду проверку прироста: {exc}'[:500], task_id))
        return False
    threshold_reached = count >= row['baseline'] + row['expected'] and row['state'] == 'waiting'
    completed = False
    with db.tx() as c:
        current = c.execute("SELECT state,confirm_hits FROM deliveries WHERE task_id=?", (task_id,)).fetchone()
        if not current or current['state'] != row['state']:
            return False
        hits = (int(current['confirm_hits']) + 1) if threshold_reached else 0
        c.execute('UPDATE deliveries SET last_count=?, checked_at=?, next_check_at=?, confirm_hits=? WHERE task_id=?',
                  (count, now, now + POLL_SECONDS, hits, task_id))
        if hits >= 2:
            units = max(0, count - row['baseline'])
            changed = c.execute("UPDATE tasks SET status='done', finished_at=?, claimed_units=?, note=? WHERE id=? "
                                "AND status='waiting_delivery'", (now, units, f"прирост по счётчику: {row['baseline']} → {count}", task_id)).rowcount
            if changed:
                c.execute("UPDATE deliveries SET state='verified', note=? WHERE task_id=?", ('прирост подтверждён двумя проверками', task_id))
                completed = True
        if not completed:
            c.execute("UPDATE tasks SET note=? WHERE id=? AND status IN ('waiting_delivery','delivery_uncertain')",
                      (f"{'ответ заказа неясен; ' if row['state']=='uncertain' else ''}счётчик: {count}; ждём {row['baseline'] + row['expected']} (было {row['baseline']})" + (f'; проверка {hits}/2' if hits else ''), task_id))
    if completed:
        db.log('info', f'Заказ #{task_id}: прирост {row["baseline"]} → {count} подтверждён; следующий сайт разрешён', task_id=task_id)
        from core import goals
        goals.on_task_done(task_id)
    return completed


def poll_due() -> None:
    with db.rd() as c:
        rows = c.execute("SELECT task_id FROM deliveries WHERE state IN ('waiting','uncertain') "
                         "AND COALESCE(next_check_at,0)<=? ORDER BY next_check_at LIMIT 10", (int(time.time()),)).fetchall()
    for row in rows:
        try:
            check_one(row['task_id'])
        except Exception as exc:
            db.log('error', f'Проверка заказа #{row["task_id"]}: {type(exc).__name__}', task_id=row['task_id'])


def confirm_accepted(task_id: int) -> None:
    """Ручное подтверждение факта приёма, не подтверждение прироста."""
    with db.tx() as c:
        n = c.execute("UPDATE deliveries SET state='waiting', note='приём заказа подтверждён вручную', "
                      "next_check_at=?, confirm_hits=0 WHERE task_id=? AND state='uncertain' AND baseline IS NOT NULL",
                      (int(time.time()), task_id)).rowcount
        if not n:
            raise ValueError('нет неопределённого заказа с исходным счётчиком')
        c.execute("UPDATE tasks SET status='waiting_delivery', note='приём подтверждён вручную; жду прирост' WHERE id=?", (task_id,))
    db.log('warn', f'Заказ #{task_id}: приём подтверждён пользователем; прирост ещё проверяется', task_id=task_id)


def abandon(task_id: int) -> None:
    """Явное решение пользователя разблокировать очередь без доказательства завершения."""
    with db.tx() as c:
        n = c.execute("UPDATE deliveries SET state='abandoned', note='вручную разблокировано без подтверждения' "
                      "WHERE task_id=? AND state IN ('waiting','uncertain')", (task_id,)).rowcount
        if not n:
            raise ValueError('для задачи нет активного ожидания')
        c.execute("UPDATE tasks SET status='failed', note='разблокировано вручную; заказ может продолжаться' WHERE id=?", (task_id,))
    db.log('warn', f'Заказ #{task_id}: вручную снята блокировка; возможен параллельный прирост', task_id=task_id)
