"""Rendering, and the 64-byte cliff at the edge of it.

The rendering tests exist for one reason: the desk SPEAKS the same frozen array
this module PRINTS, and the answer key is the index in both. So the numbering is
compared against ``jarvis.cc.narrate.script`` byte for byte rather than merely
looking right.

The callback tests exist for a different one. Telegram caps ``callback_data`` at
64 BYTES, and the obvious fix for going over — truncate to fit — can turn one
request id into another request id that also exists. That is a tap on a
permission prompt approving something the user never saw, so the boundary is
asserted from both sides.
"""

from __future__ import annotations

import pytest

from jarvis.cc import narrate
from jarvis.requests import make_presentation
from jarvis.telegram import render

QUESTIONS = [
    {
        "question": "Which database?",
        "options": [
            {"label": "SQLite", "description": "one file"},
            {"label": "Postgres", "description": "a server"},
        ],
    }
]

RID = "req_3f9a1c2b4d5e"


def _pres(**kwargs: object) -> dict:
    base = dict(
        intro="Which database?",
        options=["SQLite", "Postgres"],
        free_text_prompt="Or say your own answer.",
    )
    base.update(kwargs)
    return make_presentation(**base)  # type: ignore[arg-type]


# ───────────────────────────── the numbering ─────────────────────────────


def test_the_printed_options_match_what_the_desk_says_out_loud() -> None:
    spoken = [line.text for line in narrate.script(QUESTIONS) if line.kind == "option"]
    printed = render.numbered_body(narrate.presentation(QUESTIONS)).splitlines()
    for text in spoken:
        assert text in printed, f"{text!r} is spoken but not printed"


def test_a_label_with_markdown_in_it_is_printed_literally() -> None:
    """No parse_mode: *this* must stay four characters, not become italics."""
    pres = _pres(options=["*not bold*", "_not italic_"])
    body = render.numbered_body(pres)
    assert "1. *not bold*" in body
    assert "2. _not italic_" in body


def test_every_question_offers_none_of_these() -> None:
    message = render.render_request(_pres(), RID)
    labels = [b.text for row in message.rows for b in row]
    assert render.FREE_TEXT_BUTTON in labels
    assert "Or say your own answer." in message.text


def test_single_select_is_one_tap_and_multi_select_is_toggle_then_confirm() -> None:
    single = render.render_request(_pres(), RID)
    verbs = {render.decode_callback(b.callback_data).verb for row in single.rows for b in row}
    assert verbs == {"pick", "free"}

    multi = render.render_request(_pres(multi=True), RID)
    verbs = {render.decode_callback(b.callback_data).verb for row in multi.rows for b in row}
    # One callback per tap cannot express a set, so a set needs a confirm.
    assert verbs == {"toggle", "confirm", "free"}


def test_a_ticked_selection_is_readable_back_out_of_the_keyboard() -> None:
    """Multi-select state lives in the keyboard Telegram already stores for us."""
    drawn = render.render_request(_pres(multi=True), RID, selected=(2,))
    assert render.selected_from_markup(drawn.markup()) == (2,)
    again = render.render_request(_pres(multi=True), RID, selected=(1, 2))
    assert render.selected_from_markup(again.markup()) == (1, 2)


def test_foreign_buttons_in_a_keyboard_are_ignored_rather_than_refused() -> None:
    markup = {"inline_keyboard": [[{"text": "x", "callback_data": "something else"}]]}
    assert render.selected_from_markup(markup) == ()
    assert render.selected_from_markup(None) == ()


def test_settling_a_message_removes_every_button() -> None:
    settled = render.settled_message(_pres(), "Answered at the desk.")
    assert settled.rows == ()
    assert settled.markup() == {"inline_keyboard": []}
    assert "Answered at the desk." in settled.text


def test_a_long_label_is_shortened_in_the_button_but_not_in_the_message() -> None:
    long_label = "Use the managed Postgres instance in Frankfurt with daily snapshots"
    message = render.render_request(_pres(options=[long_label]), RID)
    assert long_label in message.text
    assert len(message.rows[0][0].text) <= render.MAX_BUTTON_CHARS


# ───────────────────────────── the 64-byte cliff ─────────────────────────────


def test_callback_data_round_trips() -> None:
    for verb in ("pick", "toggle", "confirm", "free"):
        data = render.encode_callback(RID, verb, 3)  # type: ignore[arg-type]
        assert render.decode_callback(data) == render.Callback(RID, verb, 3)  # type: ignore[arg-type]


def test_a_real_request_id_leaves_room_to_spare() -> None:
    data = render.encode_callback(RID, "pick", 40)
    assert len(data.encode("utf-8")) <= render.MAX_CALLBACK_BYTES


def test_the_boundary_is_measured_in_bytes_and_is_exact() -> None:
    fixed = len(f"{render.CALLBACK_VERSION}:p:1:".encode())
    just_fits = "r" * (render.MAX_CALLBACK_BYTES - fixed)
    data = render.encode_callback(just_fits, "pick", 1)
    assert len(data.encode("utf-8")) == render.MAX_CALLBACK_BYTES

    with pytest.raises(render.CallbackTooLong):
        render.encode_callback(just_fits + "r", "pick", 1)


def test_a_multibyte_id_is_measured_in_bytes_not_characters() -> None:
    """len() would pass this and the API would reject the whole sendMessage."""
    fixed = len(f"{render.CALLBACK_VERSION}:p:1:".encode())
    wide = "ş" * ((render.MAX_CALLBACK_BYTES - fixed) // 2 + 1)
    assert len(wide) < render.MAX_CALLBACK_BYTES
    with pytest.raises(render.CallbackTooLong):
        render.encode_callback(wide, "pick", 1)


def test_the_renderer_refuses_a_keyboard_it_cannot_address() -> None:
    """Better a loud failure here than a keyboard the API rejects wholesale."""
    huge = "req_" + "a" * 80
    with pytest.raises(render.CallbackTooLong):
        render.render_request(_pres(), huge)


def test_a_truncated_payload_never_decodes_into_a_different_instruction() -> None:
    """Every index shape, not just the one-digit one.

    An earlier version of this test used index 1 and asserted only on the request
    id, so it could not fail: 1 has no shorter valid form, and the id sat in the
    middle of the payload where truncation never reached it. With index 12 the old
    grammar truncated to a well-formed "approve option 1 on this same question".
    """
    for index in (1, 9, 12, 40, 100):
        data = render.encode_callback(RID, "pick", index)
        for cut in range(1, len(data)):
            try:
                decoded = render.decode_callback(data[:cut])
            except render.CallbackFormatError:
                continue
            # If it parsed at all, the VERB and the OPTION must be intact: a
            # truncation may not change which choice is being made.
            assert (decoded.verb, decoded.index) == ("pick", index), (
                f"{data[:cut]!r} decoded into a different instruction: {decoded}"
            )
            # All a truncation may damage is the trailing id, and what it leaves
            # is a strict prefix — shorter than any id nid() mints, so it names
            # no row and get_request fails closed on it.
            assert RID.startswith(decoded.request_id)
            assert decoded.request_id == RID or len(decoded.request_id) < len(RID)


def test_an_older_grammar_is_recognised_as_stale_rather_than_misread() -> None:
    with pytest.raises(render.CallbackFormatError):
        render.decode_callback(f"j1:p:1:{RID}")
    with pytest.raises(render.CallbackFormatError):
        render.decode_callback(f"{render.CALLBACK_VERSION}:p:1")
    with pytest.raises(render.CallbackFormatError):
        render.decode_callback(f"{render.CALLBACK_VERSION}:z:1:{RID}")


def test_an_id_containing_the_separator_is_refused_at_the_door() -> None:
    with pytest.raises(ValueError, match="separator"):
        render.encode_callback("req:with:colons", "pick", 1)
