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
# install-aes67.sh — OPT-IN AES67 audio output for the SAW video player
# =============================================================================
#   sudo bash install-aes67.sh            install, or bring an install up to date
#   sudo bash install-aes67.sh --check    report what is here, change nothing
#   sudo bash install-aes67.sh --help     every option
#
# EXPERIMENTAL. Built from the aes67-integration skill and adapted from
# obs-multisite's scripts/player/merging-aes67.sh (GPL-3.0-or-later, Stage
# Audio Works) - the canonical installer, proven on this exact Pi kernel
# (6.12.47+rpt-rpi-2712). NOT yet run against a PTP grandmaster and a receiver
# for this project. See docs/PROJECT_HANDOFF.md.
#
# What it installs
#   * Merging's MergingRavennaALSA kernel module (bondagit/ravenna-alsa-lkm,
#     branch aes67-daemon), which registers an ALSA card called RAVENNA and
#     does RTP + PTP-slave in the kernel.
#   * bondagit/aes67-linux-daemon, pinned to v4.0.2, which configures the card
#     over its REST API and announces streams with SAP/mDNS.
#   * snd-aloop, a loopback card (id SAWLoop): mpv plays into one end.
#   * saw-aes67-bridge (aes67-bridge/ in this repo, built here in seconds),
#     which reads the other end and keeps the RAVENNA card open and fed for
#     good, steering between the Pi's clock and the network's PTP clock.
#   * Host settings the skill requires (sysctl, CPU governor, module loading),
#     a first-boot identity check so cloned SD cards do not share a multicast
#     group, and a firewall rule keeping the daemon's REST API on localhost.
#
# What it deliberately does NOT do
#   * Create the AES67 stream. The player owns it (one owner per stream, as the
#     skill insists) and creates it when AES67 is chosen as the audio output.
#   * Run as part of install.sh. Most units do not need AES67, and units behind
#     a firewall cannot build it. Build it once on a connected unit and clone
#     that SD card - the identity check makes the clones safe.
#
# Needs internet (apt, GitHub) and takes 20-40 minutes on a Pi: the daemon
# compiles Boost-heavy C++ and a 2 GB Pi can only manage one compiler at a time.
# The player is stopped while it builds - see stop_player().
#
# Safe to run again. A healthy install of the same version is left exactly as
# it is - see is_current() for why that matters.
# =============================================================================
# Intentionally NOT using set -e (CLAUDE.md constraint 6): every step that
# matters checks its own result and calls die() with a reason.
set -uo pipefail

# ── What, where ──────────────────────────────────────────────────────────────
DAEMON_URL="${DAEMON_URL:-https://github.com/bondagit/aes67-linux-daemon.git}"
# Pinned: the release proven on Pi 5 in obs-multisite and the skill. v4.0.3
# exists upstream but is unproven here; moving the pin is a deliberate change.
# A reused clone's `checkout master` never moves, so refs are fetched from
# origin explicitly (fetch_source).
DAEMON_REF="${DAEMON_REF:-v4.0.2}"
BUILD_DIR="${BUILD_DIR:-/var/tmp/saw-aes67-build}"

DAEMON="/usr/local/bin/aes67-daemon"
DAEMON_CONF="/etc/daemon.conf"          # the daemon's own default --config path
STATUS_FILE="/etc/status.json"          # streams persist here across restarts
WEBUI_ROOT="/usr/local/share/aes67-daemon"
WEBUI_DIST="$WEBUI_ROOT/dist"
DAEMON_SERVICE="aes67-daemon"
DAEMON_USER="aes67-daemon"

SAW_ETC="/etc/saw-aes67"                # this installer's own state
STATE_FILE="$SAW_ETC/installed"         # pin + kernel this box was built for
IDENTITY_FILE="$SAW_ETC/identity"       # the MAC per-box settings were made for
IDENTITY_BIN="/usr/local/sbin/saw-aes67-identity"
NFT_FILE="$SAW_ETC/firewall.nft"
GOVERNOR_BIN="/usr/local/sbin/saw-aes67-governor"

BRIDGE_BIN="/usr/local/bin/saw-aes67-bridge"
BRIDGE_SERVICE="saw-aes67-bridge"
BRIDGE_HASH_FILE="$SAW_ETC/bridge.sha256"   # the source the binary was built from
BRIDGE_VAR="/var/lib/saw-aes67"             # the channel setting: the player writes it
PLAYER_USER="pi"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
BRIDGE_SRC="${BRIDGE_SRC:-$SCRIPT_DIR/aes67-bridge}"

CARD_NAME="RAVENNA"
LOOP_ID="SAWLoop"                       # fixed id, so device names never move
PLAYER_SERVICE="saw-player"
PLAYER_WEB_PORT=5000                    # the player's own web UI

# The daemon's REST/WebUI port. Upstream's 8080 is free here - the player is on
# 5000 - so it is kept unless something else holds it (choose_http_port).
HTTP_PORT="${HTTP_PORT:-8080}"
PTP_DOMAIN=""                           # empty = keep the file's, else 0

# Boost translation units hold ~1.2 GB per g++. `make -j$(nproc)` locked up and
# rebooted a 4 GB Pi; jobs come from memory free at build time.
MEM_PER_JOB_MB="${MEM_PER_JOB_MB:-1200}"
SWAPFILE_MB="${SWAPFILE_MB:-2048}"

# ── Options ──────────────────────────────────────────────────────────────────
CHECK_ONLY=0; DO_UNINSTALL=0; FORCE_REBUILD=0; REWRITE_CONFIG=0
SET_GOVERNOR=1; HOLD_KERNEL=1; FIREWALL=1; STOP_PLAYER=1

# ── Output ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()   { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()   { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()    { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }
header() { echo -e "\n${BOLD}${CYAN}══ $* ══${NC}"; }
have()   { command -v "$1" >/dev/null 2>&1; }

usage() {
    cat <<'USAGE'
Opt-in AES67 audio output for the SAW video player (EXPERIMENTAL).

  sudo bash install-aes67.sh [options]

  --check             report what is installed and change nothing
  --uninstall         remove the AES67 stack (keeps /etc/daemon.conf as .bak)
  --force-rebuild     rebuild and restart even if this version is already healthy
  --rewrite-config    replace /etc/daemon.conf outright (keeps a .bak)
  --ptp-domain N      PTP domain of the grandmaster (default: keep, else 0)
  --jobs N            compile N files at once (default: from free memory)
  --keep-player       do not stop the player during the build (risky on 2 GB)
  --keep-governor     leave the CPU governor alone (fanless enclosures)
  --no-hold-kernel    do not apt-mark hold the kernel
  --open-webui        do not firewall the daemon's REST port to localhost
  -h, --help          this text

Environment: DAEMON_URL, DAEMON_REF, BUILD_DIR, HTTP_PORT, JOBS,
             MEM_PER_JOB_MB, SWAPFILE_MB, BRIDGE_SRC
USAGE
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --check)          CHECK_ONLY=1 ;;
            --uninstall)      DO_UNINSTALL=1 ;;
            --force-rebuild)  FORCE_REBUILD=1 ;;
            --rewrite-config) REWRITE_CONFIG=1 ;;
            --ptp-domain)     PTP_DOMAIN="${2:-}"; shift
                              case "$PTP_DOMAIN" in ''|*[!0-9]*) die "--ptp-domain needs a number 0-127" ;; esac
                              [ "$PTP_DOMAIN" -le 127 ] || die "--ptp-domain must be 0-127" ;;
            --jobs)           JOBS="${2:-}"; shift
                              case "$JOBS" in ''|*[!0-9]*|0) die "--jobs needs a positive number" ;; esac ;;
            --keep-player)    STOP_PLAYER=0 ;;
            --keep-governor)  SET_GOVERNOR=0 ;;
            --no-hold-kernel) HOLD_KERNEL=0 ;;
            --open-webui)     FIREWALL=0 ;;
            -h|--help)        usage; exit 0 ;;
            *)                usage; die "unknown option: $1" ;;
        esac
        shift
    done
}

# =============================================================================
# Pure helpers - no side effects, so tools/test_install_aes67.sh can exercise
# them on any Linux box.
# =============================================================================

# The interface the AES67 traffic uses: the default route's, else the first
# non-loopback one. Hardcoding eth0 broke on boards whose NIC has another name
# ("Cannot retrieve MAC address" - ROCK 5B's is enP4p65s0).
default_iface() {
    local dev
    dev="$(ip route show default 2>/dev/null | awk '/^default/ {for (i=1;i<=NF;i++) if ($i=="dev") {print $(i+1); exit}}')"
    if [ -z "$dev" ]; then
        local n
        for n in "${SYSFS_NET:-/sys/class/net}"/*; do
            [ -e "$n" ] || continue
            n="${n##*/}"
            [ "$n" = "lo" ] && continue
            dev="$n"; break
        done
    fi
    echo "${dev:-eth0}"
}

# This box's own multicast group, from the last two bytes of the MAC on IFACE.
# Same formula as obs-multisite's aes67-address.sh and as the player, so the
# group the identity check writes is the one the player then uses. Every box on
# the daemon's sample 239.1.0.1 made receivers hear every packet twice.
box_mcast_address() {   # box_mcast_address IFACE
    local mac b5 b6
    mac="$(cat "${SYSFS_NET:-/sys/class/net}/$1/address" 2>/dev/null || true)"
    case "$mac" in
        [0-9a-fA-F][0-9a-fA-F]:[0-9a-fA-F][0-9a-fA-F]:[0-9a-fA-F][0-9a-fA-F]:[0-9a-fA-F][0-9a-fA-F]:[0-9a-fA-F][0-9a-fA-F]:[0-9a-fA-F][0-9a-fA-F]) ;;
        *) echo "239.1.0.1"; return ;;
    esac
    b5=$((16#$(echo "$mac" | cut -d: -f5)))
    b6=$((16#$(echo "$mac" | cut -d: -f6)))
    if [ "$b5" -eq 0 ] && [ "$b6" -eq 1 ]; then b6=2; fi
    echo "239.1.$b5.$b6"
}

# Package names for this kernel's headers, in the order to try them. Debian's
# and the Pi's (since Bookworm: linux-headers-6.12.47+rpt-rpi-2712), Armbian's
# branch-named packages, then pre-Bookworm Pi OS.
headers_candidates() {   # headers_candidates KERNEL_RELEASE
    local krel="$1"
    echo "linux-headers-$krel"
    case "$krel" in
        [0-9]*-vendor-*|[0-9]*-current-*|[0-9]*-edge-*|[0-9]*-legacy-*)
            echo "linux-headers-${krel#*-}" ;;
    esac
    echo "raspberrypi-kernel-headers"
}

# Compile jobs: memory is the binding constraint, not cores.
build_jobs() {   # build_jobs [MEMINFO_PATH]
    if [ -n "${JOBS:-}" ]; then echo "$JOBS"; return; fi
    local cores avail_kb by_mem
    cores="$(nproc 2>/dev/null || echo 2)"
    avail_kb="$(awk '/^MemAvailable:/ {print $2; exit}' "${1:-/proc/meminfo}" 2>/dev/null || echo 0)"
    case "$avail_kb" in ''|*[!0-9]*) echo 1; return ;; esac
    [ "$avail_kb" -gt 0 ] || { echo 1; return; }
    by_mem=$(( avail_kb / 1024 / MEM_PER_JOB_MB ))
    [ "$by_mem" -lt 1 ] && by_mem=1
    if [ "$by_mem" -lt "$cores" ]; then echo "$by_mem"; else echo "$cores"; fi
}

# The kernel's major.minor as one number: 6.12 -> 612. The module build goes
# down the clang path for >= 6.10 (upstream's build.sh compares 612 >= 72, so in
# practice every modern kernel), and >= 6.15 sends RTP in bursts.
kernel_num() {   # kernel_num RELEASE
    local maj min
    maj="$(echo "$1" | cut -d. -f1)"; min="$(echo "$1" | cut -d. -f2)"
    case "$maj$min" in ''|*[!0-9]*) echo 0; return ;; esac
    echo $(( maj * 100 + min ))
}

# Merge the keys THIS installer owns into daemon.conf, leaving every other key
# as the operator set it ("merge only keys you own; never clobber"). Written
# via a temp file + os.replace (CLAUDE.md: never truncate a file in place).
merge_daemon_conf() {   # merge_daemon_conf CONF IFACE PORT MCAST DOMAIN_OR_EMPTY
    python3 - "$@" <<'PY'
import json, os, sys, tempfile
conf, iface, port, mcast, domain = sys.argv[1:6]
try:
    with open(conf) as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict):
        raise ValueError("not a JSON object")
except FileNotFoundError:
    cfg = {}
# Upstream's sample config as the base for anything absent - the daemon's JSON
# parser defaults nothing, so a missing key is a hard error at startup.
base = {
    "http_port": 8080, "rtsp_port": 8854,
    "http_base_dir": "/usr/local/share/aes67-daemon/dist/",
    "log_severity": 2, "playout_delay": 0,
    "tic_frame_size_at_1fs": 48, "max_tic_frame_size": 1024,
    "sample_rate": 48000,
    "rtp_mcast_base": "239.1.0.1", "rtp_mcast_base_sec": "239.1.0.1",
    "rtp_port": 5004, "rtp_port_sec": 5004,
    "ptp_domain": 0, "ptp_dscp": 48,
    "sap_mcast_addr": "239.255.255.255", "sap_interval": 30,
    "syslog_proto": "none", "syslog_server": "255.255.255.254:1234",
    "status_file": "/etc/status.json", "interface_name": "eth0",
    "mdns_enabled": True, "custom_node_id": "",
    "ptp_status_script": "/usr/local/share/aes67-daemon/scripts/ptp_status.sh",
    "streamer_channels": 8, "streamer_files_num": 8,
    "streamer_file_duration": 1, "streamer_player_buffer_files_num": 1,
    "streamer_enabled": False, "auto_sinks_update": False,
    "nmos_enabled": False, "nmos_registry_address": "127.0.0.1",
    "nmos_registry_port": 3210, "nmos_node_port": 3218,
    "nmos_label": "AES67 Daemon",
}
for k, v in base.items():
    cfg.setdefault(k, v)
owned = {
    "interface_name": iface,
    "http_port": int(port),
    "http_base_dir": "/usr/local/share/aes67-daemon/dist/",
    "ptp_status_script": "/usr/local/share/aes67-daemon/scripts/ptp_status.sh",
    "status_file": "/etc/status.json",
    "sample_rate": 48000,                 # AES67
    "tic_frame_size_at_1fs": 48,          # 1 ms packets: AES67 / ST 2110-30 A
    "max_tic_frame_size": 1024,
    "rtp_mcast_base": mcast, "rtp_mcast_base_sec": mcast,   # per box
    "streamer_enabled": False,            # holds the only capture substream
    "auto_sinks_update": False,           # true can turn a sink into a loopback
    "playout_delay": 0,
}
if domain != "":
    owned["ptp_domain"] = int(domain)
cfg.update(owned)
d = os.path.dirname(conf) or "."
fd, tmp = tempfile.mkstemp(dir=d, prefix=".daemon.conf.")
with os.fdopen(fd, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
os.chmod(tmp, 0o644)
os.replace(tmp, conf)
PY
}

# Which source the bridge binary was built from, so a re-run rebuilds it only
# when the source changed. Restarting the bridge drops the stream for a moment.
bridge_source_hash() {   # bridge_source_hash DIR
    [ -f "$1/saw-aes67-bridge.c" ] || return 1
    ( cd "$1" && find . -maxdepth 1 -type f \( -name '*.c' -o -name '*.h' \) -print0 \
        | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1 )
}

# The one compile command - the test suite builds with this function too. No
# -Werror: a newer compiler's new warning must not stop an install.
build_bridge() {   # build_bridge SRC_DIR OUT
    local flags
    flags="$(pkg-config --cflags --libs alsa samplerate 2>/dev/null)" || return 1
    # shellcheck disable=SC2086
    gcc -O2 -Wall -std=gnu11 -o "$2" "$1/saw-aes67-bridge.c" $flags -lpthread -lm
}

# ── Generators: write a file, touch nothing else ────────────────────────────
# Kept free of side effects so tools/test_install_aes67.sh can produce each
# file and run it. No shell code goes inside a systemd unit: systemd expands
# "$name" in ExecStart itself, so `... > "$g"` in a unit becomes `> ""` and
# fails silently. An earlier draft of the governor unit did exactly that. Each
# unit runs a script instead.

# The firewall ruleset. Declare, delete, re-declare: loading it twice never
# duplicates the rule.
write_nft_ruleset() {   # write_nft_ruleset FILE PORT
    cat > "$1" <<EOF
# Written by install-aes67.sh - the aes67-daemon REST API, loopback only.
table inet saw_aes67
delete table inet saw_aes67
table inet saw_aes67 {
    chain input {
        type filter hook input priority 0; policy accept;
        iifname != "lo" tcp dport $2 drop
    }
}
EOF
}

# Sets every CPU to the performance governor. CPUFREQ_GLOB is a test hook.
write_governor_script() {   # write_governor_script FILE
    cat > "$1" <<'EOF'
#!/usr/bin/env bash
# saw-aes67-governor - installed by install-aes67.sh.
# perf_cpu_time_max_percent alone does not stop schedutil scaling, which is
# heard as seconds of distortion on the AES67 stream.
set -uo pipefail
n=0
for g in ${CPUFREQ_GLOB:-/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor}; do
    [ -e "$g" ] || continue
    echo performance > "$g" && n=$((n + 1))
done
logger -t saw-aes67 "CPU governor: performance on $n CPU(s)" 2>/dev/null || true
exit 0
EOF
    chmod 755 "$1"
}

# The bridge's unit. Not Requires= the daemon: the bridge waits for the daemon
# and for PTP by itself, and a daemon restart must not stop it - it reopens the
# card when PTP is back. RestrictRealtime stays off: its audio threads run
# SCHED_FIFO, which LimitRTPRIO allows without any capability. LimitMEMLOCK:
# the bridge locks itself into RAM, as page faults in an audio thread glitch.
write_bridge_unit() {   # write_bridge_unit FILE API_PORT
    cat > "$1" <<EOF
[Unit]
Description=SAW AES67 bridge: the player's audio onto the AES67 card
After=sound.target $DAEMON_SERVICE.service

[Service]
ExecStart=$BRIDGE_BIN --api-port $2
Restart=always
RestartSec=2
DynamicUser=yes
SupplementaryGroups=audio
RuntimeDirectory=saw-aes67
RuntimeDirectoryMode=0755
LimitRTPRIO=90
LimitMEMLOCK=infinity
RestrictRealtime=no
CapabilityBoundingSet=
DevicePolicy=closed
DeviceAllow=char-alsa rw
NoNewPrivileges=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
PrivateTmp=yes
ProtectHome=yes
ProtectSystem=strict
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_INET AF_UNIX
RestrictNamespaces=yes
SystemCallArchitectures=native

[Install]
WantedBy=multi-user.target
EOF
}

# Per-box AES67 state for cloned SD cards; runs before the daemon at every
# boot. SAW_CONF, SAW_STATUS, SAW_IDENTITY and SYSFS_NET are test hooks.
write_identity_script() {   # write_identity_script FILE
    cat > "$1" <<'EOF'
#!/usr/bin/env bash
# saw-aes67-identity - installed by install-aes67.sh. See install_identity().
set -uo pipefail
CONF="${SAW_CONF:-/etc/daemon.conf}"
STATUS="${SAW_STATUS:-/etc/status.json}"
REC="${SAW_IDENTITY:-/etc/saw-aes67/identity}"
NET="${SYSFS_NET:-/sys/class/net}"
IFACE="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("interface_name","eth0"))' "$CONF" 2>/dev/null || echo eth0)"
for _ in $(seq 1 20); do [ -r "$NET/$IFACE/address" ] && break; sleep 0.5; done
MAC="$(cat "$NET/$IFACE/address" 2>/dev/null || true)"
case "$MAC" in
    [0-9a-fA-F][0-9a-fA-F]:[0-9a-fA-F][0-9a-fA-F]:[0-9a-fA-F][0-9a-fA-F]:[0-9a-fA-F][0-9a-fA-F]:[0-9a-fA-F][0-9a-fA-F]:[0-9a-fA-F][0-9a-fA-F]) ;;
    *) logger -t saw-aes67 "no usable MAC on $IFACE - identity unchanged" 2>/dev/null || true; exit 0 ;;
esac
[ "$(cat "$REC" 2>/dev/null)" = "$MAC" ] && exit 0
b5=$((16#$(echo "$MAC" | cut -d: -f5))); b6=$((16#$(echo "$MAC" | cut -d: -f6)))
if [ "$b5" -eq 0 ] && [ "$b6" -eq 1 ]; then b6=2; fi
MCAST="239.1.$b5.$b6"
python3 - "$CONF" "$MCAST" <<'PY' || { logger -t saw-aes67 "could not update $CONF" 2>/dev/null || true; exit 0; }
import json, os, sys, tempfile
conf, mcast = sys.argv[1], sys.argv[2]
cfg = json.load(open(conf))
cfg["rtp_mcast_base"] = mcast
cfg["rtp_mcast_base_sec"] = mcast
st = os.stat(conf)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(conf) or ".", prefix=".daemon.conf.")
with os.fdopen(fd, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
os.chmod(tmp, 0o644)
try:
    os.chown(tmp, st.st_uid, st.st_gid)
except PermissionError:
    pass
os.replace(tmp, conf)
PY
OWNER="$(stat -c %U "$STATUS" 2>/dev/null || echo root)"
printf '{"sources":[],"sinks":[]}\n' > "$STATUS.tmp" \
    && { chown "$OWNER" "$STATUS.tmp" 2>/dev/null || true; } \
    && mv -f "$STATUS.tmp" "$STATUS"
echo "$MAC" > "$REC"
logger -t saw-aes67 "new box identity $MAC: AES67 group $MCAST, stored streams cleared" 2>/dev/null || true
exit 0
EOF
    chmod 755 "$1"
}

# =============================================================================
# Steps with side effects
# =============================================================================

report_state() {
    header "What is on this box"
    local krel; krel="$(uname -r)"
    info "kernel:      $krel ($(uname -m))"
    local kn; kn="$(kernel_num "$krel")"
    if [ "$kn" -ge 615 ]; then
        warn "             kernel >= 6.15 sends RTP in bursts (ravenna-alsa-lkm#39):"
        warn "             receivers with small buffers stutter. 6.12 is the proven one."
    fi
    if [ -d "/lib/modules/$krel/build" ]; then
        info "headers:     present"
    else
        info "headers:     missing (will install one of: $(headers_candidates "$krel" | tr '\n' ' '))"
    fi
    info "interface:   $(default_iface)  -> this box's group would be $(box_mcast_address "$(default_iface)")"
    [ -d "/proc/asound/$CARD_NAME" ] && info "card:        $CARD_NAME registered" \
                                     || info "card:        $CARD_NAME not registered"
    [ -d "/proc/asound/$LOOP_ID" ]   && info "loopback:    $LOOP_ID registered" \
                                     || info "loopback:    $LOOP_ID not registered"
    if [ -x "$DAEMON" ]; then
        info "daemon:      installed ($(cat "$STATE_FILE" 2>/dev/null | sed -n 's/^REF=//p' || true))"
    else
        info "daemon:      not installed"
    fi
    systemctl is-active --quiet "$DAEMON_SERVICE" 2>/dev/null \
        && info "service:     $DAEMON_SERVICE running" \
        || info "service:     $DAEMON_SERVICE not running"
    if have curl && curl -fsS --max-time 2 "http://127.0.0.1:$HTTP_PORT/api/ptp/status" >/dev/null 2>&1; then
        info "PTP:         $(curl -fsS --max-time 2 "http://127.0.0.1:$HTTP_PORT/api/ptp/status" 2>/dev/null)"
    fi
    if [ -x "$BRIDGE_BIN" ]; then
        if systemctl is-active --quiet "$BRIDGE_SERVICE" 2>/dev/null; then
            info "bridge:      running ($(sed -n 's/.*"state": "\([a-z_]*\)".*/\1/p' /run/saw-aes67/bridge.json 2>/dev/null | head -1))"
        else
            info "bridge:      installed, not running"
        fi
    else
        info "bridge:      not installed"
    fi
    pgrep -x pulseaudio >/dev/null 2>&1 && warn "pulseaudio:  running - fights the daemon for the card"
    pgrep -x pipewire   >/dev/null 2>&1 && warn "pipewire:    running - make sure nothing opens hw:$CARD_NAME"
    local gov; gov="$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo '?')"
    info "governor:    $gov"
    info "memory:      $(awk '/^MemAvailable:/ {printf "%d MB available", $2/1024}' /proc/meminfo) -> $(build_jobs) build job(s)"
}

# A healthy install of this same version is left alone. Restarting a daemon
# onto an identical build hung a ROCK 5B inside the driver - no SSH, sysrq only -
# so "re-running is safe" has to mean "re-running does nothing". Each failed
# check is logged so a rebuild always says why it happened.
is_current() {
    local ok=0 why=()
    [ "$(sed -n 's/^REF=//p' "$STATE_FILE" 2>/dev/null)" = "$DAEMON_REF" ] || { ok=1; why+=("pin is not $DAEMON_REF"); }
    [ -x "$DAEMON" ] || { ok=1; why+=("no daemon binary"); }
    systemctl is-active --quiet "$DAEMON_SERVICE" 2>/dev/null || { ok=1; why+=("daemon not running"); }
    modinfo -F vermagic MergingRavennaALSA 2>/dev/null | grep -q "$(uname -r)" \
        || { ok=1; why+=("module not built for kernel $(uname -r)"); }
    [ -d "/proc/asound/$LOOP_ID" ] || { ok=1; why+=("loopback $LOOP_ID not loaded"); }
    [ "$ok" -eq 0 ] || info "will build: ${why[*]}"
    return "$ok"
}

need_internet() {
    curl -fsS --max-time 10 -o /dev/null https://github.com 2>/dev/null && return 0
    die "this needs internet access (apt and GitHub).
  For a unit behind a firewall: run this on a connected unit and clone its SD
  card - the first-boot identity check gives each clone its own stream group."
}

# ── The player and the build's memory ────────────────────────────────────────
# Stopped during the build: RAM pinning (v12) locks the clips into memory, and
# the daemon's compile needs ~1.2 GB per job. Restarted on exit, whatever
# happens, by cleanup().
PLAYER_WAS_STOPPED=0
SWAPFILE=""

stop_player() {
    [ "$STOP_PLAYER" -eq 1 ] || { warn "--keep-player: leaving the player running during the build"; return; }
    if systemctl is-active --quiet "$PLAYER_SERVICE" 2>/dev/null; then
        warn "stopping $PLAYER_SERVICE for the build (the screen goes dark until it finishes)"
        systemctl stop "$PLAYER_SERVICE" && PLAYER_WAS_STOPPED=1
    fi
}

add_temp_swap() {
    local existing_kb
    existing_kb="$(awk '/^SwapTotal:/ {print $2; exit}' /proc/meminfo 2>/dev/null || echo 0)"
    case "$existing_kb" in ''|*[!0-9]*) existing_kb=0 ;; esac
    [ "$existing_kb" -ge 1048576 ] && return 0
    have mkswap || return 0
    local target="${BUILD_DIR%/*}/saw-aes67-build-swap"
    info "adding ${SWAPFILE_MB} MB of temporary swap for the build (removed on exit)"
    if ! fallocate -l "${SWAPFILE_MB}M" "$target" 2>/dev/null; then
        dd if=/dev/zero of="$target" bs=1M count="$SWAPFILE_MB" status=none 2>/dev/null \
            || { rm -f "$target"; warn "could not create swap - carrying on without it"; return 0; }
    fi
    chmod 600 "$target"
    if mkswap "$target" >/dev/null 2>&1 && swapon "$target" >/dev/null 2>&1; then
        SWAPFILE="$target"
    else
        rm -f "$target"; warn "could not enable the temporary swap - carrying on without it"
    fi
}

cleanup() {
    if [ -n "$SWAPFILE" ]; then
        swapoff "$SWAPFILE" >/dev/null 2>&1; rm -f "$SWAPFILE"; SWAPFILE=""
    fi
    if [ "$PLAYER_WAS_STOPPED" -eq 1 ]; then
        info "restarting $PLAYER_SERVICE"
        systemctl start "$PLAYER_SERVICE" || warn "could not restart $PLAYER_SERVICE - start it by hand"
        PLAYER_WAS_STOPPED=0
    fi
}

# ── Dependencies ─────────────────────────────────────────────────────────────
install_deps() {
    header "Build dependencies"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq || die "apt-get update failed"
    local krel; krel="$(uname -r)"
    # From the skill's list. libfaac-dev is left out on purpose: the daemon's
    # streamer is built OFF (it holds the only capture substream, and is a
    # file-to-AES67 feature this player does not use).
    local pkgs=(build-essential clang cmake git pkg-config bc
                libasound2-dev libsamplerate0-dev alsa-utils linuxptp libssl-dev
                libavahi-client-dev libsystemd-dev libboost-all-dev
                curl nftables psmisc python3)
    if [ ! -d "/lib/modules/$krel/build" ]; then
        local h found=""
        for h in $(headers_candidates "$krel"); do
            if apt-cache show "$h" >/dev/null 2>&1; then found="$h"; break; fi
        done
        [ -n "$found" ] || die "no kernel headers package for $krel (tried: $(headers_candidates "$krel" | tr '\n' ' '))"
        info "kernel headers: $found"
        pkgs+=("$found")
    fi
    # One name apt does not know fails the whole transaction (linux-sound-base
    # and alsa-base vanished in Bookworm), so filter first.
    local have_pkgs=() absent=() p
    for p in "${pkgs[@]}"; do
        if apt-cache show "$p" >/dev/null 2>&1; then have_pkgs+=("$p"); else absent+=("$p"); fi
    done
    [ ${#absent[@]} -eq 0 ] || warn "not in this distribution, skipping: ${absent[*]}"
    apt-get install -y -qq "${have_pkgs[@]}" || die "apt-get install failed"
    # Headers unpacked without the build symlink fail the module build oddly.
    if [ ! -e "/lib/modules/$krel/build" ] && [ -d "/usr/src/linux-headers-$krel" ]; then
        ln -sfn "/usr/src/linux-headers-$krel" "/lib/modules/$krel/build"
        info "linked /lib/modules/$krel/build"
    fi
    [ -d "/lib/modules/$krel/build" ] || die "kernel headers for $krel are still missing"
}

fetch_source() {
    header "Sources ($DAEMON_REF)"
    if [ -d "$BUILD_DIR/.git" ]; then
        info "reusing $BUILD_DIR"
    else
        mkdir -p "$(dirname "$BUILD_DIR")" || die "cannot create $(dirname "$BUILD_DIR")"
        git clone --quiet "$DAEMON_URL" "$BUILD_DIR" || die "git clone failed"
    fi
    if [ -n "$(git -C "$BUILD_DIR" status --porcelain 2>/dev/null)" ]; then
        warn "build tree has local changes - not moving it to $DAEMON_REF"
    else
        git -C "$BUILD_DIR" fetch --quiet --tags origin || die "git fetch failed"
        git -C "$BUILD_DIR" checkout --quiet --detach "origin/$DAEMON_REF" 2>/dev/null \
            || git -C "$BUILD_DIR" checkout --quiet --detach "$DAEMON_REF" \
            || die "cannot check out $DAEMON_REF"
    fi
    git -C "$BUILD_DIR" submodule update --init --recursive --depth 1 \
        || die "submodule update failed"
    local drv="$BUILD_DIR/3rdparty/ravenna-alsa-lkm"
    [ -d "$drv/driver" ] || die "ravenna-alsa-lkm submodule is missing"
    # The module only talks to this daemon on its aes67-daemon branch.
    if git -C "$drv" fetch --quiet --depth 1 origin aes67-daemon 2>/dev/null; then
        git -C "$drv" checkout --quiet --detach FETCH_HEAD || die "cannot check out the module's aes67-daemon branch"
        info "module: aes67-daemon branch"
    else
        die "the module's aes67-daemon branch could not be fetched"
    fi
}

host_settings() {
    header "Host settings"
    cat > /etc/sysctl.d/90-aes67.conf <<'EOF'
# Written by install-aes67.sh - required by the AES67 daemon.
net.ipv4.igmp_max_memberships = 66      # default 20 runs out silently, one per stream
kernel.sched_rt_runtime_us = 1000000    # >= 5.10 RT throttle fails the latency test
kernel.perf_cpu_time_max_percent = 0    # scaling events = seconds of distortion
EOF
    sysctl -p /etc/sysctl.d/90-aes67.conf >/dev/null 2>&1 \
        || warn "some sysctl settings take effect only after a reboot"

    # snd-pcm first: a headless box does not load the ALSA core on its own.
    cat > /etc/modules-load.d/saw-aes67.conf <<EOF
# Written by install-aes67.sh
snd-pcm
MergingRavennaALSA
snd-aloop
EOF
    # A fixed id, so the player's device name never depends on card numbering.
    cat > /etc/modprobe.d/saw-aes67.conf <<EOF
# Written by install-aes67.sh - the loopback the player's audio bridge uses.
options snd-aloop id=$LOOP_ID pcm_substreams=1
EOF
    cat > /etc/udev/rules.d/90-saw-aes67-sound.rules <<'EOF'
SUBSYSTEM=="sound", GROUP="audio", MODE="0660"
EOF
    udevadm control --reload >/dev/null 2>&1 || true
    id -nG pi 2>/dev/null | grep -qw audio || usermod -aG audio pi 2>/dev/null || true

    # PulseAudio respawns and fights the daemon for the card.
    if pgrep -x pulseaudio >/dev/null 2>&1; then
        warn "masking PulseAudio - it fights the daemon for the card"
        systemctl --global mask pulseaudio.service pulseaudio.socket >/dev/null 2>&1 || true
        systemctl mask pulseaudio.service pulseaudio.socket >/dev/null 2>&1 || true
        pkill -x pulseaudio 2>/dev/null || true
    fi
    pgrep -x pipewire >/dev/null 2>&1 && \
        warn "PipeWire is running: make sure nothing opens hw:$CARD_NAME (WirePlumber may)"
    info "sysctl, module loading, snd-aloop ($LOOP_ID), udev: done"
}

build_module() {
    header "Kernel module"
    local dir="$BUILD_DIR/3rdparty/ravenna-alsa-lkm/driver"
    ( cd "$dir" || exit 1
      make clean >/dev/null 2>&1
      if [ "$(kernel_num "$(uname -r)")" -ge 610 ]; then
          # Upstream's build.sh takes every kernel >= 6.10 down the clang path.
          info "kernel $(uname -r): building with clang"
          make CC=clang || { warn "clang build failed - trying gcc"; make clean >/dev/null 2>&1; make; }
      else
          make
      fi
    ) || die "the module did not build. If you see '-fmin-function-alignment=8', the
  headers were built by a newer gcc than this one - compare
  /lib/modules/$(uname -r)/build/include/generated/compile.h with 'gcc --version'.
  It looks like a driver bug; it is not."
    [ -f "$dir/MergingRavennaALSA.ko" ] || die "build finished but produced no MergingRavennaALSA.ko"
    local dest
    dest="/lib/modules/$(uname -r)/extra"
    mkdir -p "$dest"
    install -m 644 "$dir/MergingRavennaALSA.ko" "$dest/" || die "cannot install the module"
    depmod -a "$(uname -r)" || die "depmod failed"
    # Plain install, not DKMS, as obs-multisite settled on: the build takes a
    # submodule branch and a compiler choice a DKMS hook cannot reconstruct
    # reliably. A kernel upgrade means re-running this - hence hold_kernel().
    info "installed $dest/MergingRavennaALSA.ko"
}

load_modules() {
    modprobe snd-pcm 2>/dev/null || true
    if ! lsmod | grep -q '^MergingRavennaALSA'; then
        modprobe MergingRavennaALSA || die "the module would not load - see dmesg"
    fi
    if ! lsmod | grep -q '^snd_aloop'; then
        modprobe snd-aloop || die "snd-aloop would not load - see dmesg"
    fi
    # The card registers a moment after modprobe: wait, never fail fatally.
    local i
    for i in 2 4 6 10; do
        [ -d "/proc/asound/$CARD_NAME" ] && [ -d "/proc/asound/$LOOP_ID" ] && break
        sleep "$i"
    done
    [ -d "/proc/asound/$CARD_NAME" ] || warn "$CARD_NAME has not registered yet - it may after a reboot"
    [ -d "/proc/asound/$LOOP_ID" ]   || warn "$LOOP_ID has not registered - check 'modinfo snd-aloop'"
}

build_daemon() {
    header "Daemon ($DAEMON_REF)"
    # The WebUI released with this tag, not latest.
    mkdir -p "$WEBUI_ROOT"
    if [ "$(cat "$WEBUI_ROOT/.webui-ref" 2>/dev/null)" != "$DAEMON_REF" ] || [ ! -f "$WEBUI_DIST/index.html" ]; then
        local tgz="$BUILD_DIR/webui.tar.gz"
        curl -fsSL -o "$tgz" "https://github.com/bondagit/aes67-linux-daemon/releases/download/$DAEMON_REF/webui.tar.gz" \
            || die "could not download the WebUI for $DAEMON_REF"
        rm -rf "$WEBUI_DIST"
        tar -xzf "$tgz" -C "$WEBUI_ROOT" || die "could not extract the WebUI"
        echo "$DAEMON_REF" > "$WEBUI_ROOT/.webui-ref"
    fi

    add_temp_swap
    local jobs; jobs="$(build_jobs)"
    info "building with $jobs job(s) - 20-40 minutes on a Pi"
    local D="$BUILD_DIR"
    cmake -S "$D/daemon" -B "$D/build" -DCMAKE_BUILD_TYPE=Release \
        -DBoost_NO_WARN_NEW_VERSIONS=1 \
        -DCPP_HTTPLIB_DIR="$D/3rdparty/cpp-httplib" \
        -DRAVENNA_ALSA_LKM_DIR="$D/3rdparty/ravenna-alsa-lkm" \
        -DWITH_AVAHI=ON -DWITH_SYSTEMD=ON -DFAKE_DRIVER=OFF \
        -DENABLE_TESTS=OFF -DWITH_NMOS=OFF -DWITH_STREAMER=OFF >/dev/null \
        || die "cmake failed"
    if ! cmake --build "$D/build" -j"$jobs"; then
        warn "if the Pi became unresponsive or rebooted, it ran out of memory:"
        warn "re-run with  --jobs 1"
        die "the daemon build failed"
    fi
    [ -f "$D/build/aes67-daemon" ] || die "build finished but produced no aes67-daemon"
    install -m 755 "$D/build/aes67-daemon" "$DAEMON" || die "cannot install the daemon"
    if [ -d "$D/daemon/scripts" ]; then
        mkdir -p "$WEBUI_ROOT/scripts" && cp -a "$D/daemon/scripts/." "$WEBUI_ROOT/scripts/"
    fi
    if ! id "$DAEMON_USER" >/dev/null 2>&1; then
        getent group audio >/dev/null 2>&1 || groupadd --system audio
        useradd --system --gid audio --no-create-home --shell /usr/sbin/nologin "$DAEMON_USER" \
            || die "cannot create user $DAEMON_USER"
    fi
    usermod -aG audio "$DAEMON_USER" 2>/dev/null || true
    info "installed $DAEMON"
}

# A port nothing else holds. A daemon that cannot bind starts, reports itself
# active, and answers nothing.
choose_http_port() {
    local p="$HTTP_PORT"
    if [ "$p" -eq "$PLAYER_WEB_PORT" ]; then p=8081; fi
    if have ss && ss -ltnH "sport = :$p" 2>/dev/null | grep -q . \
       && ! systemctl is-active --quiet "$DAEMON_SERVICE" 2>/dev/null; then
        warn "port $p is taken by something else - using 8081"
        p=8081
    fi
    HTTP_PORT="$p"
}

write_config() {
    header "Daemon configuration"
    choose_http_port
    local iface mcast; iface="$(default_iface)"; mcast="$(box_mcast_address "$iface")"
    if [ -f "$DAEMON_CONF" ]; then
        cp -a "$DAEMON_CONF" "$DAEMON_CONF.bak"
        [ "$REWRITE_CONFIG" -eq 1 ] && rm -f "$DAEMON_CONF"
    fi
    merge_daemon_conf "$DAEMON_CONF" "$iface" "$HTTP_PORT" "$mcast" "$PTP_DOMAIN" \
        || die "could not write $DAEMON_CONF"
    [ -f "$STATUS_FILE" ] || printf '{"sources":[],"sinks":[]}\n' > "$STATUS_FILE"
    # Under ProtectSystem=strict these are the only paths the daemon may write.
    chown "$DAEMON_USER" "$DAEMON_CONF" "$STATUS_FILE" 2>/dev/null || true
    info "interface $iface, REST port $HTTP_PORT, group $mcast"
}

install_service() {
    header "Services"
    # Upstream's unit and hardening; the user is created above rather than via
    # sysusers.d. ProcSubset=all: the daemon reads /proc/net/arp.
    cat > "/etc/systemd/system/$DAEMON_SERVICE.service" <<EOF
[Unit]
Description=AES67 daemon (installed by install-aes67.sh)
After=network.target saw-aes67-identity.service
Wants=saw-aes67-identity.service

[Service]
Type=notify
WatchdogSec=10
TimeoutStartSec=0
User=$DAEMON_USER
SupplementaryGroups=audio
ExecStart=$DAEMON
CapabilityBoundingSet=
DevicePolicy=closed
DeviceAllow=char-alsa
DeviceAllow=/dev/snd/*
LockPersonality=yes
MemoryDenyWriteExecute=yes
NoNewPrivileges=yes
PrivateDevices=no
PrivateMounts=yes
PrivateTmp=yes
PrivateUsers=yes
ProcSubset=all
ProtectClock=no
ProtectControlGroups=yes
ProtectHome=yes
ProtectHostname=yes
ProtectKernelLogs=yes
ProtectKernelModules=yes
ProtectKernelTunables=yes
ProtectProc=invisible
ProtectSystem=strict
RemoveIPC=yes
RestrictAddressFamilies=AF_INET AF_NETLINK AF_UNIX
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
SystemCallArchitectures=native
SystemCallFilter=~@clock
SystemCallFilter=~@cpu-emulation
SystemCallFilter=~@debug
SystemCallFilter=~@module
SystemCallFilter=~@mount
SystemCallFilter=~@obsolete
SystemCallFilter=~@privileged
SystemCallFilter=~@raw-io
SystemCallFilter=~@reboot
SystemCallFilter=~@resources
SystemCallFilter=~@swap
UMask=077
ReadWritePaths=$DAEMON_CONF
ReadWritePaths=$STATUS_FILE

[Install]
WantedBy=multi-user.target
EOF

    install_identity
    [ "$SET_GOVERNOR" -eq 1 ] && install_governor
    [ "$FIREWALL" -eq 1 ] && install_firewall

    systemctl daemon-reload || die "systemctl daemon-reload failed"
    # enable THEN restart: `enable --now` leaves an old daemon running on the
    # old binary and port.
    systemctl enable "$DAEMON_SERVICE" >/dev/null 2>&1 || warn "could not enable $DAEMON_SERVICE"
    systemctl restart "$DAEMON_SERVICE" || warn "$DAEMON_SERVICE did not start - journalctl -u $DAEMON_SERVICE -n 40"

    # "active" is not "serving": wait for the REST API itself.
    local i
    for i in $(seq 1 30); do
        curl -fsS --max-time 2 "http://127.0.0.1:$HTTP_PORT/api/version" >/dev/null 2>&1 && break
        sleep 1
    done
    if curl -fsS --max-time 2 "http://127.0.0.1:$HTTP_PORT/api/version" >/dev/null 2>&1; then
        info "daemon answering on 127.0.0.1:$HTTP_PORT"
    else
        warn "daemon is not answering on port $HTTP_PORT - journalctl -u $DAEMON_SERVICE -n 40"
    fi
}

# Runs before the daemon at every boot. A cloned SD card carries the golden
# box's per-box settings: its multicast group in daemon.conf, and its stream in
# status.json - which the daemon restores and starts sending at once, on the
# golden box's group, before the player can correct it. So when the MAC differs
# from the one recorded, regenerate the group and clear the stored streams; the
# player recreates its stream with this box's own address.
install_identity() {
    mkdir -p "$SAW_ETC"
    write_identity_script "$IDENTITY_BIN"
    cat > /etc/systemd/system/saw-aes67-identity.service <<EOF
[Unit]
Description=SAW AES67 per-box identity (cloned SD cards)
Before=$DAEMON_SERVICE.service
After=local-fs.target

[Service]
Type=oneshot
ExecStart=$IDENTITY_BIN
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
    systemctl enable saw-aes67-identity.service >/dev/null 2>&1 || true
    # Record THIS box now, so the golden box itself is not "new" at next boot.
    local mac; mac="$(cat "/sys/class/net/$(default_iface)/address" 2>/dev/null || true)"
    [ -n "$mac" ] && echo "$mac" > "$IDENTITY_FILE"
}

install_governor() {
    # A fanless enclosure may not cope with the heat: --keep-governor, and watch
    # 'vcgencmd measure_temp'.
    write_governor_script "$GOVERNOR_BIN"
    cat > /etc/systemd/system/saw-aes67-governor.service <<EOF
[Unit]
Description=SAW AES67 CPU governor: performance
Before=$DAEMON_SERVICE.service

[Service]
Type=oneshot
ExecStart=$GOVERNOR_BIN
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
    systemctl enable saw-aes67-governor.service >/dev/null 2>&1 || true
    systemctl start saw-aes67-governor.service >/dev/null 2>&1 || true
}

install_firewall() {
    # The daemon's REST API binds 0.0.0.0, has no authentication and sends
    # Access-Control-Allow-Origin: * - any web page any operator visits could
    # reconfigure its streams. The player talks to it over loopback; nothing
    # else needs it. RTSP (8854) stays open: RAVENNA receivers fetch SDP there.
    mkdir -p "$SAW_ETC"
    write_nft_ruleset "$NFT_FILE" "$HTTP_PORT"
    cat > /etc/systemd/system/saw-aes67-firewall.service <<EOF
[Unit]
Description=SAW AES67: keep the daemon REST API on localhost
Before=$DAEMON_SERVICE.service
After=network-pre.target

[Service]
Type=oneshot
ExecStart=/usr/sbin/nft -f $NFT_FILE
ExecStop=/usr/sbin/nft delete table inet saw_aes67
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
    systemctl enable saw-aes67-firewall.service >/dev/null 2>&1 || true
    systemctl restart saw-aes67-firewall.service >/dev/null 2>&1 \
        || warn "the firewall rule did not load - the daemon REST API is reachable from the LAN"
}

# ── The bridge ───────────────────────────────────────────────────────────────
# Its own step, so a re-run on a healthy install still brings the bridge up to
# date without going near the daemon (is_current). Needs no internet when its
# build dependencies are already here, which they are after the first install.
bridge_deps() {
    if have gcc && pkg-config --exists alsa samplerate 2>/dev/null; then return 0; fi
    need_internet
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq || die "apt-get update failed"
    apt-get install -y -qq gcc libc6-dev pkg-config libasound2-dev libsamplerate0-dev \
        || die "could not install the bridge's build dependencies"
}

install_bridge() {
    header "Audio bridge"
    [ -f "$BRIDGE_SRC/saw-aes67-bridge.c" ] \
        || die "the bridge source is missing ($BRIDGE_SRC) - run this from a full copy of the repo"
    # The player writes the channel setting here; the bridge only reads it.
    install -d -m 755 -o "$PLAYER_USER" -g "$PLAYER_USER" "$BRIDGE_VAR" 2>/dev/null \
        || install -d -m 755 "$BRIDGE_VAR"
    if [ ! -f "$BRIDGE_VAR/channels" ]; then
        echo 2 > "$BRIDGE_VAR/channels"
        chown "$PLAYER_USER:" "$BRIDGE_VAR/channels" 2>/dev/null || true
    fi

    local hash unit new_unit out
    hash="$(bridge_source_hash "$BRIDGE_SRC")" || die "cannot read the bridge source"
    unit="/etc/systemd/system/$BRIDGE_SERVICE.service"
    new_unit="$(mktemp)"
    write_bridge_unit "$new_unit" "$HTTP_PORT"
    if [ -x "$BRIDGE_BIN" ] && [ "$(cat "$BRIDGE_HASH_FILE" 2>/dev/null)" = "$hash" ] \
       && cmp -s "$new_unit" "$unit" && systemctl is-active --quiet "$BRIDGE_SERVICE" 2>/dev/null; then
        rm -f "$new_unit"
        info "bridge is current and running - left alone"
        return 0
    fi

    bridge_deps
    out="$(mktemp)"
    if ! build_bridge "$BRIDGE_SRC" "$out"; then
        rm -f "$out" "$new_unit"
        die "the bridge did not build"
    fi
    install -m 755 "$out" "$BRIDGE_BIN" || die "cannot install the bridge"
    install -m 644 "$new_unit" "$unit" || die "cannot install $unit"
    rm -f "$out" "$new_unit"
    systemctl daemon-reload || die "systemctl daemon-reload failed"
    systemctl enable "$BRIDGE_SERVICE" >/dev/null 2>&1 || warn "could not enable $BRIDGE_SERVICE"
    systemctl restart "$BRIDGE_SERVICE" \
        || warn "$BRIDGE_SERVICE did not start - journalctl -u $BRIDGE_SERVICE -n 40"
    mkdir -p "$SAW_ETC" && echo "$hash" > "$BRIDGE_HASH_FILE"
    info "bridge installed: $BRIDGE_BIN, status in /run/saw-aes67/bridge.json"
}

hold_kernel() {
    [ "$HOLD_KERNEL" -eq 1 ] || { warn "--no-hold-kernel: a kernel upgrade will remove the card until this is re-run"; return; }
    local pkgs
    pkgs="$(dpkg-query -W -f='${Package} ${Status}\n' 'linux-image-*' 'linux-headers-*' 'raspberrypi-kernel*' 2>/dev/null \
            | awk '$4 == "installed" {print $1}')"
    [ -n "$pkgs" ] || return 0
    # shellcheck disable=SC2086
    apt-mark hold $pkgs >/dev/null 2>&1 \
        && info "kernel packages held (apt-mark unhold them, upgrade, then re-run this)" \
        || warn "could not hold the kernel packages"
}

record_state() {
    mkdir -p "$SAW_ETC"
    {
        echo "REF=$DAEMON_REF"
        echo "KERNEL=$(uname -r)"
        echo "HTTP_PORT=$HTTP_PORT"
        echo "INSTALLED=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } > "$STATE_FILE.tmp" && mv -f "$STATE_FILE.tmp" "$STATE_FILE"
}

uninstall() {
    header "Removing the AES67 stack"
    local u
    for u in "$BRIDGE_SERVICE" "$DAEMON_SERVICE" saw-aes67-identity saw-aes67-governor saw-aes67-firewall; do
        systemctl disable --now "$u.service" >/dev/null 2>&1 || true
        rm -f "/etc/systemd/system/$u.service"
    done
    systemctl daemon-reload >/dev/null 2>&1 || true
    rm -f /etc/modules-load.d/saw-aes67.conf /etc/modprobe.d/saw-aes67.conf \
          /etc/sysctl.d/90-aes67.conf /etc/udev/rules.d/90-saw-aes67-sound.rules \
          "$IDENTITY_BIN" "$GOVERNOR_BIN" "$DAEMON" "$BRIDGE_BIN"
    rm -rf "$WEBUI_ROOT" "$BRIDGE_VAR"
    [ -f "$DAEMON_CONF" ] && mv -f "$DAEMON_CONF" "$DAEMON_CONF.bak"
    local held
    held="$(apt-mark showhold 2>/dev/null | grep -E '^(linux-image|linux-headers|raspberrypi-kernel)' || true)"
    [ -n "$held" ] && apt-mark unhold $held >/dev/null 2>&1 && info "kernel packages unheld"
    rm -rf "$SAW_ETC"
    info "removed. The module stays loaded until reboot; the build tree is $BUILD_DIR."
    info "Choose HDMI or USB in the player's Audio Output card if AES67 was selected."
}

main() {
    parse_args "$@"
    [ -n "${BASH_VERSION:-}" ] || die "run with bash"
    [ "$(id -u)" -eq 0 ] || die "run as root: sudo bash $0"

    if [ "$DO_UNINSTALL" -eq 1 ]; then uninstall; exit 0; fi
    report_state
    if [ "$CHECK_ONLY" -eq 1 ]; then header "Check only - nothing changed"; exit 0; fi

    [ -d /proc/asound ] || die "no /proc/asound - this machine has no sound support"
    if [ "$FORCE_REBUILD" -eq 0 ] && is_current; then
        header "Already installed and healthy - the daemon is left alone"
        info "re-running never restarts a healthy daemon (see is_current)."
        info "--force-rebuild to rebuild anyway."
        local p; p="$(sed -n 's/^HTTP_PORT=//p' "$STATE_FILE" 2>/dev/null)"
        case "$p" in ''|*[!0-9]*) ;; *) HTTP_PORT="$p" ;; esac
        install_bridge
        exit 0
    fi

    need_internet
    trap cleanup EXIT
    trap 'exit 130' INT TERM
    stop_player
    install_deps
    fetch_source
    host_settings
    build_module
    load_modules
    build_daemon
    write_config
    install_service
    install_bridge
    hold_kernel
    record_state

    header "Done (EXPERIMENTAL)"
    info "Next, in the player's web UI: Audio Output -> AES67 network."
    info "No audio at all is usually PTP: the daemon only follows a grandmaster"
    info "(your Q-SYS Core). Check:  curl -s http://127.0.0.1:$HTTP_PORT/api/ptp/status"
    info "'locked' with a non-zero gmid is what you want. The bridge opens the"
    info "card once it is:  cat /run/saw-aes67/bridge.json"
    info "Q-SYS: Auto mode lists streams announced by SAP; Manual mode needs the"
    info "address, port 5004, packet time 48, L24 and the channel count."
}

# Run only when executed, so the test suite can source the helpers.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
