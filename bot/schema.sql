CREATE SCHEMA IF NOT EXISTS bot;

CREATE TABLE IF NOT EXISTS bot.kb_items (
    id          uuid PRIMARY KEY,
    kind        text NOT NULL,          -- article|course|lesson|webinar|podcast|glossary|document|calculator
    title       text NOT NULL,
    slug        text NOT NULL,
    summary     text NOT NULL DEFAULT '',
    body        text NOT NULL DEFAULT '',   -- чистый текст, можно цитировать
    hidden_body text NOT NULL DEFAULT '',   -- текст платных уроков: только для поиска, не показывается
    paid        boolean NOT NULL DEFAULT false,
    price       integer,
    search      tsvector NOT NULL
);
CREATE INDEX IF NOT EXISTS kb_items_search_idx ON bot.kb_items USING gin (search);

CREATE TABLE IF NOT EXISTS bot.users (
    tg_id       bigint PRIMARY KEY,
    stage       text,
    sphere      text,
    tax         text,
    state       jsonb NOT NULL DEFAULT '{}',
    answers     integer NOT NULL DEFAULT 0,
    last_paid_at integer NOT NULL DEFAULT -100,   -- номер ответа, на котором показывали платный курс
    profile_asked boolean NOT NULL DEFAULT false,
    reminders_paused boolean NOT NULL DEFAULT false,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bot.reminders (
    id          serial PRIMARY KEY,
    tg_id       bigint NOT NULL,
    text        text NOT NULL,
    kind        text NOT NULL,          -- once|recurring|tax
    rule        text NOT NULL DEFAULT '', -- расписание, см. reminders.py
    lead_days   integer NOT NULL DEFAULT 0,
    hour        integer NOT NULL DEFAULT 10,
    next_at     timestamptz,
    active      boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS reminders_due_idx ON bot.reminders (next_at) WHERE active;
