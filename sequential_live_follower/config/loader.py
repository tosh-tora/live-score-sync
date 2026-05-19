#!/usr/bin/env python3
"""
config/loader.py - Configuration File Parser

Loads config.json and provides access to movement definitions,
trigger settings, and global parameters (cooldown, confidence threshold).
"""

import json
import logging
from pathlib import Path
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)

_VALID_ACTIONS = {"right", "left"}


class ConfigError(ValueError):
    """Raised when config.json has a syntax error or invalid structure.

    Caught by main() to print a user-readable message and exit(1) before any
    threads are spawned — prevents silent silently bad state.
    """


class ConfigLoader:
    """
    Parses and manages config.json.

    Schema:
    {
      "settings": {
        "cooldown_seconds": 3.0,
        "confidence_threshold": 0.4
      },
      "movements": [
        {
          "id": 1,
          "xml_file": "guide_mv1.xml",
          "triggers": [
            {"measure": 1, "action": "right", "note": "開始"},
            {"measure": 45, "action": "right", "note": "テーマA"}
          ]
        }
      ]
    }
    """

    def __init__(self, config_path: str):
        """
        Load and parse config.json.

        Args:
            config_path: Path to config.json

        Raises:
            FileNotFoundError: If config file doesn't exist
            json.JSONDecodeError: If config is invalid JSON
        """
        path = Path(config_path).resolve()
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        # Remember the directory so xml_file paths can be resolved relative
        # to the config file rather than the process working directory.
        self.config_dir: Path = path.parent

        try:
            with open(path, 'r', encoding='utf-8') as f:
                self.config = json.load(f)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"config.json の JSON 構文エラー: {exc.msg} "
                f"(行 {exc.lineno}, 列 {exc.colno})\n"
                f"  ヒント: カンマ忘れ・括弧の対応ミスが多い原因です"
            ) from exc

        # Extract sections
        self.settings = self.config.get('settings', {})
        self.movements = self.config.get('movements', [])
        self.current_movement_idx = 0

        self._validate()

        logger.info(
            f"Config loaded: {len(self.movements)} movements, "
            f"cooldown={self.get_cooldown_seconds()}s, "
            f"confidence_threshold={self.get_confidence_threshold()}"
        )

    def resolve_path(self, relative_or_absolute: str) -> str:
        """Resolve a path that may be relative to the config file's directory.

        xml_file values in config.json are treated as relative to the config
        file itself, so the operator can place config.json and the .xml files
        in the same folder and refer to them by name only — regardless of the
        working directory when the app is launched.

        Absolute paths are returned unchanged.
        """
        p = Path(relative_or_absolute)
        if p.is_absolute():
            return str(p)
        resolved = (self.config_dir / p).resolve()
        logger.debug("Resolved xml path: %s → %s", relative_or_absolute, resolved)
        return str(resolved)

    def _auto_discover_mxl(self) -> Optional[Path]:
        """Return the first .mxl file in config_dir (alphabetical order), or None."""
        mxl_files = sorted(self.config_dir.glob("*.mxl"))
        return mxl_files[0] if mxl_files else None

    def _validate(self) -> None:
        """Check config structure and raise ConfigError on the first problem found.

        Called once in __init__, before any other code runs.  Errors include
        the full JSON path (e.g. "movements[0].triggers[2].action") so the
        operator can fix the config without guessing.

        If a movement omits ``xml_file``, the config directory is searched for
        the first ``.mxl`` file (alphabetically).  This lets the operator drop
        a single score file next to config.json and skip the filename field
        entirely.
        """
        if not isinstance(self.movements, list) or not self.movements:
            raise ConfigError(
                "'movements' が空または存在しません。"
                "少なくとも1楽章（movement）を定義してください"
            )

        for mv_idx, movement in enumerate(self.movements):
            mv = f"movements[{mv_idx}]"

            if not movement.get("xml_file"):
                discovered = self._auto_discover_mxl()
                if discovered is None:
                    raise ConfigError(
                        f"{mv}: 'xml_file' が指定されておらず、"
                        f"{self.config_dir} に .mxl ファイルも見つかりません。\n"
                        f"  ヒント: MusicXML (.mxl) ファイルを config.json と同じ"
                        f"フォルダに置いてください"
                    )
                movement["xml_file"] = discovered.name
                logger.info(
                    "movements[%d]: xml_file 未指定 → '%s' を自動検出しました",
                    mv_idx,
                    discovered.name,
                )

            triggers = movement.get("triggers", [])
            if not isinstance(triggers, list):
                raise ConfigError(f"{mv}.triggers: リスト形式が必要です")

            for t_idx, trig in enumerate(triggers):
                tp = f"{mv}.triggers[{t_idx}]"

                # --- measure ---
                if "measure" not in trig:
                    raise ConfigError(f"{tp}: 'measure' フィールドがありません")
                try:
                    m = int(trig["measure"])
                    if m < 1:
                        raise ValueError
                except (TypeError, ValueError):
                    raise ConfigError(
                        f"{tp}.measure: 1 以上の整数が必要です "
                        f"(got {trig['measure']!r})"
                    )

                # --- action ---
                action = trig.get("action")
                if action not in _VALID_ACTIONS:
                    raise ConfigError(
                        f"{tp}.action: 'right' または 'left' が必要です "
                        f"(got {action!r})"
                    )

        logger.debug("Config validation passed (%d movements)", len(self.movements))

    def get_current_movement(self) -> Optional[Dict]:
        """
        Get current movement configuration.

        Returns:
            Movement dict, or None if at end
        """
        if self.current_movement_idx < len(self.movements):
            return self.movements[self.current_movement_idx]
        return None

    def next_movement(self) -> bool:
        """
        Advance to next movement.

        Returns:
            True if successful, False if already at last movement
        """
        if self.current_movement_idx < len(self.movements) - 1:
            self.current_movement_idx += 1
            movement = self.get_current_movement()
            logger.info(
                f"Advanced to movement {self.current_movement_idx + 1}/{len(self.movements)}: "
                f"{movement.get('xml_file', 'unknown')}"
            )
            return True
        else:
            logger.warning("Already at last movement")
            return False

    def previous_movement(self) -> bool:
        """
        Go back to previous movement.

        Returns:
            True if successful, False if already at first movement
        """
        if self.current_movement_idx > 0:
            self.current_movement_idx -= 1
            movement = self.get_current_movement()
            logger.info(
                f"Returned to movement {self.current_movement_idx + 1}/{len(self.movements)}: "
                f"{movement.get('xml_file', 'unknown')}"
            )
            return True
        else:
            logger.warning("Already at first movement")
            return False

    def get_cooldown_seconds(self) -> float:
        """Get trigger cooldown duration."""
        return self.settings.get('cooldown_seconds', 3.0)

    def get_confidence_threshold(self) -> float:
        """Get confidence threshold for inertia activation."""
        return self.settings.get('confidence_threshold', 0.4)

    def get_silence_threshold_db(self) -> float:
        """Get mic RMS threshold (dBFS) below which we treat input as silent.

        When the live mic level is below this threshold, the matcher's
        reported confidence is forced to 0 — protecting against
        pymatchmaker's tendency to keep advancing the beat on silence /
        background noise via its score-driven prior.

        Default lowered to **-42 dBFS** after operator feedback that
        -35 was missing genuine soft playing (the user's piano passages
        sit around -34..-40 dBFS in the WSL2 + RDP-audio path).  -42 is
        a few dB above the room floor (-44 to -50 dBFS in our setup)
        so the gate still fires for true silence but stays open during
        real performance.  Non-musical content that survives this
        gate is filtered by the spectral-flatness check and (once
        calibrated) the DTW match-cost gate.
        """
        return self.settings.get('silence_threshold_db', -42.0)

    def get_musical_flatness_threshold(self) -> float:
        """Get spectral-flatness threshold above which audio is non-musical.

        Spectral flatness ∈ [0, 1]:
          * tonal (instruments, voice singing): 0.05 - 0.15
          * broadband noise (room hum, breath, taps): 0.40 - 0.80

        Blocks with flatness ≥ this value are gated identically to
        "below silence_threshold_db" — the matcher is frozen so it
        cannot lock onto a non-musical loud sound (cough, tap, speech,
        paper rustle) that survived the RMS-only silence gate.

        0.25 is a conservative midpoint that catches the typical
        ambient noise of an empty hall while passing through any
        plausible instrument timbre.  Raise toward 0.35-0.40 if soft
        wind or breathy strings are being gated; lower toward 0.20
        if loud noise still drives advances.
        """
        return self.settings.get('musical_flatness_threshold', 0.25)

    def get_inertia_timeout_seconds(self) -> float:
        """Get the max seconds inertia extrapolation runs before resetting.

        Defensive fallback: if confidence stays below threshold for this
        long after tracking initially locked in, we reset to the
        waiting-for-tracking state so a stale lock-in can't drive slides
        forever.
        """
        return self.settings.get('inertia_timeout_seconds', 5.0)

    def get_matcher_kwargs(self) -> dict:
        """Return extra kwargs forwarded to pymatchmaker's Matchmaker.

        Used to tune the OLTW algorithm (see matcher.MatchMaker.__init__
        for the keys and rationale).  Default narrows ``start_window_size``
        and ``step_size`` from the upstream defaults — both are needed to
        prevent the matcher from racing through the first few seconds of
        the score whenever a repeating motif (e.g. Beethoven 5 opening)
        makes early matches ambiguous.
        """
        # Defaults tuned against pymatchmaker 0.2.1's OnlineTimeWarpingArzt.
        # These attributes are patched onto score_follower AFTER Matchmaker
        # constructs it (matcher.py).  The algorithm stores values in
        # already-multiplied-by-frame_rate form:
        #   - window_size: in FRAMES (default 150 = 5 s @ 30 fps; too wide,
        #     allows motif-repeats to dominate)
        #   - step_size: in REF-FRAMES per input frame (default 5; way more
        #     than enough to drift forward through fast passages)
        #   - start_window_size: in FRAMES, set by __init__ from
        #     ``round(seconds * frame_rate)``; we override the frame count
        #     directly post-construction
        default_kwargs = {
            "window_size": 30,        # ~1 s @ 30 fps
            "step_size": 1,           # advance at most 1 ref-frame per input
            "start_window_size": 8,   # ~0.27 s — keep startup tight
        }
        user_kwargs = self.settings.get("matcher_kwargs", {})
        if not isinstance(user_kwargs, dict):
            logger.warning(
                "settings.matcher_kwargs must be a dict; got %r — ignoring",
                user_kwargs,
            )
            user_kwargs = {}
        merged = {**default_kwargs, **user_kwargs}
        return merged

    def get_mic_device(self):
        """Return the audio input device hint (None / int / str).

        Used both by the pymatchmaker MatchMaker and the AudioLevelMonitor
        so they consume the same source.  On WSL2, the ALSA ``default``
        device routes to a non-functional null sink while ``pulse`` is the
        one that actually receives the Windows microphone via WSLg's RDP
        audio bridge — so we default to ``"pulse"`` when nothing is
        configured.  Override with a specific index or name in config.json
        if you have multiple devices.
        """
        # ``None`` is a valid explicit value meaning "let the OS pick the
        # default", so we distinguish it from "missing key".
        if 'mic_device' in self.settings:
            return self.settings['mic_device']
        return 'pulse'

    def get_movement_triggers(self, movement_id: Optional[int] = None) -> List[Dict]:
        """
        Get triggers for a movement.

        Args:
            movement_id: Movement ID (if None, use current)

        Returns:
            List of trigger dicts
        """
        if movement_id is None:
            movement = self.get_current_movement()
        else:
            # Find movement by ID
            movement = next(
                (m for m in self.movements if m.get('id') == movement_id),
                None
            )

        if movement:
            triggers = movement.get('triggers', [])
            # Sort by measure for convenience
            return sorted(triggers, key=lambda t: t.get('measure', 0))
        return []

    def get_xml_file_for_movement(self, movement_idx: Optional[int] = None) -> Optional[str]:
        """
        Get XML file path for a movement.

        Args:
            movement_idx: Movement index (if None, use current)

        Returns:
            XML file path, or None if not found
        """
        if movement_idx is None:
            movement = self.get_current_movement()
        else:
            if 0 <= movement_idx < len(self.movements):
                movement = self.movements[movement_idx]
            else:
                return None

        return movement.get('xml_file') if movement else None

    def total_movements(self) -> int:
        """Get total number of movements."""
        return len(self.movements)

    def current_movement_number(self) -> int:
        """Get 1-indexed movement number."""
        return self.current_movement_idx + 1

    def __repr__(self) -> str:
        return (
            f"ConfigLoader("
            f"movements={len(self.movements)}, "
            f"current={self.current_movement_number()}/{self.total_movements()}, "
            f"cooldown={self.get_cooldown_seconds()}s)"
        )
