"""The real callables the app's settings screen is built with.

:class:`jarvis.app.setup.SetupService` takes plain functions — list the
microphones, fetch the wake model, play a voice sample, open the Claude Code
sign-in — so that it can be tested with fakes. These are the real ones. Each
has a decision in it worth a test (what "no PortAudio" returns, which window a
sign-in opens in), which is why they live here and not in the composition root.

Every failure is an exception carrying a sentence; SetupService turns it into
what the window shows.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jarvis.app import autostart

__all__ = [
    "CREATE_NEW_CONSOLE",
    "autostart_switch",
    "claude_login",
    "download_wake",
    "gemini_preview",
    "list_devices",
]

#: Windows: give the child a console window of its own. The app has none, and
#: a sign-in that asks for a code needs somewhere to ask.
CREATE_NEW_CONSOLE = 0x00000010


def list_devices(*, probe: Any | None = None) -> list[dict[str, str]]:
    """Every device that can both listen and speak, as ``[{"label": ...}]``.

    Empty — not an error — when there is no PortAudio or no audio extra at all:
    the picker then offers only "system default", and the desk's own refusal
    says what is missing.
    """
    try:
        from jarvis.audio.devices import DeviceError, PortAudioProbe, usable_devices
    except ImportError:
        return []
    try:
        found = (PortAudioProbe() if probe is None else probe).devices()
    except DeviceError:
        return []
    return [{"label": label} for label in usable_devices(found)]


def rescan_devices() -> None:
    """Make PortAudio look at the hardware again. Never raises.

    PortAudio reads the device list when it initialises, and sounddevice
    initialises once, on import; in a process that lives as long as the app,
    a headset plugged in after it started is never listed. Re-initialising
    resets only this process's view. Never call it while this process has a
    stream open: the voice samples are the one stream it can hold, and the
    caller keeps them apart.
    """
    try:
        import sounddevice as sd

        sd._terminate()
        sd._initialize()
    except Exception:  # noqa: BLE001 - a stale list is better than no settings screen
        pass


def download_wake(
    phrase: str, *, model_dir: Path | None = None, fetch: Callable[[str], bytes] | None = None
) -> str:
    """Fetch and verify the wake models. Raises with a sentence when it cannot."""
    from jarvis.audio import wake

    where = wake.default_model_dir() if model_dir is None else model_dir
    got = wake.download(phrase, where, fetch=fetch)
    return "The wake-word model is downloaded." if got else "The wake-word model is already here."


def gemini_preview(
    *,
    key: Callable[[], str | None],
    model: str,
    play: Callable[[bytes, int], str | None],
    engine: Callable[..., Any] | None = None,
) -> Callable[[str, str], None]:
    """``preview(voice, sentence)``: say a sample in one of Gemini's voices, here.

    The key is read when the sample is asked for, not when this is built: the
    first-run screen stores the key one step before the voice picker, and a
    preview that only knew the key the app started with would be dead until a
    restart.
    """

    def preview(voice: str, sentence: str) -> None:
        api_key = key()
        if not api_key:
            raise RuntimeError("Voice samples need the Gemini key; add it first.")
        from jarvis.voice.engines import RATE, GeminiTtsEngine

        make = GeminiTtsEngine if engine is None else engine
        pcm = make(api_key=api_key, model=model, voice=voice).synth(sentence, "en")
        if why := play(pcm, RATE):
            raise RuntimeError(why)

    return preview


def claude_login(
    cli: str | None,
    *,
    platform: str | None = None,
    popen: Callable[..., Any] = subprocess.Popen,
    which: Callable[[str], str | None] = shutil.which,
) -> str:
    """Open Claude Code's own sign-in (``claude auth login``) where the user can see it.

    Jarvis never touches the account's credentials: the CLI runs its own
    browser flow in a console window of its own and stores what it stores.
    """
    if not cli:
        raise RuntimeError("Claude Code isn't installed with this copy of Jarvis.")
    platform = sys.platform if platform is None else platform
    argv = [cli, "auth", "login"]
    if platform.startswith("win"):
        popen(argv, creationflags=CREATE_NEW_CONSOLE, close_fds=True)
        return "A Claude Code sign-in window is open. Follow it, then come back here."
    term = which("x-terminal-emulator")
    if term:
        popen([term, "-e", *argv], start_new_session=True, close_fds=True)
        return "A Claude Code sign-in terminal is open. Follow it, then come back here."
    raise RuntimeError(
        "There is no terminal I can open here; run claude auth login in one to sign in."
    )


def autostart_switch(platform: str | None = None) -> Callable[[bool], Any] | None:
    """The start-with-Windows switch, or None where there is no such thing.

    None rather than a no-op, so the settings screen hides the toggle instead
    of showing one that silently does nothing.
    """
    platform = sys.platform if platform is None else platform
    if not platform.startswith("win"):
        return None
    return autostart.set_enabled
