#!/usr/bin/env python3
"""
main.py - Sequential Live Follower Main Application

Orchestrates all threads and components:
- Audio capture (background thread)
- Feature extraction (background thread)
- Matching engine (background thread)
- Trigger execution (background thread)
- GUI (main thread, tkinter loop)
- Keyboard listener (global 'N' key for next movement)

Usage:
    python -m sequential_live_follower.main config.json
"""

import argparse
import logging
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from queue import Queue, Empty

try:
    import keyboard
except ImportError:
    keyboard = None

from sequential_live_follower.core.audio_capturer import AudioCapturer
from sequential_live_follower.core.feature_extractor import FeatureExtractor
from sequential_live_follower.core.matcher import MatchMaker
from sequential_live_follower.core.score_mapper import ScoreMapper
from sequential_live_follower.core.state_manager import AppState
from sequential_live_follower.core.inertia_engine import InertiaEngine
from sequential_live_follower.core.cooldown_timer import CooldownTimer
from sequential_live_follower.config.loader import ConfigLoader
from sequential_live_follower.ui.gui_tkinter import FollowerGUI

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)


class SequentialFollower:
    """
    Main application orchestrator.

    Manages all worker threads, state, and UI.
    """

    def __init__(self, config_path: str):
        """
        Initialize application.

        Args:
            config_path: Path to config.json
        """
        logger.info(f"Initializing SequentialFollower with {config_path}")

        # Load configuration
        self.config = ConfigLoader(config_path)

        # Central state
        self.state = AppState()

        # Worker threads (initially None)
        self.audio_worker: threading.Thread | None = None
        self.feature_worker: threading.Thread | None = None
        self.matcher_worker: threading.Thread | None = None
        self.trigger_worker: threading.Thread | None = None

        # Thread-safe queues
        self.audio_queue = Queue(maxsize=10)
        self.feature_queue = Queue(maxsize=5)

        # Processing objects
        self.score_mapper: ScoreMapper | None = None
        self.matcher: MatchMaker | None = None
        self.inertia = InertiaEngine(self.config.get_confidence_threshold())
        self.cooldown = CooldownTimer(self.config.get_cooldown_seconds())

        # GUI
        self.root = tk.Tk()
        self.gui = FollowerGUI(self.root, self.state)

        # Keyboard listener flag
        self._listener_active = False

        logger.info("SequentialFollower initialization complete")

    def _setup_keyboard_listener(self):
        """
        Register global 'N' key listener for next movement.

        Uses 'keyboard' library to intercept 'n' key globally.
        """
        if keyboard is None:
            logger.warning(
                "keyboard library not installed. Global hotkey not available. "
                "Install with: pip install keyboard"
            )
            return

        def on_n_press():
            logger.info("'N' key pressed")
            self._load_next_movement()

        try:
            keyboard.add_hotkey('n', on_n_press)
            self._listener_active = True
            logger.info("Global 'N' key listener registered")
        except Exception as e:
            logger.error(f"Error setting up keyboard listener: {e}")

    def _load_current_movement(self):
        """Load current movement (used for initial load)."""
        movement = self.config.get_current_movement()
        if not movement:
            logger.error("No movement available to load")
            return

        xml_file = movement.get('xml_file')
        if not xml_file:
            logger.error("No xml_file in movement config")
            return

        logger.info(f"Loading movement: {xml_file}")

        # Create score mapper and matcher
        try:
            self.score_mapper = ScoreMapper(xml_file)
            self.matcher = MatchMaker(xml_file)
            self.matcher.reset()
            self.inertia.reset()
            self.cooldown.cleanup_old()

            logger.info(f"Loaded score: {self.score_mapper}")

        except Exception as e:
            logger.error(f"Error loading score: {e}")
            self.state.set_next_trigger(None)
            return

        # Start audio and feature workers
        self.audio_worker = AudioCapturer(self.audio_queue)
        self.audio_worker.start()

        self.feature_worker = FeatureExtractor(self.audio_queue, self.feature_queue)
        self.feature_worker.start()

        # Update state
        triggers = movement.get('triggers', [])
        self.state.set_movement(
            movement_id=movement.get('id'),
            xml_file=xml_file,
            triggers=triggers
        )

        # Set next trigger measure for display
        if triggers:
            next_measure = triggers[0]['measure']
            self.state.set_next_trigger(next_measure)

        logger.info("Movement loaded successfully")

    def _load_next_movement(self):
        """Load next movement from config."""
        if not self.config.next_movement():
            logger.warning("No more movements to load")
            self.state.set_next_trigger(None)
            return

        movement = self.config.get_current_movement()
        if not movement:
            logger.error("Failed to get movement config")
            return

        xml_file = movement.get('xml_file')
        if not xml_file:
            logger.error("No xml_file in movement config")
            return

        logger.info(f"Loading movement: {xml_file}")

        # Stop old workers
        if self.audio_worker:
            logger.info("Stopping audio worker...")
            self.audio_worker.stop()
            time.sleep(0.5)

        if self.feature_worker:
            logger.info("Stopping feature worker...")
            self.feature_worker.stop()
            time.sleep(0.5)

        # Reset queues
        while not self.audio_queue.empty():
            try:
                self.audio_queue.get_nowait()
            except:
                break

        while not self.feature_queue.empty():
            try:
                self.feature_queue.get_nowait()
            except:
                break

        # Create new score mapper and matcher
        try:
            self.score_mapper = ScoreMapper(xml_file)
            self.matcher = MatchMaker(xml_file)
            self.matcher.reset()
            self.inertia.reset()
            self.cooldown.cleanup_old()

            logger.info(f"Loaded score: {self.score_mapper}")

        except Exception as e:
            logger.error(f"Error loading score: {e}")
            self.state.set_next_trigger(None)
            return

        # Restart audio and feature workers
        self.audio_worker = AudioCapturer(self.audio_queue)
        self.audio_worker.start()

        self.feature_worker = FeatureExtractor(self.audio_queue, self.feature_queue)
        self.feature_worker.start()

        # Update state
        triggers = movement.get('triggers', [])
        self.state.set_movement(
            movement_id=movement.get('id'),
            xml_file=xml_file,
            triggers=triggers
        )

        # Set next trigger measure for display
        if triggers:
            next_measure = triggers[0]['measure']
            self.state.set_next_trigger(next_measure)

        logger.info("Movement loaded successfully")

    def _matching_loop(self):
        """
        Background thread: consume features, match, update beat/measure.

        Runs until thread is stopped.
        """
        logger.info("Matching loop started")

        while True:
            try:
                # Check if matcher is available
                if not self.matcher or not self.score_mapper:
                    time.sleep(0.1)
                    continue

                # Get feature from queue (timeout expected when no audio)
                try:
                    chroma = self.feature_queue.get(timeout=1.0)
                except Empty:
                    # Normal: no feature available yet
                    continue

                # Run matcher
                beat, confidence = self.matcher.update(chroma)

                # Apply inertia if needed
                beat, inertia_active, tempo = self.inertia.update(beat, confidence)

                # Convert beat to measure
                measure = self.score_mapper.beat_to_measure(beat)

                # Update state
                self.state.update_beat_measure(beat, measure)
                self.state.set_confidence(confidence)
                self.state.set_inertia_mode(inertia_active, tempo)

            except Exception as e:
                logger.error(f"Matching loop error: {type(e).__name__}: {e}", exc_info=True)
                time.sleep(0.1)

    def _trigger_loop(self):
        """
        Background thread: check trigger conditions and execute actions.

        Monitors current measure and executes triggers when conditions are met.
        """
        logger.info("Trigger loop started")

        while True:
            try:
                time.sleep(0.1)  # Poll every 100ms

                state = self.state.get_all()
                current_measure = state['measure']
                triggers = self.state.current_triggers

                if not triggers:
                    continue

                # Find all triggers at current measure
                current_triggers = [t for t in triggers if t['measure'] == current_measure]

                if not current_triggers:
                    # Find next trigger
                    next_measures = [t['measure'] for t in triggers if t['measure'] > current_measure]
                    if next_measures:
                        self.state.set_next_trigger(min(next_measures))
                    else:
                        self.state.set_next_trigger(None)
                    continue

                # Check for trigger conditions
                for trigger in current_triggers:
                    measure = trigger['measure']

                    if state['cooldown_active']:
                        logger.debug(f"Measure {measure}: cooldown active, skipping")
                        continue

                    if not self.cooldown.should_trigger(measure):
                        logger.debug(f"Measure {measure}: in rate-limit cooldown")
                        continue

                    # Execute trigger
                    action = trigger['action']
                    note = trigger.get('note', '')

                    try:
                        self._execute_action(action)
                        logger.info(f"Trigger executed at measure {measure}: {action} ({note})")

                        self.cooldown.mark_triggered(measure)
                        self.state.activate_cooldown(self.config.get_cooldown_seconds())

                    except Exception as e:
                        logger.error(f"Error executing trigger: {type(e).__name__}: {e}", exc_info=True)

                # Find and set next trigger
                next_measures = [t['measure'] for t in triggers if t['measure'] > current_measure]
                if next_measures:
                    self.state.set_next_trigger(min(next_measures))
                else:
                    self.state.set_next_trigger(None)

            except Exception as e:
                logger.error(f"Trigger loop error: {type(e).__name__}: {e}", exc_info=True)

    def _execute_action(self, action: str):
        """
        Execute keyboard action (pyautogui).

        Args:
            action: Key name (e.g., 'right', 'left', 'up', 'down')
        """
        try:
            import pyautogui
            pyautogui.press(action)
            logger.debug(f"Pressed key: {action}")
        except ImportError:
            logger.error("pyautogui not installed, cannot execute action")
        except Exception as e:
            logger.error(f"Error executing action '{action}': {e}")

    def run(self):
        """
        Start application.

        Blocks until GUI window is closed.
        """
        logger.info("Starting application")

        # Setup keyboard listener
        self._setup_keyboard_listener()

        # Load first movement automatically
        logger.info("Auto-loading first movement...")
        self._load_current_movement()

        # Start worker threads (daemon)
        logger.info("Starting worker threads...")

        matching_thread = threading.Thread(target=self._matching_loop, daemon=True)
        matching_thread.start()

        trigger_thread = threading.Thread(target=self._trigger_loop, daemon=True)
        trigger_thread.start()

        # Prompt user
        logger.info("Press 'N' to advance to next movement")

        # Run GUI main loop (blocks until window closed)
        try:
            self.gui.on_closing = self._on_gui_closing
            self.root.protocol("WM_DELETE_WINDOW", self._on_gui_closing)
            self.root.mainloop()
        except KeyboardInterrupt:
            logger.info("Interrupted")
            self._cleanup()

    def _on_gui_closing(self):
        """Handle GUI window close."""
        logger.info("GUI closing...")
        self._cleanup()
        self.root.destroy()

    def _cleanup(self):
        """Clean up resources."""
        logger.info("Cleaning up...")

        # Stop workers
        if self.audio_worker:
            self.audio_worker.stop()

        if self.feature_worker:
            self.feature_worker.stop()

        logger.info("Cleanup complete")


def main():
    """Entry point."""
    parser = argparse.ArgumentParser(
        description='Sequential Live Follower - Real-time orchestral performance tracker'
    )
    parser.add_argument(
        'config',
        help='Path to config.json'
    )
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Enable verbose logging'
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Error: Config file not found: {config_path}")
        return 1

    try:
        app = SequentialFollower(str(config_path))
        app.run()
        return 0
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
