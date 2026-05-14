#!/usr/bin/env python3
"""
audio_level.py - Background Microphone RMS Monitor

pymatchmaker's DTW score follower can keep advancing the alignment position
even when the microphone is silent (its score-driven tempo prior keeps the
position moving).  Our derived "confidence" then briefly looks plausible,
the InertiaEngine locks in, and slides start drifting forward despite no
music being played.

This module opens a *separate* sounddevice InputStream just to measure the
mic's RMS level, exposing ``is_active()`` for the state-sync loop to gate
the matcher's confidence on.  When the mic is quiet, we force confidence
to 0 regardless of what the matcher reports.
"""

from __future__ import annotations

import logging
import math
import threading
from typing import Optional, Union

import numpy as np

logger = logging.getLogger(__name__)


class AudioLevelMonitor:
    """Background-thread RMS meter for an input device.

    Lazy-imports sounddevice so that ``import`` paths still work on systems
    where sounddevice cannot be loaded (e.g. CI).  ``start()`` is a no-op
    that just logs a warning if the device can't be opened — the rest of
    the application keeps working without the silence gate.
    """

    def __init__(
        self,
        threshold_db: float = -40.0,
        sample_rate: int = 16000,
        block_size: int = 1024,
        device: Optional[Union[int, str]] = None,
    ) -> None:
        self.threshold_db = float(threshold_db)
        self.sample_rate = int(sample_rate)
        self.block_size = int(block_size)
        self.device = device

        self._lock = threading.Lock()
        self._current_db: float = -math.inf
        self._stream = None  # sounddevice.InputStream when running
        self._available = False  # set True after a successful start()

    # ---------------------------------------------------------- lifecycle
    def start(self) -> None:
        """Open the input stream.  Failures are logged, not raised — the
        rest of the system still functions (just without the silence gate).

        We catch ``BaseException`` (not just ``Exception``) around the
        sounddevice import because PortAudio's C-level initialization can
        raise SystemExit-like errors or library-specific exceptions whose
        class hierarchy we do not control.  Suppressing them lets the
        rest of the app run; the worst that happens is the silence gate
        is disabled.
        """
        try:
            import sounddevice as sd  # type: ignore
        except BaseException as exc:
            logger.warning(
                "AudioLevelMonitor: sounddevice import failed (%s: %s); "
                "silence gate disabled. The app will continue without it.",
                type(exc).__name__, exc,
            )
            return

        try:
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                blocksize=self.block_size,
                callback=self._callback,
                device=self.device,
                dtype="float32",
            )
            self._stream.start()
            self._available = True
            logger.info(
                "AudioLevelMonitor started (threshold=%.1f dBFS, device=%s, sr=%d)",
                self.threshold_db, self.device, self.sample_rate,
            )
        except BaseException as exc:
            logger.warning(
                "AudioLevelMonitor: failed to open input stream (%s: %s); "
                "silence gate disabled. The app will continue but may be more "
                "prone to false tracking lock-in during silence.",
                type(exc).__name__, exc,
            )
            self._stream = None
            self._available = False

    def stop(self) -> None:
        if self._stream is None:
            return
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:  # noqa: BLE001
            logger.exception("AudioLevelMonitor: error stopping stream")
        finally:
            self._stream = None
            self._available = False
            logger.info("AudioLevelMonitor stopped")

    # ------------------------------------------------------------- query
    def is_available(self) -> bool:
        """True iff the input stream was opened successfully."""
        return self._available

    def is_active(self) -> bool:
        """True iff the most recent block was above the silence threshold.

        If the monitor is unavailable (failed to open), returns True so
        that callers fall back to trusting the matcher's confidence.
        """
        if not self._available:
            return True
        with self._lock:
            return self._current_db > self.threshold_db

    def get_level_db(self) -> float:
        """Return the most recently measured level in dBFS."""
        with self._lock:
            return self._current_db

    # ----------------------------------------------------------- private
    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ARG002 — sd signature
        if status:
            # XRuns etc. are common and harmless; only debug-log them.
            logger.debug("AudioLevelMonitor callback status: %s", status)

        if indata.size == 0:
            return

        # indata is shape (frames, channels); collapse to mono float32
        samples = indata.reshape(-1).astype(np.float32, copy=False)
        rms = float(np.sqrt(np.mean(samples * samples)))
        # Map RMS → dBFS. 1e-10 floor avoids -inf when truly silent.
        db = 20.0 * math.log10(max(rms, 1e-10))

        with self._lock:
            self._current_db = db

    def __repr__(self) -> str:
        return (
            f"AudioLevelMonitor(threshold={self.threshold_db:.1f} dBFS, "
            f"current={self.get_level_db():.1f} dBFS, available={self._available})"
        )
