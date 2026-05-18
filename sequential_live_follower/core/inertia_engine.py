#!/usr/bin/env python3
"""
inertia_engine.py - Tracking Lock-in Gate (no extrapolation)

Despite the name (kept for import-compatibility with main.py / state_manager
/ GUI), this module no longer extrapolates beat position from a stale
tempo estimate.  Live experiments showed extrapolation racing ahead of
the conductor during expressive passages, then snapping back to measure 1
when the timeout elapsed — i.e. exactly the bug we are now avoiding.

Current behaviour:

- ``confidence >= threshold``: return the matcher's current beat verbatim
  and learn the tempo (so the GUI can still display it).
- ``confidence < threshold`` before lock-in: hold beat at 0 (so the
  measure-1 trigger does not fire before any music is detected).
- ``confidence < threshold`` after lock-in: **hold the last confident
  beat** unchanged.  We trust pymatchmaker's DTW to resume tracking on
  its own when audio quality improves; freezing the displayed position
  is honest visual feedback and never wrong by more than the matcher's
  own latency.

``inertia_active`` is always False going forward, but the field is kept
so that ``state_manager.set_inertia_mode`` and the GUI label keep working
without changes.
"""

import logging
import time
from typing import Tuple

logger = logging.getLogger(__name__)

# Require this many consecutive high-confidence frames before declaring that
# tracking has truly begun.  A single isolated confident frame (e.g., from
# transient noise) is not enough to unlock inertia extrapolation.
_CONFIDENT_FRAMES_TO_LOCK_IN = 3

# Per-iteration heartbeat logs spam at the state-sync rate (20 Hz); the
# engine instead only logs on state transitions plus a periodic heartbeat
# at this interval (seconds) for diagnostics.
_HEARTBEAT_INTERVAL_SEC = 5.0

# Internal state labels used to decide when to emit a transition log.
_STATE_WAITING = "waiting"
_STATE_TRACKING = "tracking"
_STATE_HOLD = "hold"  # post-lock-in, confidence currently low — hold last beat


class InertiaEngine:
    """
    Tracking lock-in gate (legacy name).

    When confidence is high, we forward the matcher's beat verbatim and
    learn the tempo for display.  When confidence is low we hold the last
    confident beat (or stay at 0 before lock-in).  No extrapolation.
    """

    def __init__(
        self,
        confidence_threshold: float = 0.4,
        inertia_timeout_sec: float = 5.0,
    ):
        """
        Initialize the gate.

        Args:
            confidence_threshold: Threshold below which we hold position
                instead of forwarding the matcher's beat (0.0-1.0)
            inertia_timeout_sec: Accepted for backward compatibility with
                callers / config.json.  No longer used — there is no
                extrapolation timeout to enforce.
        """
        self.confidence_threshold = confidence_threshold
        self.inertia_timeout_sec = inertia_timeout_sec  # retained for API stability

        # Last high-confidence state
        self.last_confident_beat = 0.0
        self.last_confident_time = time.time()
        self.last_tempo_bpm = 120.0  # Fallback default

        self.inertia_active = False

        # Guard: do not extrapolate until tracking has clearly begun.  Without
        # this flag the engine would immediately start advancing at 120 BPM
        # from t=0, causing slides to fire before any music is detected.
        # A single noisy frame above threshold is not enough — we require
        # several consecutive confident frames (see _CONFIDENT_FRAMES_TO_LOCK_IN).
        self._has_ever_matched = False
        self._confident_streak = 0

        # Bookkeeping for noise-free logging: only emit on state changes
        # plus an occasional heartbeat so the DEBUG stream stays readable.
        self._last_state: str = _STATE_WAITING
        self._last_heartbeat_time: float = 0.0

    def update(
        self,
        current_beat: float,
        confidence: float
    ) -> Tuple[float, bool, float]:
        """
        Update with matcher output, forward or hold the beat.

        Args:
            current_beat: Beat from pymatchmaker
            confidence: Wrapper-derived confidence in [0.0, 1.0]

        Returns:
            (beat_to_use, inertia_active, estimated_tempo_bpm).
            ``inertia_active`` is always False; the field is kept so the
            GUI / state_manager continue to work unchanged.
        """
        now = time.time()

        if confidence >= self.confidence_threshold:
            # High confidence: trust matcher
            delta_time = now - self.last_confident_time

            if delta_time > 0.0 and self._has_ever_matched:
                # Estimate tempo from beat jump (only after the first match so
                # we have a meaningful previous position to compare against).
                delta_beat = current_beat - self.last_confident_beat
                # Convert beat/sec to BPM (assumes beat = quarter note)
                self.last_tempo_bpm = (delta_beat / delta_time) * 60.0

            self.last_confident_beat = current_beat
            self.last_confident_time = now
            self.inertia_active = False

            # Streak-based lock-in: only mark "tracking has begun" after
            # several consecutive confident frames.  Until then, treat the
            # frame as confident for display purposes but do NOT unlock the
            # trigger gate — so a single noisy frame can't fire slides.
            self._confident_streak += 1
            if (
                not self._has_ever_matched
                and self._confident_streak >= _CONFIDENT_FRAMES_TO_LOCK_IN
            ):
                self._has_ever_matched = True
                logger.info(
                    "Tracking locked in after %d confident frames "
                    "(beat=%.2f, tempo=%.1f BPM)",
                    self._confident_streak, current_beat, self.last_tempo_bpm,
                )

            self._log_state(
                _STATE_TRACKING, now, confidence,
                f"beat={current_beat:.1f}, tempo={self.last_tempo_bpm:.1f} BPM, "
                f"streak={self._confident_streak}",
            )
            return current_beat, False, self.last_tempo_bpm

        # Confidence below threshold: reset the streak so a future
        # recovery has to re-prove itself before firing triggers.
        self._confident_streak = 0
        self.inertia_active = False

        # Before lock-in, hold position at beat 0 so slides do not fire
        # before any music is detected.
        if not self._has_ever_matched:
            self._log_state(
                _STATE_WAITING, now, confidence,
                "holding beat 0 until tracking locks in",
            )
            return 0.0, False, self.last_tempo_bpm

        # After lock-in: hold the last confident beat unchanged.  We trust
        # pymatchmaker's DTW to resume on its own when audio quality
        # returns; we do NOT synthesize beats from a stale tempo.
        self._log_state(
            _STATE_HOLD, now, confidence,
            f"holding beat={self.last_confident_beat:.1f} "
            f"(δt={now - self.last_confident_time:.2f}s since last confident match)",
        )
        return self.last_confident_beat, False, self.last_tempo_bpm

    def _log_state(
        self, state: str, now: float, confidence: float, detail: str
    ) -> None:
        """Emit a DEBUG log only on state changes or once every heartbeat.

        ``update()`` is called at the state-sync rate (20 Hz). Logging
        every call floods ``-v`` output and makes it unreadable. We emit
        a line on transitions (most useful) and otherwise rate-limit to
        ``_HEARTBEAT_INTERVAL_SEC`` so the engine is still observable
        when nothing is changing.
        """
        if state != self._last_state:
            logger.debug(
                "state %s → %s (conf=%.2f): %s",
                self._last_state, state, confidence, detail,
            )
            self._last_state = state
            self._last_heartbeat_time = now
            return
        if now - self._last_heartbeat_time >= _HEARTBEAT_INTERVAL_SEC:
            logger.debug(
                "state %s (conf=%.2f): %s", state, confidence, detail,
            )
            self._last_heartbeat_time = now

    def is_locked_in(self) -> bool:
        """Return True once tracking has clearly begun.

        Trigger executors should refuse to fire while this is False so that
        slides do not advance before any music is detected (e.g. the
        measure=1 trigger would otherwise fire at startup because beat=0
        maps to measure 1).
        """
        return self._has_ever_matched

    def reset_tracking(self) -> None:
        """Drop the current lock-in and return to the waiting state.

        Public version of the streak/lock-in reset, intended for manual
        recovery (e.g. operator presses 'R' when tracking has clearly
        drifted away from reality).  Tempo is preserved as the next
        starting guess.
        """
        self._has_ever_matched = False
        self._confident_streak = 0
        self.inertia_active = False
        self._last_state = _STATE_WAITING
        self._last_heartbeat_time = 0.0
        logger.info("Inertia tracking state reset (manual)")

    def reset(self):
        """Reset inertia engine (for movement changes)."""
        self.last_confident_beat = 0.0
        self.last_confident_time = time.time()
        self.last_tempo_bpm = 120.0
        self.inertia_active = False
        self._has_ever_matched = False
        self._confident_streak = 0
        self._last_state = _STATE_WAITING
        self._last_heartbeat_time = 0.0

    def __repr__(self) -> str:
        return (
            f"InertiaEngine(threshold={self.confidence_threshold}, "
            f"last_tempo={self.last_tempo_bpm:.1f} BPM, inertia={self.inertia_active})"
        )
