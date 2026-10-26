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
# Stage Audio Works Video Player — One-line bootstrap deploy
# =============================================================================
# Installs or updates a Pi from git in a single command. Requires internet.
#
#   curl -fsSL https://raw.githubusercontent.com/stageaudioworks/pi-video-player/main/bootstrap.sh | sudo bash
#
# Re-run the exact same command to update an existing Pi — this script is
# idempotent: it pulls the latest code and re-runs the installer, which skips
# any dependency that is already present.
#
# Options (note the `-s --` needed to pass args through a pipe):
#   ... | sudo bash -s -- --reboot      reboot when finished
#
# Environment overrides:
#   SAW_REPO_URL    git URL to deploy from     (default: the SAW GitHub repo)
#   SAW_BRANCH      branch to deploy           (default: main)
#   SAW_DIR         where to keep the checkout (default: /home/pi/pi-video-player)
#   SAW_GIT_TOKEN   GitHub token, only needed while the repo is private.
#                   Passed as a one-shot auth header so it is NEVER written to
#                   .git/config or left on disk.
#
# For Pis behind a firewall that blocks package/git access, do not use this —
# use update.sh, which makes zero network calls.
# =============================================================================
# Intentionally NOT using set -e — see CLAUDE.md. Errors are handled explicitly.
set -uo pipefail

# Deploys come from the PUBLIC distribution repo, which carries only the files
# needed to run a Pi. Development happens in the private pi-video-player repo
# and is published across by its release.sh.
REPO_URL="${SAW_REPO_URL:-https://github.com/stageaudioworks/pi-video-player-dist.git}"
BRANCH="${SAW_BRANCH:-main}"
CLONE_DIR="${SAW_DIR:-/home/pi/pi-video-player}"
TOKEN="${SAW_GIT_TOKEN:-}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()   { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()   { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()  { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
header() { echo -e "\n${BOLD}${CYAN}══ $* ══${NC}"; }

[[ $EUID -eq 0 ]] || error "Please run as root. Pipe into 'sudo bash', not 'bash'."

header "Stage Audio Works Video Player — bootstrap"
echo ""
echo "  Repo   : ${REPO_URL}"
echo "  Branch : ${BRANCH}"
echo "  Into   : ${CLONE_DIR}"
echo ""

# ── git ───────────────────────────────────────────────────────────────────────
if ! command -v git &>/dev/null; then
    warn "git not found — installing..."
    apt-get update -qq || warn "apt-get update failed — continuing"
    apt-get install -y git || error "Could not install git"
else
    info "git present: $(git --version)"
fi

# ── Build the git argument list ───────────────────────────────────────────────
# safe.directory: we run git as root against a pi-owned checkout, which git
# otherwise refuses with "detected dubious ownership".
# http.extraHeader: passed via `git -c` BEFORE the subcommand, so it applies to
# this process only and is never persisted into the repo's config. Using a
# token-in-URL instead would write the secret into .git/config permanently.
GIT_OPTS=(-c "safe.directory=${CLONE_DIR}")
if [[ -n "${TOKEN}" ]]; then
    info "Using SAW_GIT_TOKEN for authentication (not stored on disk)"
    GIT_OPTS+=(-c "http.extraHeader=Authorization: Bearer ${TOKEN}")
fi

# ── Clone or update ───────────────────────────────────────────────────────────
if [[ -d "${CLONE_DIR}/.git" ]]; then
    info "Existing checkout found — updating..."

    git "${GIT_OPTS[@]}" -C "${CLONE_DIR}" fetch --prune origin \
        || error "git fetch failed. Check network access, or the repo may still be private (see SAW_GIT_TOKEN)."

    # Hard reset rather than merge: this directory is a pure deployment
    # checkout that is never hand-edited, so a guaranteed-clean tree matters
    # more than preserving local changes. The running player lives at
    # /home/pi/saw_player.py, not in here, so nothing live is touched.
    git "${GIT_OPTS[@]}" -C "${CLONE_DIR}" reset --hard "origin/${BRANCH}" \
        || error "git reset failed — is '${BRANCH}' a valid branch?"

    info "  ✅  Updated to $(git "${GIT_OPTS[@]}" -C "${CLONE_DIR}" rev-parse --short HEAD)"
else
    if [[ -e "${CLONE_DIR}" ]]; then
        # Something is there but it is not a checkout - project files copied
        # across by hand for update.sh, say. Moved aside, never deleted: what
        # is in it may be the only copy.
        ASIDE="${CLONE_DIR}.old-$(date +%Y%m%d-%H%M%S)"
        mv "${CLONE_DIR}" "${ASIDE}" \
            || error "Could not move ${CLONE_DIR} aside - nothing was changed. Move it by hand, then run this again."
        warn "${CLONE_DIR} was not a git checkout - moved aside to ${ASIDE} (nothing deleted)"
    fi
    info "Cloning..."
    git "${GIT_OPTS[@]}" clone --branch "${BRANCH}" "${REPO_URL}" "${CLONE_DIR}" \
        || error "git clone failed. If the repo is still private, set SAW_GIT_TOKEN (see the header of this script)."
    info "  ✅  Cloned at $(git "${GIT_OPTS[@]}" -C "${CLONE_DIR}" rev-parse --short HEAD)"
fi

# The checkout should belong to pi, not root
chown -R pi:pi "${CLONE_DIR}" 2>/dev/null || warn "Could not chown ${CLONE_DIR} to pi"

# ── Hand over to the installer ────────────────────────────────────────────────
INSTALLER="${CLONE_DIR}/install.sh"
[[ -f "${INSTALLER}" ]] || error "install.sh not found in the checkout: ${INSTALLER}"

header "Running installer"
# "$@" forwards any flags given as: ... | sudo bash -s -- --reboot
bash "${INSTALLER}" "$@"
