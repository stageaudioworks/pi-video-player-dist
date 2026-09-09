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
#   1. Installs dependencies (mpv, Python packages, Bluetooth audio, watchdog)
#   2. Creates directories and installs the player script + systemd service
#   3. Configures the Raspberry Pi hardware watchdog
#   4. Writes the sudoers rules the web UI needs, then starts the service
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
# bluez            — pairing, trusting, connecting (bluetoothctl)
# bluez-alsa-utils — exposes each connected A2DP device as an ALSA PCM named
#                    bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp, so mpv can
#                    target it with the same --audio-device flag it already
#                    uses for HDMI and USB output.
# Bluetooth is currently deprioritised and untested on hardware, so every step
# here is non-fatal: a failure must not block a working HDMI-audio install.
info "Checking Bluetooth audio support..."

if ! command -v bluetoothctl &>/dev/null; then
    warn "bluez not found — installing..."
    apt-get install -y bluez || warn "Could not install bluez — Bluetooth will be unavailable"
else
    info "  bluez already installed"
fi

if ! dpkg -s bluez-alsa-utils &>/dev/null 2>&1; then
    warn "bluez-alsa-utils not found — installing..."
    apt-get install -y bluez-alsa-utils \
        || warn "Could not install bluez-alsa-utils — Bluetooth audio output will not work"
else
    info "  bluez-alsa-utils already installed"
fi

if systemctl list-unit-files 2>/dev/null | grep -q "^bluetooth.service"; then
    systemctl enable bluetooth &>/dev/null || true
    systemctl start  bluetooth &>/dev/null || true
    info "  bluetooth.service enabled"
fi

if systemctl list-unit-files 2>/dev/null | grep -q "^bluealsa.service"; then
    systemctl enable  bluealsa &>/dev/null || true
    systemctl restart bluealsa &>/dev/null || true
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

# ── watchdog daemon (userspace helper — the kernel module is separate) ────────
if ! command -v watchdog &>/dev/null; then
    warn "watchdog daemon not found — installing..."
    apt-get install -y watchdog || warn "Could not install watchdog daemon"
else
    info "watchdog daemon already installed"
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
# The Pi 5 (BCM2712) exposes a hardware watchdog at /dev/watchdog. The kernel
# module loads automatically on Bookworm. We configure the userspace daemon to
# pet it, and also point systemd's own watchdog at the hardware device as an
# independent second layer — if systemd itself hangs, the hardware still fires.
# =============================================================================

WATCHDOG_CONF="/etc/watchdog.conf"

info "Configuring watchdog daemon (${WATCHDOG_CONF})..."
cat > "${WATCHDOG_CONF}" << 'WDEOF'
# Stage Audio Works — watchdog configuration
# Hardware watchdog device (BCM2712 on RPi 5)
watchdog-device         = /dev/watchdog
# How often the daemon pets the watchdog (seconds).
# Must be less than watchdog-timeout below.
interval                = 10
# Hardware timeout — if the daemon dies and stops petting the watchdog,
# the Pi reboots after this many seconds.
watchdog-timeout        = 60
# Reboot if load average (1 min) exceeds this — catches runaway processes
max-load-1              = 24
# Reboot if free memory falls below this many pages (~4 MB at 4 KB/page)
min-memory              = 1
# Log to syslog
log-dir                 = /var/log/watchdog
WDEOF

mkdir -p /var/log/watchdog

# ── Native systemd unit for the watchdog daemon ───────────────────────────────
# The watchdog apt package on Bookworm ships only a SysV init script. When
# systemd tries to enable it through the SysV compatibility shim it prints
# "Job for watchdog.service canceled" and silently does nothing. The fix is to
# write a proper native unit that bypasses the shim entirely.
WATCHDOG_UNIT="/etc/systemd/system/watchdog.service"
info "Writing native systemd unit → ${WATCHDOG_UNIT}"
cat > "${WATCHDOG_UNIT}" << 'WDUNIT'
[Unit]
Description=Software watchdog daemon
After=dev-watchdog.device
Wants=dev-watchdog.device
Before=saw-player.service

[Service]
Type=forking
PIDFile=/run/watchdog.pid
ExecStart=/usr/sbin/watchdog
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
WDUNIT

chmod 644 "${WATCHDOG_UNIT}"
systemctl daemon-reload
systemctl enable watchdog.service &>/dev/null || warn "Could not enable watchdog.service"
systemctl restart watchdog.service \
    && info "Watchdog daemon started ✅" \
    || warn "Watchdog failed to start — check: journalctl -u watchdog -n 20"

# Tell systemd's own watchdog to use the hardware device as well
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
