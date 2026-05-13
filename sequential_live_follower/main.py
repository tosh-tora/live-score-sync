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
        self.inertia = InertiaEngine(self.config.get_confidence_threshold())
        self.cooldown = CooldownTimer(self.config.get_cooldown_seconds())

        # Per-movement objects (recreated each load)
        self.score_mapper: ScoreMapper | None = None
        self.matcher: MatchMaker | None = None

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
            self.matcher = MatchMaker(score_file=xml_file, input_type="audio")
            self.matcher.start()
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to start matcher: %s", exc, exc_info=True)
            self.matcher = None
            return

        # Reset cross-movement helpers
        self.inertia.reset()
        self.cooldown.cleanup_old()

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
                beat, inertia_active, tempo = self.inertia.update(raw_beat, raw_conf)
                measure = mapper.beat_to_measure(beat)

                self.state.update_beat_measure(beat, measure)
                self.state.set_confidence(raw_conf)
                self.state.set_inertia_mode(inertia_active, tempo)

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

                # Update "next trigger" display
                upcoming = [t["measure"] for t in triggers if t["measure"] > current_measure]
                self.state.set_next_trigger(min(upcoming) if upcoming else None)

                # Fire any trigger whose measure has been reached and isn't
                # in cooldown.
                if snapshot["cooldown_active"]:
                    time.sleep(interval)
                    continue

                for trig in triggers:
                    if trig["measure"] != current_measure:
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
        """Bind 'N' (next) to the Tk root window.

        Bindings are scoped to the operator GUI window. The operator screen
        must have focus for the key to register — this is intentional so the
        audience-facing Chromium window does not steal the binding.
        """
        def _on_n(_event: tk.Event) -> None:
            logger.info("'N' key pressed → loading next movement")
            self._load_next_movement()

        self.root.bind("<KeyPress-n>", _on_n)
        self.root.bind("<KeyPress-N>", _on_n)
        logger.info("'N' key bound to next-movement on operator GUI")


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
