#!/usr/bin/env bash
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
# =============================================================================
# Stage Audio Works Video Player — Installer
# =============================================================================
# Usage:
#   sudo bash install.sh              install / re-install, then start the service
#   sudo bash install.sh --reboot     same, then reboot when finished
#   sudo bash install.sh --uninstall  remove the service and watchdog config
#   sudo bash install.sh --help       show usage
#
# What this script does:
#   1. Installs dependencies (mpv, Python packages, Bluetooth audio)
#   2. Creates directories and installs the player script + systemd service
#   3. Configures the Raspberry Pi hardware watchdog
#   4. Writes the sudoers rules the web UI needs, puts in place what lets the
#      web UI install AES67 (it installs nothing itself - see
#      aes67-webinstall.sh), then starts the service
#
# What this script deliberately does NOT do:
#   Hostname and static-IP configuration. That is a separate concern — see
#   parked/network-config/. Keeping this installer non-interactive is what makes
#   the one-line bootstrap.sh deploy possible.
#
# This script is NON-INTERACTIVE and idempotent: it is safe to re-run, and every
# apt install is guarded by a check so a re-run is cheap.
# =============================================================================
# Intentionally NOT using set -e — we handle errors explicitly so the script
# never exits silently. set -e + (( )) arithmetic is a known bash gotcha that
# has bitten this project before. See CLAUDE.md.
set -uo pipefail

# Resolve our own directory so the script works no matter where it is invoked
# from (bootstrap.sh runs it by absolute path).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SERVICE_SRC="${SCRIPT_DIR}/saw-player.service"
PLAYER_DEST="/home/pi/saw_player.py"
SERVICE_DEST="/etc/systemd/system/saw-player.service"
SERVICE_NAME="saw-player"
VIDEO_DIR="/home/pi/videos"
LOG_FILE="/home/pi/saw_player.log"
SUDOERS_FILE="/etc/sudoers.d/saw-player"

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
header()  { echo -e "\n${BOLD}${CYAN}══ $* ══${NC}"; }

usage() {
    cat <<'USAGE'
Stage Audio Works Video Player — Installer

  sudo bash install.sh              install / re-install, then start the service
  sudo bash install.sh --reboot     same, then reboot when finished
  sudo bash install.sh --uninstall  remove the service and watchdog config
  sudo bash install.sh --help       show this message

Network configuration (hostname / static IP) is not handled here.
See parked/network-config/ for the parked tooling.
USAGE
}

# ── Argument parsing — must happen BEFORE any work ────────────────────────────
# (An earlier version had the --uninstall branch at the bottom of the file, so
#  `install.sh --uninstall` ran a full install first and then uninstalled it.)
DO_REBOOT=false
DO_UNINSTALL=false

for arg in "$@"; do
    case "${arg}" in
        --reboot)    DO_REBOOT=true ;;
        --uninstall) DO_UNINSTALL=true ;;
        --help|-h)   usage; exit 0 ;;
        *)           usage; error "Unknown option: ${arg}" ;;
    esac
done

# ── Root check ────────────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || error "Please run as root:  sudo bash $0"

# =============================================================================
# Uninstall mode — branch out before touching anything else
# =============================================================================
if [[ "${DO_UNINSTALL}" == true ]]; then
    header "Uninstalling ${SERVICE_NAME}"
    systemctl stop    "${SERVICE_NAME}" 2>/dev/null || true
    systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
    rm -f "${SERVICE_DEST}"
    systemctl stop    watchdog 2>/dev/null || true
    systemctl disable watchdog 2>/dev/null || true
    systemctl daemon-reload
    # The web UI's way into the AES67 installer. AES67 itself, if installed, has
    # its own: sudo bash install-aes67.sh --uninstall
    rm -f /etc/sudoers.d/saw-aes67-webinstall /usr/local/sbin/saw-aes67-webinstall \
          /etc/sudoers.d/saw-bluetooth
    rm -rf /usr/local/lib/saw-aes67
    info "Service and watchdog removed."
    info "Player script, videos, and logs were left in place. To remove them:"
    info "  rm -rf ${PLAYER_DEST} ${VIDEO_DIR} ${LOG_FILE}"
    exit 0
fi

# ── Locate the player script ──────────────────────────────────────────────────
# Resolved by glob rather than hardcoded, so a version bump cannot leave this
# installer pointing at a file that no longer exists. `sort -V` orders version
# numbers correctly, so v10 wins over v9.
PLAYER_SRC="$(ls -1 "${SCRIPT_DIR}"/RPi_Video_Player_v*.py 2>/dev/null | sort -V | tail -1 || true)"

[[ -n "${PLAYER_SRC}"    ]] || error "No RPi_Video_Player_v*.py found in ${SCRIPT_DIR}"
[[ -f "${PLAYER_SRC}"    ]] || error "Player script not found: ${PLAYER_SRC}"
[[ -f "${SERVICE_SRC}"   ]] || error "Service file not found:  ${SERVICE_SRC}"

# =============================================================================
header "Stage Audio Works Video Player — Installer"
# =============================================================================
echo ""
echo "  Player script : $(basename "${PLAYER_SRC}")"
echo "  Installing to : ${PLAYER_DEST}"
echo ""

# =============================================================================
header "1 / 4 — Dependencies"
# =============================================================================

info "Updating package list..."
apt-get update -qq || warn "apt-get update failed — continuing with cached lists"

# ── mpv ───────────────────────────────────────────────────────────────────────
# NOTE: mpv, not VLC. The Bookworm VLC package has no DRM/KMS video output
# module compiled in, so it cannot display video on a headless Pi. See CLAUDE.md.
if ! command -v mpv &>/dev/null; then
    warn "mpv not found — installing..."
    apt-get install -y mpv || error "Could not install mpv — the player cannot run without it"
else
    info "mpv already installed: $(mpv --version 2>&1 | head -1)"
fi

# ── Python packages ───────────────────────────────────────────────────────────
# On Bookworm, prefer apt packages to avoid pip/PEP-668 issues entirely.
# Falls back to 'python3 -m pip --break-system-packages' if apt fails.
info "Checking Python packages..."

install_python_pkg() {
    local import_name="$1"   # name used in 'import X'
    local apt_name="$2"      # apt package name

    if python3 -c "import ${import_name}" 2>/dev/null; then
        info "  ${import_name} OK"
        return
    fi

    warn "${import_name} not installed — trying apt..."
    if apt-get install -y "${apt_name}" -qq 2>/dev/null; then
        info "  ${import_name} installed via apt"
    else
        warn "apt install failed — falling back to python3 -m pip..."
        python3 -m pip install "${import_name}" --break-system-packages -q \
            || error "Could not install ${import_name}. Try manually: sudo apt install ${apt_name}"
    fi
}

install_python_pkg "flask"    "python3-flask"
install_python_pkg "requests" "python3-requests"

# ── Bluetooth audio (bluez + bluez-alsa) ─────────────────────────────────────
# bluez                   — pairing, trusting, connecting (bluetoothctl)
# bluez-alsa-utils        — the bluealsa daemon: each connected A2DP speaker
#                           becomes an ALSA PCM, so mpv reaches it with the same
#                           --audio-device flag it uses for HDMI and USB
# libasound2-plugin-bluez — the ALSA side of that (the "bluealsa" PCM).
#                           bluez-alsa-utils depends on it; named anyway, as it
#                           is the part mpv actually opens.
# Installed on every unit but switched on from the web UI only (v12 change 4).
# Every step is non-fatal: Bluetooth must never block a working install.
info "Checking Bluetooth audio support..."

if ! command -v bluetoothctl &>/dev/null; then
    warn "bluez not found — installing..."
    apt-get install -y bluez || warn "Could not install bluez — Bluetooth will be unavailable"
else
    info "  bluez already installed"
fi

# rfkill: Trixie Lite images do not ship it, and without it a radio that comes
# up blocked (a cloned image can) cannot be unblocked by the player.
for pkg in bluez-alsa-utils libasound2-plugin-bluez rfkill; do
    if ! dpkg -s "${pkg}" &>/dev/null; then
        warn "${pkg} not found — installing..."
        apt-get install -y "${pkg}" \
            || warn "Could not install ${pkg} — Bluetooth audio output may not work"
    else
        info "  ${pkg} already installed"
    fi
done

if systemctl list-unit-files 2>/dev/null | grep -q "^bluetooth.service"; then
    systemctl enable --now bluetooth &>/dev/null || true
    info "  bluetooth.service enabled"
fi

# enable --now, never restart: a re-run (the one-liner is also the update path)
# must not cut off a speaker that is playing.
if systemctl list-unit-files 2>/dev/null | grep -q "^bluealsa.service"; then
    systemctl enable --now bluealsa &>/dev/null || true
    if systemctl is-active --quiet bluealsa; then
        info "  ✅  bluealsa.service running (Bluetooth audio ready)"
    else
        warn "  bluealsa.service failed to start — check: journalctl -u bluealsa -n 20"
    fi
else
    warn "  bluealsa.service not found — Bluetooth audio output will not be available"
fi

# The pi user needs to be in the bluetooth group to run bluetoothctl commands
if ! id -nG pi 2>/dev/null | grep -qw bluetooth; then
    usermod -aG bluetooth pi \
        && info "  ✅  pi added to bluetooth group (takes effect after reboot)" \
        || warn "  Could not add pi to bluetooth group"
else
    info "  pi is already in the bluetooth group"
fi

# A radio blocked by rfkill cannot be powered on from bluetoothctl. Unblock it
# now, and let the player do the same later (an rfkill state can come back
# after an image is cloned) through one sudo rule for exactly that command,
# checked by visudo before it goes live.
if RFKILL="$(command -v rfkill)"; then
    "${RFKILL}" unblock bluetooth &>/dev/null || true
    BT_TMP="$(mktemp)"
    cat > "${BT_TMP}" << SUDOEOF
# Written by install.sh - lets the player unblock the Bluetooth radio, nothing else.
pi ALL=(root) NOPASSWD: ${RFKILL} unblock bluetooth
SUDOEOF
    if visudo -cf "${BT_TMP}" >/dev/null 2>&1; then
        install -m 440 -o root -g root "${BT_TMP}" /etc/sudoers.d/saw-bluetooth
    else
        warn "  Bluetooth rfkill sudo rule failed visudo's check - not installed"
    fi
    rm -f "${BT_TMP}"
fi

# =============================================================================
header "2 / 4 — Player script and service"
# =============================================================================

info "Creating video directory: ${VIDEO_DIR}"
mkdir -p "${VIDEO_DIR}"
chown pi:pi "${VIDEO_DIR}"

info "Ensuring log file exists: ${LOG_FILE}"
touch "${LOG_FILE}"
chown pi:pi "${LOG_FILE}"

# Stop the service before replacing the files underneath it
if systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
    info "Stopping existing ${SERVICE_NAME} service..."
    systemctl stop "${SERVICE_NAME}"
fi

info "Installing player script → ${PLAYER_DEST}"
cp "${PLAYER_SRC}" "${PLAYER_DEST}" || error "Failed to copy player script"
chown pi:pi "${PLAYER_DEST}"
chmod 755   "${PLAYER_DEST}"

# The user manual, for the web UI's "User manual" and Help links. On the unit,
# so it opens on a closed network; without it the links go to GitHub.
MANUAL_SRC="${SCRIPT_DIR}/docs/USER_MANUAL.pdf"
MANUAL_DEST="/home/pi/saw_manual.pdf"
if [[ -f "${MANUAL_SRC}" ]]; then
    install -m 644 -o pi -g pi "${MANUAL_SRC}" "${MANUAL_DEST}" \
        && info "Installing user manual → ${MANUAL_DEST}" \
        || warn "Could not install the user manual — the web UI's link opens GitHub instead"
else
    warn "docs/USER_MANUAL.pdf not found — the web UI's manual link opens GitHub instead"
fi

# The web UI's address on the console, above the login prompt - readable
# without logging in, during the 30 s the player leaves the console on screen
# after a boot. agetty fills in \n (hostname) and \4 (IPv4) and redraws it when
# the address changes. Raspberry Pi OS's own /etc/issue.d/IP.issue already
# prints the address; this adds the URL to type.
ISSUE_FILE="/etc/issue.d/saw-player.issue"
ISSUE_TMP="$(mktemp)"
cat > "${ISSUE_TMP}" << 'ISSUEEOF'
Stage Audio Works Video Player on \n - web UI: http://\4:5000

ISSUEEOF
if [[ -d /etc/issue.d ]] || mkdir -p /etc/issue.d; then
    if ! cmp -s "${ISSUE_TMP}" "${ISSUE_FILE}"; then
        install -m 644 -o root -g root "${ISSUE_TMP}" "${ISSUE_FILE}" \
            && info "Console shows the web UI address → ${ISSUE_FILE}"
    fi
fi
rm -f "${ISSUE_TMP}"

info "Installing service file → ${SERVICE_DEST}"
cp "${SERVICE_SRC}" "${SERVICE_DEST}" || error "Failed to copy service file"
chmod 644 "${SERVICE_DEST}"

# Safety net for the lgpio working-directory gotcha. The service file in this
# repo already sets it; this catches a hand-edited or stale unit on the Pi.
# lgpio creates a .lgd-nfy* pipe in the CWD at import time — under systemd the
# CWD is / which pi cannot write to, and the import throws FileNotFoundError.
if ! grep -q "WorkingDirectory" "${SERVICE_DEST}"; then
    warn "WorkingDirectory missing from service file — adding it"
    sed -i '/\[Service\]/a WorkingDirectory=/home/pi' "${SERVICE_DEST}"
fi

info "Reloading systemd daemon..."
systemctl daemon-reload

info "Enabling ${SERVICE_NAME} to start on boot..."
systemctl enable "${SERVICE_NAME}"

# ── NetworkManager-wait-online ────────────────────────────────────────────────
# Disabled deliberately: it can add minutes to boot, and nothing here needs the
# network to be up. Video playback is entirely local and Flask binds 0.0.0.0
# fine before an address is assigned. saw-player.service must also not list it
# in Wants=, because a Wants= pulls a unit in even when it is disabled.
if systemctl is-enabled NetworkManager-wait-online.service &>/dev/null; then
    info "Disabling NetworkManager-wait-online.service (slow boot)..."
    systemctl disable NetworkManager-wait-online.service &>/dev/null \
        && info "  ✅  NM-wait-online disabled" \
        || warn "  Could not disable NM-wait-online"
else
    info "NetworkManager-wait-online already disabled"
fi

# =============================================================================
header "3 / 4 — Hardware watchdog"
# =============================================================================
# The Pi 5 (BCM2712) has a hardware watchdog. systemd itself pets it
# (RuntimeWatchdogSec below): if the kernel or systemd hangs, the hardware
# reboots the Pi. That is the whole watchdog. The player's own mpv watchdog
# looks after playback.
#
# Up to v12 this installer also ran the userspace watchdog daemon, through a
# unit of its own. Retired in v13, because on the bench it did harm and no
# good:
#   * Its unit waited for dev-watchdog.device, which never appears (systemd
#     names the device /dev/watchdog0), and saw-player was ordered after it -
#     every boot waited the full 90 s device timeout before the player started.
#   * systemd already holds the hardware device, so the daemon could not open
#     it: it ran, but pet nothing.
# A unit written by an earlier run is taken out here.
# =============================================================================

WATCHDOG_UNIT="/etc/systemd/system/watchdog.service"
if [[ -f "${WATCHDOG_UNIT}" ]] && grep -q "^Before=saw-player.service" "${WATCHDOG_UNIT}"; then
    info "Removing the old userspace watchdog unit (it delayed every boot by 90 s)..."
    systemctl stop    watchdog.service &>/dev/null || true
    systemctl disable watchdog.service &>/dev/null || true
    rm -f "${WATCHDOG_UNIT}"
    systemctl daemon-reload
    # The watchdog package's own SysV script, if the package is installed,
    # must not take over the name now that the unit above is gone.
    systemctl disable watchdog.service &>/dev/null || true
    systemctl stop    watchdog.service &>/dev/null || true
    info "  ✅  removed"
fi

# systemd pets the hardware watchdog
SYSTEMD_CONF="/etc/systemd/system.conf"
if ! grep -q "^RuntimeWatchdogSec" "${SYSTEMD_CONF}" 2>/dev/null; then
    info "Configuring systemd hardware watchdog (RuntimeWatchdogSec=30s)..."
    {
        echo ""
        echo "# Hardware watchdog — SAW installer"
        echo "RuntimeWatchdogSec=30s"
        echo "ShutdownWatchdogSec=10min"
    } >> "${SYSTEMD_CONF}"
else
    info "systemd watchdog already configured in ${SYSTEMD_CONF}"
fi

if [[ -e /dev/watchdog ]]; then
    info "Hardware watchdog device found: /dev/watchdog ✅"
else
    warn "/dev/watchdog not found — the kernel module may not be loaded."
    warn "Try:  sudo modprobe bcm2835_wdt   (then re-run this script)"
    warn "Or add 'dtparam=watchdog=on' to /boot/firmware/config.txt and reboot."
fi

# =============================================================================
header "4 / 4 — Permissions and start"
# =============================================================================

# Sudoers rules — required for the web UI's reboot button and script self-update
if [[ ! -f "${SUDOERS_FILE}" ]]; then
    info "Writing sudoers rules → ${SUDOERS_FILE}"
    cat > "${SUDOERS_FILE}" << 'SUDOEOF'
# Allow pi user to restart saw-player and reboot via the web UI
pi ALL=(ALL) NOPASSWD: /bin/systemctl restart saw-player
pi ALL=(ALL) NOPASSWD: /bin/systemctl stop saw-player
pi ALL=(ALL) NOPASSWD: /bin/systemctl start saw-player
pi ALL=(ALL) NOPASSWD: /sbin/reboot
pi ALL=(ALL) NOPASSWD: /usr/sbin/reboot
SUDOEOF
    chmod 440 "${SUDOERS_FILE}"
    info "  ✅  Sudoers rules written"
else
    info "Sudoers rules already present — skipping"
fi

# Installing AES67 from the web UI (optional, experimental). Puts root-owned
# copies of the AES67 installer and the bridge source in place, the wrapper
# that starts them, and one sudo rule allowing user pi exactly two commands -
# see aes67-webinstall.sh. Nothing runs until somebody presses Install. The
# copies are root-owned on purpose: user pi can edit /home/pi, so running the
# checkout as root would hand pi root. Refreshed on every run, so the one-liner
# is also how a unit gets a newer AES67 installer.
AES67_LIB="/usr/local/lib/saw-aes67"
AES67_WRAPPER="/usr/local/sbin/saw-aes67-webinstall"
AES67_SUDOERS="/etc/sudoers.d/saw-aes67-webinstall"
if [[ -f "${SCRIPT_DIR}/install-aes67.sh" && -f "${SCRIPT_DIR}/aes67-webinstall.sh" \
      && -f "${SCRIPT_DIR}/aes67-bridge/saw-aes67-bridge.c" ]]; then
    rm -rf "${AES67_LIB}"
    install -d -m 755 -o root -g root "${AES67_LIB}/aes67-bridge"
    install -m 644 -o root -g root "${SCRIPT_DIR}/install-aes67.sh" "${AES67_LIB}/install-aes67.sh"
    install -m 644 -o root -g root "${SCRIPT_DIR}"/aes67-bridge/*.[ch] "${AES67_LIB}/aes67-bridge/"
    install -m 755 -o root -g root "${SCRIPT_DIR}/aes67-webinstall.sh" "${AES67_WRAPPER}"
    # Checked with visudo before it goes live: a sudoers file with a syntax
    # error breaks sudo for every user on the box.
    AES67_TMP="$(mktemp)"
    cat > "${AES67_TMP}" << SUDOEOF
# Written by install.sh - lets the web UI start the AES67 installer, nothing else.
pi ALL=(root) NOPASSWD: ${AES67_WRAPPER} start keep, ${AES67_WRAPPER} start dark
SUDOEOF
    if visudo -cf "${AES67_TMP}" >/dev/null 2>&1; then
        install -m 440 -o root -g root "${AES67_TMP}" "${AES67_SUDOERS}"
        info "AES67 can be installed from the web UI"
    else
        warn "AES67 sudo rule failed visudo's check - not installed; AES67 can still be installed over SSH"
    fi
    rm -f "${AES67_TMP}"
else
    warn "AES67 files not found next to install.sh - installing AES67 from the web UI will not be offered"
fi

info "Starting ${SERVICE_NAME} service..."
systemctl start "${SERVICE_NAME}" \
    || warn "Service failed to start — check: journalctl -u ${SERVICE_NAME} -n 50"

sleep 2
echo ""
systemctl status "${SERVICE_NAME}" --no-pager -l || true

# =============================================================================
header "Install complete"
# =============================================================================

FINAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
[[ -n "${FINAL_IP}" ]] || FINAL_IP="<no-ip-yet>"

echo ""
echo -e "  ${BOLD}${GREEN}Installation complete${NC}"
echo ""
echo "    Hostname   : $(hostname)"
echo "    Web UI     : http://${FINAL_IP}:5000"
echo "    Deployed   : $(basename "${PLAYER_SRC}") → ${PLAYER_DEST}"
echo ""
echo "    Videos go in ${VIDEO_DIR}/ as loop.mp4, trigger1.mp4,"
echo "    trigger2.mp4, trigger3.mp4, override.mp4 — or upload them"
echo "    through the web UI."
echo ""
echo "    Live logs  : journalctl -u ${SERVICE_NAME} -f"
echo "    Restart    : sudo systemctl restart ${SERVICE_NAME}"
echo "    Status     : sudo systemctl status ${SERVICE_NAME}"
echo "    Uninstall  : sudo bash install.sh --uninstall"
echo ""
echo "    A reboot is recommended to confirm a clean start from power-on."
echo ""

if [[ "${DO_REBOOT}" == true ]]; then
    info "Rebooting in 5 seconds...  (Ctrl+C to cancel)"
    sleep 5
    reboot
else
    info "Run 'sudo reboot' when ready, or re-run with --reboot."
fi
