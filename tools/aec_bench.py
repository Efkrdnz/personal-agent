#!/usr/bin/env python3
"""Measure whether AEC is good enough for open speakers. One hour, on the real desk.

THIS CANNOT RUN ON THE MACHINE THAT WROTE IT, AND SAYS SO RATHER THAN PRETENDING.
``import sounddevice`` raises ``OSError("PortAudio library not found")`` here:
there is no sound card, no ``/dev/snd``, and there never will be in CI. Run
``--selftest`` to exercise the arithmetic and the decision rule on synthetic
audio with no device at all; that is the only mode CI uses and it proves the
scorer, not the room.

THE DECISION RULE IS FIXED BEFORE THE MEASUREMENT, ON PURPOSE. A threshold chosen
after seeing the number is not a threshold, it is a rationalisation, and every
"AEC is basically fine" story ends that way. So it is in this file, in one
function, and the run prints which branch it took::

    median ERLE >= 25 dB AND >= 18/20 barge-ins detected within 300 ms
                          AND <= 1 false barge-in per 30 s
        -> SHIP OPEN SPEAKERS.

    15-25 dB
        -> ship with a 6 dB playback cap and a documented mic position,
           onset_frames = 4, and RE-MEASURE.

    below 15 dB, or > 3 false per 30 s, or ERLE degrading by > 6 dB over ten
    minutes (THAT IS CLOCK DRIFT, not the room)
        -> walk the ladder: move things (mic >= 40 cm from the speakers, not
           aimed at them, one notch quieter — free, usually worth 10 dB); give
           the APM a correct stream_delay_ms; force capture and render onto one
           device; OS-native AEC (libpipewire-module-echo-cancel, which owns both
           clocks AND cancels other applications' audio, which in-process AEC
           structurally cannot see); then A WIRED USB HEADSET, ~$40, 30-40 dB of
           isolation, single clock by construction — the RECOMMENDED DEFAULT.

WHAT THE NUMBER MEANS, because ERLE is easy to fake. It is computed only over
frames where the FAR END WAS ACTUALLY LOUD, after discarding the first 3 seconds
of convergence. Averaged over a whole recording it is a ratio of two noise floors
and a canceller that does nothing scores well. The three volumes matter for the
same reason: AEC3 cancels the LINEAR echo path, and a cheap speaker driven hard
produces nonlinear distortion that no linear filter can remove, so the loud case
is the interesting one and the quiet case proves nothing.

NOBODY SHOULD TREAT THIS AS A GO/NO-GO ON THE PROJECT. If AEC is inadequate and
the user refuses a headset, what is lost is open-speaker barge-in and nothing
else: the kill switch keeps its hotkey and DTMF, briefing navigation keeps
push-to-talk, the wake word is unaffected because nothing is playing while Jarvis
is asleep, and the phone leg never needed AEC at all.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis.audio import (  # noqa: E402
    BLOCK,
    DEV_RATE,
    MIC_RATE,
    VAD_FRAME,
)
from jarvis.audio.dsp import EnergyVad, dbfs, erle_db  # noqa: E402
from jarvis.audio.graph import AudioGraph  # noqa: E402
from jarvis.audio.legs import SyntheticLeg, frames_from  # noqa: E402
from jarvis.audio.micbus import MicBus  # noqa: E402
from jarvis.audio.mixer import PlaybackMixer, Prio  # noqa: E402
from jarvis.audio.turn import RecordingUplink, TurnController  # noqa: E402

# Fixed before the measurement. Changing one of these is a design decision that
# belongs in a commit message, not a tuning session at 1am.
ERLE_SHIP_DB = 25.0
ERLE_FLOOR_DB = 15.0
BARGE_IN_TRIALS = 20
BARGE_IN_REQUIRED = 18
BARGE_IN_LATENCY_MS = 300
FALSE_PER_30S_SHIP = 1.0
FALSE_PER_30S_LADDER = 3.0
DRIFT_DEGRADATION_DB = 6.0
CONVERGENCE_DISCARD_S = 3.0

# ERLE over quiet frames is a ratio of two noise floors. Only frames where the
# far end was this loud count toward the median.
FAR_ACTIVE_DBFS = -40.0

Verdict = Literal["open_speakers", "cap_and_remeasure", "walk_the_ladder"]


@dataclass(frozen=True)
class Result:
    median_erle_db: float
    erle_first_half_db: float
    erle_second_half_db: float
    scored_frames: int
    barge_ins_detected: int
    barge_in_trials: int
    median_detect_ms: float
    false_barge_ins: int
    seconds: float

    @property
    def false_per_30s(self) -> float:
        return self.false_barge_ins * 30.0 / self.seconds if self.seconds > 0 else 0.0

    @property
    def drift_db(self) -> float:
        """Positive means ERLE got WORSE over the run, which is the clock-drift signature."""
        return self.erle_first_half_db - self.erle_second_half_db


def decide(r: Result) -> tuple[Verdict, str]:
    """The rule, in one place, applied to the numbers and to nothing else."""
    if r.drift_db > DRIFT_DEGRADATION_DB:
        return (
            "walk_the_ladder",
            f"ERLE fell {r.drift_db:.1f} dB across the run (> {DRIFT_DEGRADATION_DB:.0f}). "
            "That is CLOCK DRIFT, not the room: capture and render are on two "
            "crystals. Force one duplex device before changing anything else.",
        )
    if r.median_erle_db < ERLE_FLOOR_DB or r.false_per_30s > FALSE_PER_30S_LADDER:
        return (
            "walk_the_ladder",
            f"median ERLE {r.median_erle_db:.1f} dB and {r.false_per_30s:.1f} false "
            f"barge-ins per 30 s. Below the {ERLE_FLOOR_DB:.0f} dB floor. Move the mic, "
            "fix stream_delay_ms, force one device, try the OS AEC — then buy the "
            "$40 wired headset, which is the recommended default anyway.",
        )
    detected_ok = r.barge_ins_detected >= BARGE_IN_REQUIRED
    if r.median_erle_db >= ERLE_SHIP_DB and detected_ok and r.false_per_30s <= FALSE_PER_30S_SHIP:
        return (
            "open_speakers",
            f"median ERLE {r.median_erle_db:.1f} dB, "
            f"{r.barge_ins_detected}/{r.barge_in_trials} barge-ins under "
            f"{BARGE_IN_LATENCY_MS} ms, {r.false_per_30s:.1f} false per 30 s. Ship it.",
        )
    return (
        "cap_and_remeasure",
        f"median ERLE {r.median_erle_db:.1f} dB, "
        f"{r.barge_ins_detected}/{r.barge_in_trials} barge-ins, "
        f"{r.false_per_30s:.1f} false per 30 s. Cap playback at -6 dB, document the "
        "mic position, set onset_frames = 4, and re-measure.",
    )


def score_erle(
    near: np.ndarray, clean: np.ndarray, far: np.ndarray, *, rate: int = DEV_RATE
) -> tuple[float, float, float, int]:
    """Median ERLE over high-far-energy frames, after discarding convergence.

    Returns ``(median, first_half, second_half, frames)``. The halves exist only
    to catch drift: a canceller that is 28 dB for the first five minutes and
    19 dB for the next five has a clock problem, and a single median hides it
    completely.
    """
    skip = int(CONVERGENCE_DISCARD_S * rate)
    near, clean, far = near[skip:], clean[skip:], far[skip:]
    n = min(near.shape[0], clean.shape[0], far.shape[0]) // BLOCK * BLOCK
    scores: list[float] = []
    for i in range(0, n, BLOCK):
        if dbfs(far[i : i + BLOCK]) < FAR_ACTIVE_DBFS:
            continue
        scores.append(erle_db(near[i : i + BLOCK], clean[i : i + BLOCK]))
    if not scores:
        return 0.0, 0.0, 0.0, 0
    half = len(scores) // 2 or 1
    return (
        float(np.median(scores)),
        float(np.median(scores[:half])),
        float(np.median(scores[half:])),
        len(scores),
    )


def count_false_barge_ins(
    near: np.ndarray,
    far: np.ndarray,
    *,
    rate: int = MIC_RATE,
    aec: object | None = None,
) -> tuple[int, float]:
    """Run the REAL duck-confirm chain over a silent-room recording and count commits.

    Not a VAD-firing count. What matters is how often the whole chain — onset,
    duck, 200 ms confirm — would have opened a turn nobody asked for, because
    a VAD that fires and is then rejected costs a 200 ms dip and nothing else.
    """
    mixer = PlaybackMixer(rate=rate)
    track = mixer.track("live", Prio.LIVE, ttl_s=None)
    bus = MicBus(rate=MIC_RATE, seconds=4.0)
    uplink = RecordingUplink()
    turn = TurnController(mixer=mixer, vad=EnergyVad(frame_samples=VAD_FRAME), uplink=uplink)
    graph = AudioGraph(mixer=mixer, micbus=bus, turn=turn, aec=aec, device_rate=rate, block=BLOCK)
    track.write(np.asarray(far, dtype=np.int16), at=0.0)
    leg = SyntheticLeg(rate=rate, block=BLOCK)
    leg.feed(frames_from(np.asarray(near, dtype=np.int16), BLOCK))
    leg.run(graph)
    return uplink.starts, near.shape[0] / rate


def _selftest() -> int:
    """Prove the arithmetic and the rule with no device, no room and no sound card."""
    rate = MIC_RATE
    t = np.arange(rate * 10)
    far = (np.sin(t * 2 * np.pi * 300 / rate) * 9000).astype(np.int16)
    near = far.copy()

    for attenuation_db, expected in ((30.0, "open_speakers"), (10.0, "walk_the_ladder")):
        gain = 10 ** (-attenuation_db / 20)
        clean = (near.astype(np.float64) * gain).astype(np.int16)
        median, first, second, frames = score_erle(near, clean, far, rate=rate)
        result = Result(
            median_erle_db=median,
            erle_first_half_db=first,
            erle_second_half_db=second,
            scored_frames=frames,
            barge_ins_detected=BARGE_IN_REQUIRED,
            barge_in_trials=BARGE_IN_TRIALS,
            median_detect_ms=180.0,
            false_barge_ins=0,
            seconds=10.0,
        )
        verdict, why = decide(result)
        print(f"  {attenuation_db:.0f} dB attenuation -> ERLE {median:.1f} dB -> {verdict}")
        if not math.isclose(median, attenuation_db, abs_tol=1.0):
            print(f"  FAIL: ERLE should be ~{attenuation_db} dB, got {median}")
            return 1
        if verdict != expected:
            print(f"  FAIL: expected {expected}, got {verdict}: {why}")
            return 1

    # And the drift branch, which is the one that gets missed.
    drifting = Result(28.0, 31.0, 20.0, 500, 20, 20, 150.0, 0, 600.0)
    verdict, why = decide(drifting)
    if verdict != "walk_the_ladder" or "DRIFT" not in why:
        print(f"  FAIL: a 11 dB decay must read as clock drift, got {verdict}")
        return 1
    print(f"  ERLE 31 -> 20 dB across the run -> {verdict} (clock drift)")

    silent = np.zeros(rate * 5, dtype=np.int16)
    starts, seconds = count_false_barge_ins(silent, silent, rate=rate)
    print(f"  silent room, silent playback: {starts} false barge-ins in {seconds:.0f} s")
    if starts != 0:
        print("  FAIL: a silent room must not open a turn")
        return 1
    print("selftest OK — the scorer and the decision rule work. The ROOM is untested.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="exercise the scorer and the decision rule on synthetic audio (no device)",
    )
    parser.add_argument(
        "--device", default=None, help="name of the ONE duplex device to measure on"
    )
    parser.add_argument(
        "--volumes",
        default="0.3,0.6,1.0",
        help="three playback gains to sweep; loud is the interesting one",
    )
    parser.add_argument("--seconds", type=float, default=30.0, help="single-talk duration")
    parser.add_argument("--json", type=Path, default=None, help="write the result here")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()

    # The honest part. Everything below needs a device, and this machine has none.
    from jarvis.audio.devices import (
        AudioStackMissing,
        DeviceError,
        PortAudioProbe,
        select_duplex_device,
    )

    try:
        selection = select_duplex_device(PortAudioProbe(), name=args.device)
    except AudioStackMissing as exc:
        print(f"CANNOT MEASURE HERE: {exc}", file=sys.stderr)
        print(
            "\nThis bench needs a real room, a real speaker and a real microphone. "
            "There is nothing to measure on a box with no sound card, and a number "
            "produced here would be a number about numpy.\n"
            "Run `--selftest` to check the scorer; run this on the desk to check the room.",
            file=sys.stderr,
        )
        return 2
    except DeviceError as exc:
        print(f"CANNOT MEASURE: {exc}", file=sys.stderr)
        return 2

    print(f"Measuring on {selection.describe()}")
    print(
        "TEST 1 single-talk: silent room, 30 s of real Gemini output, three volumes.\n"
        "TEST 2 double-talk: 20 short spoken barge-ins from the normal seat.\n"
        "TEST 3: repeat with the desk fan on.\n"
        "Sit where you normally sit. Do not speak during test 1."
    )
    # Deliberately not implemented against a device that does not exist here: a
    # capture loop written blind would be wrong in ways nobody could see, and
    # writing it on the desk with the hardware in front of you takes an hour.
    print(
        "\nNOT IMPLEMENTED: the capture loop is the one part that must be written "
        "with the hardware present. Everything it needs is already here — "
        "score_erle(), count_false_barge_ins(), decide() — so it is a PortAudio "
        "duplex callback that records near/far/clean into three arrays and calls them.",
        file=sys.stderr,
    )
    if args.json:
        args.json.write_text(json.dumps({"status": "not_measured"}, indent=2))
    return 3


def render(result: Result) -> str:
    verdict, why = decide(result)
    body = json.dumps(asdict(result) | {"false_per_30s": result.false_per_30s}, indent=2)
    return f"{body}\n\nVERDICT: {verdict}\n{why}"


if __name__ == "__main__":
    raise SystemExit(main())
