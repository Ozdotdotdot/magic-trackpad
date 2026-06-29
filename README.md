# Magic Music

Turn an **Apple Magic Trackpad 2** into a physical music-control surface on Linux,
while it still works as a normal trackpad. It uses the trackpad's pressure sensor
and haptic actuator (the "Force Touch" hardware) so every action gives you a buzz.
The whole point is controlling music *by feel*, without looking.

Built and tested on **CachyOS / COSMIC (Wayland)**. Music playback is **MPD**,
controlled with `mpc`. System volume is **PipeWire**, controlled with `wpctl`.

```
3 fingers down               ->  "ready" buzz (gesture mode armed)
3-finger slide UP/DOWN       ->  volume (absolute slider, tick per notch)
3-finger slide LEFT/RIGHT    ->  previous / next track (one skip per swipe)
1-finger hard press (force)  ->  play / pause (MPD, via mpc)
3-finger hard press (force)  ->  play / pause (any MPRIS player, via playerctl)
```

The first move past a small dead zone **locks the gesture to one axis** (vertical
or horizontal), so volume and track-skip never trigger each other. Normal 1- and
2-finger use is untouched. 4-finger is left for COSMIC's workspace gestures.

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
(top byte is the click pressure threshold, low 3 bytes are the vibration pattern).

### Installing the driver

```sh
git clone https://github.com/NicoWeio/aur-hid-magicmouse-dkms.git
cd aur-hid-magicmouse-dkms
makepkg -si            # pulls dkms, builds against your kernel(s)
```

`deploy/install.sh` (below) then sets up a `modprobe.d` drop-in so this fork loads
with `host_click` on automatically at boot, replacing the stock driver. Without that,
a reboot reverts to the plain driver and you would have to `modprobe` by hand each time.

## Requirements

- The nexustar driver above (DKMS, so it survives kernel updates).
- `python-evdev`, a system package (`sudo pacman -S python-evdev`). It is the only
  third-party import.
- Python 3.11+ (for `tomllib`). Tested on 3.14.
- `mpc` (an MPD client) and `wpctl` (WirePlumber) on `PATH`.
- `playerctl` for the 3-finger force-click (controls any MPRIS player). Optional:
  without it that one gesture just no-ops.

## Install

```sh
sudo ./deploy/install.sh      # driver drop-ins, daemon, config, systemd service
sudo reboot                   # once, to confirm the driver auto-loads with host_click
```

After reboot, `systemctl status magicmusic` should show it active, and three fingers
on the pad should buzz. Logs live at `journalctl -u magicmusic -f`.

To run it by hand instead (development): `sudo python3 magicmusic.py`.

### Seamless reconnection

So the trackpad reconnects on its own when you switch it on (rather than only when
you open the Bluetooth settings panel), mark it trusted once:

```sh
bluetoothctl trust <trackpad-MAC>     # e.g. C0:95:6D:02:88:91
```

The trackpad is Bluetooth Classic and pages the host to reconnect on power-on, but
BlueZ only auto-accepts that page if the device is trusted. Pairing alone does not
set this. The setting persists across reboots.

## Configuration

All knobs live in **`/etc/magicmusic.toml`** (installed from `deploy/magicmusic.toml`,
which documents every key). Edit it, then run `sudo systemctl restart magicmusic`. The
file is optional, since the daemon has the same defaults baked in. The ones people
reach for most:

- `force_click`: how hard you press for play/pause (0 to 253). Lower it if a hard
  press feels like too much effort, raise it if play/pause fires by accident.
- `step_distance`: finger travel between volume notches (sets buzz cadence and how
  fast volume moves).
- `skips_across_pad`: how far a horizontal swipe must travel to skip a track.

## How it works (for the next hacker)

- **Passive read, no grab.** On COSMIC, 3-finger gestures are unclaimed (4-finger is
  workspace switching), so the daemon just *reads* the trackpad's evdev node alongside
  libinput. No `EVIOCGRAB`, no virtual device. (An earlier grab-based design corrupted
  libinput's multitouch state and caused phantom touches, so don't go back to it.)
- **Axis lock.** Finger count comes from MT slot tracking IDs. Once 3 fingers persist
  past a short debounce (which filters the 3-finger transient of a 4-finger swipe), the
  first movement past `volume_deadzone` locks to vertical or horizontal for the gesture.
- **Volume is an absolute slider.** On engage it reads the current volume once, then sets
  an absolute target as you slide (`anchor + steps`), fired non-blocking. Absolute rather
  than relative (`2%+`) avoids a lost-increment race and lets fast slides snap 1:1. The
  dead zone is a one-time gate, not a permanent band, so crossing your entry point stays
  smooth.
- **Track skip is one-shot per swipe**, with a symmetric threshold (truncate toward zero,
  not floor). Floor made left-swipes fire instantly and over-skip.
- **Haptics** are 15-byte HID output reports written straight to the trackpad's `hidraw`
  node (`F2 53 01 [b3] 78 02 [b6] 24 30 06 01 [b11] 18 48 12`, where byte 3 is strength).
  This works over Bluetooth with no driver involvement, which is why the fork's host-click
  mode has to be active.
- **Privilege split.** The daemon runs as root (raw hidraw + evdev), but `wpctl` needs the
  *user's* PipeWire session, so the volume subprocess drops to the user's uid/gid
  (`MM_UID`/`MM_GID` from the service, or `SUDO_UID` when run by hand) with their
  `XDG_RUNTIME_DIR`. `playerctl` drops to the user the same way (it needs their DBus
  session bus). `mpc` works as root directly, since it talks over a TCP socket.

### Machine-specific quirks worth knowing

- **MPD is intentionally not on MPRIS** here, so media keys (`KEY_NEXTSONG` and friends)
  do nothing for the music. All MPD control goes through `mpc`.
- **Volume is the PipeWire master** via `wpctl` (a hardware-knob feel), not MPD's own
  volume and not `playerctl` (there is usually no MPRIS player registered).
- Node numbers renumber across reboots, so the daemon finds the trackpad by name
  (`Apple Inc. Magic Trackpad 2`) and the `0265` hidraw, not by fixed paths.

COSMIC's on-screen volume display also appears on the `wpctl` changes, so you get the
OSD and fine control together.

## Ideas left on the table

- **Seek within a track** (maybe a different gesture, or a force-hold plus slide).
