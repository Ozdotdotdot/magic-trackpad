# Magic Music

Turn an **Apple Magic Trackpad 2** into a physical music-control surface on Linux,
while it still works as a normal trackpad. It uses the trackpad's pressure sensor
and haptic actuator (the "Force Touch" hardware) so every action gives you a buzz —
the whole point is controlling music *by feel*, without looking.

Built and tested on **CachyOS / COSMIC (Wayland)**. Music is **MPD** (controlled
with `mpc`); system volume is **PipeWire** (`wpctl`).

```
3 fingers down              ->  "ready" buzz (gesture mode armed)
3-finger slide UP/DOWN       ->  volume (absolute slider, tick per notch)
3-finger slide LEFT/RIGHT    ->  previous / next track (one skip per swipe)
1-finger hard press (force)  ->  play / pause
```

The first move past a small dead zone **locks the gesture to one axis** (vertical
or horizontal), so volume and track-skip never trigger each other. Normal 1- and
2-finger use is untouched; 4-finger is left for COSMIC's workspace gestures.

---

## Why this needs a custom driver

Out of the box, Linux's in-kernel `hid-magicmouse` driver gives you multitouch and
reads pressure, but it does **not** let the host drive the trackpad's haptic actuator
("host-click mode"). This project needs host-driven haptics, so it relies on the
**nexustar fork** of the driver:

- Fork: <https://github.com/nexustar/linux-hid-magicmouse>
- DKMS packaging (Arch/CachyOS): <https://github.com/NicoWeio/aur-hid-magicmouse-dkms>

The fork installs as a separate module named `hid-magicmouse-nexustar` (so it coexists
with the stock one) and adds `host_click=on` plus `button_down_param` / `button_up_param`
(top byte = click pressure threshold, low 3 bytes = vibration pattern).

### Installing the driver

```sh
git clone https://github.com/NicoWeio/aur-hid-magicmouse-dkms.git
cd aur-hid-magicmouse-dkms
makepkg -si            # pulls dkms, builds against your kernel(s)
```

`deploy/install.sh` (below) then sets up a `modprobe.d` drop-in so this fork loads —
with `host_click` on — automatically on boot, replacing the stock driver. (Without
that, a reboot reverts to the plain driver and you'd `modprobe` by hand each time.)

## Requirements

- The nexustar driver above (DKMS, so it survives kernel updates).
- `python-evdev` — system package (`sudo pacman -S python-evdev`). **No venv needed**;
  it's the only third-party import.
- Python 3.11+ (for `tomllib`). Tested on 3.14.
- `mpc` (an MPD client) and `wpctl` (WirePlumber) on `PATH`.

## Install

```sh
sudo ./deploy/install.sh      # driver drop-ins, daemon, config, systemd service
sudo reboot                   # once, to confirm the driver auto-loads with host_click
```

After reboot: `systemctl status magicmusic` should show it active, and three fingers
on the pad should buzz. Logs: `journalctl -u magicmusic -f`.

To run it by hand instead (development): `sudo python3 magicmusic.py`.

## Configuration

All knobs live in **`/etc/magicmusic.toml`** (installed from `deploy/magicmusic.toml`,
which documents every key). Edit it, then `sudo systemctl restart magicmusic`. The
file is optional — the daemon has the same defaults baked in. The two you'll reach
for most:

- `step_distance` — finger travel between volume notches (buzz cadence / volume speed).
- `skips_across_pad` — how far a horizontal swipe must travel to skip a track.

## How it works (for the next hacker)

- **Passive read, no grab.** On COSMIC, 3-finger gestures are unclaimed (4-finger is
  workspace switching), so the daemon just *reads* the trackpad's evdev node alongside
  libinput — no `EVIOCGRAB`, no virtual device. (An earlier grab-based design corrupted
  libinput's multitouch state and caused phantom touches; don't go back to it.)
- **Axis lock.** Finger count comes from MT slot tracking IDs. Once 3 fingers persist
  past a short debounce (which filters the 3-finger transient of a 4-finger swipe), the
  first movement past `volume_deadzone` locks to vertical or horizontal for the gesture.
- **Volume is an absolute slider.** On engage it reads the current volume once, then sets
  an absolute target as you slide (`anchor + steps`), fired non-blocking. Absolute (not
  relative `2%+`) avoids a lost-increment race and lets fast slides snap 1:1. The dead
  zone is a one-time gate, not a permanent band, so crossing your entry point is smooth.
- **Track skip is one-shot per swipe**, with a symmetric threshold (truncate toward zero,
  not floor — floor made left-swipes fire instantly and over-skip).
- **Haptics** are 15-byte HID output reports written straight to the trackpad's `hidraw`
  node (`F2 53 01 [b3] 78 02 [b6] 24 30 06 01 [b11] 18 48 12`; byte 3 = strength). This
  works over Bluetooth with no driver involvement — it's why we need the fork's host-click
  mode active.
- **Privilege split.** The daemon runs as root (raw hidraw + evdev), but `wpctl` needs the
  *user's* PipeWire session, so the volume subprocess drops to the user's uid/gid
  (`MM_UID`/`MM_GID` from the service; `SUDO_UID` when run by hand) with their
  `XDG_RUNTIME_DIR`. `mpc` works as root directly (it's a TCP socket).

### Machine-specific quirks worth knowing

- **MPD is intentionally not on MPRIS** here, so media keys (`KEY_NEXTSONG` etc.) do
  nothing for the music — all MPD control goes through `mpc`.
- **Volume is the PipeWire master** via `wpctl` (a hardware-knob feel), *not* MPD's own
  volume and *not* `playerctl` (there's usually no MPRIS player registered).
- Node numbers renumber across reboots, so the daemon finds the trackpad by name
  (`Apple Inc. Magic Trackpad 2`) and the `0265` hidraw, not by fixed paths.

COSMIC's on-screen volume display shows up on the `wpctl` changes too, so you get
the OSD *and* fine control for free.

## Ideas left on the table

- **Seek within a track** (e.g. a different gesture or a force-hold + slide).
- **Go rewrite?** Considered and declined: it's a ~250-line stdlib+evdev script that
  works; a static binary isn't worth losing Python's iteration speed and `python-evdev`'s
  maturity. Stay in Python.
