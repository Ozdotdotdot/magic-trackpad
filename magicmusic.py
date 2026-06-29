#!/usr/bin/env python3
"""
Magic Music, for COSMIC (Wayland).

On COSMIC, 3-finger gestures are unclaimed (no scroll, no workspace, that's
4-finger), so we read the trackpad PASSIVELY. No EVIOCGRAB, no virtual device,
no phantom-touch bugs.

Controls:
  - 3 fingers down            -> "ready" buzz (gesture mode armed)
  - 3-finger VERTICAL slide   -> volume, haptic tick per notch (up = louder)
  - 3-finger HORIZONTAL slide -> next/prev track (`mpc next`/`prev`), buzz per skip
  - 1-finger force-click >180 -> play/pause (`mpc toggle`). No movement check,
                                 so a hard press never needs to hold still.
  - 3-finger force-click >180 -> play/pause (`playerctl play-pause`), for any
                                 MPRIS player (Spotify, browsers, etc.).
  - 5-finger double-tap       -> toggle smart lights (Home Assistant webhook).

The first move past the dead zone LOCKS the gesture to one axis (whichever
dominates), so volume and track-skip never trigger each other.

Run with sudo (needs raw access to the evdev + hidraw nodes):
    sudo python3 magicmusic.py
"""
import glob
import os
import select
import subprocess
import time
import tomllib
import evdev
from evdev import ecodes


def _load_config():
    """Optional overrides from $MM_CONFIG (default /etc/magicmusic.toml). All keys
    optional; anything absent falls back to the defaults below."""
    path = os.environ.get("MM_CONFIG", "/etc/magicmusic.toml")
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except (PermissionError, tomllib.TOMLDecodeError) as e:
        print(f"warning: ignoring config at {path}: {e}")
        return {}


_cfg = _load_config()
def _c(key, default):
    return _cfg.get(key, default)

# --- tunables (override in /etc/magicmusic.toml; see magicmusic.toml.example) --
FORCE_CLICK      = _c("force_click", 180)       # 1-finger pressure for play/pause (0-253)
PRESSURE_REARM   = _c("pressure_rearm", 120)    # pressure must drop below this to re-arm
STEP_DISTANCE    = _c("step_distance", 90)      # trackpad units between volume notches
VOL_DELTA_PCT    = _c("vol_delta_pct", 2)       # volume % per notch
SKIPS_ACROSS_PAD = _c("skips_across_pad", 3)    # full-width swipe = this many track skips
VOLUME_DEADZONE  = _c("volume_deadzone", 200)   # axis-lock gate: travel before a slide acts
READY_DEBOUNCE   = _c("ready_debounce", 0.04)   # seconds 3 fingers must persist before buzz
SINK             = _c("sink", "@DEFAULT_AUDIO_SINK@")
# 5-finger double-tap -> toggle smart lights (Home Assistant webhook)
LIGHTS_URL       = _c("lights_url", "http://100.122.255.109:8123/api/webhook/togglelights")
TAP_MAX_DURATION = _c("tap_max_duration", 0.3)  # a "tap" is a 5-finger touch shorter than this
DOUBLE_TAP_GAP   = _c("double_tap_gap", 0.5)    # max seconds between the two taps
# haptic strengths (byte 3 of the actuator report, 0x00-0x7f)
READY_BUZZ       = _c("ready_buzz", 0x2a)       # 3 fingers landed -> gesture mode ready
TICK_BUZZ        = _c("tick_buzz", 0x12)        # light per-volume-step tick
SKIP_BUZZ        = _c("skip_buzz", 0x35)        # firmer buzz per track skip
TAP_BUZZ         = _c("tap_buzz", 0x3f)         # firm play/pause confirm (down-click)
# up-click when a force-press lifts. Matches the firmware's button_up waveform
# (0x11/0x04/0x04): it differs from the down-click in b6/b11, not just strength,
# which is what makes the pair feel like one real click. Tune with haptic_tune.py.
RELEASE_BUZZ     = _c("release_buzz", 0x11)     # up-click strength (b3)
RELEASE_B6       = _c("release_b6", 0x04)       # up-click texture (b6)
RELEASE_B11      = _c("release_b11", 0x04)      # up-click texture (b11)

# the daemon runs as root (for raw hidraw/evdev), but wpctl needs to reach the
# user's PipeWire session, so drop to that uid for the volume call. Under systemd
# the service sets MM_UID/MM_GID; when run by hand under sudo, SUDO_UID is used.
USER_UID = int(os.environ.get("MM_UID") or os.environ.get("SUDO_UID") or 1000)
USER_GID = int(os.environ.get("MM_GID") or os.environ.get("SUDO_GID") or USER_UID)
# wpctl reaches PipeWire via XDG_RUNTIME_DIR; playerctl reaches MPRIS players over
# the user's DBus session bus, so point DBUS_SESSION_BUS_ADDRESS at it explicitly.
USER_ENV = {
    "XDG_RUNTIME_DIR": f"/run/user/{USER_UID}",
    "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{USER_UID}/bus",
    "PATH": "/usr/bin:/bin",
}


def _drop_to_user():
    os.setgid(USER_GID)
    os.setuid(USER_UID)


def run_as_user(*cmd):
    """Run a command as the logged-in user (for tools that need their session bus,
    e.g. playerctl), non-blocking so the event loop never stalls."""
    subprocess.Popen(
        cmd, env=USER_ENV, preexec_fn=_drop_to_user,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def get_volume():
    """Current sink volume as a 0-1 float (read once when volume mode engages)."""
    out = subprocess.run(
        ["wpctl", "get-volume", SINK], env=USER_ENV, preexec_fn=_drop_to_user,
        capture_output=True, text=True,
    )
    try:
        return float(out.stdout.split()[1])   # "Volume: 0.78"
    except (IndexError, ValueError):
        return 0.5


_vol_procs = []

def set_volume_abs(level):
    """Set absolute sink volume, non-blocking. Absolute => no lost-increment race,
    stale calls self-correct, and the loop never stalls waiting on wpctl."""
    global _vol_procs
    _vol_procs = [p for p in _vol_procs if p.poll() is None]   # reap finished
    _vol_procs.append(subprocess.Popen(
        ["wpctl", "set-volume", "-l", "1.0", SINK, f"{level:.3f}"],
        env=USER_ENV, preexec_fn=_drop_to_user,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ))

# -------------------------------------------------------------------------


def find_trackpad_event():
    for path in evdev.list_devices():
        dev = evdev.InputDevice(path)
        abs_codes = {c for c, _ in dev.capabilities().get(ecodes.EV_ABS, [])}
        if "Magic Trackpad" in dev.name and ecodes.ABS_MT_PRESSURE in abs_codes:
            return dev
    return None


def find_hidraw():
    for sysdir in glob.glob("/sys/class/hidraw/hidraw*"):
        try:
            with open(f"{sysdir}/device/uevent") as f:
                if "0265" in f.read():
                    return "/dev/" + sysdir.rsplit("/", 1)[1]
        except OSError:
            continue
    return None


def wait_for_trackpad():
    """Block until the trackpad's evdev + hidraw nodes exist (it may connect after
    boot). Lets the systemd service start cleanly before the Bluetooth link is up."""
    announced = False
    while True:
        dev, hidpath = find_trackpad_event(), find_hidraw()
        if dev and hidpath:
            return dev, hidpath
        if not announced:
            print("waiting for Magic Trackpad (driver loaded? device connected?)...")
            announced = True
        time.sleep(2)


def haptic_report(b3, b6=0x06, b11=0x06):
    return bytes([0xF2, 0x53, 0x01, b3, 0x78, 0x02, b6, 0x24, 0x30, 0x06, 0x01, b11, 0x18, 0x48, 0x12])


def run(*cmd):
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_bg(*cmd):
    """Fire-and-forget, for calls that may be slow (e.g. a network curl) and must
    never stall the event loop."""
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    dev, hidpath = wait_for_trackpad()
    hid = open(hidpath, "wb", buffering=0)
    yinfo = dev.absinfo(ecodes.ABS_Y)
    xinfo = dev.absinfo(ecodes.ABS_X)
    step_units = max(1, STEP_DISTANCE)
    skip_units = max(1, (xinfo.max - xinfo.min) // SKIPS_ACROSS_PAD)
    notches = (yinfo.max - yinfo.min) // step_units
    print(f"trackpad: {dev.name} @ {dev.path}")
    print(f"vertical  : {step_units}u/notch -> ~{notches} notches -> "
          f"~{notches * VOL_DELTA_PCT}% volume across the full pad")
    print(f"horizontal: {skip_units}u/skip -> {SKIPS_ACROSS_PAD} skips across the full pad")
    print(f"3-finger = volume/skip, force-click ({FORCE_CLICK}) = play/pause "
          f"(1-finger -> mpc, 3-finger -> playerctl).")
    print("5-finger double-tap = toggle lights. Ctrl-C to quit.\n")

    def buzz(strength, b6=0x06, b11=0x06):
        hid.write(haptic_report(strength, b6, b11))

    # raw state, updated per event
    cur_slot = 0
    active = set()       # MT slots currently holding a finger
    slot_pressure = {}   # per-slot pressure; the force-click reads the MAX across
                         # fingers, so a whole-pad press registers once instead of
                         # flickering between each finger's individual pressure
    x = 0
    y = 0
    # decision state, evaluated per SYN frame
    gesture_latched = False  # set after 3 fingers persist past the debounce
    pending_deadline = None  # monotonic time at which a tentative 3-finger buzz fires
    axis = None              # locked to "vol" or "track" on first move past the dead zone
    anchor_x = 0             # finger X/Y the slide is measured from (re-anchored at lock)
    anchor_y = 0
    anchor_vol = 0.0         # system volume (0-1) when a volume slide locks
    last_step = 0
    skip_fired = False       # track-skip is one-shot per swipe
    pp_armed = True
    # five-finger double-tap -> lights
    tap_peak = 0             # most fingers seen during the current contact
    touch_start = 0.0        # when the current contact began (to time the tap)
    last_tap = 0.0           # when the last completed 5-finger tap ended

    def engage_gesture():
        nonlocal gesture_latched, axis, anchor_x, anchor_y, last_step, skip_fired
        gesture_latched, axis, last_step, skip_fired = True, None, 0, False
        anchor_x, anchor_y = x, y
        buzz(READY_BUZZ)
        print("3 fingers -> ready")

    while True:
        # wake on input, or when a pending ready-buzz is due to fire
        timeout = None if pending_deadline is None else max(0.0, pending_deadline - time.monotonic())
        ready = select.select([dev.fd], [], [], timeout)[0]

        # debounce fired with three fingers still down -> engage now
        if pending_deadline is not None and time.monotonic() >= pending_deadline:
            pending_deadline = None
            if len(active) == 3 and not gesture_latched:
                engage_gesture()

        if not ready:
            continue

        for e in dev.read():
            if e.type == ecodes.EV_ABS:
                if e.code == ecodes.ABS_MT_SLOT:
                    cur_slot = e.value
                elif e.code == ecodes.ABS_MT_TRACKING_ID:
                    if e.value == -1:
                        active.discard(cur_slot)
                        slot_pressure.pop(cur_slot, None)   # lifted finger stops counting
                    else:
                        active.add(cur_slot)
                elif e.code == ecodes.ABS_MT_PRESSURE:
                    slot_pressure[cur_slot] = e.value
                elif e.code == ecodes.ABS_X:
                    x = e.value
                elif e.code == ecodes.ABS_Y:
                    y = e.value
                continue

            if not (e.type == ecodes.EV_SYN and e.code == ecodes.SYN_REPORT):
                continue

            # --- one decision per frame ---
            now = time.monotonic()
            fingers = len(active)
            pressure = max(slot_pressure.values(), default=0)   # whole-pad click force

            # track the current contact so we can recognise a five-finger tap
            if fingers > 0 and tap_peak == 0:
                touch_start = now           # first finger of a fresh contact
            tap_peak = max(tap_peak, fingers)

            # full lift resets the latch (so the ready buzz fires once per touch,
            # immune to 3->2->3 finger-count flicker while fingers settle)
            if fingers == 0:
                if gesture_latched:
                    print(f"  done ({axis or 'idle'} {last_step:+d})")
                    gesture_latched = False
                # a brief 5-finger touch is a tap; two within DOUBLE_TAP_GAP -> lights
                if tap_peak == 5 and now - touch_start <= TAP_MAX_DURATION:
                    if last_tap and now - last_tap <= DOUBLE_TAP_GAP:
                        run_bg("curl", "-s", "-m", "5", LIGHTS_URL)
                        buzz(READY_BUZZ)   # same light tap as the 3-finger ready buzz
                        print("5-finger double-tap -> lights")
                        last_tap = 0.0      # consume, so a 3rd tap doesn't re-toggle
                    else:
                        last_tap = now
                tap_peak = 0

            # tentatively start the debounce on 3 fingers; cancel it the instant
            # the count isn't 3 (e.g. a 4th finger lands -> workspace swipe). tap_peak
            # < 4 keeps a 5-finger tap (which passes through 3 on the way up and down)
            # from arming gesture mode mid-tap.
            if fingers == 3 and not gesture_latched and tap_peak < 4:
                if pending_deadline is None:
                    pending_deadline = now + READY_DEBOUNCE
            else:
                pending_deadline = None

            # the first move past the dead zone locks the gesture to one axis;
            # whichever direction dominates wins, so volume and skip never cross-fire
            if gesture_latched and fingers == 3:
                if axis is None:
                    dx, dy = x - anchor_x, anchor_y - y   # dy: up = positive
                    if max(abs(dx), abs(dy)) > VOLUME_DEADZONE:
                        last_step = 0
                        if abs(dy) >= abs(dx):
                            axis, anchor_y, anchor_vol = "vol", y, get_volume()
                            print(f"  volume (from {anchor_vol:.0%})")
                        else:
                            axis, anchor_x = "track", x
                            print("  track-skip")

                if axis == "vol":
                    # absolute slider: target from distance off the (re-)anchor
                    step = (anchor_y - y) // step_units
                    if step != last_step:
                        target = min(1.0, max(0.0, anchor_vol + step * VOL_DELTA_PCT / 100))
                        set_volume_abs(target)
                        buzz(TICK_BUZZ)
                        last_step = step
                elif axis == "track" and not skip_fired:
                    # exactly ONE skip per swipe; symmetric threshold both directions.
                    # int() truncates toward zero (unlike // which floors -0.001 to -1,
                    # which made left-swipes fire instantly and over-skip on jitter).
                    step = int((x - anchor_x) / skip_units)
                    if step != 0:
                        cmd = "next" if step > 0 else "prev"
                        run("mpc", cmd)
                        buzz(SKIP_BUZZ)
                        skip_fired = True
                        print(f"  {cmd}")

            # play/pause: force-click, no movement check. 1 finger -> mpc toggle
            # (MPD); 3 fingers -> playerctl play-pause (whatever MPRIS player is
            # active). One re-arm flag, so a single click fires exactly one action.
            #
            # Like a real trackpad click, a force-click buzzes TWICE: a firm DOWN
            # actuation when the press crosses FORCE_CLICK, then a lighter UP
            # actuation when the finger lifts back below PRESSURE_REARM. The
            # pp_armed False->True edge is the release, so the up-click is tied to
            # your finger lifting (not a fixed delay), which is what makes it feel
            # like one click instead of two buzzes.
            if pressure <= PRESSURE_REARM:
                if not pp_armed:
                    buzz(RELEASE_BUZZ, RELEASE_B6, RELEASE_B11)  # up-click: "letting go" half
                pp_armed = True
            elif pp_armed and pressure >= FORCE_CLICK:
                if fingers == 1 and not gesture_latched:
                    pp_armed = False
                    run("mpc", "toggle")
                    buzz(TAP_BUZZ)
                    print(f"force-click (p={pressure}) -> play/pause")
                elif fingers == 3:
                    pp_armed = False
                    run_as_user("playerctl", "play-pause")
                    buzz(TAP_BUZZ)
                    print(f"3-finger force-click (p={pressure}) -> playerctl play-pause")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nbye")
