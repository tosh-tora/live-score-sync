#!/usr/bin/env python3
"""
state_manager.py - Thread-Safe Central State Store

Manages all application state with proper locking.
Signals UI updates via event mechanism.
"""

import logging
import threading
import time
from typing import Optional, List

logger = logging.getLogger(__name__)


class AppState:
    """
    Central state repository for the Sequential Live Follower application.

    All state reads/writes are protected by a threading.Lock to prevent race conditions.
    """

    def __init__(self):
        """Initialize state with defaults."""
        self._lock = threading.Lock()

        # Current movement context
        self.current_movement_id: Optional[int] = None
        self.current_xml_file: Optional[str] = None
        self.current_triggers: List[dict] = []

        # Playback state
        self.current_beat: float = 0.0
        self.current_measure: int = 1
        self.confidence: float = 0.0

        # Trigger/cooldown state
        self.last_trigger_measure: Optional[int] = None
        self.last_trigger_time: Optional[float] = None
        self.next_trigger_measure: Optional[int] = None
        self.cooldown_active: bool = False

        # Inertia mode (fallback when confidence is low)
        self.inertia_mode: bool = False
        self.inertia_tempo_bpm: Optional[float] = None

        # Live microphone level (dBFS) and whether the silence gate is
        # currently suppressing matcher confidence.  Surfaced to the GUI
        # so the operator can tell whether the mic is actually being heard.
        # ``mic_monitor_available`` is False when AudioLevelMonitor failed
        # to open its stream — in that case dBFS is meaningless and the
        # silence gate is bypassed.
        self.mic_level_db: float = -120.0
        self.silence_gate_active: bool = False
        self.mic_monitor_available: bool = False

        # UI update signaling
        self.ui_update_event = threading.Event()

    def get_all(self) -> dict:
        """
        Atomically get snapshot of all state.

        Returns:
            Dict with all current state values
        """
        with self._lock:
            return {
                'movement_id': self.current_movement_id,
                'xml_file': self.current_xml_file,
                'beat': self.current_beat,
                'measure': self.current_measure,
                'confidence': self.confidence,
                'inertia_mode': self.inertia_mode,
                'cooldown_active': self.cooldown_active,
                'next_trigger_measure': self.next_trigger_measure,
                'last_trigger_measure': self.last_trigger_measure,
                'mic_level_db': self.mic_level_db,
                'silence_gate_active': self.silence_gate_active,
                'mic_monitor_available': self.mic_monitor_available,
            }

    def update_beat_measure(self, beat: float, measure: int):
        """
        Update beat and measure atomically.

        Args:
            beat: Continuous beat position
            measure: Measure number

        Triggers UI update event.
        """
        with self._lock:
            self.current_beat = beat
            self.current_measure = measure
        self.ui_update_event.set()

    def set_confidence(self, confidence: float):
        """
        Update confidence score.

        Args:
            confidence: [0.0, 1.0]
        """
        with self._lock:
            self.confidence = max(0.0, min(1.0, confidence))
        self.ui_update_event.set()

    def set_movement(self, movement_id: int, xml_file: str, triggers: List[dict]):
        """
        Set current movement context.

        Args:
            movement_id: Movement ID from config
            xml_file: Path to MusicXML file
            triggers: List of trigger dicts
        """
        with self._lock:
            self.current_movement_id = movement_id
            self.current_xml_file = xml_file
            self.current_triggers = triggers
            self.current_beat = 0.0
            self.current_measure = 1
            self.confidence = 0.0
            self.inertia_mode = False
            self.cooldown_active = False
            self.last_trigger_measure = None
            self.next_trigger_measure = None
        self.ui_update_event.set()

    def set_inertia_mode(self, active: bool, tempo_bpm: Optional[float] = None):
        """
        Activate/deactivate inertia mode.

        Args:
            active: True to activate
            tempo_bpm: Estimated tempo (for extrapolation)
        """
        with self._lock:
            self.inertia_mode = active
            if tempo_bpm is not None:
                self.inertia_tempo_bpm = tempo_bpm
        self.ui_update_event.set()

    def set_mic_level(
        self,
        level_db: float,
        gate_active: bool,
        monitor_available: bool = True,
    ):
        """Update mic level (dBFS), silence-gate state and monitor health.

        Args:
            level_db: Current mic RMS level in dBFS.
            gate_active: True if the silence gate is currently forcing
                matcher confidence to 0 (i.e. level_db <= threshold).
            monitor_available: False if AudioLevelMonitor failed to open
                its input stream — dBFS is then meaningless and the gate
                is bypassed.
        """
        with self._lock:
            self.mic_level_db = level_db
            self.silence_gate_active = gate_active
            self.mic_monitor_available = monitor_available
        self.ui_update_event.set()

    def set_next_trigger(self, measure_num: Optional[int]):
        """
        Set next trigger measure for display.

        Args:
            measure_num: Measure number, or None if no upcoming trigger
        """
        with self._lock:
            self.next_trigger_measure = measure_num
        self.ui_update_event.set()

    def activate_cooldown(self, duration_sec: float):
        """
        Activate cooldown timer.

        Args:
            duration_sec: Cooldown duration in seconds

        Spawns background timer thread to auto-clear.
        """
        with self._lock:
            self.cooldown_active = True
            self.last_trigger_time = time.time()

        # Timer to auto-clear
        def clear_cooldown():
            time.sleep(duration_sec)
            self.deactivate_cooldown()

        threading.Timer(duration_sec, clear_cooldown).start()
        self.ui_update_event.set()

    def deactivate_cooldown(self):
        """Deactivate cooldown."""
        with self._lock:
            self.cooldown_active = False
        self.ui_update_event.set()

    def __repr__(self) -> str:
        state = self.get_all()
        return (
            f"AppState(measure={state['measure']}, beat={state['beat']:.1f}, "
            f"confidence={state['confidence']:.2f}, inertia={state['inertia_mode']})"
        )
