-- 002_briefing.sql — the briefing's SERVER-SIDE POINTER, and the exactness set.
--
-- The whole reason these tables exist is that a briefing must survive the
-- channel it is being said on. Where we are in the four sections is a row, not
-- a variable in the desk process and not a keyboard state on Telegram, so a
-- dropped channel resumes at section three instead of starting over or losing
-- the rest of it.
--
-- Everything else a briefing needs is already in 001: the question is a row in
-- `requests`, the ladder is `deliveries`, "since when" is `cursors`. These three
-- tables add only what 001 has no place for.

-- One run. `run_key` is the Istanbul date, which is what makes a reboot at 09:50
-- and a scheduler that fires twice produce ONE briefing rather than two: the
-- second start finds this row instead of creating a rival with its own pointer.
CREATE TABLE briefings (
  id         TEXT PRIMARY KEY,
  run_key    TEXT NOT NULL UNIQUE,
  state      TEXT NOT NULL,                    -- running|finished|stopped
  position   INTEGER NOT NULL DEFAULT 0,       -- THE POINTER: index into briefing_sections
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  finished_at TEXT,
  created_by TEXT NOT NULL
);

-- The ordered sections of one run. `content` is the composed section, stored at
-- compose time, so "repeat" repeats THE SAME WORDS and a re-attach mid-section
-- does not silently say something else because the world moved on in between.
-- `request_id` is the section's own row in `requests`: a section IS a request,
-- and navigation is the answer to it.
CREATE TABLE briefing_sections (
  briefing_id TEXT NOT NULL REFERENCES briefings(id),
  position    INTEGER NOT NULL,
  key         TEXT NOT NULL,                   -- projects|inbox|issues|comments
  state       TEXT NOT NULL,                   -- pending|delivered|skipped
  content     TEXT,                            -- JSON SectionContent
  request_id  TEXT REFERENCES requests(id),
  composed_at TEXT,
  delivered_at TEXT,                           -- set ONLY when it really reached a human
  PRIMARY KEY (briefing_id, position)
);
CREATE INDEX bs_req ON briefing_sections(request_id);

-- Exactness where exactness is cheap. The cursors in `cursors` are INCLUSIVE on
-- purpose (see jarvis/reconcile.py: repeating beats dropping), which means the
-- boundary item comes back on the next run. This set is what removes it — and
-- only it. No cursor is ever moved past an item that was not delivered, so the
-- rule stays "no unbounded repetition and no silent loss" rather than
-- "no repetition", which would cost losses.
CREATE TABLE briefing_seen (
  source        TEXT NOT NULL,                 -- the cursor name the item belongs to
  item_id       TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  PRIMARY KEY (source, item_id)
);
CREATE INDEX bseen_age ON briefing_seen(first_seen_at);
