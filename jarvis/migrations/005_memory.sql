-- 005_memory.sql — what the user asked Jarvis to remember, and when to say it.
--
-- NOTES are facts the user handed over in words: "remember my locker is 214",
-- "I take my coffee black". They are the user's, they are short, and they are
-- read back into every conversation's instructions, so the table is small on
-- purpose and every row says who wrote it and from which channel.
CREATE TABLE notes (
  id TEXT PRIMARY KEY,
  text TEXT NOT NULL,
  actor TEXT NOT NULL,
  channel TEXT NOT NULL,
  created_at TEXT NOT NULL,
  forgotten_at TEXT,                 -- kept, not deleted: "what did I tell you?" has an answer
  CHECK (length(trim(text)) > 0)
);
CREATE INDEX notes_live ON notes(forgotten_at, created_at);

-- REMINDERS are a request that does not exist yet. Raising it early would put a
-- 6pm reminder in `python -m jarvis pending` at 2pm and route it to whichever
-- room the user was in when they asked, so the row waits here and the scheduler
-- raises the request when it is due, asking presence THEN. `pending` -> `fired`
-- is a compare-and-swap, so two schedulers cannot both remind you.
CREATE TABLE reminders (
  id TEXT PRIMARY KEY,
  text TEXT NOT NULL,
  due_at TEXT NOT NULL,              -- UTC, jarvis.ids.now() shape, so it sorts
  state TEXT NOT NULL DEFAULT 'pending',
  actor TEXT NOT NULL,
  channel TEXT NOT NULL,
  request_id TEXT REFERENCES requests(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK (length(trim(text)) > 0),
  CHECK (state IN ('pending', 'fired', 'done', 'cancelled'))
);
CREATE INDEX reminders_due ON reminders(state, due_at);
