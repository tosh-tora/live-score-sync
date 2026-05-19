#!/usr/bin/env python3
"""
diag_logger.py - State-sync Diagnostic CSV Writer

Writes one CSV row per state-sync tick (20 Hz) so we can correlate the
state-sync inputs (matcher beat, confidence, mic level, freeze state,
inertia mode) after the fact.  Used to diagnose the "noise advances the
score" class of bugs by capturing exactly what each layer reported in
the moment the displayed measure jumped.

Design:

- Disk I/O happens on a daemon writer thread; ``log()`` only pushes a row
  dict to a ``queue.Queue``.  This keeps the state-sync loop (which runs
  at 20 Hz and feeds the GUI) free of synchronous disk waits.

- The queue is unbounded — under sustained burst write conditions a small
  rise in memory is preferable to dropping rows we'd need for analysis.
  At 20 Hz with ~150 bytes/row, an hour of logging is ~10 MB.

- A sentinel value (``_SHUTDOWN``) is used to terminate the writer thread
  cleanly so the file ends in a valid state.

- No third-party dependencies; uses ``csv`` and ``queue`` from stdlib.
"""

from __future__ import annotations

import csv
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

# Sentinel posted to the writer queue to signal "drain and exit".  A
# module-level singleton so identity comparison (``is``) is reliable —
# any string with the same content would otherwise be treated as data.
_SHUTDOWN = object()

# CSV column order.  Kept stable so downstream pandas/Excel analyses can
# rely on positional access if needed; new columns should be appended to
# the end.  Field names match the keys passed by ``main.py``'s state-sync
# loop to make grep-driven debugging easy.
FIELDNAMES = [
    "timestamp_iso",
    "t_rel_sec",
    "raw_beat",
    "beat",
    "measure",
    "beat_in_measure",
    "raw_conf",
    "mic_db",
    # Gating signals.  ``gate_active`` is the final OR of the two below
    # plus the mic-availability check — i.e. the actual condition that
    # forces raw_conf=0.  ``silence_gate_fired`` and ``is_musical`` are
    # split out so the CSV makes it obvious *why* the gate engaged.
    "spectral_flatness",
    "is_musical",
    "silence_gate_fired",
    "gate_active",
    "mic_available",
    "matcher_frozen",
    "matcher_frozen_frame",
    "locked_in",
    "inertia_active",
    "inertia_tempo_bpm",
    "history_len",
    "win_velocity",
    "stall_sec",
    # pymatchmaker's internal normalized DTW min cost for the most recent
    # step (captured via instrumentation in matcher.py).  Lower = better
    # match between input audio and the chosen score position.  This is the
    # raw signal a future match-quality gate can threshold on — speech and
    # tonal noise produce significantly higher values than real performance.
    # NaN when pymatchmaker isn't installed or the patch failed to attach.
    "match_cost",
]


class DiagLogger:
    """Background-thread CSV row writer.

    Typical usage::

        log = DiagLogger(Path("./diag/diag_20260519_143012.csv"))
        log.log({"raw_beat": 12.3, "raw_conf": 0.0, ...})  # 20 Hz
        ...
        log.close()  # called from main on shutdown
    """

    def __init__(self, out_path: Path) -> None:
        self.out_path = Path(out_path)
        self.out_path.parent.mkdir(parents=True, exist_ok=True)

        self._queue: queue.Queue = queue.Queue()
        self._start_time = time.time()
        self._dropped = 0  # incremented if writer fails to flush

        # Open the file in text mode with newline="" so csv writes don't
        # double-up CRLFs on Windows.  utf-8 keeps Japanese trigger notes
        # readable (the CSV doesn't include them today, but cheap insurance).
        self._fp = open(self.out_path, "w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fp, fieldnames=FIELDNAMES)
        self._writer.writeheader()
        self._fp.flush()

        self._stopped = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="diag-logger", daemon=True
        )
        self._thread.start()

        logger.info("DiagLogger started: %s", self.out_path)

    @property
    def start_time(self) -> float:
        """Wall-clock time (seconds since epoch) when the log was opened.

        Exposed so ``main`` can stamp ``t_rel_sec`` from the same origin
        across multiple log targets (e.g. if we add a second JSON log).
        """
        return self._start_time

    def log(self, row: Mapping[str, Any]) -> None:
        """Enqueue one row for the writer thread.

        ``row`` should contain the ``FIELDNAMES`` keys.  Missing keys are
        written as empty cells; extras are silently dropped by DictWriter.
        Numeric values are formatted by the writer thread.
        """
        if self._stopped.is_set():
            return
        # We don't need monotonic timing for the CSV — wall-clock and a
        # relative offset are both useful for cross-referencing with the
        # WAV recording.  ``t_rel_sec`` is set here (not in the writer
        # thread) so it represents call time, not flush time.
        out: dict = dict(row)
        now = time.time()
        out.setdefault("timestamp_iso", _iso_now(now))
        out.setdefault("t_rel_sec", f"{now - self._start_time:.4f}")
        self._queue.put(out)

    def close(self, timeout: float = 2.0) -> None:
        """Drain the queue and close the file.

        Safe to call multiple times.  Blocks up to ``timeout`` for the
        writer thread to flush remaining rows.
        """
        if self._stopped.is_set():
            return
        self._stopped.set()
        self._queue.put(_SHUTDOWN)
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            logger.warning(
                "DiagLogger writer did not exit within %.1fs; "
                "file may be missing the last %d queued rows",
                timeout, self._queue.qsize(),
            )
        try:
            self._fp.close()
        except OSError:
            pass
        if self._dropped:
            logger.warning(
                "DiagLogger: dropped %d row(s) due to write errors", self._dropped,
            )
        logger.info("DiagLogger closed: %s", self.out_path)

    # ----------------------------------------------------------- worker
    def _run(self) -> None:
        last_flush = time.monotonic()
        flush_interval = 1.0  # seconds — bounds data loss on hard crash
        while True:
            try:
                row = self._queue.get()
            except Exception:  # noqa: BLE001 — keep the thread alive
                continue

            if row is _SHUTDOWN:
                break

            try:
                self._writer.writerow(_format_row(row))
            except (OSError, ValueError):
                self._dropped += 1
                continue

            now = time.monotonic()
            if now - last_flush >= flush_interval:
                try:
                    self._fp.flush()
                except OSError:
                    pass
                last_flush = now

        # Drain anything still queued (e.g. rows pushed between the
        # _SHUTDOWN sentinel and the writer reaching it — unlikely but
        # harmless).  Then final flush.
        try:
            while True:
                row = self._queue.get_nowait()
                if row is _SHUTDOWN:
                    continue
                try:
                    self._writer.writerow(_format_row(row))
                except (OSError, ValueError):
                    self._dropped += 1
        except queue.Empty:
            pass
        try:
            self._fp.flush()
        except OSError:
            pass


def _format_row(row: Mapping[str, Any]) -> dict:
    """Format numeric values consistently for CSV output.

    Booleans become 0/1 (compact and pandas-friendly); floats are pinned
    to a reasonable precision so the file doesn't explode with
    ``1.0000000000000004``-style noise; None becomes empty cell.
    """
    out: dict = {}
    for key in FIELDNAMES:
        value = row.get(key)
        if value is None:
            out[key] = ""
        elif isinstance(value, bool):
            out[key] = "1" if value else "0"
        elif isinstance(value, float):
            # NaN/Inf round-trip as the strings "nan"/"inf" — pandas
            # reads these back as floats with na_values left at default.
            out[key] = f"{value:.6f}"
        else:
            out[key] = value
    return out


def _iso_now(t: float) -> str:
    """ISO-8601 with millisecond precision (no timezone — local wall clock)."""
    # We deliberately avoid datetime.fromtimestamp().isoformat() because
    # its microsecond field varies in width depending on trailing zeros,
    # which makes CSV inspection slightly less consistent.
    lt = time.localtime(t)
    msec = int((t - int(t)) * 1000)
    return f"{time.strftime('%Y-%m-%dT%H:%M:%S', lt)}.{msec:03d}"
