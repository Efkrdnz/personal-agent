"""The one audio graph: one microphone tap, one output stream, five rules.

Everything in this package exists to make five rules from `docs/architecture.md`
structural rather than aspirational, so a later change cannot quietly undo them:

1. THE MICROPHONE IS NEVER GATED OFF. Nothing here has a mute. The only thing
   that ever changes while Jarvis speaks is playback gain — see
   :meth:`jarvis.audio.mixer.PlaybackMixer.duck`. That single property is what
   makes barge-in, briefing navigation and the spoken kill switch one mechanism
   instead of three, which is why it is rule one.
2. EXACTLY ONE OUTPUT STREAM, owned by the mixer, and the literal array it hands
   to the device IS the AEC far-end reference. :meth:`PlaybackMixer.claim_output`
   refuses a second claim and :meth:`jarvis.audio.graph.AudioGraph.reference_is_output`
   is the assertion that the reference was not copied, rebuilt or re-mixed on the
   way to the canceller.
3. ONE MICROPHONE TAP: :class:`jarvis.audio.micbus.MicBus`, 16 kHz int16 mono,
   single writer, N independent cursors. A reader that falls behind is
   fast-forwarded and TOLD it lost samples; it never blocks the writer and never
   silently reads a torn buffer.
4. TURNS ARE CLIENT-DRIVEN ON BOTH LEGS. :class:`jarvis.audio.turn.Uplink` has
   ``activity_start``/``activity_end`` and no automatic mode, because server VAD
   tuned for a clean desk mic false-triggers on a phone line.
5. EVERYTHING ABOVE ``clean16`` IS ONE CODE PATH — :class:`AudioGraph`. Legs
   differ only in their DSP front end and which detectors are armed, which is
   why the synthetic leg in CI exercises the same code the desk does.

Import cost: numpy only. ``sounddevice``, ``soxr``, ``pywebrtc_audio`` and any
model runtime are imported lazily inside the function that needs a device or a
model, so this package imports and tests on a machine with no sound card, no
PortAudio and no API key. That is not a convenience; it is the reason the graph
can be driven by synthetic audio at all.
"""

from __future__ import annotations

# Device and bus rates. 48 kHz on the desk rather than 24 kHz because the path to
# the DAC is then bit-exact with no hidden OS resampler between the reference tap
# and the speaker, which is what makes the delay estimate honest; soxr costs
# microseconds. 20 ms blocks rather than 100 ms because every extra millisecond
# lands on the barge-in budget, and not below 20 ms because Python PortAudio
# callbacks start glitching on GIL contention.
DEV_RATE = 48_000
BLOCK = 960
MIC_RATE = 16_000
BUS_RATE = 24_000

# Gemini Live output, Gemini TTS, Kokoro and edge-tts are all natively 24 kHz
# PCM16 mono, so a track writing at BUS_RATE into a BUS_RATE mixer resamples
# nothing at all. The desk's 24k -> 48k step is the mixer's own, applied once,
# after the fade, to the array that is also the AEC reference.
BLOCK_MS = 1000 * BLOCK // DEV_RATE

# The VAD's native frame. Silero wants 512 samples at 16 kHz; the energy VAD that
# stands in for it in CI uses the same frame so the two are interchangeable
# without retuning the onset count.
VAD_FRAME = 512
VAD_FRAME_MS = 1000 * VAD_FRAME // MIC_RATE

# Barge-in: duck on suspicion, then confirm. Three 32 ms frames of speech is
# ~96 ms of onset, the duck lands one 20 ms block later (~150 ms, inside the
# ~300 ms conversational tolerance), and the 200 ms confirm window decides
# whether it was a human or the tail of our own echo.
ONSET_FRAMES = 3
CONFIRM_MS = 200
DUCK_DB = -20.0
DUCK_RAMP_MS = 20
RESTORE_RAMP_MS = 50

# 700 ms, NOT the 100 ms in the Live guide: that figure is for SERVER VAD.
# Google's manual-VAD guidance says at least 500 ms or audio quality degrades.
HANGOVER_MS = 700

# The pre-roll is what makes a confirmed barge-in lossless: by the time we know
# it was real, the first ~300 ms of the word is already past.
PREROLL_MS = 320

# A barge-in flushes the mixer, but audio already in flight from Gemini keeps
# arriving for a while. Anything older than this when it reaches the mixer is
# answering a question the user already interrupted.
LIVE_TTL_S = 2.0

# Jarvis must not wake, kill or navigate himself by reading his own words aloud.
SELF_SPEECH_WINDOW_S = 9.0
SELF_SPEECH_TAIL_S = 1.0

# MONITOR is the only MIXED tier, and it sits this far under everything else.
MONITOR_DB = -12.0

__all__ = [
    "BLOCK",
    "BLOCK_MS",
    "BUS_RATE",
    "CONFIRM_MS",
    "DEV_RATE",
    "DUCK_DB",
    "DUCK_RAMP_MS",
    "HANGOVER_MS",
    "LIVE_TTL_S",
    "MIC_RATE",
    "MONITOR_DB",
    "ONSET_FRAMES",
    "PREROLL_MS",
    "RESTORE_RAMP_MS",
    "SELF_SPEECH_TAIL_S",
    "SELF_SPEECH_WINDOW_S",
    "VAD_FRAME",
    "VAD_FRAME_MS",
]
