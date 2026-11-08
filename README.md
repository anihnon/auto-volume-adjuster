# Auto Volume Adjuster

A Python-based utility that continuously monitors ambient background noise
through your computer's microphone and dynamically adjusts the system master
volume to keep playback at an optimal level — neither too loud nor too quiet
relative to the environment.

---

## Table of Contents

- [Features](#features)
- [Supported Platforms](#supported-platforms)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [How It Works](#how-it-works)
- [Troubleshooting](#troubleshooting)
- [License](#license)

## Features

| Feature | Description |
|---------|-------------|
| **Real-time ambient monitoring** | Samples the default microphone and computes RMS / dBFS levels. |
| **Smoothing algorithm** | Exponential moving average prevents erratic, sudden volume jumps. |
| **Safe volume bounds** | Configurable min/max limits avoid mute or deafening output. |
| **Cross-platform volume control** | Windows (pycaw/Core Audio), macOS (osascript), Linux (amixer/pactl). |
| **Graceful shutdown** | Clean exit on `Ctrl+C` (SIGINT) and `SIGTERM`. |
| **Error handling** | Handles missing microphones, permission errors, and device failures. |
| **Configurable** | Sensitivity, update interval, and volume bounds are all user-configurable. |
| **One-click launcher** | `run.bat` (Windows) / `run.sh` (Mac/Linux) opens a terminal and starts the script. |
| **Interactive setup** | First-run wizard asks for your preferred starting volume, max adjustment %, and noise reference. |
| **Preference learning** | Remembers your choices in `user_preferences.json` and adapts when you manually change volume. |

---

## Supported Platforms

| OS | Volume Backend | Notes |
|------|---------------|-------|
| **Windows** | `pycaw` (Core Audio API) | Requires `pycaw` and `comtypes`. |
| **macOS** | `osascript` | Built-in; no extra packages. |
| **Linux** | `amixer` (ALSA) or `pactl` (PulseAudio) | Whichever is available on the system. |

---

## Prerequisites

- **Python 3.8 or higher**
- A working microphone (built-in or external)
- `pip` (Python package installer)

### Platform-specific prerequisites

**Windows**
- No additional system tools required. `pycaw` talks to the Windows Core Audio API directly.

**macOS**
- No additional system tools required. `osascript` is part of macOS.
- You may need to grant Terminal / your IDE permission to access the microphone
  in *System Settings → Privacy & Security → Microphone*.

**Linux**
- Either ALSA (`amixer`) or PulseAudio (`pactl`) must be installed. Most desktop
  distributions include one of these by default.
- You may need to add your user to the `audio` group.

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/<your-username>/auto-volume-adjuster.git
cd auto-volume-adjuster

### 2. Create and activate a virtual environment (recommended)

```bash
# Create the environment
python -m venv .venv

# Activate it
#   Windows (PowerShell):
.venv\Scripts\Activate.ps1
#   Windows (cmd):
.venv\Scripts\activate.bat
#   macOS / Linux:
source .venv/bin/activate

### 3. Install dependencies

```bash
pip install -r requirements.txt

---

## Configuration

All settings are centralised in `main.py` inside the `Config` dataclass. Open
`main.py` and adjust the values near the top of the file:

```python
@dataclass
class Config:
    # --- Audio / microphone ------------------------------------------------
    sample_rate: int = 44100          # Microphone sample rate (Hz)
    chunk_duration: float = 0.5       # Length of each audio snapshot (seconds)
    device_index: Optional[int] = None  # None = use the system default mic

    # --- Smoothing ---------------------------------------------------------
    # Higher = smoother / slower reaction. 0.0 = no smoothing, 1.0 = frozen.
    smoothing_alpha: float = 0.3

    # --- Relative volume adjustment ----------------------------------------
    # Ambient dB at which no change happens. Louder -> volume up, quieter -> down.
    neutral_db: float = -40.0
    # Maximum volume change per cycle (0.05 = 5% max per update).
    max_step: float = 0.05
    # Deadband (dB) around neutral_db to ignore tiny fluctuations.
    deadband_db: float = 2.0

    # --- Volume bounds (0.0 to 1.0) ----------------------------------------
    min_volume: float = 0.15          # Never go quieter than 15%
    max_volume: float = 0.85          # Never go louder than 85%

    # --- Timing ------------------------------------------------------------
    update_interval: float = 1.0      # Seconds between volume adjustments
    max_consecutive_errors: int = 5   # Give up after N mic read failures

### Tuning guide

| Goal | What to change |
|------|---------------|
| **React faster to noise changes** | Decrease `smoothing_alpha` (e.g., `0.1`) and/or `update_interval` (e.g., `0.5`). |
| **React more slowly / avoid jitter** | Increase `smoothing_alpha` (e.g., `0.6`) and/or `update_interval` (e.g., `2.0`). |
| **Make the script react more strongly** | Increase `max_step` (e.g., `0.1`). |
| **Make the script less aggressive** | Decrease `max_step` (e.g., `0.02`). |
| **Wider volume range** | Lower `min_volume` and/or raise `max_volume`. |
| **Narrower / safer range** | Raise `min_volume` and/or lower `max_volume`. |

> **Tip:** Run the script once and watch the log output (`Ambient: XX.X dBFS | Volume: YY.Y% -> ZZ.Z% (up/down)`) to calibrate `neutral_db` for your specific environment and microphone.

---

## Usage

Run the script from the repository root:

```bash
python main.py

Example output:

2026-08-11 14:32:01 [INFO] Microphone stream started (device: default)
2026-08-11 14:32:01 [INFO] Auto Volume Adjuster is running. Press Ctrl+C to stop.
2026-08-11 14:32:01 [INFO] Config: sample_rate=44100 Hz, chunk=0.50s, smoothing=0.30, volume_bounds=[15%, 85%], update_interval=1.00s
2026-08-11 14:32:02 [INFO] Ambient: -45.2 dBFS | Volume: 30.0% -> 42.5%
2026-08-11 14:32:03 [INFO] Ambient: -44.8 dBFS | Volume: 42.5% -> 43.1%
2026-08-11 14:32:04 [DEBUG] Ambient: -45.0 dBFS | Volume unchanged (43.1%)
...
^C2026-08-11 14:32:10 [INFO] Received signal 2. Shutting down gracefully...
2026-08-11 14:32:10 [INFO] Microphone stream stopped.
2026-08-11 14:32:10 [INFO] Auto Volume Adjuster stopped.

Press **Ctrl+C** at any time to stop the program cleanly.

---

## One-Click Launcher

You can start the script by double-clicking a launcher file — no terminal commands needed:

| File | Platform | How to use |
|------|----------|------------|
| `run.bat` | Windows | Double-click, or run from Command Prompt / PowerShell |
| `run.sh` | macOS / Linux | Double-click, or run `./run.sh` from Terminal |

The launcher opens a terminal window and runs `main.py`. On the very first run, it launches the **interactive setup wizard** where you choose:

1. **Starting volume %** — The volume level you want to begin from (defaults to your current system volume).
2. **Max adjustment %** — How aggressively the script may raise or lower volume each cycle (default: 5%).
3. **Neutral dBFS** — The ambient noise level at which no adjustment is needed.

Your choices are saved to `user_preferences.json` and loaded automatically on future runs.

---

## Preference Learning

The script learns from you over time:

- **Remembers your starting volume** — On subsequent launches, it restores your previously chosen starting volume and safe bounds.
- **Adapts to manual changes** — If you manually adjust the system volume while the script is running (e.g., using your keyboard media keys or system tray), the script detects the change and updates its internal "neutral" reference. Over time, it drifts toward the volume levels you personally prefer.
- **Persistent storage** — All learned preferences are stored in `user_preferences.json` (ignored by Git, so your data stays private).

To reset learned preferences and start fresh:

```bash
python main.py --reset-prefs
```

Or run the setup wizard again:

```bash
python main.py --setup
```

### Test volume control

If the script doesn't seem to change your volume, run the built-in test:

```bash
python main.py --test-volume
```

This will:
1. Read your current system volume
2. Try to change it by 10%
3. Read it back to verify the change took effect
4. Restore your original volume

If the test fails, try running the script as **Administrator** (Windows) or with `sudo` (Linux/macOS), as volume control may require elevated permissions.

---

## How It Works

1. **Audio capture** – `sounddevice` opens the default microphone input stream and reads short chunks of PCM audio.
2. **Level measurement** – The Root Mean Square (RMS) of each chunk is computed and converted to dBFS (decibels relative to full scale).
3. **Smoothing** – An exponential moving average filters out short-lived spikes so volume changes are gradual.
4. **Relative adjustment** – The script looks at the *current* system volume and nudges it up or down based on how ambient noise compares to your neutral dBFS reference. Louder environment → volume goes up; quieter → volume goes down.
5. **Learning** – If you manually change the volume while the script runs, it detects the difference and adjusts its internal reference so future automatic adjustments better match your personal preference.
6. **OS adjustment** – The platform-specific backend sets the system master volume only when the change exceeds 1%, avoiding unnecessary audio interruptions.
7. **Loop** – Steps 1–6 repeat every `update_interval` seconds until interrupted.

---

## Troubleshooting

| Problem | Likely cause | Solution |
|---------|--------------|----------|
| `No default input device found` | No microphone connected or not set as default | Plug in a microphone and set it as the default recording device in OS sound settings. |
| `Permission denied` (macOS) | Microphone access not granted | Grant microphone permission to Terminal / your IDE in *System Settings → Privacy & Security → Microphone*. |
| Volume never changes | `neutral_db` / `deadband_db` thresholds don't match your room, or volume control lacks permissions | Run `python main.py --test-volume` to verify. If it fails, run as Administrator (Windows) or with sudo (Linux/macOS). Also check the log for `[FAILED]` messages. |
| Volume shows wrong value (e.g., 50% instead of 43%) | Volume read failed and fell back to 50% | Check the log for read errors. Run `python main.py --test-volume`. May need elevated permissions. |
| Volume jumps erratically | `smoothing_alpha` too low | Increase `smoothing_alpha` (e.g., from `0.3` to `0.6`). |
| `ModuleNotFoundError: No module named 'pycaw'` | Dependencies not installed | Run `pip install -r requirements.txt` inside the activated virtual environment. |
| Linux: volume control silent | Neither `amixer` nor `pactl` found | Install ALSA or PulseAudio utilities, or run as a user with audio permissions. |

---

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details (add a `LICENSE` file if you wish to formalise it).
