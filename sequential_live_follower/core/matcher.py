#!/usr/bin/env python3
"""
matcher.py - PyMatcher Integration

Wraps pymatchmaker DTW algorithm to find current position in score
based on audio Chroma features.

Outputs beat position and confidence score for downstream trigger logic.
"""

import logging
import numpy as np
import time
from typing import Tuple, Optional

logger = logging.getLogger(__name__)


class MatchMaker:
    """
    Wrapper around pymatchmaker for real-time DTW alignment.

    Tracks current beat in score by continuously matching audio Chroma features
    against the reference score.
    """

    def __init__(self, xml_path: str):
        """
        Initialize matcher with reference score.

        Args:
            xml_path: Path to MusicXML file (reference)

        Note:
            This assumes pymatchmaker has been installed and provides
            a compatible API. Actual API details to be confirmed.
        """
        try:
            import pymatchmaker
            self.matcher = pymatchmaker.Matcher(xml_path)
        except ImportError:
            logger.warning(
                "pymatchmaker not installed. Using mock matcher for testing."
            )
            self.matcher = None

        self.xml_path = xml_path
        self.current_beat = 0.0
        self.confidence = 0.0
        self.last_update_time = time.time()

        logger.info(f"MatchMaker initialized with: {xml_path}")

    def update(self, chroma_feature: np.ndarray) -> Tuple[float, float]:
        """
        Feed Chroma feature to matcher, get current beat and confidence.

        Args:
            chroma_feature: 12-D Chroma vector (normalized [0, 1])

        Returns:
            (current_beat, confidence)
            - current_beat: float, cumulative beat position in score
            - confidence: float, [0.0, 1.0], DTW alignment quality
        """
        if self.matcher is None:
            # Mock behavior for testing (slowly advance beat)
            elapsed = time.time() - self.last_update_time
            self.current_beat += 120.0 / 60.0 * elapsed  # 120 BPM fallback
            self.confidence = 0.5  # Mock confidence
        else:
            try:
                # Call pymatchmaker matcher
                # Assumption: matcher.match(chroma) returns dict with 'beat' and 'confidence'
                result = self.matcher.match(chroma_feature)

                if isinstance(result, dict):
                    self.current_beat = result.get('beat', self.current_beat)
                    self.confidence = result.get('confidence', 0.0)
                else:
                    # If API is different, log warning
                    logger.warning(f"Unexpected matcher result type: {type(result)}")

            except Exception as e:
                logger.error(f"Matching error: {type(e).__name__}: {e}", exc_info=True)
                # Keep previous beat on error
                self.confidence = 0.0

        self.last_update_time = time.time()
        return self.current_beat, self.confidence

    def reset(self):
        """Reset matcher state (for movement changes)."""
        self.current_beat = 0.0
        self.confidence = 0.0
        self.last_update_time = time.time()

        if self.matcher is not None:
            try:
                # Reset internal state if supported
                self.matcher.reset() if hasattr(self.matcher, 'reset') else None
                logger.info("Matcher reset")
            except Exception as e:
                logger.error(f"Error resetting matcher: {type(e).__name__}: {e}", exc_info=True)

    def __repr__(self) -> str:
        return (
            f"MatchMaker(beat={self.current_beat:.1f}, "
            f"confidence={self.confidence:.2f})"
        )
