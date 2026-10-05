-- 004_hearing.sql — the words this user says that a recogniser keeps getting wrong.
--
-- WHY A TABLE. A speech recogniser trained on a general population hears "coat"
-- or "court" when a Turkish-accented speaker says "quote": the /kw/ cluster
-- reduces to /k/ and the vowel lands nearer "coat" than "quote". That is not a
-- bug anybody upstream will fix for one user, so the fix is personal, and a
-- personal fix that lives in a process dies with it. These rows are what the
-- desk, the phone leg and the CLI all read, and what `python -m jarvis hearing`
-- prints at 2am when a build was filed as a "coat widget".
--
-- The built-in seeds ("quote", "Claude") live in jarvis/hearing.py, NOT here:
-- this file is frozen once applied, and seeds are exactly the thing that gets
-- better with use. A row here EXTENDS a seed with the same term; it never has
-- to restate it.
CREATE TABLE lexicon (
  term TEXT PRIMARY KEY,                 -- what the user MEANS, lower-case: 'quote'
  heard_as TEXT NOT NULL DEFAULT '[]',   -- JSON list: what the recogniser writes instead
  cues TEXT NOT NULL DEFAULT '[]',       -- JSON list: words that make the term likely
  anti_cues TEXT NOT NULL DEFAULT '[]',  -- JSON list: words that make the heard word right
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK (term = lower(term) AND length(trim(term)) > 0),
  CHECK (json_valid(heard_as) AND json_valid(cues) AND json_valid(anti_cues))
);

-- Every time the user said what they meant. 'yes' is "I said quote, not coat";
-- 'no' is "no, I really did say coat" after a correction got it wrong. The
-- context is the sentence it happened in, because the cues a correction learns
-- are read out of these rows, and a count with no sentence teaches nothing.
CREATE TABLE hearing_fixes (
  id INTEGER PRIMARY KEY,
  heard TEXT NOT NULL,
  meant TEXT NOT NULL,
  verdict TEXT NOT NULL,
  context TEXT NOT NULL DEFAULT '',
  actor TEXT NOT NULL,
  at TEXT NOT NULL,
  CHECK (heard = lower(heard) AND meant = lower(meant)),
  CHECK (heard <> meant),
  CHECK (verdict IN ('yes', 'no'))
);
CREATE INDEX hearing_fixes_pair ON hearing_fixes(heard, meant);
