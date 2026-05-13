#!/usr/bin/env python3
"""
score_mapper.py - Partitura-based Beat ↔ Measure Conversion

Converts continuous beat counts (from pymatchmaker DTW) to measure numbers,
accounting for variable time signatures (3/4, 4/4, 5/8, etc.).

Also provides beat-within-measure calculations for detailed position tracking.
"""

import bisect
import logging
from typing import Tuple

import partitura

logger = logging.getLogger(__name__)


class ScoreMapper:
    """
    Maps between continuous beat positions and measure numbers.

    Handles:
    - Variable time signatures (anacrusis, tempo changes)
    - Accurate beat accumulation across measures
    - Binary search for fast beat→measure lookup

    Example:
        mapper = ScoreMapper("guide_mv1.xml")
        measure = mapper.beat_to_measure(45.5)  # Returns measure number
        beat_in_measure = mapper.get_beat_in_measure(45.5)  # Returns 0.5
    """

    def __init__(self, xml_path: str):
        """
        Load MusicXML and build cumulative beat map.

        Args:
            xml_path: Path to MusicXML file

        Raises:
            FileNotFoundError: If XML file doesn't exist
            Exception: If partitura fails to parse
        """
        logger.info(f"Loading score: {xml_path}")
        loaded = partitura.load_musicxml(xml_path)

        # partitura may return a list of parts or a Score object
        if isinstance(loaded, list):
            # Use first part
            self.part = loaded[0] if loaded else None
        else:
            # Score object - get first part
            parts = list(loaded.parts) if hasattr(loaded, 'parts') else [loaded]
            self.part = parts[0] if parts else loaded

        # Map: sorted list of beat positions → (measure_number, beats_in_measure)
        self.beat_thresholds = []      # Cumulative beats at start of each measure
        self.measure_info = {}         # beat_threshold → (measure_num, beats_per_measure)

        self._build_beat_map()
        logger.info(f"Built beat map with {len(self.beat_thresholds)} measures")

    def _build_beat_map(self):
        """
        Iterate through all measures, accumulate beats, build lookup map.

        Time signature interpretation:
        - (4, 4) = 4 quarter notes = 4.0 beats
        - (3, 4) = 3 quarter notes = 3.0 beats
        - (5, 8) = 5 eighth notes = 2.5 beats (5 * 0.5)
        - General: (num / denom) * 4 quarter notes per measure
        """
        cumulative_beat = 0.0

        if self.part is None:
            logger.warning("No part available to build beat map")
            return

        # Get measures from part
        measures = list(self.part.iter_all(partitura.score.Measure)) if hasattr(self.part, 'iter_all') else []

        if not measures:
            # Fallback: try to access measures directly
            measures = getattr(self.part, 'measures', [])
            if callable(measures):
                measures = measures()

        # Sort measures by start time
        sorted_measures = sorted(
            measures,
            key=lambda m: m.start.t if hasattr(m, 'start') and hasattr(m.start, 't') else 0
        )

        for measure in sorted_measures:
            measure_num = getattr(measure, 'number', len(self.beat_thresholds) + 1)

            # Get time signature - try different access patterns
            ts = None
            if hasattr(measure, 'time_signature'):
                ts = measure.time_signature
            elif hasattr(self.part, 'time_signature_map'):
                # Get time signature at measure start
                start_t = measure.start.t if hasattr(measure, 'start') and hasattr(measure.start, 't') else 0
                ts_map = self.part.time_signature_map
                if ts_map:
                    ts = ts_map(start_t) if callable(ts_map) else ts_map.get(start_t)

            if ts is None:
                ts = (4, 4)  # Default fallback

            # Handle different time signature formats
            if hasattr(ts, 'beats') and hasattr(ts, 'beat_type'):
                num, denom = ts.beats, ts.beat_type
            elif isinstance(ts, tuple):
                num, denom = ts
            else:
                num, denom = 4, 4

            # Calculate beats in this measure (quarterLength = 1 beat)
            beats_per_measure = (num / denom) * 4.0

            # Store mapping
            self.beat_thresholds.append(cumulative_beat)
            self.measure_info[cumulative_beat] = (measure_num, beats_per_measure)

            logger.debug(
                f"Measure {measure_num}: beat {cumulative_beat:.1f}→{cumulative_beat + beats_per_measure:.1f} "
                f"({num}/{denom}, {beats_per_measure:.1f} beats)"
            )

            cumulative_beat += beats_per_measure

    def beat_to_measure(self, beat_count: float) -> int:
        """
        Convert continuous beat count to measure number.

        Uses binary search for O(log n) lookup.

        Args:
            beat_count: Continuous position in beats (can be fractional)

        Returns:
            Measure number (1-indexed)
        """
        if not self.beat_thresholds:
            return 1

        # Find largest beat threshold ≤ beat_count
        idx = bisect.bisect_right(self.beat_thresholds, beat_count) - 1

        if idx < 0:
            # Before first measure (shouldn't happen in normal operation)
            return self.measure_info[self.beat_thresholds[0]][0]

        if idx >= len(self.beat_thresholds):
            # After last measure
            last_beat = self.beat_thresholds[-1]
            return self.measure_info[last_beat][0]

        beat_threshold = self.beat_thresholds[idx]
        measure_num, _ = self.measure_info[beat_threshold]
        return measure_num

    def get_beat_in_measure(self, beat_count: float) -> float:
        """
        Get beat offset within current measure (0.0 ≤ result < beats_per_measure).

        Args:
            beat_count: Continuous position in beats

        Returns:
            Beat within measure (0-indexed, e.g., 0.0, 1.5, 2.0, ...)
        """
        if not self.beat_thresholds:
            return beat_count

        idx = bisect.bisect_right(self.beat_thresholds, beat_count) - 1

        if idx < 0:
            return 0.0

        beat_threshold = self.beat_thresholds[idx]
        return beat_count - beat_threshold

    def get_total_beats(self) -> float:
        """
        Get total duration of score in beats.

        Returns:
            Total beat count
        """
        if not self.beat_thresholds:
            return 0.0

        last_threshold = self.beat_thresholds[-1]
        if last_threshold in self.measure_info:
            _, beats_in_last = self.measure_info[last_threshold]
            return last_threshold + beats_in_last

        return last_threshold

    def beat_range_for_measure(self, measure_num: int) -> Tuple[float, float]:
        """
        Get beat range [start, end) for a given measure.

        Args:
            measure_num: Measure number (1-indexed)

        Returns:
            (start_beat, end_beat)
        """
        start_beat = None
        end_beat = None

        for beat_threshold, (mnum, beats_per_measure) in self.measure_info.items():
            if mnum == measure_num:
                start_beat = beat_threshold
                end_beat = beat_threshold + beats_per_measure
                break

        if start_beat is None:
            logger.warning(f"Measure {measure_num} not found in score")
            return (0.0, 0.0)

        return (start_beat, end_beat)

    def __repr__(self) -> str:
        total_beats = self.get_total_beats()
        return f"ScoreMapper(measures={len(self.beat_thresholds)}, total_beats={total_beats:.1f})"
