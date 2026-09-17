"""What a leg is, as data: model, voice, language, turn policy and tool surface.

A profile is the whole difference between the desk, the phone and the Turkish
restaurant call. It is frozen, it is passed to a session at construction, and
there is no way to mutate one in place — so "the phone call changed Jarvis's
voice" is not a thing that can happen to a running desk session, which is
exactly what it is in the reference build.

THE TOOL SURFACE IS PART OF THE PROFILE, DEFAULT-DENY. The third-party leg
carries exactly two tools and :attr:`SessionProfile.frozen_tools` refuses to
widen it, so "the model improvised a booking" is a structural impossibility
rather than a prompt instruction it might ignore on a bad day.

TURKISH IS UNVERIFIED AND SAYS SO. ``speech_config.language_code`` was REFUTED
for native-audio models — it does not control output language — so the only
mechanism left is a Turkish system instruction, and no primary source confirms
that it holds for a whole call. :attr:`SessionProfile.language_verified` is
``False`` on every shipped profile and :attr:`language_caveat` is the string
that says so out loud. ``tools/probe_live.py`` (spike S4) is what flips it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

from jarvis.live import (
    COMPRESSION_TARGET_TOKENS,
    COMPRESSION_TRIGGER_TOKENS,
    INPUT_RATE,
    MODEL,
    OUTPUT_RATE,
    pcm_mime,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, the SDK is never imported at module scope
    from google.genai import types

__all__ = [
    "AGENT_CALL",
    "AGENT_CALL_TOOLS",
    "DESK",
    "PHONE_SERVER_VAD_FALLBACK",
    "PHONE_USER",
    "PROFILES",
    "ActivityHandling",
    "SessionProfile",
    "ToolSurfaceFrozen",
    "VadTuning",
    "live_connect_config",
    "profile",
]

ActivityHandling = Literal["START_OF_ACTIVITY_INTERRUPTS", "NO_INTERRUPTION"]

#: The third-party leg's entire tool surface. Two entries, frozen. `report_outcome`
#: is how the call result gets into the system at all, and `end_call` is how it
#: stops; there is deliberately nothing here that can book, pay or promise.
AGENT_CALL_TOOLS: tuple[str, ...] = ("report_outcome", "end_call")


class ToolSurfaceFrozen(RuntimeError):
    """Someone tried to widen a tool surface that is frozen by design."""


@dataclass(frozen=True, slots=True)
class VadTuning:
    """Server-side voice activity detection, which both shipped legs turn OFF.

    Turns are client-driven on every leg: the desk needs explicit control of
    when a barge-in counts (duck on suspicion, then confirm), and server VAD
    tuned for a clean desk mic false-triggers on phone-line noise. The tuned
    values exist anyway as :data:`PHONE_SERVER_VAD_FALLBACK`, because "turn
    server VAD back on with these numbers" is the first thing to try if local
    VAD turns out to be the weak link, and the numbers came out of research
    rather than out of the air.
    """

    disabled: bool = True
    prefix_padding_ms: int | None = None
    silence_duration_ms: int | None = None
    start_sensitivity: str | None = None
    end_sensitivity: str | None = None

    def __post_init__(self) -> None:
        if self.disabled and any(
            v is not None
            for v in (
                self.prefix_padding_ms,
                self.silence_duration_ms,
                self.start_sensitivity,
                self.end_sensitivity,
            )
        ):
            # Tuning that the server will never read is a lie in the config file:
            # the next reader believes the padding is in force and it is not.
            raise ValueError("server VAD is disabled; tuning it here has no effect")
        for name in ("prefix_padding_ms", "silence_duration_ms"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise ValueError(f"{name} must be an integer number of milliseconds")


#: LOW/LOW with a long silence window, from the telephony research: server VAD
#: tuned for a clean microphone fires on line noise, and a carrier's comfort
#: noise is not silence.
PHONE_SERVER_VAD_FALLBACK = VadTuning(
    disabled=False,
    prefix_padding_ms=300,
    silence_duration_ms=800,
    start_sensitivity="START_SENSITIVITY_LOW",
    end_sensitivity="END_SENSITIVITY_LOW",
)


@dataclass(frozen=True, slots=True)
class SessionProfile:
    """Everything one Live connection needs to exist, and nothing it produces."""

    name: str
    model: str = MODEL
    #: The conversational voice. The reader voice lives in ``jarvis.voice`` and
    #: is deliberately a DIFFERENT one: Jarvis's own voice never utters
    #: load-bearing text, so the user learns that the other voice means
    #: "somebody else's exact words".
    voice: str = "Zephyr"
    system_instruction: str = ""
    language: str = "en"
    #: False everywhere until spike S4 says otherwise. See the module docstring.
    language_verified: bool = False
    tools: tuple[str, ...] = ()
    frozen_tools: bool = False
    vad: VadTuning = field(default_factory=VadTuning)
    activity_handling: ActivityHandling = "START_OF_ACTIVITY_INTERRUPTS"
    input_rate: int = INPUT_RATE
    output_rate: int = OUTPUT_RATE
    detectors: frozenset[str] = frozenset({"wake", "kill", "nav", "vad", "uplink"})
    session_resumption: bool = True
    compression_trigger_tokens: int = COMPRESSION_TRIGGER_TOKENS
    compression_target_tokens: int = COMPRESSION_TARGET_TOKENS
    input_transcription: bool = True
    #: Needed by the self-speech veto: a rolling window of what Jarvis just said
    #: is how a detector hit on Jarvis reading a GitHub issue aloud gets thrown
    #: away instead of halting the system.
    output_transcription: bool = True
    #: Whether ``FunctionResponseScheduling.SILENT`` is known to keep this model
    #: quiet. UNVERIFIED on 3.8, so the documented fallback (send the option
    #: table as a client-content prefill before unmuting) is armed alongside it.
    silent_scheduling_verified: bool = False
    #: The synthetic ``activity_start`` barge-in. There is no way to command the
    #: model to stop, so the local drop is always done and this server-side lever
    #: is a BONUS that is never depended on. Off until a probe clears it.
    synthetic_barge_in: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("a profile with no name cannot be looked up or logged")
        if not self.model.strip():
            raise ValueError("model must be set; there is no useful default beyond MODEL")
        if not self.voice.strip():
            raise ValueError("voice must be set")
        if not self.language.strip():
            raise ValueError("language must be set; it picks the reader voice on the other track")
        if self.input_rate != INPUT_RATE:
            # The rate is not a preference. It goes into the mime tag, and a tag
            # that disagrees with the samples is the chipmunk bug: audio that
            # plays, sounds wrong, and never raises anything.
            raise ValueError(f"Live input is {INPUT_RATE} Hz PCM16 mono, got {self.input_rate}")
        if self.output_rate != OUTPUT_RATE:
            raise ValueError(f"Live output is {OUTPUT_RATE} Hz PCM16 mono, got {self.output_rate}")
        if self.activity_handling not in ("START_OF_ACTIVITY_INTERRUPTS", "NO_INTERRUPTION"):
            raise ValueError(f"unknown activity_handling {self.activity_handling!r}")
        if len(set(self.tools)) != len(self.tools):
            raise ValueError(f"duplicate tool names in profile {self.name!r}: {self.tools}")
        if any(not t.strip() for t in self.tools):
            raise ValueError("a tool name must be a non-empty string")
        if self.compression_target_tokens >= self.compression_trigger_tokens:
            raise ValueError("compression must target fewer tokens than it triggers at")

    @property
    def manual_vad(self) -> bool:
        """True when WE drive ``activity_start``/``activity_end``."""
        return self.vad.disabled

    @property
    def input_mime(self) -> str:
        return pcm_mime(self.input_rate)

    @property
    def language_caveat(self) -> str | None:
        """The sentence to print, log or say when a leg's language is unproven.

        Returns ``None`` for a verified language. This exists so the unverified
        claim shows up at runtime rather than only in a document nobody opens
        while wondering why the restaurant was addressed in English.
        """
        if self.language_verified or self.language.split("-")[0].lower() == "en":
            return None
        return (
            f"profile {self.name!r} asks for {self.language!r} output through a system "
            "instruction only: speech_config.language_code does NOT control output "
            "language on native-audio models (REFUTED), and nothing has confirmed the "
            "instruction holds for a whole call. Run tools/probe_live.py."
        )

    def allows(self, tool: str) -> bool:
        return tool in self.tools

    def with_tools(self, *names: str) -> SessionProfile:
        """A copy carrying a different tool surface. Refused when frozen."""
        if self.frozen_tools:
            raise ToolSurfaceFrozen(
                f"profile {self.name!r} has a frozen tool surface {self.tools}: "
                "widening it is how a third-party call learns to improvise"
            )
        return replace(self, tools=tuple(names))

    def with_voice(self, voice: str) -> SessionProfile:
        """A copy in another voice.

        Changing voice DOES force a reconnect — it is set at connect time and the
        Live API has no way to change it on a live socket. What it must not do is
        cost the conversation, which is why the session reconnects with its
        resumption handle instead of throwing it away. That difference is the
        whole of reference problem 3.
        """
        return replace(self, voice=voice)


def _instruction(*lines: str) -> str:
    return "\n".join(lines)


DESK = SessionProfile(
    name="desk",
    system_instruction=_instruction(
        "You are Jarvis, a voice assistant at the user's desk. Be brief: this is speech.",
        "You are NOT the only voice here. A separate reader voice speaks option labels,",
        "confirmed requirements, and anything that must be word-for-word. When a tool says",
        "something was already read aloud, do not repeat it — refer to it by number.",
        "The user answers by number. Never invent an option label; call the tool with",
        "the index the user said.",
        "Long work happens in other processes. Tools return a handle at once; say what you",
        "started, not what you finished.",
    ),
    tools=(
        "answer_question",
        "explain_option",
        "reread_options",
        "code_build",
        "job_control",
        "project_status",
        "spend",
        "reachability",
    ),
)

PHONE_USER = SessionProfile(
    name="phone_user",
    system_instruction=_instruction(
        "You are Jarvis, on a phone call with the user. There is no screen: never refer to",
        "one, and never read a URL or a path unless asked twice.",
        "Line quality is poor and the user may be walking. Short sentences, one question.",
        "Confirm anything consequential by having the user say the number back.",
    ),
    detectors=frozenset({"nav", "vad", "uplink"}),
    tools=(
        "answer_question",
        "explain_option",
        "reread_options",
        "job_control",
        "project_status",
        "spend",
        "reachability",
    ),
)

AGENT_CALL = SessionProfile(
    name="phone_tr_third_party",
    voice="Charon",
    language="tr",
    system_instruction=_instruction(
        "SADECE TÜRKÇE konuş. Karşındaki kişi Türkçe konuşan bir restoran çalışanı.",
        "Bir kişisel asistan adına arıyorsun. İlk cümlende otomatik asistan olduğunu söyle.",
        "Görevin tek: verilen tarih, saat ve kişi sayısı için masa olup olmadığını öğrenmek.",
        "Pazarlık yapma, ödeme sözü verme, menü değişikliği isteme, hiçbir taahhütte bulunma.",
        "Sonucu öğrendiğin anda report_outcome aracını çağır ve karşı tarafın kendi sözlerini",
        "exact_words alanına olduğu gibi yaz. Sonra end_call ile konuşmayı bitir.",
        "Emin olmadığın hiçbir şeyi uydurma; anlamadıysan 'unclear' olarak bildir.",
    ),
    tools=AGENT_CALL_TOOLS,
    frozen_tools=True,
    detectors=frozenset({"vad", "uplink"}),
    # A third-party who did not ask to be recorded: their speech is NOT
    # transcribed into the activity log, and the only words of theirs the system
    # keeps are the ones `report_outcome` carries in `exact_words`. Jarvis's own
    # side stays on, because that is what the self-speech veto reads.
    input_transcription=False,
    output_transcription=True,
)

#: Lookup by the architecture's names, plus the two short aliases the roadmap and
#: the CLI use. One profile object per name — ``PROFILES`` maps names to the SAME
#: frozen instances, which is safe precisely because they are frozen.
PROFILES: dict[str, SessionProfile] = {
    DESK.name: DESK,
    PHONE_USER.name: PHONE_USER,
    AGENT_CALL.name: AGENT_CALL,
    "phone": PHONE_USER,
    "agent_call": AGENT_CALL,
}


def profile(name: str) -> SessionProfile:
    """Look up a shipped profile by name or alias."""
    try:
        return PROFILES[name]
    except KeyError:
        raise KeyError(f"unknown profile {name!r}; have {sorted(PROFILES)}") from None


def live_connect_config(
    prof: SessionProfile,
    *,
    handle: str | None = None,
    declarations: list[dict[str, Any]] | None = None,
) -> types.LiveConnectConfig:
    """Build the SDK config for one connection. Imports ``google.genai`` lazily.

    ``handle`` is the resumption handle from a previous connection, and passing
    it is the entire difference between "reconnected" and "started over". It is
    a connection credential, so it is never logged and never put in a bus
    payload — only its presence is.

    Session resumption and context-window compression are both switched on here
    rather than at the call site, because both are MANDATORY for a long call and
    an optional argument is an invitation to forget one.
    """
    from google.genai import types as t

    speech = t.SpeechConfig(
        voice_config=t.VoiceConfig(
            prebuilt_voice_config=t.PrebuiltVoiceConfig(voice_name=prof.voice)
        )
    )
    # DELIBERATELY NOT SET: speech_config.language_code. It was REFUTED for
    # native-audio models — it does not control output language — and setting it
    # anyway would create a second, false explanation for Turkish working or not
    # working, which is the one thing spike S4 must not have to untangle.

    detection = t.AutomaticActivityDetection(disabled=True)
    if not prof.vad.disabled:
        detection = t.AutomaticActivityDetection(
            disabled=False,
            prefix_padding_ms=prof.vad.prefix_padding_ms,
            silence_duration_ms=prof.vad.silence_duration_ms,
            start_of_speech_sensitivity=prof.vad.start_sensitivity,
            end_of_speech_sensitivity=prof.vad.end_sensitivity,
        )

    tools: list[t.Tool] = []
    if declarations:
        tools = [t.Tool(function_declarations=[t.FunctionDeclaration(**d) for d in declarations])]

    return t.LiveConnectConfig(
        response_modalities=["AUDIO"],
        speech_config=speech,
        system_instruction=prof.system_instruction or None,
        tools=tools or None,
        session_resumption=(
            t.SessionResumptionConfig(handle=handle) if prof.session_resumption else None
        ),
        context_window_compression=t.ContextWindowCompressionConfig(
            trigger_tokens=prof.compression_trigger_tokens,
            sliding_window=t.SlidingWindow(target_tokens=prof.compression_target_tokens),
        ),
        realtime_input_config=t.RealtimeInputConfig(
            automatic_activity_detection=detection,
            activity_handling=prof.activity_handling,
        ),
        input_audio_transcription=t.AudioTranscriptionConfig()
        if prof.input_transcription
        else None,
        output_audio_transcription=(
            t.AudioTranscriptionConfig() if prof.output_transcription else None
        ),
    )
