# Stage Audio Works — Raspberry Pi Video Player

A headless video player for museum, exhibition, and event installations. Runs on a
Raspberry Pi 5, loops an attractor video, and plays trigger clips on a button press
or network command. Managed entirely from a web UI — no monitor or keyboard needed
after install.

---

> # ⚠️ NO WARRANTY — NO SUPPORT — USE ENTIRELY AT YOUR OWN RISK
>
> **Read this before you download, install, or run anything in this repository.**
>
> This code is published **as-is**, with **no warranty of any kind**, express or
> implied. It is internal tooling shared for reference. **It is not a product.**
>
> ### Stage Audio Works accepts no responsibility
>
> Stage Audio Works accepts **no responsibility and no liability whatsoever** for
> this software or for anything that results from using it — including, without
> limitation, hardware damage, data loss, corrupted SD cards, bricked or
> inaccessible devices, failed or disrupted installations, downtime during a live
> event or exhibition, missed deadlines, security exposure, or any direct,
> indirect, incidental, special or consequential loss or cost of any kind.
>
> **You alone are responsible for anything you do with this code.** Test it
> yourself, on hardware you can afford to lose, before it goes anywhere near
> something that matters.
>
> ### Stage Audio Works provides no support
>
> Stage Audio Works **cannot and will not provide any support for this code, to
> anyone, under any circumstances.** That means:
>
> - **No** installation, configuration, deployment or troubleshooting help
> - **No** bug fixes, patches, security updates or maintenance
> - **No** responses to issues, pull requests, emails, calls or messages about it
> - **No** guarantee it works, ever worked, or will continue to work
> - **No** commitment to maintain, update or keep this repository available
>
> Please do not contact Stage Audio Works for assistance with this software.
> Enquiries about it will not be answered.
>
> ### Understand what the install scripts do
>
> The scripts here **run as root and make system-level changes**: they install
> packages, write and enable systemd units, modify `/etc/sudoers.d/`, reconfigure
> the hardware watchdog, alter `/etc/systemd/system.conf`, and can reboot the
> machine. `bootstrap.sh` is designed to be piped straight into `sudo bash`.
>
> **Read every script yourself and satisfy yourself that it is safe and
> appropriate before running it.** Never pipe a script from the internet into a
> root shell on a machine you care about without reading it first.
>
> If you are not willing to accept every one of these terms in full, **do not use
> this software.**

---

## What it does

- Loops `loop.mp4` continuously while idle.
- A visitor presses one of up to 3 physical buttons → the matching clip plays once →
  returns to the loop.
- **Override mode** replaces the loop with `override.mp4` and ignores buttons (for
  closures or private functions).
- Clips are **locked into RAM** where memory allows — loop first — so playback
  carries on from memory even if the SD card turns slow or starts failing.
- **Software update from the web UI:** checks the published release and installs it
  only if it is newer, after verifying its checksum. It never downgrades.
- Each clip can be **uploaded, downloaded or deleted** from the web UI. Deleting asks
  for confirmation first, and warns more firmly for the loop video, since that is the
  one slot the player cannot run without.
- **Output resolution** can be forced from the web UI and persists across reboots.
  Only modes the attached display actually reports are offered, and the player falls
  back to the display's preferred mode automatically if it cannot start on the one
  chosen — so a wrong pick cannot leave a unit with no picture.
- **AES67 network audio** *(optional)*: on a unit where
  `install-aes67.sh` has been run, the audio can be sent to the network as an AES67
  stream — into a Q-SYS Core, for example — instead of HDMI. See below.
- **Bluetooth audio**: turn Bluetooth on in the web UI, scan, pair a speaker or
  headphones and use it as the audio output. It reconnects after a reboot, and if it
  drops out the player keeps trying — every few seconds for 20 attempts, then once a
  minute. Turning Bluetooth off sends the sound back to the previous output. Off
  until somebody turns it on.
- Everything is configurable through a Flask web UI on port **5000**; the unit is also
  controllable over the network via UDP on port **5505**.

### Video slots

All files live in `/home/pi/videos/`:

| Slot      | File           | Trigger                              |
|-----------|----------------|--------------------------------------|
| Loop      | `loop.mp4`     | Always, when idle                    |
| Trigger 1 | `trigger1.mp4` | GPIO 17 button, or `TRIGGER_1` UDP   |
| Trigger 2 | `trigger2.mp4` | GPIO 27 button, or `TRIGGER_2` UDP   |
| Trigger 3 | `trigger3.mp4` | GPIO 22 button, or `TRIGGER_3` UDP   |
| Override  | `override.mp4` | While Override mode is on            |

### UDP commands (port 5505)

`PLAY`, `PAUSE`, `RESTART_LOOP`, `OVERRIDE_ON`, `OVERRIDE_OFF`, `TRIGGER_1/2/3`.

```bash
echo -n "TRIGGER_1" | nc -u <pi-ip> 5505
```

## Target environment

Raspberry Pi 5, Raspberry Pi OS Bookworm 64-bit, user `pi`. Video is played by **mpv**
over DRM/KMS (device `/dev/dri/card1`, connector `HDMI-A-1`, HDMI port nearest the
USB-C socket). GPIO uses **lgpio** directly. Content is PAL region — **25 fps**.

> The Pi 5 needs a **5 V / 5 A** supply. An underpowered unit boots and idles fine,
> then crashes the moment the GPU loads — it looks exactly like a software fault.
> Check `dmesg | grep -i volt` first when diagnosing any oddity.

## Install & update

### Pi with internet — one line

Installs everything and starts the player. Run the **same command again** to update
a unit later; it pulls the latest code and skips any dependency already present.

```bash
curl -fsSL https://raw.githubusercontent.com/stageaudioworks/pi-video-player-dist/main/bootstrap.sh | sudo bash
```

Add `-s -- --reboot` to reboot when it finishes:

```bash
curl -fsSL https://raw.githubusercontent.com/stageaudioworks/pi-video-player-dist/main/bootstrap.sh | sudo bash -s -- --reboot
```


`bootstrap.sh` keeps its checkout in `/home/pi/pi-video-player` and then runs
`install.sh`. Both are non-interactive.

### Pi behind a firewall — no network calls

For units whose network blocks package and git access. Copy the repo files across
(SCP) and run:

```bash
sudo bash update.sh              # copy changed files + verify config
sudo bash update.sh --setup      # config/sudoers/Bluetooth services only, no file copy
sudo bash update.sh --restart    # restart the service
```

`update.sh` compares MD5s and does nothing if the unit is already current, so it is
safe to run against every Pi.

### By hand

```bash
sudo bash install.sh             # install/re-install, then start
sudo bash install.sh --uninstall # remove service + watchdog config
```

The service runs `/home/pi/saw_player.py`. Deploying means copying the versioned
script to that name and restarting `saw-player` — the install/update scripts pick the
highest-numbered `RPi_Video_Player_v*.py` automatically.

### AES67 network audio (optional)

Proven on the bench with a Q-SYS Core as the receiver and grandmaster-locked PTP:
clean audio, clip changes, cable pulls, reboots and power cuts.

Never installed by the one-liner. On a unit that already has the player, with internet
access for 20–40 minutes, install it **from the web UI** — the AES67 panel checks the
unit first, then shows the installer's progress — or over SSH. From the web UI, a Pi
with 3 GB or more normally keeps playing during the build, though the screen could
still go dark; a 2 GB Pi is stopped for it and goes dark for 20–40 minutes.

```bash
sudo bash /home/pi/pi-video-player/install-aes67.sh --check   # what is here; changes nothing
sudo bash /home/pi/pi-video-player/install-aes67.sh           # install, then reboot
```

It builds Merging's RAVENNA kernel module and the aes67-daemon, plus
**saw-aes67-bridge** — a small service that keeps the AES67 sound card open and fed
continuously, so receivers see one unbroken stream through every clip change, and
corrects for the drift between the Pi's clock and the network's PTP clock. Then choose
**AES67 network** in the web UI's Audio Output; an AES67 panel shows the stream's
state, stereo or mono, and a lip-sync offset. Once on air the stream stays on air —
silent while another output is chosen — so receivers never lose it.

Q-SYS: the stream appears in an AES67 receiver's list up to 30 seconds after it starts.
If a receiver ever shows the stream but plays nothing (seen after the aes67-daemon
restarts — the panel says when it has), change the **Player Name**: that renames the
stream, and Q-SYS picks the new name up straight away.

Needs: a Pi 5 with 2 GB or more, kernel 6.12 or later (6.15 and later send audio in
bursts that receivers with small buffers can stutter on; a Q-SYS Core played it cleanly
on 6.18), wired
Ethernet, and a PTP grandmaster on the network (normally the Q-SYS Core). Build one unit
and clone its SD card for the rest — each clone takes its own multicast group at first
boot. The kernel is held at its current version, because a kernel update removes the
sound card until the installer is run again. `--help` lists the options;
`--uninstall` removes it.

### Network configuration

Not handled by the installer. Set the hostname and IP with Raspberry Pi Imager, a DHCP
reservation, or `raspi-config` / `nmcli`.

## Diagnostics

```bash
dmesg | grep -i volt                        # check FIRST — undervoltage explains most faults
sudo systemctl status saw-player
journalctl -u saw-player -f
grep -m1 'Video Player  (v' /home/pi/saw_player.py   # which version is deployed?
pgrep -a mpv
cat /sys/class/drm/card1-HDMI-A-1/status    # is the display connected?

# AES67, on units that have it
sudo bash /home/pi/pi-video-player/install-aes67.sh --check
cat /run/saw-aes67/bridge.json              # the bridge's view: PTP, card, drift, errors
```

## Project state

- `RPi_Video_Player_v13.py` — **current version.**

## License

Copyright © 2026 Stage Audio Works

This program is free software: you can redistribute it and/or modify it under the
terms of the **GNU General Public License** as published by the Free Software
Foundation, either **version 3** of the License, or (at your option) any later
version.

This program is distributed in the hope that it will be useful, but **WITHOUT ANY
WARRANTY**; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU General Public License for more details.

The full license text is in [`LICENSE`](LICENSE). If it is missing, see
<https://www.gnu.org/licenses/>.

### No warranty and no support — restated

Sections 15, 16 and 17 of the GPL (see [`LICENSE`](LICENSE)) disclaim all warranty
and liability, and those terms apply here in full. To be unambiguous about it:

**Stage Audio Works accepts no responsibility or liability of any kind for this
software or its consequences, and provides no support for it to anyone, under any
circumstances.** There are no bug fixes, no updates, no troubleshooting assistance
and no responses to enquiries. Use of this software is entirely at your own risk.

Nothing in this repository creates any warranty, service commitment, support
obligation, or contractual relationship of any kind with Stage Audio Works.

See the notice at the top of this file for the full statement.
