#!/usr/bin/env python3
"""
matcher.py - pymatchmaker (Matchmaker) Integration

Runs `matchmaker.Matchmaker.run()` in a dedicated background thread, exposing
the latest beat position and an estimated confidence value via thread-safe
accessors.

Design notes
------------
The public `matchmaker` API yields a single `current_position` (in beats) per
audio frame; it does not surface a confidence value as of pymatchmaker 0.2.1.
To keep the existing InertiaEngine semantics (which expects a confidence in
[0, 1]), we approximate confidence from the stability of beat progression:

- "Healthy" tracking: beat advances at a roughly constant velocity.
- "Lost" tracking: beat stalls, jumps backward, or oscillates wildly.

We measure this with the coefficient of variation (std/mean) of recent beat
velocities. Low CV → high confidence; high CV or non-positive velocity → low
confidence.

If a future pymatchmaker release exposes a real confidence/cost value, this
module is the single place that needs to change.
"""

from __future__ import annotations

import logging
import statistics
import threading
import time
from collections import deque
from typing import Deque, Optional, Tuple, Union

logger = logging.getLogger(__name__)

# Confidence estimation window (seconds of recent beat history we track)
_CONFIDENCE_WINDOW_SEC = 2.0
# Coefficient of variation above this maps to confidence = 0
_CV_TO_ZERO_CONFIDENCE = 0.5
# If beat hasn't advanced for this long, force confidence to 0
_STALL_TIMEOUT_SEC = 1.0


class MatchMaker:
    """
    Thread-backed wrapper around `matchmaker.Matchmaker.run()`.

    Typical lifecycle::

        m = MatchMaker("guide_mv1.xml", device_name_or_index="default")
        m.start()
        m.wait_ready(timeout=10.0)        # block until first beat emitted
        beat, confidence = m.get_latest()  # called repeatedly by main loop
        ...
        m.stop()
    """

    def __init__(
        self,
        score_file: str,
        input_type: str = "audio",
        device_name_or_index: Optional[Union[str, int]] = None,
        method: str = "arzt",
        feature_type: str = "chroma",
    ) -> None:
        """
        Args:
            score_file: Path to MusicXML or MIDI score.
            input_type: "audio" (microphone) or "midi".
            device_name_or_index: Specific input device (None = system default).
            method: Alignment method ("arzt", "dixon", or "hmm"). For audio
                input, "arzt" or "dixon" are valid; "hmm" is MIDI-only.
            feature_type: Audio feature type ("chroma", "mfcc", "mel",
                "logspectral").
        """
        self.score_file = score_file
        self.input_type = input_type
        self.device_name_or_index = device_name_or_index
        self.method = method
        self.feature_type = feature_type

        # Thread-safe state
        self._lock = threading.Lock()
        self._latest_beat: float = 0.0
        self._latest_confidence: float = 0.0
        self._latest_update_time: float = time.time()
        self._beat_history: Deque[Tuple[float, float]] = deque()  # (timestamp, beat)

        # Lifecycle
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._fatal_error: Optional[BaseException] = None

        # The underlying matchmaker object is constructed lazily inside the
        # thread because some implementations open the audio stream in __init__
        # and we want stream lifetime tied to the worker thread.
        self._mm = None

        logger.info(
            "MatchMaker configured: score=%s input=%s method=%s feature=%s device=%s",
            score_file, input_type, method, feature_type, device_name_or_index,
        )

    # ------------------------------------------------------------------ public
    def start(self) -> None:
        """Spawn the worker thread. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("MatchMaker.start() called but thread already running")
            return

        self._stop_event.clear()
        self._ready_event.clear()
        self._fatal_error = None

        self._thread = threading.Thread(
            target=self._run_loop,
            name="matchmaker-worker",
            daemon=True,
        )
        self._thread.start()
        logger.info("MatchMaker worker thread started")

    def stop(self, timeout: float = 3.0) -> None:
        """Signal worker to stop. Best-effort join.

        The underlying Matchmaker.run() generator can only be interrupted at
        the next yield boundary, so stop is not instantaneous; we set the flag
        and let the loop exit on its next iteration. We then drop the
        Matchmaker reference so its audio stream is closed by its destructor.
        """
        self._stop_event.set()

        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning(
                    "MatchMaker worker did not stop within %.1fs (Matchmaker.run() "
                    "may be blocked in audio I/O; releasing reference anyway)",
                    timeout,
                )

        self._mm = None
        self._thread = None
        logger.info("MatchMaker stopped")

    def wait_ready(self, timeout: float = 10.0) -> bool:
        """Block until the first beat value has been emitted.

        Returns True if the matcher emitted at least one value within the
        timeout; False if the matcher failed to start. Check `last_error`
        in the False case.
        """
        return self._ready_event.wait(timeout)

    @property
    def last_error(self) -> Optional[BaseException]:
        """Return the fatal error that stopped the worker, if any."""
        return self._fatal_error

    def get_latest(self) -> Tuple[float, float]:
        """Return a snapshot of (latest_beat, estimated_confidence)."""
        with self._lock:
            # Apply stall detection at read time so confidence drops to 0
            # even while no new yields are arriving.
            if time.time() - self._latest_update_time > _STALL_TIMEOUT_SEC:
                confidence = 0.0
            else:
                confidence = self._latest_confidence
            return self._latest_beat, confidence

    def reset(self) -> None:
        """Reset cached state (used between movements before re-start())."""
        with self._lock:
            self._latest_beat = 0.0
            self._latest_confidence = 0.0
            self._latest_update_time = time.time()
            self._beat_history.clear()
        self._ready_event.clear()

    # ----------------------------------------------------------------- private
    def _run_loop(self) -> None:
        """Worker thread entry: drive Matchmaker.run() generator."""
        try:
            from matchmaker import Matchmaker  # type: ignore
        except ImportError as exc:
            self._fatal_error = exc
            logger.error(
                "pymatchmaker is not installed. Install it inside WSL2 with "
                "`pip install pymatchmaker` (Linux wheels only)."
            )
            self._ready_event.set()
            return

        try:
            mm_kwargs = {
                "score_file": self.score_file,
                "input_type": self.input_type,
                "method": self.method,
                "feature_type": self.feature_type,
            }
            if self.device_name_or_index is not None:
                mm_kwargs["device_name_or_index"] = self.device_name_or_index

            logger.info("Instantiating Matchmaker(%s)", mm_kwargs)
            self._mm = Matchmaker(**mm_kwargs)
        except Exception as exc:  # noqa: BLE001 — surface any startup error
            self._fatal_error = exc
            logger.error("Matchmaker construction failed: %s", exc, exc_info=True)
            self._ready_event.set()
            return

        logger.info("Entering Matchmaker.run() generator loop")

        try:
            for current_position in self._mm.run():
                if self._stop_event.is_set():
                    logger.info("Stop event received, leaving run() loop")
                    break

                # current_position may be a float (beats) or, for newer
                # versions, a richer object. Be defensive.
                beat = self._coerce_beat(current_position)
                self._update(beat)

        except StopIteration:
            logger.info("Matchmaker.run() exhausted (score ended)")
        except Exception as exc:  # noqa: BLE001
            self._fatal_error = exc
            logger.error("Matchmaker.run() raised: %s", exc, exc_info=True)
        finally:
            self._ready_event.set()  # unblock any wait_ready() callers
            logger.info("Matcher worker exiting")

    @staticmethod
    def _coerce_beat(value) -> float:
        """Normalize Matchmaker yield value to a float beat position."""
        if isinstance(value, (int, float)):
            return float(value)
        # Some versions may yield a dict or namespace; try common keys.
        for attr in ("beat", "position", "current_position"):
            if isinstance(value, dict) and attr in value:
                return float(value[attr])
            if hasattr(value, attr):
                return float(getattr(value, attr))
        # Fall back: try iterable unpacking (beat, ...)
        try:
            return float(next(iter(value)))
        except Exception:
            logger.debug("Could not coerce matcher value %r to beat", value)
            return 0.0

    def _update(self, beat: float) -> None:
        """Record a new beat reading and recompute confidence."""
        now = time.time()

        with self._lock:
            self._latest_beat = beat
            self._latest_update_time = now

            # Maintain recent history (sliding window)
            self._beat_history.append((now, beat))
            cutoff = now - _CONFIDENCE_WINDOW_SEC
            while self._beat_history and self._beat_history[0][0] < cutoff:
                self._beat_history.popleft()

            self._latest_confidence = self._estimate_confidence_locked()

        if not self._ready_event.is_set():
            self._ready_event.set()

    def _estimate_confidence_locked(self) -> float:
        """Compute confidence from recent beat history. Caller holds the lock."""
        history = self._beat_history
        if len(history) < 3:
            return 0.5  # not enough data yet — neutral

        velocities = []
        for i in range(1, len(history)):
            t0, b0 = history[i - 1]
            t1, b1 = history[i]
            dt = t1 - t0
            if dt > 0:
                velocities.append((b1 - b0) / dt)

        if not velocities:
            return 0.0

        v_mean = statistics.fmean(velocities)
        if v_mean <= 0:
            # Beat not progressing (stalled or moving backward) → no confidence
            return 0.0

        v_std = statistics.pstdev(velocities) if len(velocities) > 1 else 0.0
        cv = v_std / v_mean
        # Map [0, _CV_TO_ZERO_CONFIDENCE] → [1.0, 0.0]
        confidence = max(0.0, 1.0 - cv / _CV_TO_ZERO_CONFIDENCE)
        return min(1.0, confidence)

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        beat, conf = self.get_latest()
        return (
            f"MatchMaker(score={self.score_file!r}, beat={beat:.2f}, "
            f"confidence={conf:.2f}, running={self._thread is not None and self._thread.is_alive()})"
        )
