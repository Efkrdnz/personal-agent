# Setting it up

Everything here is a thing only you can do: a browser, a password prompt, a physical device. Anything a
command can do for you is already in one command:

```bash
python -m jarvis doctor
```

Run it first, and run it again after every step below. It prints one line per thing it checked, and for
anything missing it prints the command that fixes it. It **never prints a credential**, so its output is
safe to paste anywhere.

---

## 1. The machine

Linux or macOS:

```bash
uv venv && . .venv/bin/activate
uv pip install -e ".[cc,voice,live,tts,geo,wake,secrets,dev]"
sudo apt install espeak-ng      # Linux only: the built-in reader voice (macOS has `say`)
```

Windows (Command Prompt or PowerShell, from the repository folder):

```bat
uv venv
.venv\Scripts\activate
uv pip install -e ".[cc,voice,live,tts,geo,wake,secrets,dev]"
```

In PowerShell the activate line is `.venv\Scripts\Activate.ps1`. Windows reads aloud with its own SAPI
voice, so there is nothing else to install. **After every `git pull`, run the install line again**: it is
how new dependencies arrive (Windows needs `tzdata`, because its Python ships no time-zone database and
the scheduler cannot compute "10:00 in Istanbul" without one).

Two things that cost an afternoon if you do not know them:

- **Use double quotes around `".[...]"`.** Command Prompt does not treat single quotes as quotes, so
  `'.[cc,voice]'` reaches pip with the quotes still on and pip answers *"'.[cc,voice]' is not a valid
  editable requirement"*. Curly quotes (`‘ ’`, from copying rendered text) do the same in every shell.
  Double quotes work in Command Prompt, PowerShell, bash and zsh alike.
- **A venv made by `uv venv` has no pip in it.** Install with `uv pip install`, as above. If you would
  rather use pip, make the venv with `python -m venv .venv` and install with
  `python -m pip install -e ".[...]"`. A bare `pip` inside a uv venv silently falls through to some
  other Python's pip and installs Jarvis there instead.

| extra | what stops working without it |
|---|---|
| `cc` | Claude Code cannot be driven at all |
| `voice` | no microphone — `numpy`, `sounddevice`, `soxr` |
| `live` | no voice — Gemini Live is the conversational half |
| `tts` | one fewer reader voice (Microsoft's, over the network). Not required: the OS voice reads exact text with nothing installed |
| `geo` | no GeoLite2 lookup, so "where am I" needs `[location] city` in config.toml |
| `wake` | no wake word; the desk refuses to start unless `voice.wake_word = ""` (always listening) |
| `secrets` | credentials fall back to environment variables |
| `aec` | open speakers cannot barge in; a headset still works |

Two things are NOT pip packages and `doctor` checks both:

- **The Claude Code CLI** must be on `PATH`. The driver runs `claude` as a subprocess.
- **PortAudio** must be installed for `sounddevice` to open a device
  (`apt install libportaudio2`, `brew install portaudio`). Without it the audio graph still runs on
  synthetic audio — which is how CI tests it — but the desk leg cannot open your microphone.

---

## 2. The one credential that is required

**Gemini** — `aistudio.google.com` → *Get API key*. Then:

```bash
python -m jarvis secrets set gemini_api_key      # prompts; never pass it as an argument
```

It goes in the OS keyring, never in a file in this tree and never in `config.toml`. The prompt is hidden
and the value is never echoed, logged, or put in an exception message.

**On a headless box** the keyring is a trap worth knowing about in advance: on Linux the login keyring is
unlocked by PAM at *graphical* login, so a service started at boot has neither a session bus nor an
unlocked keyring. Jarvis needs a microphone and speakers anyway, so run it as a user service tied to your
desktop session. If you genuinely cannot, export the variables instead — `doctor` reports which source each
credential came from, so "it is coming from the environment" is visible rather than assumed:

```bash
export JARVIS_GEMINI_API_KEY=...
```

## 3. The three optional ones

Each one turns a feature on. Leaving it unset turns that feature off and `doctor` says so rather than
failing.

| secret | unlocks | where it comes from |
|---|---|---|
| `github_token` | creating the repo before a project starts; new issues in the briefing | github.com → Settings → Developer settings → **fine-grained** token. Contents: read/write. Administration: write only if you want repo deletion to be possible at all. |
| `telegram_bot_token` | the remote channel: plan-mode buttons, screenshots, `/status` | message **@BotFather** → `/newbot` → copy the token |
| `google_oauth_client` | briefing sections 2 and 4 — Gmail and YouTube comments | console.cloud.google.com → enable Gmail API and YouTube Data API v3 → Credentials → OAuth client ID (Desktop) |

A note on the GitHub token, because it is the one that will surprise you: a **fine-grained** token is
scoped per repository, so the capability matrix Jarvis measures from it (`tools/probe_github_token.py`) is
the capability for *the repositories you granted*, not for your account. That is why "undo that repo" can
honestly answer "renamed and archived it" on one repo and "there is nothing I can do about it now" on
another — and why it never promises the stronger one without having measured it.

**Claude Code** needs no token here. It uses your Max subscription's own OAuth, which is
[ADR 0004](adr/0004-claude-auth-oauth.md); the consequence is that the ledger's dollar figure for Claude is
an API-equivalent *estimate* and the meaningful number is rate-limit proximity. The ledger says that out
loud rather than reporting a reassuring zero.

---

## 4. Settings that are not credentials

```bash
python -m jarvis config init      # writes ~/.config/jarvis/config.toml with the defaults in it
python -m jarvis config show      # what is actually in force right now
```

The one field worth setting by hand is `desk.github_owner` — a repository has to be created under
somebody, and Jarvis will not guess whose account that is.

`config.toml` will be in your dotfiles and pasted into bug reports, so it **refuses** to load if it
contains a key that looks like a credential. That refusal is deliberate and is not configurable: a warning
would be ignored, and one tolerated secret in a config file is followed by a second.

---

## 5. Try it

```bash
python -m jarvis doctor      # should end with "Everything required is present"
python -m jarvis tools       # what a spoken sentence can make happen, per channel
python -m jarvis status      # what is running, what it cost, where it thinks you are
python -m jarvis desk        # listen and talk
```

`doctor` ends with a **what you can run** list — a verdict per process, with its blocker named. Read that
rather than this paragraph; it is generated from your machine and this is generated from memory.

**What works today.** `desk` opens your microphone, holds a spoken conversation, and files a build request
from your own words: `code_build` writes one `repo_setup` job row carrying the transcript, the model and the
effort level, all three parsed from what you actually said rather than from the model's summary of it.
`project_status`, `spend` and `reachability` answer for themselves. When a question is routed to the
desk it is **read aloud by the deterministic reader** — never by Gemini, because the options are an answer
key — and you answer it by the number you heard: "the second one". The label is looked up from the frozen
array by code, so a mishearing cannot change what you agreed to. `python -m jarvis.telegram` binds to your
chat and answers `/status`. `python -m jarvis.schedule` arms the 10am gate and really does put "Good moment
for your briefing?" on your phone.

**What does not work yet, so that nobody demonstrates it by accident.** Every one of these is a missing
caller between two processes, not a missing feature:

- **Tapping "Now" on the briefing gate composes no briefing.**
  The gate publishes an event nothing listens for.
- **The desk does not run the builder itself** — after speaking a build request, run
  `python -m jarvis build` to carry it forward.

## The window

```bash
python -m jarvis window          # opens the HUD in an app window (Edge or Chrome, no address bar)
python -m jarvis window --no-open   # print the address instead, to open it yourself
```

The window is its own process. It reads the same database the desk and scheduler write, so start it
before or after them, in any order. What it shows:

- **The orb**, which follows the desk: asleep, awake, listening, speaking, or "not running".
- **The conversation**: what you said aloud and what Jarvis said back, live, plus anything you type.
  Typing uses the same Gemini chat as `python -m jarvis chat`, with every tool. A speaker button on each
  reply reads it aloud, and the "speak replies" switch does it every time.
- **Tools** (weather, time anywhere, where am I, web search, remind, remember, say aloud, plus every tool
  as a form), **Reminders**, **Notes**, **Questions** (answer Claude Code's questions with buttons),
  **Builds**, and **Hearing**. Ctrl+K opens a command palette.
- **Running/stopped lights** for the desk, the scheduler and Telegram, and a **STOP** button you hold
  for a second.

**Speech goes through the desk when it is running**, because only one program may own the speakers
(otherwise the echo canceller hears sound it was never told about). With the desk off, the window
speaks itself, with the same reader voice.

**Only this machine can use it.** The server listens on 127.0.0.1 only, and every request must carry a
secret token that is created at launch and handed to the window in the part of the address the browser
never sends anywhere. A web page you visit cannot press STOP or answer a question for you.

## The wake word

The desk starts **asleep**: the microphone is read locally, and nothing is sent to Gemini until it hears
**"hey Jarvis"**. Then it chimes, and stays awake for `voice.wake_window_s` (20 s) after the last thing
anyone said, including a question it reads aloud on its own, so you can answer that without the name.
"Hey Jarvis, what's the weather" works as one sentence.

```bash
uv pip install -e ".[wake]"                 # onnxruntime
python -m jarvis wake download              # three small ONNX files, SHA-256 pinned
python -m jarvis wake test                  # the score your OS voice gets saying "hey jarvis"
python -m jarvis wake test --wav me.wav     # or a recording of you
```

`voice.wake_threshold` (0.5) trades distance for false wakes; measure with `wake test` rather than
guessing. `voice.wake_word = ""` turns it off and the desk listens all the time. A configured wake
word with no model is a refusal to start, never a quiet fallback to always-listening. Jarvis reading
"hey Jarvis" aloud from an email does not wake him: the self-speech veto discards it.

**Licence.** openWakeWord's pretrained models are **CC BY-NC-SA 4.0: non-commercial**. Fine for
your own desk; not for a hosted or paid deployment. They are downloaded to your machine and never
committed, so this repository stays MIT. [ADR 0012](adr/0012-wake-model-is-non-commercial.md) has the
reasoning and the ways out.

## The everyday assistant

Jarvis is general-purpose, not only a build console. Everything below works at the desk by voice, and by
text with `python -m jarvis chat` (the same tools, over the ordinary Gemini API):

```bash
python -m jarvis chat                          # a conversation; /reset, /quit
python -m jarvis chat "weather in Ankara tomorrow"
python -m jarvis chat --speak "what's on my reminders"   # and say the answer out loud
```

- **Weather, place and time.** "What's the weather", "will it rain tomorrow", "what time is it in Tokyo",
  "where am I". The place comes from `[location]` in `config.toml` (a city, or coordinates — exact), or
  else from your public IP via MaxMind GeoLite2, and an IP guess is always *said* to be approximate.
  For the IP lookup: create a free account at maxmind.com, put your account id in
  `location.maxmind_account_id`, then

  ```bash
  python -m jarvis secrets set maxmind_license_key
  python -m jarvis geo update        # downloads GeoLite2-City, checks its SHA-256, validates it
  python -m jarvis geo where         # where Jarvis thinks you are, and how sure it is
  python -m jarvis weather --when week
  ```

  Weather and geocoding are Open-Meteo: free, no key.
- **Memory.** "Remember my locker is 214", "what's my locker number", "forget the locker thing". Notes are
  your own sentences, and every conversation is given them.
- **Reminders.** "Remind me to call mum in 20 minutes / at 6pm / tomorrow at 9". The time it resolved is
  always said back. **`python -m jarvis.schedule` must be running** — it raises the reminder when it is
  due and sends it wherever you are (desk, Telegram). `python -m jarvis remind` lists them.
- **Web search.** Anything current — scores, news, opening hours — via a Google-grounded Gemini call that
  names its sources. `voice.web_search = false` turns it off.

## Built-in voice

The reader voice (the one that reads options and requirements word for word) no longer needs anything
installed beyond the OS: **espeak-ng** on Linux, **say** on macOS, **SAPI** on Windows. Kokoro and
Microsoft's Edge voices are used first when installed; Gemini TTS is last and, being generative, is
never trusted with exact text. `voice.reader_order` sets the ladder.

```bash
python -m jarvis say "Option two: SQLite"                # through the speakers
python -m jarvis say --out test.wav "Merhaba" --lang tr  # or to a file
```

## When it mishears you

Recognisers hear "coat" or "court" when an accented speaker says "quote". Jarvis corrects that from
context — "stock coat" becomes "stock quote", "winter coat" stays a coat — and learns from you:

- Say **"no, I said quote"** and it remembers which word it got wrong (it finds it in what you just said).
- Say **"no, I really said coat"** when a correction was wrong, and it backs off.
- Put your own words in `voice.vocabulary` (names, jargon, project names): they are given to the
  recogniser as vocabulary hints and to the model as instructions.

```bash
python -m jarvis hearing test "get me a stock coat for apple"   # what it would change, and why
python -m jarvis hearing teach jason json                       # teach a pair by hand
python -m jarvis hearing list                                   # everything it knows
```

Two switches, both on by default: `voice.asr_vocabulary` (the recogniser hint, which is read from the SDK
and **unverified** on the Live server — turn it off if the desk cannot connect) and
`voice.hearing_arbiter` (doubtful words get a one-word vote from the text model).

## Speaking a project into existence

```bash
python -m jarvis build        # carries every spoken build request forward one step
```

Say "let's build an app that watches my YouTube comments, and no Docker" to the desk (or file the same
request any other way) and `build` tidies your words into a numbered list, reads it back, and waits. It
never adds a requirement you did not state: every line carries a span copied out of your own words, and
anything the tidier invented is thrown away and **said out loud** ("I threw away 'add authentication'
because I couldn't trace it back to your own words").

Answer it from anywhere — `python -m jarvis pending`, or the buttons on Telegram:

- **Build it** → the repository is created first, cloned, and Claude Code starts inside it.
- **drop three** / **two should say Postgres** / **add: no Docker** → the list is edited and read back
  again. Positions that do not exist are refused rather than guessed at.

With no `github_token` it builds in a local directory instead and tells you there is no repository.
Claude Code receives both the confirmed list AND your unedited words as an appendix, so drift in the
tidier cannot lose a constraint.

Run `python -m jarvis build` again after each answer; it advances one step per call, so the state it is
waiting in is always visible rather than hidden inside a blocking loop. The last call starts Claude Code
in the directory for you.

`pending` prints each question's **id**, and `answer` takes it. A bare position works too, but the list
shifts whenever anything is answered on another channel — with two builds running, answering by position
could land an `Allow` on a tool permission you never read.

## Driving Claude Code directly

This part works, end to end, and is the reason to install it now:

```bash
python -m jarvis run "build me a todo CLI that stores tasks in one JSON file" --into ~/code/todo
```

That creates the job, starts the driver, and prints every question Claude asks as it asks it. From
another terminal — or another machine sharing the database:

```bash
python -m jarvis pending          # the questions waiting on you, numbered
python -m jarvis answer req_ab12cd34 2   # answer that question with option 2
python -m jarvis answer req_ab12cd34 --text "put them in Postgres"   # in your words
```

The option numbers run 1..N across the whole batch rather than restarting per question, so a
three-question payload is answered `python -m jarvis answer <id> 1 4 7`. They are the same numbers every
other channel shows, and nothing anywhere renumbers them.

If Claude asks while you are away, the hook parks the job instead of blocking. Answer it whenever you
like, then `python -m jarvis run --resume` picks up every job whose answer has arrived.

**For those questions to reach Telegram, the scheduler must be running**: `python -m jarvis.schedule`.
It is the process that decides when and where you get asked, and without it the questions are raised
and only the `pending` command can see them.

There is one thing a headset buys you beyond comfort: `select_duplex_device` needs your default input and
your default output to be **the same device**. A laptop's built-in mic and built-in speakers are two devices
and two clocks, and it will refuse rather than let the echo canceller silently drift. `doctor` now runs that
exact check, so it refuses in the same words `desk` would.
