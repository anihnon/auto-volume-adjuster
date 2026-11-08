"""
Auto Volume Adjuster
====================

A Python-based utility that continuously monitors ambient background noise
through the default microphone and dynamically adjusts the system master
volume to keep audio at an optimal level relative to the environment.

Features
--------
* Real-time ambient sound level monitoring (RMS / dBFS)
* Exponential moving average smoothing to prevent erratic volume jumps
* Configurable sensitivity, update interval, and volume bounds
* Cross-platform system volume control:
    - Windows: pycaw (Core Audio API)
    - macOS: osascript
    - Linux: amixer / pactl
* Graceful shutdown on Ctrl+C
* Comprehensive error handling (missing microphone, permission errors, etc.)

Author: Auto Volume Adjuster Project
License: MIT
"""

from __future__ import annotations

import sys
import time
import signal
import logging
import platform
import json
import argparse
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

# Non-blocking keyboard input (Windows only)
try:
    import msvcrt

    HAS_KEYPRESS = True
except ImportError:
    HAS_KEYPRESS = False

import numpy as np
import sounddevice as sd


# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("auto_volume_adjuster")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass
class Config:
    """Configuration parameters for the auto volume adjuster."""

    # Microphone / audio settings
    sample_rate: int = 44100          # Audio sample rate (Hz)
    chunk_duration: float = 0.5       # Duration of each audio chunk (seconds)
    device_index: Optional[int] = None  # None = use default input device

    # Smoothing: exponential moving average factor (0.0 = no smoothing,
    # 1.0 = heavy smoothing). Higher values react more slowly to changes.
    smoothing_alpha: float = 0.3


    # Volume bounds (0.0 to 1.0)
    min_volume: float = 0.15          # Never go below 15%
    max_volume: float = 0.85          # Never go above 85%

    # Reference ambient level where no adjustment is needed (dBFS).
    # Louder than this -> volume increases; quieter -> volume decreases.
    neutral_db: float = -40.0

    # Maximum volume change per update cycle (0.0 to 1.0).
    # 0.05 = at most 5% up or down per cycle.
    max_step: float = 0.05

    # Deadband (dB) around neutral_db to ignore tiny fluctuations.
    deadband_db: float = 2.0

    # --- Feedback loop detection -----------------------------------------
    # If we just raised volume and ambient dB increases by this much or more
    # on the next read, it's likely the computer's own sound being picked up
    # by the microphone (feedback loop), not real background noise.
    feedback_detection_enabled: bool = True
    feedback_db_threshold: float = 6.0        # dB increase that triggers feedback detection

    # Maximum number of consecutive automatic volume increases before
    # the script stops increasing (prevents runaway feedback).
    max_consecutive_increases: int = 4

    # How many dB above the measured baseline noise floor must ambient
    # be before the script considers increasing volume. Set during setup.
    noise_floor_db: Optional[float] = None

    # Update interval (seconds) between volume adjustments
    update_interval: float = 1.0

    # How many consecutive failures before giving up
    max_consecutive_errors: int = 5

    # --- Learning / preference settings ------------------------------------
    # File where learned user preferences are stored
    preferences_file: str = "user_preferences.json"
    # Enable learning from manual volume changes
    enable_learning: bool = True
    # How strongly to adapt to user changes (0.0 = no adaptation, 1.0 = full)
    learning_rate: float = 0.3


# ---------------------------------------------------------------------------
# Cross-platform system volume control
# ---------------------------------------------------------------------------
class SystemVolumeController:
    """Abstracts OS-specific master volume control."""

    def __init__(self) -> None:
        self.os_name = platform.system()
        self._volume_handle = None
        self._initialized = False
        self._init_backend()

    # ------------------------------------------------------------------
    def _init_backend(self) -> None:
        """Initialise the platform-specific volume backend."""
        try:
            if self.os_name == "Windows":
                self._init_windows()
            elif self.os_name == "Darwin":
                self._init_macos()
            elif self.os_name == "Linux":
                self._init_linux()
            else:
                logger.warning("Unsupported OS: %s. Volume control disabled.", self.os_name)
            self._initialized = True
        except Exception as exc:  # pragma: no cover - platform dependent
            logger.error("Failed to initialise volume controller (%s): %s", self.os_name, exc)
            self._initialized = False

    # ------------------------------------------------------------------
    def _init_windows(self) -> None:  # pragma: no cover - Windows only
        """Initialise pycaw on Windows.

        Uses the pycaw AudioDevice.EndpointVolume property which is already
        a POINTER(IAudioEndpointVolume) — no manual Activate() needed.
        """
        from pycaw.pycaw import AudioUtilities

        devices = AudioUtilities.GetSpeakers()
        self._volume_handle = devices.EndpointVolume

    # ------------------------------------------------------------------
    def _init_macos(self) -> None:  # pragma: no cover - macOS only
        """macOS uses osascript; nothing to initialise."""
        self._volume_handle = None

    # ------------------------------------------------------------------
    def _init_linux(self) -> None:  # pragma: no cover - Linux only
        """Linux uses amixer/pactl; nothing to initialise."""
        self._volume_handle = None

    # ------------------------------------------------------------------
    def get_volume(self) -> float:
        """Return current master volume as a float in range [0.0, 1.0].

        Logs a warning if the backend is not initialized or fails,
        so you can see why the reported value might be wrong.
        """
        if not self._initialized:
            logger.warning(
                "Volume controller not initialized (%s). Returning fallback 0.5.",
                self.os_name,
            )
            return 0.5
        try:
            if self.os_name == "Windows" and self._volume_handle is not None:
                return float(self._volume_handle.GetMasterVolumeLevelScalar())
            if self.os_name == "Darwin":
                return self._get_volume_macos()
            if self.os_name == "Linux":
                return self._get_volume_linux()
        except Exception as exc:  # pragma: no cover - platform dependent
            logger.error("Failed to read system volume: %s", exc)
            logger.warning("Returning fallback 0.5 (50%%) because read failed.")
        return 0.5

    # ------------------------------------------------------------------
    def set_volume(self, volume: float) -> bool:
        """Set master volume to *volume* (float in range [0.0, 1.0]).

        Returns True if the volume was set and verified, False otherwise.
        After setting, the volume is read back to confirm the OS accepted it.
        """
        volume = max(0.0, min(1.0, volume))
        if not self._initialized:
            logger.warning(
                "Volume controller not initialized (%s). Cannot set volume.",
                self.os_name,
            )
            return False

        before = self.get_volume()
        try:
            if self.os_name == "Windows" and self._volume_handle is not None:
                self._volume_handle.SetMasterVolumeLevelScalar(volume, None)
            elif self.os_name == "Darwin":
                self._set_volume_macos(volume)
            elif self.os_name == "Linux":
                self._set_volume_linux(volume)
            else:
                logger.warning("Unsupported OS: %s. Volume not set.", self.os_name)
                return False
        except Exception as exc:  # pragma: no cover - platform dependent
            logger.error("Failed to set system volume: %s", exc)
            return False

        # Verify the change took effect by reading it back.
        time.sleep(0.1)  # Give the OS a moment to apply
        after = self.get_volume()
        changed = abs(after - volume) <= 0.05  # within 5% tolerance

        if changed:
            logger.info(
                "System volume set: %.0f%% -> %.0f%% (verified)",
                before * 100,
                after * 100,
            )
            return True
        else:
            logger.warning(
                "System volume set to %.0f%% but read back as %.0f%% — "
                "the OS may not have accepted the change.",
                volume * 100,
                after * 100,
            )
            return False
    # ------------------------------------------------------------------
    # macOS helpers (osascript)
    # ------------------------------------------------------------------
    @staticmethod
    def _get_volume_macos() -> float:
        import subprocess

        result = subprocess.run(
            ["osascript", "-e", "output volume of (get volume settings)"],
            capture_output=True,
            text=True,
            check=True,
        )
        return float(result.stdout.strip()) / 100.0

    @staticmethod
    def _set_volume_macos(volume: float) -> None:
        import subprocess

        percent = int(round(volume * 100))
        subprocess.run(
            ["osascript", "-e", f"set volume output volume {percent}"],
            check=True,
        )
    # ------------------------------------------------------------------
    # Linux helpers (amixer preferred, fallback to pactl)
    # ------------------------------------------------------------------
    @staticmethod
    def _get_volume_linux() -> float:
        import subprocess

        # Try amixer first
        try:
            result = subprocess.run(
                ["amixer", "get", "Master"],
                capture_output=True,
                text=True,
                check=True,
            )
            # Parse a line like: "Front Left: Playback 65% [on]"
            for line in result.stdout.splitlines():
                if "%" in line:
                    pct = line.split("%")[0].split()[-1]
                    return float(pct) / 100.0
        except (FileNotFoundError, subprocess.CalledProcessError, ValueError, IndexError):
            pass

        # Fallback to pactl
        try:
            result = subprocess.run(
                ["pactl", "get-sink-volume", "@DEFAULT_SINK@"],
                capture_output=True,
                text=True,
                check=True,
            )
            # pactl output like: "Volume: front-left: 65536 / 100% / 65.53%"
            for part in result.stdout.split("/"):
                part = part.strip()
                if part.endswith("%"):
                    return float(part.rstrip("%")) / 100.0
        except (FileNotFoundError, subprocess.CalledProcessError, ValueError):
            pass

        return 0.5

    @staticmethod
    def _set_volume_linux(volume: float) -> None:
        import subprocess

        percent = int(round(volume * 100))
        # Try amixer first
        try:
            subprocess.run(
                ["amixer", "sset", "Master", f"{percent}%"],
                capture_output=True,
                check=True,
            )
            return
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass

        # Fallback to pactl
        try:
            subprocess.run(
                ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{percent}%"],
                capture_output=True,
                check=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            logger.error("Linux volume control failed: %s", exc)


# ---------------------------------------------------------------------------
# Audio level measurement
# ---------------------------------------------------------------------------
class AmbientNoiseMonitor:
    """Samples microphone audio and computes RMS / dBFS levels."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._stream: Optional[sd.InputStream] = None
        self._smoothed_rms: Optional[float] = None

    # ------------------------------------------------------------------
    def start(self) -> None:
        """Open the microphone input stream."""
        try:
            self._stream = sd.InputStream(
                samplerate=self.config.sample_rate,
                channels=1,
                dtype="float32",
                device=self.config.device_index,
                blocksize=int(self.config.sample_rate * self.config.chunk_duration),
            )
            self._stream.start()
            logger.info("Microphone stream started (device: %s)", self.config.device_index or "default")
        except Exception as exc:
            logger.error("Failed to open microphone stream: %s", exc)
            raise

    # ------------------------------------------------------------------
    def stop(self) -> None:
        """Close the microphone input stream."""
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as exc:  # pragma: no cover - best effort
                logger.warning("Error stopping microphone stream: %s", exc)
            self._stream = None
            logger.info("Microphone stream stopped.")

    # ------------------------------------------------------------------
    def read_rms(self) -> Optional[float]:
        """
        Read one chunk of audio and return its RMS amplitude.

        Returns None if no data is available yet.
        """
        if self._stream is None:
            return None

        try:
            frames = int(self.config.sample_rate * self.config.chunk_duration)
            data, overflowed = self._stream.read(frames)
            if overflowed:
                logger.warning("Input buffer overflowed; some audio was dropped.")

            if len(data) == 0:
                return None

            # Flatten to 1-D and compute RMS
            samples = np.asarray(data, dtype=np.float64).ravel()
            rms = float(np.sqrt(np.mean(samples ** 2)))

            # Apply exponential moving average smoothing
            if self._smoothed_rms is None:
                self._smoothed_rms = rms
            else:
                alpha = self.config.smoothing_alpha
                self._smoothed_rms = alpha * rms + (1.0 - alpha) * self._smoothed_rms

            return self._smoothed_rms
        except Exception as exc:  # pragma: no cover - hardware dependent
            logger.error("Error reading audio data: %s", exc)
            return None

    # ------------------------------------------------------------------
    @staticmethod
    def rms_to_db(rms: float, reference: float = 1.0) -> float:
        """Convert RMS amplitude to dBFS (decibels relative to full scale)."""
        if rms <= 0.0:
            return -160.0  # treat as silence floor
        return 20.0 * np.log10(rms / reference)


# ---------------------------------------------------------------------------
# Volume mapping logic
# ---------------------------------------------------------------------------
class VolumeMapper:
    """Adjusts system volume relative to the current level based on ambient noise.

    The mapper does not set an absolute target. Instead, it looks at the
    *current* system volume and nudges it up or down depending on how the
    ambient noise compares to *neutral_db*.
    """

    def __init__(self, config: Config) -> None:
        self.config = config

    def adjust_volume(
        self, current_volume: float, ambient_db: float, neutral_db: Optional[float] = None
    ) -> float:
        """Return a new volume (clamped to [min_volume, max_volume]) starting
        from *current_volume* and moving up or down based on *ambient_db*.

        - ambient_db well above neutral_db  -> increase volume
        - ambient_db well below neutral_db  -> decrease volume
        - ambient_db inside the deadband    -> no change
        """
        effective_neutral = neutral_db if neutral_db is not None else self.config.neutral_db
        lo_db = effective_neutral - self.config.deadband_db
        hi_db = effective_neutral + self.config.deadband_db
        lo_vol = self.config.min_volume
        hi_vol = self.config.max_volume
        step = self.config.max_step

        # Inside the deadband: keep the current volume.
        if lo_db <= ambient_db <= hi_db:
            return current_volume

        # Determine direction and magnitude of the nudge.
        if ambient_db > hi_db:
            # Louder than neutral: turn volume up.
            # The louder it gets, the stronger the nudge (capped at max_step).
            strength = min((ambient_db - hi_db) / max(hi_db - lo_db, 1e-6), 1.0)
            delta = step * strength
            new_volume = current_volume + delta
        else:
            # Quieter than neutral: turn volume down.
            strength = min((lo_db - ambient_db) / max(hi_db - lo_db, 1e-6), 1.0)
            delta = step * strength
            new_volume = current_volume - delta

        # Clamp to safe bounds.
        return max(lo_vol, min(hi_vol, new_volume))


# ---------------------------------------------------------------------------
# Preference learning system
# ---------------------------------------------------------------------------
class PreferenceLearner:
    """Learns and remembers user volume preferences over time.

    Stores preferences in a JSON file so they persist between runs.
    Tracks preferred starting volume, max adjustment %, and learned
    neutral_db offset that adapts when the user manually changes volume.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self.preferences_path = Path(config.preferences_file)
        self.data = self._load()
        self._last_manual_volume: Optional[float] = None

    # ------------------------------------------------------------------
    def _load(self) -> dict:
        """Load preferences from disk, returning defaults if missing."""
        defaults = {
            "preferred_start_volume": None,
            "preferred_max_adjustment_percent": None,
            "learned_neutral_db_offset": 0.0,
            "manual_adjustment_count": 0,
            "last_updated": None,
        }
        if self.preferences_path.exists():
            try:
                with open(self.preferences_path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                defaults.update(saved)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not read preferences file: %s", exc)
        return defaults

    # ------------------------------------------------------------------
    def save(self) -> None:
        """Persist current preferences to disk."""
        self.data["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(self.preferences_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
        except OSError as exc:
            logger.warning("Could not save preferences file: %s", exc)

    # ------------------------------------------------------------------
    def get_effective_neutral_db(self) -> float:
        """Return the neutral dBFS adjusted by learned user offset."""
        return self.config.neutral_db + self.data.get("learned_neutral_db_offset", 0.0)

    # ------------------------------------------------------------------
    def record_manual_adjustment(self, old_volume: float, new_volume: float) -> None:
        """Called when the user manually changes system volume while running.

        Updates the learned neutral_db offset so future automatic
        adjustments drift toward the user's preferred level.
        """
        if not self.config.enable_learning:
            return

        delta = new_volume - old_volume
        if abs(delta) < 0.01:
            return

        offset = self.data.get("learned_neutral_db_offset", 0.0)
        offset += delta * self.config.learning_rate
        offset = max(-0.4, min(0.4, offset))
        self.data["learned_neutral_db_offset"] = offset
        self.data["manual_adjustment_count"] = (
            self.data.get("manual_adjustment_count", 0) + 1
        )
        self._last_manual_volume = new_volume
        self.save()
        logger.info(
            "Learned from manual adjustment: neutral_db offset -> %.3f",
            offset,
        )

    # ------------------------------------------------------------------
    def set_start_volume(self, volume: float) -> None:
        """Save the user's preferred starting volume."""
        self.data["preferred_start_volume"] = volume
        self.save()

    # ------------------------------------------------------------------
    def set_max_adjustment(self, percent: float) -> None:
        """Save the user's preferred maximum adjustment percentage."""
        self.data["preferred_max_adjustment_percent"] = percent
        self.save()

    # ------------------------------------------------------------------
    def get_start_volume(self) -> Optional[float]:
        return self.data.get("preferred_start_volume")

    # ------------------------------------------------------------------
    def get_max_adjustment_percent(self) -> Optional[float]:
        return self.data.get("preferred_max_adjustment_percent")


# ---------------------------------------------------------------------------
# Interactive first-time setup
# ---------------------------------------------------------------------------
def run_interactive_setup(config: Config, learner: PreferenceLearner) -> Config:
    """Walk the user through initial configuration.

    Asks for preferred starting volume, max adjustment %, and neutral dBFS.
    """
    print("\n" + "=" * 60)
    print("   Auto Volume Adjuster - First Time Setup")
    print("=" * 60)
    print()
    print("This script automatically adjusts system volume based on")
    print("ambient background noise.")
    print()

    controller = SystemVolumeController()
    current_vol = controller.get_volume()
    print(f"Your current system volume is: {current_vol * 100:.0f}%")
    print()

    # --- Preferred starting volume ---
    while True:
        try:
            answer = input(
                f"Set starting volume % (Enter to keep {current_vol * 100:.0f}%): "
            ).strip()
            if answer == "":
                start_vol = current_vol
                break
            start_vol = float(answer) / 100.0
            if 0.0 <= start_vol <= 1.0:
                break
            print("Please enter a value between 0 and 100.")
        except ValueError:
            print("Please enter a valid number.")
    config.min_volume = max(0.0, start_vol - 0.10)
    config.max_volume = min(1.0, start_vol + 0.10)
    learner.set_start_volume(start_vol)
    print(f"Starting volume: {start_vol * 100:.0f}%")
    print(f"Safe bounds: {config.min_volume * 100:.0f}% - {config.max_volume * 100:.0f}%")
    print()

    # --- Max adjustment percentage ---
    while True:
        try:
            answer = input("Max adjustment per cycle % (default 5, range 1-30): ").strip()
            if answer == "":
                max_step = 0.05
                break
            max_step = float(answer) / 100.0
            if 0.01 <= max_step <= 0.30:
                break
            print("Please enter a value between 1 and 30.")
        except ValueError:
            print("Please enter a valid number.")
    config.max_step = max_step
    learner.set_max_adjustment(max_step * 100)
    print(f"Max adjustment per cycle: {max_step * 100:.0f}%")
    print()

    # --- Neutral dB reference ---
    print("Ambient noise reference (neutral dBFS):")
    print("  Quiet room:   -50 to -60")
    print("  Normal office: -40 to -50")
    print("  Noisy room:   -30 to -40")
    while True:
        try:
            answer = input("Neutral dBFS (default -40): ").strip()
            if answer == "":
                config.neutral_db = -40.0
                break
            config.neutral_db = float(answer)
            break
        except ValueError:
            print("Please enter a valid number.")
    print(f"Neutral dBFS: {config.neutral_db}")
    print()

    # --- Measure baseline noise floor ----------------------------------
    print("Measuring baseline noise level (please stay quiet for 3 seconds)...")
    try:
        monitor = AmbientNoiseMonitor(config)
        monitor.start()
        time.sleep(3.0)
        readings = []
        for _ in range(5):
            rms = monitor.read_rms()
            if rms is not None:
                readings.append(AmbientNoiseMonitor.rms_to_db(rms))
            time.sleep(0.3)
        monitor.stop()
        if readings:
            config.noise_floor_db = sum(readings) / len(readings)
            logger.info(
                "Baseline noise floor measured: %.1f dBFS", config.noise_floor_db
            )
            print(f"Baseline noise floor: {config.noise_floor_db:.1f} dBFS")
        else:
            print("Could not measure baseline noise floor.")
    except Exception as exc:
        logger.warning("Baseline noise measurement failed: %s", exc)
        print("Could not measure baseline noise floor.")
    print()

    learner.save()
    print("Setup complete! Preferences saved to user_preferences.json")
    print("=" * 60)
    print()
    return config


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------
class AutoVolumeAdjuster:
    """Top-level controller tying monitoring, mapping, and volume control together."""

    def __init__(self, config: Optional[Config] = None) -> None:
        self.config = config or Config()
        self.controller = SystemVolumeController()
        self.monitor = AmbientNoiseMonitor(self.config)
        self.mapper = VolumeMapper(self.config)
        self.learner = PreferenceLearner(self.config)
        self._running = False
        self._consecutive_errors = 0
        self._last_system_volume: Optional[float] = None
        self._last_adjustment_delta: float = 0.0
        self._last_ambient_db: Optional[float] = None
        self._consecutive_auto_increases: int = 0

        # Apply learned preferences to config
        start_vol = self.learner.get_start_volume()
        if start_vol is not None:
            self.config.min_volume = max(0.0, start_vol - 0.10)
            self.config.max_volume = min(1.0, start_vol + 0.10)
            logger.info(
                "Loaded preferred start volume: %.0f%% (bounds: %.0f%%-%.0f%%)",
                start_vol * 100,
                self.config.min_volume * 100,
                self.config.max_volume * 100,
            )

    # ------------------------------------------------------------------
    def _check_keypress(self) -> bool:
        """Return True if the user pressed 's' (non-blocking).

        On Windows, uses msvcrt to check for a keypress without pausing.
        On other platforms, always returns False.
        """
        if not HAS_KEYPRESS:
            return False
        if msvcrt.kbhit():
            key = msvcrt.getch().decode("utf-8", errors="ignore").lower()
            return key == "s"
        return False

    # ------------------------------------------------------------------
    def _show_settings_menu(self) -> None:
        """Pause auto-adjustment and let the user change settings interactively."""
        print("\n" + "=" * 60)
        print("   Settings Menu (press Enter to keep current value)")
        print("=" * 60)
        print()

        # Current values
        print(f"1. Neutral dBFS:        {self.config.neutral_db}")
        print(f"2. Max step %:          {self.config.max_step * 100:.0f}%")
        print(f"3. Min volume %:        {self.config.min_volume * 100:.0f}%")
        print(f"4. Max volume %:        {self.config.max_volume * 100:.0f}%")
        print(f"5. Smoothing alpha:     {self.config.smoothing_alpha}")
        print(f"6. Update interval sec: {self.config.update_interval}")
        print(f"7. Enable learning:     {self.config.enable_learning}")
        print()

        # --- Neutral dBFS ---
        answer = input(f"Neutral dBFS [{self.config.neutral_db}]: ").strip()
        if answer:
            try:
                self.config.neutral_db = float(answer)
            except ValueError:
                print("  Invalid value, keeping current.")

        # --- Max step % ---
        answer = input(f"Max step % [{self.config.max_step * 100:.0f}]: ").strip()
        if answer:
            try:
                val = float(answer) / 100.0
                if 0.01 <= val <= 0.30:
                    self.config.max_step = val
                else:
                    print("  Value out of range (1-30), keeping current.")
            except ValueError:
                print("  Invalid value, keeping current.")

        # --- Min volume % ---
        answer = input(f"Min volume % [{self.config.min_volume * 100:.0f}]: ").strip()
        if answer:
            try:
                val = float(answer) / 100.0
                if 0.0 <= val <= 1.0:
                    self.config.min_volume = val
                else:
                    print("  Value out of range (0-100), keeping current.")
            except ValueError:
                print("  Invalid value, keeping current.")

        # --- Max volume % ---
        answer = input(f"Max volume % [{self.config.max_volume * 100:.0f}]: ").strip()
        if answer:
            try:
                val = float(answer) / 100.0
                if 0.0 <= val <= 1.0:
                    self.config.max_volume = val
                else:
                    print("  Value out of range (0-100), keeping current.")
            except ValueError:
                print("  Invalid value, keeping current.")

        # --- Smoothing alpha ---
        answer = input(f"Smoothing alpha [{self.config.smoothing_alpha}]: ").strip()
        if answer:
            try:
                val = float(answer)
                if 0.0 <= val <= 1.0:
                    self.config.smoothing_alpha = val
                else:
                    print("  Value out of range (0-1), keeping current.")
            except ValueError:
                print("  Invalid value, keeping current.")

        # --- Update interval ---
        answer = input(f"Update interval sec [{self.config.update_interval}]: ").strip()
        if answer:
            try:
                val = float(answer)
                if val > 0:
                    self.config.update_interval = val
                else:
                    print("  Must be positive, keeping current.")
            except ValueError:
                print("  Invalid value, keeping current.")

        # --- Enable learning ---
        answer = input(f"Enable learning (y/n) [{'y' if self.config.enable_learning else 'n'}]: ").strip().lower()
        if answer in ("y", "yes"):
            self.config.enable_learning = True
        elif answer in ("n", "no"):
            self.config.enable_learning = False

        # Save preferences
        self.learner.save()
        print()
        print("Settings saved to user_preferences.json")
        print("=" * 60)
        print("Resuming auto-adjustment...")
        print()

    # ------------------------------------------------------------------
    def _handle_signal(self, signum, frame) -> None:  # pragma: no cover - signal
        logger.info("Received signal %s. Shutting down gracefully...", signum)
        self._running = False

    # ------------------------------------------------------------------
    def run(self) -> None:
        """Main loop: sample audio, compute target volume, adjust system volume."""
        signal.signal(signal.SIGINT, self._handle_signal)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, self._handle_signal)

        self._running = True

        try:
            self.monitor.start()
        except Exception:
            logger.critical("Cannot start without a working microphone. Exiting.")
            return

        logger.info("Auto Volume Adjuster is running. Press 's' for settings, Ctrl+C to stop.")
        logger.info(
            "Config: sample_rate=%d Hz, chunk=%.2fs, smoothing=%.2f, "
            "volume_bounds=[%.0f%%, %.0f%%], update_interval=%.2fs",
            self.config.sample_rate,
            self.config.chunk_duration,
            self.config.smoothing_alpha,
            self.config.min_volume * 100,
            self.config.max_volume * 100,
            self.config.update_interval,
        )

        last_update = 0.0

        try:
            while self._running:
                # Check for 's' keypress to open settings menu (non-blocking)
                if self._check_keypress():
                    self._show_settings_menu()

                now = time.time()
                if now - last_update < self.config.update_interval:
                    time.sleep(0.05)
                    continue

                rms = self.monitor.read_rms()
                if rms is None:
                    self._consecutive_errors += 1
                    if self._consecutive_errors >= self.config.max_consecutive_errors:
                        logger.critical(
                            "Too many consecutive audio read errors (%d). Stopping.",
                            self._consecutive_errors,
                        )
                        break
                    time.sleep(self.config.update_interval)
                    continue

                self._consecutive_errors = 0
                ambient_db = AmbientNoiseMonitor.rms_to_db(rms)
                current_volume = self.controller.get_volume()
                effective_neutral = self.learner.get_effective_neutral_db()
                target_volume = self.mapper.adjust_volume(
                    current_volume, ambient_db, neutral_db=effective_neutral
                )

                # Detect manual volume changes by the user since last cycle.
                if (
                    self._last_system_volume is not None
                    and self.config.enable_learning
                    and abs(current_volume - self._last_system_volume) > 0.01
                    and abs(current_volume - target_volume) > 0.01
                ):
                    # The system volume differs from both what we had and
                    # what we wanted to set -> user manually adjusted it.
                    self.learner.record_manual_adjustment(
                        self._last_system_volume, current_volume
                    )

                # --- Feedback loop detection -------------------------------
                # If we just raised volume and ambient dB jumped right after,
                # it's likely the computer's own sound being picked up by the
                # microphone, not real background noise. Don't increase again.
                feedback_detected = False
                if (
                    self.config.feedback_detection_enabled
                    and self._last_adjustment_delta > 0
                    and self._last_ambient_db is not None
                    and ambient_db - self._last_ambient_db
                    >= self.config.feedback_db_threshold
                ):
                    feedback_detected = True
                    self._consecutive_auto_increases = 0
                    logger.warning(
                        "Feedback detected: volume up %.0f%% → ambient dB rose %.1f dB. "
                        "Skipping increase to avoid feedback loop.",
                        self._last_adjustment_delta * 100,
                        ambient_db - self._last_ambient_db,
                    )

                # Track consecutive automatic increases to prevent runaway.
                if not feedback_detected and target_volume > current_volume:
                    self._consecutive_auto_increases += 1
                elif target_volume < current_volume:
                    self._consecutive_auto_increases = 0

                # Only adjust if the difference is meaningful (>1%) to avoid
                # unnecessary OS calls and potential audio artifacts.
                if abs(target_volume - current_volume) > 0.01:
                    # Skip increases if feedback was detected or we've
                    # increased too many times in a row automatically.
                    skip_increase = False
                    if feedback_detected and target_volume > current_volume:
                        skip_increase = True
                    if (
                        self._consecutive_auto_increases
                        > self.config.max_consecutive_increases
                        and target_volume > current_volume
                    ):
                        skip_increase = True
                        logger.info(
                            "Max consecutive auto-increases reached. "
                            "Pausing volume increases."
                        )

                    if not skip_increase:
                        success = self.controller.set_volume(target_volume)
                        direction = "up" if target_volume > current_volume else "down"
                        if success:
                            logger.info(
                                "Ambient: %6.1f dBFS | Volume: %5.1f%% -> %5.1f%% (%s) [OK]",
                                ambient_db,
                                current_volume * 100,
                                target_volume * 100,
                                direction,
                            )
                        else:
                            logger.error(
                                "Ambient: %6.1f dBFS | Volume: %5.1f%% -> %5.1f%% (%s) [FAILED]",
                                ambient_db,
                                current_volume * 100,
                                target_volume * 100,
                                direction,
                            )
                    else:
                        logger.debug(
                            "Ambient: %6.1f dBFS | Increase skipped (feedback/limit)",
                            ambient_db,
                        )
                else:
                    logger.debug(
                        "Ambient: %6.1f dBFS | Volume unchanged (%.1f%%)",
                        ambient_db,
                        current_volume * 100,
                    )

                # Store state for next cycle's feedback detection.
                self._last_adjustment_delta = target_volume - current_volume
                self._last_ambient_db = ambient_db
                self._last_system_volume = self.controller.get_volume()
                last_update = now

        finally:
            self.monitor.stop()
            logger.info("Auto Volume Adjuster stopped.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> int:
    """Entry point for the application."""
    parser = argparse.ArgumentParser(
        description="Auto Volume Adjuster - dynamically adjusts system volume "
        "based on ambient background noise."
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Run interactive first-time setup to configure preferences.",
    )
    parser.add_argument(
        "--reset-prefs",
        action="store_true",
        help="Delete saved preferences and start fresh.",
    )
    parser.add_argument(
        "--test-volume",
        action="store_true",
        help="Test that reading and writing system volume works, then exit.",
    )
    args = parser.parse_args()

    # Handle test volume mode
    if args.test_volume:
        controller = SystemVolumeController()
        if not controller._initialized:
            print("ERROR: Volume controller could not be initialized.")
            print(f"OS: {controller.os_name}")
            return 1
        current = controller.get_volume()
        print(f"Current system volume: {current * 100:.0f}%")
        test_vol = max(0.1, min(0.9, current + 0.10))  # nudge by 10%
        print(f"Testing volume change: {current * 100:.0f}% -> {test_vol * 100:.0f}%")
        success = controller.set_volume(test_vol)
        time.sleep(0.5)
        after = controller.get_volume()
        print(f"Volume after set: {after * 100:.0f}%")
        if success and abs(after - test_vol) <= 0.05:
            print("SUCCESS: Volume control is working correctly.")
            # Restore original volume
            controller.set_volume(current)
            print(f"Restored original volume: {current * 100:.0f}%")
            return 0
        else:
            print("FAILED: Volume control is not working as expected.")
            print("Try running as Administrator / with elevated permissions.")
            return 1

    # Handle reset preferences
    if args.reset_prefs:
        prefs_path = Path("user_preferences.json")
        if prefs_path.exists():
            prefs_path.unlink()
            print("Preferences reset. Run without --reset-prefs to set up again.")
        return 0

    config = Config()

    # First-run detection: if no preferences file exists, run setup
    prefs_path = Path(config.preferences_file)
    if args.setup or not prefs_path.exists():
        learner = PreferenceLearner(config)
        config = run_interactive_setup(config, learner)

    try:
        app = AutoVolumeAdjuster(config)
        app.run()
        return 0
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
        return 0
    except Exception as exc:  # pragma: no cover - top-level safety net
        logger.exception("Unexpected error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
