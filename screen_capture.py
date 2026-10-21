"""
Screen capture and analysis for screen-reactive mode.

Grabs the monitor image and converts it to LED colors using one of several
mapping strategies:
- Dominant: the most vivid colour, ignoring white/grey/black pixels
- Average: one ambient colour for the whole screen, biased to its accent colour
- Bands: screen split into N vertical slices, one per LED
- Edges: screen split into edge zones (top, bottom, left, right)

Every strategy discounts near-grey pixels when choosing a hue, so a white page
with green text reads green rather than pale grey. Results are eased towards
over roughly a tenth of a second so scene changes fade instead of snapping.

Uses `mss` for fast DirectX screen capture on Windows.
"""

import logging
import math
import threading
import time
from typing import List, Optional, Tuple

import numpy as np

try:
    from mss import mss as MSS
    MSS_AVAILABLE = True
    MSS_IMPORT_ERROR = None
except Exception as e:
    MSS = None
    MSS_AVAILABLE = False
    MSS_IMPORT_ERROR = str(e)


RGB = Tuple[int, int, int]


# --------------------------------------------------------------- colour maths
# Small scalar helpers. The per-pixel work uses NumPy masks instead, so these
# are only called once per LED per frame.

def _rgb_to_hsv(rgb) -> Tuple[float, float, float]:
    r, g, b = (float(c) / 255.0 for c in rgb)
    mx, mn = max(r, g, b), min(r, g, b)
    d = mx - mn
    h = 0.0
    if d > 1e-6:
        if mx == r:
            h = (60.0 * ((g - b) / d)) % 360.0
        elif mx == g:
            h = 60.0 * ((b - r) / d + 2.0)
        else:
            h = 60.0 * ((r - g) / d + 4.0)
    s = 0.0 if mx <= 0 else d / mx
    return h, s, mx


def _hsv_to_rgb(h: float, s: float, v: float) -> RGB:
    h = h % 360.0
    c = v * s
    x = c * (1.0 - abs((h / 60.0) % 2.0 - 1.0))
    m = v - c
    if h < 60:
        r, g, b = c, x, 0.0
    elif h < 120:
        r, g, b = x, c, 0.0
    elif h < 180:
        r, g, b = 0.0, c, x
    elif h < 240:
        r, g, b = 0.0, x, c
    elif h < 300:
        r, g, b = x, 0.0, c
    else:
        r, g, b = c, 0.0, x
    return (max(0, min(255, int(round((r + m) * 255.0)))),
            max(0, min(255, int(round((g + m) * 255.0)))),
            max(0, min(255, int(round((b + m) * 255.0)))))


def _mix_hsv(c1, c2, t: float) -> RGB:
    """Blend two colours through hue so they never pass through grey."""
    t = max(0.0, min(1.0, t))
    if t <= 0.0:
        return tuple(int(c) for c in c1)
    if t >= 1.0:
        return tuple(int(c) for c in c2)

    h1, s1, v1 = _rgb_to_hsv(c1)
    h2, s2, v2 = _rgb_to_hsv(c2)
    # Greys carry no hue: borrow the other colour's so the blend stays clean.
    if s1 < 0.01:
        h1 = h2
    if s2 < 0.01:
        h2 = h1

    dh = h2 - h1
    if dh > 180.0:
        dh -= 360.0
    elif dh < -180.0:
        dh += 360.0

    return _hsv_to_rgb(h1 + dh * t,
                       s1 + (s2 - s1) * t,
                       v1 + (v2 - v1) * t)


class ScreenCapture:
    """
    Continuously captures the screen and produces LED colors.

    Usage:
        cap = ScreenCapture(led_count=8, mode="bands", monitor=1, fps=30)
        cap.start()
        colors = cap.get_colors()  # [(r,g,b), (r,g,b), ...]
        cap.stop()
    """

    def __init__(
        self,
        led_count: int = 8,
        mode: str = "bands",
        monitor: int = 1,
        fps: int = 30,
        downsample: int = 4,
        smooth_seconds: float = 0.12,
    ):
        self.led_count = led_count
        self.mode = mode if mode in ("average", "bands", "edges", "dominant") else "bands"
        self.monitor_index = monitor
        self.target_fps = fps
        self.downsample = max(1, int(downsample))

        # Pixels below these levels are treated as greys when deciding the
        # colour: they count for brightness, never for hue.
        self.chroma_min_s = 0.25
        self.chroma_min_v = 22.0
        # Fraction of colourful pixels a region needs before its accent colour
        # is used instead of the plain average.
        self.chroma_fraction = 0.005

        # Time constant for easing the strip towards each new screen colour.
        self.smooth_seconds = max(0.02, float(smooth_seconds))
        self._smoothed: Optional[List[RGB]] = None
        self._smooth_t = 0.0

        self._lock = threading.Lock()
        self._colors: List[RGB] = [(0, 0, 0)] * led_count
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._running = False

        self.frames_captured = 0
        self.last_frame_time = 0.0
        self.last_error: Optional[str] = None
        self.actual_fps = 0.0

    # ----------------------------------------------------------------- control

    def start(self) -> bool:
        if not MSS_AVAILABLE:
            self.last_error = f"mss unavailable: {MSS_IMPORT_ERROR}"
            logging.error(self.last_error)
            return False

        if self._running:
            return True

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()
        self._running = True
        logging.info(
            f"Screen capture started: mode={self.mode} monitor={self.monitor_index} "
            f"target_fps={self.target_fps}"
        )
        return True

    def stop(self):
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
        self._running = False
        logging.info("Screen capture stopped")

    @property
    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    def get_colors(self) -> List[RGB]:
        with self._lock:
            return self._colors.copy()

    # -------------------------------------------------------------- capture loop

    def _capture_loop(self):
        interval = 1.0 / self.target_fps
        last_t = time.time()
        fps_frames = 0
        fps_t0 = last_t

        try:
            with MSS() as sct:
                monitors = sct.monitors
                if self.monitor_index >= len(monitors):
                    self.last_error = f"Monitor {self.monitor_index} not found"
                    logging.error(self.last_error)
                    return

                mon = monitors[self.monitor_index]

                while not self._stop_event.is_set():
                    try:
                        now = time.time()
                        elapsed = now - last_t
                        if elapsed < interval:
                            time.sleep(interval - elapsed)
                            now = time.time()

                        # Capture screen
                        shot = sct.grab(mon)
                        img = np.asarray(shot)[:, :, :3]  # BGRA -> BGR, drop alpha

                        # Sample every Nth pixel. Block averaging would be
                        # smoother but costs ~50 ms a frame on a 1600x900
                        # screen; striding leaves the frame budget to capture.
                        if self.downsample > 1:
                            img = img[::self.downsample, ::self.downsample, :]

                        # Convert to RGB (from BGR)
                        img = img[:, :, ::-1]

                        # Compute LED colors, then ease towards them so a scene
                        # change fades instead of snapping.
                        colors = self._analyze(img)
                        colors = self._smooth(colors, now)

                        with self._lock:
                            self._colors = colors

                        self.frames_captured += 1
                        self.last_frame_time = now
                        last_t = now

                        fps_frames += 1
                        if now - fps_t0 >= 1.0:
                            self.actual_fps = fps_frames / (now - fps_t0)
                            fps_frames = 0
                            fps_t0 = now

                    except Exception as e:
                        self.last_error = str(e)
                        logging.warning(f"Screen capture frame error: {e}")
                        time.sleep(0.1)

        except Exception as e:
            self.last_error = f"Capture loop error: {e}"
            logging.error(self.last_error)
        finally:
            self._running = False

    # ----------------------------------------------------------- color analysis

    def _analyze(self, img: np.ndarray) -> List[RGB]:
        """
        Convert screen pixels to LED colors according to the current mode.
        img is RGB uint8 [H, W, 3]
        """
        h, w, _ = img.shape

        if self.mode == "average":
            return self._average(img)
        elif self.mode == "bands":
            return self._bands(img, w)
        elif self.mode == "edges":
            return self._edges(img, h, w)
        elif self.mode == "dominant":
            return self._dominant(img)
        else:
            return [(0, 0, 0)] * self.led_count

    def _average(self, img: np.ndarray) -> List[RGB]:
        """Average the entire screen to one colour, broadcast to all LEDs."""
        return [self._region_colour(img)] * self.led_count

    def _bands(self, img: np.ndarray, width: int) -> List[RGB]:
        """
        Split screen into vertical slices, one per LED.
        LED 0 = leftmost, LED N-1 = rightmost.
        """
        colors = []
        for i in range(self.led_count):
            x_start = int((i * width) / self.led_count)
            x_end = int(((i + 1) * width) / self.led_count)
            colors.append(self._region_colour(img[:, x_start:x_end, :]))
        return colors

    def _edges(self, img: np.ndarray, height: int, width: int) -> List[RGB]:
        """
        Sample edge zones: top, right, bottom, left.
        Map to LEDs in a clockwise pattern. For 8 LEDs:
          [0,1] = top
          [2,3] = right
          [4,5] = bottom
          [6,7] = left
        """
        edge_depth_h = max(1, height // 8)
        edge_depth_w = max(1, width // 8)

        zones = [
            img[:edge_depth_h, :, :],
            img[:, -edge_depth_w:, :],
            img[-edge_depth_h:, :, :],
            img[:, :edge_depth_w, :],
        ]
        zone_colors = [self._region_colour(zone) for zone in zones]

        # Distribute zone colours across LEDs
        colors = []
        leds_per_zone = self.led_count // 4
        remainder = self.led_count % 4
        for i, c in enumerate(zone_colors):
            count = leds_per_zone + (1 if i < remainder else 0)
            colors.extend([c] * count)
        return colors[:self.led_count]

    # ------------------------------------------------------- colour per region

    @staticmethod
    def _split_channels(pixels: np.ndarray):
        """Return (max, saturation, normalised value) arrays for Nx3 pixels."""
        mx = pixels.max(axis=1)
        mn = pixels.min(axis=1)
        sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1.0), 0.0)
        return mx, sat, mx / 255.0

    def _vivid(self, rgb, overall_v: float) -> RGB:
        """
        Make a representative colour read clearly on the strip.

        Saturation is lifted a little; brightness gets a floor that follows the
        region's overall brightness, so a dark scene stays dim while an accent
        colour on a bright page still looks like that colour rather than pastel.
        """
        h, s, v = _rgb_to_hsv(rgb)
        if s >= 0.15:
            s = min(1.0, max(s * 1.25, 0.40))
            floor = 0.45 + 0.50 * max(0.0, min(1.0, overall_v))
            v = max(v, floor)
        return _hsv_to_rgb(h, s, v)

    def _region_colour(self, region: np.ndarray) -> RGB:
        """
        One colour for a screen region.

        A plain RGB mean turns a white page with green text into near-white.
        Instead, colourful pixels are averaged on their own when there are
        enough of them, so the accent colour survives a large grey or white
        background. Regions with no real colour fall back to the plain mean.
        """
        pixels = region.reshape(-1, 3).astype(np.float32)
        if pixels.size == 0:
            return (0, 0, 0)

        mx, sat, val = self._split_channels(pixels)
        overall_v = float(val.mean(dtype=np.float64))
        colourful = (sat >= self.chroma_min_s) & (mx >= self.chroma_min_v)

        if float(colourful.mean()) >= self.chroma_fraction:
            # float64 accumulation: summing hundreds of thousands of 255s in
            # float32 drifts above 255.
            base = pixels[colourful].mean(axis=0, dtype=np.float64)
        else:
            base = pixels.mean(axis=0, dtype=np.float64)

        return self._vivid(base, overall_v)

    def _dominant(self, img: np.ndarray) -> List[RGB]:
        """
        Find the most visually prominent *colour*, not the largest area.

        Greys, whites and blacks are excluded from the vote entirely. The
        remaining pixels are grouped into 10-degree hue bins and weighted by
        saturation squared times brightness, so a vivid accent beats a larger
        muted area. The winning bin (plus its two neighbours) is averaged and
        made vivid. A screen with no colour at all falls back to its average.
        """
        pixels = img.reshape(-1, 3).astype(np.float32)
        mx, sat, val = self._split_channels(pixels)
        overall_v = float(val.mean(dtype=np.float64))

        colourful = (sat >= self.chroma_min_s) & (mx >= self.chroma_min_v)
        if float(colourful.mean()) < self.chroma_fraction:
            return [self._vivid(pixels.mean(axis=0, dtype=np.float64),
                                overall_v)] * self.led_count

        pp = pixels[colourful]
        ss = sat[colourful]
        vv = val[colourful]

        # Hue in degrees, vectorised.
        r, g, b = pp[:, 0], pp[:, 1], pp[:, 2]
        mxp = pp.max(axis=1)
        mnp = pp.min(axis=1)
        d = np.maximum(mxp - mnp, 1e-6)
        hue = np.where(
            mxp == r, (60.0 * ((g - b) / d)) % 360.0,
            np.where(mxp == g,
                     60.0 * ((b - r) / d + 2.0),
                     60.0 * ((r - g) / d + 4.0)))

        bins = 36
        idx = np.clip((hue / (360.0 / bins)).astype(np.int32), 0, bins - 1)
        # Saturation squared: pastel greys should not outvote a real accent.
        weights = ss * ss * vv
        histogram = np.bincount(idx, weights=weights, minlength=bins)
        best = int(histogram.argmax())

        near = (idx == best) | (idx == (best - 1) % bins) | (idx == (best + 1) % bins)
        base = pp[near].mean(axis=0, dtype=np.float64)

        return [self._vivid(base, overall_v)] * self.led_count

    # ---------------------------------------------------------------- smoothing

    def _smooth(self, target: List[RGB], now: float) -> List[RGB]:
        """
        Ease the strip towards the new colours instead of switching instantly.

        Blending happens in HSV with the shortest hue path, so green -> blue
        passes through cyan rather than grey. The easing is time based, which
        keeps the feel the same at 30 and 60 fps.
        """
        target = [tuple(int(c) for c in col) for col in target]

        if self._smoothed is None or len(self._smoothed) != len(target):
            self._smoothed = target
            self._smooth_t = now
            return list(target)

        dt = min(0.5, max(0.0, now - self._smooth_t))
        self._smooth_t = now
        k = 1.0 - math.exp(-dt / self.smooth_seconds)

        self._smoothed = [_mix_hsv(cur, tgt, k)
                          for cur, tgt in zip(self._smoothed, target)]
        return list(self._smoothed)

    # ---------------------------------------------------------------- diagnostics

    def status(self) -> dict:
        return {
            "available": MSS_AVAILABLE,
            "running": self.is_running,
            "mode": self.mode,
            "monitor": self.monitor_index,
            "target_fps": self.target_fps,
            "actual_fps": round(self.actual_fps, 1),
            "frames_captured": self.frames_captured,
            "last_error": self.last_error,
        }
