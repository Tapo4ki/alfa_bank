"""Напоминания: разбор текста, расчёт следующего срабатывания, налоговый календарь.

rule:
  once:YYYY-MM-DD       — один раз
  monthly:D             — каждый месяц D-го числа (31 → последний день)
  yearly:MM-DD[,MM-DD]  — ежегодно в указанные даты
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

# Налоговые события: ключ -> (название, rule). Даты ориентировочные, переносы на выходные не учитываются.
TAX_EVENTS = {
    "usn_advance": ("Авансовый платёж по УСН", "yearly:04-28,07-28,10-28"),
    "usn_ip_year": ("Налог по УСН за год для ИП", "yearly:04-28"),
    "usn_ooo_year": ("Налог по УСН за год для ООО", "yearly:03-28"),
    "ip_contrib": ("Страховые взносы ИП за себя", "yearly:12-31"),
    "ip_1pct": ("Взнос 1% с дохода ИП свыше 300 тыс. ₽", "yearly:07-01"),
    "enp": ("Единый налоговый платёж (ЕНП)", "monthly:28"),
}

MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
          "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}


@dataclass
class Parsed:
    text: str
    rule: str
    hour: int = 10


def _last_day(y: int, m: int) -> int:
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return (nxt - timedelta(days=1)).day


def next_occurrence(rule: str, after: datetime, hour: int, lead_days: int = 0) -> Optional[datetime]:
    """Ближайшее срабатывание строго после `after` (aware datetime). None — больше не будет."""
    tz = after.tzinfo
    kind, _, arg = rule.partition(":")
    cands: list[date] = []
    base = after.date()
    if kind == "once":
        cands = [date.fromisoformat(arg)]
    elif kind == "monthly":
        d = int(arg)
        y, m = base.year, base.month
        for _ in range(14):
            cands.append(date(y, m, min(d, _last_day(y, m))))
            y, m = y + (m == 12), m % 12 + 1
    elif kind == "yearly":
        for y in (base.year, base.year + 1, base.year + 2):
            for md in arg.split(","):
                mm, dd = (int(x) for x in md.split("-"))
                cands.append(date(y, mm, min(dd, _last_day(y, mm))))
    else:
        return None
    best = None
    for c in sorted(cands):
        fire = datetime.combine(c - timedelta(days=lead_days), datetime.min.time(), tzinfo=tz).replace(hour=hour)
        if fire > after:
            best = fire
            break
    return best


def parse_reminder(text: str, today: date) -> Optional[Parsed]:
    """Разбирает фразы вроде «напомни завтра оплатить аренду», «каждый месяц 15 числа ...»,
    «через 3 дня ...», «25 декабря ...», «12.03 ...». Возвращает None, если срок не распознан."""
    t = text.strip()
    t = re.sub(r"^(/remind|напомни(те)?( мне)?)\s*", "", t, flags=re.I).strip()
    hour = 10
    m = re.search(r"\bв\s+(\d{1,2})(?::00)?(?:\s*(?:ч|час\w*))?\b", t)
    if m and 0 <= int(m.group(1)) <= 23:
        hour = int(m.group(1))
        t = (t[:m.start()] + t[m.end():]).strip()

    def done(rule: str, rest: str) -> Optional[Parsed]:
        rest = re.sub(r"\s+", " ", rest).strip(" ,.-—")
        return Parsed(rest, rule, hour) if rest else None

    m = re.search(r"кажд\w+\s+месяц\w*\s+(\d{1,2})\s*(?:-?го)?\s*(?:числа)?", t, re.I)
    if m and 1 <= int(m.group(1)) <= 31:
        return done(f"monthly:{int(m.group(1))}", t[:m.start()] + t[m.end():])
    m = re.search(r"\bзавтра\b", t, re.I)
    if m:
        return done(f"once:{(today + timedelta(days=1)).isoformat()}", t[:m.start()] + t[m.end():])
    m = re.search(r"\bпослезавтра\b", t, re.I)
    if m:
        return done(f"once:{(today + timedelta(days=2)).isoformat()}", t[:m.start()] + t[m.end():])
    m = re.search(r"через\s+(\d+)\s+(дн\w*|день|недел\w*)", t, re.I)
    if m:
        n = int(m.group(1)) * (7 if m.group(2).lower().startswith("недел") else 1)
        return done(f"once:{(today + timedelta(days=n)).isoformat()}", t[:m.start()] + t[m.end():])
    m = re.search(r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")\b", t, re.I)
    if m:
        return _dated(int(m.group(1)), MONTHS[m.group(2).lower()], t[:m.start()] + t[m.end():], today, done)
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})(?:\.\d{2,4})?\b", t)
    if m:
        return _dated(int(m.group(1)), int(m.group(2)), t[:m.start()] + t[m.end():], today, done)
    return None


def _dated(d: int, mo: int, rest: str, today: date, done):
    if not (1 <= mo <= 12 and 1 <= d <= _last_day(2024, mo)):
        return None
    y = today.year
    if date(y, mo, min(d, _last_day(y, mo))) <= today:
        y += 1
    return done(f"once:{date(y, mo, min(d, _last_day(y, mo))).isoformat()}", rest)


def now_in(tz: str) -> datetime:
    return datetime.now(ZoneInfo(tz))


# ---------------------------------------------------------------- хранилище и планировщик

async def add(pool, tg_id: int, p: Parsed, tz: str, kind: str = "once", lead_days: int = 0) -> Optional[datetime]:
    rule_kind = p.rule.split(":")[0]
    nxt = next_occurrence(p.rule, now_in(tz), p.hour, lead_days)
    if nxt is None:
        return None
    kind = "tax" if kind == "tax" else ("once" if rule_kind == "once" else "recurring")
    async with pool.acquire() as c:
        await c.execute(
            "INSERT INTO bot.reminders(tg_id,text,kind,rule,lead_days,hour,next_at) VALUES($1,$2,$3,$4,$5,$6,$7)",
            tg_id, p.text, kind, p.rule, lead_days, p.hour, nxt)
    return nxt


async def due(pool, tz: str):
    """Забирает сработавшие напоминания и сдвигает/деактивирует их. Возвращает [(tg_id, text)]."""
    out = []
    async with pool.acquire() as c, c.transaction():
        rows = await c.fetch(
            "SELECT r.*, coalesce(u.reminders_paused,false) paused FROM bot.reminders r "
            "LEFT JOIN bot.users u ON u.tg_id=r.tg_id WHERE r.active AND r.next_at <= now() FOR UPDATE OF r")
        for r in rows:
            nxt = next_occurrence(r["rule"], max(now_in(tz), r["next_at"].astimezone(ZoneInfo(tz))), r["hour"], r["lead_days"])
            if nxt is None:
                await c.execute("UPDATE bot.reminders SET active=false WHERE id=$1", r["id"])
            else:
                await c.execute("UPDATE bot.reminders SET next_at=$2 WHERE id=$1", r["id"], nxt)
            if not r["paused"]:
                out.append((r["tg_id"], r["text"], r["kind"], r["lead_days"]))
    return out


async def listing(pool, tg_id: int):
    async with pool.acquire() as c:
        return await c.fetch("SELECT id,text,kind,next_at FROM bot.reminders WHERE tg_id=$1 AND active ORDER BY next_at", tg_id)


async def remove(pool, tg_id: int, rid: int) -> bool:
    async with pool.acquire() as c:
        return (await c.execute("UPDATE bot.reminders SET active=false WHERE id=$1 AND tg_id=$2 AND active", rid, tg_id)).endswith("1")
