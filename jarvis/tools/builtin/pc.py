"""This computer, by voice: open, close, volume, media, lock, sleep and shut down.

ONLY AT THIS COMPUTER. Every tool here is ``channels=("desk", "cli")``: a
person at the desk, or at its keyboard. Never Telegram — a shutdown approved
from a phone a minute and a half later is not what anybody asked for — never
the phone line, which anyone with the number can reach, and never the
scheduler, which has nobody to hear a read-back.

THE BACKEND IS HANDED IN as ``ctx.extra[DESKTOP]`` (a :class:`jarvis.pc.Desktop`);
with none, every tool refuses with a sentence. What a sentence may make happen
is decided here; how Windows does it is :mod:`jarvis.pc`'s business.

TWO NEED A YES. Closing an app and sleeping, restarting or shutting down go
through :mod:`jarvis.tools.confirm`: the first call reads back exactly which
window, or exactly what will happen, and does nothing; the second runs only on
the user's own yes. Those three also leave an effects row, because they cost
something an opposite action cannot give back (an unsaved document, a session).
Opening, volume, media keys and locking do not: the opposite action undoes them,
and ``tool.used`` already logs that they were asked for.

THE GRACE PERIOD IS OURS. Windows will count a shutdown down only if it may
force apps closed when the count ends (``shutdown /t N`` implies ``/f``;
``InitiateShutdownW`` with a grace period requires ``SHUTDOWN_FORCE_SELF``). So
Jarvis counts the minute itself, then asks Windows to shut down NOW with no
force: any app with unsaved work stops it and asks the user. The countdown is a
row in ``effects`` with an undo plan and a deadline; when the minute is up the
timer reads that row from the database and goes ahead only if nobody — in this
process or any other — undid it. "Cancel the shutdown" is :func:`effects.undo`.
If Jarvis quits during the minute, nothing shuts down: the safe way to fail.

``confirmed_by`` IS IN ``provider_ref``. The effects column of that name is a
foreign key to ``requests``, and a spoken yes is deliberately not a request row
(see :mod:`jarvis.tools.confirm`), so the proposal id that authorised the act is
stored beside the act instead, as ``"<channel>:<proposal id>"`` — the same id
the ``confirm.granted`` event carries.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from jarvis import effects as fx
from jarvis.bus import publish
from jarvis.db import connect
from jarvis.pc import Desktop, PcRefused, choose_desktop, safety
from jarvis.pc import catalog as cat
from jarvis.pc.base import App, Volume, Window
from jarvis.tools import confirm as confirm_mod
from jarvis.tools.ctx import ToolCtx
from jarvis.tools.registry import Tool, ToolError
from jarvis.tools.reply import Reply

__all__ = [
    "ABORT_OP",
    "CLOSE_WAIT_S",
    "DESKTOP",
    "MUTE_DELAY_S",
    "POWER_GRACE_S",
    "SLEEP_DELAY_S",
    "TOOLS",
    "cancel_power",
    "close_app",
    "lock_screen",
    "media",
    "open_app",
    "open_folder",
    "open_website",
    "power",
    "volume",
]

#: Key in :attr:`ToolCtx.extra`, set by the composition root.
DESKTOP = "desktop"

#: The countdown before a shutdown or restart: long enough to hear the reply
#: and say "cancel", short enough to still be what was asked for.
POWER_GRACE_S = 60
#: Long enough for the reader to say the reply before the machine goes deaf.
SLEEP_DELAY_S = 5.0
#: The same, for muting: the warning that it mutes Jarvis too must be heard.
MUTE_DELAY_S = 4.0
#: How long to watch a window after asking it to close.
CLOSE_WAIT_S = 4.0
#: The undo plan's op for a countdown. Registered below, so every process that
#: builds the registry can call a shutdown off.
ABORT_OP = "pc.power_abort"
#: A countdown row older than this is history, not something to call off.
_STALE_COUNTDOWN_S = 600
#: How many installed names a miss hands the model to translate against.
_MAX_NAMES = 300

_PLACES = {
    "desktop": "your desktop",
    "documents": "Documents",
    "downloads": "Downloads",
    "pictures": "Pictures",
    "music": "Music",
    "videos": "Videos",
    "home": "your home folder",
}
_MEDIA_SAID = {
    "play_pause": "Pausing or resuming whatever is playing.",
    "next": "Skipping to the next track.",
    "previous": "Going back a track.",
    "stop": "Stopping playback.",
}
_POWER_WORDS = {
    "shutdown": "shutdown",
    "shut_down": "shutdown",
    "shutoff": "shutdown",
    "poweroff": "shutdown",
    "power_off": "shutdown",
    "turn_off": "shutdown",
    "restart": "restart",
    "reboot": "restart",
    "sleep": "sleep",
    "suspend": "sleep",
}
_READBACK = {
    "shutdown": (
        "I'll shut the computer down after a one-minute countdown. Apps will be asked to "
        "close, and any with unsaved work will stop it and wait for you. You can say cancel "
        "the shutdown until then."
    ),
    "restart": (
        "I'll restart the computer after a one-minute countdown. Apps will be asked to close, "
        "and any with unsaved work will stop it and wait for you. You can say cancel the "
        "restart until then."
    ),
    "sleep": "I'll put the computer to sleep. I won't be able to hear you until it wakes up.",
}


# ───────────────────────────── plumbing ─────────────────────────────


def _desktop(ctx: ToolCtx) -> Desktop:
    desk = ctx.extra.get(DESKTOP)
    if desk is None or not isinstance(desk, Desktop):
        raise ToolError("I can't control this computer from here.")
    status = desk.status()
    if not status.available:
        raise ToolError(status.detail)
    return desk


@contextlib.contextmanager
def _pc(what: str) -> Iterator[None]:
    """A backend's refusal is already a sentence; an OS error becomes one."""
    try:
        yield
    except PcRefused as exc:
        raise ToolError(str(exc)) from exc
    except OSError as exc:
        why = exc.strerror or str(exc) or type(exc).__name__
        raise ToolError(f"The computer wouldn't {what}: {why}.") from exc


def _said(text: object) -> str:
    return " ".join(str(text or "").split())


def _yes(confirm: object) -> bool:
    """Only a real true. A model that sends the string "false" has not confirmed."""
    return confirm is True or (isinstance(confirm, str) and confirm.strip().casefold() == "true")


def _listed(items: Iterable[str]) -> str:
    names = list(items)
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + f" or {names[-1]}"


def _by(ctx: ToolCtx, proposal: confirm_mod.Proposal) -> str:
    return f"{ctx.channel}:{proposal.id}"


def _db_file(con: sqlite3.Connection) -> str | None:
    for row in con.execute("PRAGMA database_list"):
        if row[1] == "main":
            return str(row[2]) or None
    return None


# ───────────────────────────── open ─────────────────────────────


def open_app(ctx: ToolCtx, name: str) -> str:
    desk = _desktop(ctx)
    said = _said(name)
    if not said:
        raise ToolError("Which app should I open?")
    with _pc("open that"):
        found = cat.match(said, desk.apps())
        if isinstance(found, cat.NotFound):
            # Installed a minute ago, perhaps. The catalog limits how often.
            found = cat.match(said, desk.rescan())
        if isinstance(found, cat.Match):
            desk.launch(found.app)
            return f"Opening {found.app.spoken}."
        if isinstance(found, cat.Ambiguous):
            return f"I have {_listed(a.spoken for a in found.apps)}. Which one should I open?"
        return _no_such_app(said, found, desk.apps())


def _no_such_app(said: str, found: cat.NotFound, apps: Iterable[App]) -> Reply:
    """Not read aloud: the model gets the installed names and can translate, then retry.

    "Calculator" on a Turkish Windows is "Hesap Makinesi". The built-in apps have
    both names in the catalog, but any app might be named in the other
    language, and the model can tell that from a list where the code cannot.
    """
    names = sorted({a.spoken for a in apps if a.source != "settings"}, key=str.casefold)
    near = [a.spoken for a in found.near]
    detail = (
        f"No installed app matched '{said}'. "
        + (f"Closest names: {', '.join(near)}. " if near else "")
        + "If what the user said is another language's name for one of the installed apps "
        "below, call open_app again with that exact installed name. Otherwise tell the user "
        "it doesn't seem to be installed. Installed: " + "; ".join(names[:_MAX_NAMES])
    )
    return Reply(f"I couldn't find an app called {said} on this computer.", detail=detail)


def open_website(ctx: ToolCtx, url: str = "", query: str = "", site: str = "web") -> str:
    desk = _desktop(ctx)
    with _pc("open the browser"):
        if _said(url):
            safe = safety.safe_url(url)
            desk.open_url(safe)
            return f"Opening {safety.site_name(safe)}."
        if _said(query):
            target, where = safety.search_url(query, site)
            desk.open_url(target)
            return f"Searching {where} for {_said(query)}."
    raise ToolError("Which website should I open, or what should I search for?")


def open_folder(ctx: ToolCtx, place: str, subfolder: str = "", file: str = "") -> str:
    desk = _desktop(ctx)
    key = _said(place).casefold()
    if key not in safety.PLACES:
        raise ToolError(
            "I can open your desktop, documents, downloads, pictures, music, videos or home folder."
        )
    with _pc("open that folder"):
        root = desk.known_folder(key)
        target = safety.resolve_inside(root, subfolder, file)
        desk.open_path(target)
    where = _PLACES[key]
    if _said(file):
        return f"Opening {target.name} from {where}."
    if _said(subfolder):
        return f"Opening {target.name} in {where}."
    return f"Opening {where}."


# ───────────────────────────── close ─────────────────────────────


def _app_of(w: Window) -> str:
    if " - " in w.title:
        return w.title.rsplit(" - ", 1)[1].strip()
    return w.title.strip()


def _label(w: Window) -> str:
    """How the read-back names the window: which app, and which of its windows."""
    title = w.title.strip()
    if " - " in title:
        doc, app = (p.strip() for p in title.rsplit(" - ", 1))
        return f"the {app} window showing {_short(doc)}"
    return _short(title)


def _short(text: str, limit: int = 60) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _the_window(said: str, windows: tuple[Window, ...]) -> Window | str:
    """The one window meant, or a question to ask. Protected windows are refused by name."""
    by_hwnd = {str(w.hwnd): w for w in windows}
    found = cat.match_window(said, windows)
    if isinstance(found, cat.Match):
        w = by_hwnd[found.app.target]
        reason = safety.protected_reason(w)
        if reason:
            raise ToolError(reason)
        return w
    allowed = tuple(w for w in windows if safety.protected_reason(w) is None)
    found = cat.match_window(said, allowed)
    if isinstance(found, cat.Match):
        return by_hwnd[found.app.target]
    if isinstance(found, cat.Ambiguous):
        titles = [f'"{_short(a.name)}"' for a in found.apps]
        return f"{len(titles)} windows match: {_listed(titles)}. Which one should I close?"
    open_now = sorted({_app_of(w) for w in allowed if _app_of(w)}, key=str.casefold)
    hint = f" Open now: {_listed(open_now[:6]).replace(' or ', ' and ')}." if open_now else ""
    raise ToolError(f"I can't see a window called {said}.{hint}")


def close_app(ctx: ToolCtx, name: str, confirm: bool = False) -> str:
    desk = _desktop(ctx)
    said = _said(name)
    if not said:
        raise ToolError("Which app should I close?")
    with _pc("list the open windows"):
        window = _the_window(said, desk.windows())
    if isinstance(window, str):
        return window
    # The window itself, not its title: a browser's title changes as it plays.
    key = f"window:{window.hwnd}:{window.pid}"
    app = _app_of(window) or said
    if not _yes(confirm):
        return confirm_mod.ask(
            ctx,
            tool="close_app",
            key=key,
            readback=f"I'll close {_label(window)}. If it has unsaved work, it will ask you first.",
            effect="pc.app_close",
        )
    proposal = confirm_mod.granted(ctx, tool="close_app", key=key)
    with _pc("pass the close request on"):
        desk.close(window)
    fx.record_effect(
        ctx.con,
        kind="pc.app_close",
        summary=f"I asked {_label(window)} to close",
        reversibility="compensatable",
        provider_ref={
            "hwnd": window.hwnd,
            "pid": window.pid,
            "exe": window.exe,
            "title": window.title,
            "confirmed_by": _by(ctx, proposal),
        },
        actor=ctx.actor,
    )
    with _pc("check on the window"):
        closed = desk.wait_closed(window, CLOSE_WAIT_S)
    if closed:
        return f"Closed {app}."
    return (
        f"I've asked {app} to close, but it's still open. It may be asking whether to save "
        "your work."
    )


# ───────────────────────────── sound and media ─────────────────────────────


def _percent(value: object, *, low: int, what: str) -> int:
    try:
        n = round(float(str(value).strip().rstrip("%")))
    except (TypeError, ValueError):
        raise ToolError(f"The {what} needs to be a number from {low} to 100.") from None
    if not low <= n <= 100:
        raise ToolError(f"The {what} needs to be a number from {low} to 100.")
    return n


def _level_line(v: Volume | None, fallback: str) -> str:
    if v is None:
        return fallback
    if not v.exact:
        return f"Set the volume to about {v.level} percent."
    if v.muted:
        return f"The volume is at {v.level} percent, but the sound is muted."
    return f"Volume is at {v.level} percent."


def _quietly(fn: Callable[[], object]) -> Callable[[], None]:
    def run() -> None:
        # On a timer, after the reply was spoken: nobody is left to tell, and a
        # traceback on a background thread would only reach a log as noise.
        with contextlib.suppress(Exception):
            fn()

    return run


def _silence(desk: Desktop, act: Callable[[], object], doing: str) -> str:
    """Do something that silences Jarvis too — after it has said so."""
    desk.after(MUTE_DELAY_S, _quietly(act))
    return f"{doing} in a moment. That silences me too: say unmute to bring the sound back."


def volume(ctx: ToolCtx, action: str, amount: int | None = 10, level: int | None = None) -> str:
    desk = _desktop(ctx)
    act = _said(action).casefold()
    with _pc("change the volume"):
        if act in ("up", "down"):
            # A model that sends amount: null means "the usual step", not "no step".
            step = _percent(10 if amount is None else amount, low=1, what="amount")
            if act == "down":
                now = desk.volume()
                if now is not None and now.level - step <= 0:
                    return _silence(desk, lambda: desk.set_volume(0), "Turning the volume to zero")
            moved = desk.step_volume(step if act == "up" else -step)
            return _level_line(moved, f"Turned the volume {act}.")
        if act == "set":
            if level is None:
                raise ToolError("What level? Any number from 0 to 100.")
            target = _percent(level, low=0, what="level")
            if target == 0:
                return _silence(desk, lambda: desk.set_volume(0), "Turning the volume to zero")
            return _level_line(
                desk.set_volume(target), f"Set the volume to about {target} percent."
            )
        if act == "mute":
            return _silence(desk, lambda: desk.set_mute(True), "Muting")
        if act == "unmute":
            return _level_line(desk.set_mute(False), "The sound is back on.")
    raise ToolError("I can turn the volume up or down, set it, mute it or unmute it.")


def media(ctx: ToolCtx, action: str) -> str:
    desk = _desktop(ctx)
    act = _said(action).casefold().replace(" ", "_").replace("-", "_")
    if act in ("play", "pause", "resume", "playpause"):
        act = "play_pause"
    if act in ("skip", "next_track"):
        act = "next"
    if act in ("back", "prev", "previous_track"):
        act = "previous"
    if act not in _MEDIA_SAID:
        raise ToolError("I can play or pause, skip to the next track, go back a track, or stop.")
    with _pc("send the media key"):
        desk.media(act)
    return _MEDIA_SAID[act]


def lock_screen(ctx: ToolCtx) -> str:
    desk = _desktop(ctx)
    with _pc("lock the screen"):
        desk.lock()
    return "Locking the screen."


# ───────────────────────────── power ─────────────────────────────


def _countdowns(con: sqlite3.Connection) -> list[fx.Effect]:
    """Shutdowns and restarts this machine has counting down, newest first."""
    out = []
    for e in fx.recent_effects(con, state="applied", limit=50):
        if e.kind != "pc.power" or (e.undo_plan or {}).get("op") != ABORT_OP or e.undo_claimed:
            continue
        left = fx.undo_window_remaining_s(e)
        if left is not None and left > -_STALE_COUNTDOWN_S:
            out.append(e)
    return out


def _counting(con: sqlite3.Connection) -> fx.Effect | None:
    for e in _countdowns(con):
        left = fx.undo_window_remaining_s(e)
        if left is not None and left > 0:
            return e
    return None


def _noun(action: object) -> str:
    return "restart" if action == "restart" else "shutdown"


def _countdown_end(
    desk: Desktop, db_file: str | None, effect_id: str, action: str, actor: str
) -> Callable[[], None]:
    """What runs when the minute is up: shut down only if nobody called it off."""

    def fire() -> None:
        if db_file is None:
            return  # nothing to check against: not shutting down is the safe failure
        # The tool's connection closed when it returned; this is a reader that
        # starts after the writer is gone, so it opens its own, on the same file.
        try:
            con = connect(db_file)
        except Exception:  # noqa: BLE001 - no way to check means no shutdown
            return
        try:
            row = con.execute(
                "SELECT state, undone_at FROM effects WHERE id=?", (effect_id,)
            ).fetchone()
            # 'expired' is still a go: the scheduler's sweep may close the window
            # a moment before this runs, and the user was told it would happen.
            go = row is not None and row[0] in ("applied", "expired") and row[1] is None
            outcome, error = ("called_off", None)
            if go:
                try:
                    desk.power(action)
                    outcome = "started"
                except Exception as exc:  # noqa: BLE001 - said in the log, not a crash
                    outcome, error = "failed", str(exc)
            publish(
                con,
                "pc.power",
                actor,
                {"action": action, "outcome": outcome, "error": error},
                effect_id=effect_id,
            )
        finally:
            con.close()

    return fire


def _sleep_now(
    desk: Desktop, db_file: str | None, effect_id: str, actor: str
) -> Callable[[], None]:
    def fire() -> None:
        error = None
        try:
            desk.sleep()
        except Exception as exc:  # noqa: BLE001 - recorded below; there is nobody to say it to
            error = str(exc)
        if db_file is None:
            return
        with contextlib.suppress(Exception):
            con = connect(db_file)
            try:
                publish(
                    con,
                    "pc.sleep",
                    actor,
                    {"outcome": "failed" if error else "started", "error": error},
                    effect_id=effect_id,
                )
            finally:
                con.close()

    return fire


def power(ctx: ToolCtx, action: str, confirm: bool = False) -> str:
    desk = _desktop(ctx)
    act = _POWER_WORDS.get(_said(action).casefold().replace(" ", "_").replace("-", "_"))
    if act is None:
        raise ToolError("I can put the computer to sleep, restart it or shut it down.")
    pending = _counting(ctx.con)
    if pending is not None:
        noun = _noun((pending.provider_ref or {}).get("action"))
        raise ToolError(
            f"A {noun} is already counting down. Say cancel the {noun} if you've changed your mind."
        )
    db_file = _db_file(ctx.con)
    if act != "sleep" and db_file is None:
        raise ToolError("I can't keep track of a countdown here, so I won't start one.")
    kind = "pc.sleep" if act == "sleep" else "pc.power"
    if not _yes(confirm):
        return confirm_mod.ask(ctx, tool="power", key=act, readback=_READBACK[act], effect=kind)
    proposal = confirm_mod.granted(ctx, tool="power", key=act)
    by = _by(ctx, proposal)

    if act == "sleep":
        effect = fx.record_effect(
            ctx.con,
            kind="pc.sleep",
            summary="I put the computer to sleep",
            reversibility="compensatable",
            provider_ref={"confirmed_by": by},
            actor=ctx.actor,
        )
        with _pc("go to sleep"):
            desk.after(SLEEP_DELAY_S, _sleep_now(desk, db_file, effect.id, ctx.actor))
        return (
            f"Going to sleep in {SLEEP_DELAY_S:g} seconds. I won't hear you until the computer "
            "wakes up."
        )

    noun = _noun(act)
    effect = fx.record_effect(
        ctx.con,
        kind="pc.power",
        summary=f"I started a {noun} with a one-minute countdown",
        reversibility="compensatable",
        undo_plan={"op": ABORT_OP, "args": {"action": act}, "speaks": "call it off"},
        undo_deadline=fx.deadline_in(POWER_GRACE_S),
        provider_ref={"action": act, "grace_s": POWER_GRACE_S, "confirmed_by": by},
        actor=ctx.actor,
    )
    with _pc(f"start the {noun}"):
        desk.after(POWER_GRACE_S, _countdown_end(desk, db_file, effect.id, act, ctx.actor))
    doing = "Shutting down" if act == "shutdown" else "Restarting"
    return (
        f"{doing} in one minute. Apps will be asked to close, and any with unsaved work will "
        f"wait for you. Say cancel the {noun} to stop it."
    )


def cancel_power(ctx: ToolCtx) -> str:
    desk = _desktop(ctx)
    rows = _countdowns(ctx.con)
    if not rows:
        # Not ours, but a shutdown Windows has pending (shutdown /t from a
        # terminal, say) is still one the user wants stopped.
        with _pc("call it off"):
            aborted = desk.cancel_power()
        if aborted:
            return "I called off the shutdown Windows had pending."
        raise ToolError("There's no shutdown or restart counting down for me to call off.")
    newest = rows[0]
    result = fx.undo(ctx.con, newest.id, actor=ctx.actor)
    if result.outcome == "undone":
        noun = _noun((newest.provider_ref or {}).get("action"))
        return f"Called off. The {noun} won't happen."
    # Too late, or already being dealt with: the ledger's own honesty-checked line.
    return result.spoken


@fx.undo_handler(ABORT_OP)
def _call_off(con: sqlite3.Connection, effect: fx.Effect, args: dict[str, Any]) -> fx.Compensation:
    """Called off before the minute ends: marking the row undone is what stops the countdown.

    :func:`effects.undo` writes that mark from the Compensation returned here, and
    the countdown reads it — from whichever process holds the timer. Aborting a
    shutdown Windows itself has pending as well costs nothing, and covers the one
    case the row cannot: the countdown having handed over a moment before.
    """
    with contextlib.suppress(Exception):
        choose_desktop().cancel_power()
    return fx.Compensation(
        kind="pc.power",
        summary=f"I called off the {_noun(args.get('action'))}",
        reversibility="compensatable",
    )


# ───────────────────────────── the table ─────────────────────────────

_STR = {"type": "STRING"}
_CONFIRM = {
    "type": "BOOLEAN",
    "description": (
        "Leave out on the first call. Set true only on the second call, after the user "
        "answered yes to the read-back in their own words."
    ),
}

TOOLS: tuple[Tool, ...] = (
    Tool(
        name="open_app",
        description=(
            "Open an installed app or a Windows Settings page on this computer: 'open Spotify', "
            "'launch Chrome', 'Hesap Makinesi'ni aç', 'open Bluetooth settings', 'open the "
            "volume mixer'. name is the app as the user said it, in any language; never a path, "
            "a command or a URL. If several apps match, ask which. If none does, the result "
            "lists the installed names: if the user's words translate one of them, call again "
            "with that exact name, otherwise say it is not installed. For web pages use "
            "open_website; for folders use open_folder."
        ),
        handler=open_app,
        parameters={"type": "OBJECT", "properties": {"name": _STR}, "required": ["name"]},
        channels=("desk", "cli"),
        effect="pc.open",
    ),
    Tool(
        name="open_website",
        description=(
            "Open a web page in the browser, or search the web, YouTube, Maps or Wikipedia in "
            "it: 'open youtube.com', 'search YouTube for lofi music', 'show Kadıköy on the "
            "map'. url is an address the user said; for a search pass query and site instead. "
            "Only for showing something on screen: to answer a question yourself, use "
            "web_search."
        ),
        handler=open_website,
        parameters={
            "type": "OBJECT",
            "properties": {
                "url": {"type": "STRING", "description": "A web address, e.g. youtube.com."},
                "query": {"type": "STRING", "description": "What to search for."},
                "site": {
                    "type": "STRING",
                    "enum": list(safety.SEARCH_SITES),
                    "description": "Where to search. Defaults to web.",
                },
            },
        },
        channels=("desk", "cli"),
        effect="pc.open",
    ),
    Tool(
        name="open_folder",
        description=(
            "Open one of the user's folders in File Explorer, a folder inside it, or a file "
            "there (a document, picture or song, never a program): 'open my downloads', 'open "
            "the invoices folder in documents', 'open report.pdf from downloads'. Pass subfolder "
            "and file as the user said them."
        ),
        handler=open_folder,
        parameters={
            "type": "OBJECT",
            "properties": {
                "place": {"type": "STRING", "enum": list(safety.PLACES)},
                "subfolder": _STR,
                "file": _STR,
            },
            "required": ["place"],
        },
        channels=("desk", "cli"),
        effect="pc.open",
    ),
    Tool(
        name="close_app",
        description=(
            "Close an open app's window the way its X button does; the app may ask to save "
            "first, and nothing is ever forced. Needs the user's yes: call once with the app's "
            "name, and the result reads back exactly which window. Call again with confirm "
            "true ONLY after the user says yes in their own words; never set confirm on the "
            "first call. If several windows match, ask which by their titles."
        ),
        handler=close_app,
        parameters={
            "type": "OBJECT",
            "properties": {"name": _STR, "confirm": _CONFIRM},
            "required": ["name"],
        },
        channels=("desk", "cli"),
        effect="pc.app_close",
    ),
    Tool(
        name="volume",
        description=(
            "Change this computer's sound volume: 'turn it up', 'a bit quieter', 'set the "
            "volume to 30', 'mute', 'unmute'. amount is the percent to move for up or down "
            "(default 10); level is 0 to 100 for set. Muting silences Jarvis too."
        ),
        handler=volume,
        parameters={
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "enum": ["up", "down", "mute", "unmute", "set"]},
                "amount": {"type": "INTEGER", "description": "Percent, for up or down."},
                "level": {"type": "INTEGER", "description": "0 to 100, for set."},
            },
            "required": ["action"],
        },
        channels=("desk", "cli"),
        effect="pc.volume",
    ),
    Tool(
        name="media",
        description=(
            "Control whatever music or video is playing on this computer: play or pause, next "
            "track, previous track, stop."
        ),
        handler=media,
        parameters={
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "enum": ["play_pause", "next", "previous", "stop"]}
            },
            "required": ["action"],
        },
        channels=("desk", "cli"),
        effect="pc.media",
    ),
    Tool(
        name="lock_screen",
        description="Lock this computer's screen: 'lock my computer', 'lock the screen'.",
        handler=lock_screen,
        parameters={"type": "OBJECT", "properties": {}},
        channels=("desk", "cli"),
        effect="pc.lock",
    ),
    Tool(
        name="power",
        description=(
            "Put this computer to sleep, restart it or shut it down. Needs the user's yes: call "
            "once and the result reads back what will happen; call again with confirm true ONLY "
            "after the user says yes in their own words; never set confirm on the first call. A "
            "restart or shutdown counts down a minute first and never forces apps closed; "
            "cancel_power calls it off."
        ),
        handler=power,
        parameters={
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "enum": ["sleep", "restart", "shutdown"]},
                "confirm": _CONFIRM,
            },
            "required": ["action"],
        },
        channels=("desk", "cli"),
        effect="pc.power",
    ),
    Tool(
        name="cancel_power",
        description=(
            "Call off a shutdown or restart while its one-minute countdown is running: 'cancel "
            "the shutdown', 'don't restart', 'stop the shutdown'."
        ),
        handler=cancel_power,
        parameters={"type": "OBJECT", "properties": {}},
        channels=("desk", "cli"),
        effect="pc.power",
    ),
)
