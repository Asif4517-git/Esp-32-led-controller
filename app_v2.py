"""
ESP 32 RGB Controller
=====================

A local Flask dashboard that drives a WLED (ESP32) LED strip from what the PC
is doing. Four modes:

    performance  CPU / RAM / GPU rotation, one colour per metric
    music        WASAPI loopback -> FFT -> per-LED spectrum
    screen       screen capture -> dominant / average / bands / edges
    custom       WLED's own effects, colours and palettes over HTTP JSON

Transport
---------
* performance / music / screen are realtime modes. Frames are pushed with DDP
  (UDP 4048), which runs at 30-60 fps and is what WLED expects for live data.
* custom is handed back to WLED's own engine with a single HTTP JSON POST, so
  effects animate on the ESP32 itself, smoothly and with no network chatter.

Everything is guarded: an unreachable device never crashes the app, and a
watchdog re-finds the strip (DHCP changes, power cycles) automatically.
"""

import atexit
import json
import logging
import os
import sys
import threading
import time

import requests
from flask import Flask, jsonify, render_template, request

from audio_analysis import AudioAnalyzer
from audio_capture import AudioCapture
from music_renderers import create_renderer
from screen_capture import ScreenCapture
from system_monitor import get_all_stats
from wled_ddp import DDPController
import wled_palettes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    force=True,
)

# When the desktop shortcut starts us there is no console to read, so keep a
# log file next to the app (the launcher sets LED_LOG).
_log_path = os.environ.get("LED_LOG")
if _log_path:
    try:
        _file_handler = logging.FileHandler(_log_path, encoding="utf-8")
        _file_handler.setFormatter(
            logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        logging.getLogger().addHandler(_file_handler)
    except Exception:
        pass

app = Flask(__name__)

# ============================================================================
# GLOBAL STATE
# ============================================================================

config = {}
ddp: DDPController = None

# Workers (only the ones the current mode needs are kept alive)
audio_capture: AudioCapture = None
audio_analyzer: AudioAnalyzer = None
music_renderer = None
screen_capture: ScreenCapture = None

# Threading
control_thread: threading.Thread = None
stop_event = threading.Event()

# Telemetry shared with the dashboard
stats_cache = {}
last_analysis = {}
last_error = None
frame_stats = {"fps": 0.0, "frames": 0}

device_online = None          # None = not checked yet
device_error = None
device_info = {}

perf_test_state = {"current": "cpu", "last_switch": 0.0, "rgb": None}

# Last colours actually pushed to the strip (dashboard preview)
last_colors = []

# Internals
_last_custom_payload = None   # dedupe key for custom mode
_last_power_sync = None       # True/False = last power state pushed to WLED
_realtime_pause_until = 0.0   # temporarily silence the control loop (tests)
_effects_cache = {"ts": 0.0, "names": [], "defaults": {}}
_palettes_cache = {"ts": 0.0, "names": []}
_custom_transition = 4      # WLED transition (units of 100 ms) for custom mode

# Control-loop heartbeat (surfaced in /api/stats so the UI can prove it is alive)
loop_state = {"mode": None, "ticks": 0, "last_tick": 0.0, "last_exc": None}

# Throttle for checking that WLED still holds the custom state we pushed.
_custom_verify_at = 0.0

# ============================================================================
# SINGLE-INSTANCE GUARD
# ============================================================================
# Windows lets two processes bind the same port, so two copies of this app can
# both drive the strip at once. They then fight: one pushes DDP frames while
# the other posts WLED effects, which looks like flickering and random colours.
# Hold an OS-level lock for the lifetime of the process; the OS drops it if the
# process dies, so a crash never leaves a stale lock behind.

_instance_lock = None

def acquire_single_instance():
    """Return True when this is the only controller process."""
    global _instance_lock
    lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "controller.lock")
    try:
        handle = open(lock_path, "a+")
    except Exception as e:
        logging.warning(f"Could not open instance lock ({e}); continuing")
        return True

    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False

    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    _instance_lock = handle
    return True

def another_controller_answering() -> bool:
    """True if something already serves the dashboard on port 5000."""
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:5000/api/ready",
                                    timeout=1.2) as r:
            json.load(r)
        return True
    except Exception:
        return False


# ============================================================================
# WLED HTTP HELPERS
# ============================================================================

def wled_post(payload: dict, timeout: float = 2.0):
    """POST a JSON command to WLED. Returns (ok, error). Never raises."""
    ip = config.get("esp32_ip")
    if not ip:
        return False, "No device address configured"
    try:
        r = requests.post(f"http://{ip}/json/state", json=payload, timeout=timeout)
        if r.status_code == 200:
            return True, None
        return False, f"HTTP {r.status_code}"
    except Exception as e:
        return False, str(e)


def wled_power(on: bool, transition: int = 0):
    """Turn the strip on or off through WLED itself.

    This matters when leaving a realtime mode: DDP stops and WLED would
    otherwise fall back to its stored effect (the "warm light" surprise).
    Sending on:false leaves the strip genuinely dark.
    """
    return wled_post({"on": bool(on), "transition": int(transition)}, timeout=2.0)

def wled_state_matches(body: dict) -> bool:
    """
    "Is the strip still showing what we set?"

    WLED can be reset by its own web UI, a preset, or another realtime sender,
    which leaves the dashboard claiming one thing while the strip shows
    another. Unknown answers count as a match so a flaky network never causes
    a storm of reposts.
    """
    ip = config.get("esp32_ip")
    if not ip:
        return True
    try:
        r = requests.get(f"http://{ip}/json/state", timeout=1.5)
        if r.status_code != 200:
            return True
        state = r.json()
    except Exception:
        return True

    def as_int(value, fallback=-999):
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    if state.get("live"):
        return False

    target = (body.get("seg") or [{}])[0]
    seg = (state.get("seg") or [{}])[0]

    if as_int(state.get("on", 0)) != as_int(body.get("on", 1)):
        return False
    if as_int(state.get("bri", -1)) != as_int(body.get("bri", -1)):
        return False
    for key in ("fx", "pal", "sx", "ix", "c1", "c2", "c3"):
        if as_int(seg.get(key)) != as_int(target.get(key)):
            return False
    try:
        actual = [as_int(v) for v in (seg.get("col") or [[0, 0, 0]])[0][:3]]
        wanted = [as_int(v) for v in (target.get("col") or [[0, 0, 0]])[0][:3]]
        if actual != wanted:
            return False
    except (TypeError, IndexError):
        pass
    return True


def fetch_device_info(host: str = None, timeout: float = 1.5):
    """Read /json/info. Returns (ok, info_dict, error)."""
    host = host or config.get("esp32_ip")
    if not host:
        return False, None, "No device address configured"
    try:
        r = requests.get(f"http://{host}/json/info", timeout=timeout)
        if r.status_code != 200:
            return False, None, f"HTTP {r.status_code}"
        info = r.json()
        if "ver" not in info:
            return False, None, "Not a WLED device"
        return True, info, None
    except Exception as e:
        return False, None, str(e)


def scan_for_wled(timeout: float = 0.35, max_workers: int = 64):
    """Sweep the local /24 for a device answering like WLED."""
    import concurrent.futures
    import socket

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        return None

    base = ".".join(local_ip.split(".")[:3])
    if base in ("127.0.0", "0.0.0"):
        return None

    def probe(last_octet: int):
        host = f"{base}.{last_octet}"
        try:
            r = requests.get(f"http://{host}/json/info", timeout=timeout)
            if r.status_code != 200:
                return None
            info = r.json()
            if info.get("ver") and "leds" in info:
                return host
        except Exception:
            return None
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        for result in pool.map(probe, range(1, 255)):
            if result:
                return result
    return None


def resolve_wled_host():
    """Configured address first, then mDNS, then a subnet scan."""
    for host in (config.get("esp32_ip"), "wled.local"):
        if not host:
            continue
        ok, info, err = fetch_device_info(host, timeout=1.2)
        if ok:
            return host, None
    found = scan_for_wled()
    if found:
        return found, None
    return None, "No WLED device found on the network"


def parse_effect_defaults(entries):
    """
    Parse /json/fxdata into {effect_id: {pal, sx, ix, c1, c2, c3, ...}}.

    Each entry looks like:
        "!,Duty cycle;!,!;!;01"
        "!,Spawning rate;!,!;!;12;ix=128,pal=0"

    The last ';' field holds WLED's own defaults for that effect. Applying them
    is what makes an effect actually animate: several effects render nothing
    useful unless their custom sliders (c1..c3) have their intended values.
    """
    defaults = {}
    if not isinstance(entries, list):
        return defaults

    for effect_id, entry in enumerate(entries):
        if not isinstance(entry, str) or "=" not in entry:
            continue
        last = entry.split(";")[-1]
        if "=" not in last:
            continue
        opts = {}
        for token in last.split(","):
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            key = key.strip()
            try:
                opts[key] = int(value)
            except (TypeError, ValueError):
                continue
        if opts:
            defaults[effect_id] = opts
    return defaults


def fetch_effect_data(host: str, max_age: float = 120):
    """Return (effect names, per-effect defaults), cached."""
    now = time.time()
    if _effects_cache["names"] and now - _effects_cache["ts"] < max_age:
        return _effects_cache["names"], _effects_cache["defaults"]

    names = []
    defaults = {}
    try:
        r = requests.get(f"http://{host}/json/effects", timeout=3)
        if r.status_code == 200:
            names = r.json()
    except Exception as e:
        logging.warning(f"Could not read WLED effects: {e}")

    try:
        r = requests.get(f"http://{host}/json/fxdata", timeout=4)
        if r.status_code == 200:
            defaults = parse_effect_defaults(r.json())
    except Exception as e:
        logging.warning(f"Could not read WLED effect metadata: {e}")

    if names:
        _effects_cache.update(ts=now, names=names, defaults=defaults)
    return _effects_cache["names"], _effects_cache["defaults"]

# WLED's fxdata leaves speed/intensity out for some effects. These name-based
# fallbacks give every effect a usable starting point, so the Custom page can
# offer a recommended value for each slider instead of a dead control.
_SLOW_EFFECT_HINTS = (
    "fire", "candle", "lava", "plasma", "breathe", "ocean", "sunset",
    "aurora", "sunrise", "smoke", "cloud", "fade", "melt", "glitter",
    "twinkle", "star", "peaceful", "gradient",
)
_FAST_EFFECT_HINTS = (
    "strobe", "chase", "scan", "comet", "theater", "knight", "blink",
    "flash", "beat", "bounce", "jump", "rainbow", "flow", "wave",
    "swirl", "ball", "runner", "sparkle", "party",
)

def _fallback_recommendation(name):
    """(speed, intensity) used only when fxdata has no sx/ix for this effect."""
    low = (name or "").lower()
    if any(word in low for word in _FAST_EFFECT_HINTS):
        return 190, 140
    if any(word in low for word in _SLOW_EFFECT_HINTS):
        return 96, 170
    return 128, 128

def with_recommendations(names, defaults):
    """
    Guarantee sx/ix for every effect.

    WLED's documented values always win; the fallbacks only fill in the blanks
    so both sliders can show a recommended value for every effect, not just
    the ones WLED ships defaults for.
    """
    filled = {k: dict(v) for k, v in defaults.items()}
    for effect_id, name in enumerate(names):
        opts = filled.setdefault(effect_id, {})
        speed, intensity = _fallback_recommendation(name)
        opts.setdefault("sx", speed)
        opts.setdefault("ix", intensity)
    return filled


# ============================================================================
# CONFIG
# ============================================================================

DEFAULT_CONFIG = {
    "esp32_ip": "wled.local",
    "led_count": 8,

    "mode": "off",
    "last_mode": "performance",
    "power_on": True,
    # Light the strip again automatically the next time the app launches
    "resume_on_start": True,

    # Performance rotation
    "perf_test_cycle_time": 5.0,
    "high_usage_threshold": 90,
    "brightness_min": 20,
    "brightness_max": 255,
    # Three-point colour ramps for the performance mode (0 %, 50 %, 100 % load)
    "perf_cpu_colors": [[0, 220, 70], [255, 190, 0], [255, 30, 20]],
    "perf_ram_colors": [[0, 60, 160], [0, 100, 220], [0, 140, 255]],
    "perf_gpu_colors": [[70, 0, 140], [130, 35, 200], [190, 70, 255]],
    "perf_dim_with_load": False,

    # Music
    "music_renderer": "spectrum",
    "music_color_palette": "default",
    "music_color_from": [0, 120, 255],
    "music_color_to": [255, 40, 80],
    "music_source": "system",
    "music_device_id": None,
    "music_fps": 60,
    "music_sensitivity": 1.6,

    # Screen
    "screen_mode": "dominant",
    "screen_monitor": 1,
    "screen_fps": 30,

    # Custom / manual
    "custom_color": [255, 0, 0],
    "custom_brightness": 200,
    "custom_effect": 0,
    "custom_effect_speed": 128,
    "custom_effect_intensity": 128,
    "custom_palette": 0,
    "custom_c1": 128,
    "custom_c2": 128,
    "custom_c3": 16,
}


def load_config():
    global config
    try:
        with open("config.json", "r") as f:
            loaded = json.load(f)
    except Exception:
        loaded = {}

    config = dict(DEFAULT_CONFIG)
    config.update(loaded or {})

    # Older config files may miss newer keys
    for key, value in DEFAULT_CONFIG.items():
        config.setdefault(key, value)

    save_config()


def save_config():
    try:
        with open("config.json", "w") as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        logging.warning(f"Could not write config.json: {e}")


# ============================================================================
# COLOUR / BRIGHTNESS MAPPING
# ============================================================================

def clamp01(v):
    return max(0.0, min(1.0, float(v)))


def lerp(a, b, t):
    return a + (b - a) * t


def lerp_rgb(c1, c2, t):
    t = clamp01(t)
    return (
        int(lerp(c1[0], c2[0], t)),
        int(lerp(c1[1], c2[1], t)),
        int(lerp(c1[2], c2[2], t)),
    )


def _rgb_from_value(raw, default):
    """Coerce a list/tuple or a hex string into an RGB triple."""
    if isinstance(raw, str):
        text = raw.strip().lstrip("#")
        try:
            if len(text) == 6:
                n = int(text, 16)
                return ((n >> 16) & 255, (n >> 8) & 255, n & 255)
        except ValueError:
            pass
        return tuple(default)

    if isinstance(raw, (list, tuple)) and len(raw) >= 3:
        try:
            return tuple(max(0, min(255, int(v))) for v in raw[:3])
        except (TypeError, ValueError):
            return tuple(default)
    return tuple(default)

def configured_colour(key: str, default):
    """Read an RGB triple from config. Lists and hex strings are accepted."""
    return _rgb_from_value(config.get(key, default), default)

# Default three-point ramps: 0 %, 50 %, 100 % load.
DEFAULT_RAMPS = {
    "perf_cpu_colors": [(0, 220, 70), (255, 190, 0), (255, 30, 20)],
    "perf_ram_colors": [(0, 60, 160), (0, 100, 220), (0, 140, 255)],
    "perf_gpu_colors": [(70, 0, 140), (130, 35, 200), (190, 70, 255)],
}

# Load percentage where the first ramp colour starts mixing into the second.
# 0 = the whole 0-50 % span blends; raise it to hold the low colour.
RAMP_LOW_STOP = 0.0

def configured_ramp(key: str):
    """Read a 0/50/100 % ramp from config, falling back per stop."""
    default = DEFAULT_RAMPS[key]
    raw = config.get(key)
    if not isinstance(raw, (list, tuple)) or len(raw) < 3:
        return list(default)
    return [_rgb_from_value(raw[i], default[i]) for i in range(3)]

def _rgb_to_hsv(rgb):
    r, g, b = (c / 255.0 for c in rgb)
    mx, mn = max(r, g, b), min(r, g, b)
    d = mx - mn
    h = 0.0
    if d:
        if mx == r:
            h = 60 * (((g - b) / d) % 6)
        elif mx == g:
            h = 60 * ((b - r) / d + 2)
        else:
            h = 60 * ((r - g) / d + 4)
    s = 0.0 if mx == 0 else d / mx
    return h % 360, s, mx

def _hsv_to_rgb(h, s, v):
    h = h % 360
    c = v * s
    x = c * (1 - abs(((h / 60) % 2) - 1))
    m = v - c
    if h < 60:
        r, g, b = c, x, 0
    elif h < 120:
        r, g, b = x, c, 0
    elif h < 180:
        r, g, b = 0, c, x
    elif h < 240:
        r, g, b = 0, x, c
    elif h < 300:
        r, g, b = x, 0, c
    else:
        r, g, b = c, 0, x
    return (int(round((r + m) * 255)),
            int(round((g + m) * 255)),
            int(round((b + m) * 255)))

def blend_colours(c1, c2, t):
    """
    Blend two colours through HSV rather than RGB.

    Blue -> yellow reaches green halfway, which is what people expect when
    mixing paints. A plain RGB blend would land on grey instead.
    """
    t = clamp01(t)
    if t <= 0:
        return tuple(c1)
    if t >= 1:
        return tuple(c2)

    h1, s1, v1 = _rgb_to_hsv(c1)
    h2, s2, v2 = _rgb_to_hsv(c2)

    # Greys have no hue; borrow the other colour's so the blend stays natural.
    if s1 < 0.01:
        h1 = h2
    if s2 < 0.01:
        h2 = h1

    dh = h2 - h1
    if dh > 180:
        dh -= 360
    elif dh < -180:
        dh += 360

    return _hsv_to_rgb(h1 + dh * t,
                       s1 + (s2 - s1) * t,
                       v1 + (v2 - v1) * t)

def ramp_colour(colours, value):
    """0-50 % blends stop 1 -> stop 2, 50-100 % blends stop 2 -> stop 3."""
    pct = clamp01(value / 100.0) * 100.0
    if pct <= RAMP_LOW_STOP:
        return tuple(colours[0])
    if pct <= 50:
        return blend_colours(colours[0], colours[1],
                             (pct - RAMP_LOW_STOP) / (50.0 - RAMP_LOW_STOP))
    return blend_colours(colours[1], colours[2], (pct - 50.0) / 50.0)

def cpu_gradient(value: float):
    """CPU: user ramp, default green -> yellow -> red."""
    return ramp_colour(configured_ramp("perf_cpu_colors"), value)

def ram_colour(value: float):
    """RAM: user ramp, default dark blue -> blue -> bright blue."""
    return ramp_colour(configured_ramp("perf_ram_colors"), value)

def gpu_colour(value: float):
    """GPU: user ramp, default dark purple -> purple -> light purple."""
    return ramp_colour(configured_ramp("perf_gpu_colors"), value)

def scale_brightness(rgb, level: float):
    """Scale a colour into the configured brightness_min..brightness_max band."""
    lo = float(config.get("brightness_min", 20) or 0)
    hi = float(config.get("brightness_max", 255) or 255)
    hi = max(hi, lo)
    bri = lo + (hi - lo) * clamp01(level)
    factor = bri / 255.0
    return tuple(int(max(0, min(255, c * factor))) for c in rgb)


# ============================================================================
# TRANSITIONS
# ============================================================================
# Every visual change passes through a short crossfade instead of a hard cut.
# That is what stops mode switches and power toggles from looking like blinks.

_fade = {"active": False, "from": [], "start": 0.0, "duration": 0.45}


def lerp_rgb(c1, c2, t):
    t = clamp01(t)
    return (
        int(c1[0] + (c2[0] - c1[0]) * t),
        int(c1[1] + (c2[1] - c1[1]) * t),
        int(c1[2] + (c2[2] - c1[2]) * t),
    )


def smoothstep(t: float) -> float:
    t = clamp01(t)
    return t * t * (3.0 - 2.0 * t)


def begin_fade(duration: float = 0.45):
    """Crossfade from whatever is currently on the strip."""
    _fade["from"] = list(last_colors)
    _fade["start"] = time.time()
    _fade["duration"] = max(0.05, float(duration))
    _fade["active"] = bool(_fade["from"])


def apply_fade(colors, now: float = None):
    """Blend a freshly computed frame out of the pre-switch frame."""
    if not _fade["active"]:
        return colors

    now = now or time.time()
    t = (now - _fade["start"]) / _fade["duration"]
    if t >= 1.0:
        _fade["active"] = False
        return colors

    k = smoothstep(t)
    src = _fade["from"]
    out = []
    for i, c in enumerate(colors):
        f = src[i] if i < len(src) else (0, 0, 0)
        out.append(lerp_rgb(f, c, k))
    return out


def send_fade_to(target, seconds: float = 0.45, led_count: int = 8):
    """
    Drive a timed crossfade straight over DDP.

    Used for the music-mode confirmation and for power off, where the control
    loop is paused and we own the strip for the duration.
    """
    if ddp is None:
        return
    start = list(last_colors[:led_count])
    while len(start) < led_count:
        start.append((0, 0, 0))
    if isinstance(target, tuple):
        target = [target] * led_count
    target = list(target[:led_count])
    while len(target) < led_count:
        target.append((0, 0, 0))

    steps = max(6, int(seconds * 30))
    pause = seconds / steps
    for step in range(1, steps + 1):
        k = smoothstep(step / steps)
        frame = [lerp_rgb(start[i], target[i], k) for i in range(led_count)]
        ddp.send_pixels(frame)
        time.sleep(pause)
    last_colors[:] = target


# ============================================================================
# CONTROL LOOP
# ============================================================================

def send_mode_indicator(mode: str):
    """
    Smooth confirmation when entering a realtime mode.

    Music gets the blue/red signature the user asked for; the other modes
    simply crossfade into their first live frame, so switching never looks
    like a blink.
    """
    if ddp is None:
        return
    try:
        if mode == "music":
            send_fade_to((0, 70, 255), 0.30)
            send_fade_to((255, 25, 25), 0.30)
            send_fade_to((0, 70, 255), 0.30)
            send_fade_to((255, 25, 25), 0.30)
            send_fade_to((0, 0, 0), 0.35)
    except Exception as e:
        logging.warning(f"Mode indicator failed: {e}")


def stop_workers(keep_mode: str = None):
    """Shut down capture workers the incoming mode does not need."""
    global audio_capture, audio_analyzer, screen_capture, music_renderer

    if keep_mode != "music":
        if audio_capture is not None:
            try:
                audio_capture.stop()
            except Exception:
                pass
            audio_capture = None
        audio_analyzer = None
        music_renderer = None

    if keep_mode != "screen":
        if screen_capture is not None:
            try:
                screen_capture.stop()
            except Exception:
                pass
            screen_capture = None


def control_loop():
    """One thread, one active mode, one output path. Never crashes."""
    global last_error, ddp, stats_cache, last_analysis, frame_stats
    global perf_test_state, last_colors, _last_custom_payload, _last_power_sync
    global audio_capture, audio_analyzer, music_renderer, screen_capture
    global loop_state, _custom_transition, _custom_verify_at

    logging.info("Control loop started")
    frame_count = 0
    fps_t0 = time.time()
    last_mode = None

    while not stop_event.is_set():
        try:
            mode = config.get("mode", "off")
            power_on = bool(config.get("power_on", True))
            active = power_on and mode != "off"
            led_count = int(config.get("led_count", 8) or 8)

            loop_state["mode"] = mode
            loop_state["ticks"] = loop_state.get("ticks", 0) + 1
            loop_state["last_tick"] = time.time()

            # ---------------------------------------------------- power off
            if not active:
                if _last_power_sync is not False:
                    if ddp is not None:
                        try:
                            ddp.send_solid(0, 0, 0)
                        except Exception:
                            pass
                    ok, err = wled_power(False, transition=2)
                    _last_power_sync = False
                    if not ok:
                        last_error = err
                    last_colors[:] = [(0, 0, 0)] * led_count
                    logging.info("Output off")
                last_mode = None
                stop_workers()
                time.sleep(0.4)
                continue

            # ----------------------------------------------------- power on
            if _last_power_sync is not True:
                ok, err = wled_power(True)
                _last_power_sync = True
                if not ok:
                    last_error = err
                else:
                    last_error = None

            # A connection test owns the strip for a moment
            if time.time() < _realtime_pause_until:
                time.sleep(0.02)
                continue

            # ------------------------------------------------- ddp controller
            ip = config.get("esp32_ip")
            if (ddp is None
                    or ddp.ip_address != DDPController._clean_ip(ip)
                    or ddp.led_count != led_count):
                if ddp is not None:
                    try:
                        ddp.close()
                    except Exception:
                        pass
                ddp = DDPController(ip, led_count=led_count)

            # --------------------------------------------- mode change
            if mode != last_mode:
                # Tear workers down here, on the loop's own thread. Doing it
                # from a Flask request thread used to race with the renderer
                # and crash a frame with "NoneType has no attribute render".
                stop_workers(keep_mode=mode)

                # Coming out of Custom, WLED is rendering its own effect. Let
                # WLED fade that effect to black first, then take over with a
                # black DDP frame, so there is no hard cut.
                if last_mode == "custom" and mode != "custom":
                    try:
                        wled_post({"bri": 0, "transition": 5})
                        time.sleep(0.55)
                        if ddp is not None:
                            ddp.send_solid(0, 0, 0)
                        wled_post({"bri": 255, "transition": 0})
                        last_colors[:] = [(0, 0, 0)] * led_count
                    except Exception:
                        pass

                if mode == "custom":
                    # Hand the strip back to WLED's own effect engine.
                    # A longer transition lets WLED fade into the new effect.
                    _custom_transition = 12
                    try:
                        ddp.release()
                    except Exception:
                        pass
                    _last_custom_payload = None
                else:
                    send_mode_indicator(mode)
                    begin_fade(1.0)

                if mode == "performance":
                    perf_test_state["current"] = "cpu"
                    perf_test_state["last_switch"] = time.time()
                if mode == "music":
                    music_renderer = None
                last_mode = mode
                logging.info(f"Mode -> {mode}")

            # ============================================ PERFORMANCE
            if mode == "performance":
                stats = stats_cache or get_all_stats()

                now = time.time()
                cycle = float(config.get("perf_test_cycle_time", 5.0) or 5.0)
                if now - perf_test_state["last_switch"] >= cycle:
                    order = ["cpu", "ram"]
                    if stats.get("gpu_available"):
                        order.append("gpu")
                    current = perf_test_state.get("current", "cpu")
                    idx = order.index(current) if current in order else 0
                    perf_test_state["current"] = order[(idx + 1) % len(order)]
                    perf_test_state["last_switch"] = now

                metric = perf_test_state.get("current", "cpu")
                value = float(stats.get(metric, 0) or 0)
                threshold = float(config.get("high_usage_threshold", 90) or 90)

                if value >= threshold:
                    # Solid red warning. Deliberately no strobe: strobing was
                    # what made the strip feel like it was blinking.
                    target = (255, 20, 10)
                elif metric == "cpu":
                    target = cpu_gradient(value)
                elif metric == "ram":
                    target = ram_colour(value)
                else:
                    target = gpu_colour(value)

                if value < threshold and config.get("perf_dim_with_load", False):
                    target = scale_brightness(target, value / 100.0)

                # Glide between the per-metric colours instead of cutting every
                # cycle. Hard switches every couple of seconds read as flicker.
                previous = perf_test_state.get("rgb")
                if previous is None:
                    rgb = target
                else:
                    rgb = tuple(
                        int(round(previous[i] + (target[i] - previous[i]) * 0.18))
                        for i in range(3)
                    )
                perf_test_state["rgb"] = list(rgb)

                colors = apply_fade([rgb] * led_count)
                ddp.send_pixels(colors)
                last_colors[:] = colors
                frame_count += 1
                time.sleep(0.05)

            # ==================================================== MUSIC
            elif mode == "music":
                if audio_capture is None or not audio_capture.is_running:
                    audio_capture = AudioCapture(
                        source=config.get("music_source", "system"),
                        device_id=config.get("music_device_id"),
                    )
                    if not audio_capture.start():
                        last_error = audio_capture.last_error
                        time.sleep(1.0)
                        continue

                if audio_analyzer is None:
                    audio_analyzer = AudioAnalyzer(num_bands=8)
                try:
                    audio_analyzer.beat_threshold = float(
                        config.get("music_sensitivity", 1.6) or 1.6)
                except Exception:
                    pass

                renderer_name = config.get("music_renderer", "spectrum")
                palette = config.get("music_color_palette", "default")
                color_from = tuple(config.get("music_color_from", [0, 120, 255]))
                color_to = tuple(config.get("music_color_to", [255, 40, 80]))

                # A WLED palette (e.g. "wled:12") is resolved to real colour
                # stops pulled from the device itself.
                wled_stops = None
                wled_tokens = None
                if str(palette).startswith("wled:"):
                    entry = wled_palettes.find_palette(config.get("esp32_ip"), palette)
                    if entry:
                        wled_stops = entry.get("stops")
                        wled_tokens = entry.get("tokens")

                palette_key = (palette, bool(wled_stops), bool(wled_tokens))
                renderer = music_renderer
                if (renderer is None
                        or getattr(renderer, "_name", None) != renderer_name
                        or getattr(renderer, "_palette_key", None) != palette_key
                        or getattr(renderer, "color_from", None) != color_from
                        or getattr(renderer, "color_to", None) != color_to
                        or getattr(renderer, "led_count", None) != led_count):
                    renderer = create_renderer(
                        renderer_name, led_count, palette, color_from, color_to,
                        wled_stops=wled_stops, wled_tokens=wled_tokens)
                    renderer._name = renderer_name
                    renderer._palette_key = palette_key
                    music_renderer = renderer

                capture = audio_capture
                samples = capture.read(2048) if capture is not None else None
                if samples is not None:
                    analysis = audio_analyzer.analyze(samples)
                    last_analysis = {
                        "bass": round(float(analysis["bass"]), 3),
                        "mid": round(float(analysis["mid"]), 3),
                        "treble": round(float(analysis["treble"]), 3),
                        "vu_level": round(float(analysis["vu_level"]), 3),
                        "beat": bool(analysis["beat_detected"]),
                        "beat_strength": round(float(analysis.get("beat_strength", 0.0)), 3),
                    }
                    colors = apply_fade(renderer.render(analysis))
                    ddp.send_pixels(colors)
                    last_colors[:] = colors
                    frame_count += 1

                fps = int(config.get("music_fps", 60) or 60)
                time.sleep(1.0 / max(15, min(fps, 60)))

            # ===================================================== SCREEN
            elif mode == "screen":
                if screen_capture is None or not screen_capture.is_running:
                    screen_capture = ScreenCapture(
                        led_count=led_count,
                        mode=config.get("screen_mode", "dominant"),
                        monitor=int(config.get("screen_monitor", 1) or 1),
                        fps=int(config.get("screen_fps", 30) or 30),
                    )
                    if not screen_capture.start():
                        last_error = screen_capture.last_error
                        time.sleep(1.0)
                        continue

                capture = screen_capture
                colors = capture.get_colors() if capture is not None else None
                if colors:
                    colors = apply_fade(colors)
                    ddp.send_pixels(colors)
                    last_colors[:] = colors
                frame_count += 1
                time.sleep(1.0 / max(10, min(int(config.get("screen_fps", 30) or 30), 60)))

            # ===================================================== CUSTOM
            elif mode == "custom":
                color = config.get("custom_color", [255, 0, 0])
                brightness = int(config.get("custom_brightness", 200) or 0)
                effect = int(config.get("custom_effect", 0) or 0)
                speed = int(config.get("custom_effect_speed", 128) or 0)
                intensity = int(config.get("custom_effect_intensity", 128) or 0)
                palette = int(config.get("custom_palette", 0) or 0)
                c1 = int(config.get("custom_c1", 128) or 0)
                c2 = int(config.get("custom_c2", 128) or 0)
                c3 = int(config.get("custom_c3", 16) or 0)

                payload_key = (tuple(color), brightness, effect, speed,
                               intensity, palette, c1, c2, c3)

                body = {
                    "on": True,
                    "bri": brightness,
                    # WLED fades between settings (units of 100 ms).
                    # Entering the mode uses a slower one; knob turns stay
                    # responsive so dragging still feels instant.
                    "transition": _custom_transition,
                    "seg": [{
                        "col": [
                            [int(color[0]), int(color[1]), int(color[2])],
                            [0, 0, 0],
                            [0, 0, 0],
                        ],
                        "fx": effect,
                        "sx": speed,
                        "ix": intensity,
                        "pal": palette,
                        "c1": c1,
                        "c2": c2,
                        "c3": c3,
                    }],
                }

                if payload_key != _last_custom_payload:
                    ok, err = wled_post(body)
                    if ok:
                        _last_custom_payload = payload_key
                        _custom_transition = 4
                        scale = brightness / 255.0

                        # Preview: a palette effect shows the palette gradient,
                        # a solid effect shows the picked colour. This keeps the
                        # dots from always looking like a flat colour.
                        preview = None
                        if effect != 0:
                            entry = wled_palettes.get_palette(
                                config.get("esp32_ip"), palette)
                            if entry and (entry.get("stops") or entry.get("tokens")):
                                preview = []
                                for i in range(led_count):
                                    t = i / max(1, led_count - 1)
                                    rgb = wled_palettes.sample(
                                        entry, t,
                                        color_from=tuple(config.get(
                                            "music_color_from", [0, 120, 255])),
                                        color_to=tuple(config.get(
                                            "music_color_to", [255, 40, 80])),
                                    )
                                    preview.append(rgb or tuple(color))
                        if preview is None:
                            preview = [tuple(color)] * led_count

                        last_colors[:] = [
                            (int(px[0] * scale), int(px[1] * scale),
                             int(px[2] * scale))
                            for px in preview
                        ]
                        last_error = None
                    else:
                        last_error = err
                        time.sleep(0.5)
                else:
                    # Self-heal: WLED can be reset by its own UI, a preset, or
                    # another realtime sender. If the strip no longer matches
                    # the dashboard, push the state again.
                    now_check = time.time()
                    if now_check - _custom_verify_at >= 10.0:
                        _custom_verify_at = now_check
                        if not wled_state_matches(body):
                            logging.info("WLED state drifted; re-applying custom settings")
                            _last_custom_payload = None
                    time.sleep(0.12)

            # --------------------------------------------------------- fps
            now = time.time()
            if now - fps_t0 >= 1.0:
                frame_stats["fps"] = frame_count / (now - fps_t0)
                frame_stats["frames"] = frame_count
                frame_count = 0
                fps_t0 = now

        except Exception as e:
            logging.exception(f"Control loop error: {e}")
            last_error = str(e)
            loop_state["last_exc"] = str(e)
            time.sleep(1.0)


def stats_sampler():
    """Keeps CPU/RAM/GPU fresh for the dashboard and the performance mode."""
    global stats_cache

    # Prime psutil so the first non-blocking reading is meaningful
    try:
        get_all_stats()
    except Exception:
        pass

    while not stop_event.is_set():
        try:
            stats_cache = get_all_stats()
        except Exception as e:
            logging.warning(f"Stats sampling failed: {e}")
        stop_event.wait(1.0)


def device_watchdog():
    """
    Keeps the controller pointed at the right device.

    A power cycle can hand the ESP32 a new DHCP address, so when the configured
    address stops answering we look around the subnet and adopt what we find.
    """
    global device_online, device_error, device_info, ddp
    global _last_power_sync, _last_custom_payload

    last_scan = 0.0

    while not stop_event.is_set():
        try:
            ip = config.get("esp32_ip")
            ok, info, err = fetch_device_info(ip, timeout=1.5)
            was_online = device_online

            if ok:
                device_online = True
                device_error = None
                device_info = {
                    "name": info.get("name"),
                    "version": info.get("ver"),
                    "leds": (info.get("leds") or {}).get("count"),
                    "wifi_signal": (info.get("wifi") or {}).get("signal"),
                    "ip": ip,
                    "mac": info.get("mac"),
                    "uptime": info.get("uptime"),
                }
                if was_online is False:
                    logging.info(f"WLED back online at {ip}")
                    # Re-assert power and effect state after a power cycle
                    _last_power_sync = None
                    _last_custom_payload = None
                    if ddp is not None:
                        ddp.ip_address = DDPController._clean_ip(ip)
            else:
                device_online = False
                device_error = f"Device unreachable at {ip}"
                device_info = {}

                now = time.time()
                if now - last_scan > 20:
                    last_scan = now
                    found = scan_for_wled()
                    if found:
                        logging.info(f"WLED found at {found} (was {ip})")
                        config["esp32_ip"] = found
                        save_config()
                        device_online = True
                        device_error = None
                        _last_power_sync = None
                        _last_custom_payload = None
                        if ddp is not None:
                            ddp.ip_address = DDPController._clean_ip(found)

        except Exception as e:
            logging.warning(f"Watchdog error: {e}")

        stop_event.wait(3.0)


def start_control_thread():
    global control_thread
    if control_thread is None or not control_thread.is_alive():
        stop_event.clear()
        control_thread = threading.Thread(target=control_loop, daemon=True)
        control_thread.start()


# ============================================================================
# FLASK ROUTES
# ============================================================================

@app.route("/")
def index():
    return render_template("home.html", active="home")


@app.route("/custom")
def custom_page():
    return render_template("custom.html", active="custom")


@app.route("/settings")
def settings_page():
    return render_template("settings.html", active="settings")


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    global config, _last_custom_payload, _last_power_sync
    global music_renderer, screen_capture

    if request.method == "POST":
        data = request.json or {}
        old_mode = config.get("mode")
        old_power = config.get("power_on")

        # Drop caches that depend on changed settings
        if any(k in data for k in ("custom_color", "custom_effect", "custom_brightness",
                                   "custom_effect_speed", "custom_effect_intensity",
                                   "custom_palette", "custom_c1", "custom_c2",
                                   "custom_c3")):
            _last_custom_payload = None
        if any(k in data for k in ("music_renderer", "music_color_palette",
                                   "music_color_from", "music_color_to",
                                   "led_count")):
            music_renderer = None
        if any(k in data for k in ("screen_mode", "screen_monitor", "screen_fps", "led_count")):
            if screen_capture is not None:
                try:
                    screen_capture.stop()
                except Exception:
                    pass
                screen_capture = None
        if "esp32_ip" in data and data["esp32_ip"] != config.get("esp32_ip"):
            _last_power_sync = None
            _last_custom_payload = None

        config.update(data)

        if "power_on" in data and data["power_on"] != old_power:
            _last_power_sync = None
        if data.get("mode") and data.get("mode") != old_mode:
            # Worker teardown happens on the control thread itself, which
            # avoids racing a half-rendered frame.
            _last_custom_payload = None

        save_config()
        return jsonify({"success": True, "config": config})

    return jsonify(config)


@app.route("/api/power", methods=["POST"])
def api_power():
    """
    Master power with a real fade.

    Off: crossfade the strip to black over half a second, then tell WLED to
    switch off so it cannot fall back to its own effect. On: pre-arm the
    realtime path with black frames so the stored WLED effect never flashes up.
    """
    global _last_power_sync, _last_custom_payload, last_colors
    global _realtime_pause_until, ddp, _custom_transition

    data = request.json or {}
    on = bool(data.get("power_on", True))
    led_count = int(config.get("led_count", 8) or 8)
    previous_mode = config.get("mode", "off")
    config["power_on"] = on

    if not on:
        config["last_mode"] = previous_mode
        config["mode"] = "off"

        if previous_mode == "custom":
            # WLED's own engine owns the strip: let WLED fade it out itself.
            # transition=15 means 1.5 s, so it visibly dims instead of cutting.
            _realtime_pause_until = time.time() + 1.9
            wled_post({"on": False, "transition": 15})
        else:
            # Realtime modes: paint the fade over DDP, then switch off.
            _realtime_pause_until = time.time() + 2.2
            if _last_power_sync is not False:
                try:
                    if ddp is not None:
                        send_fade_to((0, 0, 0), 1.5, led_count)
                except Exception:
                    pass
            wled_power(False, transition=0)

        _last_power_sync = False
        last_colors[:] = [(0, 0, 0)] * led_count
    else:
        restore = config.get("last_mode") or "performance"
        if restore == "off":
            restore = "performance"
        config["mode"] = restore

        _realtime_pause_until = time.time() + 0.4
        try:
            if ddp is None:
                ddp = DDPController(config.get("esp32_ip"), led_count=led_count)
            # Put WLED into realtime mode *before* switching it on, so the
            # stored effect is never visible for a frame.
            for _ in range(3):
                ddp.send_solid(0, 0, 0)
                time.sleep(0.02)
        except Exception:
            pass
        wled_power(True, transition=0)
        _last_power_sync = True
        _last_custom_payload = None
        _custom_transition = 12
        last_colors[:] = [(0, 0, 0)] * led_count

    save_config()
    return jsonify({
        "success": True,
        "power_on": config["power_on"],
        "mode": config["mode"],
    })


@app.route("/api/stats")
def api_stats():
    """Everything the dashboard needs, in one cheap call."""
    return jsonify({
        "config": config,
        "stats": stats_cache,
        "frame_stats": frame_stats,
        "perf_test_state": perf_test_state,
        "device_online": device_online,
        "device_error": device_error,
        "device_info": device_info,
        "last_error": last_error,
        "preview_colors": last_colors,
        "audio_analysis": last_analysis,
        "loop": loop_state,
        "audio": audio_capture.status() if audio_capture is not None else None,
        "screen": screen_capture.status() if screen_capture is not None else None,
        "ddp": ddp.stats() if ddp is not None else None,
    })


@app.route("/api/device")
def api_device():
    """Device details for the settings page (browser CORS would block this)."""
    return jsonify({
        "online": device_online,
        "error": device_error,
        "info": device_info,
    })


@app.route("/api/effects")
def api_effects():
    """
    WLED's effect list plus each effect's own default settings.

    The defaults matter: many WLED effects look broken (a single dot or a flat
    colour) unless their palette and custom sliders are set. The dashboard
    applies them when you pick an effect, then lets you adjust them.
    """
    ip = config.get("esp32_ip")
    names, defaults = fetch_effect_data(ip)

    if not names:
        names = ["Solid", "Blink", "Breathe", "Wipe", "Colorloop", "Rainbow",
                 "Scan", "Chase", "Fade", "Sparkle"]

    defaults = with_recommendations(names, defaults)

    return jsonify({
        "effects": names,
        "defaults": {str(k): v for k, v in defaults.items()},
        "current_effect": config.get("custom_effect", 0),
        "palette": config.get("custom_palette", 0),
        "speed": config.get("custom_effect_speed", 128),
        "intensity": config.get("custom_effect_intensity", 128),
        "c1": config.get("custom_c1", 128),
        "c2": config.get("custom_c2", 128),
        "c3": config.get("custom_c3", 16),
    })


@app.route("/api/palettes")
def api_palettes():
    """
    Every WLED palette the device knows, with a ready-to-use CSS gradient so
    the dashboard can show the same colour bars the WLED UI does.
    """
    host = config.get("esp32_ip")
    color_from = config.get("music_color_from", [0, 120, 255])
    color_to = config.get("music_color_to", [255, 40, 80])

    entries = wled_palettes.fetch_palettes(host)
    items = []
    for entry in entries:
        items.append({
            "index": entry["index"],
            "name": entry["name"],
            "gradient": wled_palettes.css_gradient(entry, color_from, color_to),
            "dynamic": not bool(entry.get("stops")),
            "value": f"wled:{entry['index']}",
        })

    names = [entry["name"] for entry in entries]
    return jsonify({
        "palettes": names or ["Default"],
        "items": items,
    })


@app.route("/api/autodetect")
def api_autodetect():
    """Find the device on the network and store its address."""
    host, error = resolve_wled_host()
    if host:
        config["esp32_ip"] = host
        save_config()
        return jsonify({"success": True, "ip": host})
    return jsonify({"success": False, "message": error})


@app.route("/api/preview")
def api_preview():
    return jsonify({"colors": last_colors})


@app.route("/api/ready")
def api_ready():
    """
    Used by the desktop launcher: has the control loop pushed its first frame?
    `output_on` means the strip is actually being driven right now.
    """
    return jsonify({
        "ready": bool(loop_state.get("ticks")),
        "mode": loop_state.get("mode"),
        "output_on": (config.get("power_on", True)
                      and config.get("mode", "off") != "off"),
        "device_online": device_online,
    })


@app.route("/api/last-page", methods=["GET", "POST"])
def api_last_page():
    """Remember which page the dashboard was on, so the URL can be restored."""
    if request.method == "POST":
        page = (request.json or {}).get("page")
        if page in ("home", "custom", "settings"):
            config["last_page"] = page
            save_config()
    return jsonify({"page": config.get("last_page", "home")})


@app.route("/api/test")
def api_test():
    """
    Connection test: flashes the strip red three times and reports the firmware.

    The control loop is paused while this runs, otherwise its next frame would
    overwrite the flash within milliseconds (this is why the old test button
    looked broken in realtime modes).
    """
    global _realtime_pause_until, _last_power_sync, _last_custom_payload, last_error

    ip = config.get("esp32_ip")
    ok, info, err = fetch_device_info(ip, timeout=2.0)
    if not ok:
        return jsonify({"success": False, "message": f"Device unreachable: {err}"})

    was_on = bool(config.get("power_on", True)) and config.get("mode", "off") != "off"
    _realtime_pause_until = time.time() + 5.0

    try:
        for _ in range(3):
            wled_post({"on": True, "bri": 255, "transition": 0,
                       "seg": [{"col": [[255, 0, 0], [0, 0, 0], [0, 0, 0]],
                                "fx": 0, "sx": 128}]})
            time.sleep(0.30)
            # Black, but keep bri high: DDP frames ignore colour but not brightness
            wled_post({"on": True, "bri": 255, "transition": 0,
                       "seg": [{"col": [[0, 0, 0], [0, 0, 0], [0, 0, 0]],
                                "fx": 0, "sx": 128}]})
            time.sleep(0.22)
    except Exception as e:
        last_error = str(e)
    finally:
        _realtime_pause_until = 0.0
        _last_power_sync = None
        _last_custom_payload = None
        if was_on:
            wled_post({"on": True, "bri": 255, "transition": 0})
        else:
            wled_power(False)

    name = info.get("name") or "WLED"
    ver = info.get("ver") or "?"
    leds = (info.get("leds") or {}).get("count")
    return jsonify({
        "success": True,
        "message": f"Connected to {name} · firmware {ver} · {leds} LEDs",
        "info": {"name": name, "version": ver, "leds": leds},
    })


# ============================================================================
# STARTUP
# ============================================================================

def apply_launch_options():
    """
    The desktop shortcut sets LED_AUTOSTART=1, which means "I clicked the icon
    and I want light now": start in the last used mode instead of waiting for
    a button press. Starting the app by hand leaves the previous state alone.
    """
    if os.environ.get("LED_AUTOSTART") != "1":
        return

    if config.get("power_on") and config.get("mode") not in (None, "", "off"):
        return  # already running a mode

    mode = config.get("last_mode") or "performance"
    if mode == "off":
        mode = "performance"
    config["mode"] = mode
    config["power_on"] = True
    save_config()
    logging.info(f"Autostart: lighting the strip in '{mode}' mode")


def _shutdown():
    """Hand the strip back to WLED when the controller exits."""
    try:
        stop_event.set()
        if ddp is not None:
            ddp.release()
    except Exception:
        pass


atexit.register(_shutdown)


if __name__ == "__main__":
    if not acquire_single_instance():
        logging.error("Another controller is already running — exiting. "
                      "Open http://localhost:5000")
        print("\nESP 32 RGB Controller is already running.")
        print(f"Dashboard: http://localhost:5000\n")
        sys.exit(0)

    if another_controller_answering():
        logging.error("Port 5000 is already served by another controller — exiting.")
        print("\nAnother controller is already serving port 5000.")
        print(f"Dashboard: http://localhost:5000\n")
        sys.exit(0)

    load_config()
    apply_launch_options()
    start_control_thread()
    threading.Thread(target=stats_sampler, daemon=True).start()
    threading.Thread(target=device_watchdog, daemon=True).start()

    print("\n" + "=" * 70)
    print("  ESP 32 RGB Controller")
    print("=" * 70)
    print(f"\n  Dashboard: http://localhost:5000")
    print(f"  Device:    {config.get('esp32_ip')}")
    print(f"  LEDs:      {config.get('led_count')}")
    print("=" * 70 + "\n")

    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
