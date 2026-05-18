#!/usr/bin/env python3
"""
audio_recorder.py - Tee'd Mic Capture for Diagnostic Replay

Saves the raw microphone input to a .wav file so we can replay the
exact audio that triggered an unwanted score advance after the fact.
Used together with ``DiagLogger`` to correlate "what the mic heard" with
"what the matcher / inertia engine did".

Design:

- Does NOT open its own audio stream.  pymatchmaker and AudioLevelMonitor
  already share the input device, and opening a third stream against the
  same device fails on Windows / some Linux configurations.  Instead,
  ``on_block`` is registered as an ``AudioLevelMonitor`` listener and
  receives a copy of every audio block.

- Disk I/O happens on a daemon writer thread; ``on_block`` only pushes
  the block onto a ``queue.Queue``.  This matches the policy used by the
  monitor's callback (must never block the audio thread).

- Uses the stdlib ``wave`` module (16-bit PCM) instead of ``soundfile``
  so we don't add a new dependency.  Float32 → int16 conversion is fine
  for diagnostic playback; we lose a few dB of dynamic range but keep
  full intelligibility of speech / instrument noise.

- File is opened in 'wb' mode upfront and finalized in ``close()`` —
  the ``wave`` module writes the header on close, so a hard crash
  truncates to a header-less .wav.  Acceptable for a diagnostic tool;
  the CSV log captures the events even if the .wav is corrupted.
"""

from __future__ import annotations

import logging
import queue
import threading
import wave
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

# Sentinel for the writer thread shutdown — see diag_logger for the same
# pattern.  Module-level identity check.
_SHUTDOWN = object()

# 16-bit PCM is the most widely supported wav subformat and gives ~96 dB
# of dynamic range — plenty to capture mic-level speech and noise without
# clipping artifacts on playback.
_SAMPLE_WIDTH_BYTES = 2  # int16


class AudioRecorder:
    """Background-thread WAV writer for diagnostic mic capture.

    Typical usage::

        rec = AudioRecorder(Path("./diag/audio_20260519_143012.wav"),
                            sample_rate=16000)
        monitor.add_block_listener(rec.on_block)
        ...
        rec.close()
    """

    def __init__(self, out_path: Path, sample_rate: int) -> None:
        self.out_path = Path(out_path)
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self.sample_rate = int(sample_rate)

        self._queue: queue.Queue = queue.Queue()
        self._dropped_blocks = 0
        self._written_frames = 0
        self._sample_rate_warned = False

        self._fp = wave.open(str(self.out_path), "wb")
        self._fp.setnchannels(1)
        self._fp.setsampwidth(_SAMPLE_WIDTH_BYTES)
        self._fp.setframerate(self.sample_rate)

        self._stopped = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="audio-recorder", daemon=True
        )
        self._thread.start()

        logger.info(
            "AudioRecorder started: %s (sr=%d Hz, 16-bit mono PCM)",
            self.out_path, self.sample_rate,
        )

    def on_block(self, samples: np.ndarray, sample_rate: int) -> None:
        """Listener entry point for ``AudioLevelMonitor.add_block_listener``.

        Called on the sounddevice callback thread; must not block.  We
        push to an unbounded queue (a few MB of RAM under burst is much
        better than dropping audio frames we'd need to hear back).
        """
        if self._stopped.is_set():
            return
        # If the monitor's sample rate doesn't match what we opened the
        # wav with, the file would play back at the wrong speed.  We
        # warn once and keep writing — the user can still hear what
        # happened, just at slightly off pitch.
        if sample_rate != self.sample_rate and not self._sample_rate_warned:
            logger.warning(
                "AudioRecorder sample rate mismatch: opened at %d Hz but "
                "blocks arriving at %d Hz; .wav playback speed will be off",
                self.sample_rate, sample_rate,
            )
            self._sample_rate_warned = True

        # Convert float32 [-1, 1] → int16.  np.clip first so a momentary
        # over-1.0 sample (clipping at the mic) doesn't wrap to negative
        # in the int16 cast.
        clipped = np.clip(samples, -1.0, 1.0)
        int16 = (clipped * 32767.0).astype(np.int16, copy=False)
        # We must copy the int16 buffer because the producer (sounddevice
        # callback) reuses memory between calls.  ``.copy()`` here also
        # decouples lifetime from the original float32 array.
        self._queue.put(int16.copy())

    def close(self, timeout: float = 2.0) -> None:
        """Drain the queue and finalize the .wav file.

        Safe to call multiple times.  Blocks up to ``timeout`` for the
        writer thread to flush remaining audio.  The wav header is only
        valid after ``wave.close()`` runs, so a hard kill before this
        leaves a partially-written file.
        """
        if self._stopped.is_set():
            return
        self._stopped.set()
        self._queue.put(_SHUTDOWN)
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            logger.warning(
                "AudioRecorder writer did not exit within %.1fs; "
                ".wav will be truncated to %d frames",
                timeout, self._written_frames,
            )
        try:
            self._fp.close()
        except Exception:  # noqa: BLE001 — wave can raise odd errors
            logger.exception("AudioRecorder: error finalizing %s", self.out_path)
        duration_sec = self._written_frames / self.sample_rate if self.sample_rate else 0
        logger.info(
            "AudioRecorder closed: %s (%d frames, %.1f s, %d dropped block(s))",
            self.out_path, self._written_frames, duration_sec, self._dropped_blocks,
        )

    # ----------------------------------------------------------- worker
    def _run(self) -> None:
        while True:
            try:
                block = self._queue.get()
            except Exception:  # noqa: BLE001
                continue
            if block is _SHUTDOWN:
                break
            self._write_block(block)

        # Drain anything still queued after the sentinel.
        try:
            while True:
                block = self._queue.get_nowait()
                if block is _SHUTDOWN:
                    continue
                self._write_block(block)
        except queue.Empty:
            pass

    def _write_block(self, block: np.ndarray) -> None:
        try:
            self._fp.writeframes(block.tobytes())
            self._written_frames += int(block.shape[0])
        except (OSError, wave.Error, ValueError):
            self._dropped_blocks += 1
