#!/usr/bin/env python3
"""
audio_capturer.py - Real-Time Audio Stream Capture

Captures audio from microphone input using sounddevice (or PyAudio) in a background thread.
Pushes raw PCM frames to a queue for downstream feature extraction.

Note: sounddevice/PyAudio is optional. If not installed, uses mock mode for testing.
"""

import logging
import numpy as np
import queue
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# Try to import audio libraries (prefer sounddevice, fall back to PyAudio)
AUDIO_BACKEND = None

try:
    import sounddevice as sd
    AUDIO_BACKEND = "sounddevice"
except ImportError:
    try:
        import pyaudio
        AUDIO_BACKEND = "pyaudio"
    except ImportError:
        try:
            import pyaudiowpatch as pyaudio
            AUDIO_BACKEND = "pyaudio"
        except ImportError:
            pass

if AUDIO_BACKEND is None:
    logger.warning("No audio library installed. Audio capture will use mock mode.")
else:
    logger.info(f"Using audio backend: {AUDIO_BACKEND}")


class AudioCapturer(threading.Thread):
    """
    Non-blocking microphone stream capture.

    Runs in daemon thread. Continuously reads audio frames from audio stream
    and pushes them to a queue. Drops frames if queue is full (prioritizes
    real-time timing over completeness).

    Supports multiple backends: sounddevice (preferred), PyAudio, or mock mode.
    """

    def __init__(
        self,
        audio_queue: queue.Queue,
        sample_rate: int = 44100,
        chunk_size: int = 2048,
        channels: int = 1
    ):
        """
        Initialize audio capturer.

        Args:
            audio_queue: Queue to push audio frames to
            sample_rate: Sample rate in Hz (default 44.1 kHz)
            chunk_size: Frames per buffer (default 2048, ~46ms at 44.1kHz)
            channels: Number of channels (default 1 = mono)
        """
        super().__init__(daemon=True)
        self.queue = audio_queue
        self.sample_rate = sample_rate
        self.chunk_size = chunk_size
        self.channels = channels
        self.backend = AUDIO_BACKEND

        if self.backend is None:
            logger.warning("Running in MOCK mode (no audio library available)")
            self.pa = None
            self.stream = None
        elif self.backend == "pyaudio":
            self.pa = pyaudio.PyAudio()
            self.stream: Optional = None
        else:  # sounddevice
            self.pa = None
            self.stream = None

        self._stop_event = threading.Event()

        logger.info(
            f"AudioCapturer initialized: {sample_rate}Hz, {channels}ch, {chunk_size} frames/chunk "
            f"(backend: {self.backend or 'mock'})"
        )

    def run(self):
        """
        Main thread loop: capture audio and queue frames.

        Runs until stop() is called.
        """
        if self.backend is None:
            self._run_mock()
        elif self.backend == "sounddevice":
            self._run_sounddevice()
        else:
            self._run_pyaudio()

    def _run_mock(self):
        """Mock audio capture for testing (generates synthetic data)."""
        logger.info("Mock audio capture started")
        frame_num = 0

        try:
            while not self._stop_event.is_set():
                try:
                    # Generate synthetic audio (sine wave at 440 Hz)
                    t = np.arange(self.chunk_size) / self.sample_rate + frame_num * self.chunk_size / self.sample_rate
                    audio_data = (0.1 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)

                    try:
                        self.queue.put(audio_data, block=False)
                    except queue.Full:
                        logger.debug("Mock audio queue full, dropping frame")

                    frame_num += 1

                    # Sleep to simulate real-time (no CPU spin)
                    threading.Event().wait(self.chunk_size / self.sample_rate * 0.5)

                except Exception as e:
                    logger.error(f"Mock audio generation error: {type(e).__name__}: {e}", exc_info=True)
                    break

        finally:
            logger.info("Mock audio capture stopped")

    def _run_sounddevice(self):
        """Real audio capture using sounddevice."""
        try:
            logger.info("Starting sounddevice audio stream")

            def audio_callback(indata, frames, time_info, status):
                """Callback for sounddevice stream."""
                if status:
                    logger.debug(f"sounddevice status: {status}")
                try:
                    # Convert to float32 and flatten if needed
                    audio_data = indata[:, 0].astype(np.float32) if indata.ndim > 1 else indata.astype(np.float32)
                    self.queue.put(audio_data.copy(), block=False)
                except queue.Full:
                    logger.debug("Audio queue full, dropping frame")

            with sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                blocksize=self.chunk_size,
                dtype=np.float32,
                callback=audio_callback
            ):
                logger.info("Audio stream opened (sounddevice)")
                while not self._stop_event.is_set():
                    self._stop_event.wait(0.1)

        except Exception as e:
            logger.error(f"sounddevice stream error: {type(e).__name__}: {e}", exc_info=True)

        finally:
            logger.info("sounddevice audio capture stopped")

    def _run_pyaudio(self):
        """Real audio capture using PyAudio."""
        try:
            self.stream = self.pa.open(
                format=pyaudio.paFloat32,
                channels=self.channels,
                rate=self.sample_rate,
                input=True,
                frames_per_buffer=self.chunk_size,
                exception_on_overflow=False  # Drop old frames on overflow
            )

            logger.info("Audio stream opened (PyAudio)")

            while not self._stop_event.is_set():
                try:
                    # Read chunk from microphone
                    frame_bytes = self.stream.read(
                        self.chunk_size,
                        exception_on_overflow=False
                    )

                    # Convert bytes to numpy float32
                    audio_data = np.frombuffer(frame_bytes, dtype=np.float32)

                    # Push to queue (non-blocking, drop if full)
                    try:
                        self.queue.put(audio_data, block=False)
                    except queue.Full:
                        logger.debug("Audio queue full, dropping frame")
                        pass

                except Exception as e:
                    logger.error(f"Error reading audio: {type(e).__name__}: {e}", exc_info=True)
                    break

        except Exception as e:
            logger.error(f"Audio stream error: {type(e).__name__}: {e}", exc_info=True)

        finally:
            self.stop()

    def stop(self):
        """Stop audio capture and release resources."""
        self._stop_event.set()

        if self.stream:
            try:
                self.stream.stop_stream()
                self.stream.close()
                logger.info("Audio stream closed")
            except Exception as e:
                logger.error(f"Error closing stream: {type(e).__name__}: {e}", exc_info=True)

        if self.pa:
            try:
                self.pa.terminate()
                logger.info("PyAudio terminated")
            except Exception as e:
                logger.error(f"Error terminating PyAudio: {type(e).__name__}: {e}", exc_info=True)

    def is_running(self) -> bool:
        """Check if capturer is still running."""
        return not self._stop_event.is_set()

    def __repr__(self) -> str:
        return (
            f"AudioCapturer({self.sample_rate}Hz, {self.channels}ch, "
            f"chunk={self.chunk_size}, backend={self.backend or 'mock'}, running={self.is_running()})"
        )
