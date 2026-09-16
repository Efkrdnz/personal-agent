# Architecture

**Status:** designed 16 Sep 2026, not yet built. Produced by three independent architectures written from
deliberately opposed angles, scored by three judging lenses, with dedicated deep-dives on the three hardest
sub-problems, then synthesised. All three lenses picked the same foundation independently.

Read [`findings.md`](findings.md) first if you want to know *why* these choices — several obvious designs are
refuted there by evidence. Read [`roadmap.md`](roadmap.md) for what to build in what order.

---

## The one idea

There is exactly one primitive in this system: a human decision that some process needs and cannot compute. A plan-mode question, an `ExitPlanMode` approval, a tidied-prompt read-back, a Bash confirmation, "is now a good time for your briefing", "did the restaurant have the slot" — these are the same object, and every one of them will eventually be raised by one process and answered by another, hours later, on a different channel, after the asker has died. So the whole coordination layer is one `requests` row in one SQLite file in WAL mode, plus a best-effort one-byte datagram poke that exists only to make it fast and whose total loss would cost 250ms of latency and nothing else. That is the event bus whose absence is the stall risk, and it is about 400 lines, not a framework.

Seven seams are non-negotiable from the first commit, because each one is a rewrite if retrofitted and cheap if not: the Claude Code driver is its own OS process, always; `jarvis-dispatch` — not the voice app — is the parent of every job and the owner of the scheduler, so the kill switch has a real parent and a voice-app crash costs nothing; nothing calls `speak()`, everything enqueues an `Utterance` carrying a fidelity flag; the deterministic TTS reader ships in stage 1, not "later"; all durable state is one SQLite file with a channel column from migration 001; `LiveSession` takes its profile, source and sink as constructor arguments rather than owning singletons; and `ToolCtx` is a frozen dataclass with no `player` field and no session field, enforced by a CI grep.

Two rules carry the requirement whose entire point is fidelity. Load-bearing text never passes through a generative model: Gemini Live is the conversationalist, a local Kokoro/edge-tts reader speaks the exact bytes, and Jarvis has two voices deliberately and permanently. And the model may emit an option *index*, never an option *label* — the numbering is ours, assigned locally from the payload order, so reordering, translating, merging or dropping an option is structurally impossible rather than prompt-hoped.

The microphone is never gated off. Barge-in is duck-on-suspicion then confirm: dip the playback 20dB, see whether the VAD is still firing with the echo gone, and only then commit. That drops the bar AEC must clear from "clean ASR during double-talk" to "let a VAD make a decision", which is the difference between a solved problem and a room-dependent one.

Presence is a first-class subsystem with asymmetric hysteresis — instant to become present, slow to become away — because a false "away" costs one redundant Telegram message and a false "present" costs forty minutes of a stalled build. Its cleverest sensor is free: a spoken question that nobody answers within 30 seconds is itself the evidence that the room is empty.

Undo is three honest classes — reversible, compensatable, irreversible — decided before execution, stored on the effect row, and used to pick the confirmation strength. Creating a GitHub repo is irreversible because the token deliberately lacks `delete_repo`, so it is gated by a verbatim read-back and made harmless by always being private and empty, and Jarvis says "I can't delete it" rather than pretending.

Telegram arrives at stage 3, before the repo step and long before the carrier, because it is the second channel that proves the abstraction, it delivers most of "drive the PC from anywhere" with no KYC, and its inline keyboards make plan-mode fidelity free on at least one channel while the two-voice verdict is still open. Cloud mode is cut; the classifier ships, asks when the phrasing is ambiguous, and answers "local". Honest total: about thirteen weeks of focused solo work to all seven stages, with something genuinely usable at the end of every one.

---

## Clean-room, and why

CLEAN-ROOM. Not close, and the licence is only half the reason.

THE LICENCE HALF. CC BY-NC 4.0's NonCommercial term binds every derivative permanently. Copying even 200 lines of `core/wake_word.py` into `github.com/Efkrdnz/personal-agent` makes the entire repo non-commercial forever — no consulting engagement built on it, no sponsored video, no paid template, no hosted version, ever, without a separately negotiated licence from FatihMakes, negotiated from the weak position of already having his code in your tree. That is a permanent, irreversible constraint bought in week one to save perhaps four days. Bad trade at any discount rate.

THE ARCHITECTURAL HALF, which is decisive on its own. The file worth forking is the file being deleted. Mark-LIII's value is concentrated in `main.py`'s `JarvisLive` (1,744 lines): one `self.session`, one `out_queue`, tools awaited inline in `session.receive()`, `self.ui` handed to every action as `ctx["player"]`. Every one of those is an assumption this architecture exists to refuse. A fork would spend week one deleting what it forked. The recon's own reuse verdict says REWRITE for all three things Jarvis actually needs — `JarvisLive`, the tool dispatch path, and confirm — and KEEP only two peripherals.

WHAT IS ACTUALLY WORTH HAVING, and why it still is not copied. `core/audio_devices.py` (421 lines) is genuinely hard-won: its comments document three failed device-probing designs and the measurements that killed them. Read it as a bug list and a specification; re-derive the probing against `sounddevice` from scratch. This design needs a different probe anyway — it must assert that capture and render are the *same physical device*, which the reference never checks and which is the most common silent cause of AEC quietly failing after two minutes. `core/wake_word.py` (202 lines) is a wrapper whose pretrained phrase is literally `hey_jarvis`; openWakeWord's own quickstart gets you 90% of it in 30 lines, and this design routes the wake word through a shared `MicBus` cursor rather than its own queue, so the threading is different. `core/tts.py` (442 lines) is dead code nothing imports, and its fatal defect — every engine both synthesises *and plays*, with `TTSPlayer.stop()` calling the global `sd.stop()` at line 420, which would kill the Live session's output stream as collateral damage — is exactly the collision this design eliminates by contract. Salvage the *ideas*: its `_to_numpy()` torch/numpy defensive conversion, its `_compress_silence()` (Kokoro's 1-2s punctuation pauses are genuinely annoying when reading a numbered list), and `core/llm_client.py:31`'s `_SENT_END` splitter shape. Re-implement all of them.

The session-resumption logic — `_ReconnectSignal` / `_is_reconnect_signal` / `_keep_context_of` at `main.py:314-349`, handle capture at `1010-1015` — is already documented as prose contracts in the recon. Prose describing how a protocol behaves is not a derivative work. Read the contract, write the implementation.

WHAT SHIPS. MIT `LICENSE`. A `CREDITS.md` naming Mark-LIII as acknowledged inspiration with a link — voluntary, because it is decent, not because it is owed; reading source and being influenced carries no licence obligation, copying expression does. `CONTRIBUTING.md` rule 1, one line: *nothing from `/home/user/fatihmakes/mark-liii` enters this tree; the reference file is never open in the editor while the corresponding Jarvis file is being written.* And a written escape clause: if Big Efk ever does decide to paste a file in, that same day the repo gains `LICENSE` (CC BY-NC 4.0 verbatim), `NOTICE`, `CHANGES.md` indicating modification, and a README line stating the repo is then non-commercial forever. That decision gets made once, in writing, deliberately — never accidentally by a `git add -A`.

DAY-ONE SECURITY CONSEQUENCE, verified in this session and not hypothetical. The reference's `.gitignore` secret patterns are silently inert, and I confirmed the exact mechanism: the lines carry trailing same-line comments (`config/api_keys.json          # your Gemini API key, plus per-plugin credentials`), and `.gitignore` has no trailing-comment syntax — the whole line including the spaces and the `#` is taken as one literal pattern, which matches nothing. `git check-ignore -v config/api_keys.json` returns empty. A `git add -A` in that repo publishes the Gemini key. So: commit one of this repo contains a `.gitignore` with comments on their *own* lines, a CI job that runs `git check-ignore -v` against every secret pattern and fails if any returns empty, and — the real fix — no credential in the tree at all, because they live in the OS keyring.

---

## Components

### `jarvis.db + jarvis.bus`

**Lives in:** A library linked into every process. No process of its own. SQLite's single-writer rule gives a total order for free: AUTOINCREMENT seq is assigned in commit order, so a subscriber reading `WHERE seq > cursor ORDER BY seq` can never see a hole that later fills in.

The transport and the activity log, which are the same table. One SQLite file at ~/.local/state/jarvis/jarvis.db in WAL mode holding every durable table. `publish()` appends a hash-chained, redacted-at-write event and fires a best-effort datagram poke; `Peer` is a process's attachment (poke socket + event cursor). Rejects Redis/NATS/a broker process deliberately: this system is ~300 control messages a day, needs history as the primary artifact rather than as a retention policy, and would have needed the durable store anyway — a broker adds a second source of truth and a second thing that can be down.

```python
def connect() -> sqlite3.Connection                      # WAL, synchronous=NORMAL, busy_timeout=5000, foreign_keys=ON
@contextmanager
def tx(con) -> Iterator[sqlite3.Connection]              # BEGIN IMMEDIATE; NEVER do network I/O inside
def publish(con, kind: EventKind, actor: str, payload: dict, *,
            job_id=None, request_id=None, channel_id=None, effect_id=None,
            idem_key: str | None = None) -> int          # returns seq; INSERT OR IGNORE on idem_key
def load_secrets(values: list[str]) -> None             # literal keyring values, for write-time redaction

class Peer:
    def __init__(self, con, peer_id: str, kind: ChannelKind, caps: Capabilities,
                 identity: dict | None = None) -> None
    def wait(self, timeout: float | None = None) -> None            # poke or 250ms poll floor
    def poll(self, kinds: tuple[str,...] | None = None, limit: int = 200) -> list[Event]
    def commit(self, seq: int) -> None
    def detach(self) -> None
```

> Redaction lives in exactly one function because the bus IS the activity log — anything that happened was an envelope and anything that was an envelope is in the log. Hash chain computed inside the same BEGIN IMMEDIATE, which is safe because SQLite serialises writers. `jarvis verify-log` walks it. The DB must be on a local filesystem: WAL does not work on NFS/SSHFS, and a startup check refuses to open one that is not.

### `jarvis.requests`

**Lives in:** Library. Written by runners and channels, read by everyone. The CAS in `answer_request` is the entire mechanism by which desk, Telegram and phone can race safely.

The unified gate. Plan-mode questions, ExitPlanMode, tool permissions, effect confirmations, prompt read-backs and briefing consent are ONE type with ONE lifecycle: created -> pending -> answered -> consumed, with expired/cancelled/superseded as the other terminals. Owns the idempotent-consumption-across-resume property that makes the four-hour phone answer work.

```python
def create_request(con, *, kind: ReqKind, short_label: str, presentation: Presentation,
                   payload: dict, actor: str, job_id=None, tool_use_id=None,
                   urgency: Urgency = "normal", reversibility: Reversibility | None = None,
                   expires_in_s: int | None = None, escalate_after_s: int = 90,
                   on_timeout: OnTimeout = "defer") -> Request

def find_open_for_tool(con, job_id: str, tool_use_id: str | None,
                       tool_name: str, tool_input: dict) -> Request | None
    # lookup order: (1) tool_use_id exact; (2) dedupe_key AND state<>'consumed' ORDER BY attempt DESC;
    # (3) miss -> create with attempt = N+1

def answer_request(con, request_id: str, answer: Answer, *,
                   by_channel: str, mode: str) -> bool
    # UPDATE requests SET state='answered', ... WHERE id=? AND state='pending' RETURNING job_id
    # False == someone else won. The loser SAYS SO out loud; it never fails silently.

def consume(con, request_id: str, actor: str) -> None
def wait_for_answer(con, peer: Peer, request_id: str,
                    timeout_s: float | None = None) -> Answer | None
```

> `dedupe_key = sha256(job_id ‖ tool_name ‖ canonical_json(tool_input))` with canonical = sort_keys=True, separators=(',',':'). Stable across resume, across processes, across machines. The separate `attempt` counter is what distinguishes a resume re-fire (reuse the row) from Claude legitimately asking the same question twice (new row) — a case a bare content hash gets wrong. `payload` is stored byte-for-byte and NEVER mutated, which is what lets the driver return `{**payload, "answers": ...}` with every original field unchanged; the CLI's validator rejects `changed_shown_field`.

### `jarvis.jobs + jarvis.reconcile`

**Lives in:** Library. Rows written by jarvis-ccjob; read by dispatch, voice, telegram, phone. `reconcile()` is also the data source for briefing section 1 ("project status"), which the research flagged as having no data source at all.

The session-agnostic job registry, at its honest size: one table, six methods. Jobs outlive the process that started them. Liveness is checked without lying — boot_id match plus /proc/<pid> plus field 22 of /proc/<pid>/stat, so PID reuse after a reboot cannot convince the reconciler that a dead job is running. `reconcile()` runs at the start of every process and is idempotent.

```python
JobState = Literal["queued","starting","running","blocked","deferred","parked",
                   "finishing","done","failed","killed","orphaned"]

class JobStore:
    def create(self, **kw) -> Job
    def get(self, job_id: str) -> Job | None
    def running(self) -> list[Job]
    def blocked_longer_than(self, seconds: int) -> list[Job]   # -> the briefing
    def since(self, ts: str) -> list[Job]
    def set_state(self, job_id: str, state: JobState, **fields) -> None

def reconcile(con, actor: str) -> dict
def spawn_runner(job_id: str, resume: bool = False) -> None
    # systemd-run --user --collect --unit=jarvis-job-<id>  (preferred: cgroup kill for free)
    # else subprocess.Popen(..., start_new_session=True).  NEVER a child of the voice app.
def process_alive(pid: int|None, rec_boot: str|None, rec_ticks: int|None) -> bool
```

> `blocked` (runner alive, parked on a requests row) is the DEFAULT way to wait, and it is cheap precisely because the runner is its own process. `deferred` (runner exited, cc_session_id + requests row are the entire resumable state) is for predicted long absences. Reconcile converges the deferred-with-an-answer path and the orphaned-after-reboot path onto one code path: spawn with `resume=`.

### `jarvis.audio — the one graph`

**Lives in:** jarvis-voice (P1) for the desk leg; jarvis-phone (P4) supplies its own Source/Sink over LiveKit tracks and skips AEC entirely because the carrier and handset do it.

The single microphone and speaker graph satisfying wake word, kill switch, briefing navigation, barge-in, AEC and the phone leg simultaneously. One duplex sd.Stream at 48kHz/20ms on ONE physical device; AEC3 in the callback with the reference taken post-fader from the mixer's own output and the delay taken from PortAudio's clock; `MicBus` is a single-writer N-cursor ring that every detector reads independently. THE MIC IS NEVER GATED OFF.

```python
BUS_RATE, DEV_RATE, BLOCK = 24_000, 48_000, 960      # 20 ms

class MicBus:
    def write(self, pcm: np.ndarray) -> None            # from the PortAudio callback only
    def cursor(self) -> int
    def read(self, cursor: int, n: int, timeout: float = 0.5) -> tuple[np.ndarray|None, int, int]
        # -> (pcm, next_cursor, dropped).  A lagging reader is FAST-FORWARDED and logged,
        #    never allowed to block the writer.

class AudioBus:                                          # owns the ONE output stream
    def track(self, name: str, prio: Prio, gain: float = 1.0) -> Track
    def preempt_for(self, prio: Prio) -> None            # flush+10ms fade the lower track
    def hard_stop(self) -> None                          # zero-fill the in-flight buffer
    # Prio: SYSTEM(40) > VERBATIM(30) > LIVE(20) > MONITOR(10, the only MIXED one, -12 dB)

class DeskLeg:
    has_hardware_echo_control = False                    # -> TurnController uses duck-confirm
    erle_db: float                                       # live health metric, shown in the TUI

class Spotter(_Reader):                                  # sherpa-onnx KWS, always-on, un-gated
    FRAME = 1600
    def __init__(self, bus, models_dir, keywords_file, on_hit: Callable[[str], None])
```

> VERIFIED LIBRARY FACTS, because three obvious choices are dead: `pyaudio-webrtc-apm` does not exist on PyPI; `speexdsp` last shipped 2018, sdist only; `webrtc-audio-processing` (xiongyihui) ships armv7l wheels only; `aec-audio-processing` is Windows-only. Use `pywebrtc-audio` 0.2.0 (AEC3, C++ with the GIL released, wheels cp310-cp314 across Linux/macOS/Windows) with `livekit`'s `rtc.AudioProcessingModule` as the drop-in second source. Also: `openwakeword` will NOT pip-install on Linux with Python >=3.12 — its metadata hard-requires `tflite-runtime`, whose last wheels are cp311 — so install `--no-deps` and force `inference_framework="onnx"`. Do not `pip install silero-vad`: it drags torch even for the ONNX path; sherpa-onnx bundles Silero.

### `jarvis.voice.verbatim + jarvis.speech.OutputRouter`

**Lives in:** Library. Used in jarvis-voice (to the speakers), in jarvis-telegram (to an ogg/opus file for sendVoice), and in jarvis-phone (to the call track). Synthesise once, cache, route anywhere.

The fidelity guarantee, and the only way anything in the system makes sound. Engines return BYTES and never touch a device — that one contract change is what eliminates the reference's collision. Every `Utterance` carries `fidelity: 'verbatim' | 'natural'`; verbatim bypasses Gemini's voice entirely and writes PCM into the same AudioBus at priority 0.

```python
class VerbatimEngine(Protocol):
    name: str; deterministic: bool
    def synth(self, text: str, lang: str) -> bytes       # PCM16 LE mono @ 24000 Hz

class KokoroEngine:   name, deterministic = "kokoro", True     # local, 24k native, EN, no LM in the path
class EdgeEngine:     name, deterministic = "edge",   True     # audio-24khz-...-mono-mp3, TR + EN
class GeminiTTSEngine: name, deterministic = "gemini-tts", False  # LLM; EXACT tier only behind verify=True

class VerbatimSpeaker:
    async def pcm_for(self, text: str, lang: str = "en") -> bytes      # cache -> engine ladder
    async def prefetch(self, texts: list[str], lang: str = "en") -> None
    async def say(self, bus: AudioBus, text: str, lang: str = "en",
                  mirror: bool = True, earcon: bool = True) -> None

class NoVerbatimEngine(RuntimeError): ...   # -> REFUSE and defer. Never silently paraphrase.
```

> The 24kHz rate collision is DESIGNED OUT rather than managed: Gemini Live output, Gemini TTS, Kokoro and edge-tts are all natively 24kHz mono, so there is no resampler anywhere on the desk path; the only resample in the system is 24k->8k mu-law inside the phone sink. Content-addressed disk cache at ~/.cache/jarvis/tts/<sha256>.pcm means option labels ("SQLite", "Postgres", "Yes") are a file read after the first time. Pre-synthesis fires the instant AskUserQuestion lands, while Gemini is still speaking the framing sentence, so all TTS latency hides behind Live's own utterance.

### `jarvis.live.LiveSession + LiveLease`

**Lives in:** jarvis-voice for the desk leg; jarvis-phone for a call leg. One asyncio TaskGroup per session.

One Gemini Live connection with its own profile, source, sink and tool set — multi-instantiable by construction from the first commit, with exactly one instantiated in v1. Owns connect/serve/reconnect, resumption-handle capture, GoAway handling and context-window compression. `LiveLease` makes concurrency an explicit, revocable, configurable resource rather than an accident, because the ceiling is unverified (sources conflict 3 / 1000 / 5000).

```python
@dataclass(frozen=True)
class SessionProfile:
    name: str                      # 'desk' | 'phone_user' | 'phone_tr_third_party'
    model: str = "gemini-3.8-live"
    voice: str = "Charon"
    system_instruction: str = ""
    manual_vad: bool = True        # BOTH profiles: we drive activity_start/end ourselves
    activity_handling: str = "START_OF_ACTIVITY_INTERRUPTS"
    tools: tuple[str, ...] = ()    # names allowed on THIS leg

class LiveSession:
    def __init__(self, profile: SessionProfile, source: AudioSource,
                 sink: AudioSink, tools: ToolRegistry, lease: LeaseGrant) -> None
    async def run(self) -> None
    async def send_tool_response(self, call_id: str, result: dict, *,
                                 scheduling: str = "SILENT") -> None
    async def close(self, *, checkpoint: bool = True) -> None
    @property
    def resume_handle(self) -> str | None

class LiveLease:
    def __init__(self, capacity: int = 1) -> None        # config: live_max_concurrent
    async def acquire(self, holder: str, priority: int) -> LeaseGrant
    async def revoke(self, holder: str, reason: str) -> None
```

> Single-flight by default: acquiring a phone lease while the desk holds one makes the desk `close(checkpoint=True)`, persisting `resume_handle`, and `release()` reopens with it. This is correct under the pessimistic reading of the quota and becomes correct under the optimistic one with a one-line config change, because sessions live in separate objects with separate connections and separate voices from day one. Reference problem 3 — voice baked in at connect, reconnect discarding the handle — is unrepresentable here.

### `jarvis.tools.ToolRegistry + ToolCtx`

**Lives in:** jarvis-voice and jarvis-phone. Tool handlers touch only DB objects — never a session, never a UI object.

ONE registry, not two — actions and plugins collapse into a single mechanism with an `enabled` flag, eliminating the reference's two near-identical loaders with subtly different calling conventions. Builds Gemini function declarations filtered per `SessionProfile`, and dispatches. Long tools NEVER block the receive loop: they return a job handle in milliseconds.

```python
@dataclass
class Tool:
    name: str; description: str; parameters: dict
    handler: Callable[..., Awaitable[str] | str]
    long_running: bool = False
    channels: tuple[str, ...] = ("desk",)      # DEFAULT-DENY. phone/telegram must opt in.

@dataclass(frozen=True)
class ToolCtx:                                 # the replacement for mark-liii's _CTX_KEYS
    ask: Callable        # requests.create_request + wait_for_answer
    say: Callable        # OutputRouter.say(Utterance)
    jobs: JobStore
    log: Callable
    spend: Callable
    memory: MemoryStore
    presence: Callable[[], Presence]
    channel: str
# DELIBERATELY ABSENT: 'player'. No UI object reaches a tool, ever.

class ToolRegistry:
    def load(self, pkg: str) -> None
    def declarations(self, profile: SessionProfile) -> list[dict]
    async def dispatch(self, fc, ctx: ToolCtx) -> types.FunctionResponse
```

> `ToolCtx` being frozen with no `player` field makes the reference's desk-locked-actions bug (recon: actions receive the PyQt facade as ctx['player'], so headless silently breaks every one) structurally unrepresentable rather than merely discouraged. A CI lint greps jarvis/tools/ for `import PyQt` and fails. The `channels` default-DENY tuple is the whole phone blast-radius policy, in data.

### `jarvis.cc.driver (P2) + hooks + resumer`

**Lives in:** Its own OS process, one per job, launched detached by jarvis-dispatch via systemd-run. Killing or crashing the voice app cannot touch a build.

Runs exactly ONE Claude Code job. Hosts `can_use_tool` and the PreToolUse hook. Never speaks, never touches audio, never imports google.genai or PyQt. This is the seam the whole architecture rests on: retrofitting it is a rewrite, having it on day one is an entrypoint.

```python
# entrypoint: python -m jarvis.cc.driver --job-id J [--resume] 

class Runner:
    async def run(self, prompt: str | None, resume: bool = False) -> None
    async def _can_use_tool(self, tool_name, input_data, context) -> PermissionResult
    async def _pre_tool(self, data, tool_use_id, ctx) -> dict
    def emit(self, **kw) -> None            # -> jobs/<id>/stream.ndjson, NOT the bus
    def options(self, *, resume: bool) -> ClaudeAgentOptions

def assert_no_ask_user_question_timeout() -> None
    # reads MERGED settings; refuses to start and says why if 60s/5m/10m is set anywhere.

def should_defer(con, req: Request) -> bool     # presence in ('away','asleep')
```

> Two planes, deliberately. Control (rare, durable, ordered) is the events table; data (hundreds of lines a minute of Claude Code output) is a per-job append-only NDJSON file that consumers tail by BYTE OFFSET stored in `consumers`. That split is what lets jarvis-voice restart mid-build and resume narrating from the first complete line after its offset, losing nothing and re-speaking nothing.

### `jarvis.spec.Tidier`

**Lives in:** jarvis-voice, called from the `code_build` tool before the job row is created. Two `generate_content` calls on gemini-3.8-flash with JSON schemas — not a Live-session activity.

R2's 'never adds a requirement, never drops one', enforced mechanically rather than by a second model opinion. Every tidied requirement must carry a `source_span` that is a normalised substring of the raw transcript; an invented requirement has no span and is HARD-REJECTED by string containment with no model in the loop.

```python
@dataclass
class Req:
    id: int; text: str; source_span: str     # MUST be a normalised substring of the transcript

@dataclass
class Spec:
    title: str; reqs: list[Req]
    mode: Literal["local","cloud","unclear"]; model: str | None; effort: str | None
    repo_name: str

def tidy(transcript: str) -> Spec

@dataclass
class Audit:
    invented: list[Req]     # span not found -> hard reject, NO model involved
    literals_missing: list[str]  # numbers/versions/paths/URLs/quoted/camelCase in transcript, absent from list
    uncovered: list[str]    # transcript sentences no span touches -> read aloud as 'did I miss'

def audit(transcript: str, spec: Spec) -> Audit
def readback(spec: Spec) -> list[Utterance]      # every Req.text at fidelity='verbatim'
def assemble_prompt(spec: Spec, transcript: str) -> str
```

> Three independent nets of decreasing strength, honestly labelled: (1) literal-token assertion by regex over numbers, versions, paths, URLs, quoted strings and camelCase identifiers — catches the highest-damage class ('Postgres', 'no auth', 'port 8080', 'Opus 5') perfectly and mechanically; (2) content-lemma recall, crude and noisy and cheap; (3) an independent entailment pass with a different prompt, fallible but independent. And the residual is bounded by construction, because `assemble_prompt` emits preamble + the exact confirmed bullets + the raw transcript in an appendix — so a dropped constraint is still visible to Claude even when the bullets missed it.

### `jarvis.presence`

**Lives in:** Signals written by many; the 5-second idle probe is a task in jarvis-dispatch. `evaluate_presence` is a pure function of the signals table plus the override row.

Answers two separate questions the build sheet conflates: presence (can I be HEARD if I speak into the room?) and reachability (which channels can reach the user at all?). Gates plan-mode defer, task-completion routing and the morning briefing. A first-class subsystem the critic correctly found had no mechanism anywhere in six research briefs.

```python
def idle_seconds() -> float | None      # None => 'unknown', which routes like 'maybe'
                                        # but NEVER suppresses escalation
def set_signal(con, source: str, value: dict, ttl_s: int) -> None
def evaluate_presence(con) -> Presence
def set_override(con, mode: str|None, ttl_s: int, by: str) -> None

@dataclass
class Presence:
    state: Literal["present","maybe","away","asleep","unknown"]
    since: str; confidence: float
    reachable: list[Literal["desk","telegram","phone"]]
    reason: str        # read back verbatim on 'where do you think I am?'
```

> PLATFORM TRAP, named because it silently kills the whole subsystem: XScreenSaverQueryInfo returns a constant 0 forever on Wayland. Probe order is Wayland/GNOME (`org.gnome.Mutter.IdleMonitor.GetIdletime` on `/org/gnome/Mutter/IdleMonitor/Core`, returns uint64 ms) -> logind `IdleHint`/`LockedHint` -> X11 XScreenSaver -> macOS `CGEventSourceSecondsSinceLastEventType` -> Windows `GetLastInputInfo`. A startup self-test asserts idle time actually rises across a 2-second pause.

### `jarvis.effects (undo) + jarvis.kill`

**Lives in:** effects: library, written by whoever causes the effect. kill: any process can fire it; jarvis-dispatch is the parent that actually reaps.

The honest undo ledger and the kill switch. Undo is three reversibility classes decided BEFORE execution, stored on the effect row, and used to choose confirmation strength — which is how `confirm` and `undo` finally get reconciled with `delete_repo` being scoped off. Kill is a row plus a poke plus a signal, because a wedged runner will never poll its way to a kill.

```python
CONFIRM_STRENGTH = {"reversible": "notify",
                    "compensatable": "confirm",
                    "irreversible": "confirm_readback"}

def record_effect(con, *, kind: str, summary: str, reversibility: Reversibility,
                  job_id=None, provider_ref=None, undo_plan=None,
                  undo_deadline=None, confirmed_by=None) -> Effect
def undo_handler(op: str)                 # decorator -> UNDO_HANDLERS registry
def spoken_effect_line(e: Effect) -> str  # wording GENERATED FROM the class, so it cannot drift

def stop_everything(con, issued_by: str, reason: str) -> str
    # 1. INSERT commands(verb='stop_all'); bump kill_epoch
    # 2. publish + poke every peer            (soft, ~1ms for healthy runners)
    # 3. AND, without waiting: os.killpg(pgid, SIGTERM) -> SIGKILL at 2s
    #    (or systemctl --user kill --signal=SIGTERM jarvis-job-<id>, which owns the cgroup)
    # 4. hangup commands to every live phone channel
```

> `undo_plan` is DECLARATIVE JSON dispatched through a handler registry, never a Python closure — the reference's `core/confirm.py` holds `run: Callable[[], str]` in a single module-level `_pending` slot with a 90-second timeout (verified in source this session), which survives neither a restart nor a second channel nor a remote side effect. A plan written today must be executable by a process started tomorrow. Every process re-reads `kill_epoch` before starting work and refuses if it changed after its spawn, which closes the resurrection race.

### `jarvis.router + jarvis.sched + jarvis.briefing`

**Lives in:** jarvis-dispatch. APScheduler AsyncIOScheduler with SQLAlchemyJobStore pointed at the same SQLite file, so there is one file to back up and one to lock.

Decides which channels a request reaches and when it escalates — the only place that knows 'speak at the desk, call away'. The scheduler NEVER dials: it raises a request and the router decides that a call is how to reach a human right now. The briefing pointer is server-side state, so a dropped channel resumes mid-briefing on another.

```python
def ladder(con, req: Request) -> list[tuple[str, int]]   # [(channel_kind, delay_seconds)]
def call_budget_ok(con) -> bool     # <=1 outbound to owner/hour, <=4/day, none in quiet hours
                                    # unless urgency=='critical' -- and a plan question is NEVER critical

class BriefingScript:
    sections: list[Section]         # Section = {key, title, recital: list[Utterance]}
    pointer: int
    def next(self) -> Section | None
    def skip_to(self, key_or_phrase: str) -> Section | None
    def remaining(self) -> list[str]

def project_status(con) -> list[Utterance]
    # THE MISSING DATA SOURCE, defined: jobs finished/failed since the cursor,
    # jobs still blocked or deferred (with blocked_since), open requests,
    # and open PRs on repos Jarvis itself created.
```

> 'Place a call from a cron job' — the half nobody researched — disappears: the cron job raises a `briefing_gate` request with options [Now, Five minutes, Skip today]; 'Five minutes' writes `deliver_after = now+300`, which is the same column quiet hours already needed. The morning briefing, a deferred plan-mode question and a task-finished notice are literally the same code path with a different `kind`.

### `jarvis.channels.telegram (stage 3)`

**Lives in:** jarvis-telegram, its own process, long-polling (no webhook, no tunnel).

The second channel, and the one that retires the stall risk. Authenticated identity with no PIN, inline keyboards that carry (request_id, option_index), lossless screenshots, voice notes in both directions, /kill. Zero carrier, zero KYC, zero inbound port, zero per-message cost.

```python
class TelegramChannel:
    async def present(self, req: Request) -> None      # literal text + numbered inline keyboard
    async def send_voice(self, utts: list[Utterance], *, lang: str) -> None
    async def send_screenshot(self, png: Path, caption: str) -> None   # sendDocument, NOT sendPhoto
    async def on_callback(self, q) -> None             # payload = (request_id, option_index)
    async def on_voice(self, msg) -> None              # ogg/opus -> 16k PCM -> transcribe -> intent
def authorised(user_id: int) -> bool                   # ONE allowlisted id, hard equality
```

> On this channel plan-mode fidelity is FREE and TOTAL: the option labels are printed literally and the answer comes back as an index, so neither the question nor the answer round-trips through language at all. That is why it lands before the two-voice verdict has to be final — it is the exact mirror that makes the audible seam survivable. `sendPhoto` re-encodes to JPEG and smears terminal text; `sendDocument` preserves PNG to 50MB. A bot may delete its own message only within 48h, which is what `undo_deadline` stores.

### `jarvis.phone (stage 6)`

**Lives in:** jarvis-phone, its own process, registering OUTBOUND over wss so there is no inbound port on a Turkish residential line.

A LiveKit Agents worker that dials out and answers in. It is a PEER PROCESS, not a client: it imports jarvis.bus/jobs/ledger/memory, opens the same jarvis.db, and loads the same ToolRegistry filtered to its profile. This is the direct answer to the critic's unanswered question — same memory, same actions, same in-flight job, same activity log — with no RPC and no new transport.

```python
async def entrypoint(ctx: JobContext) -> None
async def place_call(to: str, profile: SessionProfile, *,
                     opening_request: str | None = None,
                     script: list[Utterance] | None = None) -> CallResult

@dataclass
class CallResult:
    status: Literal["answered","no_answer","busy","voicemail","failed"]
    duration_s: float; transcript: str; outcome: dict | None
    disclosure_spoken: bool

def on_dtmf(ev: rtc.SipDTMF) -> None     # '*9' -> kill.stop_everything(origin='dtmf'), no PIN
                                         # digits -> pin.verify or request answer

# Third-party leg tool surface, FROZEN at exactly two:
#   report_outcome(status: Literal['booked','unavailable','will_call_back','no_answer','unclear'],
#                  slot: str | None, party_size: int | None, exact_words: str)
#   end_call(reason: str)
```

> RESOLVED AMBIGUITY, because judge 2 flagged it as the one way this design could still lose the day the phone arrives: jarvis owns the LiveSession over raw LiveKit tracks (`rtc.AudioStream.from_track(sample_rate=16000)` in, `rtc.AudioSource` out). `livekit-plugins-google` `RealtimeModel` is NOT used, because it creates and owns its own Gemini session, which would make the verbatim sink and the channel-filtered tool registry into plugin internals and leave the fidelity guarantee with no home on the phone. Cost: a resampler the SDK already ships plus local VAD. Continuity is injected at connect time as a `session_briefing` string from `narrate_day()` + `jobs.running()`, not by sharing conversational history.

---

## Repository layout

```
personal-agent/                       # github.com/Efkrdnz/personal-agent — empty today
├── pyproject.toml                    # pins: claude-agent-sdk>=0.2.153,<0.3 ; google-genai>=2.23,<3
│                                     #       pywebrtc-audio>=0.2,<1 ; sherpa-onnx>=1.13.8,<2
│                                     #       sounddevice>=0.5.6 ; soxr>=1.1 ; apscheduler==3.11.3 ; keyring
├── LICENSE                           # MIT — this tree is an independent build
├── CREDITS.md                        # acknowledges Mark-LIII as inspiration; NO source copied
├── CONTRIBUTING.md                   # rule 1: nothing from mark-liii enters this tree
├── .gitignore                        # comments on their OWN lines; CI verifies with git check-ignore -v
├── README.md
├── docs/adr/
│   ├── 0001-sqlite-is-the-bus.md          0005-two-voices-deliberately.md
│   ├── 0002-driver-is-its-own-process.md  0006-telegram-before-pstn.md
│   ├── 0003-no-code-from-mark-liii.md     0007-undo-is-three-classes.md
│   └── 0004-claude-auth-oauth-vs-api.md   0008-livekit-is-a-pipe-not-an-assistant.md
├── deploy/
│   ├── jarvis-dispatch.service       # user unit, graphical-session.target, Restart=on-failure
│   ├── jarvis-voice.service          # needs mic+speakers => needs a logged-in session anyway
│   ├── jarvis-telegram.service       # stage 3
│   ├── jarvis-phone.service          # stage 6
│   └── backup-exclude.txt            # EXCLUDES ~/.claude/projects/** (unredacted transcripts)
├── spikes/
│   └── s0_crossproc/{driver.py,answerer.py,RESULTS.md}   # becomes tests/test_defer_resume.py
├── tools/
│   ├── aec_bench.py                  # ERLE + false-barge-in count. Decides headset vs speakers.
│   ├── fidelity_probe.py             # label_exact_recall vs the ASR null baseline
│   └── probe_auth_mode.py  probe_silent_scheduling.py  probe_live_concurrency.py
├── jarvis/
│   ├── ids.py                        # nid(), now() RFC3339 UTC ms, canon(), dedupe_key()
│   ├── clock.py                      # store UTC; Europe/Istanbul (+03, no DST) only at speak time
│   ├── config.py                     # TOML at ~/.config/jarvis/config.toml. Never a secret.
│   ├── secrets.py                    # keyring; probes at startup, FAILS LOUD, never degrades
│   ├── db.py                         # connect(), tx(), migrate()
│   ├── migrations/001_init.sql …     # applied in order; version in PRAGMA user_version
│   ├── bus.py                        # events table + Peer + UDS datagram poke
│   ├── requests.py                   # THE unified gate (see components)
│   ├── jobs.py                       # JobStore
│   ├── reconcile.py                  # runs at the start of EVERY process; idempotent
│   ├── effects.py                    # undo ledger + three reversibility classes + handler registry
│   ├── ledger.py                     # activity narration + spend, three units, `unpriced` spoken
│   ├── outbox.py                     # side effects leave here; at_most_once for phone.dial
│   ├── presence.py                   # signals, hysteresis, override, per-platform idle probes
│   ├── kill.py                       # kill_epoch + commands + killpg. Three triggers, one path.
│   ├── router.py                     # the reach ladder; call budget; escalation timers
│   ├── sched.py                      # APScheduler + SQLAlchemyJobStore on the SAME sqlite file
│   ├── memory.py                     # SQLite table + recall tool. No vector store.
│   ├── presenter.py                  # Protocol with NO method that returns a decision
│   ├── audio/
│   │   ├── bus.py                    # AudioBus: the ONE output stream, priority arbitration
│   │   ├── micbus.py                 # single-writer N-cursor ring
│   │   ├── legs.py                   # DeskLeg (duplex 48k + AEC3) | PhoneLeg (LiveKit tracks)
│   │   ├── devices.py                # re-derived probing; ASSERTS capture and render are one device
│   │   ├── vad.py wake.py spotter.py # Silero / openWakeWord / sherpa-onnx KWS — all MicBus readers
│   │   └── turn.py                   # TurnController: duck-confirm barge-in, activity_start/end
│   ├── voice/
│   │   ├── verbatim.py engines.py cache.py chunk.py
│   │   └── router.py                 # OutputRouter, Utterance(text, fidelity, lang, tag)
│   ├── live/ session.py profiles.py lease.py
│   ├── tools/ registry.py ctx.py builtin/{code_build,repo,job_control,memory,spend,presence}.py
│   ├── cc/                           # process P2 — imports NO audio, NO genai, NO Qt
│   │   ├── driver.py                 # python -m jarvis.cc.driver --job-id J [--resume]
│   │   ├── hooks.py                  # PreToolUse: log + the defer decision (and its fallback)
│   │   ├── narrate.py                # questions -> spoken script (PURE function)
│   │   ├── answers.py                # spoken reply -> INDICES -> labels by local lookup
│   │   ├── project.py                # repo create-first, Turkish-safe slug, clone, mode classifier
│   │   └── stream.py                 # jobs/<id>/stream.ndjson writer + byte-offset tailer
│   ├── spec/ tidy.py schema.py
│   ├── channels/
│   │   ├── base.py                   # Channel protocol + Capabilities
│   │   ├── desk.py cli.py
│   │   ├── telegram/ channel.py auth.py voicenote.py screenshot.py
│   │   └── phone/ worker.py outbound.py dtmf.py pin.py third_party.py
│   ├── briefing/ script.py navigator.py sources/{projects,gmail,github,youtube}.py
│   └── apps/ dispatch.py voice.py telegram.py phone.py cli.py
└── tests/
    ├── test_defer_resume.py          # the stage-0 spike, in CI forever
    ├── test_request_cas.py           # two answerers race; exactly one wins
    ├── test_answers_validate.py      # an invented label can never reach updated_input
    ├── test_tidy_no_drift.py         # property: source_span containment, both directions
    ├── test_verbatim_bytes.py        # byte-equality of the spoken string through every renderer
    └── test_gitignore_effective.py   # git check-ignore -v on every secret pattern
```

---

## Data model

```sql
-- jarvis/migrations/001_init.sql   ~/.local/state/jarvis/jarvis.db
PRAGMA journal_mode = WAL;  PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;   PRAGMA busy_timeout = 5000;
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
```

---

## The bus

TRANSPORT. One SQLite file in WAL mode at `~/.local/state/jarvis/jarvis.db`, plus a best-effort one-byte datagram poke over a Unix socket at `$XDG_RUNTIME_DIR/jarvis/poke/<peer_id>.sock` (mode 0600, and `XDG_RUNTIME_DIR` is wiped at logout so stale sockets clean themselves up). The poke is a PURE LATENCY OPTIMISATION: if every poke were lost the system would still be correct, just up to 250ms slower, because every reader also polls at `Peer.POLL_S = 0.25`. Errors on send are ignored and dead addresses are reaped by the heartbeat.

Why not a broker: this system carries roughly 300 control messages a day and needs history as the primary artifact (the honesty log, spend totals, "what did you do today" is a `SELECT`), not as a retention policy. Redis would be a second always-on daemon whose persistence you would have to tune to get the durability you were going to need SQLite for anyway — two sources of truth, and the interesting bugs live in the gap. A dedicated broker process reintroduces exactly the failure the seams exist to escape: one process whose death stops everything.

Three SQLite properties do all the work, and none survives a multi-writer store: (1) exactly one write transaction at a time, so `INTEGER PRIMARY KEY AUTOINCREMENT` is assigned in commit order and a subscriber reading `WHERE seq > cursor ORDER BY seq` can never see a hole that later fills in; (2) `UPDATE … WHERE id=? AND state='pending' RETURNING *` is the entire "exactly one channel wins this answer" mechanism, no locks and no leader; (3) the backup story is `cp`.

TWO PLANES. Control (rare, durable, globally ordered) is the `events` table. Data (hundreds of lines a minute of Claude Code output) is `~/.local/state/jarvis/jobs/<job_id>/stream.ndjson`, one JSON object per line, consumed by byte offset stored in `consumers`. That split is what lets jarvis-voice die mid-build and resume narrating exactly where it left off: the runner is a different process and never stopped writing; the desk reopens, seeks to its stored offset, and reads to the last complete newline. Rotation at 8MB to `stream.1.ndjson`; consumers store `(generation, byte_offset)`.

MESSAGE SCHEMA — these dataclasses ARE the wire format; every JSON column serialises one.

```python
EventKind = Literal[
  "job.created","job.started","job.progress","job.blocked","job.deferred","job.resumed",
  "job.parked","job.orphaned","job.finished","job.failed","job.killed",
  "request.created","request.offered","request.answered","request.consumed",
  "request.expired","request.cancelled","request.superseded",
  "effect.recorded","effect.undone","effect.undo_failed",
  "presence.changed","channel.attached","channel.detached",
  "command.issued","command.acked",
  "tool.used","tool.denied","spend.recorded",
  "speech.said","call.placed","call.ended","telegram.sent"]

@dataclass(frozen=True, slots=True)
class Event:
    seq: int; id: str; ts: str; kind: EventKind; actor: str
    payload: dict[str, Any]
    job_id: str | None = None; request_id: str | None = None
    channel_id: str | None = None; effect_id: str | None = None
    redacted: bool = False

class Item(TypedDict):
    index: int          # 1-based. OURS. Never reordered, never renumbered per channel.
    label: str          # verbatim option label
    description: str    # verbatim option description

class Presentation(TypedDict):
    verbatim: bool          # True => MUST go to the deterministic reader, never to Gemini
    intro: str
    items: list[Item]
    multi: bool
    allows_free_text: bool
    free_text_prompt: str
    dtmf_map: NotRequired[dict[str, int] | None]
    image: NotRequired[str | None]

class Capabilities(TypedDict):
    human: bool         # runner/scheduler = False: they never RECEIVE requests
    speak: bool; verbatim: bool; listen: bool; free_text: bool
    dtmf: bool; buttons: bool; images: bool
    max_options: int    # phone/DTMF: 4 — matches the tool's own 2-4 limit

class Answer(TypedDict):
    answers:  NotRequired[dict[str, str | list[str]]]  # EXACT question text -> EXACT label(s)
    approved: NotRequired[bool]                        # confirm_effect / exit_plan
    text:     NotRequired[str]                         # free text, deny reason, "call back in five"
    sources:  NotRequired[dict[str, str]]              # question -> "option" | "free_text"

@dataclass
class Utterance:
    text: str
    fidelity: Literal["verbatim","natural"] = "natural"
    lang: str = "auto"
    interrupts: bool = False
    tag: str = ""       # logged, so "what did you say" is answerable
```

DELIVERY GUARANTEES, stated per class because they genuinely differ.
- Events: AT-LEAST-ONCE, deduped at write. Every publish carries `idem_key`; the insert is `INSERT OR IGNORE` on its unique index. Natural keys: `job:<id>:state:<s>:<attempt>`, `req:<id>:answered`, `eff:<id>:applied`, `tool:<tool_use_id>`.
- Consumers: AT-LEAST-ONCE, idempotent by construction. The cursor advances after handling, so a crash mid-handle replays. Every handler is an upsert or is guarded by `deliveries.presented_at`, so a duplicate poke can never read a question aloud twice.
- Answers: EXACTLY-ONCE by the CAS predicate. The loser is told out loud ("never mind, that was answered on the phone") rather than failing silently.
- Outbound side effects: OUTBOX WITH LEASES. Claim inside a transaction, execute OUTSIDE it, mark done in another. A crash between execution and marking leaves an expired claim, which is retried — hence: Telegram has no server idempotency key, so worst case is a duplicate screenshot (both `message_id`s recorded so both can be deleted); GitHub repo create retries return 422 "name already exists", treated as success after verifying ownership; `git push` is naturally idempotent; and `phone.dial` is the system's ONLY `at_most_once=1` op. Its row moves to `inflight` and is committed BEFORE the provider call, and `reconcile()` explicitly refuses to retry it: `UPDATE outbox SET state='needs_human' WHERE state='inflight' AND at_most_once=1`. Jarvis then says "I may have already called them — want me to check?". Exactly-once is impossible here and double-dialling a restaurant is worse than not dialling.

STRUCTURAL RULE: no network or subprocess I/O inside `with tx(con)`, ever. That is the entire reason the outbox exists. At this message rate a real `SQLITE_BUSY` means someone broke the rule, so it is logged loudly with the caller's stack rather than retried away.

RESTART BEHAVIOUR. `reconcile(con, actor)` runs at the start of EVERY process and is idempotent, so concurrent reconciles are fine. It: reaps channels whose heartbeat is >30s stale; expires past-deadline commands; returns expired outbox claims to `pending` EXCEPT at-most-once rows in `inflight`, which go to `needs_human`; releases deliveries whose channel is gone and re-routes the request; marks jobs `orphaned` when `boot_id` differs or the pid/start-ticks check fails; and then converges two paths onto one — jobs in `deferred` whose request is now `answered`, and `orphaned` jobs with `resume_policy='auto'` and `resume_count < 3` — by spawning `jarvis-ccjob --resume`. Durable replay after a restart is free rather than a second mechanism: a subscriber that was down when `command.issued` was published simply reads `seq > cursor` on boot and sees it. The user-visible contract: "Three things were running when we stopped. I've picked two back up. The third was waiting on a question from you — here it is."

WHAT CANNOT USE THIS BUS. A VPS job. SQLite over NFS/SSHFS is corruption, and a startup check refuses to open a DB whose filesystem is not local. A remote job runs `jarvis-ccjob --stdio` over `ssh -T` and a local `jarvis-bridge@<host>` is the only thing that touches the database. One database, one truth, no network SQLite. (Moot in v1, since cloud mode is cut.)

---

## Driving Claude Code

SDK USAGE. Always `ClaudeSDKClient` from `claude-agent-sdk>=0.2.153,<0.3`, in its own OS process, one per job. Never a bare `claude -p`: a `-p` run has NO permission host and can never receive an `AskUserQuestion`, which was the single most expensive wrong turn available. Never `--bare`: it explicitly does not read `CLAUDE_CODE_OAUTH_TOKEN`, so the "hardening" recommendation would silently move the project onto metered API billing. Hardening is done with `permissions.blockReadsOutsideWorkingDirectories`, scoped credentials and a default-DENY tool table — not with `--bare` and not with Bash deny rules, which are leaky (`Bash(rm *)` does not match `/bin/rm`). Pin `cli_path` to a known binary: about a third of the payload specifics below are undocumented internals of one build (v2.1.273) and a voice daemon auto-updating its bundled CLI is exactly the failure mode.

```python
def options(self, *, resume: bool) -> ClaudeAgentOptions:
    ident = {"resume": self.job.cc_session_id} if resume else {"session_id": self.job.cc_session_id}
    return ClaudeAgentOptions(
        cwd=self.job.cwd,
        permission_mode=self.job.permission_mode or "plan",   # NEVER "dontAsk"
        model=self.job.model, effort=self.job.effort,
        can_use_tool=self._can_use_tool,
        hooks={"PreToolUse":  [HookMatcher(matcher=None, hooks=[self._pre_tool])],
               "TaskCompleted":[HookMatcher(matcher=None, hooks=[self._task_done])],
               "Stop":        [HookMatcher(matcher=None, hooks=[self._stopped])]},
        env={"CLAUDE_CODE_OAUTH_TOKEN": keyring_get("claude_oauth")},
        include_partial_messages=True, **ident)
```

STARTUP ASSERTION, non-optional. `assert_no_ask_user_question_timeout()` reads the MERGED settings and refuses to start, speaking the reason, if `askUserQuestionTimeout` is set to `60s`/`5m`/`10m` anywhere in user or managed settings. The default is "blocks indefinitely" and that is the only supported configuration. A managed value would auto-close the very voice wait the whole design rests on, and it would look like Jarvis silently losing answers.

can_use_tool — AskUserQuestion. The lookup is idempotent by design, and that is what makes a four-hour phone answer work: the answer was written while the runner was dead; the runner comes back, the question re-fires, and it returns in microseconds.

```python
async def _can_use_tool(self, tool_name, input_data, context):
    tuid = context.tool_use_id

    if tuid in self._defer_requested:          # the CLI DROPPED our defer — see below
        self._defer_requested.discard(tuid)
        publish(con, "job.blocked", actor, {"defer_rejected": True, "tool": tool_name},
                job_id=self.job.id)
        set_job_state(con, self.job.id, "blocked")

    if tool_name == "AskUserQuestion":
        req = self._ensure_request(tool_name, input_data, tuid)   # tool_use_id, then dedupe_key
        ans = self._resolve(req)              # returns instantly if already 'answered'
        if ans is None:
            return PermissionResultDeny(message=(
                "Nobody was available to answer. Stop here, do not assume an answer; "
                "I will re-ask when the user is back."))
        return PermissionResultAllow(updated_input={**input_data, "answers": ans["answers"]})
```

THE ANSWER PAYLOAD, and the four ways to get it silently wrong. `input["questions"]` is an ARRAY of 1-4 items, each `{header (<=12 chars), question, options[2-4]{label, description}, multiSelect}`. (1) `answers` is keyed by the EXACT question text. (2) `multiSelect: true` takes a LIST of labels; single takes one string. (3) "None of these" puts the user's RAW TRANSCRIBED WORDS into `answers[question]` — free text is a valid answer, not a failure. (4) NEVER set `response` alongside `answers`: Claude then receives "The user responded: …" and the per-question answer list is silently discarded. Build as `{**input_data, "answers": …}` so every original field is echoed byte-identical; the validator refuses `changed_shown_field`, and a value over 8192 chars, and an array longer than `optionCount + 1`. Does not reach `can_use_tool` from subagents — an SDK MCP tool marked `_meta["anthropic/requiresUserInteraction"]` is the documented fix if subagent questions ever matter.

ExitPlanMode arrives the same way with `{"plan": …}`. Approve with `PermissionResultAllow(updated_input=input_data)`; deny with a message and Claude goes back to planning, which is exactly what "no, change the storage" should do.

DEFER — and the trap nobody else caught. Reading the shipped CLI's strings, `permissionDecision: "defer"` is DROPPED WITH ONLY A WARN LOG in three cases: when more than one tool call is in the assistant batch ("defer is solo-only — siblings would be orphaned on resume"); in interactive (non-print) mode; and it is converted to a hard DENY for calls served to a cloud session ("deferral is not supported for calls served to a cloud session") — which independently confirms cloud sessions cannot carry plan mode. Therefore the runner NEVER assumes a defer took. The hook records `tool_use_id` in `_defer_requested`; if `can_use_tool` is then invoked for that same id, the defer was ignored and the runner falls through to blocking, publishing `job.blocked` with `defer_rejected: true` so the honesty log records the pattern instead of it being mysterious. Blocking is cheap precisely BECAUSE the runner is its own process: it costs one idle process, not the assistant.

```python
async def _pre_tool(self, data, tool_use_id, ctx):
    publish(con, "tool.used", f"runner:{self.job.id}",
            {"tool": data["tool_name"], "input": data["tool_input"], "tool_use_id": tool_use_id},
            job_id=self.job.id, idem_key=f"tool:{tool_use_id}")
    if data["tool_name"] == "AskUserQuestion":
        req = self._ensure_request(...)      # create the row BEFORE deciding to defer, so the
                                             # payload survives even if we exit a millisecond later
        if should_defer(con, req):           # presence in ('away','asleep')
            self._defer_requested.add(tool_use_id)
            set_job_state(con, self.job.id, "deferred", blocked_request_id=req.id)
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                    "permissionDecision": "defer",
                    "permissionDecisionReason": "User is away; Jarvis will reach them and resume."}}
    return {"continue_": True}
```

DEFER -> RESUME, the full path across process death. Run ends with `stop_reason == "tool_deferred"`; the driver writes `jobs.state='deferred'` with `cc_session_id` and `cwd` and exits. The `requests` row now outlives every process. Any channel can answer it — a Telegram button tap, a spoken reply at the desk, the phone worker's `answer_decision` tool — minutes or eleven hours later. THE ORDERING RULE IS THAT THE ANSWER IS DURABLE BEFORE THE RESUME: `answer_request()` commits the row, and only then does `jarvis.cc.resumer` (a supervised loop in jarvis-dispatch, polling for `answered` requests whose job is `deferred`) spawn `python -m jarvis.cc.driver --job-id J --resume`. The question re-fires into `can_use_tool`, `find_open_for_tool` hits on `tool_use_id` or `dedupe_key`, and the stored answer returns with no blocking, no race, and no human involved twice. Cross-machine works because the only artifact is a row.

MODEL AND EFFORT BY VOICE. `model=` and `effort=` are real per-session options (`ClaudeAgentOptions.effort` -> `--effort`, `EffortLevel = Literal["low","medium","high","xhigh","max"]`, and the field also accepts an int). BUT `high` IS THE DEFAULT on every model except Opus 4.7 — so "use Opus 5 with high effort" resolves to a no-op and will look broken the first time it is tested. The tidy pass therefore maps effort words to the levels that actually change behaviour, and when the user says "high" Jarvis says out loud: "Opus 5, high effort — that's the default, so nothing changes; say extra-high or max if you want it to think harder." That is a two-line honesty fix for a demo that would otherwise feel fake.

HOOKS. `PreToolUse` on `matcher: None` does the activity log and the defer decision. `TaskCompleted`, `Stop` and `Notification` are the "go find the user" primitive for R4: they publish an event, the router consults presence, and the outcome is spoken at the desk or pushed to Telegram or (stage 6) escalated to a call. A hook NEVER renders anything and never decides routing — it emits and returns. Because Claude Code is always driven through the SDK in P2, the hook is always an in-process Python callable sharing P2's connection, so the SDK-vs-subprocess fork the critic identified simply does not arise; there is exactly one hook implementation and one redaction path in the tree. Timeout is set explicitly to 55s so it is under the CLI's 60s default and nowhere near the 600s one.

PERMISSION POLICY PER CHANNEL, in data not code. `Tool.channels` is a default-DENY tuple, so a tool is unavailable on the phone unless it opts in. Concretely: desk gets everything; Telegram gets everything except placing calls; the phone-to-owner leg runs `permission_mode="plan"` with a default-DENY `can_use_tool` table — no push, no install, no sudo, no reads of `~/.ssh`, `~/.claude` or `**/.env`; and the third-party leg has exactly two tools and therefore physically cannot commit to anything else. `permission_mode="dontAsk"` DENIES AskUserQuestion outright, so it is forbidden by a CHECK constraint on the jobs table rather than by a comment. The GitHub token has `delete_repo` OFF, so deletion is impossible rather than merely disallowed.

NARRATION POLICY, per channel. The driver never speaks and never knows who is listening; it writes `stream.ndjson` rows with `kind in {assistant_text, tool_use, tool_result, milestone, result}`. A Narrator in whichever process currently owns the user claims the job (`UPDATE jobs SET narrator_owner=?`) and tails by offset. At the desk: sentence-level narration at `fidelity='natural'`, so Gemini says "it's installing dependencies" conversationally. On a phone call: MILESTONES ONLY — narrating a compile log down a metered PSTN line is wrong on cost and on attention. Quoted file paths, commands, plans and option text are always `fidelity='verbatim'`. Moving narration from desk to phone is one UPDATE.

---

## Voice fidelity — the verbatim problem

THE RULE, and it is a type rather than a convention: LOAD-BEARING TEXT NEVER PASSES THROUGH A GENERATIVE MODEL ON ITS WAY TO THE USER. "Make the Live model recite verbatim" is not solvable to the standard R2 needs — the Live API has no way to hand the model a string and get that string back as audio, `send_client_content` is documented as a CONTEXT PREFILL mechanism whose docstring warns that interleaving it with `send_realtime_input` "is not recommended and can lead to unexpected results", and `AsyncSession` has no `interrupt()` at all, so you cannot even reliably make it stop. So the framing is refused and a second, deterministic speech path is added. That is not a workaround; a conversationalist and a reader are different jobs.

THREE TIERS, because scoping is what makes this cheap. EXACT: option `label` strings, the ordinal-to-label binding, the confirmed requirement-list items, the chosen-option read-back, the AI-disclosure line. These are answer keys and contract text; one wrong word silently builds the wrong thing. FAITHFUL: the `question` text, the `ExitPlanMode` plan body, quoted paths and commands — the user must understand them but nothing maps against them. FREE: "Claude Code has a question about storage", progress narration, briefing prose. The entire EXACT payload for a full four-question plan-mode round is roughly sixteen labels of one to five words — a few hundred bytes. Everyone's instinct is "we need verbatim TTS for the whole assistant"; you need it for about 300 bytes at a time.

ENGINES, a tier not a pick. Primary EN: **Kokoro** (82M, local, 24kHz native, G2P + acoustic model with NO language model in the path, so it cannot omit, reorder, translate or invent — a categorical property, not a measured error rate). Primary TR and EN fallback: **edge-tts** (`tr-TR-AhmetNeural`, default format is literally `audio-24khz-48kbitrate-mono-mp3`, deterministic, free). Optional: **`gemini-3.1-flash-tts-preview`** — 24kHz PCM16 mono, and it shares Live's prebuilt voice names (`charon`, `puck`, `kore`, `leda`), so it could read in the exact same timbre as the Live session and the two-voice question would disappear. But it is a prompt-steerable LLM, which is the hazard class being escaped, so it is EXACT-tier only behind a synth→transcribe→compare gate, and FAITHFUL-tier immediately (a dropped adjective in a 90-second plan body is cosmetic and it sounds far better than Kokoro over that length). ElevenLabs is supported by the interface and not recommended: 44.1kHz forces the one resampler this design otherwise avoids.

THE RATE COLLISION IS DESIGNED OUT, not managed. Gemini Live output, Gemini TTS, Kokoro and edge-tts are all natively 24kHz PCM16 mono, so `BUS_RATE = 24000` is universal and there is NO RESAMPLER ANYWHERE on the desk path. The only resample in the system is 24k→8k mu-law inside the phone sink, applied once to the mixed output. The device collision dissolves too, because the engine contract is `synth(text, lang) -> bytes` and an engine is forbidden from touching a device — which is precisely the defect in the reference, where `TTSPlayer.stop()` calls the GLOBAL `sd.stop()` and would kill the Live session's output stream as collateral damage.

KEEPING GEMINI COHERENT — the mechanism, and this is where the deep-dive overrides the proposal. The reader speaks the labels; Gemini must still KNOW what was said or "the second one" resolves against nothing. The authoritative mechanism is a SILENT function response, not a client-supplied model-role turn. Gemini calls `read_options(qid)`; the host mutes the Live track, plays `earcon + framing + numbered options + tail` through the reader, mirrors the exact text to the TUI and Telegram, and returns `types.FunctionResponse(id=fc.id, name=fc.name, response={...}, scheduling=types.FunctionResponseScheduling.SILENT)` whose docstring reads "Only add the result to the conversation context, do not interrupt or trigger generation." The labels land in context without asking for a turn, and the response carries `{"already_read_aloud": True, "do_not_repeat": True, "options": [{"n":1,"label":"SQLite"}, …]}`. Fallback if SILENT still generates: send the option table as a `send_client_content` prefill BEFORE unmuting the uplink. Both are measured in stage 0, before either is built on.

THE ANSWER PATH IS SYMMETRICALLY MODEL-FREE. THE MODEL MAY EMIT AN INDEX, NEVER A LABEL. The numbering is generated locally by `narrate.script()` from `input["questions"][i]["options"]` order — a pure function, so what was spoken is stored in the request row and `jarvis log` can show it. Gemini's only committing tools are `answer_question(qid, picks: list[int])` and `answer_question(qid, free_text: str)`; `explain_option` and `reread_options` are non-committing. Labels are resolved by LOCAL LOOKUP against the frozen `self.questions`. `validate()` raises unless every resulting string is an exact member of `options[].label` and unless the multiSelect rules hold, so a hallucinated, translated or reordered label cannot reach `updated_input` even if a constrained model call misbehaves. And `free_text` is guarded: if it normalises to a near-match (token-set ratio > 0.8) of an existing label, the call is rejected with `{"ok": False, "error": "use_picks"}`, because a genuine "none of these" never looks like the options. Ordinals are parsed locally in English and Turkish ("one and three", "bir ve üç") before any model is consulted.

R2's READ-BACK: FIDELITY BY CONSTRUCTION, NOT BY VERIFICATION. You cannot make a paraphrase faithful, so stop trying to verify the paraphrase and instead make the artifact the user approves BE the artifact that is sent, byte for byte. `tidy()` emits `{requirements: [{id, text, quote}], mode, model, effort, repo_name}` under a JSON schema where `quote` MUST be a normalised substring of the raw transcript. Then three mechanical checks before a word is spoken: literal-token assertion (regex the transcript for numbers, versions, paths, URLs, quoted strings and camelCase identifiers; every one must appear literally in the list — this catches the highest-damage class, "Postgres", "no auth", "port 8080", perfectly and with no model involved); content-lemma recall; and an independent entailment pass with a different prompt. Any flag is READ ALOUD as "I may have missed…" before the read-back. The read-back then reads the `text` strings, numbered, one cached clip each, through the reader. The user edits THE LIST by number — "drop three", "two should say Postgres not MySQL", "add: no Docker" — and edits are applied as LOCAL LIST MUTATIONS, never a re-tidy, because a re-tidy is a fresh chance to drift. Finally `assemble_prompt()` emits `preamble + "Requirements (as confirmed aloud by the user):" + the exact confirmed bullets + "Appendix — the user's own words, unedited:" + raw_transcript`. So even total prose drift cannot inject or lose a requirement, and Claude also receives the raw words as ground truth. Both the transcript and the confirmed list go into the activity log, so the claim is auditable after the fact.

LATENCY. Gemini TTS does not stream and Kokoro synthesises a whole clip, which naively means dead air at the worst possible moment. Two mechanics remove it: chunk granularity is ONE UTTERANCE PER OPTION (a question renders to N+2 clips of 1-2 seconds each), and pre-synthesis fires the instant `AskUserQuestion` lands — `asyncio.gather` over every clip including all read-back permutations — while Gemini is still speaking the free-tier framing sentence. All TTS latency hides behind Live's own utterance. A content-addressed disk cache at `~/.cache/jarvis/tts/<sha256(engine|voice|lang|text)>.pcm` means "SQLite", "Postgres", "Yes", "Skip tests" are a file read after the first time, so "repeat option two" is instant and free.

TWO VOICES: DELIBERATE, PERMANENT, AND A FEATURE. Pick them to be obviously DISTINCT (Live = `Zephyr`; reader = a Kokoro voice of the other gender). The rule that makes it legible rather than jarring: *Jarvis's own voice never utters load-bearing text; the reader voice never converses.* A 150ms earcon brackets every verbatim episode — costs nothing, survives phone-codec degradation, marks the boundary in any recording. Jarvis hands off explicitly: "Reading you the options exactly." Never mix within a sentence; switch at clip boundaries only. Within two days the user has learned something genuinely useful: *when the other voice speaks, those are somebody else's exact words.* That is an audible integrity marker and a direct contribution to R5's honesty requirement — it makes drift NOTICEABLE rather than merely absent. Precedents nobody finds jarring: a train conductor plus recorded announcements; a satnav inside a conversation; a lawyer shifting register to read a clause. Config flag `verbatim.voice_mode = "distinct" | "matched"` exists, with `matched` unlocked only if the fidelity harness clears `gemini-tts`, and `distinct` staying the default because the marker is worth more than the smoothness.

TEXT MIRRORING IS MANDATORY AND ADDITIVE, NEVER A SUBSTITUTE. Every verbatim episode also emits its exact text as an event, sunk to the TUI, to Telegram (plain message plus a numbered inline keyboard) and to the activity log. Routing critical text to a screen INSTEAD of speech is wrong — R2 is voice-first and the phone leg has no screen — but mirroring is pure profit: it gives the user a way to CHECK what they heard, and the Telegram keyboard is a tap-to-answer fallback for a noisy street that no spotter can serve. It is also the escape hatch if the audible seam turns out to be intolerable.

FAILING HONESTLY. If no verbatim engine is available (Kokoro won't load, edge-tts endpoint gone, network down), the system REFUSES rather than silently downgrading to paraphrase. `VerbatimSpeaker.pcm_for` raises `NoVerbatimEngine` after exhausting the ladder; `AudioBus` RAISES if EXACT-tier content is routed to the LIVE track. Jarvis says, in the Live voice (paraphrase is fine here): "My reader voice isn't working, so I can't read you the options exactly. They're on screen and I've sent them to Telegram — tell me the numbers." On a phone leg with no Telegram binding, the question DEFERS. A silent fallback to the paraphrase channel is the precise bug this entire design exists to prevent, so it is made structurally impossible rather than merely discouraged.

MEASURING RATHER THAN HOPING. `tools/fidelity_probe.py` scores `label_exact_recall` — fraction of labels appearing as an exact contiguous substring after NFKC/casefold/whitespace normalisation, IN ORIGINAL ORDER, with the correct ordinal immediately preceding — plus `insertion_rate` and `order_inversions`, against a NULL BASELINE produced by running the same scorer on the verbatim engine's own audio through the same transcription path (otherwise you will blame the model for "SQLite" → "sequel light"). WER is the wrong metric: it averages away exactly the failure that matters, since one wrong word in one label is a 2% WER and a 100% wrong build. Gate fixed in advance: Live may carry EXACT-tier text only at `label_exact_recall >= 0.999` over >=1000 labels with ZERO insertions and ZERO inversions. And be honest about the arithmetic — 200 adversarial payloads is about 600 labels, which cannot demonstrate 0.999 at any useful confidence. THAT ASYMMETRY IS ITSELF THE ARGUMENT: proving the paraphrase path safe costs more than building the verbatim path. One afternoon of Kokoro wiring beats a week of statistics that will probably say no.

---

## The audio graph

ONE GRAPH, FIVE RULES. (1) THE MICROPHONE IS NEVER GATED OFF — the reference's `if not jarvis_speaking …` at `main.py:938-945` is deleted, and the only thing that ever changes is PLAYBACK GAIN. That single deletion fixes barge-in, briefing navigation and the spoken kill switch simultaneously, which is why it is rule one. (2) EXACTLY ONE OUTPUT STREAM EXISTS, owned by `PlaybackMixer`/`AudioBus`, and its POST-FADER, POST-MIX output buffer is the AEC reference signal because it is the literal array handed to PortAudio. Anything that plays audio by another route is invisible to the AEC and WILL be heard as uncancellable echo — a checkable invariant, so CI greps for a second `OutputStream`. (3) ONE MICROPHONE TAP: a single 16kHz int16 mono `MicBus` published by the DSP layer, with wake word, kill-phrase spotter, navigation spotter, VAD and the Gemini uplink all READERS with independent cursors. (4) TURNS ARE CLIENT-DRIVEN ON BOTH PROFILES: `automatic_activity_detection.disabled=True` and we send `activity_start`/`activity_end` ourselves — the one decision that unifies desk and telephone, since the telephony path already demanded local VAD (server VAD tuned for a clean mic false-triggers on line noise) and the desk needs explicit control of when a barge-in counts. (5) EVERYTHING ABOVE `clean16` IS ONE CODE PATH; legs differ only in their DSP front end and which detectors are armed.

```
  mic ──ADC──► indata(48k) ──► EchoCanceller(48000) ◄── far 48k (POST-FADER)
                                     │ clean48                    ▲
                                     ▼                            │
                              soxr 48k→16k                        │
                                     │                            │
  speakers ◄──DAC── outdata ◄────────┼──── PlaybackMixer.pull(960)┘
                                     ▼
              ╔═══════════ MicBus (16 kHz int16, single writer, N cursors) ═══════════╗
              ╚═╤═══════════╤═══════════════╤═══════════════════════╤════════════════╝
                ▼           ▼               ▼                       ▼
          WakeWatch    Silero VAD      PhraseWatch            Uplink (GATED ON
       ("hey_jarvis",  (512-sample     (sherpa-onnx KWS:       ACTIVITY, ducked,
        armed ASLEEP/   32 ms frames)   KILL set / NAV set —    never muted)
        LISTENING)          │           armed by state)              │
                            └──────► TurnController ◄────────────────┘
                                  preroll ring 320 ms
                                  duck-confirm barge-in
                                  self-speech veto
                                       │ events
                                       ▼  jarvis.bus  →  kill / nav / wake / barge_in
```

DEVICE LAYER. `DEV_RATE = 48000`, `BLOCK = 960` (20ms), one FULL-DUPLEX `sd.Stream` on ONE PHYSICAL DEVICE. Duplex on a single device gives ONE HARDWARE CLOCK for capture and render; aggregating a USB mic with separate speakers works on most host APIs but PortAudio does NO drift correction between them, and a few ppm of drift is the classic reason AEC quietly stops working after two minutes. So `devices.py` ASSERTS it at startup and says so out loud rather than silently degrading — unlike the reference's `audio_devices.resolve()`, which returns `None` for both "system default" and "device has vanished". 48kHz rather than 24kHz because the path to the DAC is then bit-exact with no hidden OS resampler between the reference tap and the speaker, which makes the delay estimate honest; `soxr` costs microseconds. 20ms rather than 100ms because every extra millisecond lands on the barge-in budget.

THE REFERENCE DELAY IS FREE. In the duplex callback, `stream_delay_ms = (tinfo.outputBufferDacTime - tinfo.inputBufferAdcTime) * 1000`. AEC3 has its own estimator; the hint just makes it converge in ~0.5s instead of ~3s. Re-set only when it moves by >5ms, to avoid thrashing the filter.

AEC LIBRARY — the assessment, because three obvious choices are dead and one does not exist. `pyaudio-webrtc-apm` (named in the original problem statement) DOES NOT EXIST ON PyPI. `speexdsp` last shipped 0.1.1 in July 2018, sdist only, and Speex MDF is a generation behind AEC3. `webrtc-audio-processing` (xiongyihui) last shipped 2019 with armv7l wheels only. `aec-audio-processing` 1.0.1 ships Windows-only wheels. USE **`pywebrtc-audio` 0.2.0**: WebRTC AEC3 (the algorithm in Chrome), wheels for cp310-cp314 across manylinux/musllinux x86_64 and aarch64, macOS x86_64 and arm64, and win_amd64; `EchoCanceller(sample_rate, num_channels, stream_delay_ms)` with `.process(near, far) -> ndarray` and a runtime-writable `stream_delay_ms`; C++ WITH THE GIL RELEASED, so it runs in the PortAudio callback (~250µs per 20ms block at 48k) without stalling the asyncio loop. Keep `livekit`'s `rtc.AudioProcessingModule` as a drop-in second source — it wraps the same AEC3, is prebuilt for all three platforms, and the swap is about thirty lines.

TWO APM STAGES, NOT ONE. Stage A in the callback at 48kHz: `EchoCanceller` ONLY. A high-pass is always applied before AEC regardless of settings, which is what you want; when the far end is silent AEC3 is nearly transparent, so the detectors see essentially the raw mic. Stage B in the uplink worker at 16kHz: `NoiseSuppressor(level=1)`, AGC OFF by default — AEC+NS+AGC is tuned for a human listener and distorts speech in ways that hurt keyword spotting, and AGC pumping on an open desk mic causes more trouble than it fixes. (Phone leg: NS level 2 and AGC ON, because PSTN levels vary 30dB.)

BARGE-IN IS DUCK-ON-SUSPICION, THEN CONFIRM — and this is the move the whole design turns on. When Silero reports speech for 3 consecutive 32ms frames while Jarvis is speaking, DUCK PLAYBACK TO −20dB over one 20ms block. Gemini keeps generating; nothing is sent. Then a 200ms CONFIRM WINDOW: if the VAD is still firing with the echo 20dB down, it is real — `mixer.flush()`, `activity_start` (which cancels generation), flush the 320ms pre-roll, stream live. If the VAD stopped, it was residual echo: ramp back over 50ms and discard. The cost of a false positive is a barely audible 200ms dip, NOT a lost turn. Why this is the right shape: IT DROPS THE BAR AEC MUST CLEAR from "deliver clean ASR-grade audio during double-talk" (30+ dB of ERLE, at the mercy of the room) to "let a VAD make a decision in the presence of echo" (15-20dB), because by the time words are being captured the duck has removed another 20dB and then the flush removes the echo entirely.

HONEST LIMIT: there is no way to command the Gemini Live model to stop. `AsyncSession` exposes only send / send_client_content / send_realtime_input / send_tool_response / receive / start_stream / close — no `interrupt()`. You have two levers: LOCAL DROP (100% reliable, costs tokens for audio nobody hears) and server-side barge-in via a synthetic `activity_start` under `START_OF_ACTIVITY_INTERRUPTS`, which LiveKit's production plugin asserts works ("Gemini Live treats activity start as interruption") but which is unverified on this model. POLICY: always do the local drop; additionally send the synthetic barge-in if the probe clears it; NEVER depend on the server lever. Late arrivals from the LIVE track are dropped by a 2-second staleness TTL. Hangover before `activity_end` is 700ms, not 100ms — Google's manual-VAD guidance says at least 500ms or audio quality degrades, and the 100ms example in the Live guide is for SERVER VAD.

THE SPOTTER, and why it makes three features cheap. During verbatim playback the uplink is gated (Live must not "hear" the reader and treat it as the user), so server VAD cannot help. A LOCAL CLOSED-VOCABULARY SPOTTER (sherpa-onnx `KeywordSpotter`, open-vocabulary so no training is needed) runs on the un-gated MicBus at all times: `stop | repeat | again | next | skip to X | back | one..four | yes | no | jarvis full stop`. Small vocabulary, very low false-accept, local, instant, works with the uplink shut and with the network down. It is the SAME subsystem that R4's briefing navigation and R5's kill switch need — one graph, three requirements. Briefing "next" passes THREE GATES, all of which must hold: (1) CONTEXT — armed only in BRIEFING/READOUT, which removes ~95% of the problem; (2) TURN SHAPE — the hit must be a barge-in and the utterance must END within 400ms of the keyword, so "Next." is a command and "Next time, remind me to check the inbox" fails the gate and is forwarded whole, pre-roll included, as an ordinary turn; (3) per-keyword score, tuned LOOSE for navigation and for the kill phrase (a missed stop is far worse than a spurious one). Unmatched phrases fall through to Gemini, which has `briefing_next()` / `briefing_goto(section)` / `briefing_repeat()` / `briefing_stop()` as ordinary tools driving the same server-side pointer, so the two paths cannot disagree.

HARD LIMITATION, STATED PLAINLY BECAUSE IT BITES A TURKISH USER. sherpa-onnx ships pretrained KWS models for zh-en and Chinese only. THERE IS NO TURKISH KWS MODEL. So the instant, offline, network-independent phrases — wake word, kill phrase, "next", "skip to inbox" — MUST BE ENGLISH. Turkish navigation still works via the slow path (Gemini hears it and calls the tool), which means Turkish has no OFFLINE kill switch. Mitigation: the kill switch has two language-free non-audio paths anyway — a global hotkey and DTMF `*9` — and they are more reliable than any spoken phrase. This is an open decision the user must accept explicitly.

SELF-SPEECH VETO. Jarvis must not wake or kill himself by reading his own words. `output_audio_transcription` is already enabled, so keep a rolling 9-second window of Jarvis's OUTPUT transcript; a detector hit whose phrase appears in that window is discarded for the utterance plus one second. Without this, reading aloud a GitHub issue containing "jarvis full stop" halts the system.

THE PHONE LEG IS THREE BOXES SWAPPED. No AEC (the carrier and handset do it; a second AEC on a line that already has one makes things worse). NS level 2 + AGC on. `rtc.AudioStream.from_track(sample_rate=16000)` in and `rtc.AudioSource` out instead of PortAudio. `confirm_ms = 0`, because there is no local echo to confirm against. Everything else — MicBus, TurnController, PhraseWatch, the state machine, the event bus — is byte-identical. Mime type is `audio/pcm;rate=16000` on BOTH legs; the reference's bare `"audio/pcm"` at `main.py:914` is a latent chipmunk bug the moment a second rate appears. Never tag `rate=8000` and let the server upsample; resample mu-law locally.

LATENCY BUDGET, desk. ADC + 20ms block + 5-15ms host latency + 0.25ms AEC + 0.02ms soxr + 96ms Silero onset = ~150ms to the DUCK, which is what the user perceives as the interrupt and is inside the ~300ms conversational tolerance; plus 200ms confirm plus 20-40ms drain = ~380ms to silence. First levers if it feels slow: `onset_frames = 2` (64ms), then `confirm_ms = 120`. Do NOT shrink the block below 20ms — Python PortAudio callbacks start glitching on GIL contention. CPU, worst case with everything armed: ~9-10% of one core, roughly 1% of an eight-core machine.

IS AEC GOOD ENOUGH? MEASURE, DO NOT HOPE. `tools/aec_bench.py`, one hour, on the actual desk. TEST 1 single-talk: silent room, play 30s of real Gemini output at normal volume, compute `ERLE = 10·log10(mean(near²)/mean(clean²))` over high-far-energy frames after discarding 3s of convergence, and count FALSE BARGE-INS the duck-confirm chain would have fired. TEST 2 double-talk: 20 short spoken barge-ins from the normal seat; log detection latency. TEST 3: sweep three volumes and with the desk fan on — loud is the interesting one, because AEC3 cancels the LINEAR echo path and a cheap speaker driven hard produces nonlinear distortion no linear filter can remove.

DECISION RULE, fixed before running. Median ERLE >= 25dB AND >= 18/20 barge-ins within 300ms AND <= 1 false barge-in per 30s → SHIP OPEN SPEAKERS. 15-25dB → ship with a 6dB playback cap and a documented mic position, `onset_frames = 4`, re-measure. Below 15dB, or > 3 false per 30s, or ERLE degrading by > 6dB over ten minutes (THAT IS CLOCK DRIFT) → walk the ladder.

THE LADDER, cheapest first: (1) move things — mic >= 40cm from the speakers, not aimed at them, one notch quieter; free and usually worth 10dB. (2) Give the APM a correct `stream_delay_ms`; a misconverged delay is the single most common cause of "AEC doesn't work". (3) Force capture and render onto one device. (4) OS-NATIVE AEC — `libpipewire-module-echo-cancel` on Linux (virtual echo-cancel source/sink, same AEC3, but PipeWire owns both clocks and drift-corrects, AND it cancels other applications' audio, which in-process AEC structurally cannot see), `VoiceProcessingIO` on macOS, the comms-category APO on Windows; then open the virtual device with `echo_cancellation=False`. (5) **A WIRED USB HEADSET, ~$40, 30-40dB of acoustic isolation, single clock by construction — and this is the RECOMMENDED DEFAULT.** Open-speaker operation is the upgrade AEC buys, not the baseline it must deliver. Google's own Live API best practices say to use headphones to prevent self-interruption. For one developer on a personal budget, $40 to delete a class of bug is proportionate; three weekends tuning AEC3 for a room is not. (6) A hardware AEC speakerphone (~$50-130) if genuinely hands-free open-room is wanted. (7) PUSH-TO-TALK — a hotkey or a DTMF key — BUILT REGARDLESS of how the measurement comes out, because it is the deterministic escape hatch for the moment everything else is confused, and it costs an afternoon.

WHAT IS LOST IF AEC IS INADEQUATE AND THE USER REFUSES A HEADSET: open-speaker barge-in, and nothing else. The kill switch keeps the hotkey and DTMF. Briefing navigation keeps push-to-talk and the DTMF path. The wake word is unaffected (nothing is playing when Jarvis is asleep). The phone leg never needed AEC. Nobody should treat the AEC measurement as a go/no-go on the project.

---

## Presence

TWO AXES THE BUILD SHEET CONFLATES, and separating them is most of the design. PRESENCE: can I be HEARD if I speak into the room? REACHABILITY: which channels can reach the user at all right now? A user on a bus is `away` for presence and `[telegram, phone]` for reachability; a user at the desk with headphones off is `present` and `[desk, telegram]`.

SIGNALS, each a row in `presence_signals` with a TTL.

| source | how | weight | ttl |
|---|---|---|---|
| `idle` | OS idle seconds, polled at 5s in jarvis-dispatch | strong | 15s |
| `lock` | screen lock state (locked ⇒ away) | strong | 15s |
| `wakeword` | last "Hey Jarvis" | DECISIVE POSITIVE | 300s |
| `utterance` | last transcribed user speech at the desk | DECISIVE POSITIVE | 300s |
| `probe` | a spoken question went unanswered for 30s | DECISIVE NEGATIVE | 600s |
| `override` | "I'm going out" / "I'm back" / "don't call me" | beats everything | until expiry |
| `call` | user is on a Jarvis call right now | reachable=phone, presence≠desk | live |
| `telegram` | user messaged the bot in the last 10 min | reachable += telegram | 600s |

OS IDLE, per platform, behind one function `idle_seconds() -> float | None`. Linux/Wayland+GNOME FIRST: D-Bus `org.gnome.Mutter.IdleMonitor` on `/org/gnome/Mutter/IdleMonitor/Core`, method `GetIdletime` → uint64 ms. Linux/any session, coarse fallback: `org.freedesktop.login1` session `IdleHint` / `IdleSinceHint`, plus `LockedHint`. Linux/X11: `XScreenSaverQueryInfo` via `ctypes.CDLL("libXss.so.1")`. macOS: `CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateCombinedSessionState, kCGAnyInputEventType)`, lock via `CGSessionCopyCurrentDictionary()["CGSSessionScreenIsLocked"]`. Windows: `GetLastInputInfo`, lock via session-switch notifications.

THE TRAP THAT SILENTLY KILLS THE WHOLE SUBSYSTEM, named because nothing else would catch it: XScreenSaver returns a CONSTANT 0 FOREVER on Wayland. Presence would pin to `present`, escalation would never fire, and every away-from-desk feature — plan-mode defer, task-completion routing, the morning briefing fallback — would quietly die while appearing to work. Hence: Wayland is probed first, and a startup self-test asserts idle time actually RISES across a two-second sleep. If no probe works, `idle_seconds()` returns `None`, presence becomes `unknown` with `reason = "I can't read the idle time on this desktop"`, and — the important part — `unknown` ROUTES LIKE `maybe` BUT NEVER SUPPRESSES ESCALATION. Being blind degrades toward sending one redundant Telegram message, never toward silence.

STATE MACHINE WITH ASYMMETRIC HYSTERESIS, and the asymmetry IS the design.

```
  present ──(idle>120s sustained)──► maybe ──(idle>600s OR locked)──► away
     ▲                                  │                              │
     └──── wakeword | utterance | idle<15s : INSTANT ──────────────────┘
  away ──(quiet hours AND idle>1800s)──► asleep ──(any input)──► present
```

Fast to become present, slow to become away. A false "away" costs one unnecessary Telegram message. A false "present" costs forty minutes of a stalled build. So: `present` = idle < 120s and not locked → reachable `[desk, telegram]`; `maybe` = 120-600s → `[desk, telegram]`, with desk deliveries getting a 90s answer window before escalating; `away` = idle >= 600s or locked or override → `[telegram, phone]`; `asleep` = away + local time in 23:30-08:30 Europe/Istanbul + idle >= 1800s → `[telegram]` only.

THE CLEVEREST SENSOR IS FREE: THE UNANSWERED QUESTION IS ITSELF THE EVIDENCE. Every desk delivery doubles as a presence probe. If a spoken question gets no audio input at all within 30 seconds, the desk channel writes `presence_signals['probe'] = {"answered": false}` with a 600s TTL — a decisive negative that drops presence to `away` IMMEDIATELY and makes the Telegram delivery due now. This costs nothing, it needs no new sensor, and it is precisely the signal that the OS idle time cannot give you (the screen is not idle; the chair is empty).

MANUAL OVERRIDE, the escape hatch that must exist. Voice or Telegram: "I'm going out" → `mode='away', until=now+4h`; "I'm back" → clear; "don't call me" → `mode='dnd', until=now+8h`; "desk only" → `mode='desk_only'`. Overrides beat every sensor. And "where do you think I am?" reads back `presence_state.reason` verbatim — a debuggability feature that costs one column and saves an evening of confusion.

BOTH FAILURE DIRECTIONS, EXPLICITLY.

CALLING THE USER WHO IS SITTING RIGHT THERE AT 2AM — blocked by four independent gates, any one of which suffices: (1) quiet hours + idle >= 30min puts presence in `asleep`, which removes `phone` from the ladder entirely; (2) a plan-mode question is `urgency='high'` AT MOST, and `critical` — the only level that can dial during quiet hours — is never assigned to one; (3) a 20-second desk probe (a short chime plus "Jarvis needs you") runs before ANY dial when presence is `maybe`; (4) hard caps in `call_budget_ok()`: one outbound call to the owner per hour, four per day.

SPEAKING INTO AN EMPTY ROOM WHILE CLAUDE CODE WAITS FORTY MINUTES — blocked by three: (1) the 30-second unanswered-probe signal; (2) `escalate_after_s` (default 90) on every request, materialised into `deliveries` rows with `due_at` by the router, so Telegram fires whether or not presence was right; (3) `jobs.blocked_since` older than fifteen minutes appears in the morning briefing's project-status section, so even a total presence failure surfaces within a day rather than never.

HOW PRESENCE GATES THE THREE THINGS IT ACTUALLY DECIDES. Plan-mode defer-vs-block: `should_defer()` returns True only for `away`/`asleep`; everything else blocks, because blocking is cheap in a dedicated process. Task-completion routing: `TaskCompleted` publishes, the router's ladder picks desk / Telegram / (stage 6) phone. Morning briefing: the `briefing_gate` request goes to the ladder, which means 90% of mornings end with a tap on a Telegram button and the expensive rung is exercised rarely — which is also how the spend stays at a few dollars a month.

WHY PRESENCE IS AN ACCELERATOR AND NOT A GATE. Because Telegram is in the ladder from stage 3 onward and always fires, being WRONG about presence costs a redundant notification, never a forty-minute stall. That is what keeps this a ~200-line subsystem instead of a first-class thing the system's correctness depends on. Presence makes Jarvis feel attentive; the ladder makes Jarvis reliable. Do not confuse the two.

---

## Correction: LiveKit is a pipe, not the SIP endpoint

The architecture panel ran before the telephony procurement research landed. One finding from that research
changes a detail of the phone layer, and it is verified in code rather than inferred:

**LiveKit Cloud cannot be the SIP endpoint against a Turkish operator.** `livekit_sip.proto`'s
`SIPInboundTrunkInfo` and `SIPOutboundTrunkInfo` contain no registrar, contact, expiry or binding field of any
kind — outbound is a digest response to a 401/407 on an INVITE, not a registration. Issue `livekit/sip#338`
shows a REGISTER answered `405 Method Not Allowed`; PR `livekit/sip#774` adding REGISTER support was still
open as of 16 Sep 2026. Netgsm outbound appears to require REGISTER.

**Therefore:** Asterisk (`res_pjsip`) on a small Istanbul VPS terminates SIP in both directions —
registration-based outbound to `sip.netgsm.com.tr`, IP-based inbound from Netgsm's gateway — and bridges
media to the assistant over AudioSocket or ARI `externalMedia`. LiveKit, if used at all, sits *behind*
Asterisk as a media pipe.

This does not change the architecture above. `jarvis-phone` remains a peer process supplying its own
`Source`/`Sink`; only what sits on the far side of those changes. That is the seam working as intended.

See [`telephony.md`](telephony.md) for the full procurement path and the eight questions that settle it.
