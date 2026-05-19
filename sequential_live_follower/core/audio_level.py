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
from typing import Callable, List, Optional, Union

import numpy as np

# Type alias for raw audio block listeners.  Receives a 1-D float32 mono
# array of samples plus the sample rate.  Listeners run inside the
# sounddevice callback thread, so they must be cheap and never raise — the
# monitor wraps each call in try/except for safety, but a slow listener
# would still starve the audio thread and cause underruns.
AudioBlockListener = Callable[[np.ndarray, int], None]

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
        flatness_threshold: float = 0.25,
        sample_rate: int = 16000,
        block_size: int = 1024,
        device: Optional[Union[int, str]] = None,
    ) -> None:
        self.threshold_db = float(threshold_db)
        # Spectral flatness threshold for the "musical content" gate.
        # Tonal sounds (instruments, voice) sit around 0.05-0.15; broadband
        # noise (room hum, breath, taps, claps) climbs to 0.4-0.8.  We treat
        # blocks with flatness >= this value as non-musical and gate them
        # the same way the silence gate handles too-quiet audio.
        self.flatness_threshold = float(flatness_threshold)
        self.sample_rate = int(sample_rate)
        self.block_size = int(block_size)
        self.device = device

        self._lock = threading.Lock()
        self._current_db: float = -math.inf
        # Flatness is in [0, 1].  Initialise to 1.0 ("worst case = noise")
        # so the gate stays closed until we get a real measurement; this
        # prevents the very first state-sync tick (before any audio has
        # arrived) from briefly reporting the audio as musical.
        self._current_flatness: float = 1.0
        self._stream = None  # sounddevice.InputStream when running
        self._available = False  # set True after a successful start()

        # Optional listeners that get the raw audio block on every callback.
        # Used by the diagnostic AudioRecorder to tee the input stream into a
        # .wav file without opening a third audio device.  Empty by default
        # so the production code path is zero-overhead.
        self._listeners: List[AudioBlockListener] = []

    # ---------------------------------------------------------- lifecycle
    def start(self, timeout_sec: float = 10.0) -> None:
        """Open the input stream off the main thread with a timeout.

        On a healthy host both the ``import sounddevice`` and the
        ``InputStream`` open complete in well under a second.  In real
        deployments (WSL2 + WSLg) we've seen the import hang for minutes
        when PortAudio's PulseAudio backend was in a bad state, which
        makes the whole app appear frozen.  We run the work on a daemon
        thread and give up after ``timeout_sec`` so the rest of the app
        always comes up — the silence gate is simply disabled in that
        case.

        We also catch ``BaseException`` because PortAudio's C-level
        initialization can raise SystemExit-like errors or
        library-specific exceptions whose class hierarchy we do not
        control.
        """
        import threading

        outcome: dict = {"done": False, "stream": None, "exc": None}

        def _worker() -> None:
            try:
                import sounddevice as sd  # type: ignore
            except BaseException as exc:
                outcome["exc"] = exc
                outcome["done"] = True
                return
            try:
                stream = sd.InputStream(
                    samplerate=self.sample_rate,
                    channels=1,
                    blocksize=self.block_size,
                    callback=self._callback,
                    device=self.device,
                    dtype="float32",
                )
                stream.start()
                outcome["stream"] = stream
            except BaseException as exc:
                outcome["exc"] = exc
            finally:
                outcome["done"] = True

        worker = threading.Thread(
            target=_worker, name="audio-monitor-init", daemon=True
        )
        worker.start()
        worker.join(timeout=timeout_sec)

        if not outcome["done"]:
            logger.warning(
                "AudioLevelMonitor: initialization did not complete within %.1fs; "
                "PortAudio/PulseAudio may be in a bad state. Try `wsl --shutdown` "
                "from PowerShell to reset audio. Silence gate disabled.",
                timeout_sec,
            )
            self._stream = None
            self._available = False
            return

        if outcome["exc"] is not None:
            exc = outcome["exc"]
            logger.warning(
                "AudioLevelMonitor: failed to start (%s: %s); silence gate disabled. "
                "The app will continue without it.",
                type(exc).__name__, exc,
            )
            self._stream = None
            self._available = False
            return

        self._stream = outcome["stream"]
        self._available = self._stream is not None
        if self._available:
            logger.info(
                "AudioLevelMonitor started (silence=%.1f dBFS, flatness=%.2f, "
                "device=%s, sr=%d)",
                self.threshold_db, self.flatness_threshold,
                self.device, self.sample_rate,
            )

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

    def is_musical(self) -> bool:
        """True iff the most recent block looks like musical (tonal) audio.

        Decided by ``spectral_flatness < flatness_threshold``.  Pure tones
        and orchestral instruments register far below 0.25; speech, room
        noise, taps, coughs, and rustling paper push flatness above it.

        If the monitor is unavailable, returns True so callers fall back
        to trusting the matcher (same policy as ``is_active``).
        """
        if not self._available:
            return True
        with self._lock:
            return self._current_flatness < self.flatness_threshold

    def get_level_db(self) -> float:
        """Return the most recently measured level in dBFS."""
        with self._lock:
            return self._current_db

    def get_spectral_flatness(self) -> float:
        """Return the most recently measured spectral flatness (range 0-1)."""
        with self._lock:
            return self._current_flatness

    # -------------------------------------------------------- listeners
    def add_block_listener(self, fn: AudioBlockListener) -> None:
        """Register ``fn(samples, sample_rate)`` to receive every audio block.

        Used to tee the input stream into a second consumer (e.g. a .wav
        recorder for diagnostics) without opening a third audio device,
        which would conflict on Windows / some Linux configurations.

        The listener runs inside the sounddevice callback thread.  It MUST
        be cheap (queue.put_nowait is fine; disk I/O is not) and is wrapped
        in try/except so any error is logged and swallowed rather than
        killing the monitor.
        """
        with self._lock:
            self._listeners.append(fn)

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

        # Spectral flatness on the power spectrum:
        #   geometric_mean(|FFT|^2) / arithmetic_mean(|FFT|^2),
        # which is 1.0 for white noise and approaches 0 for pure tones.
        # Implementation notes:
        #   1. Apply a Hann window before the FFT.  Without it, a tone
        #      that doesn't sit exactly on an FFT bin smears across many
        #      bins (spectral leakage) and reads as much less tonal than
        #      it really is — a pure 440 Hz sine then scores flatness ≈
        #      0.3, comparable to mild noise.
        #   2. Use the *power* spectrum (|FFT|^2) rather than magnitude.
        #      This matches librosa.feature.spectral_flatness's default
        #      (power=2.0) and gives a much sharper separation between
        #      tonal and noisy signals (tone ~0.001 vs noise ~0.5).
        #   3. Skip the DC bin so a non-zero offset doesn't dominate the
        #      arithmetic mean.
        #   4. Bail out (flatness=1.0, "looks like noise") when the
        #      block is effectively silent — log(near-zero) is unstable
        #      and the result is moot anyway since the silence gate
        #      will already close.
        # We use raw numpy rather than librosa.feature.spectral_flatness
        # to avoid an import on the audio callback hot path; the
        # windowed FFT below is ~50 µs on a 1024-sample block.
        if rms < 1e-6:
            flatness = 1.0
        else:
            windowed = samples * np.hanning(len(samples)).astype(np.float32)
            spectrum = np.abs(np.fft.rfft(windowed))[1:]  # drop DC
            power = spectrum * spectrum
            # Add a small floor before log to keep -inf out of the
            # geometric mean when a bin is exactly zero.
            log_pow = np.log(power + 1e-20)
            geo_mean = float(np.exp(np.mean(log_pow)))
            arith_mean = float(np.mean(power)) + 1e-20
            flatness = geo_mean / arith_mean
            # Clamp into [0, 1] — numerical noise can push it slightly
            # outside on tonal input.
            if flatness < 0.0:
                flatness = 0.0
            elif flatness > 1.0:
                flatness = 1.0

        with self._lock:
            self._current_db = db
            self._current_flatness = flatness
            listeners = list(self._listeners)  # snapshot to release the lock

        # Fan out raw samples to any tee'd consumers (e.g. AudioRecorder).
        # We copy the buffer because sounddevice reuses the underlying
        # memory for the next callback and listeners typically queue the
        # array for later disk I/O.
        if listeners:
            samples_copy = samples.copy()
            for fn in listeners:
                try:
                    fn(samples_copy, self.sample_rate)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "AudioLevelMonitor: block listener %r raised; dropping block",
                        fn,
                    )

    def __repr__(self) -> str:
        return (
            f"AudioLevelMonitor(silence={self.threshold_db:.1f} dBFS, "
            f"flatness_threshold={self.flatness_threshold:.2f}, "
            f"current_db={self.get_level_db():.1f} dBFS, "
            f"current_flatness={self.get_spectral_flatness():.3f}, "
            f"available={self._available})"
        )
