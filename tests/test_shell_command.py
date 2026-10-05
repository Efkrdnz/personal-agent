"""What a command says before it runs and after: the read-back, the refusals, the decoding.

Every refusal here is a way the read-back could be made untrue — hidden
characters, an encoded payload, a script too long to hear — so each one is a
test that a user could have said yes to something they did not hear.

Hidden characters are built with ``chr`` rather than written into this file:
a test file full of literal right-to-left overrides is its own kind of hazard.
"""

from __future__ import annotations

import pytest

from jarvis.shell import command as cmd
from jarvis.shell.command import (
    ENV_KEY,
    FULL_TEXT_WHERE,
    MAX_CHARS,
    MAX_LINES,
    WRAPPER,
    Refused,
    check,
    clean,
    clip,
    decode,
    readback,
    speakable,
    warnings,
)

RLO = chr(0x202E)  # right-to-left override
ZWSP = chr(0x200B)  # zero-width space
BOM = chr(0xFEFF)
LSEP = chr(0x2028)  # line separator
EN_DASH = chr(0x2013)


# ───────────────────────────── refusals ─────────────────────────────


@pytest.mark.parametrize("text", ["", "   ", "\n\n", "\t"])
def test_nothing_to_run_is_refused(text: str) -> None:
    with pytest.raises(Refused, match="no command"):
        check(text)


def test_a_command_too_long_to_hear_is_refused() -> None:
    with pytest.raises(Refused, match="too long"):
        check("x" * (MAX_CHARS + 1))
    assert check("x" * MAX_CHARS)


def test_a_script_with_too_many_lines_is_refused() -> None:
    with pytest.raises(Refused, match=f"more than {MAX_LINES} lines"):
        check("\n".join(["Get-Date"] * (MAX_LINES + 1)))
    assert check("\n".join(["Get-Date"] * MAX_LINES))


@pytest.mark.parametrize(
    "text",
    [
        f"echo safe{RLO}txt.exe",
        f"Remove{ZWSP}-Item x",
        f"{BOM}ipconfig",
        f"echo a{LSEP}rm -rf ~",
        "echo \x07bell",
        "echo \x00nul",
        "echo \x1b[31mred",
    ],
    ids=["bidi", "zero-width", "bom", "line-separator", "bell", "nul", "escape"],
)
def test_hidden_characters_are_refused(text: str) -> None:
    with pytest.raises(Refused, match="hidden characters"):
        check(text)


@pytest.mark.parametrize(
    "text",
    [
        "powershell -EncodedCommand SQBFAFgA",
        "pwsh -enc SQBFAFgA",
        "powershell.exe -ENC:SQBFAFgA",
        "powershell -ec SQBFAFgA",
        "powershell -e SQBFAFgA",
        f"powershell {EN_DASH}enc SQBFAFgA",
        "powershell /enc SQBFAFgA",
        "& (gcm pow*) -encodedc SQBFAFgA",
        "iex ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('SQBFAFgA')))",
        "pwsh -EncodedArguments abc",
    ],
)
def test_encoded_payloads_are_refused(text: str) -> None:
    with pytest.raises(Refused, match="encoded"):
        check(text)


@pytest.mark.parametrize(
    "text",
    [
        "grep -e pattern file.txt",
        "Get-Content notes.txt -Encoding UTF8",
        "git commit -m 'encode the thing'",
        "Select-String -Pattern enc -Path *.log",
        "echo -en hello",
    ],
)
def test_ordinary_flags_are_not_mistaken_for_encoded_commands(text: str) -> None:
    assert check(text) == text


def test_tabs_survive_and_line_endings_are_normalised() -> None:
    assert check("  Get-Date\t-Format o \r\nipconfig\r ") == "Get-Date\t-Format o \nipconfig"


def test_the_refusal_is_a_sentence_for_the_user() -> None:
    with pytest.raises(Refused) as e:
        check(f"echo{RLO}")
    assert e.value.spoken.endswith("I won't run it.")


# ───────────────────────────── the read-back ─────────────────────────────


def test_a_short_command_is_read_back_exactly_with_operators_named() -> None:
    spoken, screen = readback("Get-Process | Sort-Object CPU", shell="PowerShell")
    assert screen == "Get-Process | Sort-Object CPU"
    assert "I'll run this in PowerShell, in your home folder:" in spoken
    assert "Get-Process pipe Sort-Object CPU." in spoken
    assert spoken.endswith("I can't undo a command once it has run.")


def test_a_long_script_is_summarised_aloud_and_kept_whole_for_the_screen() -> None:
    script = "Get-ChildItem -Path C:\\Temp -Recurse\nRemove-Item C:\\Temp\\old -Recurse\nGet-Date"
    spoken, screen = readback(script, shell="PowerShell")
    assert screen == script
    assert "a 3-line script in PowerShell" in spoken
    assert f"{len(script)} characters long" in spoken
    assert "starts with Get-ChildItem -Path C:\\Temp -Recurse" in spoken
    assert "It uses Remove-Item." in spoken
    assert FULL_TEXT_WHERE in spoken
    assert "Remove-Item C:\\Temp\\old" not in spoken  # not read out in full


def test_a_long_single_line_is_summarised_too() -> None:
    line = "Get-ChildItem " + " ".join(f"-Filter{i}" for i in range(30))
    spoken, _ = readback(line, shell="PowerShell")
    assert "a long command in PowerShell" in spoken and FULL_TEXT_WHERE in spoken


def test_speakable_names_what_a_listener_cannot_hear() -> None:
    assert speakable("a && b || c; d > out.txt >> log") == (
        "a and then b or else c then d into out.txt appended to log"
    )


@pytest.mark.parametrize(
    ("text", "named"),
    [
        ("Remove-Item -Recurse C:\\x", "Remove-Item"),
        ("iwr https://example.com -OutFile x.exe", "iwr"),
        ("rm -rf ~/old", "rm"),
        ("C:\\Windows\\System32\\shutdown.exe /s", "shutdown"),
        ("Stop-Process -Name chrome", "Stop-Process"),
        ("iex $payload", "iex"),
    ],
)
def test_risky_commands_are_named_in_the_read_back(text: str, named: str) -> None:
    assert any(named in w for w in warnings(text))
    assert named in readback(text, shell="PowerShell")[0]


@pytest.mark.parametrize(
    "text",
    ["Get-Date", "ipconfig /all", "Get-Process | Format-Table", "git status 2>&1", "dir > $null"],
)
def test_harmless_commands_get_no_warning(text: str) -> None:
    assert warnings(text) == ()


def test_a_redirect_into_a_file_is_mentioned() -> None:
    assert "It writes its output into a file." in warnings("Get-Process > procs.txt")


# ───────────────────────────── after it ran ─────────────────────────────

TURKISH = "Ethernet bağdaştırıcısı: ğüşıöç İ"


def test_utf8_is_tried_first() -> None:
    assert decode(TURKISH.encode("utf-8"), fallbacks=("cp857", "cp1254")) == TURKISH


def test_oem_bytes_are_decoded_with_the_oem_page_not_the_ansi_one() -> None:
    raw = TURKISH.encode("cp857")  # what ipconfig prints on a Turkish console
    assert decode(raw, fallbacks=("cp857", "cp1254")) == TURKISH
    # The ANSI page "works" on the same bytes and produces mojibake, which is
    # why the OEM page is tried first.
    assert decode(raw, fallbacks=("cp1254",)) != TURKISH


def test_undecodable_bytes_are_replaced_not_raised() -> None:
    assert "\ufffd" in decode(b"ok \xff\xfe", fallbacks=())


def test_a_byte_order_mark_is_dropped() -> None:
    assert decode(b"\xef\xbb\xbfhello") == "hello"


def test_clean_strips_colour_titles_and_progress_frames() -> None:
    raw = (
        "\x1b]0;window title\x07"
        "\x1b[31mError:\x1b[0m bad\r\n"
        "progress 10%\rprogress 50%\rprogress 100%\n"
        "done\x00"
    )
    assert clean(raw) == "Error: bad\nprogress 100%\ndone"


def test_clip_keeps_both_ends_and_counts_what_it_left_out() -> None:
    text = "\n".join(f"line {i}" for i in range(1000))
    out, cut = clip(text, head=50, tail=30)
    assert cut is True
    assert out.startswith("line 0\nline 1")
    assert out.endswith("line 999")
    assert "more lines not shown" in out
    assert clip("short", head=50, tail=30) == ("short", False)


def test_clip_shortens_one_enormous_line() -> None:
    out, cut = clip("x" * 1000 + "\nok", head=5000, tail=0, line_max=100)
    assert cut is True and out.split("\n")[0] == "x" * 99 + "…" and out.endswith("ok")


def test_the_wrapper_reads_the_command_from_the_environment_and_deletes_it() -> None:
    assert f"$c = $env:{ENV_KEY}" in WRAPPER
    assert f"Remove-Item Env:{ENV_KEY}" in WRAPPER
    # Deleted before it runs: the removal comes before the script block is invoked.
    assert WRAPPER.index("Remove-Item Env:") < WRAPPER.index("& $sb")
    assert "{0}" not in WRAPPER and "'" in WRAPPER  # built by joining, never by .format


def test_the_module_is_pure() -> None:
    import ast
    from pathlib import Path

    tree = ast.parse(Path(cmd.__file__).read_text(encoding="utf-8"))
    imported = {
        n.module if isinstance(n, ast.ImportFrom) else a.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
        for a in n.names
    }
    assert not any(str(m).startswith(("subprocess", "os", "jarvis")) for m in imported), imported
