-- 001_init.sql — the whole durable state of Jarvis, in one SQLite file.
--
-- Connection-scoped pragmas (journal_mode, synchronous, foreign_keys,
-- busy_timeout) are NOT here: foreign_keys and busy_timeout take effect per
-- connection, so setting them once in a migration would silently do nothing for
-- every process that opens the file later. They live in jarvis/db.py:connect().
--
-- Requires SQLite >= 3.35 for UPDATE ... RETURNING, asserted at connect().
-- requires SQLite >= 3.35 for UPDATE ... RETURNING (asserted at connect())

-- ───────────────────────── 1. THE BUS *IS* THE ACTIVITY LOG ─────────────────────────
-- Low-rate by construction (Claude Code output goes to the data plane), so "keep rows
-- forever" is honest: ~300 rows/day ≈ 110k/year. Hash-chained. Redacted at write.
CREATE TABLE events (
  seq        INTEGER PRIMARY KEY AUTOINCREMENT,  -- total order == commit order
  id         TEXT NOT NULL UNIQUE,
  ts         TEXT NOT NULL,                      -- RFC3339 UTC millis 'Z'
  kind       TEXT NOT NULL,
  actor      TEXT NOT NULL,   -- 'desk'|'phone:<call>'|'telegram'|'scheduler'|'runner:<job>'|'user'
  job_id     TEXT, request_id TEXT, channel_id TEXT, effect_id TEXT,
  idem_key   TEXT NOT NULL UNIQUE,               -- republish is a no-op
  payload    TEXT NOT NULL,                      -- JSON, secrets replaced at write time
  redacted   INTEGER NOT NULL DEFAULT 0,
  detail_nulled_at TEXT,                         -- payload detail nulled after 90 days
  prev_hash  TEXT NOT NULL, hash TEXT NOT NULL
);
CREATE INDEX ev_kind_ts ON events(kind, ts);
CREATE INDEX ev_job ON events(job_id, seq);
CREATE INDEX ev_req ON events(request_id, seq);

-- Per-consumer cursors. Doubles as the DATA-PLANE byte offset:
-- id='stream:<job_id>:<channel_id>', last_seq=byte offset, meta={"generation":N}
CREATE TABLE consumers (
  id TEXT PRIMARY KEY, last_seq INTEGER NOT NULL DEFAULT 0,
  meta TEXT, updated_at TEXT NOT NULL
);

-- ───────────────────────── 2. JOB REGISTRY ─────────────────────────
CREATE TABLE jobs (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL,       -- claude_code|outbound_call|briefing|repo_setup|undo
  title TEXT NOT NULL,                           -- spoken: "the todo app build"
  state TEXT NOT NULL,                           -- queued|starting|running|blocked|deferred|parked
                                                 -- |finishing|done|failed|killed|orphaned
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  created_by TEXT NOT NULL, host TEXT NOT NULL DEFAULT 'local',
  cwd TEXT, repo TEXT, branch TEXT,
  cc_session_id TEXT,                            -- a UUID *we* generate and own
  model TEXT, effort TEXT, permission_mode TEXT,
  prompt_text TEXT, prompt_request_id TEXT,      -- the read-back that was confirmed
  -- liveness: boot_id + start-ticks defeat PID reuse after a reboot
  pid INTEGER, pgid INTEGER, boot_id TEXT, proc_start_ticks INTEGER, heartbeat_at TEXT,
  stream_path TEXT, stop_reason TEXT, result_summary TEXT,
  resume_policy TEXT NOT NULL DEFAULT 'auto',    -- auto|manual|never
  resume_count INTEGER NOT NULL DEFAULT 0,
  blocked_request_id TEXT, blocked_since TEXT,   -- >15min => appears in the morning briefing
  kill_epoch INTEGER NOT NULL DEFAULT 0,
  CHECK (permission_mode IS NULL OR permission_mode <> 'dontAsk')  -- dontAsk DENIES AskUserQuestion
);
CREATE INDEX jobs_state ON jobs(state, updated_at);

-- ───────────────────────── 3. REQUESTS — the unified gate ─────────────────────────
CREATE TABLE requests (
  id TEXT PRIMARY KEY,
  job_id TEXT REFERENCES jobs(id),
  kind TEXT NOT NULL,        -- plan_question|exit_plan|tool_permission|confirm_effect
                             -- |readback|briefing_gate|free_text
  state TEXT NOT NULL,       -- pending|answered|consumed|expired|cancelled|superseded
  urgency TEXT NOT NULL DEFAULT 'normal',        -- low|normal|high|critical
  dedupe_key TEXT NOT NULL,  -- sha256(job_id ‖ tool_name ‖ canonical_json(tool_input))
  attempt INTEGER NOT NULL DEFAULT 1,            -- a GENUINE re-ask gets attempt N+1
  tool_use_id TEXT,
  short_label TEXT NOT NULL,                     -- <=3 words: "bash approval", "push to main"
  presentation TEXT NOT NULL,                    -- JSON Presentation (verbatim flag + items)
  payload TEXT NOT NULL,                         -- raw tool input, VERBATIM, never mutated
  reversibility TEXT,                            -- confirm_effect only
  created_at TEXT NOT NULL, expires_at TEXT,
  escalate_after_s INTEGER NOT NULL DEFAULT 90,
  on_timeout TEXT NOT NULL DEFAULT 'defer',      -- defer|deny|default|escalate  <- typed outcome,
                                                 -- never an implicit hang
  answer TEXT, answered_at TEXT, answered_by TEXT, answer_mode TEXT,  -- voice|dtmf|button|hud|timeout
  consumed_at TEXT,
  UNIQUE(job_id, dedupe_key, attempt)
);
CREATE INDEX req_open ON requests(state, created_at);
CREATE UNIQUE INDEX req_tuid ON requests(tool_use_id) WHERE tool_use_id IS NOT NULL;

-- One row per (request, channel, attempt). Materialised by the router's ladder.
CREATE TABLE deliveries (
  id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL REFERENCES requests(id),
  channel_kind TEXT NOT NULL, channel_id TEXT, attempt INTEGER NOT NULL DEFAULT 1,
  due_at TEXT NOT NULL,
  state TEXT NOT NULL,       -- scheduled|claimed|presented|answered|released|failed|skipped
  claimed_by TEXT, claim_expires_at TEXT, presented_at TEXT, error TEXT,
  UNIQUE(request_id, channel_kind, attempt)
);
CREATE INDEX del_due ON deliveries(state, due_at);

-- ───────────────────────── 4. CHANNELS ─────────────────────────
CREATE TABLE channels (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL,   -- desk|phone|telegram|hud|scheduler|runner|cli
  state TEXT NOT NULL, pid INTEGER, boot_id TEXT, poke_addr TEXT,
  caps TEXT NOT NULL,        -- JSON Capabilities; caps.verbatim=false => INELIGIBLE for plan_question
  identity TEXT,             -- {"chat_id":…} | {"call_id":…,"from_hash":…}
  attached_at TEXT NOT NULL, last_heartbeat_at TEXT NOT NULL, detached_at TEXT
);
CREATE INDEX ch_live ON channels(state, kind);

-- ───────────────────────── 5. EFFECTS = the undo ledger ─────────────────────────
CREATE TABLE effects (
  id TEXT PRIMARY KEY, job_id TEXT REFERENCES jobs(id), ts TEXT NOT NULL,
  kind TEXT NOT NULL,        -- fs.edit|git.commit|git.push|github.repo_create|telegram.send_document
                             -- |phone.call|github.issue_comment|…
  summary TEXT NOT NULL,                         -- spoken sentence, past tense
  reversibility TEXT NOT NULL,                   -- reversible|compensatable|irreversible
  provider_ref TEXT,                             -- {"sha":…,"full_name":…,"message_id":…,"call_sid":…}
  undo_plan TEXT,                                -- DECLARATIVE JSON. Never a closure.
  undo_deadline TEXT,                            -- e.g. Telegram's 48h delete window
  state TEXT NOT NULL DEFAULT 'applied',         -- applied|undone|undo_failed|expired
  confirmed_by_request_id TEXT REFERENCES requests(id),
  undone_at TEXT, undo_effect_id TEXT REFERENCES effects(id), undo_error TEXT
);
CREATE INDEX eff_recent ON effects(ts DESC);

-- ───────────────────────── 6. COMMANDS (kill switch) ─────────────────────────
CREATE TABLE commands (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL,
  verb TEXT NOT NULL,        -- stop_all|kill|interrupt|pause|resume|hangup|nav|reload
  target_kind TEXT NOT NULL, -- all|job|channel|process
  target_id TEXT, args TEXT, issued_by TEXT NOT NULL,
  expires_at TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE command_acks (
  command_id TEXT NOT NULL REFERENCES commands(id), actor TEXT NOT NULL,
  ts TEXT NOT NULL, result TEXT, PRIMARY KEY (command_id, actor)
);
CREATE TABLE kill_epoch (id INTEGER PRIMARY KEY CHECK(id=1), epoch INTEGER NOT NULL DEFAULT 0);

-- ───────────────────────── 7. PRESENCE ─────────────────────────
CREATE TABLE presence_signals (
  source TEXT PRIMARY KEY,   -- idle|lock|wakeword|utterance|probe|override|telegram|call|geofence
  value TEXT NOT NULL, ts TEXT NOT NULL, ttl_s INTEGER NOT NULL
);
CREATE TABLE presence_state (
  id INTEGER PRIMARY KEY CHECK(id=1),
  state TEXT NOT NULL, since TEXT NOT NULL, confidence REAL NOT NULL,
  reachable TEXT NOT NULL,                       -- ["desk","telegram","phone"]
  reason TEXT NOT NULL,                          -- spoken on "where do you think I am?"
  updated_at TEXT NOT NULL
);
CREATE TABLE presence_override (
  id INTEGER PRIMARY KEY CHECK(id=1),
  mode TEXT,                                     -- present|away|dnd|desk_only|NULL
  until TEXT, set_by TEXT, set_at TEXT
);

-- ───────────────────────── 8. OUTBOX — side effects leave here ─────────────────────────
CREATE TABLE outbox (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, due_at TEXT NOT NULL,
  op TEXT NOT NULL,          -- speak|telegram.send|telegram.delete|phone.dial|github.*|git.*
  args TEXT NOT NULL, idem_key TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL,       -- pending|claimed|inflight|done|failed|needs_human
  at_most_once INTEGER NOT NULL DEFAULT 0,       -- 1 for phone.dial. NEVER auto-retried.
  attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,
  claimed_by TEXT, claim_expires_at TEXT, result TEXT, error TEXT,
  effect_id TEXT REFERENCES effects(id)
);
CREATE INDEX ob_due ON outbox(state, due_at);

-- ───────────────────────── 9. SPEND — three units, one threshold ─────────────────────────
CREATE TABLE spend (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, job_id TEXT,
  provider TEXT NOT NULL,    -- claude_code|gemini|carrier|livekit
  unit TEXT NOT NULL,        -- usd_est | rate_window_pct | gemini_sec | call_min_try | tokens
  amount REAL NOT NULL,
  usd_equiv REAL,            -- NULL when NOT CONVERTIBLE. That NULL is spoken aloud.
  estimated INTEGER NOT NULL DEFAULT 1,
  note TEXT
);
-- SpendStatus = {priced_usd, threshold_usd, pct, by_provider,
--                unpriced: ["claude_code (Max subscription: rate-limit windows, not dollars)"]}
-- so a $0 running total is never mistaken for free.

CREATE TABLE cursors (name TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
-- gmail_history_id, github_issues_since, youtube_page, briefing_last_run
-- APScheduler's SQLAlchemyJobStore points at sqlite:///this same file.
