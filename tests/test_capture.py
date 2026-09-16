"""Policy, backends and the service that ties them to the ledger.

Two things are being defended here and neither is "the code runs".

THE REFUSALS ARE THE FEATURE. Wayland's consent dialog, macOS's un-granted
Screen Recording permission and a focused ``.env`` are not edge cases to be
tidied away; they are the reasons this package is not fifty lines. Each one has
a test that asserts a TYPED refusal arrives, in this process, with a remedy
attached — because the alternative failure is silence on a machine nobody is
standing next to.

THE LEDGER ROW MAY NOT OVER-PROMISE. Every capture is recorded through
:mod:`jarvis.effects`, whose honesty check fires on the generated verdict. So
the test that matters is not that a row exists but that
:func:`jarvis.effects.spoken_effect_line` can be generated from it without
raising, and that what it says about undoing a screenshot is nothing.
"""

from __future__ import annotations

import ast
import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from jarvis.bus import Redactor, read_since
from jarvis.capture import (
    CAPTURE_TOOLS,
    Artifact,
    BackendStatus,
    Box,
    CapturePolicy,
    Capturer,
    CaptureRefused,
    CommandCapturer,
    NullCapturer,
    SyntheticCapturer,
    Window,
    capture,
    capture_pixels,
    capture_transcript,
    choose_capturer,
    decode_png,
    detect_backend,
    encode_png,
    render_transcript,
    text_only_policy,
)
from jarvis.capture.png import Canvas
from jarvis.capture.redact import REDACTION_FILL
from jarvis.db import connect, migrate
from jarvis.effects import PROMISE_WORDS, get_effect, recent_effects, spoken_effect_line
from jarvis.ids import now

SECRET = "sk-ant-api03-NOTAREALKEY0000000000"
TS = "2026-09-16T12:00:00.000Z"
JOB = "job_capturetests"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "jarvis.db"
    c = connect(p)
    migrate(c)
    # effects.job_id is a real FK and foreign_keys is ON.
    c.execute(
        """INSERT INTO jobs (id, kind, title, state, created_at, updated_at, created_by)
           VALUES (?, 'claude_code', 'the todo app build', 'running', ?, ?, 'test')""",
        (JOB, now(), now()),
    )
    c.close()
    return p


@pytest.fixture
def con(db_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(db_path)
    yield c
    c.close()


@pytest.fixture
def redactor() -> Redactor:
    return Redactor.of([SECRET])


def terminal(*lines: str) -> str:
    return render_transcript(lines, title="claude code", now_ts=TS)


# ───────────────────────────── policy ─────────────────────────────


@pytest.mark.parametrize(
    "window",
    [
        Window(title="nvim ~/work/api/.env"),
        Window(path="/home/mark/project/.env.local"),
        Window(app="1Password"),
        Window(title="ssh-keygen id_ed25519"),
    ],
)
def test_the_never_capture_list_stops_the_text_path_too(
    con: sqlite3.Connection, redactor: Redactor, window: Window
) -> None:
    # A transcript of a session editing a .env CONTAINS the .env. Applying the
    # list only to pixels would exempt the path that carries the whole file.
    with pytest.raises(CaptureRefused) as exc:
        capture_transcript(con, text="x", redactor=redactor, window=window, now_ts=TS)
    assert exc.value.refusal.code == "policy_forbidden"
    assert exc.value.refusal.remedy


def test_a_harmless_window_is_not_refused(con: sqlite3.Connection, redactor: Redactor) -> None:
    art = capture_transcript(
        con, text="hello", redactor=redactor, window=Window(title="claude code"), now_ts=TS
    )
    assert art.data == b"hello"


def test_the_whole_screen_is_off_by_default(con: sqlite3.Connection, redactor: Redactor) -> None:
    with pytest.raises(CaptureRefused) as exc:
        capture_pixels(
            con,
            capturer=SyntheticCapturer(),
            redactor=redactor,
            subject="screen",
            window=Window(title="claude code"),
            now_ts=TS,
        )
    assert exc.value.refusal.code == "subject_not_allowed"


def test_an_unidentified_screen_blocks_pixels_but_not_text(
    con: sqlite3.Connection, redactor: Redactor
) -> None:
    # If the caller cannot say what is focused, the forbidden list cannot be
    # evaluated — and a policy that permits the capture in that case is switched
    # off exactly when the desktop is least introspectable.
    with pytest.raises(CaptureRefused) as exc:
        capture_pixels(con, capturer=SyntheticCapturer(), redactor=redactor, now_ts=TS)
    assert exc.value.refusal.code == "policy_forbidden"
    assert capture_transcript(con, text="fine", redactor=redactor, now_ts=TS).data == b"fine"


def test_text_only_policy_refuses_pixels_with_a_sentence(
    con: sqlite3.Connection, redactor: Redactor
) -> None:
    with pytest.raises(CaptureRefused) as exc:
        capture_pixels(
            con,
            capturer=SyntheticCapturer(),
            redactor=redactor,
            window=Window(title="claude code"),
            policy=text_only_policy(),
            now_ts=TS,
        )
    assert exc.value.refusal.code in ("pixels_disabled", "subject_not_allowed")
    assert exc.value.refusal.spoken


# ───────────────────────────── backends ─────────────────────────────


def which_of(*present: str):
    return lambda name: f"/usr/bin/{name}" if name in present else None


def test_wayland_refuses_rather_than_opening_a_dialog_nobody_will_click() -> None:
    status = detect_backend(
        platform="linux",
        env={"WAYLAND_DISPLAY": "wayland-0", "XDG_SESSION_TYPE": "wayland"},
        which=which_of("maim", "import"),
    )
    assert status.available is False
    assert status.refusal is not None
    assert status.refusal.code == "wayland_consent"
    assert status.needs_consent_dialog is True
    # The whole point: an X11 tool being installed does not rescue this.
    assert "portal" in status.refusal.detail


def test_wlroots_grim_is_the_one_wayland_path_that_works_unattended() -> None:
    status = detect_backend(
        platform="linux",
        env={"WAYLAND_DISPLAY": "wayland-0"},
        which=which_of("grim"),
    )
    assert status.available is True
    assert status.name == "grim"
    assert "no consent dialog" in status.note


def test_x11_picks_the_first_installed_tool() -> None:
    status = detect_backend(
        platform="linux", env={"DISPLAY": ":0"}, which=which_of("scrot", "import")
    )
    assert (status.available, status.name) == (True, "import")


def test_an_x11_display_with_no_tools_says_which_to_install() -> None:
    status = detect_backend(platform="linux", env={"DISPLAY": ":0"}, which=which_of())
    assert status.refusal is not None
    assert status.refusal.code == "no_backend"
    assert "maim" in status.refusal.remedy


def test_no_display_at_all_is_the_machine_this_runs_on() -> None:
    status = detect_backend(platform="linux", env={}, which=which_of())
    assert status.refusal is not None
    assert status.refusal.code == "no_display"
    assert "transcript" in status.refusal.remedy


def test_macos_cannot_check_the_permission_before_the_fact_and_says_so() -> None:
    status = detect_backend(platform="darwin", env={}, which=which_of("screencapture"))
    assert status.available is True
    assert "cannot be checked" in status.note


def test_choose_capturer_always_returns_a_capturer() -> None:
    # One code path for the caller: never None, and the refusal arrives from
    # grab() with a sentence attached.
    cap = choose_capturer(platform="linux", env={}, which=which_of())
    assert isinstance(cap, Capturer)
    with pytest.raises(CaptureRefused) as exc:
        cap.grab("screen")
    assert exc.value.refusal.code == "no_display"


def test_null_and_synthetic_satisfy_the_same_protocol_as_the_real_ones() -> None:
    assert isinstance(NullCapturer(), Capturer)
    assert isinstance(SyntheticCapturer(), Capturer)
    assert isinstance(CommandCapturer(CAPTURE_TOOLS[0]), Capturer)


@pytest.mark.parametrize(("name", "expected_head"), [("grim", "grim"), ("maim", "maim")])
def test_argv_templates_are_built_not_guessed(name: str, expected_head: str) -> None:
    tool = next(t for t in CAPTURE_TOOLS if t.name == name)
    argv = tool.argv("screen", Path("/tmp/x/shot.png"), None)
    assert argv[0] == expected_head
    assert "/tmp/x/shot.png" in argv


def test_a_tool_that_cannot_frame_one_window_says_so_instead_of_shooting_the_lot() -> None:
    grim = next(t for t in CAPTURE_TOOLS if t.name == "grim")
    with pytest.raises(ValueError, match="cannot capture a single window"):
        grim.argv("pane", Path("/tmp/shot.png"), "0x123")


def test_a_window_id_reaches_the_argv() -> None:
    maim = next(t for t in CAPTURE_TOOLS if t.name == "maim")
    assert "0x4400007" in maim.argv("pane", Path("/tmp/shot.png"), "0x4400007")


def test_command_capturer_decodes_what_the_tool_wrote(tmp_path: Path) -> None:
    img = Canvas.blank(6, 4, (3, 4, 5)).freeze()

    def fake_run(argv: Sequence[str], out: Path) -> None:
        assert argv[0] == "maim"
        out.write_bytes(encode_png(img))

    cap = CommandCapturer(next(t for t in CAPTURE_TOOLS if t.name == "maim"), run=fake_run)
    assert cap.grab("screen").pixels == img.pixels


def test_a_failing_tool_becomes_a_typed_refusal_not_a_traceback() -> None:
    def boom(argv: Sequence[str], out: Path) -> None:
        raise RuntimeError("exit 1: cannot open display")

    cap = CommandCapturer(CAPTURE_TOOLS[1], run=boom)
    with pytest.raises(CaptureRefused) as exc:
        cap.grab("screen")
    assert exc.value.refusal.code == "capture_failed"
    assert "cannot open display" in exc.value.refusal.detail


def test_a_picture_we_cannot_decode_is_a_picture_we_cannot_redact() -> None:
    def junk(argv: Sequence[str], out: Path) -> None:
        out.write_bytes(b"JPEG or something else entirely")

    cap = CommandCapturer(CAPTURE_TOOLS[1], run=junk)
    with pytest.raises(CaptureRefused) as exc:
        cap.grab("screen")
    assert exc.value.refusal.code == "undecodable"
    assert "cannot be redacted" in exc.value.refusal.remedy


def test_macos_without_permission_returns_a_successful_empty_desktop() -> None:
    # The trap: exit 0, a valid PNG, and nothing in it. There is no error to
    # check, so the only available evidence is the picture itself.
    def wallpaper(argv: Sequence[str], out: Path) -> None:
        out.write_bytes(encode_png(Canvas.blank(20, 20, (58, 58, 60)).freeze()))

    cap = CommandCapturer(
        next(t for t in CAPTURE_TOOLS if t.name == "screencapture"),
        run=wallpaper,
        blank_means_no_permission=True,
    )
    with pytest.raises(CaptureRefused) as exc:
        cap.grab("screen")
    assert exc.value.refusal.code == "macos_permission"
    assert "Screen Recording" in exc.value.refusal.remedy


def test_a_refusing_backend_never_runs_a_subprocess() -> None:
    def explode(argv: Sequence[str], out: Path) -> None:  # pragma: no cover - must not run
        raise AssertionError("the tool was invoked on a machine that refused")

    cap = CommandCapturer(
        CAPTURE_TOOLS[1],
        status=BackendStatus("maim", available=False, refusal=NullCapturer().status().refusal),
        run=explode,
    )
    with pytest.raises(CaptureRefused):
        cap.grab("screen")


# ───────────────────────────── the service ─────────────────────────────


def test_text_wins_when_both_are_available(con: sqlite3.Connection, redactor: Redactor) -> None:
    # "Make the honest thing the easy thing" as a default, not as advice.
    art = capture(
        con,
        redactor=redactor,
        text=terminal("build ok"),
        capturer=SyntheticCapturer(lines=["BUILD OK"]),
        subject="pane",
        window=Window(title="claude code"),
        now_ts=TS,
    )
    assert art.media == "text"
    assert art.redaction.exact is True


def test_asking_for_a_transcript_without_one_is_the_callers_bug(
    con: sqlite3.Connection, redactor: Redactor
) -> None:
    with pytest.raises(ValueError, match="needs text"):
        capture(con, redactor=redactor, subject="transcript", now_ts=TS)


def test_the_transcript_that_leaves_has_no_secret_in_it(
    con: sqlite3.Connection, redactor: Redactor
) -> None:
    art = capture(
        con,
        redactor=redactor,
        text=terminal("$ env", f"ANTHROPIC_API_KEY={SECRET}", "$ ok"),
        now_ts=TS,
    )
    assert SECRET.encode() not in art.data
    assert art.filename == "jarvis-transcript-20260916T120000Z.txt"
    assert art.mime.startswith("text/plain")
    assert art.lossless is True


def test_a_long_transcript_keeps_the_recent_end(
    con: sqlite3.Connection, redactor: Redactor
) -> None:
    text = "\n".join(f"line {i}" for i in range(5000))
    art = capture_transcript(
        con, text=text, redactor=redactor, policy=CapturePolicy(max_bytes=400), now_ts=TS
    )
    body = art.data.decode()
    assert art.size <= 400
    assert body.startswith("[earlier output trimmed")
    assert body.endswith("line 4999")
    assert art.warnings


def test_trimming_never_splits_a_character(con: sqlite3.Connection, redactor: Redactor) -> None:
    art = capture_transcript(
        con,
        text="şğüöç" * 400,
        redactor=redactor,
        policy=CapturePolicy(max_bytes=300),
        now_ts=TS,
    )
    art.data.decode("utf-8")  # raises if the tail was cut mid-codepoint


def test_the_picture_carries_its_own_warning(con: sqlite3.Connection, redactor: Redactor) -> None:
    art = capture_pixels(
        con,
        capturer=SyntheticCapturer(lines=["BUILD OK"]),
        redactor=redactor,
        subject="pane",
        window=Window(title="claude code"),
        now_ts=TS,
    )
    assert art.media == "image"
    assert art.redaction.exact is False
    assert "NOT text-redacted" in art.caption
    assert art.warnings
    # In the pixels, not only in the caption: a caption does not survive a crop.
    assert decode_png(art.data).pixel(0, 0) == REDACTION_FILL


def test_a_named_region_is_covered_in_the_delivered_bytes(
    con: sqlite3.Connection, redactor: Redactor
) -> None:
    cap = SyntheticCapturer(lines=["BUILD OK", "KEY ON THIS ROW"])
    top = cap.line_top(1)
    art = capture_pixels(
        con,
        capturer=cap,
        redactor=redactor,
        subject="pane",
        window=Window(title="claude code"),
        boxes=[Box(0, top, 240, cap.line_height)],
        now_ts=TS,
    )
    img = decode_png(art.data)
    assert {img.pixel(x, top + 2) for x in range(0, 200, 10)} == {REDACTION_FILL}


def test_a_visible_secret_with_nothing_covering_it_is_refused(
    con: sqlite3.Connection, redactor: Redactor
) -> None:
    # Without OCR this is the ONLY moment the pixel path can be honest about a
    # credential, so it is taken rather than deferred to a warning label.
    with pytest.raises(CaptureRefused) as exc:
        capture_pixels(
            con,
            capturer=SyntheticCapturer(lines=["KEY"]),
            redactor=redactor,
            subject="pane",
            window=Window(title="claude code"),
            visible_text=f"export KEY={SECRET}",
            now_ts=TS,
        )
    assert exc.value.refusal.code == "secret_visible"
    assert "transcript instead" in exc.value.refusal.remedy


def test_naming_the_region_lifts_that_refusal(con: sqlite3.Connection, redactor: Redactor) -> None:
    art = capture_pixels(
        con,
        capturer=SyntheticCapturer(lines=["KEY"]),
        redactor=redactor,
        subject="pane",
        window=Window(title="claude code"),
        visible_text=f"export KEY={SECRET}",
        boxes=[Box(0, 0, 240, 40)],
        now_ts=TS,
    )
    assert art.redaction.regions_filled == 1


def test_an_oversized_picture_is_refused_rather_than_cropped(
    con: sqlite3.Connection, redactor: Redactor
) -> None:
    with pytest.raises(CaptureRefused) as exc:
        capture_pixels(
            con,
            capturer=SyntheticCapturer(lines=["BUILD OK"]),
            redactor=redactor,
            subject="pane",
            window=Window(title="claude code"),
            policy=CapturePolicy(max_bytes=50),
            now_ts=TS,
        )
    assert exc.value.refusal.code == "too_large"


def test_a_headless_box_refuses_the_picture_and_offers_the_transcript(
    con: sqlite3.Connection, redactor: Redactor
) -> None:
    with pytest.raises(CaptureRefused) as exc:
        capture(
            con,
            redactor=redactor,
            subject="pane",
            window=Window(title="claude code"),
            policy=CapturePolicy(text_first=False),
            now_ts=TS,
        )
    assert exc.value.refusal.code == "no_display"
    assert "transcript" in exc.value.refusal.remedy


# ───────────────────────────── the ledger ─────────────────────────────


def test_every_capture_lands_in_the_effects_ledger(
    con: sqlite3.Connection, redactor: Redactor
) -> None:
    art = capture(con, redactor=redactor, text=terminal("ok"), job_id=JOB, now_ts=TS)
    effect = get_effect(con, art.effect_id or "")
    assert effect is not None
    assert effect.kind == "capture.transcript"
    assert effect.job_id == JOB
    assert effect.provider_ref is not None
    assert effect.provider_ref["sha256"] == art.sha256


def test_a_capture_is_compensatable_at_best_and_promises_no_undo(
    con: sqlite3.Connection, redactor: Redactor
) -> None:
    art = capture_pixels(
        con,
        capturer=SyntheticCapturer(lines=["BUILD OK"]),
        redactor=redactor,
        subject="pane",
        window=Window(title="claude code"),
        now_ts=TS,
    )
    effect = get_effect(con, art.effect_id or "")
    assert effect is not None
    assert effect.reversibility == "compensatable"
    # No plan: unsending is the CHANNEL's compensation on the channel's window,
    # and a plan here would be this package promising something it cannot do.
    assert effect.undo_plan is None
    assert effect.confirm_strength == "confirm"

    spoken = spoken_effect_line(effect)
    assert "nothing I can act on" in spoken
    assert not any(w in spoken.lower() for w in PROMISE_WORDS)


def test_the_ledger_row_is_redacted_too(con: sqlite3.Connection, redactor: Redactor) -> None:
    # The summary names what was captured, and what was captured may be a
    # window whose title is a path whose name is a secret.
    art = capture_pixels(
        con,
        capturer=SyntheticCapturer(lines=["OK"]),
        redactor=redactor,
        subject="pane",
        window=Window(title=f"terminal {SECRET}"),
        now_ts=TS,
    )
    effect = get_effect(con, art.effect_id or "")
    assert effect is not None
    assert SECRET not in effect.summary
    assert "[redacted]" in effect.summary


def test_the_activity_log_is_told_without_being_told_the_secret(
    con: sqlite3.Connection, redactor: Redactor
) -> None:
    capture(con, redactor=redactor, text=terminal(f"KEY={SECRET}"), job_id=JOB, now_ts=TS)
    kinds = [e.kind for e in read_since(con, 0)]
    assert "effect.recorded" in kinds
    assert SECRET not in "".join(str(e.payload) for e in read_since(con, 0))


def test_record_can_be_declined_for_a_dry_run(con: sqlite3.Connection, redactor: Redactor) -> None:
    art = capture(con, redactor=redactor, text=terminal("ok"), record=False, now_ts=TS)
    assert art.effect_id is None
    assert recent_effects(con) == []


def test_two_processes_capturing_at_once_get_two_rows(db_path: Path, redactor: Redactor) -> None:
    # The effects table is append-only and captures do not race for anything,
    # so the property worth pinning is that neither connection swallows the
    # other's row — two real connections, one file, as the house rule requires.
    a = connect(db_path)
    b = connect(db_path)
    try:
        first = capture(a, redactor=redactor, text=terminal("from a"), job_id=JOB, now_ts=TS)
        second = capture(b, redactor=redactor, text=terminal("from b"), job_id=JOB, now_ts=TS)
        assert first.effect_id != second.effect_id
        seen = {e.id for e in recent_effects(a)}
        assert {first.effect_id, second.effect_id} <= seen
    finally:
        a.close()
        b.close()


# ───────────────────────────── the contract ─────────────────────────────


def test_capture_does_not_know_where_the_picture_is_going() -> None:
    # The phone leg and the HUD deliver the same bytes. An import of a channel
    # here would make this package a Telegram detail, and stage 6 would find out
    # the hard way.
    src = Path(__file__).parent.parent / "jarvis" / "capture"
    for path in sorted(src.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert "telegram" not in name, f"{path.name} imports {name}"
                assert "phone" not in name, f"{path.name} imports {name}"


def test_a_filename_may_not_carry_a_path(con: sqlite3.Connection, redactor: Redactor) -> None:
    art = capture(con, redactor=redactor, text=terminal("ok"), now_ts=TS)
    assert "/" not in art.filename
    with pytest.raises(ValueError, match="bare"):
        Artifact(
            data=b"x",
            filename="../../etc/passwd",
            media="text",
            subject="transcript",
            caption="",
            redaction=art.redaction,
        )


def test_capture_imports_with_no_third_party_packages() -> None:
    """Like the spine: this runs in whatever process wants a picture."""
    src = Path(__file__).parent.parent
    r = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            f"import sys; sys.path.insert(0, {str(src)!r}); import jarvis.capture; print('ok')",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONNOUSERSITE": "1", "PYTHONPATH": ""},
    )
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout


# ───────────────── regressions found in review ─────────────────


@pytest.mark.parametrize(
    "boxes",
    [
        [Box(0, 0, 0, 0)],
        [Box(0, 0, 240, 0)],
        [Box(9999, 9999, 40, 40)],
    ],
)
def test_a_box_that_covers_nothing_does_not_lift_the_secret_refusal(
    con: sqlite3.Connection, redactor: Redactor, boxes: list[Box]
) -> None:
    # `not boxes` was the original gate, so a zero-area box — or one whose
    # coordinates fell off the captured frame — bought a picture with the
    # credential still legible in it. A box that covers no pixel is not a
    # redaction, and neither arithmetic nor the picture may say otherwise.
    with pytest.raises(CaptureRefused) as exc:
        capture_pixels(
            con,
            capturer=SyntheticCapturer(lines=["KEY"]),
            redactor=redactor,
            subject="pane",
            window=Window(title="claude code"),
            visible_text=f"export KEY={SECRET}",
            boxes=boxes,
            now_ts=TS,
        )
    assert exc.value.refusal.code == "secret_visible"


def test_a_png_with_a_malformed_header_refuses_rather_than_crashing() -> None:
    # struct.error is NOT a ValueError, so a 5-byte IHDR used to escape every
    # `except ValueError` between the decoder and the caller and surface as an
    # untyped crash instead of the `undecodable` refusal the design promises.
    def chunk(kind: bytes, body: bytes) -> bytes:
        import zlib

        return (
            len(body).to_bytes(4, "big") + kind + body + zlib.crc32(kind + body).to_bytes(4, "big")
        )

    bad = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", b"\x00" * 5) + chunk(b"IEND", b"")
    cap = CommandCapturer(CAPTURE_TOOLS[1], run=lambda argv, out: out.write_bytes(bad))
    with pytest.raises(CaptureRefused) as exc:
        cap.grab("screen")
    assert exc.value.refusal.code == "undecodable"


def test_an_empty_transcript_is_named_as_the_callers_bug(
    con: sqlite3.Connection, redactor: Redactor
) -> None:
    with pytest.raises(ValueError, match="no transcript to send"):
        capture_transcript(con, text="", redactor=redactor, now_ts=TS)


def test_a_timestamp_without_a_fraction_still_makes_one_filename(
    con: sqlite3.Connection, redactor: Redactor
) -> None:
    art = capture_transcript(
        con, text="ok", redactor=redactor, now_ts="2026-09-16T12:00:00Z", record=False
    )
    assert art.filename == "jarvis-transcript-20260916T120000Z.txt"


@pytest.mark.parametrize("name", ["..", ".", "a\rb.txt", "a\nb.txt", 'a"b.txt', "a\x00b.txt"])
def test_a_filename_that_could_forge_a_header_is_rejected(
    con: sqlite3.Connection, redactor: Redactor, name: str
) -> None:
    # This name ends up in a multipart `Content-Disposition: … filename="…"`,
    # where a CR, an LF or a quote is header injection, and in a "save as" on
    # the receiving device, where "." and ".." are a path. Validated here so no
    # channel has to remember to.
    art = capture_transcript(con, text="ok", redactor=redactor, now_ts=TS, record=False)
    with pytest.raises(ValueError, match="bare"):
        Artifact(
            data=b"x",
            filename=name,
            media="text",
            subject="transcript",
            caption="",
            redaction=art.redaction,
        )
