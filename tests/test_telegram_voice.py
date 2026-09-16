"""Voice notes, and the transcription seam deliberately left open.

The assertion that matters is a negative one: saving a voice note must not
require a speech model, because this process runs on machines that have none.
What it produces is a file and a log line, and the transcriber is somebody else's
problem — named, typed, and callable, but not imported.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis.db import connect, migrate
from jarvis.telegram import voice
from jarvis.telegram.transport import FakeTransport

OGG = b"OggS\x00\x02the operator's actual voice"


@pytest.fixture
def con() -> Iterator[sqlite3.Connection]:
    c = connect(":memory:")
    migrate(c)
    yield c
    c.close()


def _message(file_id: str = "AwACAgQ") -> dict:
    return {
        "message_id": 88,
        "chat": {"id": 4242},
        "voice": {"file_id": file_id, "duration": 4, "mime_type": "audio/ogg"},
    }


def _transport(file_id: str = "AwACAgQ") -> FakeTransport:
    return FakeTransport(files={f"voice/{file_id}.oga": OGG})


def test_an_inbound_note_becomes_a_file_the_desk_can_pick_up_later(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    note = voice.save_voice_note(con, _transport(), _message(), dest_dir=tmp_path)
    assert note.path.read_bytes() == OGG
    assert note.duration_s == 4
    assert note.transcript is None, "transcription is somebody else's process"


def test_the_original_bytes_are_stored_rather_than_something_we_guessed_at(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    note = voice.save_voice_note(con, _transport(), _message(), dest_dir=tmp_path)
    assert note.path.suffix == ".oga"
    assert note.mime_type == "audio/ogg"


def test_where_it_landed_is_in_the_activity_log(con: sqlite3.Connection, tmp_path: Path) -> None:
    note = voice.save_voice_note(con, _transport(), _message(), dest_dir=tmp_path)
    row = con.execute("SELECT payload FROM events WHERE kind='telegram.voice_received'").fetchone()
    payload = json.loads(str(row["payload"]))
    assert payload["path"] == str(note.path)
    assert payload["transcript"] is None


def test_saving_the_same_note_twice_logs_it_once(con: sqlite3.Connection, tmp_path: Path) -> None:
    voice.save_voice_note(con, _transport(), _message(), dest_dir=tmp_path)
    voice.save_voice_note(con, _transport(), _message(), dest_dir=tmp_path)
    n = con.execute(
        "SELECT COUNT(*) AS n FROM events WHERE kind='telegram.voice_received'"
    ).fetchone()["n"]
    assert n == 1


def test_a_message_with_no_voice_note_is_refused(con: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="no voice note"):
        voice.save_voice_note(con, FakeTransport(), {"message_id": 1, "text": "hi"})


def test_the_transcriber_is_a_protocol_and_nothing_here_implements_it(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    """The seam: file in, words out, and no dependency added to this process."""

    class Fake:
        def transcribe(self, path: Path, *, mime_type: str) -> str:
            assert path.exists() and mime_type == "audio/ogg"
            return "run the tests again"

    note = voice.save_voice_note(con, _transport(), _message(), dest_dir=tmp_path)
    filled = voice.attach_transcript(con, note, Fake())
    assert filled.transcript == "run the tests again"
    assert note.transcript is None, "the note is frozen; attach returns a copy"
    row = con.execute(
        "SELECT payload FROM events WHERE kind='telegram.voice_transcribed'"
    ).fetchone()
    assert json.loads(str(row["payload"]))["transcript"] == "run the tests again"


def test_an_outbound_note_is_sent_as_multipart_with_its_caption(
    con: sqlite3.Connection,
) -> None:
    t = FakeTransport()
    message_id = voice.send_voice(con, t, 4242, OGG, caption="the plan, read aloud")
    call = t.last("sendVoice")
    assert call is not None and call.upload is not None
    assert call.upload.content == OGG
    assert call.upload.field == "voice"
    # The caption is the record: a voice note cannot be skimmed on a train.
    assert call.params["caption"] == "the plan, read aloud"
    assert message_id > 0


def test_an_empty_voice_note_is_refused_before_it_is_sent(con: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="empty"):
        voice.send_voice(con, FakeTransport(), 1, b"")
