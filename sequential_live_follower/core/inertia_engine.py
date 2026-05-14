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

        # Guard: do not extrapolate until at least one confident match is
        # received.  Without this flag the engine would immediately start
        # advancing at 120 BPM from t=0, causing slides to fire before any
        # music is detected.
        self._has_ever_matched = False

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
            self._has_ever_matched = True

            logger.debug(
                f"High confidence ({confidence:.2f}): using matcher beat {current_beat:.1f}, "
                f"tempo {self.last_tempo_bpm:.1f} BPM"
            )

            return current_beat, False, self.last_tempo_bpm

        else:
            # Low confidence: use inertia — but only if tracking has started.
            # Before the first confident match we hold position at beat 0 so
            # that slides do not fire before any music is detected.
            if not self._has_ever_matched:
                self.inertia_active = False
                logger.debug(
                    f"Low confidence ({confidence:.2f}): waiting for first match, holding beat 0"
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

    def reset(self):
        """Reset inertia engine (for movement changes)."""
        self.last_confident_beat = 0.0
        self.last_confident_time = time.time()
        self.last_tempo_bpm = 120.0
        self.inertia_active = False
        self._has_ever_matched = False

    def __repr__(self) -> str:
        return (
            f"InertiaEngine(threshold={self.confidence_threshold}, "
            f"last_tempo={self.last_tempo_bpm:.1f} BPM, inertia={self.inertia_active})"
        )
