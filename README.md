# Magic Music

Turn an Apple Magic Trackpad 2 into a physical music-control surface on Linux,
while it still works as a normal trackpad. Uses the trackpad's pressure sensor
and haptic actuator (Force Touch) for feedback.

Built for **CachyOS / COSMIC (Wayland)**. Music is **MPD** (controlled via `mpc`);
system volume is **PipeWire** (via `wpctl`).

## Controls

| Gesture | Action |
|---|---|
| 3 fingers down | "ready" buzz — gesture mode armed |
| 3-finger vertical slide | Volume (absolute slider, haptic tick per notch) |
| 3-finger horizontal slide | Next / previous track (`mpc next`/`prev`), one skip per swipe |
| 1-finger force-click (hard press) | Play/pause (`mpc toggle`) |

The first move past the dead zone **locks the gesture to one axis** (whichever
dominates), so volume and track-skip never cross-fire. Track-skip is one-shot
per swipe — lift and swipe again to skip another.

Normal 1- and 2-finger use is untouched. 4-finger stays free for COSMIC's
workspace gestures (a short debounce keeps the 4-finger transient from buzzing).

## Requirements

- The **nexustar** fork of `hid-magicmouse`, which adds host-click mode + custom
  haptics. Installed via DKMS (`aur-hid-magicmouse-dkms/`, gitignored).
  Load with host-click enabled:
  ```sh
  sudo modprobe -r hid_magicmouse
  sudo modprobe hid-magicmouse-nexustar host_click=on button_down_param=0x203f0606 button_up_param=0x10110404
  ```
- Python `evdev`, plus `mpc` (MPD) and `wpctl` (WirePlumber) on PATH.

## Run

```sh
sudo python3 magicmusic.py
```

Root is needed for raw hidraw (haptics) + evdev access; the volume call drops to
the invoking user so `wpctl` reaches their PipeWire session.

## Tuning (top of `magicmusic.py`)

- `STEP_DISTANCE` — finger travel between volume notches (buzz cadence).
- `VOL_DELTA_PCT` — volume change per notch. Full-pad sweep = `(pad ÷ STEP_DISTANCE) × VOL_DELTA_PCT`.
- `FORCE_CLICK` — pressure threshold (0–253) for play/pause.
- `VOLUME_DEADZONE` — one-time travel gate so a 3-finger tap doesn't nudge volume.
