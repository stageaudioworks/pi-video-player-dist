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
# Stage Audio Works Video Player — Deploy / Update Script
# =============================================================================
# Safe to run behind a corporate firewall — NO apt-get, NO wget, NO curl.
# All operations are local file copies and systemd/sudoers configuration.
#
# If the Pi HAS internet access, prefer bootstrap.sh instead — it pulls the
# latest code from git and installs in one command. This script exists for the
# units whose network blocks package and git access.
#
# Usage:
#   sudo bash update.sh                  — update script + service (if present)
#   sudo bash update.sh --script         — update script only
#   sudo bash update.sh --service        — update service file only
#   sudo bash update.sh --setup          — add sudoers rules + fix service file only
#   sudo bash update.sh --restart        — restart service only
#
# Run from the same directory as the files you want to deploy.
# =============================================================================
set -euo pipefail

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Player script resolved by glob rather than hardcoded, so a version bump cannot
# leave this script pointing at a file that no longer exists. `sort -V` orders
# version numbers correctly, so v10 wins over v9. The `|| true` matters: with
# `set -o pipefail`, a no-match `ls` would otherwise abort the whole script.
PLAYER_SRC="$(ls -1 "${SCRIPT_DIR}"/RPi_Video_Player_v*.py 2>/dev/null | sort -V | tail -1 || true)"
SERVICE_SRC="${SCRIPT_DIR}/saw-player.service"
PLAYER_DEST="/home/pi/saw_player.py"
SERVICE_DEST="/etc/systemd/system/saw-player.service"
SERVICE_NAME="saw-player"
SUDOERS_FILE="/etc/sudoers.d/saw-player"

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()   { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()   { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()  { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
header() { echo -e "\n${BOLD}${CYAN}══ $* ══${NC}"; }

# ── Root check ────────────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || error "Please run as root:  sudo bash $0"

MODE="${1:-}"

# =============================================================================
header "Stage Audio Works — Deploy / Update Tool (firewall-safe)"
# =============================================================================

# ── Restart-only mode ─────────────────────────────────────────────────────────
if [[ "${MODE}" == "--restart" ]]; then
    info "Restarting ${SERVICE_NAME}..."
    systemctl restart "${SERVICE_NAME}"
    sleep 2
    systemctl status "${SERVICE_NAME}" --no-pager -l || true
    exit 0
fi

# ══════════════════════════════════════════════════════════════════════════════
# SETUP — sudoers rules + service file fixes (safe to run on any existing Pi)
# ══════════════════════════════════════════════════════════════════════════════
do_setup() {
    header "Setup — sudoers + service file"

    # ── Sudoers rules for web UI reboot + script self-update ──────────────────
    if [[ ! -f "${SUDOERS_FILE}" ]]; then
        info "Writing sudoers rules → ${SUDOERS_FILE}"
        cat > "${SUDOERS_FILE}" << 'SUDOEOF'
# Allow pi user to restart saw-player service and reboot via the web UI
pi ALL=(ALL) NOPASSWD: /bin/systemctl restart saw-player
pi ALL=(ALL) NOPASSWD: /bin/systemctl stop saw-player
pi ALL=(ALL) NOPASSWD: /bin/systemctl start saw-player
pi ALL=(ALL) NOPASSWD: /sbin/reboot
pi ALL=(ALL) NOPASSWD: /usr/sbin/reboot
SUDOEOF
        chmod 440 "${SUDOERS_FILE}"
        info "  ✅  Sudoers rules written"
    else
        info "Sudoers rules already present at ${SUDOERS_FILE} — skipping"
    fi

    # ── Ensure WorkingDirectory=/home/pi is in service file ───────────────────
    # Required for lgpio to write its notification pipe in a writable directory.
    if [[ -f "${SERVICE_DEST}" ]]; then
        if ! grep -q "WorkingDirectory" "${SERVICE_DEST}"; then
            warn "WorkingDirectory missing from service file — adding it now"
            sed -i '/ExecStartPre=/i WorkingDirectory=/home/pi' "${SERVICE_DEST}"
            info "  ✅  WorkingDirectory=/home/pi added to ${SERVICE_DEST}"
            systemctl daemon-reload
        else
            info "WorkingDirectory already present in service file"
        fi
    else
        warn "Service file not found at ${SERVICE_DEST} — run install first"
    fi

    # ── Ensure NetworkManager-wait-online doesn't block boot forever ──────────
    if systemctl is-enabled NetworkManager-wait-online.service &>/dev/null; then
        info "Disabling NetworkManager-wait-online.service (causes slow boot)..."
        systemctl disable NetworkManager-wait-online.service || true
        info "  ✅  NM-wait-online disabled"
    else
        info "NetworkManager-wait-online already disabled"
    fi

    # ── Retire the userspace watchdog unit an earlier install.sh wrote ────────
    # It waited for a device that never appears, holding the player back 90 s
    # on every boot, and could not open the hardware watchdog systemd already
    # pets (RuntimeWatchdogSec). install.sh explains; this does the same.
    local wd_unit="/etc/systemd/system/watchdog.service"
    if [[ -f "${wd_unit}" ]] && grep -q "^Before=saw-player.service" "${wd_unit}"; then
        info "Removing the old userspace watchdog unit (it delayed every boot by 90 s)..."
        systemctl stop    watchdog.service &>/dev/null || true
        systemctl disable watchdog.service &>/dev/null || true
        rm -f "${wd_unit}"
        systemctl daemon-reload
        systemctl disable watchdog.service &>/dev/null || true
        systemctl stop    watchdog.service &>/dev/null || true
        info "  ✅  removed"
    fi

    # ── The user manual, for the web UI's "User manual" and Help links ────────
    # Read by the player on each request, so no restart. Found in docs/ (a
    # checkout) or next to this script (files copied across flat).
    local manual_src="" manual_dest="/home/pi/saw_manual.pdf" m
    for m in "${SCRIPT_DIR}/docs/USER_MANUAL.pdf" "${SCRIPT_DIR}/USER_MANUAL.pdf"; do
        [[ -f "${m}" ]] && { manual_src="${m}"; break; }
    done
    if [[ -n "${manual_src}" ]] && ! cmp -s "${manual_src}" "${manual_dest}"; then
        install -m 644 -o pi -g pi "${manual_src}" "${manual_dest}" \
            && info "  ✅  User manual updated → ${manual_dest}" \
            || warn "  Could not install the user manual"
    fi

    # ── The web UI's address on the console, above the login prompt ──────────
    # As install.sh: agetty fills in \n and \4, and redraws it on a new address.
    local issue_file="/etc/issue.d/saw-player.issue" issue_tmp
    issue_tmp="$(mktemp)"
    cat > "${issue_tmp}" << 'ISSUEEOF'
Stage Audio Works Video Player on \n - web UI: http://\4:5000

ISSUEEOF
    mkdir -p /etc/issue.d
    if ! cmp -s "${issue_tmp}" "${issue_file}"; then
        install -m 644 -o root -g root "${issue_tmp}" "${issue_file}" \
            && info "  ✅  Console shows the web UI address → ${issue_file}"
    fi
    rm -f "${issue_tmp}"

    # NOTE: this used to inject Environment=GPIOZERO_PIN_FACTORY=lgpio into the
    # service file. That was removed — gpiozero is not used (the player calls
    # lgpio directly), and re-adding a line that is no longer in the repo's
    # service file made every run see an MD5 mismatch and restart the service.

    # ── Bluetooth audio readiness ─────────────────────────────────────────────
    # This script never calls apt (it must work behind a corporate firewall),
    # so we only report on and enable what is already installed.
    if command -v bluetoothctl &>/dev/null; then
        info "Bluetooth: bluez is installed"

        if systemctl list-unit-files 2>/dev/null | grep -q "^bluetooth.service"; then
            systemctl enable --now bluetooth &>/dev/null || true
        fi

        # enable --now, never restart: this runs on every update and must not
        # cut off a speaker that is playing.
        if dpkg -s bluez-alsa-utils &>/dev/null 2>&1 \
           || systemctl list-unit-files 2>/dev/null | grep -q "^bluealsa.service"; then
            systemctl enable --now bluealsa &>/dev/null || true
            if systemctl is-active --quiet bluealsa; then
                info "  ✅  bluealsa running — Bluetooth audio ready"
            else
                warn "  bluealsa installed but not running"
                warn "  Check with: journalctl -u bluealsa -n 20"
            fi
        else
            warn "  bluez-alsa-utils is NOT installed — Bluetooth audio output will not work."
            warn "  When this Pi has package access, run:  sudo apt install -y bluez-alsa-utils"
        fi

        if ! id -nG pi 2>/dev/null | grep -qw bluetooth; then
            usermod -aG bluetooth pi \
                && info "  ✅  pi added to bluetooth group (effective after reboot)" \
                || warn "  Could not add pi to bluetooth group"
        else
            info "  pi is already in the bluetooth group"
        fi
    else
        warn "Bluetooth: bluez is NOT installed — Bluetooth features will be disabled."
        warn "When this Pi has package access, run:  sudo apt install -y bluez bluez-alsa-utils"
    fi

    info "Setup complete ✅"
}

# ── Setup-only mode ───────────────────────────────────────────────────────────
if [[ "${MODE}" == "--setup" ]]; then
    do_setup
    info "Restarting service to apply changes..."
    systemctl restart "${SERVICE_NAME}" || warn "Restart failed"
    sleep 2
    systemctl status "${SERVICE_NAME}" --no-pager -l || true
    exit 0
fi

# ══════════════════════════════════════════════════════════════════════════════
# FILE UPDATE
# ══════════════════════════════════════════════════════════════════════════════

UPDATE_SCRIPT=false
UPDATE_SERVICE=false

if [[ "${MODE}" == "--script" ]]; then
    UPDATE_SCRIPT=true
elif [[ "${MODE}" == "--service" ]]; then
    UPDATE_SERVICE=true
else
    [[ -f "${PLAYER_SRC}"  ]] && UPDATE_SCRIPT=true
    [[ -f "${SERVICE_SRC}" ]] && UPDATE_SERVICE=true
    if [[ "${UPDATE_SCRIPT}" == false && "${UPDATE_SERVICE}" == false ]]; then
        error "No source files found alongside this script.\n  Expected: ${PLAYER_SRC} and/or ${SERVICE_SRC}"
    fi
fi

[[ "${UPDATE_SCRIPT}"  == true && ! -f "${PLAYER_SRC}"  ]] && error "No RPi_Video_Player_v*.py found in ${SCRIPT_DIR}"
[[ "${UPDATE_SERVICE}" == true && ! -f "${SERVICE_SRC}" ]] && error "Service file not found: ${SERVICE_SRC}"

echo ""
echo -e "  ${BOLD}Will update:${NC}"
[[ "${UPDATE_SCRIPT}"  == true ]] && echo "    • Player script  : ${PLAYER_SRC} → ${PLAYER_DEST}"
[[ "${UPDATE_SERVICE}" == true ]] && echo "    • Service file   : ${SERVICE_SRC} → ${SERVICE_DEST}"
echo ""

# ── Checksum comparison ───────────────────────────────────────────────────────
SCRIPT_CHANGED=false
SERVICE_CHANGED=false

if [[ "${UPDATE_SCRIPT}" == true ]]; then
    if [[ -f "${PLAYER_DEST}" ]]; then
        SRC_MD5=$(md5sum "${PLAYER_SRC}"  | cut -d' ' -f1)
        DST_MD5=$(md5sum "${PLAYER_DEST}" | cut -d' ' -f1)
        if [[ "${SRC_MD5}" == "${DST_MD5}" ]]; then
            info "Player script unchanged (checksums match) — skipping copy"
        else
            SCRIPT_CHANGED=true
            info "Player script has changed — will update"
        fi
    else
        SCRIPT_CHANGED=true
        info "Player script not yet installed — will install"
    fi
fi

if [[ "${UPDATE_SERVICE}" == true ]]; then
    if [[ -f "${SERVICE_DEST}" ]]; then
        SRC_MD5=$(md5sum "${SERVICE_SRC}"  | cut -d' ' -f1)
        DST_MD5=$(md5sum "${SERVICE_DEST}" | cut -d' ' -f1)
        if [[ "${SRC_MD5}" == "${DST_MD5}" ]]; then
            info "Service file unchanged (checksums match) — skipping copy"
        else
            SERVICE_CHANGED=true
            info "Service file has changed — will update"
        fi
    else
        SERVICE_CHANGED=true
        info "Service file not yet installed — will install"
    fi
fi

if [[ "${SCRIPT_CHANGED}" == false && "${SERVICE_CHANGED}" == false ]]; then
    info "Files are up to date — running setup checks anyway..."
    do_setup
    info "Nothing to restart."
    exit 0
fi

# ── Stop service ──────────────────────────────────────────────────────────────
if systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
    info "Stopping ${SERVICE_NAME}..."
    systemctl stop "${SERVICE_NAME}"
fi

# ── Copy player script ────────────────────────────────────────────────────────
if [[ "${SCRIPT_CHANGED}" == true ]]; then
    info "Installing player script → ${PLAYER_DEST}"
    cp "${PLAYER_SRC}" "${PLAYER_DEST}"
    chown pi:pi "${PLAYER_DEST}"
    chmod 755   "${PLAYER_DEST}"
    info "  ✅  Script updated"
fi

# ── Copy service file ─────────────────────────────────────────────────────────
if [[ "${SERVICE_CHANGED}" == true ]]; then
    info "Installing service file → ${SERVICE_DEST}"
    cp "${SERVICE_SRC}" "${SERVICE_DEST}"
    chmod 644 "${SERVICE_DEST}"
    info "  ✅  Service file updated"
    systemctl daemon-reload
    info "  ✅  Daemon reloaded"
fi

# ── Always run setup to ensure sudoers/WorkingDirectory are correct ───────────
do_setup

# ── Start service ─────────────────────────────────────────────────────────────
info "Starting ${SERVICE_NAME}..."
systemctl start "${SERVICE_NAME}" \
    || warn "Service failed to start — check: journalctl -u ${SERVICE_NAME} -n 30"

sleep 2

echo ""
systemctl status "${SERVICE_NAME}" --no-pager -l || true
echo ""

echo -e "${BOLD}${GREEN}"
echo "  ┌─────────────────────────────────────────────────────────┐"
echo "  │                   Update complete!                       │"
[[ "${SCRIPT_CHANGED}"  == true ]] && \
echo "  │   ✅  Player script updated                              │"
[[ "${SERVICE_CHANGED}" == true ]] && \
echo "  │   ✅  Service file updated + daemon reloaded             │"
echo "  │   ✅  Sudoers rules verified                             │"
echo "  │   ✅  Service file config verified                       │"
echo "  │                                                           │"
echo "  │   Useful commands:                                        │"
echo "  │     Live logs : journalctl -u saw-player -f              │"
echo "  │     Restart   : sudo systemctl restart saw-player        │"
echo "  │     Status    : sudo systemctl status saw-player         │"
echo "  └─────────────────────────────────────────────────────────┘"
echo -e "${NC}"
