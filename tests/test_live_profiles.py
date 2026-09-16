"""What a leg is, as data — and the two claims in it that are not proven.

The interesting tests here are not "the dataclass holds values". They are: the
mime tag always carries the rate, the third-party tool surface cannot be
widened, the refuted language knob is not set in the config we actually build,
and importing this package does not drag in the SDK.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from jarvis.live import INPUT_RATE, MODEL, OUTPUT_RATE, pcm_mime
from jarvis.live.profiles import (
    AGENT_CALL,
    AGENT_CALL_TOOLS,
    DESK,
    PHONE_SERVER_VAD_FALLBACK,
    PHONE_USER,
    SessionProfile,
    ToolSurfaceFrozen,
    VadTuning,
    live_connect_config,
    profile,
)


def test_the_model_is_the_current_one_not_the_build_sheets() -> None:
    assert MODEL == "gemini-3.8-live"
    assert all(p.model == MODEL for p in (DESK, PHONE_USER, AGENT_CALL))


def test_the_mime_tag_always_carries_the_rate() -> None:
    # A bare "audio/pcm" is the chipmunk bug the moment a second rate exists,
    # and the phone leg is that second rate.
    assert DESK.input_mime == "audio/pcm;rate=16000"
    assert AGENT_CALL.input_mime == "audio/pcm;rate=16000"
    assert pcm_mime(8_000) == "audio/pcm;rate=8000"
    with pytest.raises(ValueError, match="positive"):
        pcm_mime(0)


def test_a_wrong_rate_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="16000"):
        SessionProfile(name="bad", input_rate=8_000)
    with pytest.raises(ValueError, match="24000"):
        SessionProfile(name="bad", output_rate=48_000)
    assert (DESK.input_rate, DESK.output_rate) == (INPUT_RATE, OUTPUT_RATE)


def test_both_shipped_legs_drive_their_own_turns() -> None:
    assert DESK.manual_vad and PHONE_USER.manual_vad and AGENT_CALL.manual_vad
    assert DESK.activity_handling == "START_OF_ACTIVITY_INTERRUPTS"


def test_the_documented_server_vad_fallback_carries_the_researched_numbers() -> None:
    assert PHONE_SERVER_VAD_FALLBACK.disabled is False
    assert PHONE_SERVER_VAD_FALLBACK.prefix_padding_ms == 300
    assert PHONE_SERVER_VAD_FALLBACK.silence_duration_ms == 800


def test_tuning_a_disabled_vad_is_a_lie_and_raises() -> None:
    with pytest.raises(ValueError, match="no effect"):
        VadTuning(disabled=True, silence_duration_ms=800)


def test_the_third_party_leg_has_exactly_two_tools_and_cannot_grow_one() -> None:
    assert AGENT_CALL.tools == AGENT_CALL_TOOLS == ("report_outcome", "end_call")
    assert len(AGENT_CALL.tools) == 2
    with pytest.raises(ToolSurfaceFrozen, match="improvise"):
        AGENT_CALL.with_tools("report_outcome", "end_call", "place_order")
    assert AGENT_CALL.allows("end_call")
    assert not AGENT_CALL.allows("code_build")


def test_an_open_profile_can_be_narrowed_without_touching_the_shipped_one() -> None:
    narrowed = DESK.with_tools("answer_question")
    assert narrowed.tools == ("answer_question",)
    assert DESK.tools != narrowed.tools  # frozen: the original is untouched


def test_turkish_says_out_loud_that_it_is_unverified() -> None:
    assert AGENT_CALL.language == "tr"
    assert AGENT_CALL.language_verified is False
    caveat = AGENT_CALL.language_caveat
    assert caveat is not None
    assert "REFUTED" in caveat and "probe_live" in caveat
    assert DESK.language_caveat is None
    assert SessionProfile(name="x", language="tr", language_verified=True).language_caveat is None


def test_the_turkish_instruction_is_actually_turkish() -> None:
    # The only mechanism left after language_code was refuted. If this string
    # stops being Turkish, the leg silently starts speaking English.
    assert "SADECE TÜRKÇE" in AGENT_CALL.system_instruction
    assert "report_outcome" in AGENT_CALL.system_instruction


def test_duplicate_tools_and_empty_names_are_refused() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        SessionProfile(name="x", tools=("a", "a"))
    with pytest.raises(ValueError, match="non-empty"):
        SessionProfile(name="x", tools=("a", " "))


def test_lookup_by_name_and_alias() -> None:
    assert profile("desk") is DESK
    assert profile("phone") is PHONE_USER
    assert profile("phone_user") is PHONE_USER
    assert profile("agent_call") is AGENT_CALL
    assert profile("phone_tr_third_party") is AGENT_CALL
    with pytest.raises(KeyError, match="unknown profile"):
        profile("desk_v2")


def test_changing_voice_produces_a_new_profile_not_a_mutation() -> None:
    other = DESK.with_voice("Puck")
    assert other.voice == "Puck"
    assert DESK.voice == "Zephyr"
    assert other.name == DESK.name


# ───────────────────────── the config we actually send ─────────────────────────


def test_the_config_sets_resumption_and_compression_on_every_connection() -> None:
    cfg = live_connect_config(DESK, handle="h-123")
    assert cfg.session_resumption is not None
    assert cfg.session_resumption.handle == "h-123"
    assert cfg.context_window_compression is not None
    assert cfg.context_window_compression.sliding_window is not None
    assert (
        cfg.context_window_compression.sliding_window.target_tokens
        < cfg.context_window_compression.trigger_tokens
    )


def test_the_refuted_language_knob_is_never_set() -> None:
    cfg = live_connect_config(AGENT_CALL)
    assert cfg.speech_config is not None
    # speech_config.language_code does NOT control output language on
    # native-audio models. Setting it would give Turkish a second, false
    # explanation, which is the one thing spike S4 must not have to untangle.
    assert cfg.speech_config.language_code is None
    assert cfg.speech_config.voice_config.prebuilt_voice_config.voice_name == "Charon"
    assert AGENT_CALL.system_instruction in str(cfg.system_instruction)


def test_server_vad_is_disabled_and_the_fallback_tuning_round_trips() -> None:
    cfg = live_connect_config(DESK)
    detection = cfg.realtime_input_config.automatic_activity_detection
    assert detection.disabled is True
    assert cfg.realtime_input_config.activity_handling == "START_OF_ACTIVITY_INTERRUPTS"

    tuned = SessionProfile(name="phone_fallback", vad=PHONE_SERVER_VAD_FALLBACK)
    detection = live_connect_config(tuned).realtime_input_config.automatic_activity_detection
    assert detection.disabled is False
    assert detection.prefix_padding_ms == 300
    assert detection.silence_duration_ms == 800


def test_declarations_reach_the_config_as_one_tool_block() -> None:
    cfg = live_connect_config(
        AGENT_CALL,
        declarations=[
            {"name": "report_outcome", "description": "", "parameters": {"type": "OBJECT"}},
            {"name": "end_call", "description": "", "parameters": {"type": "OBJECT"}},
        ],
    )
    assert cfg.tools is not None
    names = [d.name for d in cfg.tools[0].function_declarations]
    assert names == ["report_outcome", "end_call"]
    assert live_connect_config(AGENT_CALL).tools is None


def test_transcription_is_on_because_the_self_speech_veto_reads_it() -> None:
    cfg = live_connect_config(DESK)
    assert cfg.output_audio_transcription is not None
    assert cfg.input_audio_transcription is not None
    off = live_connect_config(SessionProfile(name="quiet", output_transcription=False))
    assert off.output_audio_transcription is None


def test_the_third_party_leg_does_not_transcribe_the_person_it_called() -> None:
    # A restaurant employee did not ask to be recorded. The only words of theirs
    # the system keeps are the ones report_outcome carries in `exact_words`;
    # Jarvis's own side stays on because the self-speech veto reads it.
    assert AGENT_CALL.input_transcription is False
    cfg = live_connect_config(AGENT_CALL)
    assert cfg.input_audio_transcription is None
    assert cfg.output_audio_transcription is not None


def test_importing_the_package_does_not_import_the_sdk() -> None:
    # The whole voice layer must import on a machine with no SDK, no key and no
    # sound card. A module-scope `from google import genai` would make the
    # import cost of `jarvis.live` a network-capable client object.
    code = (
        "import sys; import jarvis.live, jarvis.live.profiles, jarvis.live.session,"
        " jarvis.live.lease, jarvis.live.fake;"
        " print(any(m == 'google.genai' or m.startswith('google.genai.') for m in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False", out.stdout
