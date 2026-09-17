# Roadmap

**Honest total: about thirteen weeks of focused solo work**, with something genuinely usable at the end of
every stage. Two deliberate deviations from the build sheet's suggested order are marked and argued below.

Two dates matter more than the rest:
- **Day one:** start telephony procurement. It is the only thing with an external lead time, and from day one
  it runs underneath five stages of useful work instead of becoming the critical path at the worst moment.
- **Before the first commit:** settle the licence question. It is the only decision here that cannot be revisited.

---

## Status — 16 September 2026

Stage 0 and the stage-1 spine are built. What is actually true today, as opposed to planned:

| | |
|---|---|
| **Spike S1** | **PASS, run for real.** Cross-process plan-mode answering, and defer → SIGKILL → 180s gap → resume → answer replayed. Cost $0.12. See [`spikes/s0_crossproc/RESULTS.md`](../spikes/s0_crossproc/RESULTS.md). |
| **Probe S9(c)** | A `can_use_tool` callback survives a 3-minute block (measured). The 90-minute case has a probe: `tools/probe_long_block.py`. |
| **The spine** | Built: `bus`, `requests`, `jobs`, `reconcile`, `effects`, `presence`, `kill`, `ledger`, `spec`, `answers`, `secrets`, `config`, on `db`/`ids`/`clock`. Stdlib-only, integration-tested across connections. |
| **Stages 2–5** | The PARTS are built and tested: the Claude Code driver (proved live against the real CLI), the voice layer, Telegram, repo-first project creation, the scheduler and the briefing. **1,991 tests pass.** |
| **The front door** | `python -m jarvis doctor / secrets / config / status / tools / desk`. `doctor` names every gap and the command that closes it; [`docs/setup.md`](setup.md) covers the parts that happen in a browser. |
| **Not yet wired** | **Composition, everywhere.** An audit of all five entry points found four missing CALLERS, each one function: no `deliveries` row is ever written for a Claude Code question (so no channel can answer one), nothing consumes `repo_setup`, nothing consumes `briefing.started`, and nothing reads the event log. Plus: no command creates a `claude_code` job, and there is no wake word. See CLAUDE.md, 'Where things stand'. |
| **CI guards** | Secret patterns verified against real `git check-ignore`; a smoke alarm for CC BY-NC reference code. |

Three findings from S1 changed the plan and are recorded in `RESULTS.md`: `tool_use_id` is stable across
resume (so `dedupe_key` is a convenience, not a correctness mechanism); a `PreToolUse` hook **can** return
`defer` (the research's refutation was wrong for this build); and `AskUserQuestion` must never appear in
`allowed_tools`.

**One thing worth watching.** The spine is ~4,700 lines of code where the architecture claimed the
coordination layer would be "about 400 lines, not a framework". The API surface looks purposeful rather than
speculative, but this is the risk the design flagged against itself, and the two falsifiable tests at the end
of this document are what will settle it.

---

## Stage 0 first — nine spikes, two days

Nine cheap experiments, each killing a hypothesis whose failure would invalidate part of the design. Every
one has a defined failure branch, so a "no" is a fork in the plan rather than a dead end.

### S1 — Cross-process AskUserQuestion, and defer/resume across real process death

**Cost:** Half a day. No Gemini, no telephony, no money, no audio.

**Question.** Does a permission host exist at all, is the answer payload shape right against the real CLI, can a DIFFERENT PROCESS answer, and does defer/resume survive killing the driver and waiting ten minutes? Also: is `tool_use_id` identical when the question re-fires after a resume?

**Method.** spikes/s0_crossproc/driver.py — ClaudeSDKClient, permission_mode='plan', can_use_tool serialising input['questions'] to a Unix socket, blocking, returning PermissionResultAllow(updated_input={**input_data,'answers':{...}}) and NEVER setting 'response'. answerer.py — a 40-line socket client that prints the question and options and sends a label back. A PreToolUse hook that writes one row and, under DEFER=1, returns permissionDecision:'defer'. Three scenarios: single-select; multi-select with a list plus a free-text 'none of these'; then DEFER=1 → exit with stop_reason tool_deferred → KILL PROCESS A → wait ten real minutes → fresh process with resume=<session_id> → confirm the question re-fires and the answer lands. Add what the original spike does not test: a DIFFERENT cwd on resume, and assert whether tool_use_id is stable.

**If it fails.** If scenario 3 fails, R3's core flow needs a different design and we learn it on day one instead of month four — most likely falling back to block-only with Telegram as the sole away channel and no defer at all. If tool_use_id is NOT stable across resume, dedupe_key becomes load-bearing and the unique index on tool_use_id must become advisory.

### S2 — Does gemini-3.1-flash-tts-preview recite exactly?

**Cost:** 30 minutes and a few dollars.

**Question.** Can a dedicated TTS model read 200 adversarial label strings back byte-exact? If yes, Jarvis has ONE voice — the Live session's own timbre, since it shares the prebuilt voice names — and the whole two-voice debate ends.

**Method.** tools/fidelity_probe.py. 200 deliberately adversarial labels: near-duplicates (Postgres / PostgreSQL), versioned (Opus 5 / Opus 4.5), Turkish orthography (ığşçöü, dotless ı), camelCase identifiers, one option that is a prefix of another, digits that collide with the ordinals. Feed each with `contents='Read the following text exactly as written. Add nothing, omit nothing.'`, transcribe the returned PCM with gemini-3.5-transcribe-live in VERBATIM mode, score label_exact_recall AGAINST THE ASR NULL BASELINE produced by scoring Kokoro's own audio through the same path. Also assert the model ID exists on this account and returns 24kHz PCM16 mono.

**If it fails.** Ship two voices as designed — deliberate, legible, earcon-bracketed. This is the DEFAULT plan, not a fallback, so failure costs nothing but the chance at something nicer. Run it in week one anyway, because it is the only experiment whose result reorders the design. If the model ID 404s, fall back to gemini-2.5-flash-tts before concluding anything.

### S3 — Does FunctionResponseScheduling.SILENT keep the model quiet while putting labels in context?

**Cost:** 15 minutes.

**Question.** The entire read-options handoff rests on SILENT meaning 'add to context, do not trigger generation'. Does gemini-3.8-live honour it?

**Method.** Declare a `read_options` tool, have the model call it, return a large response with scheduling=SILENT, and assert (a) no audio follows and (b) the content IS in context by asking 'what was option two?' afterwards. Same script also tests the fallback: send_client_content with a model-role turn and turn_complete=False, and observe whether the session then sits waiting for more client content instead of responding to the user's next audio — which would look exactly like Jarvis going deaf right after reading a question aloud.

**If it fails.** Fall back to sending the option table as a send_client_content prefill BEFORE unmuting the uplink, which is the documented prefill pattern and involves no interleaving inside the recital window. If BOTH fail, the deterministic answer matcher still works against the original options array, but conversational follow-up about the options ('what's the difference between one and two?') becomes ungrounded — a real degradation of the requirement, and one we would know about in week one.

### S4 — Two concurrent gemini-3.8-live sessions, one Turkish, held for 45 minutes

**Cost:** About an hour and under a dollar.

**Question.** Four unverified load-bearing claims in one test: does the model ID exist on this account, what is the concurrent ceiling (sources conflict 3 / 1000 / 5000), do the session-limit and GoAway/resumption numbers hold for 3.8, and can Turkish output be controlled AT ALL given that speech_config.language_code was refuted for native-audio models?

**Method.** Open two sessions from one account, one with a Turkish-only system instruction. Hold one open for 45 minutes through the full GoAway/resumption/compression cycle. Read usage_metadata per modality throughout. Have a native speaker score twenty minutes of the Turkish output for language drift and for whether input transcription lands in the right language.

**If it fails.** Concurrency of 1 is already the shipped default (LiveLease capacity=1 with checkpoint-and-restore), so a low ceiling changes a config constant and nothing else. Turkish failing is the expensive one: stage 7 then ships its defined fallback — Jarvis drafts the call script, reads it to you verbatim, you dial — and R3's third-party half is honestly descoped rather than silently broken.

### S5 — AEC on the actual desk

**Cost:** One hour, on the real hardware. Do it in stage 0, before building anything on the assumption.

**Question.** Does echo cancellation clear 25dB ERLE in this room at this volume with these speakers, and how many times in 30 seconds would the barge-in chain false-fire on pure echo?

**Method.** tools/aec_bench.py, run where Jarvis will actually live. Test 1 single-talk: silent room, 30s of real Gemini output at normal listening volume, compute ERLE over high-far-energy frames after discarding 3s of convergence, count false barge-ins. Test 2 double-talk: 20 spoken barge-ins from the normal seat, log detection latency. Test 3: sweep three volumes and with the desk fan on. Also record erle_first_vs_last_third to detect clock drift.

**If it fails.** Walk the ladder: reposition, correct stream_delay_ms, force one device, OS-native AEC (PipeWire echo-cancel owns both clocks AND cancels other apps' audio), then a $40 wired headset — which is the RECOMMENDED DEFAULT regardless. Nothing in the feature list is lost: the kill switch keeps the hotkey and DTMF, briefing navigation keeps push-to-talk, the wake word is unaffected, and the phone leg never needed AEC.

### S6 — Install smoke test on the target Python, and duplex stream health

**Cost:** 50 minutes.

**Question.** Do the four load-bearing audio packages actually install and run on this interpreter, and does PortAudio give a trustworthy playback-to-capture delay on this device?

**Method.** pip install pywebrtc-audio sounddevice soxr sherpa-onnx, then `EchoCanceller(sample_rate=48000).process(zeros, zeros)`. Separately `pip install openwakeword` and CONFIRM IT FAILS on Linux with Python >=3.12 (its metadata hard-requires tflite-runtime, last wheels cp311), then confirm `--no-deps` plus onnxruntime works with inference_framework='onnx'. Then open sd.Stream at 48000/960/duplex for ten minutes with a passthrough callback: count XRun status flags, print median and stddev of (outputBufferDacTime - inputBufferAdcTime), and confirm capture and render are actually one card.

**If it fails.** If 48kHz is refused by EchoCanceller, drop the device rate to 16k and accept duller playback, or swap to livekit's rtc.AudioProcessingModule (same AEC3, thirty-line swap). If the PortAudio delay is negative, zero or wildly jittery, pass a fixed hint and let AEC3's own estimator work. If openWakeWord cannot be made to load, the wake word moves to sherpa-onnx KWS and S7 becomes the deciding test rather than a comparison. Do NOT pin the project to Python 3.11 for one package.

### S7 — Keyword spotter false-accept rate with the speakers on

**Cost:** Two hours passive plus thirty minutes active.

**Question.** How often does the kill phrase fire when nobody said it, and how often does it miss when somebody did? This decides whether v1 ships headset-only and it sizes the risk for the one control that must not misfire.

**Method.** Record two hours of the user's ordinary room audio and normal speech once, then replay it offline through sherpa-onnx KeywordSpotter with the KILL keyword file so the test is repeatable. Count false accepts per hour. Then 20 deliberate utterances at normal volume from the normal seat; count misses. Repeat for the NAV set with looser thresholds, and for the wake word with both openWakeWord and sherpa KWS. PASS: 0 false accepts in 2h, >=19/20 detected.

**If it fails.** Tighten the phrase (longer, more plosive, less like normal speech) before tightening the threshold, since a missed stop is worse than a spurious one. If it still fails with speakers on, that is the headset argument made twice. The hotkey and DTMF paths become load-bearing rather than belt-and-braces, and that must be written down and agreed rather than assumed.

### S8 — GitHub token capability matrix

**Cost:** 20 minutes.

**Question.** With the EXACT token Jarvis will use, which compensations actually work? The whole spoken undo wording for repo creation depends on the answer.

**Method.** On a throwaway repo: (a) confirm DELETE /repos/{owner}/{repo} returns 403 because delete_repo is deliberately off; (b) confirm PATCH /repos/{owner}/{repo} succeeds for {"archived":true}, for {"private":true} and for {"name":"zz-abandoned-x"}; (c) confirm POST /user/repos with {"private":true,"auto_init":false} and note the default-branch behaviour. Test against the live API — docs.github.com was egress-blocked during research, so the documented body is itself unverified.

**If it fails.** If archive or rename also fail, github.repo_create has NO compensation at all and the spoken line must change from 'I can archive and rename it' to 'nothing can be done about it' — which is a wording change, not a design change, but it is exactly the kind of thing that turns the honesty layer into a lie if it is not checked.

### S9 — Auth mode, idle time, and the 90-minute block

**Cost:** 20 minutes for (a) and (b); (c) runs unattended for 90 minutes.

**Question.** Three small things that each silently reshape a subsystem. Does ResultMessage report total_cost_usd under the OAuth token (dollars) or not (rate-limit windows)? Does idle_seconds() actually rise on this desktop, or is it pinned at 0 because we are on Wayland? And can can_use_tool block for 90 minutes and still have the answer land?

**Method.** (a) Run one trivial job under CLAUDE_CODE_OAUTH_TOKEN and one under ANTHROPIC_API_KEY; print ResultMessage.total_cost_usd and model_usage for each. (b) Print idle_seconds() every second and stop typing for three minutes; confirm it rises and is not constant zero; confirm the lock signal fires. (c) Start a runner, let it hit an AskUserQuestion, block for 90 minutes, then answer. Confirm the CLI subprocess and the upstream connection survive. Run it once more with askUserQuestionTimeout=60s set, to see exactly what Claude receives when a question auto-closes.

**If it fails.** If (a) reports no dollars, R5's ledger ships with unit='rate_window_pct' as the primary meter and SpendStatus.unpriced speaks the reason aloud — which is the honest answer anyway. If (b) is pinned at zero, the Wayland probe is wrong and presence degrades to 'unknown', which still escalates. If (c) fails, blocking stops being the default, defer becomes mandatory, and given S1's finding that defer is solo-only and sometimes dropped, that combination would need a real rethink — which is why it is worth 90 unattended minutes to know.

---

## The stages

## Stage 0 — The spike, the probes, and the carrier clock

**Effort:** 2 days (half a day spike, a day and a half of probes). Start telephony procurement the SAME DAY — it is the only thing with an external lead time, and from here it runs underneath five stages of useful work instead of becoming the critical path at the worst moment.

**Goal.** Kill the four hypotheses whose failure would invalidate the architecture, before a line is built on any of them — and start the one external dependency that the project cannot unblock itself.

**What ships.** A terminal in one process answering a REAL Claude Code plan-mode question asked by another process — single-select, multi-select with a list, and a free-text 'none of these' — then a defer, a process kill, a ten-minute gap, a resume, and the same answer landing. Useful on its own forever: it is what you run when the voice layer is broken. Plus RESULTS.md with eight measured numbers.

**Files.** spikes/s0_crossproc/{driver.py,answerer.py,RESULTS.md} — refactored IN PLACE into jarvis/{bus.py,db.py,requests.py,ids.py}, jarvis/migrations/001_init.sql, jarvis/cc/hooks.py. tools/{aec_bench.py,fidelity_probe.py,probe_auth_mode.py,probe_silent_scheduling.py,probe_live_concurrency.py}. tests/test_gitignore_effective.py. .gitignore, LICENSE (MIT), CREDITS.md, CONTRIBUTING.md, docs/adr/0001-0003.

**What it proves.** That a permission host exists at all (a bare `claude -p` never receives AskUserQuestion — the most expensive wrong turn available); the exact answer payload including that `response` must never accompany `answers`; that a DIFFERENT PROCESS can answer, which is the phone layer in miniature; that defer/resume survives genuine process death and a long gap; that defer is silently ignored when siblings are in the batch; whether SILENT function responses keep Gemini quiet while putting labels in context; whether ResultMessage reports total_cost_usd under the OAuth token (this decides whether R5's unit is dollars or rate-limit windows); and whether AEC on this desk clears 25dB.

**Done when.** Scenario 3 passes end to end with a real ten-minute gap and a real process kill. `git check-ignore -v` returns a rule for every secret pattern in CI. RESULTS.md records a number for each of the eight probes, including the ones that failed. A carrier account application is submitted or a decision to defer it is written into docs/adr/.

---

## Stage 1 — Base agent — two processes, two voices, one audio graph

**Effort:** 12-18 days. This is the honest number and it is roughly double what a first pass would estimate: Gemini Live plus a duplex audio graph plus device probing plus wake word plus spotter plus Kokoro/edge-tts with a Turkish voice plus keyring, ledger, memory, presence and a TUI is two to three weeks for one person. Saying 6-8 days here would be the plan's first lie.

**Goal.** A working voice assistant with the audio graph drawn correctly ONCE, the deterministic reader present from the start, and the spine already split across two processes.

**What ships.** 'Hey Jarvis' wakes it. A real Gemini Live conversation with working barge-in — interrupt it mid-sentence and it stops. It remembers things. It tells you what it did today. A Rich terminal pane shows the live session, the spend total with its unpriced meters spoken aloud, and the live ERLE number. You pick your microphone and speakers. Secrets are in the OS keyring and it refuses to start without them. And it can say a sentence WORD FOR WORD when it needs to — 'read me this file exactly' is demoable on its own.

**Files.** jarvis/{ids,clock,config,secrets,db,bus,requests,jobs,reconcile,ledger,memory,presence,kill,presenter,router}.py; jarvis/audio/{bus,micbus,legs,devices,vad,wake,spotter,turn}.py; jarvis/voice/{verbatim,engines,cache,chunk,router}.py; jarvis/live/{session,profiles,lease}.py; jarvis/tools/{registry,ctx}.py + builtin/{memory,spend,job_control,presence}.py; jarvis/apps/{dispatch,voice,cli}.py; deploy/jarvis-{dispatch,voice}.service.

**What it proves.** That gemini-3.8-live works on this account with this hardware; that the SILENT-response context mechanism actually produces a conversation where 'the second one' resolves; that duck-confirm barge-in works in this room; that LiveSession survives a real GoAway with its resumption handle; that the keyring path holds; and — architecturally — that jarvis-dispatch being the parent rather than the voice app costs nothing while it is doing almost nothing.

**Done when.** You can hold a five-minute conversation, interrupt it three times, and have it survive one forced reconnect. `say_verbatim` speaks a 200-character string and a byte-comparison test passes through every renderer. `systemctl --user stop jarvis-voice` leaves jarvis-dispatch and the DB intact, and restarting resumes cleanly. aec_bench's verdict is written into docs/adr/ and the headset decision is made.

---

## Stage 2 — THE SLICE — speak a project into existence

**Effort:** 8-12 days

**Goal.** The headline feature, end to end, locally. This is the stage that makes the project worth finishing.

**What ships.** "Hey Jarvis, let's build an app that watches my YouTube comments and pings me on Telegram. Use Opus 5 with extra-high effort." Jarvis tidies it, reads the numbered requirement list back to you VERBATIM item by item, says 'I may have missed…' if the coverage check flagged anything, takes 'drop three' and 'add: no Docker', you confirm, and Claude Code starts in plan mode in a directory. Each plan question and its numbered options are read aloud in exact words; you answer 'one and three' or 'the SQLite one' or 'none of these — use Postgres'. The plan is read back, you approve, it runs, it narrates progress conversationally, and it tells you when it's done. `jarvis log` shows every tool call. Ctrl-C on the voice app does not lose the build.

**Files.** jarvis/cc/{driver,hooks,narrate,answers,resumer,stream}.py; jarvis/spec/{tidy,schema}.py; jarvis/tools/builtin/code_build.py; the async long_running dispatch path in registry.py; the Narrator; tests/{test_defer_resume,test_answers_validate,test_tidy_no_drift}.py.

**What it proves.** That long tools do not deafen the assistant, because the driver is a different process. That the never-add guarantee survives a real multi-minute spoken spec. That deterministic index-only answering beats a model round trip. That model and effort map straight onto session options — including speaking the honest line that 'high' is the default and changes nothing. That the two-voice seam is or is not tolerable after an hour of real use, which is the only way that question can be answered.

**Done when.** Three real projects specified by voice, planned, and built, with at least one four-question multi-select plan round and at least one 'none of these'. The tidy audit catches a deliberately-mumbled requirement in a test. Killing the voice app mid-build and restarting resumes narration at the right sentence. A written verdict on the two-voice seam, and if it is 'intolerable', the mitigation decision is made here rather than discovered later.

---

## Stage 3 — Telegram — the second channel, and the insurance

**Effort:** 5-7 days. DELIBERATE DEVIATION from the user's suggested build order, which put the repo step third. The repo step is a three-day prepend to an already-working flow and its absence costs nothing (you run in an existing directory chosen by voice); the second channel is the thing that proves the abstraction and is the insurance if the carrier never arrives.

**Goal.** Retire the stall risk against a real remote human with zero carrier dependency, and put a byte-exact mirror under the fidelity requirement.

**What ships.** From a phone, anywhere: plan-mode questions arrive as a message with the option labels printed literally and numbered inline buttons underneath, honouring single- vs multi-select with a 'None of these — reply with text' button. Tap an answer and a parked job resumes and finishes. Screenshots come back as PNG documents with terminal text intact. Voice notes in both directions. Task-finished pushes. /status, /log today, /spend, /kill. You can say 'I'm going out' and 'I'm back'.

**Files.** jarvis/channels/telegram/{channel,auth,voicenote,screenshot}.py; jarvis/apps/telegram.py; jarvis/router.py escalation timers and deliveries materialisation; the presence idle probe wired into dispatch; the defer branch in cc/hooks.py wired to presence; resumer.py promoted to a supervised task; deploy/jarvis-telegram.service.

**What it proves.** The channel abstraction under a channel whose affordances are COMPLETELY different from the desk's — buttons instead of speech, asynchronous instead of live, text instead of audio. If a request survives Telegram it will survive PSTN. It also proves the full away-handling loop (defer → out-of-band delivery → answer from another process → resume) against a real remote human, months before a phone number exists. AND: on this channel plan-mode fidelity is free and total, which is the escape hatch if the audible seam was judged bad in stage 2.

**Done when.** THE FALSIFIABLE TEST: `git diff --stat` for this stage touches ZERO files in jarvis/audio/, jarvis/voice/, jarvis/live/ and zero lines of jarvis/cc/driver.py. If it does not, the seams did not hold and we find out in week six with two working systems rather than in month four. Plus: one full plan-mode round answered entirely from outside the house while the desk machine is locked.

---

## Stage 4 — Repo first

**Effort:** 3-5 days

**Goal.** Complete R2's stated flow, and reconcile confirm+undo with a token that deliberately cannot delete.

**What ships.** "Let's build an app called comment watcher" now creates github.com/Efkrdnz/comment-watcher FIRST — private and empty — clones it into the workspace, and then runs stage 2's flow inside it. Turkish speech is slugged safely (ö→o, ş→s, ı→i), collisions trigger a spoken rename loop, and the confirmation is a verbatim read-back of owner, exact slug and visibility before anything is created, including the honest line: 'I can't delete a repo afterwards — the token deliberately can't. The most I can do is archive it, make it private and rename it to zz-abandoned-comment-watcher.'

**Files.** jarvis/cc/project.py; jarvis/tools/builtin/repo.py; jarvis/effects.py with three handlers (cc.rewind_files, git.revert_push, github.repo_compensate); the local|cloud|unclear mode classifier that asks when ambiguous and RECORDS the answer in the ledger so six months of real phrasing data exists if cloud mode ever returns.

**What it proves.** The confirm+undo requirement against a real irreversible remote side effect, and the reconciliation nobody had done: with delete_repo scoped off, undo is COMPENSATION not reversal, the system must say so BEFORE acting, and harm is engineered down (private + auto_init:false means a wrong repo is an empty private repo that costs nothing) rather than reversed afterwards.

**Done when.** A repo created by voice, and an 'undo that' which archives, renames and makes private while SAYING what it could not do. The GitHub token capability matrix from spike S8 is confirmed: if archive or rename also fail, the spoken wording changes to 'nothing can be done about it' before this stage closes.

---

## Stage 5 — Scheduler and briefing

**Effort:** 6-9 days. SECOND DELIBERATE DEVIATION: the build sheet put the phone at 5 and the briefing at 6. Swapped, because the carrier is the only unbunchable external dependency and every week it is off the critical path is a week of insurance — and the urgent half of R3 ('reach me when plan mode needs a decision') has already been satisfied by Telegram since stage 3.

**Goal.** R4, delivered to whatever channel presence picks, with no carrier involved.

**What ships.** At 10:00 Istanbul, a 'Good moment for your briefing?' request with Now / Five minutes / Skip today — spoken at the desk if you are there, tapped on Telegram if you are not. 'Five minutes' re-queues by writing deliver_after, no scheduler round trip. Then four sections — project status, what matters in the inbox, new issues on your repos, new YouTube comments — delivered one at a time with 'next', 'skip to inbox' and 'repeat', spoken at the desk or buttoned on Telegram. Task completion routes the same way.

**Files.** jarvis/sched.py (APScheduler + SQLAlchemyJobStore on the SAME sqlite file); jarvis/briefing/{script,navigator}.py; jarvis/briefing/sources/{projects,gmail,github,youtube}.py; jarvis/cursors usage. Gmail: users.history.list with a stored historyId, 404 → full resync, and an LLM pass over headers+snippets because Gmail's own IMPORTANT marker was measured unusable for this user (9,210 inbox, 7,807 unread, 100% of yesterday bulk marketing). GitHub: one search/issues?q=user:<login> created:>=cursor. YouTube: allThreadsRelatedToChannelId, 1 quota unit of 10,000/day.

**What it proves.** That a briefing section really is a request, so 'next' is the existing spotter and the existing matcher rather than new code. That the scheduler's entire coupling to everything else is one create_request call. That the pointer being SERVER-SIDE means a dropped channel resumes mid-briefing. And that project status — the section the research says had no data source at all — is just a query against the spine: jobs finished/failed since the cursor, jobs still blocked or deferred with blocked_since, open requests, and open PRs on repos Jarvis created.

**Done when.** Seven consecutive mornings delivered without manual intervention, including one where the machine was rebooted at 09:50 and one where 'call back in five' was used. Cursor discipline verified: no section ever repeats an item across two runs.

---

## Stage 6 — Phone layer — outbound, then inbound

**Effort:** 10-15 days of code, plus the carrier lead time that has been running since day one. This is the honest number against a paid BYO SIP trunk, a regulatory bundle, three separate meters and AMD handling; the one-week estimates elsewhere are not credible.

**Goal.** R3's own-user half, both directions, as one more channel rather than a second assistant.

**What ships.** OUTBOUND: a blocking request unanswered for fifteen minutes makes your phone ring; Jarvis reads the question and its options verbatim and takes the answer by voice or DTMF, and the job resumes. The morning briefing escalates to a call when Telegram goes unanswered. INBOUND: you call your own number, enter an 8-digit PIN, and drive the PC by voice in plan mode. `*9` drops everything, no PIN required.

**Files.** jarvis/channels/phone/{worker,outbound,dtmf,pin}.py; jarvis/apps/phone.py; the phone_user SessionProfile with manual VAD; the default-DENY channels table exercised for real; PIN verification (8 digits, hmac.compare_digest against a hash, 3 tries per call, escalating lockout); AMD / busy / no-answer handling; the telephony meter into the ledger; deploy/jarvis-phone.service.

**What it proves.** That a peer process opening the same DB and loading the same ToolRegistry really is the whole integration — the critic's unanswered question, answered by construction. That the verbatim reader crosses to a phone leg as a constructor argument, because AudioBus was per-endpoint from day one. That inbound is the SMALL half once outbound exists, which is why it was worth keeping rather than deleting a stated requirement.

**Done when.** SECOND FALSIFIABLE TEST: this stage touches zero files in jarvis/cc/ and zero lines of jarvis/requests.py. Plus: one plan-mode question deferred while away, delivered by an outbound call, answered by voice, and resumed — and one inbound call that creates and drives a real job. `*9` demonstrably kills a running build from a live call.

---

## Stage 7 — Calls on the user's behalf

**Effort:** 6-8 days, gated on the Turkish spike

**Goal.** R3's third-party half, with 'must not improvise' enforced by schema rather than by prompt.

**What ships.** "Book a table for four at Çiya for Saturday at eight." Jarvis dials, discloses in one verbatim spoken line that it is an assistant calling on Big Efk's behalf, speaks Turkish for a +90 number, and returns a structured outcome — booked / unavailable / will_call_back / no_answer / unclear — with the exact words heard. If the slot is not free it says it will check and call back, in a line the VERBATIM ENGINE speaks rather than the model composes, and then actually comes back to you.

**Files.** jarvis/channels/phone/third_party.py; the tr_third_party SessionProfile with NO_INTERRUPTION and a Turkish system instruction; the call state machine (AMD, ring-no-answer, busy, IVR, hold music, human hangup mid-sentence, retry/back-off); the disclosure-fired flag written into the activity log with a timestamp.

**What it proves.** That 'must not improvise' is structural: a session with exactly two function declarations — report_outcome(status, slot, party_size, exact_words) and end_call(reason) — physically cannot commit to an alternative it invented, and the outcome enum is what the follow-up conversation reads. The only booked path is status='booked' with a slot; anything else forces one of the other four.

**Done when.** Five real calls placed, including at least one no-answer, one busy and one 'that time isn't free'. The disclosure line fired and was logged on every one. No call ever produced a booking the user had not been told about. If spike S4 showed Turkish output is not controllable, this stage ships its DEFINED FALLBACK instead: Jarvis drafts the script, reads it to you verbatim, and you dial — which is a real deliverable, not a failure.

---

## Cut from v1

Each of these is a deliberate decision with a revisit condition, not an oversight.

### Cloud mode (Claude Code running remotely, inferred from phrasing)

Claude Code cloud sessions are a product surface, not an API: create-and-forget, with no LIVE streaming read path (--teleport is a post-hoc read). Plan mode — the core feature — needs a bidirectional readable stream. And the CLI independently confirms it: a PreToolUse defer is converted to a hard DENY for calls served to a cloud session, with the message 'deferral is not supported for calls served to a cloud session'. Shipping cloud mode on a transport that cannot carry plan mode would mean shipping a version of the headline feature that silently doesn't work at the exact moment it matters. R2 is still honoured in substance: the classifier ships, returns local|unclear, ASKS when the phrasing is ambiguous, and records the answer in the ledger.

**Revisit:** When there are six months of recorded phrasing data showing the user actually asks for it — and then via Anthropic's own documented pattern: plan LOCALLY where the permission host exists, commit the plan, then `claude --cloud "execute docs/plan.md"`. A VPS over SSH is the other route, and the seam is already drawn: the DB follows the hook, the VPS gets its own jarvis.db, and `jarvis sync-ledger` pulls it.

### A general undo stack

The reference's model — a volatile in-RAM list of closures capturing before-state, in a single module-level slot with a 90-second timeout (verified in source) — does not survive a restart and is meaningless for a created repo, a pushed commit, a placed call or a sent Telegram message. Building a durable version of it is a week of work for a feature whose real content is about six cases. Replaced by: rewind_files(user_message_id) for file changes, which is the actual SDK primitive, plus a declarative compensation registry with one entry per remote side effect, added as each one is built.

**Revisit:** Never as a general mechanism. Add compensations one at a time; six entries covers every effect this system can produce.

### The Qt HUD (4,687 lines of PyQt6)

R1 asks for a live status display; a Rich terminal pane IS a live status display, it costs a day instead of a week, it works over SSH, and it removes 'who owns the main thread' from the critical path of the feature the user actually wants. The reference's ui.py is also the single largest source of accidental coupling in that tree — it is why confirm.request hard-refuses headless and why every action takes the PyQt facade as ctx['player'].

**Revisit:** Stage 8+, as an OPTIONAL read-only bus subscriber, if it is still wanted after six months of the TUI. By then the Presenter protocol is proven and the Qt blast radius is one file by construction. The reference's PluginSettingsOverlay (a schema-driven settings renderer) is genuinely the best asset in that file and is worth re-deriving if the HUD ever happens.

### Plugins as a second mechanism alongside actions

The reference has two near-identical loaders with subtly different calling conventions — one passes `parameters` positionally, one by keyword; one injects `speak`, one doesn't. One registry with a `channels` tuple and an `enabled` flag does everything both do, and cutting the duality removes an entire class of 'why didn't my tool get speak' bugs before the first tool is written.

**Revisit:** Never. If third-party drop-ins ever matter, they are the same Tool dataclass loaded from a different directory.

### The voice picker as a UI affordance

Changing voice forces a reconnect, and a reconnect DISCARDS the resumption handle — that is the exact defect (reference problem 3) that this architecture exists to avoid reproducing. Shipping a picker that silently destroys conversational continuity is worse than not shipping one. Instead: `voice` is a config value on SessionProfile that takes effect on the next session, and Jarvis says so out loud when asked to change it mid-conversation. The DEVICE picker is NOT cut — it is needed, and it has to assert that capture and render are one physical device anyway.

**Revisit:** Stage 6, once LiveLease can checkpoint-and-restore properly for the phone leg. At that point changing voice mid-session becomes checkpoint → close → reopen with the handle, and the picker is safe.

### Streaming Claude Code stdout as speech during a phone call

Narrating a compile log down a metered PSTN line is wrong on cost and on attention, and it is the flow where 'stop reading that' matters most and works least. Replaced by a per-channel NarrationPolicy: sentence-level narration at the desk, MILESTONES ONLY on a call (plan produced, files written, tests run, finished, failed). Moving narration between channels is one UPDATE of jobs.narrator_owner.

**Revisit:** If the user asks for it. The tailer already supports it; it is one policy flag.

### gemini-3.8-live-extended-thinking for the plan-mode loop

The plan-mode loop is deterministic local code walking a question array — nothing has to hold a decision tree in its head. The extended-thinking model's async-only constraint (tool responses accepted only when interaction_status is IDLE) would add a real failure mode for no gain.

**Revisit:** If a future feature genuinely needs the model to reason across a long multi-turn negotiation — the third-party call state machine is the only candidate, and that one deliberately has two tools and a script precisely so it does not need to think.

### MMS for screenshots, and any vector store for memory

MMS to Turkish handsets from an international number is effectively dead, and Telegram sendDocument preserves PNG to 50MB while sendPhoto re-encodes to JPEG and smears terminal text. Memory: a SQLite table plus a `recall` tool and the reference's genuinely good three-part prompt design (full identity, recency-ranked entries with a per-category cap, then a keys-only INDEX so the model knows what it can look up) is the whole feature; embeddings would be a dependency and a background job for a corpus of a few hundred rows.

**Revisit:** MMS: never. Memory: when lexical recall demonstrably fails on a real question, which for a few hundred personal facts it will not.

---

## Decisions still open

Four of these were settled in the planning session and are recorded in `docs/adr/`: clean-room build,
Max-subscription auth, chase a +90 Turkish number, cloud mode cut from v1. The rest are still live.

### Claude Code authentication: Max-subscription OAuth token, or a metered API key?

**Recommendation.** If you already pay for Max, use `claude setup-token` and accept two consequences: hardening is done with sandboxing plus `permissions.blockReadsOutsideWorkingDirectories` and a default-DENY tool table rather than `--bare` (which explicitly does NOT read CLAUDE_CODE_OAUTH_TOKEN and would silently move you onto metered billing), and R5's spend tracker measures SESSIONS and rate-limit proximity rather than dollars. Set a calendar reminder at 11 months: token expiry is the single most likely silent failure of the entire system.

**If you choose otherwise.** With an API key, `ResultMessage.total_cost_usd` is meaningful and the ledger's primary unit is `usd_est`; spend becomes a real number with a real threshold. With OAuth, the primary unit is `rate_window_pct`, `usd_equiv` is NULL for Claude, and `SpendStatus.unpriced` speaks that aloud every time — which is honest but is a different R5 than the build sheet imagines. It also decides whether cloud sessions are even available. This is the highest-leverage unresolved fact in the plan and it touches four requirements.

**Decide by:** BEFORE stage 2 begins. Probe S9(a) answers it in twenty minutes; the answer goes in docs/adr/0004.

### Telephony provider, and a Turkish (+90) number versus an international one

**Recommendation.** Start procurement on DAY ONE of stage 0, in parallel with everything, because it blocks and nothing else does. Take an international trunk now (Twilio or Telnyx behind LiveKit) and make outbound-callback the default pattern, which makes the expensive inbound leg mostly disappear. Separately spend 30 minutes on Netgsm's individual-subscriber flow (T.C. Kimlik No + e-Devlet) before accepting that a +90 number is impossible — the 'closed to individuals' finding was generalised from ONE operator. Budget for three separate meters: agent minutes, LiveKit SIP minutes, carrier minutes. The LiveKit free-tier number is INBOUND-ONLY and US-only, so every flow in the build sheet needs a paid trunk regardless.

**If you choose otherwise.** If no trunk ever arrives, stages 0-5 and 7's fallback are a complete, daily-use product and only R3's two call directions are missing — which is exactly why Telegram is stage 3. If a +90 number IS obtainable, third-party Turkish calls stop looking like a foreign scam call and the answer rate goes up materially, which is the difference between stage 7 being useful and being a demo.

**Decide by:** Application submitted in stage 0, week one. The decision itself can wait until stage 5 ends, but the CLOCK cannot.

### Third-party calls: disclose that it is an AI, and record or not?

**Recommendation.** Disclose, always, in ONE spoken verbatim line at the start of every third-party call, and log that the line fired with a timestamp. Do not record third-party audio; keep a text transcript and a structured outcome only. The legal framing in the research was overturned on review — EU AI Act Art. 50 binds providers not deployers, and Art. 2(10) plus KVKK Art. 28 personal/household exemptions likely put a one-user assistant out of scope — so this is an ethics and relationship call, not a compliance one. Both defaults are cheap and both make the honesty requirement real rather than nominal.

**If you choose otherwise.** Not disclosing changes the Turkish system instruction and removes the disclosure_spoken flag, and means the first time a restaurant realises what happened it is a relationship problem rather than a non-event. Recording audio changes the activity log's retention policy and its backup exclusions, and makes the log itself a liability.

**Decide by:** Before stage 7 begins. It shapes the system instruction, so it cannot be bolted on.

### Blast radius: what may the phone channel never do?

**Recommendation.** Phone-to-owner runs plan mode with a default-DENY `can_use_tool` table — an explicit allow list, NOT allow-unless-Write. No push, no install, no sudo, no reads of ~/.ssh, ~/.claude or **/.env. GitHub token with delete_repo OFF so deletion is impossible rather than merely disallowed. Callback-to-stored-number for anything privileged. The research is unambiguous that deny rules are leaky (Bash(rm *) does not match /bin/rm) and that the durable controls are scoped credentials and sandboxing.

**If you choose otherwise.** A permissive phone table makes an 8-digit PIN the only thing between a wrong number and your repositories, and PIN brute-forcing is exactly what the callback pattern exists to eliminate. A stricter one means some legitimate 'just fix it while I'm on the train' requests get denied and have to wait for the desk — which is annoying and recoverable, the right direction to err.

**Decide by:** Before stage 6. It is one data table (`Tool.channels`), so changing it later is cheap; getting it wrong once is not.

### Headset or open speakers at the desk?

**Recommendation.** A wired USB headset with a boom mic, ~$40, as the DEFAULT — and treat open-speaker operation as the upgrade AEC buys rather than the baseline it must deliver. It gives 30-40dB of acoustic isolation, makes AEC nearly irrelevant, gives one hardware clock by construction, and is what every voice developer actually uses (Google's own Live API best practices say to use headphones to prevent self-interruption). For one developer on a personal budget, $40 to delete a class of bug is proportionate engineering; three weekends tuning AEC3 for a room is not.

**If you choose otherwise.** Open speakers are genuinely nicer — you can talk to Jarvis from across the room — and if aec_bench comes back at 25dB+ with under one false barge-in per 30 seconds, take them. Below 15dB, or with a drifting ERLE, you get self-interruption every few seconds and the spotter starts hearing the reader, which degrades the kill switch as well as barge-in.

**Decide by:** End of stage 0, decided by the aec_bench number rather than by preference.

### Two voices, or one? (Only live if spike S2 clears gemini-tts.)

**Recommendation.** Two, DISTINCT and deliberately legible, with an earcon bracketing every verbatim episode and an explicit handoff line. The rule 'when the other voice speaks, those are somebody else's exact words' is an audible integrity marker and a direct contribution to R5's honesty requirement — it makes drift NOTICEABLE rather than merely absent. Consistency beats seamlessness.

**If you choose otherwise.** If S2 shows gemini-3.1-flash-tts-preview recites at 1.000 over 600+ labels with zero insertions and zero inversions, `verbatim.voice_mode = "matched"` gives one beautiful voice in the Live session's own timbre, behind a synth→transcribe→compare gate that costs one extra API call per clip and is hidden by pre-synthesis. You lose the audible marker and gain smoothness. That is a taste call and it is yours.

**Decide by:** Judged at the end of stage 2, after an hour of real plan-mode use — the only way this question can honestly be answered.

### Accept that local, offline voice commands are ENGLISH-ONLY

**Recommendation.** Accept it, and lean on the two language-free paths. sherpa-onnx ships pretrained keyword-spotting models for zh-en and Chinese only; there is NO Turkish KWS model. So the wake word, the kill phrase, 'next' and 'skip to X' must be English phrases if they are to work instantly, offline and mid-utterance. Turkish navigation still works through Gemini (the briefing_goto tool), just not offline — which means Turkish has no offline kill switch. Mitigate with a global hotkey (build it FIRST, it takes twenty minutes and it is the one that will actually save you) and DTMF *9 on any call. Both are language-free and more reliable than any spoken phrase.

**If you choose otherwise.** Training a Turkish KWS model is a real side project — data collection, training, evaluation — and it would be unbudgeted work that the kill switch and briefing navigation both silently depend on. If Turkish local commands matter enough to you, say so now and it becomes a named stage rather than a surprise.

**Decide by:** Before stage 5 (briefing navigation) and before the kill switch is un-parked.

### Do you ever intend to monetise this — consulting, a sponsored video, a course, a paid template, a hosted version?

**Recommendation.** Assume YES and ship MIT with zero copied code, which is what this architecture does. The cost of assuming yes when the answer is no is a few extra days re-deriving audio_devices.py and the wake-word wrapper. The cost of assuming no when the answer is yes is that CC BY-NC 4.0's NonCommercial term binds every derivative FOREVER, and you would be negotiating a separate licence from FatihMakes from the weak position of already having his code in your public tree. Asymmetric, irreversible, and decided before the first commit.

**If you choose otherwise.** If you are certain you will never monetise and you want the four days back, fork properly: LICENSE verbatim, NOTICE, CHANGES.md indicating modification, and a README line stating the repo is non-commercial forever. Do it deliberately and in writing on commit one — never accidentally by pasting one function in month three.

**Decide by:** BEFORE THE FIRST COMMIT. This is the only decision on this list that cannot be revisited.

---

## Risks, stated honestly

**Stage 1 is honestly 12-18 days, and I could still be under. Gemini Live plus a duplex audio graph plus device probing plus wake word plus spotter plus Kokoro with a Turkish fallback plus keyring, ledger, memory, presence and a TUI is the single largest stage, and it sits between the spike and the feature the user actually wants. If it drifts to five weeks, momentum — the scarcest resource on a personal project — is spent before anything impressive happens.**

> Every sub-piece of stage 1 is independently demoable, so progress is visible weekly: wake word alone, then a conversation, then barge-in, then 'read me this file exactly'. If it drifts past three weeks, cut the spotter to the pretrained wake word only and defer briefing navigation to stage 5 — both are MicBus readers, so adding them later is arming a detector, not rewiring a graph.

**The whole verbatim-context mechanism rests on one unverified server behaviour. If FunctionResponseScheduling.SILENT does not actually suppress generation on gemini-3.8-live, and the send_client_content prefill fallback also misbehaves, then the reader speaks the labels but Gemini does not know what was said — so 'the second one' and 'what's the difference between one and two?' have nothing to resolve against.**

> Spike S3, fifteen minutes, in stage 0 rather than week three. And the degradation is bounded rather than total: the deterministic matcher resolves numbers and labels against the ORIGINAL options array held locally, so answering still works; only conversational follow-up about the options is lost. The Telegram mirror from stage 3 is a second, exact channel that does not depend on this at all.

**The two-voice seam may simply be intolerable. Big Efk will hear Gemini's warm native voice say 'okay, it's asking about storage', then a flatter voice read 'Option one. SQLite. Stores todos in a local file', then Gemini again — every few seconds, for the length of a planning conversation. I cannot de-risk this with a spike, because the question is not 'does it work' but 'does one specific person find it acceptable after an hour of real use'.**

> Earcons and an explicit handoff line make the switch legible rather than jarring, and the rule 'when the other voice speaks, those are somebody else's exact words' turns it into an honesty feature. S2 might collapse it to one voice. And if the verdict at the end of stage 2 is 'no', the fallback is defined in advance: keep the reader only for option labels and the disclosure line, accept paraphrase elsewhere, and rely on the Telegram and TUI mirrors for exactness — which preserves the requirement in substance while conceding that voice alone could not carry it.

**AEC quality is a property of the room, the speakers and the mic, not of the software, and I cannot promise it from here. A bad result means self-interruption every few seconds and a spotter that hears the reader, which degrades the kill switch as well as barge-in.**

> The design deliberately lowers the bar AEC must clear — duck-confirm means AEC only needs to let a VAD make a DECISION (15-20dB), not deliver clean ASR during double-talk (30+dB). Measured in stage 0 with published pass/fail numbers. A seven-rung fallback ladder ending in a $40 headset, which is the recommended default anyway. And push-to-talk is built regardless, because it is the deterministic escape hatch for the moment everything else is confused.

**Defer is best-effort in ways the research did not catch: silently ignored when siblings are in the tool batch, ignored in interactive mode, and refused outright for cloud sessions. A design that assumed defer always takes would have a runner exiting when it should have blocked, or blocking when it thought it had exited.**

> The runner never assumes. `_defer_requested` records the tool_use_id and the fallback to blocking is explicit, publishing `job.blocked` with `defer_rejected: true` so the pattern shows up in the honesty log rather than being mysterious. Blocking is cheap because the runner is its own process — which is the seam paying for itself before stage 2 is over. S1 confirms the failure shape in stage 0.

**The permission callback blocking for 90 minutes over the SDK's stdio control protocol is untested. If the CLI reaps a long-pending callback, the at-desk flow silently becomes defer-only — and since defer is itself best-effort (above), that combination would be a real problem discovered late.**

> S9(c) runs it unattended for 90 minutes in stage 0. Separately, `assert_no_ask_user_question_timeout()` refuses to start if a user or managed settings file sets the 60s/5m/10m knob, because that would auto-close questions under us and look exactly like lost answers.

**The GitHub token may not be able to archive or rename either, in which case github.repo_create has no compensation at all and the spoken undo line — 'I can archive and rename it' — becomes a lie. That is precisely the kind of drift that turns the honesty layer into theatre.**

> S8 tests it against the live API on a throwaway repo in stage 0, because docs.github.com was egress-blocked during research and the documented body is itself unverified. If archive and rename fail, the spoken wording changes to 'nothing can be done about it' before stage 4 closes. And `spoken_effect_line()` GENERATES the wording from the reversibility class, so the words cannot drift from the truth independently.

**jarvis-dispatch is a single point for routing. If it is down, a plan question sits pending and nobody escalates it, so an away-from-desk question waits until it restarts.**

> It is a SPOF for routing, not for correctness, and that distinction is designed in: jobs keep running, answers given at the desk still land, and on restart it sees the pending row and escalates. It is a user systemd unit with Restart=on-failure, and it is deliberately the thinnest of the four processes. `reconcile()` at its startup is what makes a restart a non-event rather than a recovery procedure.

**Turkish output control has NO confirmed mechanism. speech_config.language_code was refuted for native-audio models, and a related bug report suggests input transcription can land in the wrong language regardless — which would also poison the activity log's audit trail for third-party calls.**

> S4 tests it with a native speaker scoring the output in stage 0, not in month four. And stage 7 has a DEFINED fallback rather than a shrug: Jarvis drafts the call script, reads it to you verbatim, you dial. That is a real deliverable that still saves the user the work of composing a reservation request in the middle of something else.

**Nine tables, four long-lived processes and a two-plane bus is more surface than a one-developer project usually carries, and I am asking for it before the phone exists. If the seams do not actually hold, this is a framework nobody asked for and the buildability judge's verdict on the spine-first proposal applies to this one instead.**

> Two falsifiable tests, both one `git diff --stat` away from settling it: stage 3 (Telegram) must touch zero files in jarvis/audio/, jarvis/voice/, jarvis/live/ and zero lines of jarvis/cc/driver.py; stage 6 (phone) must touch zero files in jarvis/cc/ and zero lines of jarvis/requests.py. If either is false, the bet did not pay off — and it is discovered in week six with two working systems rather than in month four with re-plumbing stacked on a carrier that cannot be unblocked. The counterweight to the surface is that at stage 1 there are exactly two long-lived processes and both are thin; the rest arrives one stage at a time, each paying for itself on arrival.

---

## Two falsifiable tests

The whole architecture rests on a claim — that seven seams make the phone layer a re-wiring rather than a
rewrite. That claim is checkable with `git diff --stat`, and it is worth checking, because if it is false the
right time to learn that is week six with one working system, not month four with two.

1. **Stage 3 (Telegram)** must touch **zero** files in `jarvis/audio/`, `jarvis/voice/`, `jarvis/live/`, and
   zero lines of `jarvis/cc/driver.py`.
2. **Stage 6 (phone)** must touch **zero** files in `jarvis/cc/` and zero lines of `jarvis/requests.py`.

If either fails, the seams did not hold — stop and fix the seam rather than working around it.
