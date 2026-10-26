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
# aes67-webinstall.sh — lets the web UI start install-aes67.sh, and nothing else
# =============================================================================
# install.sh installs this as /usr/local/sbin/saw-aes67-webinstall, owned by
# root, with one sudo rule (/etc/sudoers.d/saw-aes67-webinstall) allowing user
# pi exactly these two commands:
#
#   saw-aes67-webinstall start keep    build with the picture still playing
#   saw-aes67-webinstall start dark    stop the player for the build
#
# Why a wrapper rather than a sudo rule for the installer itself:
#   * Root, narrowly. It runs the root-owned copy of the installer that
#     install.sh keeps in /usr/local/lib/saw-aes67 - never the checkout in
#     /home/pi, which user pi can edit and so would turn into root.
#   * It has to outlive the player. install-aes67.sh can stop saw-player, and
#     the web UI lives inside saw-player: started as its child, the build would
#     be killed along with it. So the build runs as its own transient systemd
#     unit, and this returns at once.
#   * A 2 GB Pi always builds dark. The compile needs about 1.2 GB per job;
#     with the player running beside it, 2 GB can run out. "keep" below 3 GB of
#     RAM is quietly made "dark" here - decided by root, not taken on trust
#     from a web request.
#
# Progress goes to LOG; RESULT holds "running", then the installer's exit code.
# Both are world-readable, so the player (user pi) can show them.
#
# Intentionally NOT using set -e (CLAUDE.md constraint 6).
set -uo pipefail

LIB="/usr/local/lib/saw-aes67"
LOG="/var/log/saw-aes67-install.log"
RESULT="/var/log/saw-aes67-install.result"
UNIT="saw-aes67-install"
MEMINFO="/proc/meminfo"
KEEP_MIN_KB=$((3 * 1024 * 1024))       # a "2 GB" Pi reports just under 2 GiB

usage() { echo "usage: saw-aes67-webinstall start keep|dark" >&2; exit 2; }

# keep only with the memory for it; anything else is dark.
effective_mode() {   # effective_mode REQUESTED
    local kb
    kb="$(awk '/^MemTotal:/ {print $2; exit}' "$MEMINFO" 2>/dev/null)"
    case "$kb" in ''|*[!0-9]*) kb=0 ;; esac
    if [ "$1" = keep ] && [ "$kb" -ge "$KEEP_MIN_KB" ]; then echo keep; else echo dark; fi
}

[ "$(id -u)" -eq 0 ] || { echo "must be run as root" >&2; exit 1; }

case "${1:-}" in
start)
    [ $# -eq 2 ] || usage
    case "$2" in keep|dark) ;; *) usage ;; esac
    [ -f "$LIB/install-aes67.sh" ] \
        || { echo "the AES67 installer is not on this unit - run the one-line installer again" >&2; exit 4; }
    if systemctl is-active --quiet "$UNIT" 2>/dev/null; then
        echo "an AES67 install is already running" >&2
        exit 3
    fi
    mode="$(effective_mode "$2")"
    systemctl reset-failed "$UNIT" >/dev/null 2>&1 || true
    : > "$LOG" && chmod 644 "$LOG"
    echo running > "$RESULT" && chmod 644 "$RESULT"
    echo "AES67 install started from the web UI, $(date '+%Y-%m-%d %H:%M:%S'), mode: $mode" >> "$LOG"
    if ! systemd-run --unit="$UNIT" --description="SAW AES67 install (from the web UI)" \
            "$(readlink -f "$0")" run "$mode" >/dev/null 2>>"$LOG"; then
        echo "systemd-run failed" >> "$LOG"
        echo 125 > "$RESULT"
        echo "could not start the install - see $LOG" >&2
        exit 5
    fi
    echo "$mode"
    ;;
run)
    # Only ever reached through systemd-run above: sudo does not pass
    # INVOCATION_ID through, and the sudo rule does not allow "run" anyway.
    [ -n "${INVOCATION_ID:-}" ] || { echo "run is started by systemd only" >&2; exit 2; }
    [ $# -eq 2 ] || usage
    args=()
    case "$2" in
        keep) args+=(--keep-player) ;;
        dark) ;;
        *)    usage ;;
    esac
    bash "$LIB/install-aes67.sh" "${args[@]}" >> "$LOG" 2>&1
    rc=$?
    echo "$rc" > "$RESULT"
    echo "AES67 install finished, $(date '+%Y-%m-%d %H:%M:%S'), exit $rc" >> "$LOG"
    exit "$rc"
    ;;
*)
    usage
    ;;
esac
