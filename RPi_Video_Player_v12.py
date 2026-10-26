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
Stage Audio Works Raspberry Pi Video Player  (v12)
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
  • Clips locked into RAM where memory allows — loop first, then override,
    then the triggers — within a budget that always leaves headroom for the
    system. A reliability feature: playback carries on from memory even if the
    SD card turns slow or starts failing. (Reading a clip does not wear the card.)
  • Software update from GitHub: checks the published release, and installs
    it only when it is newer, after verifying its checksum. Never downgrades.
  • AES67 network audio (EXPERIMENTAL, opt-in): on a unit where
    install-aes67.sh has been run, "AES67 network" appears as an audio output.
    mpv then plays into a loopback that the saw-aes67-bridge service keeps
    feeding to the network card, and the player keeps one stream on the
    aes67-daemon - named after the Player Name, on this box's own multicast
    group - matching its settings (stereo or mono, lip-sync offset). On every
    other unit none of it is offered and nothing runs.
  • Bluetooth audio (bluez-alsa): turn Bluetooth on, scan, pair, choose a
    speaker as the audio output, forget it, turn Bluetooth off. The chosen
    speaker reconnects after a reboot, and whenever it drops out the player
    keeps reconnecting - every 5 s for 20 attempts, then once a minute.
  • Web UI — video upload/download/delete, volume, override toggle, log viewer,
    system info, reboot, player name, and in-place script update
  • UDP remote commands
  • Volume persistence across reboots
  • mpv watchdog, log rotation + compression

Note on Bluetooth audio:
  Back in v12 (change 4), rewritten rather than restored: v10's version, in
  archive/RPi_Video_Player_v10.py, was never run on hardware and would have
  quit its pairing session before pairing finished. See the BLUETOOTH AUDIO
  section for what changed and why.

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

Software update endpoints:
  /update/version GET  — installed version
  /update/check   GET  — compare with the published GitHub release
  /update/apply   POST — download, verify and install a newer release

AES67 endpoints (report {"installed": false} on a unit without it):
  /aes67/status   GET  — stream, PTP and bridge state, settings, alerts
  /aes67/settings POST — channels (1 or 2) and lip-sync offset in ms
  /aes67/sdp      GET  — the stream's SDP, as the daemon publishes it
  /aes67/dismiss  POST — acknowledge the grandmaster-changed alert

Bluetooth endpoints (the actions are queued; /bluetooth/status reports them):
  /bluetooth/status    GET  — adapter, paired devices, scan results, link state
  /bluetooth/enable    POST — {"enabled": true|false}
  /bluetooth/scan      POST — look for devices for BT_SCAN_S seconds
  /bluetooth/pair      POST — {"mac": ...}: pair, trust and connect
  /bluetooth/use       POST — {"mac": ...}: make it the audio output
  /bluetooth/forget    POST — {"mac": ...}: unpair
  /bluetooth/reconnect POST — try the audio device now, back to the fast retries
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
import ctypes
import ctypes.util
import errno
import hashlib
import mmap
import queue
try:
    import resource          # Unix only; RAM pinning reports unavailable without it
except ImportError:
    resource = None
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

# ── AES67 network audio (optional — see the AES67 NETWORK AUDIO section) ──────
# Only on units where install-aes67.sh has been run. On any other unit the
# option is not offered and nothing here runs.
AES67_DEVICE         = "aes67"     # the Audio Output value meaning "AES67 network"
AES67_INSTALLED      = "/etc/saw-aes67/installed"     # written by install-aes67.sh
AES67_BRIDGE_BIN     = "/usr/local/bin/saw-aes67-bridge"
AES67_BRIDGE_STATUS  = "/run/saw-aes67/bridge.json"   # the bridge rewrites it each second
AES67_CHANNELS_FILE  = "/var/lib/saw-aes67/channels"  # the player writes, the bridge reads
AES67_DAEMON_CONF    = "/etc/daemon.conf"
AES67_LOOP_DEVICE    = "alsa/plughw:CARD=SAWLoop,DEV=0"   # mpv's end of the loopback
AES67_DEFAULT_PORT   = 8080        # the daemon's REST port if nothing records another
AES67_SOURCE_ID      = 0           # the one stream on the daemon this player owns
AES67_RECONCILE_S    = 4           # how often the stream is checked against the settings
AES67_LATENCY_MS     = 92          # bridge buffer 60 ms + card buffer 32 ms, until it reports
AES67_OFFSET_MAX_MS  = 1000        # lip-sync offset limit, either way
# Installing it from the web UI - see AES67 INSTALL FROM THE WEB UI.
AES67_WEBINSTALL     = "/usr/local/sbin/saw-aes67-webinstall"   # root wrapper, from install.sh
AES67_INSTALL_LOG    = "/var/log/saw-aes67-install.log"
AES67_INSTALL_RESULT = "/var/log/saw-aes67-install.result"      # "running", then an exit code
AES67_INSTALL_UNIT   = "saw-aes67-install"
AES67_KEEP_MIN_MB    = 3072        # below this the install always goes dark (the wrapper agrees)
AES67_MIN_RAM_MB     = 1800        # a "2 GB" Pi reports about 2000
AES67_INSTALL_DISK_MB = 5120       # build tree plus the installer's temporary swap

# ── Bluetooth audio (see the BLUETOOTH AUDIO section) ─────────────────────────
BT_CTL               = ["bluetoothctl"]   # a list, so the tests can put a fake in its place
BT_SCAN_S            = 15        # how long a scan listens for devices
BT_PAIR_TIMEOUT_S    = 40        # pairing, including the device answering
BT_CONNECT_TIMEOUT_S = 25        # one connection attempt
BT_MONITOR_S         = 5         # how often a connected device's link is checked
BT_RETRY_FAST_S      = 5         # gap between reconnect attempts at first...
BT_RETRY_FAST_N      = 20        # ...for this many failed attempts...
BT_RETRY_SLOW_S      = 60        # ...then once a minute, for as long as it takes
BT_REFRESH_S         = 30        # adapter, paired devices and services re-read
BT_BOOT_GRACE_S      = 10        # let bluetoothd and bluealsa start before acting

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

# -- RAM pinning (see the RAM PINNING section) --------------------------------
# Clips are locked into RAM where memory allows: loop first, then override,
# then the triggers. The budget always leaves the larger of these two free for
# the system, mpv's decode buffers and uploads.
RAM_PIN_ENABLED          = True
RAM_PIN_RESERVE_MIN_MB   = 512    # never lock RAM below this much headroom...
RAM_PIN_RESERVE_FRACTION = 0.25   # ...or below this share of total RAM, if larger
RAM_PIN_STARTUP_DELAY_S  = 10     # let mpv allocate its buffers before budgeting

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

# Integer release number. Must match the filename (RPi_Video_Player_vNN.py) and
# the docstring title - tools/validate_player.py checks that all three agree,
# and the GitHub update compares against it. Deliberately NOT in
# REQUIRED_FUNCTIONS: older scripts do not have it, and listing it there would
# stop a manual rollback to them through the web UI.
PLAYER_VERSION = 12

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

# ── Software update from GitHub (see SOFTWARE UPDATE FROM GITHUB) ─────────────
UPDATE_REPO       = "stageaudioworks/pi-video-player-dist"
UPDATE_BRANCH     = "main"
UPDATE_MANIFEST   = "release.json"
UPDATE_TIMEOUT    = (6, 30)               # seconds: connect, read
UPDATE_MAX_SCRIPT = 4 * 1024 * 1024       # refuse a download larger than this
SERVICE_INSTALLED = "/etc/systemd/system/saw-player.service"
# Test hook only: a base URL serving the same files, e.g. a staging copy. Set it
# with a systemd drop-in (root only) - it is never taken from a web request.
# Setting it also disables the GitHub API fallback.
UPDATE_BASE_OVERRIDE = os.environ.get("SAW_UPDATE_BASE", "").strip()

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

# Flask's development server (werkzeug) logs every HTTP request at INFO. An open
# browser tab polls about 1.5 times a second, so a dashboard left open on a desk
# writes ~130,000 lines a day to the journal - by far the largest continuous
# write this player makes to the SD card. Keep werkzeug's warnings and errors,
# drop the per-request access lines. This is a separate logger from saw_player's,
# so the web UI's log-level selector does not bring the access lines back.
logging.getLogger("werkzeug").setLevel(logging.WARNING)

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
    """
    Write config to disk.  Always call OUTSIDE state_lock.

    Temp file + os.replace: opening the real file with "w" truncates it at
    once, so a failed write would lose the override state, the player name and
    the AES67 settings together.
    """
    try:
        directory = os.path.dirname(CONFIG_FILE) or "."
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".config.json.")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(cfg, f)
            os.replace(tmp, CONFIG_FILE)
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise
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

                # Build the mpv-compatible device string. The AES67 card and
                # its loopback are never offered as raw devices: the bridge
                # holds the card, and the loopback without the bridge's stream
                # management is silence. "AES67 network" below is the way in.
                if name.startswith(("plughw:", "hw:")) and not _aes67_card_name(name):
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

    if aes67_installed():
        devices.append({
            "device": AES67_DEVICE,
            "label":  f"AES67 network  —  stream \"{aes67_stream_name(_player_name, socket.gethostname())}\"",
        })

    # Paired Bluetooth speakers, from what the Bluetooth manager last saw -
    # this never waits on Bluetooth. The current output is listed even if it
    # has dropped off that list, so the selection always shows.
    if bt_enabled():
        with state_lock:
            current = state.audio_device
        listed = set()
        for d in bt_manager.paired_audio():
            dev = bt_device_string(d["mac"])
            listed.add(dev)
            devices.append({"device": dev, "label": "Bluetooth  —  %s (%s)" % (
                d["name"], "connected" if d.get("connected") else "not connected")})
        if bt_mac_from_device(current) and current not in listed:
            devices.append({"device": current, "label": "Bluetooth  —  %s" % bt_mac_from_device(current)})
    return devices

def _aes67_card_name(alsa_name: str) -> bool:
    """True for an 'aplay -L' name on the AES67 card or its loopback, matched
    on the whole CARD= value so a card merely containing the word is kept."""
    m = re.search(r"CARD=([^,]+)", alsa_name)
    return bool(m) and m.group(1) in ("RAVENNA", "SAWLoop")

# ══════════════════════════════════════════════════════════════════════════════
# AES67 NETWORK AUDIO  (EXPERIMENTAL — only on units with install-aes67.sh)
# ══════════════════════════════════════════════════════════════════════════════
#
#   mpv -> SAWLoop loopback -> saw-aes67-bridge -> RAVENNA card -> network
#
# The bridge (aes67-bridge/ in the repo) owns the card for good and keeps it
# fed, so receivers see one continuous stream through every clip change. This
# player owns the stream itself - source 0 on the aes67-daemon - and
# Aes67Manager keeps it matching the settings every few seconds. One owner per
# stream, as the aes67-integration skill insists: nothing else here touches the
# daemon, and the manager touches nothing on it but source 0.
#
# Rules from the skill that this code keeps:
#   * The daemon defaults nothing: every key of a source is sent, every time.
#   * Every PUT restarts the stream, so one is sent only when something differs.
#   * refclk_ptp_traceable is false - Q-SYS and Dante reject "traceable" as a
#     grandmaster mismatch.
#   * The stream is not created before PTP locks: its SDP names the grandmaster.
#   * A grandmaster change means receivers set up by hand from the SDP must
#     import it again, so the web UI says so until somebody dismisses it.
#   * The width comes from the setting, never from the clip: receivers drop or
#     have to re-learn a stream that changes width.
# Nothing here is needed for playback. If the daemon or the bridge is down the
# video plays on; the web UI says what is wrong.
# ══════════════════════════════════════════════════════════════════════════════

def aes67_installed() -> bool:
    """install-aes67.sh has run here (it writes the marker last) and built the bridge."""
    return os.path.exists(AES67_INSTALLED) and os.path.exists(AES67_BRIDGE_BIN)

def aes67_clean_settings(raw) -> dict:
    """Valid settings from whatever was stored; anything else is the default.
    bool is checked for because True == 1 in Python."""
    out = {"channels": 2, "offset_ms": 0}
    if isinstance(raw, dict):
        ch = raw.get("channels")
        if not isinstance(ch, bool) and ch in (1, 2):
            out["channels"] = ch
        off = raw.get("offset_ms")
        if isinstance(off, int) and not isinstance(off, bool) and abs(off) <= AES67_OFFSET_MAX_MS:
            out["offset_ms"] = off
    return out

def aes67_settings() -> dict:
    return aes67_clean_settings(config.get("aes67"))

def aes67_http_port(installed_file: Optional[str] = None,
                    conf_file: Optional[str] = None) -> int:
    """The daemon's REST port: what install-aes67.sh recorded (it moves off
    8080 if something else holds it), else the daemon's own config, else 8080.
    Paths default to the module settings, read at call time."""
    installed_file = installed_file or AES67_INSTALLED
    conf_file = conf_file or AES67_DAEMON_CONF
    try:
        with open(installed_file) as f:
            for line in f:
                if line.startswith("HTTP_PORT="):
                    port = int(line.split("=", 1)[1].strip())
                    if 0 < port < 65536:
                        return port
    except Exception:
        pass
    try:
        with open(conf_file) as f:
            port = json.load(f).get("http_port")
        if isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536:
            return port
    except Exception:
        pass
    return AES67_DEFAULT_PORT

def aes67_rtp_port(conf_file: Optional[str] = None) -> int:
    """The RTP port the stream is sent on, for display. 5004 is AES67's usual."""
    try:
        with open(conf_file or AES67_DAEMON_CONF) as f:
            port = json.load(f).get("rtp_port")
        if isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536:
            return port
    except Exception:
        pass
    return 5004

def aes67_box_address(conf_file: Optional[str] = None,
                      sysfs_net: str = "/sys/class/net") -> str:
    """
    This box's own multicast group: 239.1.<MAC byte 5>.<MAC byte 6> of the
    interface the daemon sends on, with ..:00:01 moved to 239.1.0.2 so no box
    lands on the daemon's sample group. The same rule as install-aes67.sh's
    box_mcast_address and its first-boot identity check, so on a cloned SD card
    the player and the daemon agree. "" when it cannot be worked out - the
    daemon then uses its own rtp_mcast_base, which the installer set per box.
    """
    try:
        with open(conf_file or AES67_DAEMON_CONF) as f:
            iface = json.load(f).get("interface_name") or ""
        if not isinstance(iface, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", iface):
            return ""
        with open(os.path.join(sysfs_net, iface, "address")) as f:
            mac = f.read().strip()
    except Exception:
        return ""
    m = re.fullmatch(r"(?:[0-9a-fA-F]{2}:){4}([0-9a-fA-F]{2}):([0-9a-fA-F]{2})", mac)
    if not m:
        return ""
    b5, b6 = int(m.group(1), 16), int(m.group(2), 16)
    if b5 == 0 and b6 == 1:
        b6 = 2
    return "239.1.%d.%d" % (b5, b6)

def aes67_stream_name(player_name: str, hostname: str) -> str:
    """What receivers list the stream as - the Player Name, else the hostname.
    Plain characters only: it goes into the SDP and SAP announcements."""
    raw = (player_name or "").strip() or ("SAW Player " + (hostname or "")).strip()
    clean = re.sub(r"[^A-Za-z0-9 _.()-]", "", raw)
    clean = re.sub(r"\s+", " ", clean).strip()[:40].strip()
    return clean or "SAW Player"

def aes67_source_body(name: str, channels: int, address: str, enabled: bool) -> dict:
    """
    Source 0 exactly as the daemon reads it: it defaults nothing, so a missing
    key is an HTTP 400. L24 is payload type 98, 48 samples (1 ms) a packet.
    The map names the card channels the bridge writes: 0-1 for stereo, 0 for
    the mono mix.
    """
    return {
        "id": AES67_SOURCE_ID,
        "enabled": bool(enabled),
        "name": name,
        "io": "Audio Device",
        "max_samples_per_packet": 48,
        "codec": "L24",
        "address": address,
        "ttl": 15,
        "payload_type": 98,
        "dscp": 34,
        "refclk_ptp_traceable": False,
        "map": list(range(channels)),
    }

_AES67_COMPARED = ("enabled", "name", "io", "max_samples_per_packet", "codec", "ttl",
                   "payload_type", "dscp", "refclk_ptp_traceable", "map")

def aes67_source_differs(have, want: dict) -> bool:
    """Whether a PUT is needed. The address is compared only when one was asked
    for: an empty one is the daemon's to choose, and it echoes back its choice."""
    if not isinstance(have, dict):
        return True
    if any(have.get(k) != want.get(k) for k in _AES67_COMPARED):
        return True
    return bool(want.get("address")) and have.get("address") != want["address"]

def aes67_plan(selected: bool, ptp_locked: bool, have, want: dict):
    """
    The body to PUT on source 0, or None for nothing to do.

    AES67 is the audio output: the stream must exist, be on, and match - once
    PTP is locked, and only if it differs. Another output is chosen: an
    existing stream that is on is switched off - receivers then hear nothing
    rather than a stream of silence - keeping the rest of it as it is.
    """
    if selected:
        if not ptp_locked:
            return None
        return want if aes67_source_differs(have, want) else None
    if isinstance(have, dict) and have.get("enabled"):
        off = dict(want)
        off.update({k: have[k] for k in want if k in have})
        off["enabled"] = False
        return off
    return None

def aes67_read_bridge(path: Optional[str] = None, now=None, stale_s: float = 10) -> dict:
    """The bridge's status file, or {} if missing or unreadable. "stale" is
    added: true when the bridge has not rewritten it lately - stopped or hung."""
    try:
        with open(path or AES67_BRIDGE_STATUS) as f:
            d = json.load(f)
    except Exception:
        return {}
    if not isinstance(d, dict):
        return {}
    upd = d.get("updated")
    now = time.time() if now is None else now
    d["stale"] = isinstance(upd, bool) or not isinstance(upd, (int, float)) or now - upd > stale_s
    return d

def aes67_audio_delay(bridge: dict, offset_ms: int) -> float:
    """
    mpv's --audio-delay in seconds. Negative, so the picture waits for the
    audio's trip through the bridge's buffer and the card's - which the bridge
    reports, about 92 ms - plus the installer's offset for the receiver and the
    display (positive = audio later).
    """
    lat = bridge.get("latency_ms") if isinstance(bridge, dict) else None
    if (isinstance(lat, bool) or not isinstance(lat, (int, float)) or not 20 <= lat <= 1000
            or bridge.get("stale", True)):
        lat = AES67_LATENCY_MS
    return round((offset_ms - lat) / 1000.0, 3)

def aes67_write_channels(path: str, channels: int) -> bool:
    """The setting the bridge re-reads each second. Written only when it
    changes - it is on the SD card - atomically, and readable by the bridge's
    own user (mkstemp makes files 0600)."""
    want = "%d\n" % channels
    try:
        with open(path) as f:
            if f.read() == want:
                return False
    except FileNotFoundError:
        pass
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".channels.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(want)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise
    return True

def aes67_gm_change(last: str, ptp: dict):
    """(grandmaster to remember, the previous one if it just changed, else
    None). Only a locked, non-zero id counts: unlocked for a moment between two
    readings of the same master is not a change."""
    gm = ptp.get("gmid") or ""
    if not ptp.get("locked") or not gm.strip("0-:"):
        return last, None
    if last and gm != last:
        return gm, last
    return gm, None

def aes67_view(snap: dict) -> dict:
    """
    What the web UI shows, from one reconcile pass: a headline (level ok /
    warn / err / off, and a sentence that says what to do), alerts, and rows
    of detail. The headline checks run in the order faults have to be fixed:
    a missing bridge first, PTP before the stream, the stream before the card.
    """
    b = snap.get("bridge") or {}
    ptp = snap.get("ptp") or {}
    src = snap.get("source")
    settings = snap.get("settings") or {"channels": 2, "offset_ms": 0}
    card = b.get("card") if isinstance(b.get("card"), dict) else {}
    mode = "mono (channel 1)" if settings["channels"] == 1 else "stereo (channels 1-2)"
    address = (src or {}).get("address") or snap.get("address") or "the daemon's group"
    where = "%s port %s" % (address, snap.get("rtp_port", 5004))

    def head(level, text):
        return {"level": level, "text": text}

    if not snap.get("selected"):
        h = head("off", "Not in use. Choose \"AES67 network\" under Audio Output to send "
                        "the player's audio to the network.")
    elif not b or b.get("stale"):
        h = head("err", "The AES67 bridge is not running, so nothing reaches the network. "
                        "On the Pi: sudo systemctl status saw-aes67-bridge")
    elif not snap.get("daemon_reachable"):
        h = head("err", "The AES67 daemon is not answering. On the Pi: "
                        "sudo systemctl status aes67-daemon")
    elif not ptp.get("locked"):
        gm = ptp.get("gmid") or ""
        if not gm.strip("0-:"):
            h = head("warn", "Waiting for PTP: no grandmaster heard. The Q-SYS Core (or other "
                             "grandmaster) must be on this network and in the same PTP domain.")
        else:
            h = head("warn", "Waiting for PTP to lock (the daemon says \"%s\")."
                             % (ptp.get("status") or "?"))
    elif snap.get("error"):
        h = head("err", "The AES67 stream could not be set up: %s" % snap["error"])
    elif not isinstance(src, dict) or not src.get("enabled"):
        h = head("warn", "Setting up the stream...")
    elif not card.get("open"):
        if b.get("open_failures"):
            h = head("err", "The AES67 card cannot be opened. After a kernel update, run "
                            "install-aes67.sh again.")
        else:
            h = head("warn", "Opening the AES67 card...")
    elif b.get("state") != "running":
        h = head("warn", "Starting the audio (%s)..." % (b.get("state") or "?"))
    else:
        h = head("ok", "On air: \"%s\" to %s, %s, L24 48 kHz." % (snap.get("name"), where, mode))

    alerts = []
    gm = snap.get("gm_alert")
    if gm:
        alerts.append({
            "text": "PTP grandmaster changed from %s to %s at %s. Receivers set up by hand "
                    "from this stream's SDP must import it again; Q-SYS in Auto mode picks "
                    "it up from the SAP announcement." % (gm["from"], gm["to"], gm["at"]),
            "dismiss": "gm",
        })
    if b.get("clamp_alarm") and not b.get("stale"):
        alerts.append({"text": "The Pi's clock and the network's PTP clock are further apart than "
                               "the bridge can correct (500 ppm), so audio is dropped every few "
                               "minutes. Check the grandmaster, and NTP on this Pi."})

    rows = [["Stream", "\"%s\", %s" % (snap.get("name"), mode)],
            ["Address", where],
            ["PTP", "%s%s" % (ptp.get("status") or "unknown",
                              (", grandmaster " + ptp["gmid"]) if ptp.get("gmid") else "")]]
    if b and not b.get("stale"):
        rows.append(["Bridge", "%s; card %s" % (
            b.get("state", "?"),
            "%s ch %s" % (card.get("channels"), card.get("format")) if card.get("open") else "closed")])
        if b.get("state") == "running":
            rows.append(["Clock", "%+.1f ppm against PTP; %.0f ms buffered (target %s ms)" % (
                b.get("ppm", 0.0), b.get("level_ms", 0.0), b.get("target_ms", "?"))])
        rows.append(["Since start", "%s underruns, %s card xruns, %s stalls, %s reopens" % (
            b.get("underruns", 0), b.get("xruns", 0), b.get("stalls", 0), b.get("reopens", 0))])
    if snap.get("selected"):
        rows.append(["Lip-sync", "picture held back %d ms (offset %+d ms)" % (
            round(-aes67_audio_delay(b, settings["offset_ms"]) * 1000), settings["offset_ms"])])
    return {"headline": h, "alerts": alerts, "rows": rows}

class Aes67Manager:
    """
    Keeps source 0 on the daemon matching the settings, every
    AES67_RECONCILE_S seconds or at once when kick()ed. Caches what it found
    for /aes67/status, so the web UI's polling never waits on the daemon.
    Logs only when something happens: it runs for the life of the unit.
    """

    def __init__(self, port=None, bridge_status=None, channels_file=None,
                 conf_file=None, sysfs_net="/sys/class/net"):
        self.port          = port          # None: read from the install when first needed
        self.bridge_status = bridge_status or AES67_BRIDGE_STATUS
        self.channels_file = channels_file or AES67_CHANNELS_FILE
        self.conf_file     = conf_file or AES67_DAEMON_CONF
        self.sysfs_net     = sysfs_net
        self._lock         = threading.Lock()
        self._wake         = threading.Event()
        self._snap         = None
        self._last_gmid    = ""
        self._gm_alert     = None
        self._last_error   = ""
        self._thread       = None

    def _url(self, path: str) -> str:
        return "http://127.0.0.1:%d/api%s" % (self.port, path)

    def _get_json(self, path: str):
        r = requests.get(self._url(path), timeout=(1, 3))
        r.raise_for_status()
        return r.json()

    def reconcile_once(self, selected: bool, settings: dict, name: str) -> dict:
        if self.port is None:
            self.port = aes67_http_port(conf_file=self.conf_file)
        error = ""
        try:
            if aes67_write_channels(self.channels_file, settings["channels"]):
                logger.info("AES67: output set to %s", "mono" if settings["channels"] == 1 else "stereo")
        except Exception as e:
            error = "cannot write %s (%s)" % (self.channels_file, e)

        reachable, ptp, source = False, {"status": "", "gmid": "", "locked": False}, None
        try:
            p = self._get_json("/ptp/status")
            reachable = True
            if isinstance(p, dict):
                ptp["status"] = re.sub(r"[^A-Za-z0-9 _-]", "", str(p.get("status", "")))[:24]
                ptp["gmid"] = re.sub(r"[^0-9A-Fa-f:-]", "", str(p.get("gmid", "")))[:40]
                ptp["locked"] = ptp["status"].lower() == "locked"
            listing = self._get_json("/sources")
            for s in (listing.get("sources") or []) if isinstance(listing, dict) else []:
                if isinstance(s, dict) and s.get("id") == AES67_SOURCE_ID:
                    source = s
                    break
        except Exception:
            pass

        address = aes67_box_address(self.conf_file, self.sysfs_net)
        want = aes67_source_body(name, settings["channels"], address, True)
        plan = aes67_plan(selected, ptp["locked"], source, want) if reachable else None
        if plan is not None:
            what = "created" if source is None else ("switched off" if not plan["enabled"] else "updated")
            try:
                r = requests.put(self._url("/source/%d" % AES67_SOURCE_ID), json=plan, timeout=(1, 5))
                if r.status_code >= 300:
                    raise RuntimeError("HTTP %d %s" % (r.status_code, (r.text or "").strip()[:160]))
                logger.info("AES67 stream %s: \"%s\" to %s, %d channel(s)", what, plan["name"],
                            plan["address"] or "the daemon's group", len(plan["map"]))
                source = plan
            except Exception as e:
                error = error or "the daemon would not take the stream (%s)" % e

        self._last_gmid, previous = aes67_gm_change(self._last_gmid, ptp)
        if previous:
            self._gm_alert = {"from": previous, "to": ptp["gmid"],
                              "at": time.strftime("%Y-%m-%d %H:%M:%S")}
            logger.warning("AES67: PTP grandmaster changed from %s to %s - receivers set up "
                           "from the SDP by hand must import it again", previous, ptp["gmid"])
        if error != self._last_error:
            if error:
                logger.warning("AES67: %s", error)
            self._last_error = error

        snap = {
            "selected": selected, "settings": dict(settings), "name": name,
            "address": address, "rtp_port": aes67_rtp_port(self.conf_file),
            "bridge": aes67_read_bridge(self.bridge_status),
            "daemon_reachable": reachable, "ptp": ptp, "source": source,
            "error": error, "gm_alert": self._gm_alert,
        }
        with self._lock:
            self._snap = snap
        return snap

    def status(self) -> dict:
        with self._lock:
            snap = self._snap
            gm_alert = self._gm_alert
        if snap is None:
            return {"installed": True, "settings": aes67_settings(),
                    "headline": {"level": "warn", "text": "Checking the AES67 stream..."},
                    "alerts": [], "rows": []}
        snap = dict(snap, gm_alert=gm_alert)
        out = aes67_view(snap)
        out.update({"installed": True, "settings": snap["settings"], "selected": snap["selected"]})
        return out

    def dismiss_gm_alert(self):
        with self._lock:
            self._gm_alert = None

    def fetch_sdp(self) -> Tuple[bool, str]:
        if self.port is None:
            self.port = aes67_http_port(conf_file=self.conf_file)
        try:
            r = requests.get(self._url("/source/sdp/%d" % AES67_SOURCE_ID), timeout=(1, 3))
            if r.status_code != 200:
                return False, "No stream yet (the daemon answered HTTP %d)." % r.status_code
            return True, r.text
        except Exception as e:
            return False, "The AES67 daemon is not answering (%s)." % e.__class__.__name__

    def kick(self):
        self._wake.set()

    def _run(self):
        logger.info("AES67 manager started (daemon REST port %d)", aes67_http_port(conf_file=self.conf_file))
        last_exc = ""
        while True:
            try:
                with state_lock:
                    selected = state.audio_device == AES67_DEVICE
                self.reconcile_once(selected, aes67_settings(),
                                    aes67_stream_name(_player_name, socket.gethostname()))
                last_exc = ""
            except Exception as e:
                if repr(e) != last_exc:     # once per distinct fault, not every 4 s
                    logger.exception("AES67 manager error")
                    last_exc = repr(e)
            self._wake.wait(AES67_RECONCILE_S)
            self._wake.clear()

    def start(self) -> bool:
        """Starts the manager thread on a unit with AES67 installed; does
        nothing anywhere else."""
        if self._thread is not None or not aes67_installed():
            return False
        self._thread = threading.Thread(target=self._run, daemon=True, name="aes67-manager")
        self._thread.start()
        return True

aes67_manager = Aes67Manager()

# ══════════════════════════════════════════════════════════════════════════════
# AES67 INSTALL FROM THE WEB UI
# ══════════════════════════════════════════════════════════════════════════════
#
# The button starts install-aes67.sh through saw-aes67-webinstall, a root-owned
# wrapper that install.sh puts in place with a sudo rule for exactly
# "start keep" and "start dark" (aes67-webinstall.sh says why it is built that
# way). The build runs as its own systemd unit, so it outlives the player.
#
#   keep - 3 GB of RAM or more: the picture and this page keep running. The
#          player first releases its RAM-pinned clips to make room, and pins
#          them again when the build ends. The web UI warns that the screen
#          could still go dark: the build pushes the Pi hard.
#   dark - less than 3 GB: the installer stops the player for the build (20-40
#          minutes) and starts it again at the end, whatever happens.
#
# The checks before the button contact GitHub, so they run only when asked
# ("Check this unit"), never on a page load - many units sit on networks where
# that request would just hang.
# ══════════════════════════════════════════════════════════════════════════════

def _mem_total_mb(meminfo: str = "/proc/meminfo") -> int:
    try:
        with open(meminfo) as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 0

def aes67_install_mode(mem_total_mb: int) -> str:
    """keep with 3 GB or more; dark otherwise, and when the size is unknown."""
    return "keep" if mem_total_mb >= AES67_KEEP_MIN_MB else "dark"

def _kernel_version(release: str):
    """(major, minor) from a kernel release such as '6.12.47+rpt-rpi-2712'."""
    m = re.match(r"(\d+)\.(\d+)", release or "")
    return (int(m.group(1)), int(m.group(2))) if m else None

def aes67_install_checks(release: str, mem_total_mb: int, free_mb: int, wired, internet,
                         installed: bool = False) -> list:
    """
    Whether this unit can build AES67, as rows of {"name", "level", "text"}:
    level "ok", "warn", or "fail" - any fail and the button stays off. On a unit
    that already has it, a re-run usually rebuilds only the bridge, so no
    internet and little disk are warnings there rather than failures.
    """
    soft = "warn" if installed else "fail"
    rows = []
    if internet:
        rows.append({"name": "Internet", "level": "ok", "text": "GitHub can be reached"})
    else:
        rows.append({"name": "Internet", "level": soft,
                     "text": "GitHub cannot be reached. The build downloads its sources: build on a unit "
                             "with internet access and clone its SD card."})
    kv = _kernel_version(release)
    if kv == (6, 12):
        rows.append({"name": "Kernel", "level": "ok", "text": "%s (the proven version)" % release})
    elif kv and kv >= (6, 15):
        rows.append({"name": "Kernel", "level": "warn",
                     "text": "%s: 6.15 and later send AES67 audio in bursts that receivers with small "
                             "buffers stutter on. 6.12 is the proven version." % release})
    else:
        rows.append({"name": "Kernel", "level": "warn",
                     "text": "%s: not a tested version (6.12 is proven)" % (release or "unknown")})
    if mem_total_mb >= AES67_MIN_RAM_MB:
        rows.append({"name": "Memory", "level": "ok", "text": "%d MB%s" % (
            mem_total_mb, "" if mem_total_mb >= AES67_KEEP_MIN_MB else
            " - under 3 GB, so the player is stopped while it builds")})
    else:
        rows.append({"name": "Memory", "level": "fail",
                     "text": "%d MB: AES67 needs a Pi with 2 GB or more" % mem_total_mb})
    if free_mb >= AES67_INSTALL_DISK_MB:
        rows.append({"name": "Disk", "level": "ok", "text": "%d MB free" % free_mb})
    else:
        rows.append({"name": "Disk", "level": soft,
                     "text": "%d MB free: the build needs about %d MB" % (free_mb, AES67_INSTALL_DISK_MB)})
    if wired is None:
        rows.append({"name": "Network", "level": "warn", "text": "could not tell which interface is in use"})
    elif wired:
        rows.append({"name": "Network", "level": "ok", "text": "wired Ethernet"})
    else:
        rows.append({"name": "Network", "level": "warn",
                     "text": "not on wired Ethernet: AES67 and PTP need a cable"})
    return rows

def _default_route_wired(route: str = "/proc/net/route", sysfs_net: str = "/sys/class/net"):
    """True if the default route is on a wired interface, False if wireless,
    None if there is no default route or it cannot be read."""
    try:
        with open(route) as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) > 2 and parts[1] == "00000000":
                    return not os.path.exists(os.path.join(sysfs_net, parts[0], "wireless"))
    except Exception:
        pass
    return None

def _aes67_install_inputs() -> tuple:
    try:
        free_mb = shutil.disk_usage("/var/tmp").free // (1024 * 1024)
    except Exception:
        free_mb = 0
    try:
        internet = requests.head("https://github.com", timeout=(4, 6),
                                 allow_redirects=True).status_code < 500
    except Exception:
        internet = False
    return (os.uname().release, _mem_total_mb(), free_mb, _default_route_wired(), internet)

def _aes67_install_active():
    """Whether the install's systemd unit is running; None if it cannot be asked."""
    try:
        return subprocess.run(["systemctl", "is-active", "--quiet", AES67_INSTALL_UNIT],
                              timeout=5).returncode == 0
    except Exception:
        return None

def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)

def aes67_install_state(result_file: Optional[str] = None, log_file: Optional[str] = None,
                        active=None, tail_lines: int = 40) -> dict:
    """
    The last install from the web UI: state never / running / done / failed /
    interrupted, its exit code, when it started and in which mode (run_mode -
    not "mode", which the install panel uses for the mode the NEXT run would
    use), and the end of its log.
    "running" with the unit no longer active means it was cut off - a power
    cut mid-build, for instance - and is reported as interrupted.
    """
    result_file = result_file or AES67_INSTALL_RESULT
    log_file = log_file or AES67_INSTALL_LOG
    out = {"state": "never", "exit_code": None, "started_at": None, "run_mode": None, "log": ""}
    try:
        with open(result_file) as f:
            word = f.read().strip()
    except FileNotFoundError:
        return out
    except Exception:
        word = ""
    if word == "running":
        out["state"] = "interrupted" if active is False else "running"
    elif re.fullmatch(r"\d{1,3}", word):
        out["exit_code"] = int(word)
        out["state"] = "done" if word == "0" else "failed"
    else:
        out["state"] = "failed"
    try:
        with open(log_file, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 65536))
            text = f.read().decode("utf-8", errors="replace")
        lines = [l.rstrip() for l in _strip_ansi(text).replace("\r", "\n").split("\n") if l.strip()]
        out["log"] = "\n".join(lines[-tail_lines:])
        try:
            with open(log_file) as f:
                first = f.readline()
            m = re.search(r"started from the web UI, ([0-9: -]+), mode: (\w+)", first)
            if m:
                out["started_at"], out["run_mode"] = m.group(1).strip(), m.group(2)
        except Exception:
            pass
    except Exception:
        pass
    return out

_aes67_watch_lock = threading.Lock()
_aes67_watching = False

def _aes67_install_watch():
    """
    Follows a "keep" install to its end, then puts back what was moved aside
    for it: the RAM-pinned clips, and the AES67 manager if it is now installed.
    (A "dark" install restarts the player, which does both at startup.)
    """
    global _aes67_watching
    with _aes67_watch_lock:
        if _aes67_watching:
            return
        _aes67_watching = True
    try:
        while True:
            time.sleep(5)
            st = aes67_install_state(active=_aes67_install_active())
            if st["state"] != "running":
                break
        logger.info("AES67 install from the web UI ended: %s%s", st["state"],
                    "" if st["exit_code"] is None else " (exit %d)" % st["exit_code"])
        ram_pinner.release()
        if aes67_installed():
            aes67_manager.start()
    finally:
        with _aes67_watch_lock:
            _aes67_watching = False

def aes67_install_start() -> Tuple[bool, str, str]:
    """Start the install. (ok, message, mode). The wrapper has the final say on
    the mode, and its answer is what is reported."""
    if not os.path.exists(AES67_WEBINSTALL):
        return False, ("Installing from here is not set up on this unit yet - run the one-line "
                       "installer again, then reload this page."), ""
    mode = aes67_install_mode(_mem_total_mb())
    if mode == "keep":
        ram_pinner.hold("the AES67 install")    # free the memory before the build wants it
    try:
        r = subprocess.run(["sudo", "-n", AES67_WEBINSTALL, "start", mode],
                           capture_output=True, text=True, timeout=30)
    except Exception as e:
        r = None
        err = str(e)
    if r is None or r.returncode != 0:
        if mode == "keep":
            ram_pinner.release()
        why = err if r is None else ((r.stderr or r.stdout or "").strip() or "exit %d" % r.returncode)
        logger.error("AES67 install could not start: %s", why)
        return False, "The install could not start: %s" % why[:300], mode
    used = (r.stdout or "").strip() or mode
    if used != mode and mode == "keep":
        ram_pinner.release()                     # the wrapper chose dark after all
    logger.warning("AES67 install started from the web UI (%s)", used)
    if used == "keep":
        threading.Thread(target=_aes67_install_watch, daemon=True, name="aes67-install-watch").start()
    return True, "started", used

# ══════════════════════════════════════════════════════════════════════════════
# BLUETOOTH AUDIO  (BlueZ + bluez-alsa)
# ══════════════════════════════════════════════════════════════════════════════
#
#   mpv -> alsa/bluealsa:DEV=<MAC>,PROFILE=a2dp -> bluealsa -> BlueZ -> speaker
#
# bluez-alsa makes each connected A2DP speaker an ALSA device, so to mpv a
# Bluetooth speaker is just another --audio-device. BlueZ keeps the pairing
# keys (/var/lib/bluetooth). This player keeps whether Bluetooth is on
# (config.json, "bluetooth") and which device is the output (the audio device
# file, like any other output).
#
# The Pi is the audio source, so it has to make the connection: a speaker
# never reconnects to it by itself. BluetoothManager watches the device that is
# the audio output and, while it is away, keeps reconnecting - every
# BT_RETRY_FAST_S for BT_RETRY_FAST_N attempts, then every BT_RETRY_SLOW_S for
# as long as it takes. While it is away the exhibit is silent; it does not jump
# to HDMI, which may be a screen with speakers nobody meant to use.
#
# Rewritten, not restored, from v10 (never run on hardware). What changed:
#   * bluetoothctl prints colour codes and its prompt into piped output, and
#     reports property changes as "[CHG] Device <MAC> RSSI: -52". Unstripped,
#     that renames a speaker "RSSI: -52" (handoff 3.8). bt_clean strips both.
#   * Its commands are asynchronous. v10 wrote pair, trust, connect and quit
#     all at once, and the session quit before pairing had happened. Pairing
#     and scanning go through _BtSession here, which waits for each answer;
#     single queries use bluetoothctl's one-shot command mode.
#   * "trust" stays essential: without it BlueZ turns the device away after a
#     reboot and the pairing looks lost.
# And from this project generally: every call is bounded (bluetoothctl waits
# for ever for a bluetoothd that is not running); every operation runs on the
# one manager thread, so a scan never collides with a reconnect; the log
# records changes, not attempts - this runs for the life of the unit.
#
# A unit where Bluetooth was never turned on is left exactly as it was: its
# adapter is not powered on or off, and nothing polls it.
# ══════════════════════════════════════════════════════════════════════════════

_BT_MAC_RE    = re.compile(r"^[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}$")
_BT_DEVICE_RE = re.compile(r"^alsa/bluealsa:DEV=([0-9A-F]{2}(?::[0-9A-F]{2}){5}),PROFILE=a2dp$")
_BT_ANSI_RE   = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|[\x01\x02]")
_BT_PROMPT_RE = re.compile(r"^\[[^\]\n]{1,64}\][#>]\s*")
# Property names BlueZ reports; a "name" shaped like one of these is a property
# change that lost its [CHG] marker, not a device called that.
_BT_PROP_RE   = re.compile(r"^(?:RSSI|TxPower|ManufacturerData|ServiceData|UUIDs|Connected|"
                           r"Paired|Bonded|Trusted|Blocked|Alias|Class|Icon|ServicesResolved|"
                           r"LegacyPairing|Appearance|AdvertisingFlags|AdvertisingData|Modalias|"
                           r"WakeAllowed|CablePairing|Sets|PreferredBearer):\s")

def bt_mac(value) -> Optional[str]:
    """A MAC address, upper case, or None if it is not one."""
    if isinstance(value, str) and _BT_MAC_RE.match(value.strip()):
        return value.strip().upper()
    return None

def bt_device_string(mac: str) -> str:
    """The --audio-device for a Bluetooth speaker."""
    return "alsa/bluealsa:DEV=%s,PROFILE=a2dp" % mac.upper()

def bt_mac_from_device(device) -> Optional[str]:
    """The MAC in a Bluetooth --audio-device, or None for any other output."""
    m = _BT_DEVICE_RE.match(device or "") if isinstance(device, str) else None
    return m.group(1) if m else None

def bt_clean(text) -> str:
    """bluetoothctl output as plain lines: no colour codes, no readline
    markers, no carriage returns, no prompt in front, no blank lines."""
    text = _BT_ANSI_RE.sub("", text or "").replace("\r", "\n")
    lines = []
    for line in text.split("\n"):
        prev = None
        while prev != line:                 # the prompt can repeat: "[bluetooth]# [bluetooth]# ..."
            prev, line = line, _BT_PROMPT_RE.sub("", line)
        line = line.strip()
        if line:
            lines.append(line)
    return "\n".join(lines)

def bt_parse_devices(text) -> list:
    """
    "Device <MAC> <name>" and "[NEW] Device <MAC> <name>" lines -> [{mac, name}]
    in the order first seen. [CHG] and [DEL] lines are property changes and
    removals, never names. name is None for a device that has not said what
    it is called (BlueZ then shows its address, with dashes).
    """
    seen = {}
    for line in bt_clean(text).split("\n"):
        if line.startswith(("[CHG]", "[DEL]")):
            continue
        if line.startswith("[NEW] "):
            line = line[6:]
        m = re.match(r"^Device ([0-9A-Fa-f:]{17})(?: (.*))?$", line)
        if not m or not _BT_MAC_RE.match(m.group(1)):
            continue
        mac, name = m.group(1).upper(), (m.group(2) or "").strip()
        if _BT_PROP_RE.match(name):
            continue
        if not name or name.replace("-", ":").upper() == mac:
            name = None
        if mac not in seen or (seen[mac] is None and name):
            seen[mac] = name
    return [{"mac": k, "name": v} for k, v in seen.items()]

def bt_parse_info(text) -> dict:
    """
    "bluetoothctl info <MAC>" -> what the panel and the reconnect loop need.
    found is False when BlueZ does not know the device. audio is a best guess
    from what a device advertises before pairing (its icon or class) and what
    it lists once paired (the A2DP Audio Sink service).
    """
    t = bt_clean(text)
    def flag(key):
        m = re.search(r"^%s:\s*(yes|no)\s*$" % key, t, re.M)
        return bool(m) and m.group(1) == "yes"
    def value(key):
        m = re.search(r"^%s:\s*(.+?)\s*$" % key, t, re.M)
        return m.group(1) if m else None
    head = re.search(r"^Device ([0-9A-Fa-f:]{17})(.*)$", t, re.M)
    found = bool(head) and "not available" not in head.group(2)     # "Device <MAC> not available"
    name = value("Alias") or value("Name")
    if name and head and name.replace("-", ":").upper() == head.group(1).upper():
        name = None                                                  # its address, not a name
    icon = value("Icon") or ""
    audio = icon.startswith("audio") or bool(re.search(r"Audio Sink|0000110b-", t, re.I))
    cls = value("Class")
    if cls:
        try:
            audio = audio or ((int(cls, 16) >> 8) & 0x1F) == 0x04      # major class: audio/video
        except ValueError:
            pass
    return {"found": found, "name": name if found else None, "paired": flag("Paired"),
            "trusted": flag("Trusted"), "connected": flag("Connected"), "audio": audio}

def bt_parse_show(text) -> dict:
    """"bluetoothctl show" -> whether there is an adapter, powered, or blocked."""
    t = bt_clean(text)
    return {"present": bool(re.search(r"^Controller [0-9A-Fa-f:]{17}", t, re.M)),
            "powered": bool(re.search(r"^Powered:\s*yes", t, re.M)),
            "blocked": bool(re.search(r"^PowerState:\s*off-blocked", t, re.M))}

def bt_retry_delay(failed_attempts: int) -> int:
    """Seconds before the next reconnect attempt."""
    return BT_RETRY_FAST_S if failed_attempts < BT_RETRY_FAST_N else BT_RETRY_SLOW_S

def bt_explain(line: str) -> str:
    """A bluetoothctl failure, in words for the panel."""
    low = (line or "").lower()
    if "authenticationfailed" in low or "authentication" in low:
        return "the device refused to pair - put it in pairing mode; if it asks for a PIN, it cannot be paired from here"
    if "page-timeout" in low or "pagetimeout" in low or "hostdown" in low or "connectionattemptfailed" in low:
        return "the device did not answer - is it switched on and in range?"
    if "not available" in low or "doesnotexist" in low:
        return "the device is not in range or not in pairing mode - scan again"
    if "inprogress" in low or "busy" in low:
        return "Bluetooth is busy with something else - try again in a moment"
    if "blocked" in low or "rfkill" in low:
        return "Bluetooth is blocked on this Pi (rfkill)"
    return (line or "no answer").strip()[:160]

# -- talking to bluetoothctl --------------------------------------------------
def _bt_run(args, timeout: float = 10) -> Tuple[Optional[int], str]:
    """One bluetoothctl command in its one-shot mode, e.g. ["info", MAC].
    (exit code, cleaned output); the code is None if it did not finish -
    usually bluetoothd not running, which bluetoothctl waits for without end."""
    try:
        r = subprocess.run(BT_CTL + list(args), capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
        return r.returncode, bt_clean((r.stdout or "") + (r.stderr or ""))
    except subprocess.TimeoutExpired as e:
        out = e.stdout or b""
        return None, bt_clean(out.decode("utf-8", "replace") if isinstance(out, bytes) else out)
    except Exception as e:
        return None, str(e)

class _BtSession:
    """
    An interactive bluetoothctl, driven a step at a time: send a command, then
    wait for one of the answers it can give. Pairing needs one - its agent
    lives only as long as the session. Prompts ending "(yes/no):" arrive
    without a newline and are answered "yes" (a passkey to confirm, a service
    to authorise). Use as a context manager: closing it always ends the process.
    """

    def __init__(self):
        self.transcript = []
        self._q = queue.Queue()
        self.p = subprocess.Popen(BT_CTL, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, bufsize=0)
        threading.Thread(target=self._read, daemon=True, name="bt-session").start()

    def _read(self):
        buf, fd = b"", self.p.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                chunk = b""
            if not chunk:
                break
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for raw in lines:
                self._emit(raw)
            if b"(yes/no)" in buf:              # a prompt, waiting with no newline
                self._emit(buf)
                buf = b""
        if buf:
            self._emit(buf)
        self._q.put(None)

    def _emit(self, raw: bytes):
        for line in bt_clean(raw.decode("utf-8", "replace")).split("\n"):
            if line:
                self.transcript.append(line)
                self._q.put(line)

    def send(self, command: str):
        try:
            self.p.stdin.write((command + "\n").encode())
            self.p.stdin.flush()
        except Exception:
            pass

    def expect(self, patterns, timeout: float) -> Tuple[Optional[int], str]:
        """Wait for a line matching one of `patterns` (case-insensitive).
        (index, line), or (None, "") on timeout or if bluetoothctl exits."""
        regs = [re.compile(p, re.I) for p in patterns]
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return None, ""
            try:
                line = self._q.get(timeout=min(left, 0.5))
            except queue.Empty:
                continue
            if line is None:
                return None, ""
            if "(yes/no)" in line:
                self.send("yes")
                continue
            for i, r in enumerate(regs):
                if r.search(line):
                    return i, line

    def wait(self, seconds: float):
        """Let output arrive for a while, answering any prompt."""
        self.expect([r"(?!x)x"], seconds)

    def close(self):
        self.send("quit")
        try:
            self.p.wait(timeout=3)
        except Exception:
            self.p.kill()
            try:
                self.p.wait(timeout=2)
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

# -- operations (called on the manager thread only) ----------------------------
def bt_installed() -> bool:
    """BlueZ's bluetoothctl and the bluez-alsa daemon are both here."""
    return bool(shutil.which(BT_CTL[0]) or os.path.exists(BT_CTL[-1])) and bool(
        shutil.which("bluealsa") or shutil.which("bluealsad"))

def bt_adapter_state() -> dict:
    rc, out = _bt_run(["show"], timeout=8)
    st = bt_parse_show(out)
    st["answered"] = rc is not None
    return st

def bt_info(mac: str) -> dict:
    rc, out = _bt_run(["info", mac], timeout=8)
    return bt_parse_info(out)

def bt_set_power(on: bool) -> Tuple[bool, str]:
    """Adapter on (not discoverable, not pairable) or off. A radio blocked by
    rfkill is unblocked first, through the one sudo rule install.sh adds."""
    rc, out = _bt_run(["power", "on" if on else "off"], timeout=10)
    if on and re.search(r"blocked|rfkill", out, re.I):
        rfkill = shutil.which("rfkill") or "/usr/sbin/rfkill"
        try:
            subprocess.run(["sudo", "-n", rfkill, "unblock", "bluetooth"],
                           capture_output=True, timeout=10)
        except Exception:
            pass
        time.sleep(1)
        rc, out = _bt_run(["power", "on"], timeout=10)
    ok = bool(re.search(r"succeeded", out, re.I))
    if ok and on:
        _bt_run(["discoverable", "off"], timeout=8)     # nothing should find and pair with the Pi
        _bt_run(["pairable", "off"], timeout=8)
    return ok, "" if ok else bt_explain(out.split("\n")[-1] if out else "no answer from bluetoothd")

def bt_paired_devices() -> list:
    """Paired devices with their state. Each is checked with "info": older
    bluetoothctl lists every known device for "devices Paired"."""
    rc, out = _bt_run(["devices", "Paired"], timeout=8)
    found = bt_parse_devices(out)
    if not found:
        rc, out = _bt_run(["paired-devices"], timeout=8)
        found = bt_parse_devices(out)
    devices = []
    for d in found[:20]:
        info = bt_info(d["mac"])
        if info["paired"]:
            devices.append({"mac": d["mac"], "name": info["name"] or d["name"] or d["mac"],
                            "connected": info["connected"], "audio": info["audio"]})
    return devices

def bt_scan(seconds: int = BT_SCAN_S) -> list:
    """Look for devices. Classic Bluetooth only where bluetoothctl can filter
    (A2DP speakers are classic; low-energy gadgets are just noise here).
    Speakers and headphones first, then named devices, unnamed ones last."""
    with _BtSession() as s:
        for cmd in ("menu scan", "transport bredr", "back", "scan on"):
            s.send(cmd)
        s.wait(seconds)
        s.send("devices")
        s.wait(1.5)
        s.send("scan off")
        s.wait(0.5)
        found = bt_parse_devices("\n".join(s.transcript))
    devices = []
    for d in found[:40]:
        info = bt_info(d["mac"])
        devices.append({"mac": d["mac"], "name": info["name"] or d["name"],
                        "audio": info["audio"], "paired": info["paired"]})
    devices.sort(key=lambda d: (not d["audio"], d["name"] is None, (d["name"] or "").lower()))
    return devices

def bt_pair(mac: str) -> Tuple[bool, str]:
    """Pair, trust and connect, a step at a time in one session (its agent
    answers the device). An already-paired device counts as paired."""
    m = re.escape(mac)
    with _BtSession() as s:
        s.send("agent NoInputNoOutput")
        s.expect([r"Agent registered", r"already registered", r"Failed"], 6)
        s.send("default-agent")
        s.expect([r"Default agent request successful", r"Failed|No agent"], 6)
        s.send("pairable on")
        s.expect([r"pairable on succeeded", r"Failed"], 6)
        if not bt_info(mac)["found"]:
            # BlueZ forgets a device it only discovered shortly after the scan
            # ends: find it again before asking to pair.
            s.send("scan on")
            s.expect([r"Device %s" % m], 15)
            s.send("scan off")
            s.wait(1)
        s.send("pair " + mac)
        i, line = s.expect([r"Pairing successful", r"%s Paired: yes" % m, r"AlreadyExists|Already Exists",
                            r"Failed to pair|not available|Authentication"], BT_PAIR_TIMEOUT_S)
        if i is None:
            s.send("cancel-pairing " + mac)
            return False, "the device did not answer - is it in pairing mode?"
        if i == 3:
            return False, bt_explain(line)
        s.send("trust " + mac)
        s.expect([r"trust succeeded", r"Failed"], 8)
        s.send("connect " + mac)
        j, line = s.expect([r"Connection successful", r"%s Connected: yes" % m, r"Failed to connect"],
                           BT_CONNECT_TIMEOUT_S)
        s.send("pairable off")
        s.wait(0.3)
    if j in (0, 1):
        return True, "paired and connected"
    return True, "paired, but not connected yet (%s)" % bt_explain(line) if line else "paired, but not connected yet"

def bt_connect(mac: str) -> Tuple[bool, str]:
    """One connection attempt. (connected, why not)."""
    rc, out = _bt_run(["connect", mac], timeout=BT_CONNECT_TIMEOUT_S)
    if re.search(r"Connection successful", out) or bt_info(mac)["connected"]:
        return True, ""
    lines = [l for l in out.split("\n") if "fail" in l.lower() or "error" in l.lower()]
    return False, bt_explain(lines[-1] if lines else ("no answer" if rc is None else out[-160:]))

def bt_remove(mac: str) -> Tuple[bool, str]:
    rc, out = _bt_run(["remove", mac], timeout=10)
    ok = bool(re.search(r"removed", out, re.I)) or not bt_info(mac)["found"]
    return ok, "" if ok else bt_explain(out)

def bt_bluealsa_running() -> bool:
    for unit in ("bluealsa", "bluealsad"):
        try:
            if subprocess.run(["systemctl", "is-active", "--quiet", unit], timeout=5).returncode == 0:
                return True
        except Exception:
            pass
    return False

def _bt_pipewire_running() -> bool:
    """PipeWire (desktop images) claims Bluetooth audio itself, and then
    bluealsa cannot."""
    try:
        return subprocess.run(["pgrep", "-x", "wireplumber"], capture_output=True,
                              timeout=5).returncode == 0
    except Exception:
        return False

# -- settings ------------------------------------------------------------------
def bt_config() -> dict:
    c = config.get("bluetooth")
    return c if isinstance(c, dict) else {}

def bt_enabled() -> bool:
    return bt_config().get("enabled") is True

def bt_fallback_device() -> str:
    """Where the audio goes when Bluetooth stops being the output: whatever it
    was before Bluetooth was chosen, else HDMI."""
    fb = bt_config().get("fallback")
    if isinstance(fb, str) and fb.strip() and not bt_mac_from_device(fb):
        return fb.strip()
    return ALSA_DEVICE_DEFAULT

def _bt_config_update(**changes):
    c = dict(bt_config())
    c.update(changes)
    config["bluetooth"] = c
    save_config(dict(config))

def _mpv_has_audio_track() -> Optional[bool]:
    tracks = mpv_get_property("track-list")
    if not isinstance(tracks, list):
        return None
    return any(isinstance(t, dict) and t.get("type") == "audio" for t in tracks)

def _bt_reattach_audio(force: bool) -> str:
    """
    After the speaker (re)connects, put mpv's sound back on it - without
    stopping the picture where mpv allows. ao-reload first (mpv calls it
    internal, so it is checked); then the audio track off and on, which
    reopens the output and is documented; only then a restart of mpv.
    `force`: the device dropped while mpv was using it, so mpv's output may
    look alive and be dead - reload regardless.
    """
    if not _mpv_is_running():
        return "mpv not running - the watchdog starts it on this device"
    if _mpv_has_audio_track() is False:
        return "this clip has no audio"
    if not force and mpv_get_property("current-ao"):
        return "already playing"
    mpv_command("ao-reload")
    time.sleep(1.5)
    if mpv_get_property("current-ao"):
        return "audio output reopened"
    mpv_set_property("aid", "no")
    time.sleep(0.3)
    mpv_set_property("aid", "auto")
    time.sleep(1.5)
    if mpv_get_property("current-ao"):
        return "audio track reselected"
    with state_lock:
        if state.restart_in_progress:
            return "a restart is already under way"
        state.restart_in_progress = True
    logger.warning("Bluetooth: mpv would not reopen its audio output - restarting it")
    try:
        path, loop = _desired_video()
        _start_mpv(path, loop=loop)
    finally:
        with state_lock:
            state.restart_in_progress = False
    return "mpv restarted"

def bt_view(st: dict) -> dict:
    """The panel's one line: level ok / warn / err / off, and what to do."""
    def head(level, text):
        return {"level": level, "text": text}
    if not st.get("installed"):
        return head("off", "The Bluetooth audio software is not installed on this unit. "
                           "Run the one-line installer to add it.")
    ad = st.get("adapter") or {}
    if st.get("adapter_checked") and not ad.get("present"):
        return head("err", "No Bluetooth adapter found on this unit." if ad.get("answered", True) else
                    "Bluetooth (bluetoothd) is not answering. On the Pi: sudo systemctl status bluetooth")
    if st.get("enabled") is not True:
        return head("off", "Bluetooth is off.")
    if ad.get("blocked"):
        return head("err", "Bluetooth is blocked on this Pi (rfkill). On the Pi: sudo rfkill unblock bluetooth")
    if st.get("busy"):
        return head("warn", st.get("busy_label") or "Working...")
    if not st.get("bluealsa"):
        return head("err", "The Bluetooth audio service is not running, so nothing can play. "
                           "On the Pi: sudo systemctl status bluealsa")
    out = st.get("output")
    if not out:
        return head("ok", "Bluetooth is on. No Bluetooth device is the audio output: pair one, "
                          "then choose Use for audio.")
    link = st.get("link") or {}
    name = out.get("name") or out.get("mac")
    if link.get("state") == "connected":
        return head("ok", "Playing to %s." % name)
    if link.get("state") == "slow":
        return head("warn", "%s has not come back after %d attempts - trying once a minute (next in %d s). "
                            "Check it is switched on, in range and not connected to something else."
                    % (name, link.get("attempts", 0), link.get("next_in", 0)))
    if link.get("attempts"):
        return head("warn", "%s is not connected - reconnecting (attempt %d of %d before slowing "
                            "to once a minute)." % (name, link["attempts"], BT_RETRY_FAST_N))
    return head("warn", "Connecting to %s..." % name)

class BluetoothManager:
    """
    The one thread that talks to Bluetooth. Each second it either runs the
    action somebody asked for in the web UI (one at a time - a second is
    refused while one runs), or looks after the link to the speaker that is
    the audio output. Everything it found is kept for /bluetooth/status, so
    the panel's polling never waits on Bluetooth.
    """

    JOBS = {"enable": "Turning Bluetooth on...", "disable": "Turning Bluetooth off...",
            "scan": "Scanning for devices (%d s) - put the speaker in pairing mode..." % BT_SCAN_S,
            "pair": "Pairing with %s...", "forget": "Forgetting %s...",
            "reconnect": "Reconnecting..."}

    def __init__(self, clock=time.monotonic):
        self._clock      = clock
        self._lock       = threading.Lock()
        self._wake       = threading.Event()
        self._job        = None          # (kind, args) for the thread
        self._busy       = ""            # the job running now
        self._busy_label = ""
        self._message    = None          # the last job's outcome, for the panel
        self._scan       = None
        self._paired     = []
        self._adapter    = {}
        self._adapter_checked = False
        self._installed  = None
        self._bluealsa   = False
        self._pipewire   = False
        self._applied    = False         # the saved on/off state applied since start
        self._next_refresh = 0.0
        self._start_at   = 0.0
        self._thread     = None
        # the link to the speaker that is the audio output
        self._target     = None
        self._connected  = False
        self._attempts   = 0
        self._next_try   = 0.0
        self._dropped    = False         # it dropped while mpv was using it
        self._lost_at    = None
        self._last_why   = ""

    # -- public ---------------------------------------------------------------
    def start(self):
        if self._thread is None:
            self._start_at = self._clock() + BT_BOOT_GRACE_S
            self._thread = threading.Thread(target=self._run, daemon=True, name="bluetooth")
            self._thread.start()

    def kick(self):
        """Look at the link now - the audio output has just changed."""
        with self._lock:
            self._next_try = 0.0
        self._wake.set()

    def submit(self, kind: str, **args) -> Tuple[bool, str]:
        with self._lock:
            if self._job or self._busy:
                return False, "Bluetooth is busy: %s" % (self._busy_label or "another action")
            self._job = (kind, args)
            label = self.JOBS[kind]
            self._busy_label = label % args.get("name", args.get("mac", "")) if "%s" in label else label
        self._wake.set()
        return True, self._busy_label

    def paired_audio(self) -> list:
        with self._lock:
            return [dict(d) for d in self._paired if d.get("audio", True)]

    def status(self) -> dict:
        now = self._clock()
        with self._lock:
            out_mac = bt_mac_from_device(state.audio_device) if bt_enabled() else None
            names = {d["mac"]: d["name"] for d in self._paired}
            link = {"state": "none", "attempts": self._attempts,
                    "next_in": max(0, int(self._next_try - now)), "why": self._last_why}
            if out_mac and self._target == out_mac:
                if self._connected:
                    link["state"] = "connected"
                elif self._attempts >= BT_RETRY_FAST_N:
                    link["state"] = "slow"
                else:
                    link["state"] = "reconnecting" if self._attempts else "connecting"
            st = {
                "installed": bool(self._installed), "enabled": bt_config().get("enabled"),
                "adapter": dict(self._adapter), "adapter_checked": self._adapter_checked,
                "bluealsa": self._bluealsa, "pipewire": self._pipewire,
                "busy": bool(self._busy or self._job), "busy_label": self._busy_label,
                "message": self._message, "scan": self._scan,
                "paired": [dict(d, output=(d["mac"] == out_mac)) for d in self._paired],
                "output": {"mac": out_mac, "name": names.get(out_mac, out_mac)} if out_mac else None,
                "link": link,
            }
        st["view"] = bt_view(st)
        return st

    # -- the thread -------------------------------------------------------------
    def _run(self):
        last_exc = ""
        while True:
            try:
                self.tick()
                last_exc = ""
            except Exception as e:
                if repr(e) != last_exc:             # once per distinct fault
                    logger.exception("Bluetooth manager error")
                    last_exc = repr(e)
            self._wake.wait(1.0)
            self._wake.clear()

    def tick(self):
        now = self._clock()
        if self._installed is None:
            self._installed = bt_installed()
            if self._installed:
                self._adapter = bt_adapter_state()      # one look, for the panel
                self._adapter_checked = True
        if not self._installed:
            with self._lock:
                self._job = None
            return
        with self._lock:
            job, self._job = self._job, None
            if job:
                self._busy = job[0]
        if job:
            self._run_job(*job)
            return
        if now < self._start_at:
            return
        enabled = bt_config().get("enabled")
        if not self._applied and enabled is not None:
            bt_set_power(enabled is True)               # as it was left, whatever booted with
            self._applied = True
            self._next_refresh = 0.0
        if enabled is not True:
            self._set_target(None, now)                 # never turned on: nothing is touched
            return
        if now >= self._next_refresh:
            self._refresh(now)
            ad = self._adapter
            if ad.get("present") and not ad.get("powered") and not ad.get("blocked"):
                logger.warning("Bluetooth: the adapter was off - turning it back on")
                bt_set_power(True)
        with state_lock:
            target = bt_mac_from_device(state.audio_device)
        self._set_target(target, now)
        if target and now >= self._next_try:
            self._check_link()

    def _refresh(self, now: float):
        adapter = bt_adapter_state()
        paired = bt_paired_devices() if adapter.get("present") else []
        bluealsa, pipewire = bt_bluealsa_running(), _bt_pipewire_running()
        with self._lock:
            self._adapter, self._adapter_checked = adapter, True
            self._paired, self._bluealsa, self._pipewire = paired, bluealsa, pipewire
            self._next_refresh = now + BT_REFRESH_S

    def _set_target(self, target, now: float):
        with self._lock:
            if target == self._target:
                return
            self._target, self._connected, self._attempts = target, False, 0
            self._next_try, self._dropped, self._lost_at, self._last_why = now, False, None, ""

    def _name(self, mac: str) -> str:
        with self._lock:
            for d in self._paired:
                if d["mac"] == mac:
                    return d["name"]
        return mac

    def _check_link(self):
        mac = self._target
        info = bt_info(mac)
        name = info["name"] or self._name(mac)
        if info["connected"]:
            if not self._connected:
                self._on_connected(mac, name)
            with self._lock:
                self._next_try = self._clock() + BT_MONITOR_S
            return
        if self._connected:
            logger.warning("Bluetooth: %s disconnected - reconnecting", name)
            with self._lock:
                self._connected, self._attempts = False, 0
                self._dropped, self._lost_at = True, time.time()
        ok, why = bt_connect(mac)
        if ok:
            self._on_connected(mac, name)
            with self._lock:
                self._next_try = self._clock() + BT_MONITOR_S
            return
        with self._lock:
            self._attempts += 1
            n = self._attempts
            self._last_why = why
            self._next_try = self._clock() + bt_retry_delay(n)
        if n == 1 and not self._dropped:
            logger.info("Bluetooth: %s is not connected - connecting (%s)", name, why)
        elif n == BT_RETRY_FAST_N:
            logger.warning("Bluetooth: %s not back after %d attempts - now trying once a minute (%s)",
                           name, n, why)
        elif n > BT_RETRY_FAST_N and (n - BT_RETRY_FAST_N) % 60 == 0:
            logger.warning("Bluetooth: %s still not reachable after %d attempts (%s)", name, n, why)

    def _on_connected(self, mac: str, name: str):
        with self._lock:
            n, dropped, lost_at = self._attempts, self._dropped, self._lost_at
            self._connected, self._attempts, self._last_why = True, 0, ""
            self._dropped, self._lost_at = False, None
        if dropped:
            logger.info("Bluetooth: %s reconnected after %d attempt(s), %d s away",
                        name, n + 1, int(time.time() - (lost_at or time.time())))
        else:
            logger.info("Bluetooth: connected to %s", name)
        how = _bt_reattach_audio(force=dropped)
        logger.info("Bluetooth: audio to %s - %s", name, how)
        self._next_refresh = 0.0

    def _run_job(self, kind: str, args: dict):
        ok, text = False, ""
        try:
            if kind == "enable":
                _bt_config_update(enabled=True)
                ok, why = bt_set_power(True)
                self._applied = True
                text = "Bluetooth is on." if ok else "Bluetooth could not be turned on: " + why
                logger.info("Bluetooth turned on from the web UI%s", "" if ok else " - " + why)
            elif kind == "disable":
                with state_lock:
                    using = bt_mac_from_device(state.audio_device)
                if using:
                    _apply_audio_device(bt_fallback_device())
                _bt_config_update(enabled=False)
                bt_set_power(False)
                self._applied = True
                self._set_target(None, self._clock())
                ok = True
                text = "Bluetooth is off." + (" The audio went back to %s." % bt_fallback_device() if using else "")
                logger.info("Bluetooth turned off from the web UI")
            elif kind == "scan":
                found = bt_scan(BT_SCAN_S)
                with self._lock:
                    self._scan = {"at": time.strftime("%H:%M:%S"), "devices": found}
                ok = True
                audio = sum(1 for d in found if d["audio"])
                text = ("Found %d device(s), %d of them speakers or headphones." % (len(found), audio)
                        if found else "Nothing found. Put the speaker in pairing mode and scan again.")
            elif kind == "pair":
                ok, why = bt_pair(args["mac"])
                name = args.get("name") or args["mac"]
                text = ("%s: %s." % (name, why)) if ok else "Could not pair with %s: %s." % (name, why)
                if ok:
                    text += " Choose Use for audio to send the player's sound to it."
                logger.info("Bluetooth: pair %s (%s) - %s", name, args["mac"], why)
            elif kind == "forget":
                mac = args["mac"]
                with state_lock:
                    using = bt_mac_from_device(state.audio_device) == mac
                if using:
                    _apply_audio_device(bt_fallback_device())
                ok, why = bt_remove(mac)
                text = ("Forgotten." if ok else "Could not forget it: %s" % why) + (
                    " The audio went back to %s." % bt_fallback_device() if using else "")
                logger.info("Bluetooth: forget %s - %s", mac, "done" if ok else why)
            elif kind == "reconnect":
                with self._lock:
                    self._attempts, self._next_try = 0, 0.0
                ok, text = True, "Trying now."
        except Exception as e:
            logger.exception("Bluetooth: %s failed", kind)
            ok, text = False, "%s failed: %s" % (kind, e)
        finally:
            with self._lock:
                self._message = {"ok": ok, "text": text, "at": time.strftime("%H:%M:%S")}
            try:
                self._refresh(self._clock())
            finally:
                with self._lock:
                    self._busy, self._busy_label = "", ""

bt_manager = BluetoothManager()

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
                                For AES67, the loopback and three more flags:
                                see _mpv_audio_args.
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
        *_mpv_audio_args(audio_dev),
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

def _mpv_audio_args(audio_dev: str) -> list:
    """
    mpv's audio output flags. Any device but AES67: exactly --audio-device, as
    before AES67 existed.

    AES67: mpv plays into the loopback, at the one format the bridge reads —
    48 kHz float — so nothing on the way converts. --audio-delay is negative:
    the picture waits for the audio's trip through the bridge and the card,
    which mpv cannot see (it only knows the loopback's own buffer), plus the
    installer's lip-sync offset. If AES67 is saved but no longer installed,
    fall back to HDMI rather than play into a loopback nobody reads.
    """
    if bt_mac_from_device(audio_dev) and not bt_enabled():
        # A Bluetooth speaker is saved but Bluetooth has been turned off (or
        # never back on after an update): play where the audio was before it.
        fallback = bt_fallback_device()
        logger.warning("A Bluetooth speaker is the saved audio output but Bluetooth is off - using %s",
                       fallback)
        return _mpv_audio_args(fallback)
    if audio_dev != AES67_DEVICE:
        # Includes a Bluetooth speaker with Bluetooth on: it is an ALSA device
        # like any other. If it is not connected yet, mpv plays the picture
        # without sound, and BluetoothManager reattaches the audio when it is.
        return [f"--audio-device={audio_dev}"]
    if not aes67_installed():
        logger.warning("AES67 is the saved audio output but is not installed - using %s",
                       ALSA_DEVICE_DEFAULT)
        return [f"--audio-device={ALSA_DEVICE_DEFAULT}"]
    delay = aes67_audio_delay(aes67_read_bridge(), aes67_settings()["offset_ms"])
    return [
        f"--audio-device={AES67_LOOP_DEVICE}",
        "--audio-samplerate=48000",
        "--audio-format=float",
        f"--audio-delay={delay:.3f}",
    ]

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
# SOFTWARE UPDATE FROM GITHUB
# ══════════════════════════════════════════════════════════════════════════════
#
# Checks the public distribution repo for a newer published release and, when
# asked, installs it. Version numbers are the whole basis of this, so:
#
#   * PLAYER_VERSION is an integer, and must match the filename and the
#     docstring title. tools/validate_player.py checks all three agree.
#   * release.sh publishes release.json alongside the script, recording its
#     version and SHA-256, and REFUSES to publish different content under a
#     version number that is already published. That is what makes "same
#     version means nothing to do" a safe rule: a version can never quietly
#     change underneath a unit that is already running it.
#   * It never downgrades. A unit running something newer than the published
#     release (a test unit, say) is told so and left alone.
#
# What it installs: the player script only, through _deploy_script() - the same
# validate / back up / atomic write / restart path as a manual upload. It does
# NOT install saw-player.service or run the installer. Those need root, and
# granting root to an unauthenticated web page is not a trade worth making. If
# a release changes the service file, the check says so and points at the
# install one-liner.
#
# Trust: the source is fixed in code, never taken from a request, and fetched
# over HTTPS with certificate checking. The SHA-256 in release.json catches a
# truncated or corrupted download; it is not a signature, so authenticity rests
# on HTTPS and on the GitHub repo itself - exactly as it does for the bootstrap
# one-liner.
# ══════════════════════════════════════════════════════════════════════════════

class UpdateError(Exception):
    """A problem with an update, worded to be shown to the operator as-is."""


def _update_sources(path: str) -> list:
    if UPDATE_BASE_OVERRIDE:
        return [(UPDATE_BASE_OVERRIDE.rstrip("/") + "/" + path, {})]
    return [
        # raw first: no rate limit, and it is what the install one-liner uses.
        ("https://raw.githubusercontent.com/%s/%s/%s"
         % (UPDATE_REPO, UPDATE_BRANCH, path), {}),
        # The API as a fallback: some corporate proxies block raw but pass the
        # API. Unauthenticated it allows 60 requests an hour per IP - plenty for
        # a button someone presses by hand.
        ("https://api.github.com/repos/%s/contents/%s?ref=%s"
         % (UPDATE_REPO, path, UPDATE_BRANCH),
         {"Accept": "application/vnd.github.raw"}),
    ]


def _update_fetch(path: str, max_bytes: int) -> bytes:
    """Download a file from the release source, trying each source in turn."""
    problems = []
    for url, headers in _update_sources(path):
        host = url.split("/")[2]
        try:
            with requests.get(url, headers=headers, timeout=UPDATE_TIMEOUT,
                              stream=True) as r:
                if r.status_code != 200:
                    problems.append("%s returned HTTP %d" % (host, r.status_code))
                    continue
                buf = bytearray()
                for chunk in r.iter_content(65536):
                    buf += chunk
                    if len(buf) > max_bytes:
                        raise UpdateError("%s sent more than %d bytes for %s - refused"
                                          % (host, max_bytes, path))
                return bytes(buf)
        except UpdateError:
            raise
        except requests.exceptions.SSLError:
            problems.append("%s: secure connection failed - check the unit's clock "
                            "with 'date'" % host)
        except requests.exceptions.Timeout:
            problems.append("%s timed out" % host)
        except requests.exceptions.ConnectionError:
            problems.append("%s unreachable" % host)
        except requests.RequestException as e:
            problems.append("%s: %s" % (host, type(e).__name__))
    # Every source answered, and every one said 404: the network is fine, the
    # file simply is not published. Saying "firewall" here would send someone
    # chasing the wrong problem - e.g. before the first release.json exists.
    if problems and all(p.endswith("HTTP 404") for p in problems):
        raise UpdateError("%s is not published on the update source yet "
                          "(every source answered 404)" % path)
    raise UpdateError("could not download %s (%s). If this unit is behind a "
                      "firewall, update it with update.sh instead."
                      % (path, "; ".join(problems)))


def _update_manifest() -> dict:
    """Fetch and strictly validate release.json."""
    raw = _update_fetch(UPDATE_MANIFEST, 64 * 1024)
    try:
        m = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise UpdateError("the published %s is not valid JSON" % UPDATE_MANIFEST)
    if not isinstance(m, dict):
        raise UpdateError("the published %s is not a JSON object" % UPDATE_MANIFEST)
    v, f, sha = m.get("version"), m.get("file"), m.get("sha256")
    # bool is a subclass of int in Python, so rule it out explicitly.
    if not isinstance(v, int) or isinstance(v, bool) or v < 1:
        raise UpdateError("%s has no valid version number" % UPDATE_MANIFEST)
    # The filename becomes part of a download URL, so it must be exactly the
    # expected name for that version - nothing else can reach the path.
    if f != "RPi_Video_Player_v%d.py" % v:
        raise UpdateError("%s names %r; expected RPi_Video_Player_v%d.py"
                          % (UPDATE_MANIFEST, f, v))
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise UpdateError("%s has no valid SHA-256" % UPDATE_MANIFEST)
    size = m.get("size")
    if size is not None and (not isinstance(size, int) or isinstance(size, bool)
                             or not 0 < size <= UPDATE_MAX_SCRIPT):
        raise UpdateError("%s has an invalid size" % UPDATE_MANIFEST)
    return m


def _script_version(content: bytes):
    """The PLAYER_VERSION a script declares, or None if it declares none."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    m = re.search(r"^PLAYER_VERSION\s*=\s*(\d+)\s*$", text, re.M)
    return int(m.group(1)) if m else None


def _sha256_file(path: str):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def update_check() -> dict:
    """Compare this unit with the published release. Changes nothing."""
    res = {"installed_version": PLAYER_VERSION,
           "checked": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        m = _update_manifest()
    except UpdateError as e:
        logger.warning("Update check failed: %s", e)
        res.update(ok=False, error=str(e))
        return res
    latest        = m["version"]
    pub_service   = m.get("service_sha256")
    local_service = _sha256_file(SERVICE_INSTALLED)
    res.update(
        ok=True,
        latest_version=latest,
        released=m.get("released", ""),
        update_available=latest > PLAYER_VERSION,
        newer_than_published=PLAYER_VERSION > latest,
        # Only meaningful at equal versions: is this unit's copy the published build?
        build_matches=((_sha256_file(SCRIPT_PATH) == m["sha256"])
                       if latest == PLAYER_VERSION else None),
        # None when it cannot be told: a manifest without the field, or no readable unit file.
        service_changed=((local_service != pub_service)
                         if (pub_service and local_service) else None),
    )
    logger.info("Update check: installed v%d, published v%d%s", PLAYER_VERSION, latest,
                " - update available" if res["update_available"] else "")
    return res


_update_lock = threading.Lock()


def update_apply():
    """Install the published release if it is newer. Returns (ok, message, http_status)."""
    if not _update_lock.acquire(blocking=False):
        return False, "an update is already in progress", 409
    try:
        m = _update_manifest()      # re-read: never act on an earlier check
        latest = m["version"]
        if latest <= PLAYER_VERSION:
            return False, ("nothing to install: this unit runs v%d and the published "
                           "release is v%d" % (PLAYER_VERSION, latest)), 409
        content = _update_fetch(m["file"], UPDATE_MAX_SCRIPT)
        if m.get("size") is not None and len(content) != m["size"]:
            raise UpdateError("the download is %d bytes but %s says %d - incomplete; "
                              "nothing was changed" % (len(content), UPDATE_MANIFEST, m["size"]))
        if hashlib.sha256(content).hexdigest() != m["sha256"]:
            raise UpdateError("the checksum does not match %s - the download is corrupt "
                              "or incomplete; nothing was changed" % UPDATE_MANIFEST)
        declared = _script_version(content)
        if declared != latest:
            raise UpdateError("the downloaded file declares version %s but %s says %d; "
                              "nothing was changed" % (declared, UPDATE_MANIFEST, latest))
        logger.warning("Updating from v%d to v%d (published %s)",
                       PLAYER_VERSION, latest, m.get("released", "?"))
        return _deploy_script(content, "GitHub release v%d" % latest)
    except UpdateError as e:
        logger.warning("Update not applied: %s", e)
        return False, str(e), 502
    finally:
        _update_lock.release()


def _deploy_script(content: bytes, source: str):
    """
    Validate, back up, atomically install, and restart the service.

    The single path for replacing the running script - shared by the manual
    upload and the GitHub update, so both get the same proven behaviour.
    Returns (ok, message, http_status).
    """
    ok, reason = _validate_script(content)
    if not ok:
        logger.warning("Script from %s rejected: %s", source, reason)
        return False, reason, 422

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
        fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(SCRIPT_PATH),
                                        suffix=".upload.tmp")
        with os.fdopen(fd, "wb") as tmp_f:
            tmp_f.write(content)
        os.chmod(tmp_path, 0o755)
        os.replace(tmp_path, SCRIPT_PATH)
        tmp_path = None
        logger.info("New script deployed to %s (from %s)", SCRIPT_PATH, source)
    except Exception:
        logger.exception("Failed to write new script")
        return False, "Failed to write script", 500
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
        return True, "Script deployed but service restart failed — restart manually", 207

    return True, "Script deployed — restarting service", 200

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
/* AES67 */
.aes-head{padding:8px 10px;border-radius:6px;font-size:0.9em;margin:4px 0 8px}
.aes-ok{background:#d4edda;color:#155724}
.aes-warn{background:#fff3cd;color:#856404}
.aes-err{background:#f8d7da;color:#721c24}
.aes-off{background:#eef0f4;color:#555}
.aes-alert{background:#fff3cd;color:#856404;border-left:4px solid #e0a800;padding:6px 10px;font-size:0.85em;margin:0 0 8px;border-radius:4px}
.aes-grid{display:grid;grid-template-columns:max-content 1fr;gap:3px 14px;font-size:0.82em;margin:4px 0 6px}
.aes-grid .key{color:#666}
.aes-grid div{overflow-wrap:anywhere}
/* Bluetooth */
.bt-sub{font-weight:600;font-size:0.9em;margin:14px 0 4px}
.bt-row{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px;padding:7px 0;border-top:1px solid #eef0f4;font-size:0.88em}
.bt-name{font-weight:600;overflow-wrap:anywhere}
.bt-meta{color:#666;font-size:0.9em;flex:1;min-width:150px;overflow-wrap:anywhere}
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
/* RAM pinning status. Pinned clips are locked into memory so playback never
   depends on the SD card - see RAM PINNING in the player script. */
function ramPinText(rp){
  const DASH=' '+String.fromCharCode(8212)+' ';
  if(!rp||!rp.state)return 'RAM-pinned clips: unknown';
  if(rp.state==='not started')return 'RAM-pinned clips: starting...';
  if(rp.state==='disabled'||rp.state==='unavailable'||rp.state==='paused')return 'RAM-pinned clips: '+rp.message;
  const names=(rp.pinned||[]).map(p=>p.file+' ('+p.mb+' MB)').join(', ')||'none';
  let t='RAM-pinned clips: '+names+DASH+rp.pinned_mb+' of '+rp.budget_mb+' MB budget';
  const sk=(rp.skipped||[]).map(p=>p.file+' ('+p.why+')').join(', ');
  if(sk)t+='. Not pinned: '+sk;
  if(rp.message)t+='. '+rp.message;
  return t;
}
async function fetchSysInfo(){
  try{
    const d=await(await fetch('/system/info')).json();
    if(d.error)return;
    document.getElementById('si_temp').textContent=d.cpu_temp;
    const mem=d.memory;
    /* Pinned clips count as used memory, so say so - otherwise a full RAM-pin
       budget (~75%) turns this bar amber and looks like a fault. */
    const pin=(d.ram_pin&&d.ram_pin.pinned_mb)?(' (incl. '+d.ram_pin.pinned_mb+' MB pinned clips)'):'';
    document.getElementById('si_mem').textContent=mem.used_mb+'MB / '+mem.total_mb+'MB'+pin;
    setBar('bar_mem',mem.pct);
    const disk=d.disk;
    document.getElementById('si_disk').textContent=disk.used_gb+'GB / '+disk.total_gb+'GB';
    setBar('bar_disk',disk.pct);
    document.getElementById('si_uptime').textContent=d.uptime;
    document.getElementById('si_host').textContent=d.hostname;
    const ram=document.getElementById('si_ram');
    ram.textContent=ramPinText(d.ram_pin);
    ram.style.color=(d.ram_pin&&(d.ram_pin.state==='limited'||d.ram_pin.state==='partial'))?'#b06d00':'#555';
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
/* Deleting a clip is irreversible and there is no copy on the Pi, so this
   always confirms first. The loop video gets a stronger warning: every other
   slot degrades harmlessly when absent (play_trigger just logs, override falls
   back to the loop) but with no loop there is nothing left to show. */
async function deleteClip(url,label,essential){
  const NL=String.fromCharCode(10);
  let msg='Delete '+label+'?'+NL+NL+'This permanently removes the file from the Pi. It cannot be undone - you would have to upload the clip again.';
  if(essential){
    msg+=NL+NL+'WARNING: this is the attractor loop. With no loop video there is nothing to play, so the screen will go black the next time the player restarts, until you upload a replacement.';
  }
  if(!confirm(msg))return;
  try{
    const r=await fetch(url,{method:'POST'});
    if(r.ok){
      const d=await r.json().catch(()=>({}));
      if(d.was_playing){
        toast(label+' deleted '+String.fromCharCode(8212)+' still on screen until the player next restarts','ok',7000);
      }else{
        toast(label+' deleted','ok');
      }
    }else{
      const t=await r.text().catch(()=>'');
      toast('Could not delete '+label+(t?(': '+t):''),'err');
    }
  }catch(e){toast('Delete request failed','err');}
  fetchSlotStatus();
}
async function fetchSlotStatus(){
  try{
    const d=await(await fetch('/system/video_slots')).json();
    const slots={loop:'sl_loop',trigger1:'sl_t1',trigger2:'sl_t2',trigger3:'sl_t3',override:'sl_ov'};
    const dels={loop:'d_f_loop',trigger1:'d_f_t1',trigger2:'d_f_t2',trigger3:'d_f_t3',override:'d_f_ov'};
    for(const[key,id] of Object.entries(slots)){
      const s=d[key];
      const db=document.getElementById(dels[key]);
      if(db)db.disabled=!(s&&s.exists);   /* nothing to delete on an empty slot */
      const el=document.getElementById(id);
      if(!el)continue;
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
    const cur=[...sel.options].find(o=>o.value===current);
    st.textContent='Current: '+(cur?cur.textContent:current);
  }catch(e){st.textContent='Could not load devices';}
}

/* ── Bluetooth ──
   Actions are queued on the Pi and answered at once; the panel follows them
   through /bluetooth/status. Device names come from radios nearby, so all of
   it is set with textContent, never innerHTML. */
let btTimer=null,btData=null,btWasBusy=false;
function btEl(tag,cls,text){
  const e=document.createElement(tag);
  if(cls)e.className=cls;
  if(text!==undefined)e.textContent=text;
  return e;
}
function btButton(label,cls,fn,disabled){
  const b=btEl('button','btn btn-sm '+cls,label);
  b.onclick=fn;b.disabled=!!disabled;
  return b;
}
async function loadBt(){
  let d;
  try{d=await (await fetch('/bluetooth/status')).json();}catch(e){return;}
  btData=d;
  const DASH=' '+String.fromCharCode(8212)+' ';
  const h=document.getElementById('btHead');
  h.textContent=d.view.text;
  h.className='aes-head aes-'+d.view.level;
  const al=document.getElementById('btAlerts');
  al.innerHTML='';
  if(d.pipewire)al.appendChild(btEl('div','aes-alert','PipeWire is running on this Pi. It takes over Bluetooth audio, so this player cannot use it. PipeWire comes with the desktop version of Raspberry Pi OS; units should run the Lite version.'));
  const st=document.getElementById('btSt');
  if(d.message&&d.message.text&&!d.busy){
    st.textContent=d.message.text+' ('+d.message.at+')';
    st.style.color=d.message.ok?'#155724':'#721c24';
  }
  const usable=d.installed&&(!d.adapter_checked||d.adapter.present);
  document.getElementById('btMain').style.display=usable?'':'none';
  const on=d.enabled===true;
  const ls=d.link?d.link.state:'none';
  document.getElementById('btOnBtn').style.display=on?'none':'';
  document.getElementById('btOffBtn').style.display=on?'':'none';
  document.getElementById('btReconBtn').style.display=(on&&(ls==='reconnecting'||ls==='slow'))?'':'none';
  document.getElementById('btOnPart').style.display=on?'':'none';
  ['btOnBtn','btOffBtn','btReconBtn','btScanBtn'].forEach(id=>{document.getElementById(id).disabled=d.busy;});
  const pl=document.getElementById('btPaired');
  pl.innerHTML='';
  if(!d.paired.length)pl.appendChild(btEl('div','audio-hint','None yet.'));
  d.paired.forEach(p=>{
    const row=btEl('div','bt-row');
    row.appendChild(btEl('span','bt-name',p.name||p.mac));
    row.appendChild(btEl('span','bt-meta',p.mac+DASH+(p.connected?'connected':'not connected')+(p.output?DASH+'the audio output':'')));
    if(!p.output&&p.audio!==false)row.appendChild(btButton('Use for audio','btn-green',()=>btUse(p.mac,p.name),d.busy));
    row.appendChild(btButton('Forget','btn-red',()=>btForget(p.mac,p.name,p.output),d.busy));
    pl.appendChild(row);
  });
  const fl=document.getElementById('btFound');
  fl.innerHTML='';
  if(d.scan&&d.scan.devices){
    fl.appendChild(btEl('div','audio-hint','Found at '+d.scan.at+':'));
    d.scan.devices.forEach(f=>{
      const row=btEl('div','bt-row');
      row.appendChild(btEl('span','bt-name',f.name||'(no name)'));
      row.appendChild(btEl('span','bt-meta',f.mac+DASH+(f.audio?'speaker or headphones':'may not be an audio device')));
      if(f.paired)row.appendChild(btEl('span','bt-meta','paired'));
      else row.appendChild(btButton('Pair',f.audio?'btn-blue':'btn-grey',()=>btPair(f.mac,f.name),d.busy));
      fl.appendChild(row);
    });
  }
  if(btWasBusy&&!d.busy)loadAudioDevices();     /* a finished action may change the output list */
  btWasBusy=d.busy;
  const poll=d.installed&&(on||d.busy);
  if(poll&&!btTimer)btTimer=setInterval(loadBt,3000);
  if(!poll&&btTimer){clearInterval(btTimer);btTimer=null;}
}
async function btAction(url,body){
  const st=document.getElementById('btSt');
  try{
    const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d=await r.json();
    st.textContent=d.message||(d.ok?'':'Not done');
    st.style.color=d.ok?'#555':'#721c24';
  }catch(e){st.textContent='Request failed';st.style.color='#721c24';}
  await loadBt();
  if(!btTimer)btTimer=setInterval(loadBt,3000);
}
function btEnable(on){
  if(!on&&btData&&btData.output&&!confirm('Turn Bluetooth off? The audio goes back to the output that was used before Bluetooth.'))return;
  btAction('/bluetooth/enable',{enabled:on});
}
function btPair(mac,name){btAction('/bluetooth/pair',{mac:mac,name:name||''});}
async function btUse(mac,name){
  const st=document.getElementById('btSt');
  st.textContent='Switching the audio to '+(name||mac)+String.fromCharCode(8230);
  st.style.color='#555';
  await btAction('/bluetooth/use',{mac:mac});
  loadAudioDevices();
}
async function btForget(mac,name,isOutput){
  const msg='Forget '+(name||mac)+'?'+(isOutput?' It is the audio output, so the audio goes back to the output that was used before Bluetooth.':'')+' Using it again means pairing it again.';
  if(!confirm(msg))return;
  await btAction('/bluetooth/forget',{mac:mac,name:name||''});
}

/* ── AES67 network audio ──
   One fetch on load. A unit without AES67 answers installed:false and the
   card stays hidden; with it, the card polls every 3 s. Everything shown is
   set with textContent - names and ids come from the network. */
let aesTimer=null,aesFormLoaded=false;
async function loadAes67(){
  try{
    const d=await (await fetch('/aes67/status')).json();
    const card=document.getElementById('aesCard');
    card.style.display='';
    if(!d.installed){
      document.getElementById('aesMain').style.display='none';
      document.getElementById('aesInstall').style.display='';
      if(aesTimer){clearInterval(aesTimer);aesTimer=null;}
      refreshAes67Install();
      return;
    }
    document.getElementById('aesMain').style.display='';
    document.getElementById('aesInstall').style.display=aesInstOpen?'':'none';
    if(!aesTimer)refreshAes67Install();
    const h=document.getElementById('aesHead');
    h.textContent=d.headline.text;
    h.className='aes-head aes-'+d.headline.level;
    const al=document.getElementById('aesAlerts');
    al.innerHTML='';
    (d.alerts||[]).forEach(a=>{
      const div=document.createElement('div');
      div.className='aes-alert';
      div.textContent=a.text;
      if(a.dismiss){
        const b=document.createElement('button');
        b.className='btn btn-grey btn-sm';
        b.style.marginLeft='8px';
        b.textContent='Dismiss';
        b.onclick=dismissAes67;
        div.appendChild(b);
      }
      al.appendChild(div);
    });
    const g=document.getElementById('aesGrid');
    g.innerHTML='';
    (d.rows||[]).forEach(r=>{
      const k=document.createElement('div');k.className='key';k.textContent=r[0];
      const v=document.createElement('div');v.textContent=r[1];
      g.appendChild(k);g.appendChild(v);
    });
    if(!aesFormLoaded&&d.settings){
      document.getElementById('aesCh').value=String(d.settings.channels);
      document.getElementById('aesOff').value=d.settings.offset_ms;
      aesFormLoaded=true;
    }
    if(!aesTimer)aesTimer=setInterval(loadAes67,3000);
  }catch(e){}
}
async function saveAes67(){
  const st=document.getElementById('aesSt');
  const ch=parseInt(document.getElementById('aesCh').value,10);
  const off=parseInt(document.getElementById('aesOff').value||'0',10);
  if(isNaN(off)||off<-1000||off>1000){st.textContent='The offset must be a whole number from -1000 to 1000.';return;}
  st.textContent='Saving'+String.fromCharCode(8230);
  try{
    const r=await fetch('/aes67/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({channels:ch,offset_ms:off})});
    if(!r.ok){st.textContent='Not saved: '+(await r.text());toast('AES67 settings not saved','err');return;}
    const d=await r.json();
    st.textContent=d.stream_restarted
      ?'Saved. The stream is being rewritten as '+(ch===1?'mono':'stereo')+' '+String.fromCharCode(8212)+' receivers have to pick it up again.'
      :'Saved.';
    toast('AES67 settings saved','ok');
    loadAes67();
  }catch(e){st.textContent='Request failed';}
}
async function dismissAes67(){
  try{await fetch('/aes67/dismiss',{method:'POST'});}catch(e){}
  loadAes67();
}
/* ── AES67 install from the web UI ──
   The card shows on every unit. Without AES67 it offers the install. The
   checks contact GitHub, so they run only when asked. While a build runs the
   page polls its log; in "dark" mode the player is stopped, this page loses
   its server, and the poll keeps trying until the player is back. */
let aesInstTimer=null,aesInstOpen=false,aesInstMode='dark',aesInstWasRunning=false;
const AES_WARN={
  keep:'The picture and this page normally keep running during the install, which takes 20 to 40 minutes. The build works the Pi hard, though, so the screen could still go dark, or this page stop answering, until it finishes. Do it outside opening hours.',
  dark:'This Pi has less than 3 GB of memory, so the player is stopped while AES67 builds: the screen and this page go dark for 20 to 40 minutes and come back by themselves when it finishes. Do it outside opening hours.'
};
function setInstSt(t,c){const s=document.getElementById('aesInstSt');s.textContent=t;s.style.color=c||'#555';}
function showAes67Install(){
  aesInstOpen=true;
  document.getElementById('aesInstall').style.display='';
  refreshAes67Install();
}
function renderAesChecks(checks){
  const g=document.getElementById('aesChecks');
  const mark={ok:String.fromCharCode(10004),warn:'!',fail:String.fromCharCode(10008)};
  const col={ok:'#155724',warn:'#856404',fail:'#721c24'};
  g.innerHTML='';
  (checks||[]).forEach(c=>{
    const k=document.createElement('div');k.className='key';k.textContent=c.name;
    const v=document.createElement('div');v.textContent=mark[c.level]+' '+c.text;v.style.color=col[c.level];
    g.appendChild(k);g.appendChild(v);
  });
}
async function refreshAes67Install(){
  let d;
  try{d=await (await fetch('/aes67/install')).json();}
  catch(e){
    if(aesInstWasRunning)setInstSt('The player is stopped while AES67 builds. This page reconnects by itself when it is back.','#856404');
    return;
  }
  aesInstMode=d.mode;
  const w=document.getElementById('aesInstWarn');
  w.textContent=AES_WARN[d.mode]||AES_WARN.dark;
  w.style.display='';
  document.getElementById('aesInstIntro').textContent=d.installed
    ?'Runs the AES67 installer again. On a healthy unit it only rebuilds the bridge, if a newer one came with an update, in seconds; after a kernel update it rebuilds everything.'
    :'AES67 sends the player audio to the network '+String.fromCharCode(8212)+' into a Q-SYS Core, for example '+String.fromCharCode(8212)+' instead of HDMI. It is not installed on this unit.';
  const btn=document.getElementById('aesInstBtn'),chk=document.getElementById('aesChkBtn'),rb=document.getElementById('aesRebootBtn');
  const log=document.getElementById('aesInstLog');
  rb.style.display='none';
  if(d.state!=='never'&&d.log){log.textContent=d.log;log.style.display='';log.scrollTop=log.scrollHeight;}
  if(!d.available){
    btn.style.display='none';chk.style.display='none';
    setInstSt('Installing from here is not set up on this unit yet: run the one-line installer again, then reload this page.','#856404');
    return;
  }
  if(d.state==='running'){
    aesInstWasRunning=true;
    aesInstOpen=true;     /* keep the outcome on screen once AES67 shows as installed */
    document.getElementById('aesInstall').style.display='';
    btn.style.display='none';chk.style.display='none';
    setInstSt('Installing'+(d.started_at?' since '+d.started_at:'')+String.fromCharCode(8230)+' This takes 20 to 40 minutes.','#155724');
    if(!aesInstTimer)aesInstTimer=setInterval(refreshAes67Install,5000);
    return;
  }
  if(aesInstTimer){clearInterval(aesInstTimer);aesInstTimer=null;}
  chk.style.display='';
  if(d.state==='done'){
    setInstSt('Installed'+(d.started_at?' (started '+d.started_at+')':'')+'. A reboot is recommended to finish.','#155724');
    rb.style.display='';
    if(aesInstWasRunning){aesInstWasRunning=false;toast('AES67 installed','ok');loadAes67();loadAudioDevices();}
  }else if(d.state==='failed'){
    aesInstWasRunning=false;
    setInstSt('The last install failed'+(d.exit_code!==null?' (exit '+d.exit_code+')':'')+'. The end of its log is below; all of it is in /var/log/saw-aes67-install.log.','#721c24');
  }else if(d.state==='interrupted'){
    aesInstWasRunning=false;
    setInstSt('The last install was cut off before it finished '+String.fromCharCode(8212)+' a power cut, perhaps. Run it again.','#721c24');
  }
}
async function checkAes67Install(){
  setInstSt('Checking'+String.fromCharCode(8230)+' (up to 10 seconds)');
  try{
    const d=await (await fetch('/aes67/install?check=1')).json();
    renderAesChecks(d.checks);
    const blocked=(d.checks||[]).some(c=>c.level==='fail');
    const btn=document.getElementById('aesInstBtn');
    btn.textContent=d.installed?'Run the AES67 installer':'Install AES67';
    btn.style.display=(blocked||d.state==='running')?'none':'';
    setInstSt(blocked?'This unit cannot install AES67 as it is '+String.fromCharCode(8212)+' see above.':'Ready.',blocked?'#721c24':'#155724');
  }catch(e){setInstSt('The check failed','#721c24');}
}
async function startAes67Install(){
  const NL=String.fromCharCode(10);
  const msg=['Install AES67 on this unit now?','',
    'It takes 20 to 40 minutes and needs internet access throughout.',
    aesInstMode==='dark'
      ?'The screen and this page go DARK until it finishes.'
      :'The picture should keep playing, but the screen could still go dark until it finishes.',
    'It also holds the Linux kernel at its current version, runs the CPU at full speed, and keeps the web page of the AES67 software off the network.',
    'A reboot is recommended afterwards.'].join(NL);
  if(!confirm(msg))return;
  setInstSt('Starting'+String.fromCharCode(8230));
  try{
    const r=await fetch('/aes67/install',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirm:true})});
    const d=await r.json();
    if(!d.ok){
      setInstSt(d.message||'The install could not start','#721c24');
      if(d.checks)renderAesChecks(d.checks);
      toast('AES67 install not started','err');
      return;
    }
    aesInstWasRunning=true;
    aesInstOpen=true;
    aesInstMode=d.mode;
    document.getElementById('aesInstBtn').style.display='none';
    toast('AES67 install started','ok');
    refreshAes67Install();
    if(!aesInstTimer)aesInstTimer=setInterval(refreshAes67Install,5000);
  }catch(e){setInstSt('Request failed','#721c24');}
}
async function toggleSdp(){
  const pre=document.getElementById('aesSdp');
  if(pre.style.display!=='none'){pre.style.display='none';return;}
  pre.textContent='Loading'+String.fromCharCode(8230);
  pre.style.display='';
  try{pre.textContent=await (await fetch('/aes67/sdp')).text();}
  catch(e){pre.textContent='Request failed';}
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
    st.textContent=r.ok?'✔ Active: '+(sel.value==='aes67'?'AES67 network':sel.value):'Error setting device';
    loadAes67();
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
/* -- Software update from GitHub -- */
let _updLatest=null;
async function loadInstalledVersion(){
  try{
    const d=await(await fetch('/update/version')).json();
    document.getElementById('updInstalled').textContent='v'+d.installed_version;
  }catch(e){}
}
async function checkUpdate(){
  const st=document.getElementById('updSt');
  const ab=document.getElementById('updApplyBtn');
  const cb=document.getElementById('updCheckBtn');
  ab.style.display='none';_updLatest=null;cb.disabled=true;
  st.style.color='#555';st.textContent='Checking GitHub...';
  try{
    const d=await(await fetch('/update/check')).json();
    if(!d.ok){st.style.color='#b06d00';st.textContent='Could not check for updates: '+d.error;return;}
    if(d.update_available){
      _updLatest=d.latest_version;
      let t='Version v'+d.latest_version+' is available (this unit runs v'+d.installed_version+')';
      if(d.released)t+=', published '+d.released;
      t+='.';
      if(d.service_changed)t+=' This release also changes the service file, which the web UI cannot install: after updating, run the install one-liner over SSH to apply it.';
      st.style.color='#155724';st.textContent=t;ab.style.display='';
    }else if(d.newer_than_published){
      st.style.color='#555';
      st.textContent='This unit runs v'+d.installed_version+', which is newer than the published release (v'+d.latest_version+'). Nothing to do.';
    }else{
      let t='Up to date: v'+d.installed_version+' is the latest published release.';
      if(d.build_matches===false)t+=' Note: the copy on this unit differs from the published build.';
      st.style.color='#155724';st.textContent=t;
    }
  }catch(e){st.style.color='#b00020';st.textContent='Check failed: the unit did not respond.';}
  finally{cb.disabled=false;}
}
async function applyUpdate(){
  if(!_updLatest)return;
  const NL=String.fromCharCode(10);
  if(!confirm('Update this unit to v'+_updLatest+'?'+NL+NL+'The player restarts, so video stops for about 10 seconds. The current version is kept as a backup.'))return;
  const st=document.getElementById('updSt');
  const ab=document.getElementById('updApplyBtn');
  const cb=document.getElementById('updCheckBtn');
  ab.disabled=true;cb.disabled=true;
  st.style.color='#555';st.textContent='Downloading and verifying v'+_updLatest+'...';
  try{
    const r=await fetch('/update/apply',{method:'POST'});
    const d=await r.json().catch(()=>({ok:false,message:'unexpected response from the unit'}));
    if(r.status===207){
      st.style.color='#b06d00';st.textContent='Installed, but the service did not restart: '+d.message;
      return;
    }
    if(!r.ok||!d.ok){
      st.style.color='#b00020';st.textContent='Update not applied: '+d.message;
      toast('Update not applied','err',8000);
      ab.disabled=false;cb.disabled=false;return;
    }
    st.textContent='Installed v'+_updLatest+'. Restarting - waiting for the unit to come back...';
    toast('Update installed, restarting','ok',8000);
    waitForVersion(_updLatest,st);
  }catch(e){st.style.color='#b00020';st.textContent='Request failed.';ab.disabled=false;cb.disabled=false;}
}
/* Reload only once the NEW version answers. The old process can still reply
   for a moment before systemd stops it, so "the page responds" is not enough. */
async function waitForVersion(want,st){
  const sleep=ms=>new Promise(res=>setTimeout(res,ms));
  const t0=Date.now();
  await sleep(4000);
  while(Date.now()-t0<120000){
    try{
      const r=await fetch('/update/version',{cache:'no-store'});
      if(r.ok){
        const d=await r.json();
        if(d.installed_version===want){st.textContent='v'+want+' is running. Reloading...';location.reload();return;}
      }
    }catch(e){}
    await sleep(2000);
  }
  st.style.color='#b00020';
  st.textContent='v'+want+' was installed but has not reported back within 2 minutes. Check the unit over SSH. The previous version is saved as /home/pi/saw_player.py.bak.';
}
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
  fetchSlotStatus();loadAudioDevices();loadDisplayModes();fetchPlayerName();fetchMaxUpload();loadInstalledVersion();loadAes67();loadBt();
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
  <div id="si_ram" style="font-size:0.82em;color:#555;margin-top:10px">RAM-pinned clips: starting&hellip;</div>
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

<!-- AES67 network audio: shown only on units with install-aes67.sh -->
<div class="card" id="aesCard" style="display:none">
  <h3>&#127760; AES67 Network Audio <span style="font-size:0.65em;font-weight:normal;color:#a66b00">EXPERIMENTAL</span></h3>
  <!-- Not installed: the way to install it. Installed: the same panel, for a re-run. -->
  <div id="aesInstall" style="display:none">
    <p class="audio-hint" id="aesInstIntro">AES67 sends the player's audio to the network &mdash; into a Q-SYS Core,
       for example &mdash; instead of HDMI. It is not installed on this unit.</p>
    <div id="aesInstWarn" class="aes-alert" style="display:none"></div>
    <div id="aesChecks" class="aes-grid"></div>
    <button class="btn btn-grey btn-sm" id="aesChkBtn" onclick="checkAes67Install()">Check this unit</button>
    <button class="btn btn-orange btn-sm" id="aesInstBtn" onclick="startAes67Install()" style="display:none;margin-left:6px">Install AES67</button>
    <button class="btn btn-grey btn-sm" id="aesRebootBtn" onclick="doReboot()" style="display:none;margin-left:6px">Reboot now</button>
    <div id="aesInstSt" style="font-size:0.85em;margin-top:8px"></div>
    <pre id="aesInstLog" style="display:none;height:220px;overflow-y:auto;background:#1a1a2e;color:#a8d8a8;padding:8px;border-radius:6px;font-size:0.72em;white-space:pre-wrap;word-break:break-all;margin:8px 0 0"></pre>
  </div>
  <div id="aesMain">
  <div id="aesHead" class="aes-head aes-off">&hellip;</div>
  <div id="aesAlerts"></div>
  <div id="aesGrid" class="aes-grid"></div>
  <hr class="sep">
  <label for="aesCh" style="font-size:0.82em;color:#555">Channels</label><br>
  <select id="aesCh" style="width:100%;max-width:340px;padding:5px;margin-bottom:8px">
    <option value="2">Stereo &mdash; channels 1 and 2</option>
    <option value="1">Mono &mdash; left and right mixed to channel 1</option>
  </select><br>
  <label for="aesOff" style="font-size:0.82em;color:#555">Lip-sync offset (ms)</label><br>
  <input type="number" id="aesOff" min="-1000" max="1000" step="1" style="width:110px;padding:5px;margin-bottom:4px">
  <p class="audio-hint">The player already holds the picture back for the bridge's own delay (about 90 ms).
     Use the offset for the rest of the chain: positive plays the audio later, negative earlier.
     Changing the channels changes the stream's format, so receivers have to pick it up again.</p>
  <button class="btn btn-green btn-sm" onclick="saveAes67()">Save</button>
  <button class="btn btn-grey btn-sm" onclick="toggleSdp()" style="margin-left:6px">Show SDP</button>
  <button class="btn btn-grey btn-sm" onclick="showAes67Install()" style="margin-left:6px">Update AES67 software&hellip;</button>
  <div id="aesSt" style="font-size:0.82em;margin-top:6px;color:#155724"></div>
  <pre id="aesSdp" style="display:none;background:#f4f5f7;padding:8px;border-radius:6px;font-size:0.75em;white-space:pre-wrap;word-break:break-all;margin:8px 0 0"></pre>
  </div>
</div>

<!-- Bluetooth audio -->
<div class="card" id="btCard">
  <h3>&#127911; Bluetooth Audio</h3>
  <div id="btHead" class="aes-head aes-off">&hellip;</div>
  <div id="btAlerts"></div>
  <div id="btMain" style="display:none">
    <button class="btn btn-green btn-sm" id="btOnBtn" onclick="btEnable(true)">Turn Bluetooth on</button>
    <button class="btn btn-grey btn-sm" id="btOffBtn" onclick="btEnable(false)">Turn Bluetooth off</button>
    <button class="btn btn-blue btn-sm" id="btReconBtn" onclick="btAction('/bluetooth/reconnect',{})" style="margin-left:6px">Reconnect now</button>
    <div id="btOnPart">
      <div class="bt-sub">Paired devices</div>
      <div id="btPaired"></div>
      <div class="bt-sub">Add a device</div>
      <p class="audio-hint">Put the speaker or headphones in pairing mode first. A scan takes 15 seconds.
         The speaker chosen for audio reconnects by itself after a reboot, and whenever it drops out.</p>
      <button class="btn btn-blue btn-sm" id="btScanBtn" onclick="btAction('/bluetooth/scan',{})">Scan for devices</button>
      <div id="btFound"></div>
    </div>
  </div>
  <div id="btSt" style="font-size:0.85em;margin-top:8px"></div>
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
    <button class="btn btn-red btn-sm" id="d_{{ slot_id }}" title="Delete this clip"
      onclick="deleteClip('{{ slot_url.replace('/upload/','/delete/') }}','{{ slot_label }}',{{ 'true' if slot_id == 'f_loop' else 'false' }})">&#128465;</button>
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
    <button class="btn btn-red btn-sm" id="d_f_ov" title="Delete this clip"
      onclick="deleteClip('/delete/override','Override',false)">&#128465;</button>
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

<!-- ── Software update ── -->
<div class="card">
  <h3>⚙️ Software Update</h3>
  <div style="font-size:0.9em;margin:0 0 10px">Installed version: <b id="updInstalled">&hellip;</b></div>

  <div style="font-weight:600;font-size:0.9em;margin:0 0 4px">Update from GitHub</div>
  <p style="font-size:0.85em;color:#555;margin:0 0 8px">
    Checks the published release. If it is newer than this unit, it is downloaded, its checksum
    verified, validated and installed, with the current version kept as a backup. The service
    restarts (about 10s without video). Needs internet access to GitHub.
  </p>
  <button class="btn btn-blue btn-sm" id="updCheckBtn" onclick="checkUpdate()">Check for updates</button>
  <button class="btn btn-orange btn-sm" id="updApplyBtn" onclick="applyUpdate()" style="margin-left:6px;display:none">Update now</button>
  <div id="updSt" style="font-size:0.85em;margin-top:6px;color:#555"></div>

  <hr class="sep">

  <div style="font-weight:600;font-size:0.9em;margin:0 0 4px">Upload a script manually</div>
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
        if device == AES67_DEVICE and not aes67_installed():
            return ("AES67 is not installed on this unit", 400)
        if "bluealsa" in device:
            if not bt_mac_from_device(device):
                return ("Not a Bluetooth audio device", 400)
            if not bt_enabled():
                return ("Bluetooth is off - turn it on first", 400)
        _apply_audio_device(device)
        return ("", 204)
    except Exception:
        logger.exception("Error in /audio_device POST")
        return ("Bad request", 400)

def _apply_audio_device(device: str):
    """
    Make `device` the audio output: persist it, restart mpv on it, and tell
    the AES67 and Bluetooth managers. Used by the Audio Output card, and by
    Bluetooth when it hands the audio back (turned off, or the device in use
    forgotten). Choosing Bluetooth remembers the output it replaces, which is
    where the audio goes back to.
    """
    with state_lock:
        previous = state.audio_device
        state.audio_device = device
    save_audio_device(device)
    if bt_mac_from_device(device) and not bt_mac_from_device(previous):
        _bt_config_update(fallback=previous)
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
    # Switching to or from AES67 turns its stream on or off, and to or from a
    # Bluetooth device starts or stops its reconnecting: check both now rather
    # than at their next pass.
    aes67_manager.kick()
    bt_manager.kick()

def _as_int(value):
    """An int from JSON or form input, or None. Not bools (True == 1), not floats."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"\s*[+-]?[0-9]{1,6}\s*", value):
        return int(value)
    return None

@app.route("/aes67/status")
def aes67_status_route():
    """Cached by the manager - the UI polls this, so it never waits on the daemon."""
    if not aes67_installed():
        return jsonify({"installed": False})
    aes67_manager.start()       # no-op once running; starts it if installed since boot
    return jsonify(aes67_manager.status())

@app.route("/aes67/settings", methods=["POST"])
def aes67_settings_route():
    """Channels (1 or 2) and lip-sync offset (ms). The offset applies to mpv at
    once; a channel change rewrites the stream, which receivers re-learn."""
    if not aes67_installed():
        return ("AES67 is not installed on this unit", 404)
    data = request.get_json(silent=True) or {}
    channels = _as_int(data.get("channels"))
    offset   = _as_int(data.get("offset_ms"))
    if channels not in (1, 2):
        return ("channels must be 1 (mono) or 2 (stereo)", 400)
    if offset is None or abs(offset) > AES67_OFFSET_MAX_MS:
        return (f"offset_ms must be a whole number from -{AES67_OFFSET_MAX_MS} to {AES67_OFFSET_MAX_MS}", 400)
    old = aes67_settings()
    new = {"channels": channels, "offset_ms": offset}
    config["aes67"] = new
    save_config(dict(config))
    logger.info("AES67 settings: %s, lip-sync offset %+d ms",
                "mono" if channels == 1 else "stereo", offset)
    with state_lock:
        selected = state.audio_device == AES67_DEVICE
    if selected and offset != old["offset_ms"]:
        mpv_set_property("audio-delay", aes67_audio_delay(aes67_read_bridge(), offset))
    aes67_manager.kick()
    return jsonify({"ok": True, "settings": new,
                    "stream_restarted": selected and channels != old["channels"]})

@app.route("/aes67/sdp")
def aes67_sdp_route():
    """The stream's SDP as the daemon publishes it - what a receiver imports."""
    if not aes67_installed():
        return ("AES67 is not installed on this unit", 404)
    ok, text = aes67_manager.fetch_sdp()
    return (text, 200 if ok else 503, {"Content-Type": "text/plain; charset=utf-8"})

@app.route("/aes67/dismiss", methods=["POST"])
def aes67_dismiss_route():
    aes67_manager.dismiss_gm_alert()
    return ("", 204)

@app.route("/aes67/install", methods=["GET"])
def aes67_install_info_route():
    """The install panel. ?check=1 adds the pre-install checks, which contact
    GitHub - so only when somebody asks, never on a page load."""
    out = {"available": os.path.exists(AES67_WEBINSTALL), "installed": aes67_installed(),
           "mode": aes67_install_mode(_mem_total_mb())}
    out.update(aes67_install_state(active=_aes67_install_active()))
    if request.args.get("check"):
        out["checks"] = aes67_install_checks(*_aes67_install_inputs(), installed=out["installed"])
    return jsonify(out)

@app.route("/aes67/install", methods=["POST"])
def aes67_install_route():
    """Start install-aes67.sh. Needs {"confirm": true}, and runs the checks
    again here rather than trusting the page's copy of them."""
    data = request.get_json(silent=True) or {}
    if data.get("confirm") is not True:
        return jsonify({"ok": False, "message": "Not confirmed"}), 400
    if not os.path.exists(AES67_WEBINSTALL):
        return jsonify({"ok": False, "message": "Installing from here is not set up on this unit - "
                                               "run the one-line installer again."}), 404
    if aes67_install_state(active=_aes67_install_active())["state"] == "running":
        return jsonify({"ok": False, "message": "An AES67 install is already running"}), 409
    checks = aes67_install_checks(*_aes67_install_inputs(), installed=aes67_installed())
    failed = [c["text"] for c in checks if c["level"] == "fail"]
    if failed:
        return jsonify({"ok": False, "message": " ".join(failed), "checks": checks}), 409
    ok, message, mode = aes67_install_start()
    return jsonify({"ok": ok, "message": message, "mode": mode}), (200 if ok else 500)

# ── Bluetooth ────────────────────────────────────────────────────────────────
# Every action is queued for BluetoothManager's thread and answered at once
# (202); the panel follows it through /bluetooth/status, which is the manager's
# cached view and never waits on Bluetooth.

def _bt_refuse():
    """A reason the request cannot be queued, or None."""
    if not bt_manager.status()["installed"]:
        return jsonify({"ok": False, "message": "The Bluetooth software is not installed on this unit"}), 404
    return None

def _bt_queue(kind: str, **args):
    ok, text = bt_manager.submit(kind, **args)
    return jsonify({"ok": ok, "message": text}), (202 if ok else 409)

@app.route("/bluetooth/status")
def bluetooth_status_route():
    return jsonify(bt_manager.status())

@app.route("/bluetooth/enable", methods=["POST"])
def bluetooth_enable_route():
    refused = _bt_refuse()
    if refused:
        return refused
    on = (request.get_json(silent=True) or {}).get("enabled")
    if not isinstance(on, bool):
        return jsonify({"ok": False, "message": "enabled must be true or false"}), 400
    return _bt_queue("enable" if on else "disable")

@app.route("/bluetooth/scan", methods=["POST"])
def bluetooth_scan_route():
    refused = _bt_refuse()
    if refused:
        return refused
    if not bt_enabled():
        return jsonify({"ok": False, "message": "Turn Bluetooth on first"}), 409
    return _bt_queue("scan")

def _bt_mac_request():
    """(mac, name) from the request body, or (None, None)."""
    data = request.get_json(silent=True) or {}
    name = data.get("name") if isinstance(data.get("name"), str) else ""
    return bt_mac(data.get("mac")), name.strip()[:64]

@app.route("/bluetooth/pair", methods=["POST"])
def bluetooth_pair_route():
    refused = _bt_refuse()
    if refused:
        return refused
    mac, name = _bt_mac_request()
    if not mac:
        return jsonify({"ok": False, "message": "Not a Bluetooth address"}), 400
    if not bt_enabled():
        return jsonify({"ok": False, "message": "Turn Bluetooth on first"}), 409
    return _bt_queue("pair", mac=mac, name=name or mac)

@app.route("/bluetooth/forget", methods=["POST"])
def bluetooth_forget_route():
    refused = _bt_refuse()
    if refused:
        return refused
    mac, name = _bt_mac_request()
    if not mac:
        return jsonify({"ok": False, "message": "Not a Bluetooth address"}), 400
    return _bt_queue("forget", mac=mac, name=name or mac)

@app.route("/bluetooth/reconnect", methods=["POST"])
def bluetooth_reconnect_route():
    refused = _bt_refuse()
    if refused:
        return refused
    if not bt_enabled():
        return jsonify({"ok": False, "message": "Turn Bluetooth on first"}), 409
    return _bt_queue("reconnect")

@app.route("/bluetooth/use", methods=["POST"])
def bluetooth_use_route():
    """Make a paired speaker the audio output - the same as choosing it under
    Audio Output, which restarts mpv on it."""
    refused = _bt_refuse()
    if refused:
        return refused
    mac, _name = _bt_mac_request()
    if not mac:
        return jsonify({"ok": False, "message": "Not a Bluetooth address"}), 400
    if not bt_enabled():
        return jsonify({"ok": False, "message": "Turn Bluetooth on first"}), 409
    if mac not in {d["mac"] for d in bt_manager.paired_audio()}:
        return jsonify({"ok": False, "message": "That device is not paired"}), 409
    _apply_audio_device(bt_device_string(mac))
    return jsonify({"ok": True, "message": "The audio now goes to it"})

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
        ram_pinner.request("upload %s" % video_type)   # unpin old, pin new
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

@app.route("/delete/<video_type>", methods=["POST"])
@app.route("/delete/<video_type>/<int:trigger_id>", methods=["POST"])
def delete_video(video_type, trigger_id=None):
    """
    Delete the video in one slot.

    Paths come from the same fixed mapping the download route uses. Nothing
    from the request ever reaches the filesystem, so there is no filename to
    traverse out of VIDEO_DIR with.

    mpv is deliberately left running. If the deleted file is the one currently
    playing, mpv still holds the inode open and keeps looping it, so an exhibit
    carries on until a replacement is uploaded. Restarting mpv here would
    instead guarantee an immediately black screen and leave the watchdog
    retrying a file that no longer exists. The response reports this so the UI
    can say so rather than implying the clip stopped.

    A missing trigger clip is handled safely elsewhere (play_trigger logs and
    returns), and _desired_video falls back to the loop video, so the only
    genuinely consequential deletion is the loop itself - which the UI warns
    about separately.
    """
    if video_type == "loop":
        path = LOOP_VIDEO
    elif video_type == "override":
        with state_lock:
            path = state.override_video
    elif video_type == "trigger" and trigger_id in (1, 2, 3):
        path = TRIGGER_VIDEOS[GPIO_PINS[trigger_id - 1]]
    else:
        return ("Unknown type", 404)

    if not os.path.exists(path):
        return ("File not found", 404)

    with state_lock:
        playing = (state.current_trigger_video if state.trigger_running
                   else state.current_video)
    was_playing = (playing == os.path.basename(path))

    try:
        os.remove(path)
    except Exception:
        logger.exception("Failed to delete %s", path)
        return ("Could not delete file", 500)

    logger.warning("Deleted video via web UI: %s%s", path,
                   " (was playing)" if was_playing else "")
    # A pinned clip keeps its inode - and its RAM and disk space - alive until
    # it is unpinned, so release it straight away.
    ram_pinner.request("delete %s" % video_type)
    return jsonify({
        "deleted":     True,
        "slot":        video_type,
        "file":        os.path.basename(path),
        "was_playing": was_playing,
    })

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
            "ram_pin":   ram_pinner.status(),
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

    # Same path as the GitHub update; responses are unchanged from before the
    # logic was shared - plain-text reason on failure, JSON on success.
    ok, message, code = _deploy_script(content, "web upload")
    if not ok or code != 200:
        return (message, code)
    return jsonify({"status": "ok", "message": message})

@app.route("/update/version")
def update_version():
    """Installed version. Cheap - the UI polls it while a unit restarts."""
    return jsonify({"installed_version": PLAYER_VERSION})

@app.route("/update/check")
def update_check_route():
    """Compare with the published release. Makes an outbound HTTPS request."""
    return jsonify(update_check())

@app.route("/update/apply", methods=["POST"])
def update_apply_route():
    """Download, verify and install the published release, if it is newer."""
    ok, message, code = update_apply()
    return jsonify({"ok": ok, "message": message}), code

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
# RAM PINNING
# ══════════════════════════════════════════════════════════════════════════════
#
# Evaluated before this was written (2026-10-26):
#   * Reading a file does not wear an SD card. Flash endurance is consumed by
#     program/erase cycles - writes - not by reads.
#   * Linux already keeps recently read file data in otherwise-free RAM (the
#     page cache), so after its first pass a looping clip is normally served
#     from RAM anyway.
#
# So this is a RELIABILITY feature, not a wear feature. It turns "probably in
# RAM" into "guaranteed in RAM": a pinned clip cannot be evicted under memory
# pressure, and keeps playing even if the card turns slow or starts failing.
#
# Each clip is mapped READ-ONLY and SHARED, then mlock()ed. That locks the page
# cache's own pages for the file, so:
#   * there is no second copy of the clip in memory;
#   * locked pages can never be swapped out. A tmpfs RAM disk's pages CAN be,
#     and on Bookworm the swap file lives on the SD card - so a RAM disk would
#     start writing to the card in exactly the low-memory case it should cover;
#   * mpv keeps opening the same path, so upload and delete need no syncing.
#
# The mapping MUST be read-only and MAP_SHARED. mlock() on a writable private
# mapping (Python's mmap ACCESS_COPY, for instance) breaks copy-on-write for
# every page and silently builds a full anonymous duplicate of the file. That
# is why this calls libc's mmap directly: Python's mmap module gives no way to
# get the address of a read-only mapping.
#
# Requires LimitMEMLOCK in saw-player.service - a service's default lock limit
# is only a few MB. If it is too low, the System panel says so and how to fix
# it, and the player carries on exactly as before. Nothing here is needed for
# playback to work.
# ══════════════════════════════════════════════════════════════════════════════

_MB         = 1024 * 1024
_PROT_READ  = getattr(mmap, "PROT_READ", 1)
_MAP_SHARED = getattr(mmap, "MAP_SHARED", 1)
_MAP_FAILED = ctypes.c_void_p(-1).value


class _PinnedFile:
    """One locked, read-only, shared mapping of a clip."""
    __slots__ = ("slot", "path", "size", "addr")

    def __init__(self, slot, path, size, addr):
        self.slot = slot
        self.path = path
        self.size = size
        self.addr = addr


class RamPinner:
    """
    Keeps the video clips locked in RAM, within a budget.

    Priority is loop, then override, then triggers 1-3. A clip that does not fit
    is skipped and the smaller ones after it are still tried, so the budget is
    used as fully as it can be.

    All the work happens on one background thread. request() only schedules a
    rebalance, so nothing on a web request ever waits for a large file to be
    read in from the card, and a burst of requests coalesces into one pass.

    Locking: this class has its own lock and never holds state_lock across any
    slow work (constraint 13). _candidates() takes state_lock briefly while
    holding self._lock; nothing ever takes them in the opposite order.
    """

    def __init__(self):
        self._lock   = threading.Lock()
        self._event  = threading.Event()
        self._reason = "startup"
        self._pinned = {}                   # slot -> _PinnedFile
        self._held   = None                 # why pinning is paused, or None
        self._libc   = self._load_libc()
        self._status = {"enabled": RAM_PIN_ENABLED, "state": "not started",
                        "message": "", "pinned": [], "skipped": []}

    # -- public --------------------------------------------------------------
    def start(self):
        threading.Thread(target=self._run, daemon=True, name="ram-pin").start()

    def request(self, reason: str):
        """Schedule a rebalance. Returns immediately."""
        self._reason = reason
        self._event.set()

    def status(self) -> dict:
        # Replaced wholesale on every rebalance and never mutated afterwards,
        # so handing out the reference is safe without the lock.
        return self._status

    def rebalance(self, reason: str = "manual"):
        """Unpin everything, measure memory afresh, pin in priority order."""
        with self._lock:
            st = self._compute(reason)
            self._status = st
        self._log(st)

    def hold(self, why: str):
        """
        Release every clip and pin nothing until release(): hands the memory to
        something that needs it more, such as building AES67 with the picture
        still playing. Done here, not on the thread, so the memory is free when
        this returns. Clips keep playing from the page cache or the card.
        """
        with self._lock:
            self._held = why
            st = self._compute("paused for " + why)
            self._status = st
        self._log(st)

    def release(self):
        """Undo hold(): pin again, on the background thread."""
        with self._lock:
            was, self._held = self._held, None
        if was:
            self.request(was + " finished")

    # -- background thread ---------------------------------------------------
    def _run(self):
        # Let mpv allocate its decode buffers first, so they are already counted
        # against available memory when the budget is worked out.
        time.sleep(RAM_PIN_STARTUP_DELAY_S)
        self._event.set()
        while True:
            self._event.wait()
            self._event.clear()
            try:
                self.rebalance(self._reason)
            except Exception:
                logger.exception("RAM pin: rebalance failed")

    # -- the actual work (caller holds self._lock) ----------------------------
    def _compute(self, reason: str) -> dict:
        # Unpinning does not evict anything from the page cache, so re-locking
        # an unchanged clip a moment later is instant - no reads from the card.
        self._unpin_all()

        st = {"enabled": RAM_PIN_ENABLED, "reason": reason,
              "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
              "pinned": [], "skipped": [], "pinned_mb": 0.0, "message": ""}
        if not RAM_PIN_ENABLED:
            st.update(state="disabled", message="switched off (RAM_PIN_ENABLED)")
            return st
        if self._libc is None or resource is None:
            st.update(state="unavailable", message="not supported on this system")
            return st
        if self._held:
            st.update(state="paused", message="released during %s - pinned again after it" % self._held)
            return st

        total, avail = self._meminfo()
        reserve    = max(RAM_PIN_RESERVE_MIN_MB * _MB,
                         int(total * RAM_PIN_RESERVE_FRACTION))
        mem_budget = max(0, avail - reserve)
        limit      = self._memlock_limit()
        budget     = mem_budget if limit is None else min(mem_budget, limit)
        by_rlimit  = limit is not None and limit < mem_budget
        st.update(mem_total_mb=round(total / _MB),
                  mem_available_mb=round(avail / _MB),
                  reserve_mb=round(reserve / _MB),
                  budget_mb=round(budget / _MB),
                  memlock_limit_mb=None if limit is None else round(limit / _MB, 1))

        used, limit_hit = 0, False
        for slot, path in self._candidates():
            if not path or not os.path.isfile(path):
                continue                                  # empty slot
            size = os.path.getsize(path)
            if size <= 0:
                continue
            entry = {"slot": slot, "file": os.path.basename(path),
                     "mb": round(size / _MB, 1)}
            if used + size > budget:
                if by_rlimit and used + size <= mem_budget:
                    entry["why"] = "service lock limit too low"
                    limit_hit = True
                else:
                    entry["why"] = "does not fit in the RAM budget"
                st["skipped"].append(entry)
                continue
            try:
                pf = self._pin_file(slot, path)
            except OSError as e:
                if e.errno in (errno.ENOMEM, errno.EPERM, errno.EAGAIN) and limit is not None:
                    limit_hit = True
                entry["why"] = "could not lock (%s)" % (e.strerror or e)
                st["skipped"].append(entry)
                continue
            self._pinned[slot] = pf
            used += pf.size
            st["pinned"].append(entry)

        st["pinned_mb"] = round(used / _MB, 1)
        if limit_hit:
            st["state"] = "limited"
            st["message"] = ("the service's memory lock limit (%s MB) is too low - "
                             "run 'sudo bash update.sh --service' to apply LimitMEMLOCK"
                             % st["memlock_limit_mb"])
        elif os.path.isfile(LOOP_VIDEO) and "loop" not in self._pinned:
            st["state"] = "partial"
            st["message"] = "loop.mp4 is not pinned - it plays from the card, as before"
        else:
            st["state"] = "ok"
        return st

    def _candidates(self) -> list:
        with state_lock:
            override = state.override_video
        return [
            ("loop",     LOOP_VIDEO),
            ("override", override),
            ("trigger1", TRIGGER_VIDEOS.get(GPIO_PINS[0], "")),
            ("trigger2", TRIGGER_VIDEOS.get(GPIO_PINS[1], "")),
            ("trigger3", TRIGGER_VIDEOS.get(GPIO_PINS[2], "")),
        ]

    # -- syscalls ------------------------------------------------------------
    @staticmethod
    def _load_libc():
        try:
            libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6",
                               use_errno=True)
            libc.mmap.restype     = ctypes.c_void_p
            libc.mmap.argtypes    = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_long]
            libc.munmap.argtypes  = [ctypes.c_void_p, ctypes.c_size_t]
            libc.mlock.argtypes   = [ctypes.c_void_p, ctypes.c_size_t]
            libc.munlock.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
            return libc
        except Exception:
            return None

    def _pin_file(self, slot: str, path: str) -> _PinnedFile:
        fd = os.open(path, os.O_RDONLY)
        try:
            size = os.fstat(fd).st_size
            if size <= 0:
                raise OSError(errno.EINVAL, "empty file")
            # Read-only + MAP_SHARED: see the section notes for why this matters.
            addr = self._libc.mmap(None, size, _PROT_READ, _MAP_SHARED, fd, 0)
            if addr is None or addr == _MAP_FAILED:
                err = ctypes.get_errno()
                raise OSError(err, os.strerror(err))
        finally:
            os.close(fd)        # the mapping keeps the file alive on its own
        # mlock faults in every page - reading from the card once if the clip
        # is not already cached - and then holds them in RAM.
        if self._libc.mlock(addr, size) != 0:
            err = ctypes.get_errno()
            self._libc.munmap(addr, size)
            raise OSError(err, os.strerror(err))
        return _PinnedFile(slot, path, size, addr)

    def _unpin_file(self, pf: _PinnedFile):
        try:
            self._libc.munlock(pf.addr, pf.size)
        finally:
            self._libc.munmap(pf.addr, pf.size)

    def _unpin_all(self):
        for pf in list(self._pinned.values()):
            try:
                self._unpin_file(pf)
            except Exception:
                logger.exception("RAM pin: could not unpin %s", pf.path)
        self._pinned.clear()

    @staticmethod
    def _meminfo():
        vals = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts:
                    vals[key] = int(parts[0]) * 1024
        return vals["MemTotal"], vals["MemAvailable"]

    @staticmethod
    def _memlock_limit():
        """Lift the soft lock limit to the hard limit; return it. None = unlimited."""
        soft, hard = resource.getrlimit(resource.RLIMIT_MEMLOCK)
        if soft != hard:
            try:
                resource.setrlimit(resource.RLIMIT_MEMLOCK, (hard, hard))
                soft = hard
            except (ValueError, OSError):
                pass
        return None if soft == resource.RLIM_INFINITY else soft

    @staticmethod
    def _log(st: dict):
        if st.get("state") in ("disabled", "unavailable", "paused"):
            logger.info("RAM pin: %s", st.get("message"))
            return
        pinned = ", ".join("%s (%s MB)" % (p["file"], p["mb"])
                           for p in st["pinned"]) or "nothing"
        msg = ("RAM pin [%s]: pinned %s - %s of %s MB budget"
               % (st.get("reason"), pinned, st.get("pinned_mb"), st.get("budget_mb")))
        if st["skipped"]:
            msg += "; not pinned: " + ", ".join(
                "%s (%s)" % (x["file"], x["why"]) for x in st["skipped"])
        if st.get("message"):
            msg += "; " + st["message"]
        if st.get("state") in ("limited", "partial"):
            logger.warning(msg)
        else:
            logger.info(msg)


ram_pinner = RamPinner()

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
    logger.info("Stage Audio Works Video Player v%d starting", PLAYER_VERSION)

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

    # Lock the clips into RAM in the background - see RAM PINNING.
    ram_pinner.start()

    # Keep the AES67 stream matching the settings - on units with AES67 only.
    aes67_manager.start()

    # Bluetooth: reconnect the speaker that is the audio output, and run what
    # the web UI asks for. Touches nothing on a unit where it was never turned on.
    bt_manager.start()

    # An AES67 build with the picture kept up, still running from before this
    # restart: keep the memory free for it, and pin again when it ends.
    try:
        _st = aes67_install_state(active=_aes67_install_active())
        if _st["state"] == "running" and _st.get("run_mode") == "keep":
            ram_pinner.hold("the AES67 install")
            threading.Thread(target=_aes67_install_watch, daemon=True,
                             name="aes67-install-watch").start()
    except Exception:
        logger.exception("Could not check for a running AES67 install")

    # Flask (blocks until killed)
    # werkzeug's own "Running on ..." banner is logged at INFO, so it is dropped
    # along with the per-request lines (constraint 15). This restores the startup
    # confirmation through the player's logger - file and journal both. It runs
    # just BEFORE the port is bound, hence "starting": if the bind fails, the
    # traceback follows this line.
    logger.info("Web UI starting on port 5000 (hostname %s)", socket.gethostname())
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
