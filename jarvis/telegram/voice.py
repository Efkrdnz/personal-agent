"""Voice notes, both directions, and the transcription seam left OPEN.

Inbound: a voice note becomes a FILE plus a row in the activity log, and stops
there. It is deliberately not transcribed here. The models that can do it live
behind the ``voice`` extra in the desk process, and importing them into the
Telegram bot would mean this channel could not start on a machine with no audio
stack — which is most of the machines it will ever run on, including CI. So the
seam is a :class:`Transcriber` protocol and a file path: the desk (or anything
else that has an engine) implements the protocol, calls
:func:`attach_transcript`, and the words land on the bus where every channel can
already read them.

Outbound: ``sendVoice`` takes the bytes some other part of the system already
synthesised. This module does not synthesise anything; the architecture has one
cache and one router for that, and a second spelling of "say this" on the
Telegram side is how two channels start saying different words.

Thin on purpose. Everything below is file handling and one multipart upload.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from jarvis.bus import publish
from jarvis.db import default_path
from jarvis.ids import nid
from jarvis.telegram.transport import Transport, Upload

__all__ = [
    "OGG_MIME",
    "Transcriber",
    "VoiceNote",
    "attach_transcript",
    "save_voice_note",
    "send_voice",
    "voice_dir",
]

OGG_MIME = "audio/ogg"


def voice_dir(base: Path | None = None) -> Path:
    """Where inbound notes land: beside the database, never inside the repo.

    Same reasoning as :func:`jarvis.db.default_path` — this is the operator's
    own recorded voice, and it does not belong in a git tree or a synced folder.
    """
    root = base if base is not None else default_path().parent
    return root / "voice-notes"


class Transcriber(Protocol):
    """THE SEAM. Implemented elsewhere, by whoever owns a speech model.

    Deliberately file-in, text-out: it makes no assumption about sample rate,
    engine or language, and it can be satisfied by the desk process, by a batch
    job at 3am, or by a human typing what they meant.
    """

    def transcribe(self, path: Path, *, mime_type: str) -> str:
        """The words in this audio file."""


@dataclass(frozen=True, slots=True)
class VoiceNote:
    """One inbound note. ``transcript`` is None until something fills it in."""

    id: str
    path: Path
    file_id: str
    chat_id: int
    message_id: int
    duration_s: int = 0
    mime_type: str = OGG_MIME
    transcript: str | None = None


def save_voice_note(
    con: sqlite3.Connection,
    transport: Transport,
    message: dict[str, Any],
    *,
    dest_dir: Path | None = None,
    actor: str = "telegram",
) -> VoiceNote:
    """Download the note, write it to disk, and log where it went.

    The returned path is the handoff. Nothing is decoded, resampled or
    interpreted here — ogg/opus is what Telegram sent and ogg/opus is what gets
    stored, so the transcriber downstream sees the original bytes rather than
    something this module guessed at.
    """
    voice = message.get("voice") or message.get("audio") or {}
    file_id = str(voice.get("file_id") or "")
    if not file_id:
        raise ValueError("this message carries no voice note")

    meta = transport.call("getFile", {"file_id": file_id})
    file_path = str(meta["file_path"])
    content = transport.download(file_path)

    note_id = nid("vn")
    folder = dest_dir if dest_dir is not None else voice_dir()
    folder.mkdir(parents=True, exist_ok=True)
    suffix = Path(file_path).suffix or ".oga"
    target = folder / f"{note_id}{suffix}"
    target.write_bytes(content)

    note = VoiceNote(
        id=note_id,
        path=target,
        file_id=file_id,
        chat_id=int((message.get("chat") or {}).get("id") or 0),
        message_id=int(message.get("message_id") or 0),
        duration_s=int(voice.get("duration") or 0),
        mime_type=str(voice.get("mime_type") or OGG_MIME),
    )
    publish(
        con,
        "telegram.voice_received",
        actor,
        {
            "note_id": note.id,
            "path": str(note.path),
            "duration_s": note.duration_s,
            "mime_type": note.mime_type,
            "bytes": len(content),
            "transcript": None,
        },
        idem_key=f"tg:voice:{note.chat_id}:{note.message_id}",
        poke_peers=False,
    )
    return note


def attach_transcript(
    con: sqlite3.Connection,
    note: VoiceNote,
    transcriber: Transcriber,
    *,
    actor: str = "desk",
) -> VoiceNote:
    """Run a transcriber over a saved note and publish the words. Returns a copy.

    Separate from :func:`save_voice_note` because the two happen in different
    PROCESSES and often minutes apart: the bot saves, something with a model
    transcribes. The event is what joins them, which also means a failed
    transcription leaves the audio intact rather than losing the message.
    """
    text = transcriber.transcribe(note.path, mime_type=note.mime_type)
    publish(
        con,
        "telegram.voice_transcribed",
        actor,
        {"note_id": note.id, "path": str(note.path), "transcript": text},
        idem_key=f"tg:voice_text:{note.id}",
        poke_peers=False,
    )
    return replace(note, transcript=text)


def send_voice(
    con: sqlite3.Connection,
    transport: Transport,
    chat_id: int,
    audio: bytes,
    *,
    caption: str | None = None,
    filename: str = "jarvis.ogg",
    mime_type: str = OGG_MIME,
    actor: str = "telegram",
    reply_to_message_id: int | None = None,
) -> int:
    """``sendVoice`` with bytes somebody else synthesised. Returns the message id.

    A caption is sent alongside whenever there is one, because a voice note is
    the one thing in this chat that cannot be searched, skimmed on a train, or
    read back later — the text is not a fallback, it is the record.
    """
    if not audio:
        raise ValueError("refusing to send an empty voice note")
    params: dict[str, Any] = {"chat_id": chat_id}
    if caption:
        params["caption"] = caption
    if reply_to_message_id is not None:
        params["reply_to_message_id"] = reply_to_message_id
    result = transport.upload("sendVoice", params, Upload("voice", filename, audio, mime_type))
    message_id = int(result["message_id"])
    publish(
        con,
        "telegram.sent",
        actor,
        {"chat_id": chat_id, "message_id": message_id, "kind": "voice", "bytes": len(audio)},
        idem_key=f"tg:sent:voice:{chat_id}:{message_id}",
        poke_peers=False,
    )
    return message_id
