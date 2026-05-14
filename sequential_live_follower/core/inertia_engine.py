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


class InertiaEngine:
    """
    Fallback beat extrapolation when matching confidence is low.

    When confidence > threshold: trust matcher output, learn tempo
    When confidence ≤ threshold: extrapolate using last known tempo

    This ensures smooth playback even during difficult audio passages.
    """

    def __init__(self, confidence_threshold: float = 0.4):
        """
        Initialize inertia engine.

        Args:
            confidence_threshold: Threshold below which inertia activates (0.0-1.0)
        """
        self.confidence_threshold = confidence_threshold

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

            logger.debug(
                f"High confidence ({confidence:.2f}): using matcher beat {current_beat:.1f}, "
                f"tempo {self.last_tempo_bpm:.1f} BPM, streak={self._confident_streak}"
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
                logger.debug(
                    f"Low confidence ({confidence:.2f}): waiting for tracking lock-in, holding beat 0"
                )
                return 0.0, False, self.last_tempo_bpm

            delta_time = now - self.last_confident_time

            # Extrapolate: new_beat = last_beat + tempo * delta_time
            # tempo in beats/sec = BPM / 60
            delta_beat = (self.last_tempo_bpm / 60.0) * delta_time
            inertia_beat = self.last_confident_beat + delta_beat

            self.inertia_active = True

            logger.debug(
                f"Low confidence ({confidence:.2f}): inertia beat {inertia_beat:.1f} "
                f"(δt={delta_time:.2f}s, tempo={self.last_tempo_bpm:.1f} BPM)"
            )

            return inertia_beat, True, self.last_tempo_bpm

    def is_locked_in(self) -> bool:
        """Return True once tracking has clearly begun.

        Trigger executors should refuse to fire while this is False so that
        slides do not advance before any music is detected (e.g. the
        measure=1 trigger would otherwise fire at startup because beat=0
        maps to measure 1).
        """
        return self._has_ever_matched

    def reset(self):
        """Reset inertia engine (for movement changes)."""
        self.last_confident_beat = 0.0
        self.last_confident_time = time.time()
        self.last_tempo_bpm = 120.0
        self.inertia_active = False
        self._has_ever_matched = False
        self._confident_streak = 0

    def __repr__(self) -> str:
        return (
            f"InertiaEngine(threshold={self.confidence_threshold}, "
            f"last_tempo={self.last_tempo_bpm:.1f} BPM, inertia={self.inertia_active})"
        )
