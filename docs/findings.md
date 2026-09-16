# Established findings

Research phase, 16 September 2026. Seventeen agents across four phases: four deep reads of the reference
build (FatihMakes/Mark-LIII, ~21k lines of Python), six externally-researched briefs each adversarially
fact-checked by a second agent, then a completeness critic. Roughly 2.5M tokens.

**Read this before disagreeing with [`architecture.md`](architecture.md).** Several obvious designs are
refuted here by evidence, including some that earlier passes of this same research asserted confidently and
that review overturned. Where something could not be established it says so rather than guessing.

Confidence conventions: claims marked REFUTED were checked against primary sources and found wrong. Claims
marked UNVERIFIED are load-bearing and still unproven — each has a spike in [`roadmap.md`](roadmap.md).

## THE GOOD NEWS (empirically verified by running the real CLI v2.1.273 + claude-agent-sdk 0.2.153)
The voice plan-mode loop — the core of the build sheet — is DIRECTLY SUPPORTED, not a hack.
- `AskUserQuestion` is a real tool call routed to the Agent SDK's `can_use_tool` callback.
- Host answers with `PermissionResultAllow(updated_input={**input_data, "answers": {question_text: label_or_list}})`.
- `input["questions"]` is an ARRAY; each has `header`, `question`, `options[]{label,description}`, `multiSelect`.
- multiSelect -> return a LIST of labels. "None of these" -> put the user's raw transcribed words in answers[question].
- NEVER set `response` alongside `answers` — it silently discards the answers.
- `ExitPlanMode` arrives the same way with `{"plan": ...}`; deny with a message to send Claude back to planning.
- Limits: 1-4 questions, 2-4 options each. Does not reach `can_use_tool` from subagents.
- `askUserQuestionTimeout` setting: 60s / 5m / 10m. Default: blocks indefinitely.
- PreToolUse hook can return `defer` -> process exits with stop_reason `tool_deferred`; resume later re-fires the question. This is the "Jarvis calls you when you're away" primitive.
- `rewind_files(user_message_id)` is the real undo primitive.
- Hooks `TaskCompleted` / `Stop` / `Notification` are the "task finished, go find the user" primitive.
- Use `ClaudeSDKClient` (claude-agent-sdk >=0.2.153,<0.3), NOT raw `claude -p` stream-json: a bare -p run has NO permission host and can never receive an AskUserQuestion. This was the single most expensive wrong turn available and it was caught.
- `model=` and `effort=` are per-session options -> "use Opus 5 with high effort" maps directly. NOTE `high` is the DEFAULT; the levels that change behaviour are low/medium/xhigh/max.
- `permission_mode="dontAsk"` DENIES AskUserQuestion -> the phone channel must run in `plan` mode.

## THE BUILD SHEET'S PINS ARE STALE
- There is no current "Gemini 3.1 Flash Live". Current: `gemini-3.8-live` (default voice agent),
  `gemini-3.8-live-extended-thinking`, `gemini-3.5-transcribe-live`, `gemini-3.5-live-translate-preview`.
  The reference build pins `models/gemini-3.1-flash-live-preview` (legacy) and `google-genai>=2.8.0` (PyPI is at 2.23.0).
  Pin `google-genai>=2.23,<3`. Native-audio vs half-cascade is a historical 2.x distinction — non-decision now.
- Audio: raw LE int16 mono PCM, 16 kHz in / 24 kHz out. Tag as `audio/pcm;rate=16000`.
- `FunctionResponseScheduling.INTERRUPT` exists — correct for "Claude Code just asked a question, stop talking and read it".
- Session resumption (`SessionResumptionConfig`) + `ContextWindowCompression(sliding_window)` are mandatory for long calls.
- Telephony audio: resample 8 kHz mu-law -> 16 kHz LOCALLY with a real resampler; do not tag rate=8000 and let the server do it.
  Either disable server VAD (`automatic_activity_detection.disabled=True`) and drive with local Silero/WebRTC VAD,
  or tune it LOW/LOW with prefix_padding~300ms, silence_duration~800ms. Server VAD tuned for a clean mic false-triggers on line noise.
- Google ships an official reference: `gemini/sample-apps/gemini-live-telephony-app` in GoogleCloudPlatform/generative-ai (Twilio Media Streams + FastAPI). READ IT.
- REFUTED: `speech_config.language_code` for Turkish. Native-audio models choose language automatically. Turkish must be
  driven by a Turkish system instruction, and MUST be verified empirically. Requirement 3's Turkish call has no confirmed mechanism.
- Pricing ~$0.005/min in, $0.018/min out (third-party source; primary was egress-blocked).
- UNVERIFIED and load-bearing: exact session limits for 3.8, and the CONCURRENT session ceiling (sources conflict 3 / 1000 / 5000).
  The concurrency answer decides whether a desk conversation and an outbound restaurant call can coexist at all.

## FIVE STRUCTURAL PROBLEMS IN THE REFERENCE BUILD (all verified in source)
1. NO VERBATIM SPEECH PATH. Everything Jarvis "says" is injected as a fake USER turn (`send_client_content`) and paraphrased
   by Gemini. `speak()` at main.py:655. So: the read-back of a tidied prompt is itself paraphrased, and plan-mode option
   labels — which the user answers by number and which must not be reordered, translated or dropped — cannot be guaranteed.
   The one requirement whose entire point is fidelity runs over a channel that cannot promise it.
   core/tts.py (442 lines) is the only verbatim engine in the tree and is DEAD CODE nothing imports.
2. TOOLS ARE AWAITED INLINE IN THE `session.receive()` LOOP (main.py:1115). A long-running Claude Code job makes the whole
   assistant deaf and mute, and `speak()` from inside a running action is emitted but inaudible until the action returns.
   Actions are SYNCHRONOUS handlers returning one string (core/action_loader.py). Driving Claude Code does not fit this contract.
3. ONE `self.session`, ONE `out_queue`, no source tagging. Voice is baked in at connect time and changing it forces a
   reconnect that DELIBERATELY DISCARDS the resumption handle — so placing a Turkish restaurant call in another voice
   destroys the user's desk conversation.
4. NO BARGE-IN. The mic is hard-gated off while Jarvis speaks, so server VAD can never fire. That kills "next"/"skip to X"
   briefing navigation and the spoken kill switch in one stroke.
5. confirm.py is HUD-only with ONE global pending slot and throws away the confirmed action's result; undo.py is a volatile
   in-RAM list of closures. Neither survives a restart, a second channel, or a remote side effect (a pushed commit, a
   created repo, a placed call, a sent screenshot).
Also: DEAD CODE — core/tts.py, core/stt.py, core/llm_client.py are imported by nothing. llm_client's `_SENT_END` sentence
splitter (line 31, 488-586) is exactly the shape needed to chunk Claude Code stdout into speakable sentences.
Also: the reference `.gitignore`'s secret patterns are SILENTLY INERT (verified with `git check-ignore`) — `git add -A`
publishes the Gemini key. All settings including `gemini_api_key` live in plaintext `config/api_keys.json`.
Qt blast radius is one file; a `HeadlessUI` implementing the `JarvisUI` facade (ui.py:4526-4687) is feasible — BUT
`confirm.request` HARD-REFUSES when no UI is bound, and actions receive the PyQt facade directly as `ctx["player"]`,
so going headless silently makes every confirmation impossible.

## TELEPHONY
- Recommended: LiveKit Agents + livekit-plugins-google RealtimeModel, worker on the home PC. The worker dials OUT over wss
  to register for jobs, so there is no inbound port, no tunnel, no VPS relay, no exposed surface on a Turkish residential line.
  The plugin hardcodes 16k in / 24k out and resamples via `rtc.AudioResampler` (verified in source).
- Twilio ConversationRelay is definitively WRONG here: it is a TEXT protocol that synthesises speech with its own TTS,
  which would reduce Gemini Live to a text LLM and lose the native voice the build sheet asks for.
- REFUTED: "LiveKit free tier covers it." The included US number is INBOUND-ONLY and US-only. Every outbound flow in the
  build sheet (call-me-back, plan-mode decisions, restaurant calls) needs a paid BYO SIP trunk (Twilio/Telnyx) — a carrier
  account, a paid number (~$1.15/mo), and a regulatory bundle. Three separate meters: agent minutes, LiveKit SIP minutes, carrier minutes.
- COLLISION NOBODY NOTICED: `google.realtime.RealtimeModel` CREATES AND OWNS its own Gemini Live session inside the LiveKit
  worker. So the phone is not a second audio source into the desk session — it is a SECOND ASSISTANT with its own connection,
  voice, system prompt and tool registry, which cannot see the desk's memory, actions, in-flight Claude Code job or activity log.
- Missed alternative worth weighing: Pipecat (open source) with `TwilioFrameSerializer` — handles mu-law/resampling on the raw
  Twilio path, undercutting the main argument for LiveKit. Also LiveKit Connectors (bridges Twilio Programmable Voice with no trunk).
- Turkish number: "closed to individuals" was generalised from ONE operator (Verimor). Netgsm's published flow appears to accept
  individuals via T.C. Kimlik No + e-Devlet. Unverified. Worth 30 minutes before accepting an international number.
- Screenshots: Telegram bot, `sendDocument` NOT `sendPhoto` (sendPhoto re-encodes to JPEG and smears terminal text). MMS to
  Turkish handsets from an international number is effectively dead. Telegram could be promoted from a screenshot sink to the
  primary remote-control channel, leaving PSTN only for the two things only PSTN does.
- Legal: the alarming framing was OVERTURNED. EU AI Act Art. 50 binds providers not deployers; Art. 2(10) and KVKK Art. 28
  personal/household exemptions likely put a one-user assistant out of scope. Disclosure is an ethics call, not a €15M one.

## CLOUD MODE
Claude Code cloud sessions are a PRODUCT SURFACE, NOT AN API — create-and-forget only (`claude --cloud`, routines `/fire`),
no live streaming read path (`--teleport` is a post-hoc read). Plan mode needs a bidirectional readable stream, so cloud
sessions cannot carry the core feature. Managed Agents has full create/send/SSE-read but is a different harness.
Options: (a) cut cloud mode from v1 and ship a classifier stub that always answers "local"; (b) same Claude Code binary on a
small VPS driven over SSH; (c) Anthropic's own documented pattern — plan LOCALLY where the permission host exists, commit
the plan, then `claude --cloud "execute the plan in docs/plan.md"`.

## BRIEFING
One SQLite file (WAL), four cursors, APScheduler 3.11.3 + SQLAlchemyJobStore in the same DB. All timestamps UTC; Europe/Istanbul
(+03, no DST) exists only at the moment Jarvis speaks.
- Gmail: `gmail.readonly`; `users.history.list` with a stored historyId, 404 -> full resync. NOT a `q=after:` date query.
  Triage: Gmail's own IMPORTANT marker is UNUSABLE for this user — measured live: 9,210 inbox messages, 7,807 unread,
  100% of yesterday's arrivals bulk marketing. Use an LLM pass over headers+snippets.
- GitHub: one `GET /search/issues?q=user:<login> is:issue created:>=<cursor>` covers every owned repo. Search is a separate
  rate bucket (30/min authenticated). GraphQL is a missed alternative.
- YouTube: `commentThreads.list` with `allThreadsRelatedToChannelId` — 1 quota unit against 10,000/day. Polling 4x/day costs 4 units.
  Replies need a separate `comments.list?parentId=`.
- Section 1 "project status" HAS NO IDENTIFIED DATA SOURCE. It is the first thing heard every morning and nobody defined it.

## SECURITY / OPS
- `keyring` (OS keychain) + run as a USER service tied to graphical-session.target. Jarvis needs mic and speakers, so it must
  run inside a logged-in desktop session anyway — which is exactly when the keychain is unlocked. Make that explicit, not accidental.
  Headless fallback is `keyrings.cryptfile` + systemd `LoadCredentialEncrypted=`. Probe at startup and fail LOUDLY; never
  let it degrade to a plaintext backend.
- CONTRADICTION TO RESOLVE: `--bare` (the hardening recommendation) explicitly does NOT read `CLAUDE_CODE_OAUTH_TOKEN`, so
  hardening silently switches the project to metered API billing. Max-subscription-vs-API-key decides the spend tracker's
  UNIT (dollars vs rate-limit windows), whether cloud sessions are available, and whether `--bare` can be used at all.
- `permissions.blockReadsOutsideWorkingDirectories` is what actually fences the file tools. Bash deny rules are leaky
  (`Bash(rm *)` does not match `/bin/rm`). Durable controls are scoped credentials and sandboxing, not deny lists.
- GitHub token: `delete_repo` OFF — which means "undo the repo you just made" is impossible by design. Nobody reconciled that
  with the confirm+undo requirement.
- PIN: 8 digits, `hmac.compare_digest` against a hash, 3 tries/call, escalating lockout. Caller-ID is a language hint, never authorization.
- `max_budget_usd` is a CLIENT-side circuit breaker from estimated costs, not a server-enforced cap.
- Activity log: SQLite WAL at `~/.local/share/jarvis/activity.db`, append-only, redaction at write time against the literal
  keyring values, hash chain for tamper-evidence. Keep rows forever (spend totals), null `detail` after 90 days.
  Exclude `~/.claude/projects/**` from backups — those transcripts hold everything the model ever saw, unredacted.

## LICENSE
Reference is CC BY-NC 4.0. NonCommercial binds every derivative FOREVER — any later monetisation needs a separate licence
from FatihMakes. Attribution conditions are hard requirements: retain the notice, state the licence, link the original,
and explicitly indicate modification (CHANGES.md). Must be settled BEFORE the first commit to a public repo.
Counterpoint: the session-lifecycle logic actually worth copying is ~200 lines, now documented as contracts in recon,
and the plan rewrites `JarvisLive` anyway — so a clean-room build is a genuine option rather than a sacrifice.

## THE STALL RISK (the critic's central warning)
Stage 2 will build a single-process, desk-shaped assistant — one session, one queue, tools inline, the UI object handed to
every action, the Claude Code driver held open by an in-process callback — and it will work beautifully at the desk, because
nothing in stage 2 requires otherwise. Then stage 3 breaks every assumption at once: the phone is a second assistant in
another process, defer/resume must cross a process boundary only ever tested in-process, the confirm gate hard-refuses
without a HUD, the DTMF kill switch lives in a different process from the thing it must kill, and presence detection — which
gates "speaks at the desk, calls away" — has no mechanism at all. Four weeks of re-plumbing stacked on an external telephony
dependency with a KYC lead time, with a working desk demo making it feel like the hard part is already done.
The fix is to put the session-agnostic job registry and event bus in at stage 2, not stage 3.

## THE CHEAPEST SPIKE (half a day, two files, no telephony, no money, no Gemini)
Prove a Claude Code plan-mode question can be answered by a DIFFERENT PROCESS, and survives defer/resume across process death.
`driver.py`: ClaudeSDKClient, permission_mode="plan", `can_use_tool` that serialises the `questions` array to a Unix socket,
blocks on the reply, returns PermissionResultAllow(updated_input={**input_data, "answers": {...}}). PreToolUse hook writes a
SQLite row and, under DEFER=1, returns `permissionDecision: "defer"`.
`answerer.py`: 40-line socket client that prints question+options and sends a label back. Stands in for the phone leg.
Three scenarios: single-select; multi-select with a list and a free-text "none of these"; then DEFER=1 -> exit with
stop_reason tool_deferred -> KILL the process -> wait 10 min -> fresh process with resume=<session_id> -> confirm the
question re-fires and the answer lands.
That socket IS the event bus whose absence is the stall risk.
