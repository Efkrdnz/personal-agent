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

```bash
uv venv && . .venv/bin/activate
uv pip install -e '.[cc,voice,live,tts,secrets,dev]'
```

| extra | what stops working without it |
|---|---|
| `cc` | Claude Code cannot be driven at all |
| `voice` | no microphone — `numpy`, `sounddevice`, `soxr` |
| `live` | no voice — Gemini Live is the conversational half |
| `tts` | no reader voice, so Gemini reads load-bearing text in its own words |
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
`project_status`, `spend` and `reachability` answer for themselves. `python -m jarvis.telegram` binds to your
chat and answers `/status`. `python -m jarvis.schedule` arms the 10am gate and really does put "Good moment
for your briefing?" on your phone.

**What does not work yet, so that nobody demonstrates it by accident.** Every one of these is a missing
caller between two processes, not a missing feature:

- **Tapping "Now" on the briefing gate composes no briefing.**
  The gate publishes an event nothing listens for.
- **There is no wake word**: the desk listens from the moment it starts.

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
waiting in is always visible rather than hidden inside a blocking loop.

## Driving Claude Code directly

This part works, end to end, and is the reason to install it now:

```bash
python -m jarvis run "build me a todo CLI that stores tasks in one JSON file" --into ~/code/todo
```

That creates the job, starts the driver, and prints every question Claude asks as it asks it. From
another terminal — or another machine sharing the database:

```bash
python -m jarvis pending          # the questions waiting on you, numbered
python -m jarvis answer 1 2       # answer the first one with option 2
python -m jarvis answer 1 --text "put them in Postgres"   # none of these, in your words
```

The option numbers run 1..N across the whole batch rather than restarting per question, so a
three-question payload is answered `python -m jarvis answer 1 1 4 7`. They are the same numbers every
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
