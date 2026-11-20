# -*- coding: utf-8 -*-
"""Прототип «движка целей»: считает, реально ли получить N единиц за T времени.

Ничего не пишет и ничего не запускает — только читает data/catalog.json и считает.
Это тот самый расчёт, который должен лечь в основу нового слоя «целей».

    python tools/goal_planner.py                        # демо-примеры
    python tools/goal_planner.py post likes=100 views=200 --days 2
    python tools/goal_planner.py post views=500 --hours 3
    python tools/goal_planner.py community followers=300 --days 1
"""
from __future__ import annotations

import json
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "data" / "catalog.json"

# Правила «человечности»: софт не долбит сайты в одну секунду.
GAP_MIN_SEC = 60          # минимальная пауза между любыми заходами
GAP_JITTER_SEC = 120      # случайная добавка: получается 1–3 минуты между заходами
COOLDOWN_JITTER = 0.15    # к кулдауну сайта добавляем до +15% «на подумать»
WORK_HOURS = (8, 23)      # внутри этих часов разрешены заходы

PLATFORM = "vk"          # платформа ссылки
METRIC_TITLES = {
    "likes": ("лайк", "лайка", "лайков"),
    "views": ("просмотр", "просмотра", "просмотров"),
    "followers": ("подписчик", "подписчика", "подписчиков"),
    "plays": ("прослушивание", "прослушивания", "прослушиваний"),
    "reposts": ("репост", "репоста", "репостов"),
    "comments": ("комментарий", "комментария", "комментариев"),
}


def plural(n: int, metric: str) -> str:
    one, few, many = METRIC_TITLES.get(metric, ("единица", "единицы", "единиц"))
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} {few}"
    return f"{n} {many}"


@dataclass
class Service:
    id: str
    site_id: str
    site: str
    metric: str
    subject: str
    amount: int
    cooldown_min: int
    max_day: int | None
    label: str

    @property
    def cooldown_sec(self) -> int:
        return max(1, int(self.cooldown_min)) * 60


@dataclass
class Slot:
    at: datetime
    service: Service
    units: int


@dataclass
class Result:
    metric: str
    need: int
    reachable: int
    slots: list[Slot] = field(default_factory=list)
    per_site: dict[str, int] = field(default_factory=dict)
    per_day: dict[str, int] = field(default_factory=dict)
    stops: list[str] = field(default_factory=list)


def load_services() -> list[Service]:
    raw = json.loads(CATALOG.read_text(encoding="utf-8"))
    sites = raw["sites"] if isinstance(raw, dict) else raw
    out: list[Service] = []
    for site in sites:
        if not site.get("enabled", True):
            continue
        for v in site.get("services", []):
            if not v.get("enabled", True):
                continue
            if v.get("platform", "vk") != PLATFORM:
                continue
            out.append(Service(
                id=v["id"], site_id=site["id"], site=site["name"],
                metric=v.get("type", ""), subject=v.get("subject", "any"),
                amount=int(v.get("amount") or 0),
                cooldown_min=int(v.get("cooldown_min") or 0),
                max_day=v.get("max_claims_day"),
                label=v.get("label", ""),
            ))
    return out


def fits(svc: Service, kind: str) -> bool:
    """Подходит ли услуга к типу ссылки. 'any' подходит ко всему.

    Внутри «записи» каталог пока не различает пост / видео / фото — различаем по названию,
    чтобы «просмотры видео» не улетали в обычный текстовый пост.
    """
    label = svc.label.lower()
    is_video = ("видео" in label) or ("клип" in label)
    if kind == "video":
        if svc.subject not in ("", "any", "post"):
            return False
        return is_video or svc.metric in ("likes", "views", "comments", "reposts")
    if kind == "post":
        if svc.metric == "views" and is_video:
            return False
        if svc.subject in ("", "any"):
            return True
        return svc.subject == "post"
    if svc.subject in ("", "any"):
        return True
    return svc.subject == kind


def work_start(dt: datetime) -> datetime:
    """Ближайшее разрешённое время (если сейчас ночь — сдвигаем на утро)."""
    lo, hi = WORK_HOURS
    if dt.hour < lo:
        return dt.replace(hour=lo, minute=0, second=0, microsecond=0)
    if dt.hour >= hi:
        return (dt + timedelta(days=1)).replace(hour=lo, minute=0, second=0, microsecond=0)
    return dt


def simulate(services: list[Service], metric: str, need: int, start: datetime,
             deadline: datetime, humanize: bool = True, seed: int = 2026,
             mode: str = "spread") -> Result:
    """Жадно раскладывает заходы по времени. need=10**9 → замер capacity за период."""
    rnd = random.Random(seed)
    pool = [s for s in services if s.metric == metric and s.amount > 0]
    res = Result(metric=metric, need=need, reachable=0)
    if not pool:
        res.stops.append("нет ни одной услуги с такой метрикой")
        return res

    next_ok = {s.id: start for s in pool}
    claims_today: dict[str, tuple[str, int]] = {}   # service_id -> (дата, сколько заходов)
    limit_hit: set[str] = set()

    t = work_start(start)
    while res.reachable < need:
        if t >= deadline:
            res.stops.append("закончилось время")
            break
        # сайт, который освободится раньше всех
        candidates = [s for s in pool if next_ok[s.id] <= t]
        if not candidates:
            t = min(next_ok[s.id] for s in pool)
            t = work_start(t)
            if t >= deadline:
                res.stops.append("все сайты на кулдауне до конца срока")
                break
            continue

        # из свободных выбираем по стратегии; суточные лимиты уважаем всегда
        usable = []
        for s in candidates:
            day = t.strftime("%Y-%m-%d")
            used = claims_today.get(s.id, ("", 0))
            used_n = used[1] if used[0] == day else 0
            if s.max_day is not None and used_n >= s.max_day:
                limit_hit.add(s.id)
                continue
            site_share = res.per_site.get(s.site, 0)
            if mode == "spread":
                # «по-человечески»: сначала сайты, которые ещё не трогали, и мелкие пачки
                usable.append((site_share, s.amount, -s.cooldown_sec, s.id, s))
            else:
                # «впритык»: выжимаем объём
                usable.append((-s.amount, site_share, -s.cooldown_sec, s.id, s))
        if not usable:
            # все свободные упёрлись в суточный лимит → ждём следующий день
            nxt = (t + timedelta(days=1)).replace(hour=WORK_HOURS[0], minute=0, second=0, microsecond=0)
            if nxt <= t:
                nxt = t + timedelta(hours=1)
            t = nxt
            continue

        usable.sort(reverse=True)
        svc = usable[0][-1]
        units = min(svc.amount, need - res.reachable)
        res.slots.append(Slot(at=t, service=svc, units=units))
        res.reachable += units
        res.per_site[svc.site] = res.per_site.get(svc.site, 0) + units
        key = t.strftime("%Y-%m-%d")
        res.per_day[key] = res.per_day.get(key, 0) + units

        day = t.strftime("%Y-%m-%d")
        used = claims_today.get(svc.id, ("", 0))
        claims_today[svc.id] = (day, (used[1] if used[0] == day else 0) + 1)

        pause = svc.cooldown_sec
        if humanize:
            pause += int(pause * rnd.uniform(0, COOLDOWN_JITTER))
        next_ok[svc.id] = t + timedelta(seconds=pause)

        gap = GAP_MIN_SEC + (rnd.randint(0, GAP_JITTER_SEC) if humanize else 0)
        t = work_start(t + timedelta(seconds=gap))

    if limit_hit:
        res.stops.append(f"часть сайтов упёрлась в суточный лимит ({len(limit_hit)})")
    return res


def retime(slots: list[Slot], start: datetime, deadline: datetime,
           seed: int = 7) -> tuple[list[Slot], bool]:
    """Растягивает уже готовый список заходов на весь срок — чтобы не было «всё за 5 минут».

    Возвращает (новое расписание, уложились_ли_в_срок).
    """
    if len(slots) < 2:
        return slots, True
    rnd = random.Random(seed)
    window = max(1.0, (deadline - start).total_seconds())
    # берём 70% срока и не больше 3 часов на шаг: нет смысла цедить 100 лайков двое суток
    step = min(window * 0.7 / len(slots), 3 * 3600)
    out: list[Slot] = []
    last_use: dict[str, datetime] = {}
    for i, slot in enumerate(slots):
        target = work_start(start + timedelta(seconds=step * (i + rnd.uniform(0.2, 0.8))))
        svc = slot.service
        prev = last_use.get(svc.id)
        if prev is not None:
            earliest = prev + timedelta(seconds=int(svc.cooldown_sec * 1.1))
            target = max(target, work_start(earliest))
        if out:
            target = max(target, out[-1].at + timedelta(seconds=GAP_MIN_SEC))
        target = work_start(target)
        last_use[svc.id] = target
        out.append(Slot(at=target, service=svc, units=slot.units))
    return out, out[-1].at <= deadline


def report(kind: str, goals: dict[str, int], start: datetime, deadline: datetime,
           services: list[Service]) -> None:
    horizon_h = (deadline - start).total_seconds() / 3600
    print(f"\n{'=' * 78}")
    print(f"ЦЕЛЬ: {kind} · " + ", ".join(f"{plural(v, m)}" for m, v in goals.items())
          + f"  за {horizon_h:.1f} ч (до {deadline:%d.%m %H:%M})")
    print("=" * 78)
    for metric, need in goals.items():
        pool = [s for s in services if s.metric == metric and fits(s, kind)]
        if not pool:
            print(f"\n{metric}: ❌ нет услуг под этот тип ссылки")
            continue
        # 1) потолок за весь срок (need = бесконечность)
        cap = simulate(pool, metric, 10 ** 9, start, deadline, humanize=True, mode="tight")
        # 2) реальный план под нужное количество. Если запаса хватает с двойным
        #    избытком — раскладываем «спокойно», иначе добиваем объёмом
        mode = "spread" if cap.reachable >= need * 2 else "tight"
        plan = simulate(pool, metric, need, start, deadline, humanize=True, mode=mode)
        verdict = "✅ выполнимо" if plan.reachable >= need else "⚠️ не хватает"
        print(f"\n{plural(need, metric)} — {verdict}")
        print(f"   максимум за срок: {plural(cap.reachable, metric)} "
              f"(сайтов в пуле: {len({s.site for s in pool})}, услуг: {len(pool)})")
        if plan.reachable < need:
            print(f"   получится: {plural(plan.reachable, metric)}, "
                  f"не хватит {plural(need - plan.reachable, metric)}")
            for s in plan.stops:
                print(f"   причина: {s}")
            # что даст продление срока
            for extra_days in (1, 2, 3):
                longer = simulate(pool, metric, need, start,
                                  deadline + timedelta(days=extra_days), humanize=True)
                if longer.reachable >= need:
                    first = longer.slots[-1].at
                    print(f"   → хватит, если продлить срок до {first:%d.%m %H:%M} "
                          f"(+{extra_days} дн.)")
                    break
        if plan.slots:
            print(f"   режим раскладки: {'спокойный, размазываем по сайтам' if mode == 'spread' else 'впритык, выжимаем объём'}")
            paced, inside = retime(plan.slots, start, deadline)
            if inside and paced != plan.slots:
                plan.per_day = {}
                plan.per_site = {}
                for sl in paced:
                    d = sl.at.strftime("%Y-%m-%d")
                    plan.per_day[d] = plan.per_day.get(d, 0) + sl.units
                    plan.per_site[sl.service.site] = plan.per_site.get(sl.service.site, 0) + sl.units
                print(f"   если растянуть ровно на весь срок: с {paced[0].at:%d.%m %H:%M} "
                      f"по {paced[-1].at:%d.%m %H:%M}, паузы между заходами "
                      f"{int((paced[-1].at - paced[0].at).total_seconds() / max(1, len(paced) - 1) / 60)} мин")
                plan.slots = paced
            print(f"   заходов в плане: {len(plan.slots)}, "
                  f"последний — {plan.slots[-1].at:%d.%m %H:%M}")
            print("   по дням: " + ", ".join(f"{d}: {plural(u, metric)}"
                                             for d, u in sorted(plan.per_day.items())))
            print("   по сайтам: " + ", ".join(f"{s} {u}" for s, u in
                                               sorted(plan.per_site.items(), key=lambda x: -x[1])[:6]))
            print("   первые заходы:")
            for slot in plan.slots[:6]:
                print(f"     {slot.at:%d.%m %H:%M}  {slot.service.site:<12} "
                      f"{slot.service.label[:34]:<34} +{slot.units}")


def main(argv: list[str]) -> int:
    services = load_services()
    now = datetime.now().replace(second=0, microsecond=0)
    if len(argv) >= 2:
        kind = argv[1]
        goals = {}
        for token in argv[2:]:
            if "=" in token:
                metric, qty = token.split("=", 1)
                goals[metric] = int(qty)
        if "--days" in argv:
            days = float(argv[argv.index("--days") + 1])
            deadline = now + timedelta(days=days)
        elif "--hours" in argv:
            deadline = now + timedelta(hours=float(argv[argv.index("--hours") + 1]))
        else:
            deadline = now + timedelta(days=1)
        report(kind, goals, now, deadline, services)
        return 0

    print(f"Сейчас: {now:%d.%m %H:%M} · рабочих часов в сутках: {WORK_HOURS[1] - WORK_HOURS[0]}")
    report("post", {"likes": 100, "views": 200}, now, now + timedelta(days=2), services)
    report("post", {"views": 500}, now, now + timedelta(hours=3), services)
    report("post", {"likes": 5000}, now, now + timedelta(days=1), services)
    report("community", {"followers": 300}, now, now + timedelta(days=1), services)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
