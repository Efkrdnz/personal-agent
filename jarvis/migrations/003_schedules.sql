-- 003_schedules.sql — the thing that wakes up, in a table you can read at 2am.
--
-- WHY A TABLE AND NOT APSCHEDULER: docs/adr/0011-scheduler-is-a-table.md. The short
-- form is that APScheduler 3.x's SQLAlchemyJobStore stores each job as a PICKLE of a
-- callable, which cannot be read with the sqlite3 CLI when the briefing did not
-- arrive and breaks when a function moves module; and running two schedulers against
-- one job store is explicitly unsupported, which is exactly the failure the claim
-- columns below exist to make impossible.
--
-- TIME. Every timestamp here is UTC in jarvis.ids.now()'s exact shape, so it sorts
-- lexicographically against every other timestamp in the file. `at_local` is the one
-- piece of local wall clock in the database and it has to be: the daemon may run on a
-- box in another zone, and "10:00" then still has to mean 10:00 where the user is.
-- Turkey is UTC+3 with no DST, which is easy to be casual about right up until it is
-- not, so the zone is stored per row rather than assumed.
CREATE TABLE schedules (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,              -- 'morning_briefing'
  fires TEXT NOT NULL,                    -- handler key, e.g. 'briefing_gate'
  payload TEXT NOT NULL,                  -- JSON, handed to the handler verbatim
  at_local TEXT NOT NULL,                 -- 'HH:MM' WALL CLOCK in tz, never UTC
  tz TEXT NOT NULL,                       -- IANA name, e.g. 'Europe/Istanbul'
  enabled INTEGER NOT NULL DEFAULT 1,
  -- Later than this and the fire is DROPPED rather than delivered. A briefing that
  -- arrives at 16:00 is not a late briefing, it is a wrong one.
  grace_s INTEGER NOT NULL DEFAULT 3600,
  next_run_at TEXT NOT NULL,              -- UTC. The promise: "I owe you this fire"
  last_due_at TEXT,                       -- the occurrence the last fire was FOR
  last_fired_at TEXT,                     -- when it actually happened
  last_late_s REAL,                       -- the difference, so the log cannot lie
  last_outcome TEXT,                      -- fired|missed|error
  -- THE POINTER, SERVER-SIDE. The request this schedule last raised; a dropped
  -- channel resumes from this row rather than from anything held in a process.
  last_request_id TEXT REFERENCES requests(id),
  fire_count INTEGER NOT NULL DEFAULT 0,
  missed_count INTEGER NOT NULL DEFAULT 0,
  -- Two processes must not both brief you. Claim, fire, then advance next_run_at:
  -- a lease rather than a straight advance, so a daemon killed between claiming and
  -- asking loses the lease and not the morning.
  claimed_by TEXT, claim_expires_at TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  -- Both halves, because the obvious GLOB only constrains the MINUTES: with a
  -- single '[0-2][0-9]' the hour 25 passes, and a row nobody can compute an
  -- occurrence for is exactly the row a 2am edit produces. This is the same set
  -- of strings jarvis.schedule.recurrence.at_local_parts accepts, so the CLI and
  -- the Python door agree about what a time of day is.
  CHECK (at_local GLOB '[01][0-9]:[0-5][0-9]' OR at_local GLOB '2[0-3]:[0-5][0-9]'),
  CHECK (enabled IN (0,1)),
  CHECK (grace_s >= 0)
);
CREATE INDEX sched_due ON schedules(enabled, next_run_at);
