import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo

from bot.dialog import Dialog, User
from bot.kb import Hit, SearchResult, clean_html
from bot.reminders import next_occurrence, parse_reminder

TZ = ZoneInfo("Europe/Moscow")


def hit(title, paid=False, cov=1.0, kind="article", price=None):
    return Hit("1", "course" if paid else kind, title, "slug", "", paid, price, 1, 1, cov,
              None if paid else "<b>текст</b>")


class FakeKB:
    def __init__(self, hits, stems=("усн", "налог"), total=None):
        self.hits, self.stems, self.total = hits, list(stems), total

    async def search(self, q, limit=6, only_paid=None):
        return SearchResult(self.stems, self.hits, self.total if self.total is not None else len(self.hits))


class MemStore:
    def __init__(self):
        self.u = {}

    async def get(self, i):
        return self.u.setdefault(i, User(i))

    async def save(self, u):
        self.u[u.tg_id] = u


def run(c):
    return asyncio.run(c)


def test_clean_html():
    assert clean_html("<p>Привет&nbsp;мир</p><script>x</script>") == "Привет мир."


def test_not_found_is_honest():
    d = Dialog(FakeKB([], stems=["абракадабра"]), MemStore(), "https://x")
    assert "ничего не нашёл" in run(d.ask(1, "абракадабра")).text


def test_paid_text_never_shown_and_throttled():
    kb = FakeKB([hit("Статья"), hit("Курс УСН", paid=True, price=3990)])
    d = Dialog(kb, MemStore(), "https://x")
    r1 = run(d.ask(1, "усн налог"))
    assert "Курс УСН" in r1.text and "3990" in r1.text and "текст" in r1.text
    assert r1.text.count("текст") == 1                       # сниппета платного нет
    r2 = run(d.ask(1, "усн налог"))
    assert "платный курс" not in r2.text                     # не чаще раза в 5 ответов


def test_vague_query_asks_once():
    kb = FakeKB([hit("A"), hit("B")], stems=["налог"], total=50)
    d = Dialog(kb, MemStore(), "https://x")
    r = run(d.ask(1, "налоги"))
    assert "Слишком широкий" in r.text and r.buttons
    assert d.store.u[1].pending == "налоги"
    kb.total = 2
    assert "Нашёл" in run(d.ask(1, "для ИП")).text


def test_profile_after_second_answer_and_skip():
    d = Dialog(FakeKB([hit("A")]), MemStore(), "https://x")
    assert not run(d.ask(1, "усн налог")).buttons
    r = run(d.ask(1, "усн налог"))
    assert r.buttons and r.buttons[0][1].startswith("p:stage")
    run(d.profile(1, "stage", "idea"))
    assert d.store.u[1].stage == "idea"
    run(d.profile(1, "skip", "1"))
    assert not run(d.ask(1, "усн налог")).buttons


def test_parse():
    today = date(2026, 11, 9)
    p = parse_reminder("напомни завтра в 9 оплатить аренду", today)
    assert (p.rule, p.hour, p.text) == ("once:2026-11-10", 9, "оплатить аренду")
    assert parse_reminder("каждый месяц 15 числа сдать отчёт", today).rule == "monthly:15"
    assert parse_reminder("через 3 дня позвонить", today).rule == "once:2026-11-12"
    assert parse_reminder("25 декабря сдать отчёт", today).rule == "once:2026-12-25"
    assert parse_reminder("5 ноября что-то", today).rule == "once:2027-11-05"   # прошло → следующий год
    assert parse_reminder("когда-нибудь позвонить", today) is None


def test_next_occurrence():
    after = datetime(2026, 11, 9, 12, tzinfo=TZ)
    assert next_occurrence("monthly:31", after, 10) == datetime(2026, 11, 30, 10, tzinfo=TZ)
    assert next_occurrence("yearly:04-28,07-28,10-28", after, 10, 3) == datetime(2027, 4, 25, 10, tzinfo=TZ)
    assert next_occurrence("once:2026-11-01", after, 10) is None
    assert next_occurrence("once:2026-11-10", after, 9) == datetime(2026, 11, 10, 9, tzinfo=TZ)
