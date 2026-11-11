"""База знаний: индексация данных «Альфа-Курс» и поиск.

Поиск спрятан за интерфейсом SearchBackend. Сейчас единственная реализация —
PgSearchBackend (Postgres full-text, конфигурация russian). Позже её можно
заменить/дополнить LLM- или векторным бэкендом, не трогая диалоговую логику.
"""
from __future__ import annotations

import html
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol

import asyncpg

PUBLISHED = uuid.UUID("61f12dbc-3cd4-4c14-b64b-1709548b904e")
FINISHED = uuid.UUID("078537f9-7672-44c9-87ba-43dc6aba6e47")
PATHS = {
    "article": "articles", "course": "courses", "webinar": "webinars",
    "podcast": "podcasts", "glossary": "glossary", "document": "documents",
    "calculator": "calculators",
}
KIND_LABEL = {
    "article": "Статья", "course": "Курс", "webinar": "Вебинар", "podcast": "Подкаст",
    "glossary": "Словарь", "document": "Шаблон документа", "calculator": "Калькулятор",
}
# слова-«вода», которые не должны влиять на поиск
FILLERS = set("""хочу хотел хотела хотелось подскажите подскажи расскажите расскажи скажите скажи
нужно нужна нужен надо можно пожалуйста помогите помоги мне меня как что где когда какой какая какие
почему зачем есть ли бот привет здравствуйте добрый день вопрос узнать понять разобраться
интересует интересно""".split())

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def clean_html(s: Optional[str]) -> str:
    if not s:
        return ""
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"</(p|div|li|h\d|tr)>|<br\s*/?>", ". ", s, flags=re.I)
    s = html.unescape(_TAG.sub(" ", s)).replace("\xa0", " ")
    s = _WS.sub(" ", s)
    s = re.sub(r"\s+([.,;:!?])", r"\1", s)
    s = re.sub(r"([.!?:;])(\s*\.)+", r"\1", s)
    return s.strip()


# ---------------------------------------------------------------- индексация

def _embed(table: str, fk: str, alias: str, tbl: str) -> str:
    return (f'coalesce((SELECT string_agg(e."HtmlContent", \' \' ORDER BY e."Order") FROM "{table}" e '
            f'WHERE e."{fk}"={alias}."Id" AND e."Type"=0), \'\') body')


SRC_SQL = {
    "article": f'''SELECT a."Id" id, a."Title" title, a."Url" slug,
        concat_ws(' ', a."Description", a."CardDescription") summary,
        {_embed("ArticleEmbedItems", "ArticleId", "a", "")}
        FROM "Articles" a WHERE NOT a."IsDeleted" AND a."StatusId"=$1 AND NOT coalesce(a."IsClosed", false)''',
    "webinar": '''SELECT "Id" id, "Title" title, "Url" slug, "Description" summary, coalesce("DescriptionExt",'') body
        FROM "Webinars" WHERE NOT "IsDeleted" AND "StatusId" IN ($1, $2)''',
    "podcast": '''SELECT "Id" id, "Title" title, "Url" slug, "Description" summary, coalesce("DescriptionExt",'') body
        FROM "Podcasts" WHERE NOT "IsDeleted" AND "StatusId"=$1''',
    "glossary": f'''SELECT g."Id" id, g."Title" title, g."Url" slug, g."Description" summary,
        {_embed("GlossaryEmbedItems", "GlossaryId", "g", "")}
        FROM "Glossary" g WHERE NOT g."IsDeleted" AND g."StatusId"=$1''',
    "document": f'''SELECT d."Id" id, d."Title" title, d."Url" slug, d."Description" summary,
        {_embed("DocumentEmbedItems", "DocumentId", "d", "")}
        FROM "Documents" d WHERE NOT d."IsDeleted" AND d."StatusId"=$1''',
    "calculator": '''SELECT "Id" id, "Title" title, "Url" slug, coalesce("Description",'') summary, '' body
        FROM "Calculators" WHERE NOT "IsDeleted"''',
}
COURSE_SQL = '''SELECT c."Id" id, c."Title" title, c."Url" slug,
        concat_ws(' ', c."Description", c."Target") summary, c."Cost" cost,
        coalesce((SELECT string_agg(e."HtmlContent", ' ' ORDER BY b."Order", e."Order")
                  FROM "CourseLessonBindings" b
                  JOIN "LessonEmbedItems" e ON e."LessonId"=b."LessonId" AND e."Type"=0
                  WHERE b."CourseId"=c."Id" AND NOT b."IsDeleted"), '') lessons
        FROM "Courses" c WHERE NOT c."IsDeleted" AND c."StatusId"=$1 AND NOT coalesce(c."IsClosed", false)'''

INSERT = '''INSERT INTO bot.kb_items(id,kind,title,slug,summary,body,hidden_body,paid,price,search)
    VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,
      setweight(to_tsvector('russian',$3),'A') || setweight(to_tsvector('russian',$5),'B')
      || setweight(to_tsvector('russian',$6),'C') || setweight(to_tsvector('russian',$7),'D'))'''


async def init_schema(pool: asyncpg.Pool) -> None:
    sql = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
    async with pool.acquire() as c:
        await c.execute(sql)


async def build_index(pool: asyncpg.Pool, force: bool = False) -> int:
    """Собирает bot.kb_items из исходных таблиц. Если индекс уже есть — пропускает."""
    async with pool.acquire() as c:
        n = await c.fetchval("SELECT count(*) FROM bot.kb_items")
        if n and not force:
            return n
        rows = []
        for kind, sql in SRC_SQL.items():
            args = {"webinar": (PUBLISHED, FINISHED), "calculator": ()}.get(kind, (PUBLISHED,))
            for r in await c.fetch(sql, *args):
                rows.append((r["id"], kind, str(r["title"]), str(r["slug"] or ""),
                             clean_html(str(r["summary"] or ""))[:2000],
                             clean_html(str(r["body"] or ""))[:200_000], "", False, None))
        for r in await c.fetch(COURSE_SQL, PUBLISHED):
            paid = bool(r["cost"] and r["cost"] > 0)
            lessons = clean_html(str(r["lessons"] or ""))[:200_000]
            summary = clean_html(str(r["summary"] or ""))[:2000]
            # платный курс: текст уроков идёт только в hidden_body (для релевантности)
            rows.append((r["id"], "course", str(r["title"]), str(r["slug"] or ""), summary,
                         "" if paid else lessons, lessons if paid else "", paid, r["cost"]))
        async with c.transaction():
            await c.execute("TRUNCATE bot.kb_items")
            for row in rows:
                await c.execute(INSERT, *row)
        return len(rows)


# ---------------------------------------------------------------- поиск

@dataclass
class Hit:
    id: str
    kind: str
    title: str
    slug: str
    summary: str
    paid: bool
    price: Optional[int]
    matched: int          # сколько основ из запроса найдено в материале
    title_matched: int
    coverage: float       # matched / число основ запроса
    snippet: Optional[str] = None   # None для платных материалов


@dataclass
class SearchResult:
    stems: list[str] = field(default_factory=list)
    hits: list[Hit] = field(default_factory=list)
    total: int = 0        # сколько материалов содержат хоть одну основу


class SearchBackend(Protocol):
    async def search(self, query: str, limit: int = 6, only_paid: Optional[bool] = None) -> SearchResult: ...
    async def define(self, term: str) -> Optional[Hit]: ...


def esc_snippet(s: str) -> str:
    return html.escape(s).replace("\x01", "<b>").replace("\x02", "</b>")


class PgSearchBackend:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def stems(self, query: str) -> list[str]:
        words = [w for w in re.findall(r"[\w-]+", query.lower()) if w not in FILLERS]
        if not words:
            return []
        async with self.pool.acquire() as c:
            arr = await c.fetchval("SELECT tsvector_to_array(to_tsvector('russian', $1))", " ".join(words))
        return list(arr or [])[:8]

    async def search(self, query: str, limit: int = 6, only_paid: Optional[bool] = None) -> SearchResult:
        stems = await self.stems(query)
        if not stems:
            return SearchResult()
        n = len(stems)
        tq = " || ".join(f"plainto_tsquery('simple',${i + 1})" for i in range(n))
        matched = " + ".join(f"(search @@ plainto_tsquery('simple',${i + 1}))::int" for i in range(n))
        tmatch = " + ".join(f"(to_tsvector('russian',title) @@ plainto_tsquery('simple',${i + 1}))::int" for i in range(n))
        paid_f = "" if only_paid is None else f"AND paid = {'true' if only_paid else 'false'}"
        sql = f'''
        WITH cand AS (
          SELECT id,kind,title,slug,summary,paid,price,body,
                 ({matched}) AS matched, ({tmatch}) AS tmatched,
                 ts_rank_cd(search, {tq}, 32) AS rank
          FROM bot.kb_items WHERE search @@ ({tq}) {paid_f}
        )
        SELECT *, count(*) OVER () AS total FROM cand
        ORDER BY matched*10 + tmatched*6 + rank*20 DESC LIMIT {int(limit)}'''
        hl_q = " || ".join(f"plainto_tsquery('simple',${i + 2})" for i in range(n))
        hl = (f"SELECT ts_headline('russian', $1::text, {hl_q}, "
              "'StartSel=\x01,StopSel=\x02,MaxFragments=1,MinWords=14,MaxWords=38')")
        async with self.pool.acquire() as c:
            rows = await c.fetch(sql, *stems)
            res = SearchResult(stems=stems, total=rows[0]["total"] if rows else 0)
            for r in rows:
                snippet = None
                if not r["paid"]:
                    text = r["body"] or r["summary"]
                    if text:
                        snippet = esc_snippet(await c.fetchval(hl, text[:30000], *stems))
                res.hits.append(Hit(str(r["id"]), r["kind"], r["title"], r["slug"], r["summary"], r["paid"],
                                    r["price"], r["matched"], r["tmatched"], r["matched"] / n, snippet))
        return res

    async def define(self, term: str) -> Optional[Hit]:
        term = term.strip(" ?.!\"«»").lower()
        if not term:
            return None
        async with self.pool.acquire() as c:
            r = await c.fetchrow(
                "SELECT id,title,slug,summary,body FROM bot.kb_items WHERE kind='glossary' AND lower(title)=$1 LIMIT 1", term)
        if not r:
            return None
        text = r["summary"] or r["body"]
        return Hit(str(r["id"]), "glossary", r["title"], r["slug"], r["summary"], False, None, 1, 1, 1.0,
                   html.escape(text[:350]) if text else None)
