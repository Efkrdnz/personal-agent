"""A Presentation, rendered as a message and an inline keyboard. PURE.

This is the Telegram half of the fidelity guarantee, and it is the same
guarantee the desk reader gives out loud: the option labels are printed
LITERALLY and numbered from the frozen array, and what comes back is an INDEX.
Neither the question nor the answer round-trips through language, so on this
channel the guarantee costs nothing and is total.

Three decisions here are load-bearing.

*No ``parse_mode``, ever.* Telegram's Markdown and HTML modes would interpret
``*``, ``_``, ``[`` and ``<`` inside a label. A label containing ``a*b`` would
either render wrong or be rejected outright with ``400 can't parse entities``,
and a label that renders wrong is a label that is no longer verbatim. Plain text
is the only mode in which "printed literally" is true.

*``callback_data`` is capped at 64 BYTES by Telegram.* Not characters — bytes.
Going over is not an error a bot sees at a useful moment: the API rejects the
whole ``sendMessage``, and the obvious fix, truncating the payload to fit, is far
worse than the crash it prevents, because a truncated request id can still parse
as a VALID id belonging to a DIFFERENT question. So nothing here truncates: the
encoder raises, and the renderer refuses to build a keyboard it cannot address.

The field ORDER is part of that defence. The request id is the only
variable-length field and it goes LAST, so a payload that loses its tail loses
the id — and a short id matches no row, which fails closed. Putting the id first
and the option index last reads better and is wrong: ``…:p:12`` truncated to
``…:p:1`` is a well-formed instruction to approve option 1 on the very question
the user was looking at, which is the one mistake in here nobody would catch by
reading the log afterwards.

*Multi-select state lives in the rendered keyboard, not in a column.* One
callback per tap cannot express a set, so multi-select is toggle-then-confirm —
and the toggles' current state is read back out of the message's own keyboard,
which Telegram stores and hands back on every callback. That needs no new
column, survives a bot restart mid-selection, and cannot go stale relative to
what the user is looking at.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from jarvis.requests import Presentation

__all__ = [
    "CALLBACK_VERSION",
    "CHECKED",
    "MAX_BUTTON_CHARS",
    "MAX_CALLBACK_BYTES",
    "UNCHECKED",
    "Button",
    "Callback",
    "CallbackFormatError",
    "CallbackTooLong",
    "Message",
    "Verb",
    "decode_callback",
    "encode_callback",
    "free_text_hint",
    "numbered_body",
    "render_request",
    "selected_from_markup",
    "settled_message",
]

#: Telegram's documented hard limit on callback_data, in BYTES.
MAX_CALLBACK_BYTES = 64

#: Bumped if the payload grammar ever changes. An old message left in a chat
#: still has old buttons in it, and a v1 tap arriving at a v2 bot must be
#: recognised as stale rather than mis-parsed into a plausible request id.
CALLBACK_VERSION = "j2"

_SEP = ":"

#: Button text is a HANDLE, not the record: the literal label is printed in the
#: message body above it, so shortening the button cannot lose anything.
MAX_BUTTON_CHARS = 56

CHECKED = "☑"  # ☑
UNCHECKED = "☐"  # ☐

Verb = Literal["pick", "toggle", "confirm", "free"]

_VERB_CODES: dict[Verb, str] = {"pick": "p", "toggle": "t", "confirm": "c", "free": "f"}
_CODE_VERBS: dict[str, Verb] = {v: k for k, v in _VERB_CODES.items()}

FREE_TEXT_BUTTON = "None of these — reply with text"


class CallbackTooLong(ValueError):
    """The payload would exceed 64 bytes, so this keyboard cannot be addressed.

    A hard failure on purpose. The alternatives are a ``sendMessage`` the API
    rejects wholesale, or a truncated request id that parses as a different
    question and answers it — a silently wrong build approving a command nobody
    was shown.
    """


class CallbackFormatError(ValueError):
    """Callback data this bot did not write, or wrote under an older grammar."""


@dataclass(frozen=True, slots=True)
class Callback:
    """A decoded button press: which request, what kind of tap, which option."""

    request_id: str
    verb: Verb
    index: int


@dataclass(frozen=True, slots=True)
class Button:
    text: str
    callback_data: str

    def as_dict(self) -> dict[str, str]:
        return {"text": self.text, "callback_data": self.callback_data}


@dataclass(frozen=True, slots=True)
class Message:
    """What to send: plain text, and a keyboard that may be empty."""

    text: str
    rows: tuple[tuple[Button, ...], ...] = ()

    def markup(self) -> dict[str, Any]:
        """``reply_markup`` for the Bot API. An empty keyboard REMOVES buttons."""
        return {"inline_keyboard": [[b.as_dict() for b in row] for row in self.rows]}


# ───────────────────────────── callback payloads ─────────────────────────────


def encode_callback(request_id: str, verb: Verb, index: int = 0) -> str:
    """``j2:p:3:req_…``. Raises rather than truncating. Ever.

    The request id is written WHOLE and written LAST — see the module docstring
    on why the variable-length field is the one that must absorb a truncation.
    Telegram counts bytes, so the check is over the encoded form and not over
    ``len()``: a non-ASCII id would pass a character count and be rejected by the
    API.
    """
    if verb not in _VERB_CODES:
        raise ValueError(f"unknown callback verb {verb!r}")
    if not request_id or _SEP in request_id:
        raise ValueError(f"request id {request_id!r} cannot carry the separator {_SEP!r}")
    if index < 0:
        raise ValueError("option index is 1-based; 0 means 'no option'")
    data = f"{CALLBACK_VERSION}{_SEP}{_VERB_CODES[verb]}{_SEP}{index}{_SEP}{request_id}"
    size = len(data.encode("utf-8"))
    if size > MAX_CALLBACK_BYTES:
        raise CallbackTooLong(
            f"callback_data is {size} bytes, over Telegram's {MAX_CALLBACK_BYTES}; "
            f"request id {request_id!r} is too long to address from a button"
        )
    return data


def decode_callback(data: str) -> Callback:
    """The inverse of :func:`encode_callback`. Raises on anything else.

    Strict about the field COUNT as well as the version, because that is what
    makes a mangled payload fail loudly: a four-field grammar cannot lose its
    tail and still look like a complete instruction. What a truncation CAN do is
    shorten the trailing request id, and a shortened id names no row.
    """
    if not isinstance(data, str):
        raise CallbackFormatError("callback data must be a string")
    parts = data.split(_SEP)
    if len(parts) != 4:
        raise CallbackFormatError(f"expected 4 fields, got {len(parts)}: {data!r}")
    version, code, raw_index, request_id = parts
    if version != CALLBACK_VERSION:
        raise CallbackFormatError(f"callback grammar {version!r} is not {CALLBACK_VERSION!r}")
    if code not in _CODE_VERBS:
        raise CallbackFormatError(f"unknown verb {code!r}")
    if not request_id:
        raise CallbackFormatError("callback names no request")
    if not raw_index.isdigit():
        raise CallbackFormatError(f"option index {raw_index!r} is not a number")
    return Callback(request_id=request_id, verb=_CODE_VERBS[code], index=int(raw_index))


# ───────────────────────────── the message body ─────────────────────────────


def numbered_body(pres: Presentation) -> str:
    """The intro and the options, numbered exactly as the desk reads them aloud.

    ``f"{index}. {label}"`` with nothing between the ordinal and the label is the
    same adjacency ``jarvis.voice.script`` speaks, so the two channels' renderings
    of one question can be compared byte for byte.
    """
    lines: list[str] = []
    intro = str(pres.get("intro") or "").strip()
    if intro:
        lines.append(intro)
    items = pres.get("items") or []
    if items:
        lines.append("")
        for item in items:
            lines.append(f"{item['index']}. {item['label']}")
            description = str(item.get("description") or "").strip()
            if description:
                lines.append(f"    {description}")
    return "\n".join(lines)


def free_text_hint(pres: Presentation) -> str:
    prompt = str(pres.get("free_text_prompt") or "").strip()
    return prompt or "Or reply to this message with your own answer."


def render_request(
    pres: Presentation,
    request_id: str,
    *,
    require_confirm: bool = False,
    selected: tuple[int, ...] = (),
) -> Message:
    """Presentation in, one Telegram message plus its keyboard out.

    ``require_confirm`` forces the toggle-then-confirm shape on a presentation
    whose ``multi`` flag is False. The caller needs it because a single
    ``AskUserQuestion`` call may carry several questions whose options are
    numbered across the whole batch: one tap then cannot complete the answer, and
    a keyboard that looks like it can is a keyboard that lies.
    """
    items = pres.get("items") or []
    body = numbered_body(pres)
    toggles = bool(pres.get("multi")) or require_confirm
    allows_free_text = pres.get("allows_free_text", True)

    if allows_free_text:
        body = f"{body}\n\n{free_text_hint(pres)}" if body else free_text_hint(pres)

    rows: list[tuple[Button, ...]] = []
    for item in items:
        index = int(item["index"])
        label = str(item["label"])
        if toggles:
            mark = CHECKED if index in selected else UNCHECKED
            rows.append(
                (
                    Button(
                        f"{mark} {_button_text(index, label)}",
                        encode_callback(request_id, "toggle", index),
                    ),
                )
            )
        else:
            rows.append(
                (Button(_button_text(index, label), encode_callback(request_id, "pick", index)),)
            )
    if toggles and items:
        chosen = f" ({len(selected)})" if selected else ""
        send = Button(f"Send these answers{chosen}", encode_callback(request_id, "confirm"))
        rows.append((send,))
    if allows_free_text:
        rows.append((Button(FREE_TEXT_BUTTON, encode_callback(request_id, "free")),))
    return Message(text=body, rows=tuple(rows))


def settled_message(pres: Presentation, note: str) -> Message:
    """The same question with its buttons GONE and a line saying what happened.

    Editing rather than deleting: the question the user was asked stays visible
    in their chat history, which is the whole reason a text channel is worth
    having next to a spoken one.
    """
    body = numbered_body(pres)
    return Message(text=f"{body}\n\n{note}" if body else note, rows=())


def selected_from_markup(markup: Any) -> tuple[int, ...]:
    """Read the ticked options back out of a keyboard Telegram handed us.

    This is where multi-select state lives. Telegram echoes the message's own
    ``reply_markup`` on every callback, so the set the user has built is already
    durable, already shared between devices, and already exactly what they are
    looking at. A ``selected`` column would only be able to disagree with it.

    Unknown or foreign buttons are ignored rather than refused: a keyboard from
    an older grammar still has to be readable enough to say "that question is
    gone".
    """
    if not isinstance(markup, dict):
        return ()
    found: list[int] = []
    for row in markup.get("inline_keyboard") or []:
        if not isinstance(row, list):
            continue
        for button in row:
            if not isinstance(button, dict):
                continue
            try:
                cb = decode_callback(str(button.get("callback_data") or ""))
            except CallbackFormatError:
                continue
            if cb.verb == "toggle" and str(button.get("text") or "").startswith(CHECKED):
                found.append(cb.index)
    return tuple(sorted(set(found)))


def _button_text(index: int, label: str) -> str:
    text = f"{index}. {label}"
    if len(text) <= MAX_BUTTON_CHARS:
        return text
    return text[: MAX_BUTTON_CHARS - 1] + "…"
