#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Реальный последовательный запуск с измерением счётчика; без фиктивных успехов."""
from __future__ import annotations

import argparse
import json
import sys
import time

from core import db, verifier


def main() -> int:
    parser = argparse.ArgumentParser(description='Проверить площадки по реальному росту подписчиков')
    parser.add_argument('--target', required=True, help='Ссылка на сообщество ВК или публичный Telegram-канал')
    parser.add_argument('--json', action='store_true', help='Вывести отчёт в JSON')
    parser.add_argument('--output', help='Сохранить отчёт в файл (JSON с --json, иначе текст)')
    parser.add_argument('--acknowledge-risk', action='store_true',
                        help='Подтвердить риск незавершённых ранее отправленных заказов')
    args = parser.parse_args()
    db.init_db()
    try:
        started = verifier.start(args.target, acknowledge_risk=args.acknowledge_risk)
    except ValueError as exc:
        print('Запуск невозможен:', exc, file=sys.stderr)
        return 2
    cid = started['campaign_id']
    previous = None
    interrupted = False
    while True:
        try:
            c = verifier.status(cid)
            if not c:
                print('Кампания исчезла из базы', file=sys.stderr)
                return 2
            phase = (c['status'], c.get('current_service'), c.get('current_phase'))
            if not args.json and phase != previous:
                print(' | '.join(str(x) for x in phase if x), file=sys.stderr, flush=True)
                previous = phase
            if c['status'] not in ('running', 'stopping'):
                break
            time.sleep(1)
        except KeyboardInterrupt:
            if not interrupted:
                interrupted = True
                print('\nОстанавливаю проверку; уже отправленные заявки могут продолжаться.', file=sys.stderr)
                try:
                    verifier.stop(cid)
                except ValueError:
                    pass
            else:
                print('Ожидаю завершения текущего драйвера…', file=sys.stderr)
    text = json.dumps(c, ensure_ascii=False, indent=2) if args.json else verifier.format_report(c)
    print(text)
    if args.output:
        with open(args.output, 'w', encoding='utf-8') as out:
            out.write(text + '\n')
    return 0 if c['status'] == 'completed' else (130 if interrupted else 2)


if __name__ == '__main__':
    raise SystemExit(main())
