#!/usr/bin/env python3
#
# Stage Audio Works Raspberry Pi Video Player
# Copyright (C) 2026 Stage Audio Works
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""  
Stage Audio Works Raspberry Pi Video Player  (v11)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Headless video player for museum / exhibition installations on a Raspberry Pi 5.
An attractor video loops continuously; a visitor presses one of up to three
physical buttons and the matching clip plays once, then the loop resumes.

Features:
  • Loop attractor video
  • GPIO triggers (up to 3) — ignored while a clip is already playing
  • Override mode — replaces the attractor video and ignores GPIO
  • Audio output device selection, persisted across reboots. Enumerates ALSA
    PCMs via 'aplay -L'; changing the device hot-restarts mpv so audio
    switches without a page reload. Built for USB headphone adapters:
    plug in, refresh the page, pick the device, done.
  • Forced output resolution and refresh rate, persisted across reboots.
    Resolutions come from what the attached display reports; refresh rate is a
    fixed list of candidates, since the display reports rates for no mode.
    Falls back to the display's preferred mode automatically if mpv cannot
    start on the chosen combination, so a wrong pick cannot cost the picture.
  • Web UI — video upload/download, volume, override toggle, log viewer,
    system info, reboot, player name, and in-place script update
  • UDP remote commands
  • Volume persistence across reboots
  • mpv watchdog, log rotation + compression

Note on Bluetooth audio:
  Deliberately NOT in this version. It was added in v10 and then parked as
  non-essential before it was ever tested on hardware, so v11 returns to the
  hardware-proven v9 codebase. The Bluetooth implementation is preserved in
  archive/RPi_Video_Player_v10.py — see docs/PROJECT_HANDOFF.md §8.1 before
  reviving it.

Why mpv instead of VLC?
  The Raspberry Pi OS Bookworm package of VLC 3.0.23 was compiled
  WITHOUT the DRM/KMS video output module, making it impossible to
  display video on a headless RPi 5 regardless of flags used.
  mpv v0.40.0 (Bookworm) has full DRM/KMS support via libplacebo
  and works correctly on RPi 5 out of the box.

Control mechanism:
  VLC used an HTTP/XML interface (polling-based, slow).
  mpv uses a Unix domain socket with JSON IPC — lower latency,
  more reliable, and supports near-instant video switching via
  the 'loadfile' command (append/replace modes).

Video switching:
  'loadfile <path> replace' swaps the playing file in ~50-150 ms
  with no process restart, giving minimal black-screen transitions.

Audio device endpoints:
  /audio_devices GET  — list available ALSA devices
  /audio_device  GET  — currently active device
  /audio_device  POST — set new device, persist, hot-restart mpv

Display endpoints:
  /display_modes GET  — modes the attached display advertises
  /resolution    GET  — current output mode + refresh rate mpv reports
  /resolution    POST — force a mode, persist, hot-restart mpv; reverts to
                        "preferred" automatically if mpv will not start on it
"""
from __future__ import annotations

import os
import re
import sys
import time
import json
import socket
import signal
import shutil
import gzip
import threading
import subprocess
import tempfile
import logging
import requests
from logging.handlers import RotatingFileHandler
from dataclasses import dataclass, field
from typing import Optional, Tuple
from flask import (Flask, render_template_string, jsonify, request,
                   send_from_directory, send_file, redirect, url_for)

# ── GPIO via lgpio directly (RPi 5 / Bookworm) ───────────────────────────────
# Both RPi.GPIO and gpiozero fail when run as a systemd service on RPi 5 —
# RPi.GPIO uses the broken legacy /dev/mem backend, and gpiozero's lgpio pin
# factory crashes during module-level __init__ in a non-interactive session.
# Using lgpio directly (the underlying C library) works reliably in all
# contexts including systemd services.
#
# IMPORTANT: lgpio creates its notification pipe (.lgd-nfy*) in the current
# working directory. When run as a systemd service the CWD is '/' which the
# pi user cannot write to, causing a FileNotFoundError at import time.
# os.chdir('/home/pi') here ensures the pipe can always be created.
# The service file also sets WorkingDirectory=/home/pi as a second layer.
#
# lgpio API used here:
#   h = lgpio.gpiochip_open(0)  — open /dev/gpiochip0
#   lgpio.gpio_claim_input(h, pin, lgpio.SET_PULL_UP)
#   lgpio.gpio_read(h, pin)     — returns 0 (LOW) or 1 (HIGH)
os.chdir('/home/pi')   # must happen before lgpio import — see note above

try:
    import lgpio as _lgpio
    _LGPIO_AVAILABLE = True
except ImportError:
    _lgpio = None
    _LGPIO_AVAILABLE = False

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION  — edit this section for your installation
# ══════════════════════════════════════════════════════════════════════════════

VIDEO_DIR = "/home/pi/videos"
os.makedirs(VIDEO_DIR, exist_ok=True)

GPIO_PINS      = [17, 27, 22]
TRIGGER_VIDEOS = {
    17: os.path.join(VIDEO_DIR, "trigger1.mp4"),
    27: os.path.join(VIDEO_DIR, "trigger2.mp4"),
    22: os.path.join(VIDEO_DIR, "trigger3.mp4"),
}
LOOP_VIDEO             = os.path.join(VIDEO_DIR, "loop.mp4")
OVERRIDE_VIDEO_DEFAULT = os.path.join(VIDEO_DIR, "override.mp4")

# ── Display ───────────────────────────────────────────────────────────────────
# RPi 5: card1-HDMI-A-1 is HDMI port 0 (closest to USB-C power).
#        card1-HDMI-A-2 is HDMI port 1.
# Confirmed by:  cat /sys/class/drm/card1-HDMI-A-1/status
DRM_DEVICE    = "card1"
DRM_CONNECTOR = "HDMI-A-1"

# ── Audio ─────────────────────────────────────────────────────────────────────
# Default device used on first boot (before user selects one via the Web UI).
# Use plughw so ALSA handles sample-rate and channel conversion automatically.
# vc4hdmi0 = HDMI port 0.  Change to vc4hdmi1 for HDMI port 1.
# USB headphone adapters will appear as e.g. "alsa/plughw:CARD=Device,DEV=0"
# after plugging in — use the Web UI dropdown to discover and select them.
ALSA_DEVICE_DEFAULT = "alsa/plughw:CARD=vc4hdmi0,DEV=0"
AUDIO_DEVICE_FILE   = "/home/pi/.mpv_audio_device"   # persists selected device

# -- Output resolution / display mode ----------------------------------------
# mpv performs the KMS modeset when it takes over the display, so --drm-mode is
# what actually forces the Pi's HDMI output resolution while the player runs.
# "preferred" means "whatever the display advertises as its preferred mode",
# which is the safe default and the behaviour every version before this one had.
#
# Accepted values: "preferred", "highest", a mode index, or WxH[@R]
# e.g. 1920x1080 or 1920x1080@50.  Content here is PAL 25 fps, so a 50 Hz mode
# divides evenly into the frame rate and a 60 Hz one does not - if you see
# judder on an otherwise fine file, try forcing @50.
RESOLUTION_FILE    = "/home/pi/.mpv_resolution"   # persists selected output mode
RESOLUTION_DEFAULT = "preferred"

# Modes the attached display advertises, straight from the kernel.
# NOTE: sysfs uses the LONG connector name (card1-HDMI-A-1) while mpv wants the
# SHORT one (HDMI-A-1).  Mixing these up is a documented trap on this project.
DRM_MODES_SYSFS = f"/sys/class/drm/{DRM_DEVICE}-{DRM_CONNECTOR}/modes"

# Validates anything we are about to hand to --drm-mode.  Also rejects
# interlaced entries such as "1920x1080i" that appear in sysfs but are not a
# valid --drm-mode value.
RESOLUTION_RE = re.compile(
    r"^(?:preferred|highest|[0-9]{1,2}|[0-9]{3,5}x[0-9]{3,5}(?:@[0-9]{1,3}(?:\.[0-9]{1,3})?)?)$"
)

# ── mpv IPC socket ────────────────────────────────────────────────────────────
MPV_SOCKET = "/tmp/saw_mpv.sock"

# ── Volume ────────────────────────────────────────────────────────────────────
VOLUME_FILE  = "/home/pi/.mpv_volume"   # 0–100 (mpv scale)
DEFAULT_VOLUME = 80

# ── Other paths ───────────────────────────────────────────────────────────────
CONFIG_FILE         = "/home/pi/config.json"
BRANDING_IMAGE_PATH = "/home/pi/saw.jpg"
ALLOWED_EXTENSIONS  = {"mp4"}

# ── GPIO debounce ─────────────────────────────────────────────────────────────
# Passed to lgpio poll worker for manual debounce — minimum quiet time between triggers.
GPIO_DEBOUNCE_S = 0.5

# ── UDP ───────────────────────────────────────────────────────────────────────
UDP_PORT     = 5505
UDP_COMMANDS = {
    "PLAY":         "play",
    "PAUSE":        "pause",
    "RESTART_LOOP": "restart",
    "OVERRIDE_ON":  "override_on",
    "OVERRIDE_OFF": "override_off",
    "TRIGGER_1":    "trigger_1",
    "TRIGGER_2":    "trigger_2",
    "TRIGGER_3":    "trigger_3",
}

# ── Script identity — used to validate script uploads via the web UI ──────────
# Any .py file uploaded as a script update MUST contain this exact string.
# This prevents accidental upload of unrelated Python files.
SAW_PLAYER_SIGNATURE = "SAW-PLAYER-AUTHENTIC"

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_FILE                 = "/home/pi/saw_player.log"
LOG_BACKUP_COUNT         = 10
MAX_LOG_MB_BEFORE_ROTATE = 200
LOG_CHECK_INTERVAL       = 30

# ── Script management ─────────────────────────────────────────────────────────
SCRIPT_PATH   = "/home/pi/saw_player.py"
SCRIPT_BACKUP = "/home/pi/saw_player.py.bak"
# Functions that MUST be present in any uploaded script for it to be accepted
REQUIRED_FUNCTIONS = [
    "play_trigger",
    "mpv_watchdog",
    "udp_listener",
    "switch_video",
    "SAW_PLAYER_SIGNATURE",
]

# ══════════════════════════════════════════════════════════════════════════════
# APP + STATE
# ══════════════════════════════════════════════════════════════════════════════

app = Flask(__name__)
branding_image_exists = os.path.isfile(BRANDING_IMAGE_PATH)

@app.before_request
def _update_max_upload():
    """Recalculate upload limit before every request based on current free space."""
    app.config['MAX_CONTENT_LENGTH'] = _get_max_upload_bytes()

@dataclass
class PlayerState:
    override_active:       bool          = False
    override_video:        str           = OVERRIDE_VIDEO_DEFAULT
    current_video:         str           = field(default_factory=lambda: os.path.basename(LOOP_VIDEO))
    current_trigger_video: Optional[str] = None
    trigger_running:       bool          = False
    restart_in_progress:   bool          = False
    paused:                bool          = False
    volume:                int           = DEFAULT_VOLUME
    audio_device:          str           = ALSA_DEVICE_DEFAULT
    resolution:            str           = RESOLUTION_DEFAULT

state      = PlayerState()
state_lock = threading.Lock()

# ══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════════════

logger = logging.getLogger("saw_player")
logger.setLevel(logging.INFO)

def _create_file_handler() -> RotatingFileHandler:
    fh = RotatingFileHandler(LOG_FILE, maxBytes=10_000_000, backupCount=1)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(threadName)s: %(message)s"))
    return fh

file_handler = _create_file_handler()
logger.addHandler(file_handler)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(threadName)s: %(message)s"))
logger.addHandler(console_handler)

LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

def _close_file_handler():
    global file_handler
    try:
        logger.removeHandler(file_handler)
        file_handler.flush()
        file_handler.close()
    except Exception:
        print("Error closing log handler", file=sys.stderr)

def _reopen_file_handler():
    global file_handler
    try:
        file_handler = _create_file_handler()
        logger.addHandler(file_handler)
    except Exception:
        print("Error reopening log handler", file=sys.stderr)

def rotate_and_compress(log_path: str):
    try:
        if not os.path.exists(log_path):
            return
        logger.info("Rotating log: %s", log_path)
        _close_file_handler()
        ts      = time.strftime("%Y%m%d_%H%M%S")
        rotated = f"{log_path}.{ts}"
        os.rename(log_path, rotated)
        gz_path = rotated + ".gz"
        with open(rotated, "rb") as fin, gzip.open(gz_path, "wb") as fout:
            shutil.copyfileobj(fin, fout)
        try:
            os.remove(rotated)
        except Exception:
            logger.exception("Failed to remove rotated log")
    except Exception:
        print("rotate_and_compress failed", file=sys.stderr)
    finally:
        _reopen_file_handler()
        try:
            d        = os.path.dirname(log_path) or "."
            base     = os.path.basename(log_path)
            gz_files = sorted(
                f for f in os.listdir(d)
                if f.startswith(base + ".") and f.endswith(".gz")
            )
            while len(gz_files) > LOG_BACKUP_COUNT:
                old = gz_files.pop(0)
                try:
                    os.remove(os.path.join(d, old))
                except Exception:
                    logger.exception("Failed to remove old gz: %s", old)
        except Exception:
            logger.exception("Error pruning old gz files")

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG PERSISTENCE
# ══════════════════════════════════════════════════════════════════════════════

def load_config() -> dict:
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
                logger.debug("Config loaded: %s", cfg)
                return cfg
    except Exception:
        logger.exception("Error reading config")
    return {"override_active": False, "override_video": OVERRIDE_VIDEO_DEFAULT, "player_name": ""}

def save_config(cfg: dict):
    """Write config to disk.  Always call OUTSIDE state_lock."""
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f)
    except Exception:
        logger.exception("Error saving config")

config = load_config()
state.override_active = bool(config.get("override_active", False))
state.override_video  = config.get("override_video", OVERRIDE_VIDEO_DEFAULT)
if state.override_active:
    logger.info("Override mode ACTIVE on startup (persisted from previous session) — playing: %s",
                os.path.basename(state.override_video))
else:
    logger.info("Override mode inactive on startup")
# Player name — stored in config.json, shown in UI title and header
_player_name: str = config.get("player_name", "")

# ══════════════════════════════════════════════════════════════════════════════
# VOLUME PERSISTENCE
# ══════════════════════════════════════════════════════════════════════════════

def save_volume(vol: int):
    try:
        with open(VOLUME_FILE, "w") as f:
            f.write(str(int(vol)))
    except Exception:
        logger.exception("Error saving volume")

def load_saved_volume() -> int:
    try:
        if os.path.exists(VOLUME_FILE):
            with open(VOLUME_FILE) as f:
                return max(0, min(100, int(f.read().strip())))
    except Exception:
        logger.exception("Error loading saved volume")
    return DEFAULT_VOLUME

# ══════════════════════════════════════════════════════════════════════════════
# AUDIO DEVICE PERSISTENCE + ENUMERATION
# ══════════════════════════════════════════════════════════════════════════════

def save_audio_device(device: str):
    """Persist the selected ALSA device string to disk."""
    try:
        with open(AUDIO_DEVICE_FILE, "w") as f:
            f.write(device.strip())
    except Exception:
        logger.exception("Error saving audio device")

def load_saved_audio_device() -> str:
    """Load the persisted ALSA device, falling back to the compiled-in default."""
    try:
        if os.path.exists(AUDIO_DEVICE_FILE):
            raw = open(AUDIO_DEVICE_FILE).read().strip()
            if raw:
                return raw
    except Exception:
        logger.exception("Error loading saved audio device")
    return ALSA_DEVICE_DEFAULT

def list_audio_devices() -> list:
    """
    Return a list of dicts describing available ALSA PCM playback devices.

    Each dict has:
      device  — the mpv --audio-device string, e.g. "alsa/plughw:CARD=Device,DEV=0"
      label   — human-readable name shown in the UI dropdown

    Strategy:
      1. Run 'aplay -L' which lists all PCM device strings one-per-line,
         with an optional description on the indented line(s) that follow.
      2. Collect every line that starts a new entry (not indented) as a
         candidate device name.
      3. Convert each name to an mpv audio-device string:
           plughw:CARD=Foo,DEV=0  →  alsa/plughw:CARD=Foo,DEV=0
           hw:CARD=Foo,DEV=0      →  alsa/hw:CARD=Foo,DEV=0
      4. Filter out obviously useless entries (null, default, surround*, etc.)
         unless they are the only option (safety fallback).
    """
    devices = []
    try:
        result = subprocess.run(
            ["aplay", "-L"],
            capture_output=True, text=True, timeout=5
        )
        lines = result.stdout.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            # New device entries start at column 0 (not indented)
            if line and not line[0].isspace():
                name = line.strip()
                # Grab description from the next indented line if present
                desc = ""
                if i + 1 < len(lines) and lines[i + 1] and lines[i + 1][0].isspace():
                    desc = lines[i + 1].strip()

                # Build the mpv-compatible device string
                if name.startswith(("plughw:", "hw:")):
                    mpv_dev = f"alsa/{name}"
                    label   = f"{mpv_dev}" + (f"  —  {desc}" if desc else "")
                    devices.append({"device": mpv_dev, "label": label})
            i += 1
    except FileNotFoundError:
        logger.warning("aplay not found — cannot enumerate audio devices")
    except Exception:
        logger.exception("Error enumerating audio devices")

    # Always include the built-in default so the list is never empty
    default_entry = {
        "device": ALSA_DEVICE_DEFAULT,
        "label":  f"{ALSA_DEVICE_DEFAULT}  —  HDMI audio (default)",
    }
    if not any(d["device"] == ALSA_DEVICE_DEFAULT for d in devices):
        devices.insert(0, default_entry)

    return devices

# ══════════════════════════════════════════════════════════════════════════════
# OUTPUT RESOLUTION PERSISTENCE + ENUMERATION
# ══════════════════════════════════════════════════════════════════════════════

def save_resolution(mode: str):
    """
    Persist the selected output mode.

    Written via a temp file + os.replace rather than open(path, "w"): opening
    the real file truncates it immediately, so a failure mid-write would leave
    an empty file behind.  An empty or garbled mode is the one setting here
    that can cost a headless unit its picture on the next boot, so it is worth
    the few extra lines to make the write atomic.
    """
    try:
        directory = os.path.dirname(RESOLUTION_FILE) or "."
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".mpv_resolution.")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(mode.strip())
            os.replace(tmp, RESOLUTION_FILE)
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise
    except Exception:
        logger.exception("Error saving resolution")

def load_saved_resolution() -> str:
    """
    Load the persisted output mode, falling back to the safe default.

    A saved value that no longer passes validation is ignored rather than
    handed to mpv - better to come up at the display's preferred mode than not
    to come up at all.
    """
    try:
        if os.path.exists(RESOLUTION_FILE):
            raw = open(RESOLUTION_FILE).read().strip()
            if raw and RESOLUTION_RE.match(raw):
                return raw
            if raw:
                logger.warning("Ignoring invalid saved resolution %r - using %s",
                               raw, RESOLUTION_DEFAULT)
    except Exception:
        logger.exception("Error loading saved resolution")
    return RESOLUTION_DEFAULT

def list_drm_modes() -> list:
    """
    Return the modes the attached display advertises, in the kernel's order.

    Read from sysfs, so there is no extra package to install and no parsing of
    modetest output.  The list is exactly what the screen reported over EDID,
    which is why the UI only ever offers modes that stand a chance of working.

    An empty list means the file could not be read - usually nothing plugged
    into the HDMI port.  Treat that as "unknown", not "no modes available".
    """
    modes = []
    try:
        with open(DRM_MODES_SYSFS) as f:
            for line in f:
                mode = line.strip()
                if not mode or mode in modes:
                    continue
                if RESOLUTION_RE.match(mode):
                    modes.append(mode)
                else:
                    logger.debug("Skipping unusable display mode: %s", mode)
    except FileNotFoundError:
        logger.warning("Could not read %s - is anything connected to HDMI?",
                       DRM_MODES_SYSFS)
    except Exception:
        logger.exception("Error reading DRM modes")
    return modes

# ══════════════════════════════════════════════════════════════════════════════
# MPV PROCESS MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

mpv_process: Optional[subprocess.Popen] = None
mpv_lock = threading.Lock()

def _build_mpv_cmd(path: str, loop: bool = False) -> list:
    """
    Build the mpv command for headless RPi 5.

    Key flags:
      --vo=drm                  DRM/KMS video output — works headless on RPi 5.
      --drm-device              Which DRM card to use.
      --drm-connector           Which connector (HDMI port) to output to.
      --drm-mode                Output resolution.  Read from state so Web UI
                                changes apply on the next mpv restart.
      --audio-device            ALSA device — read from state so Web UI changes
                                take effect immediately after mpv restart.
      --audio-channels=stereo   Downmix any surround audio to stereo.
      --input-ipc-server        Unix socket for JSON IPC control.
      --really-quiet            Suppress verbose stdout; errors still logged.
    """
    with state_lock:
        audio_dev  = state.audio_device
        resolution = state.resolution
    cmd = [
        "mpv",
        f"--vo=drm",
        f"--drm-device=/dev/dri/{DRM_DEVICE}",
        f"--drm-connector={DRM_CONNECTOR}",
        f"--drm-mode={resolution}",   # "preferred", or a forced mode e.g. 1920x1080@50
        "--hwdec=auto-safe",          # use hardware decode where available
        f"--audio-device={audio_dev}",
        "--audio-channels=stereo",    # downmix 5.1 etc to stereo automatically
        f"--volume={state.volume}",
        f"--input-ipc-server={MPV_SOCKET}",
        "--no-terminal",
        "--really-quiet",
        "--no-input-default-bindings",
        "--no-osc",
        "--no-osd-bar",
    ]
    if loop:
        cmd.append("--loop-file=inf")
    cmd.append(path)
    return cmd

def _terminate_mpv():
    global mpv_process
    with mpv_lock:
        proc = mpv_process
        if proc is None:
            return
        try:
            logger.info("Terminating mpv pid=%s", proc.pid)
            proc.terminate()
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                logger.warning("mpv did not terminate — killing")
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    proc.kill()
        except Exception:
            logger.exception("Error terminating mpv")
        finally:
            mpv_process = None
    # Remove stale socket
    try:
        if os.path.exists(MPV_SOCKET):
            os.remove(MPV_SOCKET)
    except Exception:
        pass

def _mpv_is_running() -> bool:
    with mpv_lock:
        return mpv_process is not None and mpv_process.poll() is None

def _start_mpv(path: str, loop: bool = False):
    """
    Start a fresh mpv process.  Used at startup and crash recovery only.

    Returns True if mpv came up and its IPC socket is ready.  Callers rely on
    this to detect a forced display mode the screen will not accept, and to
    fall back before the unit is left with no picture.
    """
    global mpv_process
    logger.info("Starting mpv: %s (loop=%s)", path, loop)
    _terminate_mpv()
    time.sleep(0.1)
    cmd = _build_mpv_cmd(path, loop=loop)
    try:
        with mpv_lock:
            mpv_process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,      # capture stderr for watchdog logging
                preexec_fn=os.setsid,
            )
        # Wait for IPC socket to appear (up to 8 s)
        deadline = time.time() + 8
        while time.time() < deadline:
            if os.path.exists(MPV_SOCKET):
                break
            time.sleep(0.1)
        else:
            logger.warning("mpv IPC socket did not appear within 8 s")
            return False

        with state_lock:
            state.current_video = os.path.basename(path)
        logger.info("mpv started and IPC socket ready")

        # Start a thread to drain stderr so the pipe buffer never fills
        threading.Thread(
            target=_drain_mpv_stderr,
            args=(mpv_process,),
            daemon=True,
            name="mpv-stderr",
        ).start()
        return True

    except FileNotFoundError:
        logger.exception("'mpv' not found — run: sudo apt install mpv")
        mpv_process = None
    except Exception:
        logger.exception("Failed to start mpv")
        mpv_process = None
    return False

def _drain_mpv_stderr(proc: subprocess.Popen):
    """Log any mpv stderr output and discard the rest."""
    try:
        for line in proc.stderr:
            txt = line.decode("utf-8", errors="replace").rstrip()
            if txt:
                logger.debug("mpv: %s", txt)
    except Exception:
        pass

def _apply_resolution(mode: str) -> Tuple[bool, str]:
    """
    Persist an output mode and restart mpv so --drm-mode takes effect.

    Returns (applied_as_requested, active_mode).

    If mpv will not come up on the requested mode, revert to RESOLUTION_DEFAULT
    and persist that.  This matters more than it first looks: a forced mode the
    display cannot actually do would otherwise leave a headless unit with no
    picture AND the mpv watchdog restarting a process that can never succeed,
    which is not recoverable without SSH access to the box.

    Note the lock discipline.  state_lock is a plain, non-reentrant Lock, so it
    is taken and released around each small update rather than held across the
    mpv restart - holding it and then re-acquiring it in the finally block
    would deadlock the player outright.
    """
    with state_lock:
        previous            = state.resolution
        state.resolution    = mode
        state.restart_in_progress = True
    save_resolution(mode)
    logger.info("Output resolution set to %s — restarting mpv", mode)

    ok       = False
    reverted = False
    try:
        path, loop = _desired_video()
        ok = _start_mpv(path, loop=loop)
        if not ok and mode != RESOLUTION_DEFAULT:
            logger.error(
                "mpv would not start on mode %r (previously %r) — reverting to %s",
                mode, previous, RESOLUTION_DEFAULT,
            )
            with state_lock:
                state.resolution = RESOLUTION_DEFAULT
            save_resolution(RESOLUTION_DEFAULT)
            ok = _start_mpv(path, loop=loop)
            reverted = True
    except Exception:
        logger.exception("Error applying resolution")
    finally:
        with state_lock:
            state.restart_in_progress = False

    with state_lock:
        active = state.resolution
    return (ok and not reverted, active)

# ══════════════════════════════════════════════════════════════════════════════
# MPV IPC — JSON SOCKET COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

def _mpv_send(command: list, timeout: float = 2.0) -> Optional[dict]:
    """
    Send a JSON IPC command to the running mpv process via the Unix socket.
    Returns the response dict or None on failure.
    """
    if not os.path.exists(MPV_SOCKET):
        logger.debug("mpv IPC socket not present")
        return None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(MPV_SOCKET)
        payload = json.dumps({"command": command}) + "\n"
        sock.sendall(payload.encode())
        response = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
            if b"\n" in response:
                break
        sock.close()
        return json.loads(response.split(b"\n")[0])
    except Exception:
        logger.debug("mpv IPC error for command %s", command)
        return None

def mpv_get_property(prop: str) -> Optional[object]:
    resp = _mpv_send(["get_property", prop])
    if resp and resp.get("error") == "success":
        return resp.get("data")
    return None

def mpv_set_property(prop: str, value: object):
    _mpv_send(["set_property", prop, value])

def mpv_command(*args):
    _mpv_send(list(args))

def get_mpv_status() -> dict:
    """Return a status dict compatible with the existing /status route."""
    if not _mpv_is_running():
        return {"state": "stopped", "time": 0, "length": 0, "volume": state.volume}
    try:
        pos     = mpv_get_property("time-pos")    or 0
        length  = mpv_get_property("duration")    or 0
        paused  = mpv_get_property("pause")
        volume  = mpv_get_property("volume")      or state.volume
        return {
            "state":  "paused" if paused else "playing",
            "time":   int(pos),
            "length": int(length),
            "volume": int(volume),
        }
    except Exception:
        return {"state": "unknown", "time": 0, "length": 0, "volume": state.volume}

# ══════════════════════════════════════════════════════════════════════════════
# VIDEO SWITCHING
# ══════════════════════════════════════════════════════════════════════════════

def switch_video(path: str, loop: bool = False):
    """
    Switch to a new video with minimal black-screen time.

    If mpv is running:  uses 'loadfile replace' IPC command (~50-150 ms gap).
    If mpv has crashed: falls back to a full process restart.

    Loop mode is set immediately after loadfile via set_property.
    """
    if not _mpv_is_running():
        logger.warning("mpv not running — starting fresh")
        _start_mpv(path, loop=loop)
        return

    try:
        # 'loadfile <path> replace' swaps the media without restarting mpv
        mpv_command("loadfile", path, "replace")

        # Set loop mode immediately after
        time.sleep(0.05)   # brief settle for mpv to register the new file
        mpv_set_property("loop-file", "inf" if loop else "no")

        # Restore volume (mpv resets it on some loadfile calls)
        with state_lock:
            vol = state.volume
        mpv_set_property("volume", vol)

        with state_lock:
            state.current_video = os.path.basename(path)

        logger.info("Switched video via IPC: %s (loop=%s)", path, loop)

    except Exception:
        logger.exception("IPC video switch failed — restarting mpv")
        _start_mpv(path, loop=loop)

def load_video(path: str, loop: bool = False):
    """Full mpv restart.  Use only for crash recovery."""
    _start_mpv(path, loop=loop)

def _desired_video() -> Tuple[str, bool]:
    """Return (path, loop) for what should currently be playing."""
    with state_lock:
        ov_active = state.override_active
        ov_path   = state.override_video
    if ov_active and os.path.exists(ov_path):
        return ov_path, True
    return LOOP_VIDEO, True

# ══════════════════════════════════════════════════════════════════════════════
# VOLUME
# ══════════════════════════════════════════════════════════════════════════════

def set_volume(vol: int):
    """Set volume (0-100) in mpv and persist it."""
    vol = max(0, min(100, int(vol)))
    with state_lock:
        state.volume = vol
    mpv_set_property("volume", vol)
    save_volume(vol)

def get_volume() -> int:
    v = mpv_get_property("volume")
    if v is not None:
        return int(v)
    with state_lock:
        return state.volume

# ══════════════════════════════════════════════════════════════════════════════
# PLAYBACK-END DETECTION
# ══════════════════════════════════════════════════════════════════════════════

def wait_for_playback_end(
    startup_grace:  float = 0.3,
    poll_interval:  float = 0.2,
    safety_timeout: float = 24 * 3600,
) -> bool:
    """
    Block until the currently playing trigger video finishes.
    Uses mpv's 'time-pos' and 'duration' properties.

    Returns True if playback ended naturally.
    Returns False if interrupted early (override activated, mpv stopped).
    The caller uses the return value to decide whether to resume the loop.
    """
    time.sleep(startup_grace)
    start = time.time()
    while True:
        if not _mpv_is_running():
            return False

        # Exit immediately if override has been activated mid-trigger.
        # The override handler has already called switch_video(override, loop=True)
        # so we just need to stop blocking — the finally block in play_trigger
        # checks this return value and skips the redundant switch_video call.
        with state_lock:
            if state.override_active:
                logger.info("wait_for_playback_end: override activated — exiting early")
                return False

        st = get_mpv_status()

        # mpv reports None for time-pos when playback ends
        pos    = mpv_get_property("time-pos")
        length = st.get("length", 0)
        vstate = st.get("state", "unknown")

        if vstate == "stopped" or pos is None:
            return True
        if length > 0 and pos >= length - 0.5:
            return True
        if time.time() - start > safety_timeout:
            logger.warning("Safety timeout in wait_for_playback_end")
            return True

        time.sleep(poll_interval)

# ══════════════════════════════════════════════════════════════════════════════
# GPIO TRIGGERS  (lgpio direct — RPi 5 / Bookworm compatible)
# ══════════════════════════════════════════════════════════════════════════════
# Uses lgpio directly rather than through gpiozero. lgpio.gpiochip_open()
# opens /dev/gpiochip0 and returns a handle used for all subsequent calls.
# Polling is done in a per-pin daemon thread at 50 Hz (every 20 ms) which
# is fast enough to catch any button press while remaining lightweight.
# Debounce is handled manually with a per-pin timestamp.
# ══════════════════════════════════════════════════════════════════════════════

def play_trigger(pin: int):
    logger.info("Trigger: pin %s", pin)
    with state_lock:
        if state.override_active:
            logger.info("Trigger pin %s ignored — override is active", pin)
            return
        if state.trigger_running:
            logger.info("Trigger pin %s ignored — trigger already playing: %s",
                        pin, state.current_trigger_video)
            return
        if state.restart_in_progress:
            logger.info("Trigger pin %s ignored — restart in progress", pin)
            return
        state.trigger_running       = True
        video_path                  = TRIGGER_VIDEOS.get(pin)
        state.current_trigger_video = os.path.basename(video_path) if video_path else None
        state.paused                = False

    natural_end = False
    try:
        if not video_path or not os.path.exists(video_path):
            logger.warning("Trigger video not found: %s", video_path)
            return
        switch_video(video_path, loop=False)
        natural_end = wait_for_playback_end()
    except Exception:
        logger.exception("Error in trigger playback pin %s", pin)
    finally:
        with state_lock:
            state.trigger_running       = False
            state.current_trigger_video = None
            state.paused                = False
            override_interrupted        = state.override_active
            should_resume               = not state.restart_in_progress

        if override_interrupted:
            # Override activated mid-trigger — it already called switch_video.
            # Don't issue another switch here or it will cause a stutter.
            logger.info("Trigger finished — override is active, not resuming loop.")
        elif should_resume:
            path, loop = _desired_video()
            switch_video(path, loop=loop)

# Open the GPIO chip handle once at module level and share across all pin workers
_gpio_handle: Optional[int] = None
_last_trigger_time: dict = {p: 0.0 for p in GPIO_PINS}

def _gpio_init() -> bool:
    """Open /dev/gpiochip0 and claim all trigger pins as inputs with pull-up."""
    global _gpio_handle
    if not _LGPIO_AVAILABLE:
        logger.warning("lgpio not available — GPIO triggers disabled")
        return False
    try:
        _gpio_handle = _lgpio.gpiochip_open(0)
        for pin in GPIO_PINS:
            _lgpio.gpio_claim_input(_gpio_handle, pin, _lgpio.SET_PULL_UP)
            logger.info("GPIO trigger registered on pin %s", pin)
        return True
    except Exception:
        logger.exception("Failed to initialise GPIO chip")
        return False

def _gpio_trigger_worker(pin: int):
    """Poll a single GPIO pin at 50 Hz and fire play_trigger on falling edge."""
    logger.info("GPIO poll worker started for pin %s", pin)
    last_state = 1   # pins are HIGH at rest (pull-up)
    while True:
        try:
            if _gpio_handle is not None:
                current = _lgpio.gpio_read(_gpio_handle, pin)
                now     = time.time()
                # Falling edge = button pressed (HIGH → LOW)
                if current == 0 and last_state == 1:
                    if now - _last_trigger_time[pin] >= GPIO_DEBOUNCE_S:
                        _last_trigger_time[pin] = now
                        threading.Thread(
                            target=play_trigger, args=(pin,),
                            daemon=True, name=f"trigger-{pin}",
                        ).start()
                last_state = current
        except Exception:
            logger.exception("GPIO poll error on pin %s", pin)
        time.sleep(0.02)   # 50 Hz poll — fast enough, lightweight

# Initialise GPIO and start per-pin poll threads
_gpio_buttons: list = []   # kept for cleanup compatibility
if _gpio_init():
    for _pin in GPIO_PINS:
        threading.Thread(
            target=_gpio_trigger_worker, args=(_pin,),
            daemon=True, name=f"gpio-{_pin}",
        ).start()

# ══════════════════════════════════════════════════════════════════════════════
# UDP LISTENER
# ══════════════════════════════════════════════════════════════════════════════

def udp_listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("0.0.0.0", UDP_PORT))
    except Exception:
        logger.exception("Failed to bind UDP port %s", UDP_PORT)
        return
    logger.info("UDP listener on port %s", UDP_PORT)
    while True:
        try:
            data, addr = sock.recvfrom(1024)
            cmd = data.decode("utf-8").strip().upper()
            logger.debug("UDP from %s: %s", addr, cmd)
            if cmd not in UDP_COMMANDS:
                continue
            action = UDP_COMMANDS[cmd]

            if action == "play":
                mpv_set_property("pause", False)
                with state_lock:
                    state.paused = False

            elif action == "pause":
                mpv_set_property("pause", True)
                with state_lock:
                    state.paused = True

            elif action == "restart":
                with state_lock:
                    state.restart_in_progress = True
                try:
                    path, loop = _desired_video()
                    switch_video(path, loop=loop)
                finally:
                    with state_lock:
                        state.restart_in_progress = False

            elif action == "override_on":
                with state_lock:
                    state.override_active     = True
                    config["override_active"] = True
                    ov                        = state.override_video
                    cfg_snap                  = dict(config)
                logger.info("Override ACTIVATED via UDP — switching to: %s",
                            os.path.basename(ov))
                save_config(cfg_snap)
                if os.path.exists(ov):
                    switch_video(ov, loop=True)
                else:
                    logger.warning("Override video not found: %s", ov)

            elif action == "override_off":
                with state_lock:
                    state.override_active     = False
                    config["override_active"] = False
                    cfg_snap                  = dict(config)
                logger.info("Override DEACTIVATED via UDP — returning to loop")
                save_config(cfg_snap)
                if os.path.exists(LOOP_VIDEO):
                    switch_video(LOOP_VIDEO, loop=True)
                else:
                    logger.warning("Loop video not found: %s", LOOP_VIDEO)

            elif action in ("trigger_1", "trigger_2", "trigger_3"):
                # Map action string to GPIO pin number then fire play_trigger
                # in a daemon thread — identical behaviour to a physical button press.
                pin_map = {
                    "trigger_1": GPIO_PINS[0],
                    "trigger_2": GPIO_PINS[1],
                    "trigger_3": GPIO_PINS[2],
                }
                pin = pin_map[action]
                logger.info("UDP trigger command: %s → pin %s", action, pin)
                threading.Thread(
                    target=play_trigger, args=(pin,),
                    daemon=True, name=f"udp-trigger-{pin}",
                ).start()

        except Exception:
            logger.exception("UDP listener error")

# ══════════════════════════════════════════════════════════════════════════════
# UPLOAD HELPER
# ══════════════════════════════════════════════════════════════════════════════

def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

# ══════════════════════════════════════════════════════════════════════════════
# SYSTEM INFO HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _get_cpu_temp() -> str:
    """Read CPU temperature from thermal zone or vcgencmd."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return f"{int(f.read().strip()) / 1000:.1f}°C"
    except Exception:
        pass
    try:
        out = subprocess.check_output(
            ["vcgencmd", "measure_temp"], stderr=subprocess.DEVNULL
        ).decode().strip()
        return out.replace("temp=", "")
    except Exception:
        return "N/A"

def _get_mem_info() -> dict:
    """Parse /proc/meminfo for total and available memory."""
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if parts[0] in ("MemTotal:", "MemAvailable:"):
                    info[parts[0].rstrip(":")] = int(parts[1]) * 1024
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", 0)
        used  = total - avail
        pct   = int(used / total * 100) if total else 0
        return {
            "used_mb":  round(used  / 1048576, 1),
            "total_mb": round(total / 1048576, 1),
            "pct":      pct,
        }
    except Exception:
        return {"used_mb": 0, "total_mb": 0, "pct": 0}

def _get_disk_info(path: str = "/") -> dict:
    """Use os.statvfs to get disk usage."""
    try:
        st    = os.statvfs(path)
        total = st.f_frsize * st.f_blocks
        free  = st.f_frsize * st.f_bfree
        used  = total - free
        pct   = int(used / total * 100) if total else 0
        return {
            "used_gb":  round(used  / 1073741824, 1),
            "total_gb": round(total / 1073741824, 1),
            "pct":      pct,
        }
    except Exception:
        return {"used_gb": 0, "total_gb": 0, "pct": 0}

def _get_max_upload_bytes(path: str = "/", headroom_pct: float = 15.0) -> int:
    """
    Return the maximum allowed upload size in bytes.
    Calculated as: free disk space × (1 - headroom_pct/100)
    i.e. we always keep at least headroom_pct% of current free space reserved.
    Recalculated on every request so it reflects the current state of the disk.
    Minimum returned value is 64 MB to avoid pathological edge cases.
    """
    try:
        st   = os.statvfs(path)
        free = st.f_frsize * st.f_bavail   # f_bavail = free for non-root users
        limit = int(free * (1.0 - headroom_pct / 100.0))
        return max(limit, 64 * 1024 * 1024)  # floor of 64 MB
    except Exception:
        return 4 * 1024 * 1024 * 1024       # 4 GB fallback

def _get_uptime() -> str:
    """Parse /proc/uptime and return human-readable string."""
    try:
        with open("/proc/uptime") as f:
            secs = int(float(f.read().split()[0]))
        d, rem = divmod(secs, 86400)
        h, rem = divmod(rem, 3600)
        m, s   = divmod(rem, 60)
        if d:
            return f"{d}d {h:02d}:{m:02d}:{s:02d}"
        return f"{h:02d}:{m:02d}:{s:02d}"
    except Exception:
        return "N/A"

def _get_video_slots() -> dict:
    """Return size and existence info for each video slot."""
    slots = {
        "loop":     LOOP_VIDEO,
        "trigger1": TRIGGER_VIDEOS.get(GPIO_PINS[0], ""),
        "trigger2": TRIGGER_VIDEOS.get(GPIO_PINS[1], ""),
        "trigger3": TRIGGER_VIDEOS.get(GPIO_PINS[2], ""),
        "override": OVERRIDE_VIDEO_DEFAULT,
    }
    result = {}
    for name, path in slots.items():
        if path and os.path.exists(path):
            size_mb = round(os.path.getsize(path) / 1048576, 1)
            mtime   = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(path))
            )
            result[name] = {"exists": True, "size_mb": size_mb, "modified": mtime}
        else:
            result[name] = {"exists": False}
    return result

def _validate_script(content: bytes) -> tuple[bool, str]:
    """
    Validate an uploaded Python script before deploying it.
    Checks:
      1. Valid Python syntax
      2. Contains SAW_PLAYER_SIGNATURE constant
      3. Contains all required function definitions
    Returns (ok, reason).
    """
    import ast as _ast
    try:
        text = content.decode("utf-8")
    except Exception:
        return False, "File is not valid UTF-8 text"
    try:
        _ast.parse(text)
    except SyntaxError as e:
        return False, f"Python syntax error at line {e.lineno}: {e.msg}"
    if 'SAW_PLAYER_SIGNATURE = "SAW-PLAYER-AUTHENTIC"' not in text:
        return False, "Missing SAW_PLAYER_SIGNATURE — this does not appear to be a valid SAW player script"
    for fn in REQUIRED_FUNCTIONS:
        if fn not in text:
            return False, f"Required identifier '{fn}' not found in script"
    return True, "OK"

# ══════════════════════════════════════════════════════════════════════════════
# FLASK HTML TEMPLATE
# ══════════════════════════════════════════════════════════════════════════════

HTML_TEMPLATE = """<!doctype html>
<html>
<head>
<title>{% if player_name %}{{ player_name }} — {% endif %}SAW Video Player</title>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{box-sizing:border-box}
body{font-family:Arial,sans-serif;margin:0;padding:16px;background:#f0f2f5;color:#222}
h1{font-size:1.6em;margin:0 0 4px}
h3{margin:0 0 10px;font-size:1.05em}
.card{background:#fff;border-radius:10px;border:1px solid #dde;padding:16px;margin-bottom:14px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px}
.row label.lbl{width:150px;flex-shrink:0;font-size:0.95em}
.row input[type=file]{flex:1;min-width:0;font-size:0.9em}
/* buttons */
.btn{padding:7px 16px;font-size:0.93em;border-radius:6px;border:none;cursor:pointer;color:#fff;white-space:nowrap}
.btn-blue{background:#007bff}.btn-green{background:#28a745}.btn-grey{background:#6c757d}
.btn-red{background:#dc3545}.btn-orange{background:#fd7e14}
.btn:disabled{background:#aaa;cursor:not-allowed}
.btn-sm{padding:5px 12px;font-size:0.85em}
/* status */
.status-grid{display:grid;grid-template-columns:auto 1fr;gap:2px 10px;font-size:0.95em}
.status-grid b{text-align:right;color:#555}
/* volume */
input[type=range]{width:260px;vertical-align:middle}
/* progress */
.prog-wrap{display:none;margin-top:4px;width:100%}
.prog-bg{background:#e0e0e0;border-radius:6px;height:12px;overflow:hidden}
.prog-fg{height:12px;width:0;background:#28a745;border-radius:6px;transition:width .15s}
.prog-lbl{font-size:0.78em;color:#555;margin-top:2px}
/* system info */
.sys-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.sys-item{background:#f8f9fa;border-radius:8px;padding:10px 12px}
.sys-item .val{font-size:1.15em;font-weight:bold;color:#007bff}
.sys-item .key{font-size:0.78em;color:#666;margin-top:2px}
.bar-bg{background:#ddd;border-radius:4px;height:6px;margin-top:4px}
.bar-fg{height:6px;border-radius:4px;background:#28a745;transition:width .5s}
.bar-fg.warn{background:#fd7e14}
.bar-fg.danger{background:#dc3545}
/* slot status */
.slot-badge{font-size:0.75em;padding:2px 7px;border-radius:10px;font-weight:bold;margin-left:6px}
.slot-ok{background:#d4edda;color:#155724}
.slot-missing{background:#f8d7da;color:#721c24}
/* toast */
#toast{position:fixed;bottom:28px;left:50%;transform:translateX(-50%) translateY(80px);
  background:#222;color:#fff;padding:12px 26px;border-radius:10px;font-size:0.95em;
  opacity:0;transition:opacity .3s,transform .3s;z-index:9999;pointer-events:none;
  max-width:92vw;text-align:center}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
#toast.ok{background:#28a745}
#toast.err{background:#dc3545}
#toast.info{background:#007bff}
/* override / audio */
.audio-hint{font-size:0.8em;color:#555;margin:4px 0 6px}
.audio-hint a{color:#007bff}
/* misc */
.downloads a{margin-right:10px;text-decoration:none;color:#007bff;font-size:0.9em}
.sep{border:none;border-top:1px solid #eee;margin:12px 0}
</style>
<script>
/* ── Toast ── */
let _tt=null;
function toast(msg,type='ok',ms=4000){
  const t=document.getElementById('toast');
  t.textContent=msg; t.className='show '+type;
  if(_tt)clearTimeout(_tt);
  _tt=setTimeout(()=>{t.className='';},ms);
}

/* ── Status polling ── */
async function fetchStatus(){
  try{
    const d=await(await fetch('/status')).json();
    document.getElementById('sv').textContent=d.video;
    document.getElementById('ss').textContent=d.state;
    document.getElementById('sp').textContent=d.position;
    document.getElementById('sr').textContent=d.remaining;
    document.getElementById('overrideCheckbox').checked=!!d.override_active;
  }catch(e){}
}
async function fetchVolume(){
  try{
    const j=await(await fetch('/volume')).json();
    document.getElementById('volSlider').value=j.volume;
    document.getElementById('volVal').textContent=j.volume+'%';
  }catch(e){}
}
function setVolume(v){
  fetch('/volume',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({volume:parseInt(v)})});
  document.getElementById('volVal').textContent=v+'%';
}
async function setOverride(a){
  await fetch('/set_override',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({active:!!a})});
  fetchStatus();
}
async function mpvCmd(url){await fetch(url,{method:'POST'});}
async function setLogLevel(l){
  await fetch('/logs/set_level',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({level:l})});
}
setInterval(fetchStatus,1000);
setInterval(fetchVolume,5000);

/* ── Log viewer ── */
let _logAutoRefresh=null;
let _logOpen=false;

async function fetchLogs(){
  // Only fetch if the viewer is open
  if(!_logOpen) return;
  const n=document.getElementById('logLines').value||100;
  try{
    const d=await(await fetch('/logs/tail?lines='+n)).json();
    const pre=document.getElementById('logViewer');
    pre.textContent=d.lines.join(String.fromCharCode(10));
    const atBottom=pre.scrollHeight-pre.scrollTop<=pre.clientHeight+40;
    const autoScroll=document.getElementById('autoScrollLog').checked;
    if(autoScroll||atBottom) pre.scrollTop=pre.scrollHeight;
    const sz=d.size_mb<1?(d.size_bytes/1024).toFixed(1)+' KB':d.size_mb+' MB';
    document.getElementById('logSize').textContent=sz+' ('+d.total_lines+' lines)';
  }catch(e){}
}

function toggleLogPanel(){
  _logOpen=!_logOpen;
  const body=document.getElementById('logBody');
  const btn=document.getElementById('logToggleBtn');
  body.style.display=_logOpen?'block':'none';
  btn.textContent=_logOpen?'▲ Hide Logs':'▼ Show Logs';
  // Fetch immediately on first open; start auto-refresh if checkbox ticked
  if(_logOpen){
    fetchLogs();
    const cb=document.getElementById('autoRefreshLog');
    if(cb&&cb.checked&&!_logAutoRefresh)
      _logAutoRefresh=setInterval(fetchLogs,5000);
  } else {
    // Pause auto-refresh while collapsed
    if(_logAutoRefresh){ clearInterval(_logAutoRefresh); _logAutoRefresh=null; }
    const cb=document.getElementById('autoRefreshLog');
    if(cb) cb.checked=false;
  }
}

function toggleLogAutoRefresh(cb){
  if(!_logOpen) return; // no-op if panel is collapsed
  if(cb.checked){
    fetchLogs();
    _logAutoRefresh=setInterval(fetchLogs,5000);
  } else {
    clearInterval(_logAutoRefresh);_logAutoRefresh=null;
  }
}

/* ── System info ── */
async function fetchSysInfo(){
  try{
    const d=await(await fetch('/system/info')).json();
    if(d.error)return;
    document.getElementById('si_temp').textContent=d.cpu_temp;
    const mem=d.memory;
    document.getElementById('si_mem').textContent=mem.used_mb+'MB / '+mem.total_mb+'MB';
    setBar('bar_mem',mem.pct);
    const disk=d.disk;
    document.getElementById('si_disk').textContent=disk.used_gb+'GB / '+disk.total_gb+'GB';
    setBar('bar_disk',disk.pct);
    document.getElementById('si_uptime').textContent=d.uptime;
    document.getElementById('si_host').textContent=d.hostname;
  }catch(e){}
}
function setBar(id,pct){
  const b=document.getElementById(id);
  if(!b)return;
  b.style.width=pct+'%';
  b.className='bar-fg'+(pct>=90?' danger':pct>=70?' warn':'');
}
setInterval(fetchSysInfo,5000);

/* ── Video slot status ── */
async function fetchSlotStatus(){
  try{
    const d=await(await fetch('/system/video_slots')).json();
    const slots={loop:'sl_loop',trigger1:'sl_t1',trigger2:'sl_t2',trigger3:'sl_t3',override:'sl_ov'};
    for(const[key,id] of Object.entries(slots)){
      const el=document.getElementById(id);
      if(!el)continue;
      const s=d[key];
      if(s&&s.exists){
        el.className='slot-badge slot-ok';
        el.title=s.modified;
        el.textContent=s.size_mb+'MB';
      } else {
        el.className='slot-badge slot-missing';
        el.textContent='missing';
        el.title='';
      }
    }
  }catch(e){}
}
setInterval(fetchSlotStatus,10000);

/* ── Audio device ── */
async function loadAudioDevices(){
  const sel=document.getElementById('audioSel');
  const st=document.getElementById('audioSt');
  try{
    const [dr,cr]=await Promise.all([fetch('/audio_devices'),fetch('/audio_device')]);
    const devices=await dr.json();
    const current=(await cr.json()).audio_device;
    sel.innerHTML='';
    devices.forEach(d=>{
      const o=document.createElement('option');
      o.value=d.device;o.textContent=d.label;
      if(d.device===current)o.selected=true;
      sel.appendChild(o);
    });
    st.textContent='Current: '+current;
  }catch(e){st.textContent='Could not load devices';}
}
/* A refresh rate can only be pinned to a real WxH mode, never to
   "preferred"/"highest". Offered as a fixed list because sysfs reports the
   resolutions a display supports but not the rates, so these are candidates
   rather than a verified list - a combination the screen cannot do simply
   fails to start and the player reverts on its own. */
const RES_HZ_CHOICES=['Auto','24','25','30','50','60'];
function isFixedMode(v){return /^[0-9]+x[0-9]+$/.test(v);}
function syncHzEnabled(){
  const sel=document.getElementById('resSel');
  const hz=document.getElementById('resHz');
  const on=isFixedMode(sel.value);
  hz.disabled=!on;
  if(!on)hz.value='Auto';
}
function resolutionValue(){
  const base=document.getElementById('resSel').value;
  const hz=document.getElementById('resHz').value;
  if(!isFixedMode(base))return base;
  return (hz==='Auto')?base:(base+'@'+hz);
}
async function loadDisplayModes(){
  const sel=document.getElementById('resSel');
  const hz=document.getElementById('resHz');
  const st=document.getElementById('resSt');
  try{
    const d=await (await fetch('/display_modes')).json();
    const modes=d.modes||[];
    const cur=String(d.current||d.default);
    const at=cur.indexOf('@');
    const curBase=(at>=0)?cur.substring(0,at):cur;
    const curHz=(at>=0)?cur.substring(at+1):'Auto';
    sel.innerHTML='';
    const opts=[{v:d.default,t:'Auto '+String.fromCharCode(8212)+' display preferred mode'}];
    modes.forEach(m=>opts.push({v:m,t:m}));
    if(curBase&&!opts.some(o=>o.v===curBase))opts.push({v:curBase,t:curBase+' (saved)'});
    opts.forEach(o=>{
      const e=document.createElement('option');
      e.value=o.v;e.textContent=o.t;
      if(o.v===curBase)e.selected=true;
      sel.appendChild(e);
    });
    hz.innerHTML='';
    const rates=RES_HZ_CHOICES.slice();
    if(curHz!=='Auto'&&rates.indexOf(curHz)<0)rates.push(curHz);
    rates.forEach(h=>{
      const e=document.createElement('option');
      e.value=h;
      e.textContent=(h==='Auto')?'Auto (let the display choose)':(h+' Hz');
      if(h===curHz)e.selected=true;
      hz.appendChild(e);
    });
    syncHzEnabled();
    st.textContent=modes.length
      ? 'Current: '+cur
      : 'Current: '+cur+' (could not read display modes '+String.fromCharCode(8212)+' is a screen connected?)';
  }catch(e){st.textContent='Could not load display modes';}
}
async function applyResolution(){
  const st=document.getElementById('resSt');
  const want=resolutionValue();
  st.textContent='Applying '+String.fromCharCode(8212)+' the picture will go black briefly...';
  try{
    const r=await fetch('/resolution',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({resolution:want})});
    if(!r.ok){st.textContent='Error setting resolution';toast('Could not set resolution','err');return;}
    const d=await r.json();
    const hz=d.display_fps?('  ('+Math.round(d.display_fps)+' Hz)'):'';
    if(d.reverted){
      st.textContent='Rejected: '+want+' would not start '+String.fromCharCode(8212)+' reverted to '+d.resolution+hz;
      toast(want+' not supported by this display '+String.fromCharCode(8212)+' reverted','err');
    }else{
      st.textContent='Active: '+d.resolution+hz;
      toast('Output resolution set to '+d.resolution,'ok');
    }
  }catch(e){st.textContent='Request failed';}
}
async function applyAudioDevice(){
  const sel=document.getElementById('audioSel');
  const st=document.getElementById('audioSt');
  st.textContent='Applying…';
  try{
    const r=await fetch('/audio_device',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({audio_device:sel.value})});
    st.textContent=r.ok?'✔ Active: '+sel.value:'Error setting device';
  }catch(e){st.textContent='Request failed';}
}

/* ── XHR upload with progress ── */
function doUpload(inputId,url,label,barId,lblId,wrapId,btnId){
  const input=document.getElementById(inputId);
  const file=input.files[0];
  if(!file){toast('Please choose a file first.','err');return;}
  const bar=document.getElementById(barId),lbl=document.getElementById(lblId),
        wrap=document.getElementById(wrapId),btn=document.getElementById(btnId);
  wrap.style.display='block';bar.style.width='0%';lbl.textContent='Starting…';btn.disabled=true;
  const xhr=new XMLHttpRequest();
  xhr.open('POST',url);
  xhr.upload.addEventListener('progress',e=>{
    if(e.lengthComputable){
      const pct=Math.round(e.loaded/e.total*100);
      bar.style.width=pct+'%';
      lbl.textContent=pct+'%  ('+(e.loaded/1048576).toFixed(1)+' / '+(e.total/1048576).toFixed(1)+' MB)';
    }
  });
  xhr.addEventListener('load',()=>{
    if(xhr.status===200||xhr.status===302){
      bar.style.width='100%';lbl.textContent='Complete!';
      toast('✅ '+label+' uploaded — '+file.name,'ok');
      input.value='';
      fetchSlotStatus();
      fetchMaxUpload();
      setTimeout(()=>{wrap.style.display='none';bar.style.width='0%';},3000);
    } else {
      lbl.textContent='Failed ('+xhr.status+')';
      toast('❌ Upload failed for '+label+' ('+xhr.status+')','err');
    }
    btn.disabled=false;
  });
  xhr.addEventListener('error',()=>{lbl.textContent='Network error';toast('❌ Network error','err');btn.disabled=false;});
  xhr.addEventListener('abort',()=>{lbl.textContent='Cancelled';btn.disabled=false;});
  const fd=new FormData();fd.append('file',file);xhr.send(fd);
}

/* ── Script update upload ── */
function doScriptUpload(){
  const input=document.getElementById('scriptFile');
  const file=input.files[0];
  if(!file){toast('Please choose a .py file','err');return;}
  const btn=document.getElementById('scriptBtn');
  const st=document.getElementById('scriptSt');
  btn.disabled=true;
  st.textContent='Validating and uploading…';
  const xhr=new XMLHttpRequest();
  xhr.open('POST','/upload/script');
  xhr.upload.addEventListener('progress',e=>{
    if(e.lengthComputable)
      st.textContent='Uploading… '+(e.loaded/1048576).toFixed(1)+' MB';
  });
  xhr.addEventListener('load',()=>{
    if(xhr.status===200){
      st.textContent='✅ Deployed! Service restarting — page will reconnect in ~15s…';
      toast('✅ Script deployed — service restarting','ok',8000);
      input.value='';
      setTimeout(()=>{
        st.textContent='Reconnecting…';
        window.location.reload();
      },15000);
    } else {
      st.textContent='❌ Rejected: '+xhr.responseText;
      toast('❌ Script rejected: '+xhr.responseText,'err',8000);
      btn.disabled=false;
    }
  });
  xhr.addEventListener('error',()=>{
    st.textContent='Network error';toast('❌ Network error','err');btn.disabled=false;
  });
  const fd=new FormData();fd.append('file',file);xhr.send(fd);
}

/* ── Player name ── */
async function fetchPlayerName(){
  try{
    const d=await(await fetch('/player_name')).json();
    const inp=document.getElementById('playerNameInput');
    if(inp) inp.value=d.player_name||'';
    updatePageTitle(d.player_name||'');
  }catch(e){}
}
async function savePlayerName(){
  const inp=document.getElementById('playerNameInput');
  const name=(inp?inp.value:'').trim().slice(0,64);
  try{
    await fetch('/player_name',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({player_name:name})});
    updatePageTitle(name);
    toast('✅ Player name saved','ok');
  }catch(e){toast('❌ Failed to save name','err');}
}
function updatePageTitle(name){
  document.title=name?name+' — SAW Video Player':'SAW Video Player';
  const h=document.getElementById('pageTitle');
  if(h) h.textContent=name?name:'Stage Audio Works Video Player';
  const sub=document.getElementById('pageSubtitle');
  if(sub) sub.textContent=name?'Stage Audio Works Video Player':'';
}
// Allow Enter key to save
function playerNameKeydown(e){
  if(e.key==='Enter') savePlayerName();
}

/* ── Max upload size ── */
async function fetchMaxUpload(){
  try{
    const d=await(await fetch('/system/max_upload')).json();
    const el=document.getElementById('maxUploadLabel');
    if(el) el.textContent='Max upload: '+d.max_label+' ('+d.free_label+' free, 15% reserved)';
  }catch(e){}
}

/* ── Reboot ── */
async function doReboot(){
  if(!confirm('Reboot the Pi now? The player will be unavailable for ~30 seconds.'))return;
  toast('Rebooting… reconnecting in 40s','info',45000);
  await fetch('/system/reboot',{method:'POST'});
  setTimeout(()=>window.location.reload(),40000);
}

window.onload=function(){
  fetchStatus();fetchVolume();fetchSysInfo();
  fetchSlotStatus();loadAudioDevices();loadDisplayModes();fetchPlayerName();fetchMaxUpload();
}
</script>
</head>
<body>
<div id="toast"></div>
<h1 id="pageTitle" style="margin-bottom:2px">{% if player_name %}{{ player_name }}{% else %}Stage Audio Works Video Player{% endif %}</h1>
<div id="pageSubtitle" style="font-size:0.95em;color:#666;margin-bottom:12px">{% if player_name %}Stage Audio Works Video Player{% endif %}</div>

<!-- ── Player name ── -->
<div class="card" style="padding:12px 16px">
  <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
    <label style="font-size:0.9em;color:#555;flex-shrink:0">📍 Player Name:</label>
    <input id="playerNameInput" type="text" maxlength="64"
      value="{{ player_name }}"
      placeholder="e.g. Entrance Hall, Gallery 3, Main Stage…"
      onkeydown="playerNameKeydown(event)"
      style="flex:1;min-width:180px;padding:6px 10px;border:1px solid #ccc;
             border-radius:6px;font-size:0.95em;max-width:400px">
    <button class="btn btn-green btn-sm" onclick="savePlayerName()">Save</button>
  </div>
</div>

<!-- ── Playback status ── -->
<div class="card">
  <div class="status-grid">
    <b>Video:</b>    <span id="sv">—</span>
    <b>Status:</b>   <span id="ss">—</span>
    <b>Position:</b> <span id="sp">—</span>
    <b>Remaining:</b><span id="sr">—</span>
  </div>
  <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
    <button class="btn btn-blue" onclick="mpvCmd('/play')">▶ Play</button>
    <button class="btn btn-blue" onclick="mpvCmd('/pause')">⏸ Pause</button>
    <button class="btn btn-grey" onclick="mpvCmd('/restart')">🔄 Restart Loop</button>
    <button class="btn btn-red"  onclick="doReboot()" style="margin-left:auto">⏻ Reboot Pi</button>
  </div>
  <div style="margin-top:12px">
    <label>Volume:</label>
    <input type="range" id="volSlider" min="0" max="100" step="1" oninput="setVolume(this.value)">
    <span id="volVal">--%</span>
  </div>
</div>

<!-- ── System info ── -->
<div class="card">
  <h3>🖥 System</h3>
  <div class="sys-grid">
    <div class="sys-item">
      <div class="val" id="si_temp">—</div>
      <div class="key">CPU Temperature</div>
    </div>
    <div class="sys-item">
      <div class="val" id="si_uptime">—</div>
      <div class="key">Uptime</div>
    </div>
    <div class="sys-item">
      <div class="val" id="si_mem">—</div>
      <div class="key">Memory</div>
      <div class="bar-bg"><div class="bar-fg" id="bar_mem"></div></div>
    </div>
    <div class="sys-item">
      <div class="val" id="si_disk">—</div>
      <div class="key">Disk</div>
      <div class="bar-bg"><div class="bar-fg" id="bar_disk"></div></div>
    </div>
    <div class="sys-item">
      <div class="val" id="si_host">—</div>
      <div class="key">Hostname</div>
    </div>
  </div>
</div>

<!-- ── Audio output ── -->
<div class="card">
  <h3>🔊 Audio Output</h3>
  <p class="audio-hint">Plug in USB adapter then click Refresh. Selection persists across reboots.</p>
  <select id="audioSel" style="width:100%;max-width:500px;padding:5px;margin-bottom:8px"></select><br>
  <button class="btn btn-green btn-sm" onclick="applyAudioDevice()">Apply</button>
  <button class="btn btn-grey btn-sm" onclick="loadAudioDevices()" style="margin-left:6px">↺ Refresh</button>
  <div id="audioSt" style="font-size:0.82em;margin-top:6px;color:#155724"></div>
</div>

<!-- Output resolution -->
<div class="card">
  <h3>&#128250; Output Resolution</h3>
  <p class="audio-hint">Forces the HDMI output mode. Only resolutions this display reports are listed.
     Content here is PAL 25 fps, so 50 Hz divides evenly into the frame rate and 60 Hz does not &mdash;
     if a clip judders on an otherwise clean file, try 50 Hz first.
     Refresh rates are not reported by the display, so not every combination will exist; if one
     will not start the player falls back to the display's preferred mode on its own.
     Applying restarts the player, so the picture goes black for a second.
     Selection persists across reboots.</p>
  <label for="resSel" style="font-size:0.82em;color:#555">Resolution</label><br>
  <select id="resSel" onchange="syncHzEnabled()" style="width:100%;max-width:340px;padding:5px;margin-bottom:8px"></select><br>
  <label for="resHz" style="font-size:0.82em;color:#555">Refresh rate</label><br>
  <select id="resHz" style="width:100%;max-width:340px;padding:5px;margin-bottom:8px"></select><br>
  <button class="btn btn-green btn-sm" onclick="applyResolution()">Apply</button>
  <button class="btn btn-grey btn-sm" onclick="loadDisplayModes()" style="margin-left:6px">&#8634; Refresh</button>
  <div id="resSt" style="font-size:0.82em;margin-top:6px;color:#155724"></div>
</div>

<!-- ── Manage videos ── -->
<div class="card">
  <h3>📁 Manage Videos</h3>
  <div id="maxUploadLabel" style="font-size:0.82em;color:#555;margin:-4px 0 10px">Calculating available space…</div>

  {% for slot_id, slot_label, slot_url, slot_badge in [
    ('f_loop','Loop Video',    '/upload/loop',       'sl_loop'),
    ('f_t1',  'Trigger 1',    '/upload/trigger/1',  'sl_t1'),
    ('f_t2',  'Trigger 2',    '/upload/trigger/2',  'sl_t2'),
    ('f_t3',  'Trigger 3',    '/upload/trigger/3',  'sl_t3'),
  ] %}
  <div class="row">
    <label class="lbl">{{ slot_label }}:<span class="slot-badge slot-missing" id="{{ slot_badge }}">…</span></label>
    <input type="file" id="{{ slot_id }}" accept="video/mp4">
    <button class="btn btn-blue btn-sm" id="b_{{ slot_id }}"
      onclick="doUpload('{{ slot_id }}','{{ slot_url }}','{{ slot_label }}',
               'pb_{{ slot_id }}','pl_{{ slot_id }}','pw_{{ slot_id }}','b_{{ slot_id }}')">Upload</button>
    <a href="{{ slot_url.replace('/upload/','/download/') }}" class="btn btn-grey btn-sm">⬇</a>
  </div>
  <div class="prog-wrap" id="pw_{{ slot_id }}">
    <div class="prog-bg"><div class="prog-fg" id="pb_{{ slot_id }}"></div></div>
    <div class="prog-lbl" id="pl_{{ slot_id }}"></div>
  </div>
  {% endfor %}

  <hr class="sep">

  <div class="row">
    <label class="lbl">Override:<span class="slot-badge slot-missing" id="sl_ov">…</span></label>
    <input type="file" id="f_ov" accept="video/mp4">
    <button class="btn btn-orange btn-sm" id="b_f_ov"
      onclick="doUpload('f_ov','/upload/override','Override','pb_ov','pl_ov','pw_ov','b_f_ov')">Upload</button>
    <a href="/download/override" class="btn btn-grey btn-sm">⬇</a>
  </div>
  <div class="prog-wrap" id="pw_ov">
    <div class="prog-bg"><div class="prog-fg" id="pb_ov"></div></div>
    <div class="prog-lbl" id="pl_ov"></div>
  </div>

  <div style="margin-top:12px">
    <label>
      <input type="checkbox" id="overrideCheckbox" onchange="setOverride(this.checked)">
      <strong>Activate Override</strong>
    </label>
  </div>
</div>

<!-- ── Script update ── -->
<div class="card">
  <h3>⚙️ Script Update</h3>
  <p style="font-size:0.85em;color:#555;margin:0 0 10px">
    Upload a new <code>.py</code> player script. The file will be validated before deployment —
    it must contain the <code>SAW_PLAYER_SIGNATURE</code> identifier and pass a Python syntax check.
    The current script is backed up automatically before replacement.
    The service will restart automatically (~10s downtime).
  </p>
  <div class="row">
    <input type="file" id="scriptFile" accept=".py" style="flex:1">
    <button class="btn btn-orange btn-sm" id="scriptBtn" onclick="doScriptUpload()">Deploy Script</button>
  </div>
  <div id="scriptSt" style="font-size:0.85em;margin-top:6px;color:#555"></div>
</div>

<!-- ── Logs ── -->
<div class="card">
  <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px">
    <h3 style="margin:0">📋 Logs — <span id="logSize" style="font-size:0.85em;font-weight:normal;color:#555">—</span></h3>
    <button id="logToggleBtn" class="btn btn-grey btn-sm" onclick="toggleLogPanel()">▼ Show Logs</button>
  </div>
  <!-- collapsed by default — logBody is hidden until user clicks Show Logs -->
  <div id="logBody" style="display:none;margin-top:12px">
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px">
      <a href="/logs/download" class="btn btn-grey btn-sm">⬇ Download</a>
      <a href="/logs/wipe" class="btn btn-red btn-sm" onclick="return confirm('Clear all logs?');">🗑 Wipe</a>
      <label style="font-size:0.9em;margin-left:4px">Level:
        <select onchange="setLogLevel(this.value)" style="padding:3px;margin-left:4px">
          <option>DEBUG</option><option selected>INFO</option>
          <option>WARNING</option><option>ERROR</option><option>CRITICAL</option>
        </select>
      </label>
    </div>
    <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:8px;font-size:0.88em">
      <label>Lines:
        <select id="logLines" onchange="fetchLogs()" style="padding:3px;margin-left:4px">
          <option value="50">50</option>
          <option value="100" selected>100</option>
          <option value="200">200</option>
          <option value="500">500</option>
        </select>
      </label>
      <button class="btn btn-grey btn-sm" onclick="fetchLogs()">↺ Refresh</button>
      <label><input type="checkbox" id="autoRefreshLog" onchange="toggleLogAutoRefresh(this)"> Auto-refresh (5s)</label>
      <label><input type="checkbox" id="autoScrollLog" checked> Auto-scroll</label>
    </div>
    <pre id="logViewer" style="height:300px;overflow-y:auto;background:#1a1a2e;color:#a8d8a8;
      padding:10px;border-radius:8px;font-size:0.75em;margin:0;font-family:monospace;
      white-space:pre-wrap;word-break:break-all">Loading…</pre>
  </div>
</div>

<!-- ── Branding ── -->
<div style="text-align:center;color:#888;font-size:0.85em;margin-top:8px;padding-top:12px;border-top:1px solid #dde">
  A Stage Audio Works Innovation
  {% if branding_image %}<br>
    <a href="https://stageaudioworks.com" target="_blank" rel="noopener noreferrer">
      <img src="/branding-image" alt="Stage Audio Works Logo" style="max-height:50px;margin-top:8px">
    </a>
  {% endif %}
</div>
</body>
</html>
"""

# ══════════════════════════════════════════════════════════════════════════════
# FLASK ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template_string(
        HTML_TEMPLATE,
        branding_image=branding_image_exists,
        player_name=_player_name,
    )

@app.route("/branding-image")
def branding_image():
    if branding_image_exists:
        return send_from_directory(
            os.path.dirname(BRANDING_IMAGE_PATH),
            os.path.basename(BRANDING_IMAGE_PATH),
        )
    return ("", 404)

@app.route("/play", methods=["POST"])
def play():
    mpv_set_property("pause", False)
    with state_lock:
        state.paused = False
    return ("", 204)

@app.route("/pause", methods=["POST"])
def pause():
    mpv_set_property("pause", True)
    with state_lock:
        state.paused = True
    return ("", 204)

@app.route("/restart", methods=["POST"])
def restart():
    with state_lock:
        state.restart_in_progress = True
    try:
        path, loop = _desired_video()
        switch_video(path, loop=loop)
    finally:
        with state_lock:
            state.restart_in_progress = False
    return ("", 204)

@app.route("/status")
def status():
    st     = get_mpv_status()
    pos    = st.get("time", 0)
    length = st.get("length", 0)
    with state_lock:
        video           = state.current_trigger_video if state.trigger_running else state.current_video
        override_active = state.override_active
    return jsonify({
        "video":           video,
        "state":           st.get("state", "unknown"),
        "position":        time.strftime("%H:%M:%S", time.gmtime(pos)),
        "remaining":       time.strftime("%H:%M:%S", time.gmtime(max(0, length - pos))),
        "override_active": override_active,
        "volume":          st.get("volume", state.volume),
    })

@app.route("/volume", methods=["GET", "POST"])
def volume():
    if request.method == "GET":
        return jsonify({"volume": get_volume()})
    try:
        data = request.get_json()
        vol  = int(data.get("volume", DEFAULT_VOLUME))
        set_volume(vol)
        return ("", 204)
    except Exception:
        logger.exception("Error in /volume POST")
        return ("Invalid volume", 400)

@app.route("/audio_devices")
def audio_devices():
    """Return the list of available ALSA playback devices for the UI dropdown."""
    return jsonify(list_audio_devices())

@app.route("/audio_device", methods=["GET", "POST"])
def audio_device():
    if request.method == "GET":
        with state_lock:
            return jsonify({"audio_device": state.audio_device})
    # POST — set new device, persist, hot-restart mpv on current video
    try:
        data   = request.get_json()
        device = (data.get("audio_device") or "").strip()
        if not device:
            return ("Missing audio_device", 400)
        with state_lock:
            state.audio_device = device
        save_audio_device(device)
        logger.info("Audio device changed to: %s — restarting mpv", device)
        # Restart mpv so the new --audio-device flag takes effect immediately
        with state_lock:
            state.restart_in_progress = True
        try:
            path, loop = _desired_video()
            _start_mpv(path, loop=loop)
        finally:
            with state_lock:
                state.restart_in_progress = False
        return ("", 204)
    except Exception:
        logger.exception("Error in /audio_device POST")
        return ("Bad request", 400)

@app.route("/display_modes")
def display_modes():
    """
    Modes the attached display advertises, for the UI dropdown.

    An empty "modes" list means sysfs could not be read (usually nothing
    plugged into HDMI), which the UI reports rather than presenting as
    "no modes available".
    """
    with state_lock:
        current = state.resolution
    return jsonify({
        "modes":   list_drm_modes(),
        "current": current,
        "default": RESOLUTION_DEFAULT,
    })

@app.route("/resolution", methods=["GET", "POST"])
def resolution_route():
    """
    GET  — current output mode, plus the refresh rate mpv reports.
    POST — force a mode, persist it, hot-restart mpv.

    Reverts to the display's preferred mode automatically if mpv will not start
    on the requested one, and says so in the response so the UI can tell the
    user instead of silently doing something different.
    """
    if request.method == "GET":
        with state_lock:
            current = state.resolution
        return jsonify({
            "resolution":  current,
            "display_fps": mpv_get_property("display-fps"),
        })
    try:
        data = request.get_json(silent=True) or {}
        mode = str(data.get("resolution", "")).strip()
        if not RESOLUTION_RE.match(mode):
            return ("Invalid resolution", 400)
        ok, active = _apply_resolution(mode)
        return jsonify({
            "resolution":  active,
            "requested":   mode,
            "reverted":    (not ok),
            "display_fps": mpv_get_property("display-fps"),
        })
    except Exception:
        logger.exception("Error in /resolution POST")
        return ("Bad request", 400)

@app.route("/set_override", methods=["POST"])
def set_override():
    try:
        data   = request.get_json()
        active = bool(data.get("active", False))
        with state_lock:
            state.override_active     = active
            config["override_active"] = active
            ov                        = state.override_video
            cfg_snap                  = dict(config)
        save_config(cfg_snap)
        if active:
            logger.info("Override ACTIVATED via web UI — switching to: %s",
                        os.path.basename(ov))
            if os.path.exists(ov):
                switch_video(ov, loop=True)
            else:
                logger.warning("Override video not found: %s", ov)
        else:
            logger.info("Override DEACTIVATED via web UI — returning to loop")
            if os.path.exists(LOOP_VIDEO):
                switch_video(LOOP_VIDEO, loop=True)
            else:
                logger.warning("Loop video not found: %s", LOOP_VIDEO)
        return ("", 204)
    except Exception:
        logger.exception("Error in /set_override")
        return ("Bad request", 400)

@app.route("/upload/<video_type>", methods=["POST"])
@app.route("/upload/<video_type>/<int:trigger_id>", methods=["POST"])
def upload(video_type, trigger_id=None):
    if "file" not in request.files:
        return ("No file", 400)
    f = request.files["file"]
    if f.filename == "" or not allowed_file(f.filename):
        return ("Invalid file", 400)
    if video_type == "loop":
        dest = LOOP_VIDEO
    elif video_type == "override":
        with state_lock:
            dest = state.override_video
    elif video_type == "trigger" and trigger_id in (1, 2, 3):
        dest = TRIGGER_VIDEOS[GPIO_PINS[trigger_id - 1]]
    else:
        return ("Unknown type", 400)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=VIDEO_DIR, suffix=".upload.tmp")
        with os.fdopen(fd, "wb") as tmp_f:
            f.save(tmp_f)
        os.replace(tmp_path, dest)
        tmp_path = None
        logger.info("Uploaded %s → %s", f.filename, dest)
        return redirect(url_for("index"))
    except Exception:
        logger.exception("Upload failed")
        return ("Upload failed", 500)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

@app.errorhandler(413)
def upload_too_large(e):
    limit     = _get_max_upload_bytes()
    limit_str = f"{limit/1048576:.0f} MB" if limit < 1073741824 else f"{limit/1073741824:.1f} GB"
    st        = os.statvfs("/")
    free      = st.f_frsize * st.f_bavail
    free_str  = f"{free/1048576:.0f} MB" if free < 1073741824 else f"{free/1073741824:.1f} GB"
    msg       = f"File too large. Max upload is {limit_str} (free disk: {free_str}, 15% reserved)."
    logger.warning("Upload rejected — file too large. Limit: %s, Free disk: %s", limit_str, free_str)
    return (msg, 413)

@app.route("/download/<video_type>")
@app.route("/download/<video_type>/<int:trigger_id>")
def download(video_type, trigger_id=None):
    if video_type == "loop":
        path = LOOP_VIDEO
    elif video_type == "override":
        with state_lock:
            path = state.override_video
    elif video_type == "trigger" and trigger_id in (1, 2, 3):
        path = TRIGGER_VIDEOS[GPIO_PINS[trigger_id - 1]]
    else:
        return ("Unknown type", 404)
    if os.path.exists(path):
        return send_from_directory(os.path.dirname(path), os.path.basename(path), as_attachment=True)
    return ("File not found", 404)

@app.route("/logs/download")
def logs_download():
    if os.path.exists(LOG_FILE):
        return send_file(LOG_FILE, as_attachment=True)
    return ("No log file", 404)

@app.route("/logs/wipe")
def logs_wipe():
    _close_file_handler()
    try:
        with open(LOG_FILE, "w"):
            pass
        return redirect(url_for("index"))
    except Exception:
        print("Failed wiping logs", file=sys.stderr)
        return ("Failed", 500)
    finally:
        _reopen_file_handler()

@app.route("/logs/set_level", methods=["POST"])
def logs_set_level():
    data  = request.get_json()
    level = (data.get("level") or "INFO").upper()
    if level in LOG_LEVELS:
        logger.setLevel(getattr(logging, level))
        logger.info("Log level set to %s", level)
        return ("", 204)
    return ("Invalid level", 400)

# ══════════════════════════════════════════════════════════════════════════════
# LOGS TAIL ROUTE
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/logs/tail")
def logs_tail():
    """Return the last N lines of the log file plus file size info."""
    try:
        n = min(int(request.args.get("lines", 100)), 1000)
    except ValueError:
        n = 100
    try:
        if not os.path.exists(LOG_FILE):
            return jsonify({"lines": [], "size_bytes": 0, "size_mb": 0.0})
        size = os.path.getsize(LOG_FILE)
        with open(LOG_FILE, errors="replace") as f:
            all_lines = f.readlines()
        tail = [l.rstrip() for l in all_lines[-n:]]
        return jsonify({
            "lines":      tail,
            "size_bytes": size,
            "size_mb":    round(size / 1048576, 2),
            "total_lines": len(all_lines),
        })
    except Exception:
        logger.exception("Error in /logs/tail")
        return jsonify({"lines": [], "size_bytes": 0, "size_mb": 0.0, "total_lines": 0})

# ══════════════════════════════════════════════════════════════════════════════
# SYSTEM INFO + MANAGEMENT ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/system/info")
def system_info():
    """Return system health metrics for the UI dashboard."""
    try:
        return jsonify({
            "cpu_temp":  _get_cpu_temp(),
            "memory":    _get_mem_info(),
            "disk":      _get_disk_info("/"),
            "uptime":    _get_uptime(),
            "hostname":  socket.gethostname(),
        })
    except Exception:
        logger.exception("Error in /system/info")
        return jsonify({"error": "Could not retrieve system info"}), 500

@app.route("/system/max_upload")
def max_upload():
    """Return current max upload limit and free disk space for the UI."""
    try:
        limit    = _get_max_upload_bytes()
        st       = os.statvfs("/")
        free     = st.f_frsize * st.f_bavail
        limit_mb = round(limit / 1048576, 1)
        free_mb  = round(free  / 1048576, 1)
        return jsonify({
            "max_bytes":  limit,
            "max_mb":     limit_mb,
            "max_label":  f"{limit_mb:.0f} MB" if limit_mb < 1024 else f"{limit_mb/1024:.1f} GB",
            "free_mb":    free_mb,
            "free_label": f"{free_mb:.0f} MB" if free_mb < 1024 else f"{free_mb/1024:.1f} GB",
        })
    except Exception:
        logger.exception("Error in /system/max_upload")
        return jsonify({"max_bytes": 0, "max_label": "unknown", "free_label": "unknown"}), 500

@app.route("/system/video_slots")
def video_slots():
    """Return file info for all video slots."""
    try:
        return jsonify(_get_video_slots())
    except Exception:
        logger.exception("Error in /system/video_slots")
        return jsonify({}), 500

@app.route("/system/reboot", methods=["POST"])
def system_reboot():
    """Reboot the Pi.  Requires pi to have NOPASSWD sudo for reboot."""
    logger.warning("Reboot requested via web UI")
    try:
        subprocess.Popen(
            ["sudo", "reboot"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return jsonify({"status": "rebooting"})
    except Exception:
        logger.exception("Reboot failed")
        return ("Reboot failed", 500)

@app.route("/upload/script", methods=["POST"])
def upload_script():
    """
    Validate and deploy an updated player script.
    Validation: UTF-8, Python syntax, SAW_PLAYER_SIGNATURE, required functions.
    On success: backs up current script, writes new one, restarts the service.
    """
    if "file" not in request.files:
        return ("No file", 400)
    f = request.files["file"]
    if not f.filename or not f.filename.endswith(".py"):
        return ("File must be a .py script", 400)
    try:
        content = f.read()
    except Exception:
        return ("Failed to read uploaded file", 500)

    ok, reason = _validate_script(content)
    if not ok:
        logger.warning("Script upload rejected: %s", reason)
        return (reason, 422)

    # Back up current script
    try:
        if os.path.exists(SCRIPT_PATH):
            shutil.copy2(SCRIPT_PATH, SCRIPT_BACKUP)
            logger.info("Current script backed up to %s", SCRIPT_BACKUP)
    except Exception:
        logger.exception("Failed to back up current script")

    # Write new script atomically
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir="/home/pi", suffix=".upload.tmp")
        with os.fdopen(fd, "wb") as tmp_f:
            tmp_f.write(content)
        os.chmod(tmp_path, 0o755)
        os.replace(tmp_path, SCRIPT_PATH)
        tmp_path = None
        logger.info("New script deployed to %s", SCRIPT_PATH)
    except Exception:
        logger.exception("Failed to write new script")
        return ("Failed to write script", 500)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    # Restart the service — runs in background so Flask can return the response
    logger.info("Restarting saw-player service to apply new script")
    try:
        subprocess.Popen(
            ["sudo", "systemctl", "restart", "saw-player"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        logger.exception("Failed to restart service after script update")
        return ("Script deployed but service restart failed — restart manually", 207)

    return jsonify({"status": "ok", "message": "Script deployed — restarting service"})

@app.route("/player_name", methods=["GET", "POST"])
def player_name_route():
    """GET — return current player name.  POST — update and persist it."""
    global _player_name
    if request.method == "GET":
        return jsonify({"player_name": _player_name})
    try:
        data = request.get_json()
        name = str(data.get("player_name", "")).strip()[:64]   # max 64 chars
        _player_name              = name
        config["player_name"]     = name
        save_config(dict(config))
        logger.info("Player name set to: %r", name)
        return ("", 204)
    except Exception:
        logger.exception("Error in /player_name POST")
        return ("Bad request", 400)

# ══════════════════════════════════════════════════════════════════════════════
# WATCHDOGS
# ══════════════════════════════════════════════════════════════════════════════

def mpv_watchdog(interval: float = 5):
    logger.info("mpv watchdog started (interval=%ss)", interval)
    while True:
        try:
            with state_lock:
                restarting = state.restart_in_progress
            if not restarting and not _mpv_is_running():
                logger.warning("Watchdog: mpv is down — restarting")
                with state_lock:
                    state.restart_in_progress = True
                try:
                    path, loop = _desired_video()
                    _start_mpv(path, loop=loop)
                finally:
                    with state_lock:
                        state.restart_in_progress = False
            time.sleep(interval)
        except Exception:
            logger.exception("mpv watchdog error")
            time.sleep(interval)

def log_watchdog(interval: float = LOG_CHECK_INTERVAL):
    logger.info("Log watchdog started")
    max_bytes = MAX_LOG_MB_BEFORE_ROTATE * 1024 * 1024
    while True:
        try:
            if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > max_bytes:
                logger.warning("Log exceeded threshold — rotating")
                rotate_and_compress(LOG_FILE)
            time.sleep(interval)
        except Exception:
            logger.exception("Log watchdog error")
            time.sleep(interval)

# ══════════════════════════════════════════════════════════════════════════════
# STARTUP
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logger.info("Stage Audio Works Video Player v11 starting")

    # Load saved volume, audio device and output resolution into state before
    # mpv starts so _build_mpv_cmd picks them all up.
    with state_lock:
        state.volume       = load_saved_volume()
        state.audio_device = load_saved_audio_device()
        state.resolution   = load_saved_resolution()
        boot_resolution    = state.resolution
    logger.info("Volume set to %s", state.volume)
    logger.info("Audio device set to %s", state.audio_device)
    logger.info("Output resolution set to %s", state.resolution)

    # Start mpv with initial video
    try:
        path, loop = _desired_video()
        if os.path.exists(path):
            if not _start_mpv(path, loop=loop) and boot_resolution != RESOLUTION_DEFAULT:
                # A saved mode this display cannot do would otherwise leave the
                # unit with no picture and the watchdog restarting mpv forever,
                # with no way back short of SSH.  Revert and persist so the
                # next boot is clean too.
                logger.error(
                    "mpv would not start on saved mode %r — reverting to %s",
                    boot_resolution, RESOLUTION_DEFAULT,
                )
                with state_lock:
                    state.resolution = RESOLUTION_DEFAULT
                save_resolution(RESOLUTION_DEFAULT)
                _start_mpv(path, loop=loop)
        else:
            logger.warning("Initial video not found: %s — waiting for upload", path)
    except Exception:
        logger.exception("Failed to start initial video")

    # Background threads
    for _target, _name in [
        (udp_listener,  "udp-listener"),
        (mpv_watchdog,  "mpv-watchdog"),
        (log_watchdog,  "log-watchdog"),
    ]:
        threading.Thread(target=_target, daemon=True, name=_name).start()

    # Flask (blocks until killed)
    try:
        app.run(host="0.0.0.0", port=5000)
    finally:
        # Close the lgpio chip handle on shutdown
        if _gpio_handle is not None:
            try:
                _lgpio.gpiochip_close(_gpio_handle)
            except Exception:
                pass
        _terminate_mpv()
