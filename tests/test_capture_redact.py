"""Redaction, tested from the attacker's side rather than the author's.

The question these ask is never "did the function run" but "is the secret gone,
and can that be shown from the bytes that would have left the machine". For text
that means searching the output for the literal. For pixels it means reading the
colour back and asserting the region is a single opaque colour — because a blur
would leave a spread of colours that still encodes the glyphs underneath.
"""

from __future__ import annotations

import pytest

from jarvis.bus import REDACTED, Redactor
from jarvis.capture.png import Box, Canvas, RawImage
from jarvis.capture.redact import (
    REDACTION_FILL,
    Finding,
    banner,
    redact_pixels,
    redact_text,
    scan_shapes,
    surviving_secrets,
)

SECRET = "sk-ant-api03-REALKEYMATERIAL999"


def test_the_literal_is_gone_and_provably_so() -> None:
    r = Redactor.of([SECRET])
    out, report = redact_text(f"before {SECRET} after", redactor=r)
    assert SECRET not in out
    assert surviving_secrets(out, r) == ()
    assert report.exact is True
    assert report.literals_removed == 1


def test_a_short_secret_inside_a_long_one_leaves_no_tail() -> None:
    # The failure this ordering prevents: replacing "abcd" first turns
    # "abcdefgh" into "[redacted]efgh", which still identifies the long secret.
    short, long = "abcd1234", "abcd1234efgh5678"
    r = Redactor.of([short, long])
    out, report = redact_text(f"x {long} y", redactor=r)
    assert out == f"x {REDACTED} y"
    assert report.literals_removed == 1


def test_every_occurrence_is_counted_not_just_the_first() -> None:
    r = Redactor.of([SECRET])
    _, report = redact_text(f"{SECRET} {SECRET} {SECRET}", redactor=r)
    assert report.literals_removed == 3


def test_nothing_to_do_is_reported_as_nothing_to_do() -> None:
    out, report = redact_text("a clean build log", redactor=Redactor.of([SECRET]))
    assert out == "a clean build log"
    assert report.method == "none"
    assert report.anything_removed is False


@pytest.mark.parametrize(
    ("sample", "name"),
    [
        ("sk-ant-api03-" + "A" * 30, "anthropic_key"),
        ("ghp_" + "b" * 36, "github_token"),
        ("github_pat_" + "c" * 40, "github_pat"),
        ("AIza" + "D" * 35, "google_key"),
        ("AKIAIOSFODNN7EXAMPLE", "aws_key_id"),
        ("xoxb-12345678901-abcdefghij", "slack_token"),
        ("1234567890:" + "E" * 35, "telegram_bot_token"),
        ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NX0.dBjftJeZ4CVPmB92K", "jwt"),
        ("-----BEGIN OPENSSH PRIVATE KEY-----", "private_key_block"),
    ],
)
def test_the_credentials_nobody_put_in_the_keyring(sample: str, name: str) -> None:
    # A terminal shows the user's OTHER tokens too: a colleague's, a CI
    # runner's, one pasted from a ticket. None are in the Redactor.
    out, report = redact_text(f"$ echo {sample}", redactor=Redactor.of([]))
    assert sample not in out
    assert name in report.shapes_removed


@pytest.mark.parametrize(
    "innocent",
    [
        "commit 4f9a1c2b4d5e6f708192a3b4c5d6e7f8091a2b3c",
        "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
        "id 3f9a1c2b4d5e",
        "req_3f9a1c2b4d5e answered by desk",
    ],
)
def test_the_net_does_not_catch_the_things_it_would_be_turned_off_for(innocent: str) -> None:
    out, _ = redact_text(innocent, redactor=Redactor.of([]))
    assert out == innocent


def test_an_env_dump_keeps_the_name_and_loses_the_value() -> None:
    # Which credential was on screen is the one thing a person needs afterwards.
    out, report = redact_text("ANTHROPIC_API_KEY=hunter2secretvalue", redactor=Redactor.of([]))
    assert out.startswith("ANTHROPIC_API_KEY=")
    assert "hunter2secretvalue" not in out
    assert report.shapes_removed == ("env_assignment",)


def test_the_placeholder_is_not_redacted_a_second_time() -> None:
    r = Redactor.of([SECRET])
    out, report = redact_text(f"MY_API_KEY={SECRET}", redactor=r)
    assert out == f"MY_API_KEY={REDACTED}"
    assert report.literals_removed == 1
    assert report.shapes_removed == ()


def test_findings_never_carry_the_value() -> None:
    # A finding ends up in a log line; an excerpt would leak what it removed.
    found = scan_shapes("AKIAIOSFODNN7EXAMPLE")
    assert found == (Finding("aws_key_id", 0, 20),)
    assert "EXAMPLE" not in repr(found[0])


def test_shape_masking_can_be_switched_off_and_says_so() -> None:
    out, report = redact_text("ghp_" + "z" * 36, redactor=Redactor.of([]), mask_shapes=False)
    assert out.startswith("ghp_")
    assert "switched off" in report.residual_risk


def test_overlapping_shapes_collapse_to_one_mask() -> None:
    out, _ = redact_text("GITHUB_TOKEN=ghp_" + "y" * 36, redactor=Redactor.of([]))
    assert out.count("[redacted:") == 1


# ───────────────────────────── pixels ─────────────────────────────


def noisy(width: int = 40, height: int = 20) -> RawImage:
    canvas = Canvas.blank(width, height, (10, 10, 10))
    canvas.text(1, 1, "AKIA 1234", (240, 240, 240), scale=1)
    return canvas.freeze()


def test_a_box_replaces_the_pixels_rather_than_attenuating_them() -> None:
    before = noisy()
    after, report = redact_pixels(before, (Box(0, 0, 20, 10),))
    covered = {after.pixel(x, y) for x in range(20) for y in range(10)}
    # ONE colour. A blur would leave a spread here, and that spread is what
    # still encodes the glyphs.
    assert covered == {REDACTION_FILL}
    assert report.pixels_filled == 200
    assert after.pixel(30, 15) == before.pixel(30, 15)


def test_pixel_redaction_is_never_called_exact() -> None:
    # There is no OCR here, so "clean" is not a claim this path may make — not
    # even when the caller named a region and the region was filled.
    _, report = redact_pixels(noisy(), (Box(0, 0, 40, 20),))
    assert report.exact is False
    assert "left the machine as it was" in report.residual_risk


def test_naming_no_region_redacts_nothing_and_admits_it() -> None:
    before = noisy()
    after, report = redact_pixels(before, ())
    assert after.pixels == before.pixels
    assert report.exact is False
    assert report.method == "none"


def test_the_warning_is_burned_into_the_picture() -> None:
    before = Canvas.blank(240, 80, (10, 10, 10)).freeze()
    after = banner(before, ("NOT TEXT-REDACTED",))
    assert after.pixels != before.pixels
    # A caption can be cropped out of a forward; the top-left pixel cannot.
    assert after.pixel(0, 0) == REDACTION_FILL


def test_a_picture_too_small_for_the_band_still_gets_delivered() -> None:
    tiny = Canvas.blank(12, 8, (10, 10, 10)).freeze()
    assert banner(tiny, ("NOT TEXT-REDACTED",)).pixels == tiny.pixels


def test_a_hand_built_redactor_cannot_leave_a_recognisable_tail() -> None:
    # `Redactor.of` sorts longest-first; `Redactor(secrets=(...))` does not, and
    # nothing stops a caller from building one. Replacing the short secret first
    # would leave "SUPERSECRET" in the text AND hide it from surviving_secrets,
    # because the long literal is no longer there to be found.
    short = "abc123def456"
    long = short + "SUPERSECRET"
    r = Redactor(secrets=(short, long))
    out, report = redact_text(f"token={long}", redactor=r)
    assert "SUPERSECRET" not in out
    assert out == f"token={r.placeholder}"
    assert report.literals_removed == 1
