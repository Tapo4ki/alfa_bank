"""Точка входа: Telegram-бот «Альфа-Курс»."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import reminders as rem
from .config import Config
from .dialog import Dialog, Reply, User
from .kb import PgSearchBackend, build_index, init_schema

log = logging.getLogger("bot")

HELP = ("Я помощник «Альфа-Курс». Задайте вопрос о бизнесе, налогах, документах — найду материалы в базе.\n\n"
        "/remind — создать напоминание, например: <i>напомни завтра в 9 оплатить аренду</i> "
        "или <i>каждый месяц 15 числа сдать отчёт</i>\n"
        "/tax — подписаться на налоговые даты\n"
        "/list — мои напоминания, /del N — удалить\n"
        "/pause и /resume — выключить и включить напоминания")


class PgStore:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def get(self, tg_id: int) -> User:
        async with self.pool.acquire() as c:
            await c.execute("INSERT INTO bot.users(tg_id) VALUES($1) ON CONFLICT DO NOTHING", tg_id)
            r = await c.fetchrow("SELECT * FROM bot.users WHERE tg_id=$1", tg_id)
        st = json.loads(r["state"]) if isinstance(r["state"], str) else r["state"]
        return User(tg_id, r["stage"], r["sphere"], r["tax"], r["answers"], r["last_paid_at"],
                    r["profile_asked"], st.get("pending"))

    async def save(self, u: User) -> None:
        async with self.pool.acquire() as c:
            await c.execute(
                "UPDATE bot.users SET stage=$2,sphere=$3,tax=$4,answers=$5,last_paid_at=$6,profile_asked=$7,state=$8::jsonb WHERE tg_id=$1",
                u.tg_id, u.stage, u.sphere, u.tax, u.answers, u.last_paid_at, u.profile_asked,
                json.dumps({"pending": u.pending}))


def cb(d: str) -> str:
    """callback_data в Telegram ограничена 64 БАЙТАМИ (кириллица = 2 байта на символ)."""
    return d.encode()[:64].decode(errors="ignore")


def kb_markup(r: Reply) -> InlineKeyboardMarkup | None:
    if not r.buttons:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=cb(d))] for t, d in r.buttons])


async def amain() -> None:
    logging.basicConfig(level=logging.INFO)
    cfg = Config.from_env()
    if not cfg.token:
        raise SystemExit("Не задан TELEGRAM_BOT_TOKEN")
    pool = await asyncpg.create_pool(cfg.database_url, min_size=1, max_size=5)
    await init_schema(pool)
    log.info("индекс: %s материалов", await build_index(pool))
    dlg = Dialog(PgSearchBackend(pool), PgStore(pool), cfg.base_url)
    bot, dp = Bot(cfg.token, default=__import__("aiogram").client.default.DefaultBotProperties(parse_mode="HTML")), Dispatcher()

    async def send(m: Message, r: Reply):
        await m.answer(r.text, reply_markup=kb_markup(r), disable_web_page_preview=True)

    @dp.message(CommandStart())
    @dp.message(Command("help"))
    async def start(m: Message):
        await m.answer(HELP)

    @dp.message(Command("remind"))
    async def remind(m: Message):
        p = rem.parse_reminder(m.text or "", date.today())
        if not p:
            return await m.answer("Не понял срок. Примеры: «напомни завтра в 9 оплатить аренду», "
                                  "«через 3 дня позвонить в банк», «25 декабря сдать отчёт», "
                                  "«каждый месяц 15 числа платить налог».")
        nxt = await rem.add(pool, m.from_user.id, p, cfg.tz)
        await m.answer(f"Готово: «{p.text}». Ближайшее — {nxt:%d.%m.%Y в %H:%M}." if nxt else "Эта дата уже прошла.")

    def lead_kb():
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"За {n} дн.", callback_data=f"tl:{n}") for n in (1, 3, 7)
        ] + [InlineKeyboardButton(text="Отключить", callback_data="td")]])

    @dp.message(Command("tax"))
    async def tax(m: Message):
        rows = [[InlineKeyboardButton(text=v[0], callback_data=f"tm:{k}")] for k, v in rem.TAX_MODES.items()]
        rows.append([InlineKeyboardButton(text="Другой / не знаю", callback_data="tm:other")])
        await m.answer("Чтобы напоминать про нужные налоги, уточните: какой у вас режим?",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    @dp.callback_query(F.data.startswith("tm:"))
    async def tax_mode(cq: CallbackQuery):
        mode = cq.data[3:]
        if mode not in rem.TAX_MODES:
            await cq.message.answer("Тогда лучше сначала определиться с режимом — спросите меня, например, «какую систему налогообложения выбрать». "
                                    "Автоматические налоговые даты я веду для УСН и НПД.")
            return await cq.answer()
        res = await rem.tax_subscribe(pool, cq.from_user.id, cfg.tz, mode, 3)
        lines = "\n".join(f"• {n} — {d:%d.%m.%Y}" for n, d in res)
        await cq.message.answer(f"Подписал ({rem.TAX_MODES[mode][0]}), предупрежу за 3 дня:\n{lines}\n"
                                "Даты ориентировочные, без переноса на выходные. Можно изменить частоту или отключить:",
                                reply_markup=lead_kb())
        await cq.answer()

    @dp.callback_query(F.data.startswith("tl:"))
    async def tax_lead(cq: CallbackQuery):
        n = await rem.tax_set_lead(pool, cq.from_user.id, cfg.tz, int(cq.data[3:]))
        await cq.message.answer(f"Теперь предупреждаю за {cq.data[3:]} дн. (напоминаний: {n})." if n else "Налоговых напоминаний нет, начните с /tax.")
        await cq.answer()

    @dp.callback_query(F.data == "td")
    async def tax_off(cq: CallbackQuery):
        n = await rem.tax_disable(pool, cq.from_user.id)
        await cq.message.answer(f"Налоговые напоминания отключены ({n})." if n else "Налоговых напоминаний не было.")
        await cq.answer()

    @dp.message(Command("list"))
    async def lst(m: Message):
        rows = await rem.listing(pool, m.from_user.id)
        await m.answer("\n".join(f"{r['id']}. {r['text']} — {r['next_at']:%d.%m.%Y}" for r in rows) or "Напоминаний нет.")

    @dp.message(Command("del"))
    async def dele(m: Message):
        arg = (m.text or "").split(maxsplit=1)[-1]
        ok = arg.isdigit() and await rem.remove(pool, m.from_user.id, int(arg))
        await m.answer("Удалил." if ok else "Укажите номер из /list, например: /del 3")

    for name, flag in (("pause", True), ("resume", False)):
        @dp.message(Command(name))
        async def pause(m: Message, flag=flag):
            async with pool.acquire() as c:
                await c.execute("INSERT INTO bot.users(tg_id) VALUES($1) ON CONFLICT DO NOTHING", m.from_user.id)
                await c.execute("UPDATE bot.users SET reminders_paused=$2 WHERE tg_id=$1", m.from_user.id, flag)
            await m.answer("Напоминания выключены." if flag else "Напоминания включены.")

    @dp.callback_query(F.data.startswith("p:"))
    async def prof(cq: CallbackQuery):
        _, key, val = cq.data.split(":", 2)
        await send(cq.message, await dlg.profile(cq.from_user.id, key, val))
        await cq.answer()

    @dp.callback_query(F.data.startswith("q:"))
    async def quick(cq: CallbackQuery):
        await send(cq.message, await dlg.ask(cq.from_user.id, cq.data[2:]))
        await cq.answer()

    @dp.message(F.text)
    async def text(m: Message):
        await send(m, await dlg.ask(m.from_user.id, m.text))

    async def scheduler():
        while True:
            try:
                for tg_id, txt, kind, lead in await rem.due(pool, cfg.tz):
                    pre = "Налоговая дата скоро: " if kind == "tax" else "Напоминание: "
                    await bot.send_message(tg_id, pre + txt)
            except Exception:
                log.exception("scheduler")
            await asyncio.sleep(30)

    task = asyncio.create_task(scheduler())
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()


if __name__ == "__main__":
    asyncio.run(amain())
