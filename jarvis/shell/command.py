"""What a command says, before it runs and after: refusals, read-back, decoding. Pure.

A COMMAND IS READ BACK BY CODE, NEVER BY THE MODEL. The model wrote the
command; if it also wrote the sentence that describes it, the user would be
approving the model's summary of its own work. So the read-back is built here,
from the text that will actually run, and the screen copy is that text verbatim.

REFUSED BEFORE ANYTHING IS SAID ABOUT IT, when the read-back cannot be true:

* hidden characters (control, zero-width, bidirectional overrides): what the
  user hears and sees is then not what runs — a right-to-left override can make
  ``rm`` display as anything at all;
* an encoded payload (``-EncodedCommand``, ``FromBase64String``): the words read
  back would be a blob, and the blob is the command;
* more than :data:`MAX_LINES` lines or :data:`MAX_CHARS` characters: nobody can
  check that much by ear, and a summary of it is not consent to it.

WARNINGS ONLY EVER ADD CAUTION. :func:`warnings` names the cmdlets that delete,
download, execute strings or power off, and its sentences are added to the
read-back. It is not a boundary: PowerShell is far too expressive for a list to
decide what is safe, and a command it misses is still read back and still needs
the user's yes. What it may never do is make anything run without one.

AFTER THE RUN, the bytes a console program prints are not UTF-8 on Windows:
native tools write the console's OEM code page (cp857 on a Turkish machine),
which is not the ANSI page (cp1254) Python's locale decoding would pick. Strict
UTF-8 fails on OEM bytes, so "UTF-8, else OEM, else ANSI" is decidable, and is
what :func:`decode` does with the fallbacks the caller looked up.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

__all__ = [
    "ENV_KEY",
    "FULL_TEXT_WHERE",
    "MAX_CHARS",
    "MAX_LINES",
    "SHORT_CHARS",
    "WRAPPER",
    "Refused",
    "check",
    "clean",
    "clip",
    "decode",
    "readback",
    "speakable",
    "warnings",
]

#: Beyond these a read-back is a summary, and a summary is not something the
#: user can say yes to with their eyes shut.
MAX_CHARS = 4000
MAX_LINES = 40
#: A command this short, on one line, is read out in full.
SHORT_CHARS = 120

#: Where the full text of a long command can be read. The read-back is spoken,
#: and a script is not something to listen to.
FULL_TEXT_WHERE = "The full text is in the Jarvis window's activity feed."

#: The environment variable that carries the command into the wrapper.
ENV_KEY = "JARVIS_RUN_COMMAND"

#: The fixed PowerShell program. The user's command arrives in an environment
#: variable and is deleted from the environment before it runs, so it is never
#: on a command line (no quoting to get wrong, nothing for another process to
#: read from the argv) and the command cannot read itself back out. Joined
#: rather than formatted: PowerShell's braces are str.format's placeholders.
#:
#: Measured on pwsh 7.4 (see the commands research): quotes and Turkish text
#: round-trip, `exit 3` and a native `exit 5` keep their codes, a parse error is
#: exit 2 with the parser's message, a non-terminating error is exit 1, and
#: Read-Host fails fast under -NonInteractive instead of hanging.
WRAPPER = "; ".join(
    (
        "$ProgressPreference = 'SilentlyContinue'",
        "try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch {}",
        "$OutputEncoding = [System.Text.UTF8Encoding]::new($false)",
        "$c = $env:" + ENV_KEY,
        "Remove-Item Env:" + ENV_KEY,
        "$global:LASTEXITCODE = 0",
        "try { $sb = [ScriptBlock]::Create($c) } catch "
        "{ [Console]::Error.WriteLine($_.Exception.Message); exit 2 }",
        "$Error.Clear()",
        "& $sb",
        "if ($LASTEXITCODE) { exit $LASTEXITCODE }",
        "if ($Error.Count -gt 0) { exit 1 }",
    )
)


class Refused(ValueError):
    """A command that will not be read back or run. ``spoken`` says why, to the user."""

    def __init__(self, spoken: str) -> None:
        super().__init__(spoken)
        self.spoken = spoken


#: Unicode categories that render as nothing, or as something else: control,
#: format (zero-width joiners, bidi overrides, the BOM), surrogates, private use,
#: unassigned, and the line/paragraph separators a shell may or may not split on.
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
_ALLOWED_CONTROLS = frozenset({"\t", "\n"})

#: PowerShell accepts any unambiguous prefix of a parameter, and an en or em
#: dash in place of the hyphen.
_DASHES = re.escape("-/\u2013\u2014\u2015")
_PARAM = re.compile(rf"(?<![\w{_DASHES}])[{_DASHES}]([A-Za-z]+)(?=[\s:'\"]|$)")
_POWERSHELL = re.compile(r"(?i)\b(?:powershell|pwsh)(?:\.exe)?\b")


def check(command: str) -> str:
    """The text that will run, normalised; or :class:`Refused` with a sentence."""
    if not isinstance(command, str):
        raise Refused("There's no command there for me to run.")
    text = command.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise Refused("There's no command there for me to run.")
    if len(text) > MAX_CHARS:
        raise Refused(
            f"That command is {len(text)} characters long, too long to read back to you, so "
            "I won't run it. A script file you can open and check is the better way."
        )
    if text.count("\n") + 1 > MAX_LINES:
        raise Refused(
            f"That command is more than {MAX_LINES} lines, too long to read back to you, so "
            "I won't run it. A script file you can open and check is the better way."
        )
    for ch in text:
        if ch not in _ALLOWED_CONTROLS and unicodedata.category(ch) in _HIDDEN_CATEGORIES:
            raise Refused(
                "That command has hidden characters in it, so what I'd read back to you is not "
                "what would run. I won't run it."
            )
    if _encoded(text):
        raise Refused(
            "That command hides what it does in encoded text, so I can't read it back to you. "
            "I won't run it."
        )
    return text


def _encoded(text: str) -> bool:
    if "frombase64string" in text.casefold():
        return True
    for name in (m.group(1).casefold() for m in _PARAM.finditer(text)):
        if name == "ec" or (
            len(name) >= 3
            and ("encodedcommand".startswith(name) or "encodedarguments".startswith(name))
        ):
            return True
        # Bare -e / -en are too common elsewhere (grep -e) to refuse on their
        # own; next to a PowerShell they are -EncodedCommand.
        if name in ("e", "en") and _POWERSHELL.search(text):
            return True
    return False


# ───────────────────────────── the read-back ─────────────────────────────

#: Spoken words for the operators a listener cannot hear. Longest first, so
#: "&&" is not read as two ampersands.
_SPOKEN_OPERATORS: tuple[tuple[str, str], ...] = (
    ("&&", " and then "),
    ("||", " or else "),
    (">>", " appended to "),
    ("|", " pipe "),
    (";", " then "),
    (">", " into "),
)

#: Lower-cased command or cmdlet -> how to name it. Things that delete, wipe,
#: power off, kill, download, run strings as code, start other programs, or
#: change security settings and the registry.
_RISKY: dict[str, str] = {
    name.casefold(): name
    for name in (
        "Remove-Item", "rm", "del", "erase", "rd", "rmdir", "ri", "Clear-Content",
        "Clear-RecycleBin", "Move-Item", "mv", "Rename-Item", "Set-Content", "Out-File",
        "format", "Format-Volume", "Clear-Disk", "Initialize-Disk", "Remove-Partition",
        "diskpart", "Stop-Computer", "Restart-Computer", "shutdown", "Stop-Process", "kill",
        "taskkill", "Set-ExecutionPolicy", "Invoke-WebRequest", "iwr", "Invoke-RestMethod",
        "irm", "curl", "wget", "Start-BitsTransfer", "Invoke-Expression", "iex",
        "Start-Process", "saps", "reg", "Set-ItemProperty", "New-ItemProperty",
        "Remove-ItemProperty", "Set-MpPreference", "Add-MpPreference", "netsh", "bcdedit",
        "takeown", "icacls", "cipher", "schtasks", "New-Service", "Set-Acl", "winget",
        "sudo",
    )
}  # fmt: skip
_WORD = re.compile(r"(?<![\w$.\-])[A-Za-z][\w.\-]*")
_REDIRECT = re.compile(r">(?!&)\s*(?!\$null\b|/dev/null\b|nul\b)\S", re.IGNORECASE)


def speakable(command: str) -> str:
    """The command as words a listener can follow: operators named, whitespace folded."""
    out = command
    for op, words in _SPOKEN_OPERATORS:
        out = out.replace(op, words)
    return " ".join(out.split())


def warnings(command: str) -> tuple[str, ...]:
    """Sentences of extra caution, from the text alone. Never a reason to skip the yes."""
    seen: list[str] = []
    for word in _WORD.findall(command):
        key = word.casefold().removesuffix(".exe")
        name = _RISKY.get(key)
        if name is not None and name not in seen:
            seen.append(name)
    out: list[str] = []
    if seen:
        out.append(f"It uses {_and(seen)}.")
    if _REDIRECT.search(command):
        out.append("It writes its output into a file.")
    return tuple(out)


def readback(command: str, *, shell: str) -> tuple[str, str]:
    """``(spoken, screen)``: the sentence the reader says, and the exact text for a screen.

    ``command`` is what :func:`check` returned. ``shell`` is how to name the
    shell aloud ("PowerShell").
    """
    lines = command.split("\n")
    caution = " ".join(warnings(command))
    tail = " I can't undo a command once it has run."
    if len(lines) == 1 and len(command) <= SHORT_CHARS:
        spoken = f"I'll run this in {shell}, in your home folder: {speakable(command)}."
        return _join(spoken, caution) + tail, command
    first = next((ln for ln in lines if ln.strip()), "")
    opening = " ".join(speakable(first).split()[:6])
    if len(opening) > 60:
        opening = opening[:59].rstrip() + "…"
    what = (
        f"a {len(lines)}-line script in {shell}" if len(lines) > 1 else f"a long command in {shell}"
    )
    spoken = (
        f"I'll run {what}, in your home folder. It is {len(command)} characters long and "
        f"starts with {opening}."
    )
    return _join(spoken, caution, FULL_TEXT_WHERE) + tail, command


def _join(*parts: str) -> str:
    return " ".join(p for p in parts if p)


def _and(items: Sequence[str]) -> str:
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f" and {items[-1]}"


# ───────────────────────────── after it ran ─────────────────────────────

_ANSI = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI: colours, cursor moves
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC: window titles, hyperlinks
    r"|\x1b[PX^_][^\x1b]*\x1b\\"  # DCS, SOS, PM, APC strings
    r"|\x1b[@-Z\\-_]"  # two-byte escapes
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def decode(raw: bytes, *, fallbacks: Sequence[str] = ()) -> str:
    """Strict UTF-8, then each fallback code page strictly, then UTF-8 with replacement.

    Strict first, because a lenient UTF-8 decode of OEM bytes "works" and hands
    the model mojibake it will confidently read out.
    """
    for encoding in ("utf-8", *fallbacks):
        try:
            return raw.decode(encoding).removeprefix("\ufeff")
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace").removeprefix("\ufeff")


def clean(text: str) -> str:
    """What a terminal would have shown: no escape codes, progress lines collapsed.

    A ``\\r`` without a ``\\n`` is a progress bar redrawing itself; the terminal
    shows only the last frame, so that is what is kept.
    """
    text = _ANSI.sub("", text).replace("\r\n", "\n")
    lines = []
    for line in text.split("\n"):
        if "\r" in line:
            frames = [f for f in line.split("\r") if f]
            line = frames[-1] if frames else ""
        lines.append(line)
    return _CONTROL.sub("", "\n".join(lines))


def clip(text: str, *, head: int, tail: int, line_max: int | None = None) -> tuple[str, bool]:
    """At most ``head`` characters from the start and ``tail`` from the end.

    Both ends, because the start says what the command is and the end says how
    it finished; the middle of a long listing is the part nobody asked about.
    The marker counts the lines left out, so a reader can tell "short output"
    from "clipped output".
    """
    clipped = False
    if line_max is not None:
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if len(line) > line_max:
                lines[i] = line[: line_max - 1] + "…"
                clipped = True
        text = "\n".join(lines)
    if len(text) <= head + tail:
        return text, clipped
    start = text[:head]
    end = text[len(text) - tail :] if tail else ""
    omitted = text[head : len(text) - tail].count("\n")
    marker = f"\n[… {omitted} more lines not shown …]\n" if omitted else "\n[…]\n"
    return start + marker + end, True
