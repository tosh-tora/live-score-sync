#!/usr/bin/env python3
"""
inertia_engine.py - Confidence-Based Beat Extrapolation

When matcher confidence drops, maintains playback continuity by extrapolating beat
position using the last known tempo. Prevents stalling or wild jumps.
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
_STATE_INERTIA = "inertia"


class InertiaEngine:
    """
    Fallback beat extrapolation when matching confidence is low.

    When confidence > threshold: trust matcher output, learn tempo
    When confidence ≤ threshold: extrapolate using last known tempo

    This ensures smooth playback even during difficult audio passages.
    """

    def __init__(
        self,
        confidence_threshold: float = 0.4,
        inertia_timeout_sec: float = 5.0,
    ):
        """
        Initialize inertia engine.

        Args:
            confidence_threshold: Threshold below which inertia activates (0.0-1.0)
            inertia_timeout_sec: After this many seconds of sustained low
                confidence, the engine resets to the waiting-for-tracking
                state.  Prevents a stale lock-in from driving slides forever.
        """
        self.confidence_threshold = confidence_threshold
        self.inertia_timeout_sec = inertia_timeout_sec

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
        Update with matcher output, decide whether to use it or inertia.

        Args:
            current_beat: Beat from pymatchmaker
            confidence: DTW confidence score [0.0, 1.0]

        Returns:
            (beat_to_use, inertia_active, estimated_tempo_bpm)
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
            # inertia engine — so a single noisy frame can't trigger drift.
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

        else:
            # Confidence below threshold: reset the streak.
            self._confident_streak = 0

            # Use inertia — but only if tracking has clearly started.
            # Before lock-in we hold position at beat 0 so slides do not fire
            # before any music is detected.
            if not self._has_ever_matched:
                self.inertia_active = False
                self._log_state(
                    _STATE_WAITING, now, confidence,
                    "holding beat 0 until tracking locks in",
                )
                return 0.0, False, self.last_tempo_bpm

            delta_time = now - self.last_confident_time

            # Defensive timeout: a stale lock-in must not drive slides
            # forever.  If we have been below threshold for too long, drop
            # back into the waiting state so the operator sees measure 1
            # and no triggers fire until real tracking returns.
            if delta_time > self.inertia_timeout_sec:
                logger.warning(
                    "Inertia timeout: no confident match for %.1fs (> %.1fs); "
                    "resetting tracking state",
                    delta_time, self.inertia_timeout_sec,
                )
                self._has_ever_matched = False
                self._confident_streak = 0
                self.inertia_active = False
                self._last_state = _STATE_WAITING
                self._last_heartbeat_time = now
                # Hold tempo so a future lock-in starts with the previous
                # tempo as its initial guess.
                return 0.0, False, self.last_tempo_bpm

            # Extrapolate: new_beat = last_beat + tempo * delta_time
            # tempo in beats/sec = BPM / 60
            delta_beat = (self.last_tempo_bpm / 60.0) * delta_time
            inertia_beat = self.last_confident_beat + delta_beat

            self.inertia_active = True

            self._log_state(
                _STATE_INERTIA, now, confidence,
                f"inertia beat={inertia_beat:.1f}, δt={delta_time:.2f}s, "
                f"tempo={self.last_tempo_bpm:.1f} BPM",
            )
            return inertia_beat, True, self.last_tempo_bpm

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
