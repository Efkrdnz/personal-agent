# Jarvis on Windows: the app

This page is for using Jarvis, not for working on it. There is no terminal anywhere in it: you download a
folder, double-click `Jarvis.exe`, and answer a few questions in Jarvis's own window.

If you are here to change the code, skip to [For developers](#for-developers) at the bottom.

---

## 1. Download it

Jarvis is built automatically every time its code changes, on a real Windows machine, and every build checks
itself before it is offered to you.

1. Open **[the Windows app builds](https://github.com/Efkrdnz/personal-agent/actions/workflows/windows-app.yml)**
   (you need to be signed in to GitHub).
2. Click the newest run with a **green tick**.
3. Scroll to **Artifacts** at the bottom and click **Jarvis-Windows-x64**. You get `Jarvis-Windows-x64.zip`.

When there is a numbered release, the same zip is also on the repository's **Releases** page, and that is
the easier place to get it.

## 2. Unzip it somewhere it can stay

Right-click the zip → **Extract All…** → pick a folder you will keep, for example
`C:\Users\<you>\Apps\Jarvis`. Not your Downloads folder (you will tidy that one day and Jarvis will vanish
with it), and not `Program Files` (updating there needs an administrator every time).

Inside the folder there is `Jarvis.exe` and a folder called `_internal`. Leave `_internal` where it is:
that is Jarvis's voice, ears and everything else. Only `Jarvis.exe` is meant to be clicked.

If you want Jarvis on the desktop or the Start menu, right-click `Jarvis.exe` → **Send to** →
**Desktop (create shortcut)**. Do not move the exe itself out of its folder.

## 3. Double-click Jarvis.exe

The first time, Windows will probably stop you with a blue box: **"Windows protected your PC"**. That is
because Jarvis is not signed with a paid certificate, not because anything is wrong with it. Click
**More info**, then **Run anyway**. Windows remembers, and does not ask again for this copy.

The first start takes a little longer than later ones while your antivirus looks through the folder.

Jarvis opens in its own window. (It is Microsoft Edge without an address bar, which is why it looks so much
like a web page; it is still Jarvis, and it only talks to your own PC.)

## 4. The first-run screens

Jarvis asks four things, one screen at a time:

1. **The key to its voice.** Jarvis listens and speaks through Google's Gemini, which needs a free API key.
   Click **Get one free at Google AI Studio**, sign in with a Google account, click *Create API key*, copy
   it, and paste it into the box. Jarvis puts it in Windows Credential Manager. It is never written to a
   file and never shown again.
2. **Which microphone.** Pick yours from the list, or leave it on the system default. A headset is best:
   Jarvis cannot hear itself in one, so you can interrupt it mid-sentence.
3. **What to call you.** *Sir*, *ma'am*, *boss*, and your name if you want it to know it.
4. **Where you are, roughly.** A city, for the weather and the time. You can leave it empty.

While you answer, Jarvis downloads its wake word ("hey Jarvis") on its own. The screen shows a line about
its licence: those wake-word files are free for **personal, non-commercial** use, which is why they are
downloaded onto your PC rather than shipped inside the zip.

Then **Very good. I'll start listening now.** Say *"Hey Jarvis"* and talk.

You can change every one of these later in the **Settings** tab, which also has the voice picker (with a
▶ to hear each voice), the wake-word sensitivity, **Start with Windows**, the optional Telegram and GitHub
keys, and **Sign in to Claude Code** (that one opens Claude Code's own sign-in window; follow it, then come
back).

### Jarvis on your phone (optional)

1. In Telegram, message **@BotFather**, send `/newbot`, and follow it. It gives you a token.
2. In Jarvis: **Settings → Keys → Telegram bot token → Add**, and paste it.
3. Click **Pair my phone**. Jarvis shows an eight-character code for ten minutes.
4. Send that code to your new bot from your phone. From then on the bot answers only you.

A new phone is **Unpair**, then **Pair my phone** again.

### If Jarvis cannot hear you

Windows 11 has a switch that silently gives desktop apps no microphone at all:
**Settings → Privacy & security → Microphone → Let desktop apps access your microphone** must be **On**.

### Not in the app yet

Building software with Claude Code by voice ("build me a to-do app") is not wired into the app yet, and
Jarvis says so if you ask rather than pretending to start. It still works from a terminal
(`python -m jarvis build`), and **Sign in to Claude Code** in Settings is ready for when it arrives.

## 5. The icon by the clock

Jarvis keeps running when you close its window — it is still listening for "hey Jarvis". It lives as a
small glowing ring by the clock (if you do not see it, click the **^** arrow next to the clock). Click it
for:

- **Open Jarvis** — the window again.
- **Restart voice** — if the voice has gone quiet or strange.
- **Quit** — stops everything.

Double-clicking `Jarvis.exe` while Jarvis is already running does not start a second copy; it just brings
the window back.

You can also quit from the window: **Settings → Quit Jarvis**, and hold the button for a second.

## 6. When something is wrong

Jarvis says so in its window, in amber, at the top left: what is wrong, in one sentence, and a button that
fixes it (paste a key, pick another microphone, download the wake word again). The **Settings** tab also
lists every part of Jarvis that should be running, with a restart button for each.

If Jarvis does not open at all, a box says why and where the details are. Those details are in the logs:
paste `%LOCALAPPDATA%\Jarvis\logs` into the address bar of any Explorer window. `app.log` is the app itself;
`desk.log` is the voice; `schedule.log` and `telegram.log` are the rest. Jarvis is written never to put
your keys in them, but glance through one before you send it to anybody.

## 7. Where your things live

None of this is in the Jarvis folder, which is why replacing the folder (updating) loses nothing.

| What | Where |
|---|---|
| Your keys (Gemini, Telegram, GitHub) | Windows Credential Manager → *Windows Credentials*, entries named `jarvis` or ending in `@jarvis` |
| The settings you chose in the window | `%USERPROFILE%\.config\jarvis\app-settings.toml` |
| Hand-written settings, if you ever make any | `%USERPROFILE%\.config\jarvis\config.toml` |
| History, reminders, notes, the activity log | `%USERPROFILE%\.local\state\jarvis\jarvis.db` |
| The wake-word files | `%USERPROFILE%\.local\share\jarvis\wake\` |
| Logs, and the note that says which copy is running | `%LOCALAPPDATA%\Jarvis\` |

## 8. Updating

1. Quit Jarvis (the icon by the clock → **Quit**).
2. Download the new `Jarvis-Windows-x64.zip` the same way as the first time.
3. Extract it **over the same folder**, and say yes to replacing the files.
4. Double-click `Jarvis.exe`.

Your keys, settings and history are untouched. Windows may ask **More info → Run anyway** once more, because
to it a new build is a new program. If *Start with Windows* is on, keep using the same folder: that switch
points at the exe where it was when you turned it on.

## 9. Removing it

Turn off **Settings → Start with Windows** first, then quit and delete the Jarvis folder. To remove
everything it kept, also delete the folders in the table above and the `jarvis` entries in Credential
Manager.

---

## For developers

**The immediate path, no build.** The `app` extra plus the `gui-scripts` entry in `pyproject.toml` give a
source checkout a double-clickable launcher with no console window:

```bat
uv venv
uv pip install -e ".[cc,voice,live,tts,geo,wake,secrets,app]"
.venv\Scripts\Jarvis.exe
```

It is the same app as the built exe — the same function, `jarvis.app.entry:main`, starts both. Right-click
it → *Send to → Desktop* for a shortcut. `python -m jarvis` with no subcommand runs the same app with a
console, which is the way to watch it start.

**Building `Jarvis.exe` yourself:**

```powershell
uv pip install -e ".[cc,voice,live,tts,geo,wake,secrets,app,build]"
pyinstaller packaging\jarvis.spec --noconfirm
Start-Process dist\Jarvis\Jarvis.exe -ArgumentList '--selftest','--report','selftest.json' -Wait
Get-Content selftest.json
```

`Jarvis.exe` is a windowed program, so a shell does not wait for it and nothing it prints reaches the
console: `Start-Process -Wait` and the JSON report are how you see the result. `--selftest` checks the
build from inside itself — PortAudio and onnxruntime load, the Claude Code CLI is in the bundle, the
migrations and the window's files are there, a database migrates, the window answers over real HTTP, the
Windows voice speaks — and exits non-zero if anything critical is missing.

`packaging/jarvis.spec` explains what it collects and why. `packaging/make_icon.py` draws
`packaging/jarvis.ico`; run it after changing the drawing and commit both. The openWakeWord models must
never be inside the build ([ADR 0012](adr/0012-wake-model-is-non-commercial.md)): the spec refuses to
finish if one turns up, and CI checks the output folder again.

**CI.** `.github/workflows/windows-app.yml` runs on `windows-latest`: the whole test suite, the build, the
licence check, the selftest, then uploads the folder as the `Jarvis-Windows-x64` artifact. A `v*` tag also
publishes the zip as a release, but only from a run that was green end to end. Where a tag cannot be pushed,
run the workflow by hand (Actions → Windows app → Run workflow) with `release` set to e.g. `v0.2.0`: the
same checks run, and the release job creates the tag on the commit that passed them. Every step tees its output
into `ci-logs/`, and on failure `packaging/ci_annotate.py` turns the end of each log into an `::error`
annotation, because annotations are what the check-run API returns when the raw logs are out of reach.
