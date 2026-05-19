#!/usr/bin/env python3
"""
main.py - Sequential Live Follower Entry Point

Components running in the process:

  Main thread
    └─ Tkinter GUI (operator screen)

  Worker threads (daemon)
    ├─ matchmaker-worker    (MatchMaker — owns Matchmaker.run() generator)
    ├─ slide-controller     (SlideController — owns Playwright Chromium)
    ├─ state-sync           (polls MatchMaker, updates AppState)
    └─ trigger-executor     (watches AppState, fires slide presses)

Usage::

    python -m sequential_live_follower.main config.json \\
        --slide-url "https://docs.google.com/presentation/d/<ID>/present"

Press the 'N' key (global hotkey on supported platforms) to advance to the
next movement listed in config.json.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

from sequential_live_follower.config.loader import ConfigError, ConfigLoader
from sequential_live_follower.core.audio_level import AudioLevelMonitor
from sequential_live_follower.core.audio_recorder import AudioRecorder
from sequential_live_follower.core.cooldown_timer import CooldownTimer
from sequential_live_follower.core.diag_logger import DiagLogger
from sequential_live_follower.core.inertia_engine import InertiaEngine
from sequential_live_follower.core.matcher import MatchMaker
from sequential_live_follower.core.score_mapper import ScoreMapper
from sequential_live_follower.core.slide_controller import SlideController
from sequential_live_follower.core.state_manager import AppState
from sequential_live_follower.ui.gui_tkinter import FollowerGUI

logger = logging.getLogger(__name__)

# How often we poll the matcher for an updated beat. Matchmaker yields at the
# audio frame rate (typically 10-50 Hz); polling at 20 Hz is plenty.
_STATE_SYNC_HZ = 20
# How often the trigger executor checks for measure hits.
_TRIGGER_POLL_HZ = 20


class SequentialFollower:
    """Top-level application orchestrator."""

    def __init__(
        self,
        config_path: str,
        slide_url: str,
        diag_dir: "Path | None" = None,
        silence_threshold_db_override: "float | None" = None,
        musical_flatness_threshold_override: "float | None" = None,
    ) -> None:
        logger.info("Initializing SequentialFollower (config=%s)", config_path)

        self.config = ConfigLoader(config_path)
        self.slide_url = slide_url

        # Shared state and per-instance helpers
        self.state = AppState()
        self.inertia = InertiaEngine(
            confidence_threshold=self.config.get_confidence_threshold(),
            inertia_timeout_sec=self.config.get_inertia_timeout_seconds(),
        )
        self.cooldown = CooldownTimer(self.config.get_cooldown_seconds())
        # Live mic level monitor — when the mic is silent, force matcher
        # confidence to 0 so pymatchmaker's score-driven advance cannot
        # falsely lock in the InertiaEngine.  Must consume the same input
        # device as MatchMaker (see ConfigLoader.get_mic_device).
        #
        # CLI ``--silence-threshold-db`` overrides config.json so the
        # operator can tighten the gate on-site without editing the file
        # (which is awkward during rehearsal).  Logged so the running
        # value is visible in -v output.
        silence_threshold_db = (
            silence_threshold_db_override
            if silence_threshold_db_override is not None
            else self.config.get_silence_threshold_db()
        )
        if silence_threshold_db_override is not None:
            logger.info(
                "silence_threshold_db = %.1f dBFS (CLI override; config value was %.1f)",
                silence_threshold_db, self.config.get_silence_threshold_db(),
            )

        # Spectral-flatness threshold: blocks above this are non-musical
        # (e.g. coughs, taps, speech) and trigger the same freeze path
        # the silence gate uses.  CLI override layered the same way for
        # field tuning during rehearsals.
        flatness_threshold = (
            musical_flatness_threshold_override
            if musical_flatness_threshold_override is not None
            else self.config.get_musical_flatness_threshold()
        )
        if musical_flatness_threshold_override is not None:
            logger.info(
                "musical_flatness_threshold = %.2f (CLI override; config value was %.2f)",
                flatness_threshold, self.config.get_musical_flatness_threshold(),
            )

        self.audio_monitor = AudioLevelMonitor(
            threshold_db=silence_threshold_db,
            flatness_threshold=flatness_threshold,
            device=self.config.get_mic_device(),
        )

        # Diagnostic recorders (only enabled when --diag-dir was passed).
        # Both are tee'd off existing streams — no extra audio device is
        # opened — so leaving --diag-dir off has zero runtime cost.
        self.diag_dir = diag_dir
        self.diag_logger: DiagLogger | None = None
        self.audio_recorder: AudioRecorder | None = None
        if diag_dir is not None:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            self.diag_logger = DiagLogger(diag_dir / f"diag_{stamp}.csv")
            self.audio_recorder = AudioRecorder(
                diag_dir / f"audio_{stamp}.wav",
                sample_rate=self.audio_monitor.sample_rate,
            )
            # Tee mic blocks into the WAV recorder.  Registered before
            # ``audio_monitor.start()`` so we don't miss the very first
            # block on machines where the callback fires immediately.
            self.audio_monitor.add_block_listener(self.audio_recorder.on_block)

        # Per-movement objects (recreated each load)
        self.score_mapper: ScoreMapper | None = None
        self.matcher: MatchMaker | None = None

        # Trigger measures already fired in the current movement. Each
        # trigger fires at most once per movement load; this set is cleared
        # whenever a new movement is loaded.
        self._fired_trigger_measures: set[int] = set()

        # Long-lived browser controller (lives across movements)
        self.slide_controller = SlideController(slide_url=slide_url)

        # Tkinter root + GUI
        self.root = tk.Tk()
        self.gui = FollowerGUI(self.root, self.state)

        # Worker thread handles
        self._state_sync_thread: threading.Thread | None = None
        self._trigger_thread: threading.Thread | None = None
        self._workers_stop = threading.Event()

        # Throttle for the per-iteration diagnostic log in _state_sync_loop.
        # The loop runs at _STATE_SYNC_HZ (20 Hz); logging every tick floods
        # the -v output.  We emit a single summary line every ~1 s instead.
        # If env var SLF_VERBOSE_SYNC=1 is set, the throttle is bypassed and
        # every iteration is logged — useful for diagnosing ratio/jitter
        # issues at the full state-sync rate.
        self._last_state_diag_log = 0.0
        self._verbose_sync = os.environ.get("SLF_VERBOSE_SYNC", "").strip() == "1"
        if self._verbose_sync:
            logger.info("SLF_VERBOSE_SYNC=1 → state-sync DEBUG log un-throttled (20 Hz)")

        logger.info("SequentialFollower initialization complete")

    # ----------------------------------------------------- lifecycle
    def run(self) -> None:
        """Start everything, then run the Tk main loop until the window closes."""
        logger.info("Launching AudioLevelMonitor …")
        try:
            self.audio_monitor.start()
        except BaseException as exc:
            # Belt-and-suspenders: if anything in start() escapes its own
            # try/except (e.g. PortAudio's C-level abort), we still want
            # the rest of the app to come up.  The silence gate just stays
            # disabled and the matcher's raw confidence is used as-is.
            logger.warning(
                "AudioLevelMonitor.start() raised (%s: %s); "
                "continuing without silence gate",
                type(exc).__name__, exc,
            )

        logger.info("Launching SlideController …")
        self.slide_controller.start()
        if not self.slide_controller.wait_ready(timeout=30.0):
            err = self.slide_controller.last_error
            logger.error("SlideController failed to become ready: %s", err)
            # Continue anyway — the user may still be able to use keyboard
            # navigation manually; trigger presses will be no-ops.

        self._bind_keys()

        logger.info("Loading first movement …")
        self._load_current_movement()

        logger.info("Starting state-sync and trigger threads …")
        self._state_sync_thread = threading.Thread(
            target=self._state_sync_loop, name="state-sync", daemon=True
        )
        self._state_sync_thread.start()

        self._trigger_thread = threading.Thread(
            target=self._trigger_loop, name="trigger-executor", daemon=True
        )
        self._trigger_thread.start()

        logger.info("Press 'N' to advance to next movement. Close GUI window to exit.")

        self.root.protocol("WM_DELETE_WINDOW", self._on_gui_closing)
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            logger.info("Interrupted via keyboard")
        finally:
            self._cleanup()

    def _on_gui_closing(self) -> None:
        logger.info("GUI window closing …")
        self._cleanup()
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001 — root may already be torn down
            pass

    def _cleanup(self) -> None:
        logger.info("Shutting down …")
        self._workers_stop.set()

        if self.matcher is not None:
            self.matcher.stop()
            self.matcher = None

        self.audio_monitor.stop()
        self.slide_controller.stop()

        # Close diagnostic artifacts after the audio + matcher threads
        # have stopped pushing data so the final CSV rows and WAV blocks
        # are captured in full.
        if self.audio_recorder is not None:
            self.audio_recorder.close()
            self.audio_recorder = None
        if self.diag_logger is not None:
            self.diag_logger.close()
            self.diag_logger = None

        logger.info("Shutdown complete")

    # ---------------------------------------------------- movement loading
    def _load_current_movement(self) -> None:
        """Load the movement currently pointed to by the config."""
        movement = self.config.get_current_movement()
        if not movement:
            logger.error("No movement available to load")
            return
        self._load_movement(movement)

    def _load_next_movement(self) -> None:
        """Advance the config pointer and load the next movement."""
        if not self.config.next_movement():
            logger.warning("Already at the last movement; nothing to load")
            self.state.set_next_trigger(None)
            return
        movement = self.config.get_current_movement()
        if not movement:
            logger.error("Failed to get next movement from config")
            return
        self._load_movement(movement)

    def _load_movement(self, movement: dict) -> None:
        """Tear down any current matcher and start a new one for ``movement``."""
        xml_file_raw = movement.get("xml_file")
        if not xml_file_raw:
            logger.error("Movement has no xml_file: %s", movement)
            return

        # Resolve relative to config.json's directory so the operator can
        # place config.json and .xml files in the same folder without worrying
        # about the working directory at launch time.
        xml_file = self.config.resolve_path(xml_file_raw)

        # Fail fast with a clear placement instruction before touching any
        # audio/browser threads.
        from pathlib import Path as _Path
        if not _Path(xml_file).exists():
            msg = (
                f"楽譜ファイルが見つかりません。\n"
                f"  → {xml_file}\n"
                f"  に置いてください"
            )
            logger.error(msg)
            self.state.set_load_error(
                f"ファイルが見つかりません。\n{xml_file}\nに置いてください"
            )
            return

        logger.info("Loading movement: %s", xml_file)

        # Stop the previous matcher cleanly before swapping in the new one.
        if self.matcher is not None:
            logger.info("Stopping previous matcher …")
            self.matcher.stop()
            self.matcher = None

        try:
            self.score_mapper = ScoreMapper(xml_file)
            logger.info("Score map ready: %s", self.score_mapper)
        except Exception as exc:  # noqa: BLE001 — surface and skip
            logger.error("Failed to build score map: %s", exc, exc_info=True)
            self.state.set_next_trigger(None)
            return

        try:
            self.matcher = MatchMaker(
                score_file=xml_file,
                input_type="audio",
                device_name_or_index=self.config.get_mic_device(),
                extra_kwargs=self.config.get_matcher_kwargs(),
            )
            self.matcher.start()
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to start matcher: %s", exc, exc_info=True)
            self.matcher = None
            return

        # Reset cross-movement helpers
        self.inertia.reset()
        self.cooldown.cleanup_old()
        self._fired_trigger_measures.clear()

        triggers = movement.get("triggers", [])
        total_measures = self.score_mapper.get_total_measures()
        self.state.set_movement(
            movement_id=movement.get("id"),
            xml_file=xml_file,
            triggers=triggers,
            movement_number=self.config.current_movement_number(),
            total_movements=self.config.total_movements(),
            total_measures=total_measures,
        )
        if triggers:
            self.state.set_next_trigger(min(t["measure"] for t in triggers))

        # Best-effort: warn (but don't block) if matcher fails to emit
        # within a reasonable time. We do this on a side thread so GUI
        # stays responsive.
        def _ready_check() -> None:
            assert self.matcher is not None  # captured at scheduling time
            if not self.matcher.wait_ready(timeout=15.0):
                err = self.matcher.last_error
                logger.error("Matcher did not become ready in time: %s", err)

        threading.Thread(target=_ready_check, daemon=True, name="matcher-ready-check").start()

        logger.info("Movement loaded: %s", xml_file)

    # ---------------------------------------------------- worker loops
    def _state_sync_loop(self) -> None:
        """Pull the latest (beat, confidence) from the matcher into AppState."""
        logger.info("State-sync loop started (%.0f Hz)", _STATE_SYNC_HZ)
        interval = 1.0 / _STATE_SYNC_HZ

        while not self._workers_stop.is_set():
            try:
                matcher = self.matcher
                mapper = self.score_mapper
                if matcher is None or mapper is None:
                    time.sleep(interval)
                    continue

                raw_beat, raw_conf = matcher.get_latest()

                # Combined musical-content gate: pymatchmaker keeps
                # advancing the beat from its score-prior on noise just
                # as readily as on silence (the DTW finds *some* chroma
                # match for any sustained sound).  We block both cases:
                #
                #   silent       : mic_db below the silence threshold
                #   non_musical  : spectral flatness above the threshold
                #                  (broadband noise — coughs, taps, paper,
                #                   speech — rather than tonal content)
                #
                # Either condition forces raw_conf to 0 so the
                # InertiaEngine cannot lock in, and triggers a freeze on
                # the matcher's DTW position (below) so it cannot drift
                # forward through the bad audio.  When AudioLevelMonitor
                # failed to open its stream, both is_active() and
                # is_musical() fall through to True so we don't unfairly
                # gate the matcher.
                mic_available = self.audio_monitor.is_available()
                mic_level_db = self.audio_monitor.get_level_db()
                flatness = self.audio_monitor.get_spectral_flatness()
                silent = not self.audio_monitor.is_active()
                non_musical = not self.audio_monitor.is_musical()
                gate_active = mic_available and (silent or non_musical)
                if gate_active:
                    raw_conf = 0.0

                # Drive the matcher's freeze/unfreeze state from the *current*
                # gate signal every tick, not just on transitions.
                #
                # Transition-based freezing missed a critical case: when the
                # operator presses 'R' to reload the movement, a fresh matcher
                # is constructed but ``_prev_gate_active`` retains its old
                # value from before the reload.  If the room is silent at the
                # moment of the reload (the typical case — the operator only
                # presses R when tracking has gone wrong, which usually
                # coincides with quiet moments), gate_active stays True
                # continuously across the reload, no transition is detected,
                # and freeze() is never called on the new matcher.  Diagnostic
                # data (diag4) showed the new matcher's DTW racing forward
                # 11.5 beats over 9 silent seconds, then jumping the displayed
                # measure from 1 to 6 the moment the gate first opened.
                #
                # ``freeze()`` and ``unfreeze()`` are both idempotent
                # (matcher.py:283–310) and only log on actual state change, so
                # calling them every tick is cheap.
                if gate_active:
                    matcher.freeze()
                else:
                    matcher.unfreeze()

                beat, inertia_active, tempo = self.inertia.update(raw_beat, raw_conf)
                measure = mapper.beat_to_measure(beat)
                # ScoreMapper returns a 0-indexed offset (downbeat = 0.0);
                # shift to 1-indexed so the GUI shows "1拍目" on the downbeat.
                beat_in_measure = mapper.get_beat_in_measure(beat) + 1.0

                self.state.update_beat_measure(beat, measure, beat_in_measure)
                self.state.set_confidence(raw_conf)
                self.state.set_inertia_mode(inertia_active, tempo)
                self.state.set_mic_level(mic_level_db, gate_active, mic_available)

                # Diagnostic CSV row (always full rate when --diag-dir is on).
                # Written off-thread by DiagLogger so the 20 Hz state-sync
                # cadence isn't disturbed.  We fetch the matcher's internal
                # diagnostic snapshot once per tick — it's lock-protected and
                # cheap.  Skipped entirely when --diag-dir wasn't passed.
                if self.diag_logger is not None:
                    diag = matcher.get_diagnostics()
                    self.diag_logger.log({
                        "raw_beat": raw_beat,
                        "beat": beat,
                        "measure": measure,
                        "beat_in_measure": beat_in_measure,
                        "raw_conf": raw_conf,
                        "mic_db": mic_level_db,
                        "spectral_flatness": flatness,
                        "is_musical": not non_musical,
                        "silence_gate_fired": silent,
                        "gate_active": gate_active,
                        "mic_available": mic_available,
                        "matcher_frozen": diag.frozen,
                        "matcher_frozen_frame": diag.frozen_frame,
                        "locked_in": self.inertia.is_locked_in(),
                        "inertia_active": inertia_active,
                        "inertia_tempo_bpm": tempo,
                        "history_len": diag.history_len,
                        "win_velocity": diag.win_velocity,
                        "stall_sec": diag.stall_sec,
                    })

                # Diagnostic line.  Throttled to 1 Hz by default so the -v
                # output stays readable; set SLF_VERBOSE_SYNC=1 to emit at
                # the full state-sync rate (20 Hz) for ratio/jitter analysis.
                now = time.time()
                if self._verbose_sync or now - self._last_state_diag_log >= 1.0:
                    logger.debug(
                        "sync raw_beat=%.2f beat=%.2f measure=%d conf=%.2f "
                        "mic_db=%.1f flat=%.3f gate=%s(silent=%s,non_musical=%s) locked=%s",
                        raw_beat, beat, measure, raw_conf,
                        mic_level_db, flatness, gate_active,
                        silent, non_musical,
                        self.inertia.is_locked_in(),
                    )
                    self._last_state_diag_log = now

            except Exception as exc:  # noqa: BLE001 — keep the thread alive
                logger.error("State-sync error: %s", exc, exc_info=True)

            time.sleep(interval)

        logger.info("State-sync loop exiting")

    def _trigger_loop(self) -> None:
        """Watch the current measure and fire slide actions at trigger points."""
        logger.info("Trigger loop started (%.0f Hz)", _TRIGGER_POLL_HZ)
        interval = 1.0 / _TRIGGER_POLL_HZ

        while not self._workers_stop.is_set():
            try:
                snapshot = self.state.get_all()
                triggers = self.state.current_triggers
                current_measure = snapshot["measure"]

                if not triggers:
                    time.sleep(interval)
                    continue

                # Update "next trigger" display: only show measures we
                # haven't fired yet, so the operator sees the *real* next
                # cue rather than one that's already played.
                upcoming = [
                    t["measure"] for t in triggers
                    if t["measure"] > current_measure
                    and t["measure"] not in self._fired_trigger_measures
                ]
                self.state.set_next_trigger(min(upcoming) if upcoming else None)

                # Do not fire anything until tracking has clearly begun.
                # Otherwise the measure=1 trigger fires at startup (beat=0
                # maps to measure 1) before any music is detected.
                if not self.inertia.is_locked_in():
                    time.sleep(interval)
                    continue

                # Fire any trigger whose measure has been reached and isn't
                # in cooldown.
                if snapshot["cooldown_active"]:
                    time.sleep(interval)
                    continue

                for trig in triggers:
                    if trig["measure"] != current_measure:
                        continue
                    # Each trigger fires at most once per movement load.
                    if current_measure in self._fired_trigger_measures:
                        continue
                    if not self.cooldown.should_trigger(current_measure):
                        continue

                    action = trig.get("action", "right")
                    note = trig.get("note", "")
                    self._execute_action(action)
                    logger.info(
                        "Trigger fired at measure %s: action=%s note=%s",
                        current_measure, action, note,
                    )
                    self.cooldown.mark_triggered(current_measure)
                    self.state.activate_cooldown(self.config.get_cooldown_seconds())
                    self._fired_trigger_measures.add(current_measure)
                    break  # one trigger per measure visit

            except Exception as exc:  # noqa: BLE001
                logger.error("Trigger loop error: %s", exc, exc_info=True)

            time.sleep(interval)

        logger.info("Trigger loop exiting")

    def _execute_action(self, action: str) -> None:
        """Send the configured action to the slide controller."""
        try:
            self.slide_controller.press(action)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to dispatch slide action %s: %s", action, exc, exc_info=True)

    # ---------------------------------------------------- keyboard bindings
    def _bind_keys(self) -> None:
        """Bind operator hotkeys to the Tk root window.

        Bindings are scoped to the operator GUI window. The operator screen
        must have focus for the key to register — this is intentional so the
        audience-facing Chromium window does not steal the binding.

        Hotkeys:
            N       : load next movement
            R       : reload current movement (restarts matcher + resets tracking)
            → / Space : manually advance one slide
            ←       : manually go back one slide
        """
        def _on_n(_event: tk.Event) -> None:
            logger.info("'N' key pressed → loading next movement")
            self._load_next_movement()

        def _on_r(_event: tk.Event) -> None:
            # Full reload of the current movement.  We intentionally do
            # *not* just reset the InertiaEngine because the Matchmaker
            # generator may already have terminated — e.g. when playback
            # ran past the end of the score, Matchmaker.run() raises
            # StopIteration and the worker thread exits.  After that the
            # worker is dead and no amount of inertia-reset will revive it.
            # Reloading the movement constructs a fresh Matchmaker, restarts
            # its worker thread, and clears inertia / cooldown / fired
            # triggers in one shot.
            logger.info("'R' key pressed → reloading current movement")
            self._load_current_movement()

        def _on_slide_next(_event: tk.Event) -> None:
            logger.info("Manual slide advance (→/Space)")
            self._execute_action("right")

        def _on_slide_prev(_event: tk.Event) -> None:
            logger.info("Manual slide back (←)")
            self._execute_action("left")

        self.root.bind("<KeyPress-n>", _on_n)
        self.root.bind("<KeyPress-N>", _on_n)
        self.root.bind("<KeyPress-r>", _on_r)
        self.root.bind("<KeyPress-R>", _on_r)
        self.root.bind("<KeyPress-Right>", _on_slide_next)
        self.root.bind("<KeyPress-space>", _on_slide_next)
        self.root.bind("<KeyPress-Left>", _on_slide_prev)
        logger.info(
            "Operator hotkeys bound: N=next movement, R=reload current movement, "
            "→/Space=manual next slide, ←=manual previous slide"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sequential Live Follower — real-time orchestral score-following slide control",
    )
    parser.add_argument("config", help="Path to config.json")
    parser.add_argument(
        "--slide-url",
        required=True,
        help=(
            "Google Slides URL to control. Use the /present variant "
            "(e.g. https://docs.google.com/presentation/d/<ID>/present) for "
            "auto-fullscreen presentation mode."
        ),
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging",
    )
    parser.add_argument(
        "--diag-dir",
        type=Path,
        default=None,
        help=(
            "診断ログ (CSV) と raw マイク音声 (WAV) の出力ディレクトリ。"
            "指定しなければ何も出力しない。「ただの雑音で進行する」など、"
            "現場でしか再現しない問題の原因切り分け用。"
        ),
    )
    parser.add_argument(
        "--silence-threshold-db",
        type=float,
        default=None,
        help=(
            "config.json の silence_threshold_db を上書きする (dBFS)。"
            "現場で雑音通過の閾値を即座に詰めるための調整スイッチ。"
            "例: --silence-threshold-db -40 で -40 dBFS 以下を無音扱い"
        ),
    )
    parser.add_argument(
        "--musical-flatness-threshold",
        type=float,
        default=None,
        help=(
            "config.json の musical_flatness_threshold を上書きする (0-1)。"
            "spectral flatness がこの値以上のブロックを「楽音でない」と"
            "判定して matcher を止める。例: --musical-flatness-threshold 0.30"
            " で楽音判定を緩く、--musical-flatness-threshold 0.20 で厳しく"
        ),
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Third-party libraries flood -v with bytecode dumps / font cache lookups
    # that drown out our own DEBUG messages.  Pin them to INFO so the
    # operator can actually read the matcher/inertia state transitions.
    for noisy in ("numba", "matplotlib", "asyncio", "PIL", "fontTools"):
        logging.getLogger(noisy).setLevel(logging.INFO)

    config_path = Path(args.config)
    if not config_path.exists():
        logger.error("Config file not found: %s", config_path)
        return 1

    # Validate / prepare the diagnostic output directory up front so the
    # operator gets a clear error before any audio threads are spawned.
    diag_dir: Path | None = None
    if args.diag_dir is not None:
        diag_dir = args.diag_dir.expanduser().resolve()
        try:
            diag_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error("--diag-dir を作成できません: %s (%s)", diag_dir, exc)
            return 1
        logger.info("Diagnostic capture enabled → %s", diag_dir)

    try:
        app = SequentialFollower(
            str(config_path),
            slide_url=args.slide_url,
            diag_dir=diag_dir,
            silence_threshold_db_override=args.silence_threshold_db,
            musical_flatness_threshold_override=args.musical_flatness_threshold,
        )
        app.run()
        return 0
    except ConfigError as exc:
        # User-visible config mistake — print cleanly without a traceback so
        # the operator can act on the message directly.
        logger.error("設定ファイルエラー — config.json を修正してから再起動してください")
        logger.error("  %s", exc)
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.error("Fatal error: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
