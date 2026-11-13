"""Диалоговая логика без привязки к Telegram: текст на входе — Reply на выходе."""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from typing import Optional, Protocol

from .kb import KIND_LABEL, PATHS, Hit, SearchBackend

PAID_GAP = 5          # платный курс не чаще, чем раз в N ответов
PROFILE_AFTER = 2     # спрашиваем профиль после N-го ответа
CLARIFY_TOTAL = 8     # столько совпадений на одно слово — запрос слишком общий

PROFILE_Q = {
    "stage": ("На каком вы этапе?", [("Только думаю", "idea"), ("Уже открыл(а) дело", "running")]),
    "sphere": ("Чем занимаетесь?", [("Торговля", "trade"), ("Услуги", "services"), ("Производство", "prod")]),
    "tax": ("Какая система налогов?", [("УСН", "usn"), ("Патент/НПД", "npd"), ("Не знаю", "unknown")]),
}
PROFILE_ORDER = ["stage", "sphere", "tax"]


@dataclass
class User:
    tg_id: int
    stage: Optional[str] = None
    sphere: Optional[str] = None
    tax: Optional[str] = None
    answers: int = 0
    last_paid_at: int = -100
    profile_asked: bool = False
    pending: Optional[str] = None      # запрос, по которому задали уточняющий вопрос


@dataclass
class Reply:
    text: str                                              # HTML
    buttons: list[tuple[str, str]] = field(default_factory=list)   # (подпись, callback_data)


class Store(Protocol):
    async def get(self, tg_id: int) -> User: ...
    async def save(self, u: User) -> None: ...


def link(base_url: str, h: Hit) -> str:
    return f"{base_url}/{PATHS.get(h.kind, h.kind)}/{h.slug}"


def fmt_hit(base_url: str, h: Hit, with_snippet: bool = True) -> str:
    out = f'• {KIND_LABEL.get(h.kind, h.kind)}: <a href="{html.escape(link(base_url, h))}">{html.escape(h.title)}</a>'
    if with_snippet and h.snippet and not h.paid:
        out += f"\n  …{h.snippet}…"
    return out


class Dialog:
    def __init__(self, backend: SearchBackend, store: Store, base_url: str):
        self.kb, self.store, self.base = backend, store, base_url

    async def ask(self, tg_id: int, text: str) -> Reply:
        u = await self.store.get(tg_id)
        q = text.strip()
        if u.pending:                       # ответ на уточняющий вопрос
            q, u.pending = f"{u.pending} {q}", None
        res = await self.kb.search(q, limit=8)
        free = [h for h in res.hits if not h.paid]
        # платные ищем отдельно: иначе их вытесняют бесплатные статьи из топа
        paid = (await self.kb.search(q, limit=2, only_paid=True)).hits

        # слишком общий запрос из одного слова → один уточняющий вопрос
        if len(res.stems) == 1 and res.total >= CLARIFY_TOTAL and not text.startswith("/"):
            u.pending = q
            await self.store.save(u)
            opts = [(h.title.replace(" ", " ")[:40], "q:" + h.title.replace(" ", " ")) for h in free[:3]]
            return Reply("Слишком широкий вопрос. Что именно вас интересует? Опишите подробнее "
                         "или выберите ближайшее:", opts)

        strong = [h for h in free if h.coverage >= 0.5]
        rec = [h for h in paid if h.coverage >= 0.5]
        if not (strong or rec):
            await self.store.save(u)
            return Reply("В базе «Альфа-Курс» по этому вопросу ничего не нашёл. Попробуйте переформулировать "
                         "или уточнить тему, например: налоги, патент, самозанятость, касса.")

        parts = []
        if strong:
            parts.append("Нашёл в базе:\n" + "\n".join(fmt_hit(self.base, h) for h in strong[:3]))
        elif free:
            parts.append("Точного ответа нет, ближайшее по теме:\n" + "\n".join(fmt_hit(self.base, h, False) for h in free[:2]))
        else:
            parts.append("Бесплатных материалов по этому вопросу в базе нет.")

        u.answers += 1
        if rec and u.answers - u.last_paid_at >= PAID_GAP:
            h = rec[0]
            price = f" ({h.price} ₽)" if h.price else ""
            parts.append(f"Если нужно глубже — есть платный курс{price}:\n" + fmt_hit(self.base, h, False))
            u.last_paid_at = u.answers

        buttons: list[tuple[str, str]] = []
        nxt = self._next_profile(u)
        if nxt:
            u.profile_asked = True
            question, opts = PROFILE_Q[nxt]
            parts.append(f"\n{question} (поможет подбирать материалы точнее)")
            buttons = [(label, f"p:{nxt}:{val}") for label, val in opts] + [("Пропустить", "p:skip:1")]
        await self.store.save(u)
        return Reply("\n\n".join(parts), buttons)

    @staticmethod
    def _next_profile(u: User) -> Optional[str]:
        if u.answers < PROFILE_AFTER or u.profile_asked:
            return None
        return next((k for k in PROFILE_ORDER if getattr(u, k) is None), None)

    async def profile(self, tg_id: int, key: str, val: str) -> Reply:
        u = await self.store.get(tg_id)
        u.profile_asked = False
        if key == "skip":
            u.profile_asked = True            # больше не спрашиваем
            await self.store.save(u)
            return Reply("Хорошо, не буду спрашивать.")
        if key in PROFILE_Q and val in {v for _, v in PROFILE_Q[key][1]}:
            setattr(u, key, val)
        nxt = next((k for k in PROFILE_ORDER if getattr(u, k) is None), None)
        await self.store.save(u)
        if not nxt:
            return Reply("Спасибо, запомнил.")
        u.profile_asked = True
        await self.store.save(u)
        question, opts = PROFILE_Q[nxt]
        return Reply(question, [(l, f"p:{nxt}:{v}") for l, v in opts] + [("Пропустить", "p:skip:1")])
