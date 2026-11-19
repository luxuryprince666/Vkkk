# -*- coding: utf-8 -*-
"""Последовательные заказы: бронирование метрики и проверка фактического прироста.

Неподтверждённый заказ удерживает блокировку даже после перезапуска. Автоматического
освобождения по таймеру нет: заказ с задержкой может продолжиться позже.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from core import db

ACTIVE = ('reserved', 'submitting', 'waiting', 'uncertain')
POLL_SECONDS = 60


class CounterError(ValueError):
    pass


def identity(target: dict, metric: str) -> tuple[str, str]:
    """Нормализуем только однозначно определённые VK-сообщества и подписчиков."""
    url = urllib.parse.urlsplit(str(target['url']))
    host = (url.hostname or '').lower()
    if target['platform'] != 'vk' or metric != 'followers' or host not in ('vk.com', 'vk.ru', 'm.vk.com', 'm.vk.ru'):
        raise CounterError('счётчик доступен только для подписчиков сообщества ВК')
    segment = url.path.strip('/').split('/')[0].lower()
    match = re.fullmatch(r'(?:club|public|event)([0-9]+)', segment)
    if not match:
        raise CounterError('для проверки укажите числовую ссылку сообщества ВК: vk.ru/club… или public…')
    gid = match.group(1).lstrip('0') or '0'
    if gid == '0':
        raise CounterError('некорректный номер сообщества')
    return 'vk:group:' + gid, gid


class _CountParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.matches = []
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if self.depth:
            self.depth += 1
        elif dict(attrs).get('id') == 'members_count' and tag == 'a':
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
    """Принимаем только точное целое число и подтверждённый идентификатор группы."""
    if not re.search(r'\boid\s*:\s*-' + re.escape(group_id) + r'(?!\d)', html):
        raise CounterError('виджет ВК не подтвердил идентификатор сообщества')
    parser = _CountParser()
    parser.feed(html)
    if len(parser.matches) != 1:
        raise CounterError('виджет ВК не вернул однозначный счётчик подписчиков')
    text = parser.matches[0].strip()
    # Не принимаем округлённые значения (1,2 тыс.; 1.2K), и не собираем любые цифры страницы.
    m = re.fullmatch(r'([0-9]+(?:[\s\u00a0\u202f][0-9]{3})*)[\s\u00a0\u202f]+'
                     r'(?:подписчик(?:а|ов)?|участник(?:а|ов)?|followers?|members?)', text, re.I)
    if not m:
        raise CounterError('виджет ВК не вернул точное неокруглённое число подписчиков')
    return int(re.sub(r'\s+', '', m.group(1)))


def _curl_available() -> bool:
    try:
        import curl_cffi  # type: ignore
        return True
    except Exception:
        return False


def read_vk_widget(group_id: str) -> int:
    params = urllib.parse.urlencode({'app': '0', 'width': '300px', 'mode': '0',\
                                     'gid': group_id, '_': str(int(time.time()))})
    url = 'https://vk.com/widget_community.php?' + params
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '\
                           '(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36',
               'Accept-Language': 'ru-RU,ru;q=0.9',
               'Cache-Control': 'no-cache'}
    # Сначала пробуем curl_cffi с отпечатком Chrome — VK реже блокирует такой трафик.
    if _curl_available():
        try:
            from curl_cffi import requests as creq  # type: ignore
            r = creq.get(url, impersonate='chrome', timeout=12, headers=headers)
            if r.status_code != 200:
                raise CounterError(f'виджет ВК вернул HTTP {r.status_code}')
            hostname = urllib.parse.urlsplit(str(r.url)).hostname
            if hostname not in ('vk.com', 'vk.ru'):
                raise CounterError('виджет ВК перенаправил на другой сайт')
            text = r.text
        except CounterError:
            raise
        except Exception as exc:
            text = None
            db.log('warn', f'curl_cffi виджет ВК: {str(exc)[:120]}')
    else:
        text = None

    if text is None:
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                if urllib.parse.urlsplit(response.geturl()).hostname not in ('vk.com', 'vk.ru'):
                    raise CounterError('виджет ВК перенаправил на другой сайт')
                encoding = response.headers.get_content_charset() or 'windows-1251'
                raw = response.read(1024 * 1024)
                text = raw.decode(encoding, errors='strict')
        except CounterError:
            raise
        except Exception as exc:
            raise CounterError('виджет ВК недоступен или вернул некорректный ответ') from exc
    return parse_widget_count(text, group_id)

    return parse_widget_count(text, group_id)


def read_vk_count(target: dict, metric: str) -> int:
    """Точное число участников через API (если есть ключ) или публичный виджет ВК."""
    _key, group_id = identity(target, metric)
    token = os.environ.get('VKFREE_VK_TOKEN', '').strip()
    if not token:
        return read_vk_widget(group_id)
    data = urllib.parse.urlencode({'group_id': group_id, 'fields': 'members_count',
                                  'v': '5.199', 'access_token': token}).encode()
    request = urllib.request.Request('https://api.vk.com/method/groups.getById', data=data,
                                     headers={'User-Agent': 'VKFreeFarm/1.0'})
    try:
        with urllib.request.urlopen(request, timeout=12) as res:
            obj = json.loads(res.read(1024 * 1024).decode('utf-8'))
    except Exception as exc:
        raise CounterError('VK API недоступен или вернул некорректный ответ') from exc
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
    return value


def reserve(conn, task, service, target) -> bool:
    """Вызывается внутри BEGIN IMMEDIATE до любого запуска площадки."""
    metric = service['stype']
    # Уже идущие заказы блокируют запуск даже после выключения режима.
    try:
        key, _ = identity(dict(target), metric)
    except CounterError:
        key = f"target:{target['id']}"
    conflict = conn.execute("SELECT task_id FROM deliveries WHERE target_key=? AND metric=? "
                            "AND state IN ('reserved','submitting')",
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


def set_baseline(task_id: int, target: dict, metric: str) -> int:
    count = read_vk_count(target, metric)
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
        count = read_vk_count(dict(row), row['metric'])
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
