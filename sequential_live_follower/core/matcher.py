#!/usr/bin/env python3
"""
matcher.py - pymatchmaker (Matchmaker) Integration

Runs `matchmaker.Matchmaker.run()` in a dedicated background thread, exposing
the latest beat position and an estimated confidence value via thread-safe
accessors.

Design notes
------------
The public `matchmaker` API yields a single `current_position` (in beats) per
audio frame; it does not surface a confidence value as of pymatchmaker 0.2.1.
We expose a binary confidence (0.0 or 1.0) derived from two checks over a
recent history window:

1. The matcher has emitted ``_MIN_HISTORY_FOR_CONFIDENCE`` samples in the
   last ``_CONFIDENCE_WINDOW_SEC`` seconds (i.e. it's actually running).
2. The window-wide beat velocity falls inside
   ``[_MIN_VELOCITY_FOR_CONFIDENCE, _MAX_VELOCITY_FOR_CONFIDENCE]`` — fast
   enough to be real music, slow enough to reject runaway matches.

What we deliberately do *not* do
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
We previously gated on the *coefficient of variation* of sub-window mean
velocities (rejecting when CV > 0.4).  In practice live music routinely
crossed that threshold during expressive playing — rit/accel, fermatas,
strong dynamics, rests — collapsing confidence to 0 for long stretches.
The InertiaEngine then started extrapolating at a stale tempo and the
beat counter raced ahead of the conductor.  The CV check has been
removed; reasoning against the prior designs is preserved here for the
next person who is tempted to put a clever filter back in:

- **Per-frame CV** breaks under pymatchmaker's burst-mode output: it
  races through buffered frames, then blocks on audio I/O.  Per-frame
  velocity swings between very large and zero even during correct
  tracking.

- **Endpoint velocity** is what we use now.  Yes, irrelevant audio can
  cause pymatchmaker to advance and trivially scores confidence=1.0 —
  but the silence gate (``AudioLevelMonitor``) catches the no-input case
  and the operator-controlled lock-in delay (``_CONFIDENT_FRAMES_TO_LOCK_IN``
  in inertia_engine.py) prevents a single bad frame from firing slides.
  Trying to filter out "wrong piece" audio in this layer added more
  pain than it solved.

- **Sub-window mean velocity CV** (removed): see above — too strict for
  real live performance.

If a future pymatchmaker release exposes a real confidence/cost value, this
module is the single place that needs to change.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Deque, Optional, Tuple, Union

logger = logging.getLogger(__name__)

# Confidence estimation window (seconds of recent beat history we track)
_CONFIDENCE_WINDOW_SEC = 2.0
# If beat hasn't advanced for this long, force confidence to 0.
# pymatchmaker emits in bursts (many frames in milliseconds, then blocks on
# audio I/O for hundreds of milliseconds).  1 s was too short and confidence
# briefly dropped to 0 in normal operation; 2 s gives the burst pattern room.
_STALL_TIMEOUT_SEC = 2.0
# Minimum number of beat samples before any positive confidence is reported.
# Below this threshold confidence is forced to 0.0 so the inertia engine does
# not falsely conclude that tracking has begun during the matcher's warmup.
_MIN_HISTORY_FOR_CONFIDENCE = 5
# Minimum average beat-velocity (beats/sec) over the window to count as real
# tracking. 0.5 beats/sec ≈ 30 BPM — slower than any practical orchestral tempo.
_MIN_VELOCITY_FOR_CONFIDENCE = 0.5
# Maximum reasonable beat-velocity (beats/sec). pymatchmaker can lock onto
# spurious matches in irrelevant audio and race through the score; this cap
# rejects those. 5 beats/sec = 300 BPM — above any realistic orchestral tempo.
_MAX_VELOCITY_FOR_CONFIDENCE = 5.0


# ------------------------------------------------------------------ DTW cost
# Per-thread storage for the ``min_costs`` value computed inside pymatchmaker's
# OLTW step.  See ``_install_oltw_instrumentation`` for the rationale: the
# upstream algorithm computes a normalized "best path cost" each step but
# discards it after using it to pick the next position, so we wrap the C-level
# ``oltw_arzt_loop`` function to capture it.  Keyed by thread id so multiple
# MatchMaker instances (or test code) don't clobber each other.
_dtw_min_cost_capture: dict = {}


def _install_oltw_instrumentation() -> None:
    """Monkey-patch ``matchmaker.dp.oltw_arzt.oltw_arzt_loop`` to capture
    the per-step normalized min cost.

    Why we patch
    ------------
    pymatchmaker's OnlineTimeWarpingArzt has no public match-quality signal.
    Its DTW always advances forward (step_size ≥ 1 reference frame per input
    frame) regardless of how well the audio matches the score.  In live
    deployment, any tonal sound — including human speech, which has strong
    harmonics and looks "musical" to the spectral-flatness gate — drives the
    score counter forward.

    Internally the algorithm DOES compute a useful signal: ``min_costs``, the
    normalized best-path cost in the current search window.  Well-matched
    audio yields a small value; mismatched audio (speech, noise) yields a
    much larger one.  The C-level ``oltw_arzt_loop`` returns ``min_costs``
    as its 3rd tuple element but the Python ``step()`` discards it after
    using it to decide the next position.

    We wrap ``oltw_arzt_loop`` once (idempotent) and stash the value in a
    thread-keyed dict so the matcher's worker can read it back after each
    yield from ``Matchmaker.run()``.  Wrapping at this layer is the only
    place the value is accessible without forking pymatchmaker.

    Fragility note
    --------------
    This patches a private/internal function of pymatchmaker 0.2.x.  If a
    future upstream release renames the function or changes its return
    shape, we silently lose the signal — ``get_diagnostics().match_cost``
    will stay at NaN and the gate (if enabled) will simply pass everything.
    """
    try:
        import matchmaker.dp.oltw_arzt as _oltw_mod  # type: ignore
    except ImportError:
        # pymatchmaker not installed — nothing to do.  This happens in CI
        # (we keep the import path working there for testing) and on Windows
        # where only the Linux wheels exist.
        return

    if getattr(_oltw_mod, "_slf_instrumented", False):
        return  # already wrapped

    original_loop = _oltw_mod.oltw_arzt_loop

    def _wrapped_loop(*args, **kwargs):
        result = original_loop(*args, **kwargs)
        # Expected return: (global_cost_matrix, min_index, min_costs)
        try:
            _dtw_min_cost_capture[threading.get_ident()] = float(result[2])
        except (IndexError, TypeError, ValueError):
            # Shape unexpected — leave previous value in place (or absent).
            pass
        return result

    _oltw_mod.oltw_arzt_loop = _wrapped_loop
    _oltw_mod._slf_instrumented = True  # type: ignore[attr-defined]
    logger.info("Installed pymatchmaker OLTW instrumentation (match_cost capture)")


# Install at import time.  Cheap (one attribute lookup if pymatchmaker is
# absent) and avoids the worker thread racing the first step() call.
_install_oltw_instrumentation()


@dataclass(frozen=True)
class MatcherDiagnostics:
    """Read-only snapshot of MatchMaker internal state for diagnostic logging.

    Exposed via ``MatchMaker.get_diagnostics()`` so the state-sync loop can
    correlate confidence drops with the underlying causes (history length,
    window velocity, stall, freeze) without reaching into private fields.
    Cheap to construct — read once per state-sync tick (20 Hz).
    """
    history_len: int
    """Number of (timestamp, beat) samples currently in the confidence window."""

    win_velocity: float
    """Window-wide endpoint velocity (beats/sec). NaN if too few samples."""

    stall_sec: float
    """Seconds since the last beat update; > _STALL_TIMEOUT_SEC forces conf=0."""

    frozen: bool
    """True if the silence-gate path has pinned the score follower's position."""

    frozen_frame: Optional[int]
    """The pinned reference frame when frozen, else None."""

    match_cost: float
    """Normalized best-path DTW cost from the most recent OLTW step.

    Captured from pymatchmaker's internal ``oltw_arzt_loop`` via a module-level
    monkey-patch (see ``_install_oltw_instrumentation``).  Lower = better
    chroma match between input and reference at the chosen position; high
    values indicate the audio doesn't match the score (speech, noise, wrong
    piece).  NaN if pymatchmaker is not installed or the patch failed."""


class MatchMaker:
    """
    Thread-backed wrapper around `matchmaker.Matchmaker.run()`.

    Typical lifecycle::

        m = MatchMaker("guide_mv1.xml", device_name_or_index="default")
        m.start()
        m.wait_ready(timeout=10.0)        # block until first beat emitted
        beat, confidence = m.get_latest()  # called repeatedly by main loop
        ...
        m.stop()
    """

    def __init__(
        self,
        score_file: str,
        input_type: str = "audio",
        device_name_or_index: Optional[Union[str, int]] = None,
        method: str = "arzt",
        feature_type: str = "chroma",
        extra_kwargs: Optional[dict] = None,
    ) -> None:
        """
        Args:
            score_file: Path to MusicXML or MIDI score.
            input_type: "audio" (microphone) or "midi".
            device_name_or_index: Specific input device (None = system default).
            method: Alignment method ("arzt", "dixon", or "hmm"). For audio
                input, "arzt" or "dixon" are valid; "hmm" is MIDI-only.
            feature_type: Audio feature type ("chroma", "mfcc", "mel",
                "logspectral").
            extra_kwargs: Optional dict forwarded to ``Matchmaker(kwargs=...)``.
                Used to tune the underlying online DTW algorithm.  Common keys
                (see matchmaker.dp.oltw_arzt.OnlineTimeWarpingArzt):

                * ``start_window_size`` (float, default 0.1): fraction of the
                  score the initial position search can wander over.  At the
                  default, on a 24-measure score the matcher can lock onto any
                  point in the first ~5 seconds of the reference — disastrous
                  for music with repeating motifs (Beethoven 5 opening).
                  Setting this to ``0.02`` narrows the search to ~1 second.
                * ``step_size`` (int, default 3): max reference-frame advance
                  per input frame.  Lowering to ``1`` makes the alignment
                  more conservative (less prone to racing ahead on noise).
                * ``window_size`` (int, default 10): DP search radius around
                  the current position for steady-state tracking.
        """
        self.score_file = score_file
        self.input_type = input_type
        self.device_name_or_index = device_name_or_index
        self.method = method
        self.feature_type = feature_type
        self.extra_kwargs = dict(extra_kwargs) if extra_kwargs else {}

        # Thread-safe state
        self._lock = threading.Lock()
        self._latest_beat: float = 0.0
        self._latest_confidence: float = 0.0
        self._latest_update_time: float = time.time()

        # Silence-freeze: when the mic is silent, the upstream score-sync loop
        # calls freeze() to pin the matcher's reference position.  pymatchmaker
        # otherwise keeps consuming audio frames and drifting current_position
        # forward; on resume that drifted position is reported and the GUI
        # jumps ahead.  When _frozen_frame is set, _update() snaps
        # score_follower.current_position back to it on every emission, so
        # DTW restarts from the same place when audio returns.
        self._frozen_frame: Optional[int] = None
        self._beat_history: Deque[Tuple[float, float]] = deque()  # (timestamp, beat)

        # Latest normalized DTW match cost from the OLTW step (lower = better
        # match).  NaN until pymatchmaker has run at least one step or if the
        # OLTW instrumentation patch isn't active (e.g. CI without
        # pymatchmaker installed).  Updated each yield in ``_run_loop``.
        self._latest_match_cost: float = float("nan")

        # Lifecycle
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._fatal_error: Optional[BaseException] = None

        # The underlying matchmaker object is constructed lazily inside the
        # thread because some implementations open the audio stream in __init__
        # and we want stream lifetime tied to the worker thread.
        self._mm = None

        # Optional raw-beat CSV trace.  Enabled by setting the env var
        # SLF_BEAT_LOG=<path>.  Writes one row per pymatchmaker emission so
        # we can correlate displayed beat against wall-clock time during a
        # rehearsal — used to diagnose ratio mismatches (e.g. the GUI
        # advancing 2x faster than the music) without modifying code.
        # The file is line-buffered and protected by its own lock so the
        # main beat lock is not held across I/O.
        self._beat_log_path = os.environ.get("SLF_BEAT_LOG", "").strip() or None
        self._beat_log_fp = None
        self._beat_log_lock = threading.Lock()
        if self._beat_log_path:
            try:
                self._beat_log_fp = open(self._beat_log_path, "a", buffering=1, encoding="utf-8")
                if self._beat_log_fp.tell() == 0:
                    self._beat_log_fp.write("wall_iso,monotonic_s,raw_beat,score_file\n")
                logger.info("Beat CSV trace enabled: %s", self._beat_log_path)
            except OSError as exc:
                logger.warning(
                    "Could not open SLF_BEAT_LOG=%s for writing (%s); "
                    "beat tracing disabled",
                    self._beat_log_path, exc,
                )
                self._beat_log_fp = None

        logger.info(
            "MatchMaker configured: score=%s input=%s method=%s feature=%s device=%s",
            score_file, input_type, method, feature_type, device_name_or_index,
        )

    # ------------------------------------------------------------------ public
    def start(self) -> None:
        """Spawn the worker thread. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("MatchMaker.start() called but thread already running")
            return

        self._stop_event.clear()
        self._ready_event.clear()
        self._fatal_error = None

        self._thread = threading.Thread(
            target=self._run_loop,
            name="matchmaker-worker",
            daemon=True,
        )
        self._thread.start()
        logger.info("MatchMaker worker thread started")

    def stop(self, timeout: float = 3.0) -> None:
        """Signal worker to stop. Best-effort join.

        The underlying Matchmaker.run() generator can only be interrupted at
        the next yield boundary, so stop is not instantaneous; we set the flag
        and let the loop exit on its next iteration. We then drop the
        Matchmaker reference so its audio stream is closed by its destructor.
        """
        self._stop_event.set()

        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning(
                    "MatchMaker worker did not stop within %.1fs (Matchmaker.run() "
                    "may be blocked in audio I/O; releasing reference anyway)",
                    timeout,
                )

        self._mm = None
        self._thread = None

        # Close the beat CSV trace last so any final flush happens after the
        # worker thread has joined.
        if self._beat_log_fp is not None:
            try:
                self._beat_log_fp.close()
            except OSError:
                pass
            self._beat_log_fp = None

        logger.info("MatchMaker stopped")

    def wait_ready(self, timeout: float = 10.0) -> bool:
        """Block until the first beat value has been emitted.

        Returns True if the matcher emitted at least one value within the
        timeout; False if the matcher failed to start. Check `last_error`
        in the False case.
        """
        return self._ready_event.wait(timeout)

    def freeze(self) -> None:
        """Pin the score follower's current_position to its present value.

        Used by the silence-gate path to prevent pymatchmaker from drifting
        forward through the reference while the mic is silent.  Without this,
        the algorithm keeps consuming audio frames (silence/noise) and the
        DP search steadily advances current_position; when sound returns the
        matcher reports a position several beats ahead of where the music
        actually picked up, and the GUI jumps forward on resume.

        Idempotent — calling freeze() while already frozen leaves the pinned
        frame unchanged.
        """
        if self._mm is None or not hasattr(self._mm, "score_follower"):
            return
        with self._lock:
            if self._frozen_frame is not None:
                return  # already frozen, keep the original pin
            self._frozen_frame = int(self._mm.score_follower.current_position)
        logger.info("MatchMaker frozen at frame %d", self._frozen_frame)

    def unfreeze(self) -> None:
        """Release the pin so the score follower can advance again."""
        with self._lock:
            was_frozen = self._frozen_frame is not None
            self._frozen_frame = None
        if was_frozen:
            logger.info("MatchMaker unfrozen — resuming forward tracking")

    @property
    def last_error(self) -> Optional[BaseException]:
        """Return the fatal error that stopped the worker, if any."""
        return self._fatal_error

    def get_latest(self) -> Tuple[float, float]:
        """Return a snapshot of (latest_beat, estimated_confidence)."""
        with self._lock:
            # Apply stall detection at read time so confidence drops to 0
            # even while no new yields are arriving.
            if time.time() - self._latest_update_time > _STALL_TIMEOUT_SEC:
                confidence = 0.0
            else:
                confidence = self._latest_confidence
            return self._latest_beat, confidence

    def get_diagnostics(self) -> MatcherDiagnostics:
        """Snapshot internal state for the diagnostic CSV log.

        Returns the same fields that ``_estimate_confidence_locked`` reasons
        over, plus the freeze state, so a single CSV row can answer
        "why was confidence 0/1 at this moment?" after the fact.

        Cheap and lock-protected — safe to call at the state-sync rate.
        Returns NaN for ``win_velocity`` when fewer than 2 samples are
        in the history (no velocity defined yet).
        """
        with self._lock:
            history = self._beat_history
            history_len = len(history)
            stall_sec = time.time() - self._latest_update_time

            if history_len >= 2:
                oldest_time, oldest_beat = history[0]
                newest_time, newest_beat = history[-1]
                elapsed = newest_time - oldest_time
                win_velocity = (
                    (newest_beat - oldest_beat) / elapsed
                    if elapsed > 0 else float("nan")
                )
            else:
                win_velocity = float("nan")

            frozen_frame = self._frozen_frame
            match_cost = self._latest_match_cost

        return MatcherDiagnostics(
            history_len=history_len,
            win_velocity=win_velocity,
            stall_sec=stall_sec,
            frozen=frozen_frame is not None,
            frozen_frame=frozen_frame,
            match_cost=match_cost,
        )

    def reset(self) -> None:
        """Reset cached state (used between movements before re-start())."""
        with self._lock:
            self._latest_beat = 0.0
            self._latest_confidence = 0.0
            self._latest_update_time = time.time()
            self._beat_history.clear()
            self._latest_match_cost = float("nan")
        self._ready_event.clear()

    # ----------------------------------------------------------------- private
    def _run_loop(self) -> None:
        """Worker thread entry: drive Matchmaker.run() generator."""
        try:
            from matchmaker import Matchmaker  # type: ignore
        except ImportError as exc:
            self._fatal_error = exc
            logger.error(
                "pymatchmaker is not installed. Install it inside WSL2 with "
                "`pip install pymatchmaker` (Linux wheels only)."
            )
            self._ready_event.set()
            return

        try:
            mm_kwargs = {
                "score_file": self.score_file,
                "input_type": self.input_type,
                "method": self.method,
                "feature_type": self.feature_type,
            }
            if self.device_name_or_index is not None:
                mm_kwargs["device_name_or_index"] = self.device_name_or_index

            logger.info("Instantiating Matchmaker(%s)", mm_kwargs)
            self._mm = Matchmaker(**mm_kwargs)

            # Tune the OLTW score follower in place.  pymatchmaker 0.2.1 does
            # *not* forward tuning params from Matchmaker() to OnlineTimeWarpingArzt(),
            # so the only way to change ``window_size`` / ``step_size`` /
            # ``start_window_size`` is to patch the score_follower object after
            # construction.  The algorithm reads these via ``self.*`` on each
            # step (oltw_arzt.py:215, 264) so the change takes effect from the
            # next frame.  Defaults of (window_size=5s, step_size=5) are far
            # too permissive for repeating motifs (e.g. Beethoven 5 opening)
            # and cause the alignment to race ahead at startup.
            if self.extra_kwargs and hasattr(self._mm, "score_follower"):
                sf = self._mm.score_follower
                for key, value in self.extra_kwargs.items():
                    if hasattr(sf, key):
                        old = getattr(sf, key)
                        setattr(sf, key, value)
                        logger.info(
                            "Patched score_follower.%s: %r → %r", key, old, value,
                        )
                    else:
                        logger.warning(
                            "score_follower has no attribute %r; skipped tuning", key,
                        )
        except Exception as exc:  # noqa: BLE001 — surface any startup error
            self._fatal_error = exc
            logger.error("Matchmaker construction failed: %s", exc, exc_info=True)
            self._ready_event.set()
            return

        logger.info("Entering Matchmaker.run() generator loop")

        try:
            tid = threading.get_ident()
            for current_position in self._mm.run():
                if self._stop_event.is_set():
                    logger.info("Stop event received, leaving run() loop")
                    break

                # Pull the OLTW step's normalized min cost out of the
                # per-thread capture dict populated by the instrumentation
                # patch.  Stored as a plain attribute (no lock needed on the
                # write — Python float assignment is atomic under the GIL;
                # the read path in get_diagnostics() takes the lock anyway).
                match_cost = _dtw_min_cost_capture.get(tid, float("nan"))
                self._latest_match_cost = match_cost

                # While frozen (silence gate), snap score_follower back to
                # the pinned frame so DTW cannot drift forward through silence.
                # Done immediately after each yield, before the next step()
                # iteration begins.
                frozen = self._frozen_frame  # snapshot without lock — int read is atomic
                if frozen is not None and hasattr(self._mm, "score_follower"):
                    try:
                        self._mm.score_follower.current_position = frozen
                    except Exception:  # noqa: BLE001 — never let intervention kill the loop
                        pass

                # current_position may be a float (beats) or, for newer
                # versions, a richer object. Be defensive.
                beat = self._coerce_beat(current_position)
                self._update(beat)

        except StopIteration:
            logger.info("Matchmaker.run() exhausted (score ended)")
        except Exception as exc:  # noqa: BLE001
            self._fatal_error = exc
            logger.error("Matchmaker.run() raised: %s", exc, exc_info=True)
        finally:
            self._ready_event.set()  # unblock any wait_ready() callers
            logger.info("Matcher worker exiting")

    @staticmethod
    def _coerce_beat(value) -> float:
        """Normalize Matchmaker yield value to a float beat position."""
        if isinstance(value, (int, float)):
            return float(value)
        # Some versions may yield a dict or namespace; try common keys.
        for attr in ("beat", "position", "current_position"):
            if isinstance(value, dict) and attr in value:
                return float(value[attr])
            if hasattr(value, attr):
                return float(getattr(value, attr))
        # Fall back: try iterable unpacking (beat, ...)
        try:
            return float(next(iter(value)))
        except Exception:
            logger.debug("Could not coerce matcher value %r to beat", value)
            return 0.0

    def _update(self, beat: float) -> None:
        """Record a new beat reading and recompute confidence."""
        now = time.time()
        mono = time.monotonic()

        with self._lock:
            self._latest_beat = beat
            self._latest_update_time = now

            # Maintain recent history (sliding window)
            self._beat_history.append((now, beat))
            cutoff = now - _CONFIDENCE_WINDOW_SEC
            while self._beat_history and self._beat_history[0][0] < cutoff:
                self._beat_history.popleft()

            self._latest_confidence = self._estimate_confidence_locked()
            confidence = self._latest_confidence  # capture before releasing lock

        logger.debug("beat=%.3f conf=%.2f", beat, confidence)

        # CSV trace (opt-in via SLF_BEAT_LOG).  Done outside the main lock so
        # disk I/O cannot stall the main beat update path.
        if self._beat_log_fp is not None:
            try:
                line = f"{datetime.fromtimestamp(now).isoformat()},{mono:.6f},{beat:.6f},{self.score_file}\n"
                with self._beat_log_lock:
                    self._beat_log_fp.write(line)
            except (OSError, ValueError):
                pass  # never let trace errors kill the matcher thread

        if not self._ready_event.is_set():
            self._ready_event.set()

    def _estimate_confidence_locked(self) -> float:
        """Compute confidence from recent beat history. Caller holds the lock.

        Returns 0.0 (not a "neutral" 0.5) until we have enough data to make
        a real judgement.  Returning 0.5 here would briefly exceed the
        default inertia threshold (0.4) during the first 1-2 matchmaker
        emissions, falsely signalling that tracking has begun.

        Two checks, both must pass:
        1. At least ``_MIN_HISTORY_FOR_CONFIDENCE`` samples in the history
           window (the matcher is actually running, not in warm-up).
        2. Window-wide velocity within [_MIN_VELOCITY_FOR_CONFIDENCE,
           _MAX_VELOCITY_FOR_CONFIDENCE].  Rejects stalls and runaway matches.

        See the module docstring for why we no longer try to filter on
        sub-window stability: live performance trivially fails such checks
        during expressive playing, and the InertiaEngine then takes over
        with stale-tempo extrapolation, which is the bug we're fixing.
        """
        history = self._beat_history
        if len(history) < _MIN_HISTORY_FOR_CONFIDENCE:
            return 0.0

        oldest_time, oldest_beat = history[0]
        newest_time, newest_beat = history[-1]
        elapsed = newest_time - oldest_time
        if elapsed <= 0:
            return 0.0

        velocity = (newest_beat - oldest_beat) / elapsed
        if velocity < _MIN_VELOCITY_FOR_CONFIDENCE:
            return 0.0
        if velocity > _MAX_VELOCITY_FOR_CONFIDENCE:
            logger.debug(
                "confidence=0: velocity %.2f exceeds max %.2f (runaway match?)",
                velocity, _MAX_VELOCITY_FOR_CONFIDENCE,
            )
            return 0.0

        return 1.0

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        beat, conf = self.get_latest()
        return (
            f"MatchMaker(score={self.score_file!r}, beat={beat:.2f}, "
            f"confidence={conf:.2f}, running={self._thread is not None and self._thread.is_alive()})"
        )
