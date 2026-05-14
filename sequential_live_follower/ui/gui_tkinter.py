#!/usr/bin/env python3
"""
ui/gui_tkinter.py - Real-Time Display GUI

Tkinter-based GUI showing:
- Current file name
- Current measure (large)
- Confidence score (color-coded)
- Next trigger measure
- Inertia mode indicator
"""

import logging
import tkinter as tk
from tkinter import font

from sequential_live_follower.core.state_manager import AppState

logger = logging.getLogger(__name__)

# Font families preferred for rendering Japanese filenames / labels.  We pick
# the first one that the local Tk installation actually has — falling back to
# the generic "TkDefaultFont" so the GUI still works (with tofu glyphs) when
# no CJK font is installed.  On WSL2/Ubuntu, `sudo apt install fonts-noto-cjk`
# makes "Noto Sans CJK JP" available.
_PREFERRED_FONT_FAMILIES = (
    "Noto Sans CJK JP",
    "Noto Sans JP",
    "Yu Gothic UI",
    "Yu Gothic",
    "Meiryo",
    "MS Gothic",
    "TakaoPGothic",
    "TakaoGothic",
    "IPAexGothic",
    "IPAPGothic",
    "Hiragino Sans",
    "DejaVu Sans",
)


def _pick_font_family(root: tk.Tk) -> str:
    """Return the first available CJK-capable font family for this Tk root."""
    try:
        available = set(font.families(root=root))
    except Exception:  # noqa: BLE001 — Tk could be in a weird state
        available = set()
    for family in _PREFERRED_FONT_FAMILIES:
        if family in available:
            logger.info("GUI font family: %s", family)
            return family
    logger.warning(
        "No CJK-capable font found among %s — Japanese text may render as tofu. "
        "Install fonts-noto-cjk (Ubuntu) or equivalent.",
        _PREFERRED_FONT_FAMILIES,
    )
    return "TkDefaultFont"


class FollowerGUI:
    """
    Tkinter GUI for Sequential Live Follower.

    Displays playback status in real-time without blocking.
    """

    def __init__(self, root: tk.Tk, state: AppState):
        """
        Initialize GUI.

        Args:
            root: tkinter root window
            state: Shared AppState object
        """
        self.root = root
        self.state = state

        self.root.title("Sequential Live Follower")
        self.root.geometry("900x500")
        self.root.configure(bg="#f0f0f0")

        # Pick a font family that can actually render Japanese.  The previous
        # hard-coded "Arial" has no CJK glyphs, so Japanese filenames (e.g.
        # "運命_冒頭_guide.mxl") rendered as tofu boxes.
        self._font_family = _pick_font_family(self.root)

        # Create widgets
        self._create_widgets()

        # Start polling for state updates
        self._poll_state()

        logger.info("GUI initialized")

    def _create_widgets(self):
        """Create and layout tkinter widgets."""
        family = self._font_family

        # Title
        title_font = font.Font(family=family, size=14, weight="bold")
        title_label = tk.Label(
            self.root, text="Sequential Live Follower", font=title_font, bg="#f0f0f0"
        )
        title_label.pack(pady=10)

        # File name (large font)
        file_font = font.Font(family=family, size=16, weight="bold")
        self.label_file = tk.Label(
            self.root, text="[No file loaded]", font=file_font, bg="#f0f0f0", fg="#333"
        )
        self.label_file.pack(pady=10)

        # Current measure (very large)
        measure_font = font.Font(family=family, size=72, weight="bold")
        self.label_measure = tk.Label(
            self.root,
            text="--",
            font=measure_font,
            bg="#f0f0f0",
            fg="blue"
        )
        self.label_measure.pack(pady=20)

        # Confidence bar frame
        conf_frame = tk.Frame(self.root, bg="#f0f0f0")
        conf_frame.pack(pady=15)

        conf_label = tk.Label(conf_frame, text="Confidence:", font=(family, 12), bg="#f0f0f0")
        conf_label.pack(side=tk.LEFT, padx=10)

        self.label_confidence = tk.Label(
            conf_frame, text="-- (--)", font=(family, 12), bg="#f0f0f0", fg="gray"
        )
        self.label_confidence.pack(side=tk.LEFT, padx=10)

        # Progress bar (simple visual representation)
        self.canvas_confidence = tk.Canvas(
            conf_frame, width=200, height=20, bg="white", highlightthickness=1
        )
        self.canvas_confidence.pack(side=tk.LEFT, padx=10)

        # Next trigger measure
        trigger_font = font.Font(family=family, size=14)
        self.label_next_trigger = tk.Label(
            self.root,
            text="Next trigger: --",
            font=trigger_font,
            bg="#f0f0f0",
            fg="#555"
        )
        self.label_next_trigger.pack(pady=5)

        # Inertia mode indicator
        self.label_inertia = tk.Label(
            self.root, text="", font=(family, 12, "bold"), bg="#f0f0f0", fg="red"
        )
        self.label_inertia.pack(pady=5)

        # Cooldown indicator
        self.label_cooldown = tk.Label(
            self.root, text="", font=(family, 11), bg="#f0f0f0", fg="orange"
        )
        self.label_cooldown.pack(pady=2)

        # Key hints (always visible at the bottom)
        self.label_hints = tk.Label(
            self.root,
            text="N: 次の楽章   R: 追従状態をリセット",
            font=(family, 10),
            bg="#f0f0f0",
            fg="#888",
        )
        self.label_hints.pack(side=tk.BOTTOM, pady=8)

    def update_display(self):
        """Update GUI with current state."""
        try:
            state = self.state.get_all()

            # File name (show basename only, handles both / and \ separators)
            filename = state['xml_file'] or "[No file]"
            if isinstance(filename, str):
                filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
            self.label_file.config(text=filename)

            # Measure (large)
            measure = state['measure']
            self.label_measure.config(text=str(measure))

            # Confidence with color coding
            conf = state['confidence']
            if conf > 0.6:
                color = "green"
            elif conf > 0.4:
                color = "orange"
            else:
                color = "red"

            self.label_confidence.config(
                text=f"{conf:.2f} ({int(conf*100)}%)",
                fg=color
            )

            # Confidence bar
            self.canvas_confidence.delete("all")
            bar_width = 200 * conf
            self.canvas_confidence.create_rectangle(0, 0, bar_width, 20, fill=color, outline="black")

            # Next trigger
            next_trig = state['next_trigger_measure']
            if next_trig:
                self.label_next_trigger.config(text=f"Next trigger: {next_trig}")
            else:
                self.label_next_trigger.config(text="Next trigger: --")

            # Inertia indicator
            if state['inertia_mode']:
                self.label_inertia.config(text="⚠ INERTIA MODE")
            else:
                self.label_inertia.config(text="")

            # Cooldown indicator
            if state['cooldown_active']:
                self.label_cooldown.config(text="🔒 Cooldown active")
            else:
                self.label_cooldown.config(text="")

        except Exception as e:
            logger.error(f"GUI update error: {e}")

    def _poll_state(self):
        """Poll state for updates every 100ms."""
        try:
            self.update_display()
        except Exception as e:
            logger.error(f"Polling error: {e}")

        # Schedule next poll
        self.root.after(100, self._poll_state)

    def on_closing(self):
        """Handle window close event."""
        logger.info("GUI closing")
        self.root.destroy()
