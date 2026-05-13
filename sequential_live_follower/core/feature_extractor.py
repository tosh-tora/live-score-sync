#!/usr/bin/env python3
"""
feature_extractor.py - Chroma Feature Extraction

Consumes raw audio frames from queue, extracts Chroma features using librosa,
and pushes normalized Chroma vectors to output queue.

Chroma = 12-dimensional pitch class feature (one for each semitone, C-B).
Normalized to [0, 1] range for consistency with pymatchmaker.
"""

import logging
import numpy as np
import librosa
import queue
import threading
from typing import Optional

logger = logging.getLogger(__name__)


class FeatureExtractor(threading.Thread):
    """
    Audio-to-Chroma pipeline in background thread.

    Buffers incoming audio frames, extracts Chroma features at regular intervals,
    and pushes to output queue.
    """

    def __init__(
        self,
        audio_queue: queue.Queue,
        feature_queue: queue.Queue,
        sample_rate: int = 44100,
        chroma_hop_length: int = 512,
        chroma_bins: int = 12
    ):
        """
        Initialize feature extractor.

        Args:
            audio_queue: Input queue (raw audio frames)
            feature_queue: Output queue (Chroma vectors)
            sample_rate: Sample rate in Hz
            chroma_hop_length: Hop length for Chroma extraction (~11.6ms at 44.1kHz)
            chroma_bins: Number of Chroma bins (always 12)
        """
        super().__init__(daemon=True)
        self.audio_queue = audio_queue
        self.feature_queue = feature_queue
        self.sr = sample_rate
        self.hop_length = chroma_hop_length
        self.chroma_bins = chroma_bins

        self._stop_event = threading.Event()

        logger.info(
            f"FeatureExtractor initialized: {sample_rate}Hz, "
            f"hop={chroma_hop_length} (~{1000*chroma_hop_length/sample_rate:.1f}ms), "
            f"bins={chroma_bins}"
        )

    def run(self):
        """
        Main thread loop: consume audio, extract Chroma, push features.

        Accumulates frames in a buffer, extracts Chroma when buffer is ready.
        """
        try:
            buffer = np.array([], dtype=np.float32)
            frame_count = 0

            while not self._stop_event.is_set():
                try:
                    # Get audio frame with timeout
                    frame = self.audio_queue.get(timeout=1.0)
                    buffer = np.concatenate([buffer, frame])
                    frame_count += 1

                    # Extract Chroma when buffer has enough samples
                    # librosa.feature.chroma_cqt needs sufficient samples (at least 2048 for default n_fft)
                    if len(buffer) >= 2048:
                        chroma = self._extract_chroma(buffer)

                        # Push to output queue
                        try:
                            self.feature_queue.put(chroma, block=False)
                        except queue.Full:
                            logger.debug("Feature queue full, dropping Chroma")

                        # Shift buffer by hop_length for next extraction
                        buffer = buffer[self.hop_length:]

                except queue.Empty:
                    continue
                except Exception as e:
                    logger.error(f"Feature extraction error: {type(e).__name__}: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"FeatureExtractor thread error: {type(e).__name__}: {e}", exc_info=True)

        finally:
            logger.info(f"FeatureExtractor stopped (processed {frame_count} frames)")

    def _extract_chroma(self, audio_data: np.ndarray) -> np.ndarray:
        """
        Extract Chroma features from audio using librosa.

        Args:
            audio_data: Raw audio samples (float32)

        Returns:
            Normalized 12-D Chroma vector [0, 1]
        """
        try:
            # Use chroma_stft instead of chroma_cqt to avoid warnings
            # and have explicit control over n_fft
            chroma = librosa.feature.chroma_stft(
                y=audio_data,
                sr=self.sr,
                hop_length=self.hop_length,
                n_fft=512  # Smaller n_fft to match typical buffer sizes
            )

            # Average across time axis (frames → single vector)
            chroma_mean = np.mean(chroma, axis=1)

            # Normalize to [0, 1]
            max_val = np.max(chroma_mean)
            if max_val > 1e-9:
                chroma_norm = chroma_mean / max_val
            else:
                chroma_norm = chroma_mean

            return chroma_norm.astype(np.float32)

        except Exception as e:
            logger.error(f"Error extracting Chroma: {e}")
            # Return zero vector on error
            return np.zeros(self.chroma_bins, dtype=np.float32)

    def stop(self):
        """Stop feature extraction thread."""
        self._stop_event.set()

    def is_running(self) -> bool:
        """Check if extractor is still running."""
        return not self._stop_event.is_set()

    def __repr__(self) -> str:
        return (
            f"FeatureExtractor({self.sr}Hz, hop={self.hop_length}, "
            f"chroma={self.chroma_bins}D, running={self.is_running()})"
        )
