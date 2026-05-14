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
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

from sequential_live_follower.config.loader import ConfigLoader
from sequential_live_follower.core.audio_level import AudioLevelMonitor
from sequential_live_follower.core.cooldown_timer import CooldownTimer
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

    def __init__(self, config_path: str, slide_url: str) -> None:
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
        self.audio_monitor = AudioLevelMonitor(
            threshold_db=self.config.get_silence_threshold_db(),
            device=self.config.get_mic_device(),
        )

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
        xml_file = movement.get("xml_file")
        if not xml_file:
            logger.error("Movement has no xml_file: %s", movement)
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
        self.state.set_movement(
            movement_id=movement.get("id"),
            xml_file=xml_file,
            triggers=triggers,
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

                # Silence gate: pymatchmaker keeps advancing the beat from
                # its score-prior even when the mic is dead silent.  Force
                # confidence to 0 in that case so the InertiaEngine cannot
                # falsely lock in tracking.  When AudioLevelMonitor failed
                # to open its stream, is_active() falls through to True so
                # we don't unfairly gate the matcher's own confidence.
                mic_available = self.audio_monitor.is_available()
                mic_level_db = self.audio_monitor.get_level_db()
                gate_active = mic_available and not self.audio_monitor.is_active()
                if gate_active:
                    raw_conf = 0.0

                beat, inertia_active, tempo = self.inertia.update(raw_beat, raw_conf)
                measure = mapper.beat_to_measure(beat)

                self.state.update_beat_measure(beat, measure)
                self.state.set_confidence(raw_conf)
                self.state.set_inertia_mode(inertia_active, tempo)
                self.state.set_mic_level(mic_level_db, gate_active, mic_available)

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
            R       : reset tracking state
            → / Space : manually advance one slide
            ←       : manually go back one slide
        """
        def _on_n(_event: tk.Event) -> None:
            logger.info("'N' key pressed → loading next movement")
            self._load_next_movement()

        def _on_r(_event: tk.Event) -> None:
            logger.info("'R' key pressed → resetting tracking state")
            self.inertia.reset_tracking()
            # Also clear fired triggers so the operator can re-fire from
            # the top after a manual reset.
            self._fired_trigger_measures.clear()

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
            "Operator hotkeys bound: N=next movement, R=reset tracking, "
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

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config_path = Path(args.config)
    if not config_path.exists():
        logger.error("Config file not found: %s", config_path)
        return 1

    try:
        app = SequentialFollower(str(config_path), slide_url=args.slide_url)
        app.run()
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.error("Fatal error: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
