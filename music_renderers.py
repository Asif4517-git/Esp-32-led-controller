"""
Music-reactive LED renderers.

Design rules that keep the strip looking good on 8 LEDs:

* Silence means black. A renderer never lights up on room noise.
* Every palette is a real RGB gradient (not a hue formula), so "blue → red"
  actually looks blue to red on the strip.
* A palette can also be two colours picked by the user ("custom").
* Beat effects share a decaying beat envelope, so a kick produces a smooth
  flash instead of a one-frame blink.
"""

import math
import time
from typing import List, Optional, Sequence, Tuple

RGB = Tuple[int, int, int]

# Below this level a band is treated as black. Stops the AGC from lifting
# the audio device's noise floor into visible light.
MIN_BRIGHTNESS_THRESHOLD = 0.06


# --------------------------------------------------------------------- colour

def clamp01(v) -> float:
    return max(0.0, min(1.0, float(v)))


def lerp_rgb(c1: RGB, c2: RGB, t: float) -> RGB:
    t = clamp01(t)
    return (
        int(c1[0] + (c2[0] - c1[0]) * t),
        int(c1[1] + (c2[1] - c1[1]) * t),
        int(c1[2] + (c2[2] - c1[2]) * t),
    )


def gradient(anchors: Sequence[RGB], t: float) -> RGB:
    """Interpolate through a list of colour anchors."""
    t = clamp01(t)
    if not anchors:
        return (0, 0, 0)
    if len(anchors) == 1:
        return anchors[0]
    pos = t * (len(anchors) - 1)
    i = int(pos)
    if i >= len(anchors) - 1:
        return anchors[-1]
    return lerp_rgb(anchors[i], anchors[i + 1], pos - i)


# Built-in palettes. Each entry is a gradient from index 0 to 1.
PALETTES = {
    "default":       [(0, 90, 255), (0, 220, 220), (255, 210, 0), (255, 40, 80)],
    "blue_red":      [(0, 60, 255), (90, 0, 255), (255, 0, 70)],
    "purple_yellow": [(150, 0, 255), (255, 70, 200), (255, 220, 0)],
    "green_red":     [(0, 220, 70), (190, 230, 0), (255, 30, 20)],
    "ocean":         [(0, 30, 160), (0, 150, 255), (0, 255, 205)],
    "fire":          [(170, 0, 0), (255, 80, 0), (255, 220, 0)],
    "mono":          [(255, 255, 255)],
    "custom":        [(0, 120, 255), (255, 40, 80)],
}

PALETTE_LABELS = {
    "default": "Spectrum",
    "blue_red": "Blue → Red",
    "purple_yellow": "Purple → Yellow",
    "green_red": "Green → Red",
    "ocean": "Ocean",
    "fire": "Fire",
    "mono": "Monochrome",
    "custom": "Custom two colours",
}


# ------------------------------------------------------------------ renderers

class MusicRenderer:
    """Base class: palette handling, silence gate, beat envelope, smoothing."""

    # Instantaneous RMS below this counts as silence (about -56 dBFS)
    SILENCE_RMS = 0.0015
    # Consecutive silent frames before blacking out
    SILENCE_HOLD_FRAMES = 3

    def __init__(
        self,
        led_count: int = 8,
        color_palette: str = "default",
        color_from: Sequence[int] = (0, 120, 255),
        color_to: Sequence[int] = (255, 40, 80),
        wled_stops: Optional[Sequence[Sequence[int]]] = None,
        wled_tokens: Optional[Sequence[str]] = None,
    ):
        self.led_count = max(1, int(led_count))
        self.color_palette = color_palette
        self.color_from = tuple(int(c) for c in (color_from or (0, 120, 255)))
        self.color_to = tuple(int(c) for c in (color_to or (255, 40, 80)))
        self.wled_stops = [tuple(int(v) for v in s) for s in (wled_stops or [])]
        self.wled_tokens = list(wled_tokens or [])

        # Stable random colours for "Random Cycle"-style palettes
        self._random_colors = [
            tuple((c * 73 + 41) % 205 + 50 for c in (i * 89, i * 151, i * 211))
            for i in range(8)
        ]

        self.last_colors: List[RGB] = [(0, 0, 0)] * self.led_count
        self.smoothing_factor = 0.35   # 0 = instant, 1 = no movement
        self._silent_frames = 0
        self._beat_env = 0.0
        self._hue = 0.0

    # ------------------------------------------------------------- palette

    def palette_rgb(self, t: float) -> RGB:
        """Colour at position t (0..1) for the active palette."""
        name = self.color_palette or "default"

        # A palette taken from the WLED device's own collection
        if name.startswith("wled:") or self.wled_stops or self.wled_tokens:
            rgb = self._wled_palette_rgb(t)
            if rgb is not None:
                return rgb

        if name == "custom":
            return lerp_rgb(self.color_from, self.color_to, clamp01(t))
        if name.startswith("wled:"):
            # Palette index known but the device data was unavailable
            return gradient([tuple(self.color_from), tuple(self.color_to)], t)
        return gradient(PALETTES.get(name, PALETTES["default"]), t)

    def _wled_palette_rgb(self, t: float) -> Optional[RGB]:
        t = clamp01(t)

        if self.wled_stops:
            stops = self.wled_stops
            pos = t * 255.0
            if pos <= stops[0][0]:
                return tuple(stops[0][1:4])
            if pos >= stops[-1][0]:
                return tuple(stops[-1][1:4])
            for i in range(len(stops) - 1):
                p0, r0, g0, b0 = stops[i]
                p1, r1, g1, b1 = stops[i + 1]
                if p0 <= pos <= p1:
                    k = (pos - p0) / max(1, p1 - p0)
                    return lerp_rgb((r0, g0, b0), (r1, g1, b1), k)
            return tuple(stops[-1][1:4])

        if self.wled_tokens:
            tokens = self.wled_tokens
            idx = int(round(t * (len(tokens) - 1)))
            token = tokens[max(0, min(len(tokens) - 1, idx))]
            if token == "c1":
                return tuple(self.color_from)
            if token == "c2":
                return tuple(self.color_to)
            if token == "c3":
                return tuple((a + b) // 2 for a, b in zip(self.color_from, self.color_to))
            if token == "r":
                n = len(self._random_colors)
                return self._random_colors[int(round(t * (n - 1))) % n]

        return None

    # -------------------------------------------------------------- gating

    def is_silent(self, analysis: dict) -> bool:
        rms = float(analysis.get("rms", 0.0) or 0.0)
        if rms < self.SILENCE_RMS:
            self._silent_frames += 1
        else:
            self._silent_frames = 0
        return self._silent_frames >= self.SILENCE_HOLD_FRAMES

    def beat_envelope(self, analysis: dict, decay: float = 0.86) -> float:
        """1.0 on a detected beat, then a smooth exponential fall."""
        if analysis.get("beat_detected"):
            strength = float(analysis.get("beat_strength", 1.0) or 1.0)
            self._beat_env = max(self._beat_env, 0.55 + 0.45 * clamp01(strength))
        else:
            self._beat_env *= decay
            if self._beat_env < 0.01:
                self._beat_env = 0.0
        return self._beat_env

    def smooth_transition(self, new_colors: List[RGB]) -> List[RGB]:
        smoothed = []
        k = 1.0 - self.smoothing_factor
        for i in range(self.led_count):
            old = self.last_colors[i] if i < len(self.last_colors) else (0, 0, 0)
            new = new_colors[i] if i < len(new_colors) else (0, 0, 0)
            smoothed.append(lerp_rgb(old, new, k))
        self.last_colors = smoothed
        return smoothed

    def fade_to_black(self) -> List[RGB]:
        faded = [lerp_rgb(c, (0, 0, 0), 0.55) for c in self.last_colors[:self.led_count]]
        while len(faded) < self.led_count:
            faded.append((0, 0, 0))
        faded = [(0, 0, 0) if max(c) <= 2 else c for c in faded]
        self.last_colors = faded
        return faded

    # --------------------------------------------------------------- render

    def render(self, analysis: dict) -> List[RGB]:
        if self.is_silent(analysis):
            self._beat_env = 0.0
            return self.fade_to_black()
        colors = self._render_active(analysis)
        return self.smooth_transition(colors)

    def _render_active(self, analysis: dict) -> List[RGB]:
        raise NotImplementedError


class SpectrumRenderer(MusicRenderer):
    """Classic spectrum analyser: low frequencies left, high right."""

    def _render_active(self, analysis: dict) -> List[RGB]:
        bands = analysis.get("bands", [0.0] * self.led_count)
        colors = []
        for i in range(self.led_count):
            level = clamp01(bands[i] if i < len(bands) else 0.0)
            if level < MIN_BRIGHTNESS_THRESHOLD:
                colors.append((0, 0, 0))
                continue
            t = i / max(1, self.led_count - 1)
            base = self.palette_rgb(t)
            colors.append(tuple(int(c * level) for c in base))
        return colors


class MirrorSpectrumRenderer(MusicRenderer):
    """Spectrum mirrored from the middle: bass in the centre, treble at ends."""

    def _render_active(self, analysis: dict) -> List[RGB]:
        bands = analysis.get("bands", [0.0] * self.led_count)
        colors = []
        centre = (self.led_count - 1) / 2.0
        for i in range(self.led_count):
            # distance from the middle, 0 at centre -> 1 at the ends
            d = abs(i - centre) / max(0.5, centre)
            band_idx = int(d * (len(bands) - 1))
            level = clamp01(bands[band_idx])
            if level < MIN_BRIGHTNESS_THRESHOLD:
                colors.append((0, 0, 0))
                continue
            base = self.palette_rgb(d)
            colors.append(tuple(int(c * level) for c in base))
        return colors


class BassPulseRenderer(MusicRenderer):
    """Whole strip breathes with the bass."""

    def _render_active(self, analysis: dict) -> List[RGB]:
        bass = clamp01(analysis.get("bass", 0.0))
        if bass < MIN_BRIGHTNESS_THRESHOLD:
            return [(0, 0, 0)] * self.led_count
        brightness = 0.15 + bass * 0.85
        color = self.palette_rgb(0.25 + bass * 0.5)
        return [tuple(int(c * brightness) for c in color)] * self.led_count


class VUMeterRenderer(MusicRenderer):
    """Volume fills the strip from the left."""

    def _render_active(self, analysis: dict) -> List[RGB]:
        vu = clamp01(analysis.get("vu_level", 0.0))
        if vu < MIN_BRIGHTNESS_THRESHOLD:
            return [(0, 0, 0)] * self.led_count

        lit = int(round(vu * self.led_count))
        colors = []
        for i in range(self.led_count):
            if i < lit:
                t = i / max(1, self.led_count - 1)
                colors.append(self.palette_rgb(t))
            else:
                colors.append((0, 0, 0))
        return colors


class BeatFlashRenderer(MusicRenderer):
    """Dark background; the whole strip flashes on every beat."""

    def _render_active(self, analysis: dict) -> List[RGB]:
        env = self.beat_envelope(analysis, decay=0.80)
        bass = clamp01(analysis.get("bass", 0.0))
        brightness = max(env, bass * 0.18)
        if brightness < MIN_BRIGHTNESS_THRESHOLD:
            return [(0, 0, 0)] * self.led_count
        color = self.palette_rgb(0.85)
        return [tuple(int(c * brightness) for c in color)] * self.led_count


class BeatPulseRenderer(MusicRenderer):
    """
    Beat-first renderer: a strong, smooth pulse on every kick with a soft
    bass glow in between. This is the one that reads best on a short strip.
    """

    def _render_active(self, analysis: dict) -> List[RGB]:
        env = self.beat_envelope(analysis, decay=0.88)
        bass = clamp01(analysis.get("bass", 0.0))
        mid = clamp01(analysis.get("mid", 0.0))

        brightness = 0.06 + 0.94 * max(env, bass * 0.35)
        base = self.palette_rgb(0.15 + env * 0.7)

        colors = []
        centre = (self.led_count - 1) / 2.0
        for i in range(self.led_count):
            # small radial falloff makes the pulse feel centred
            d = abs(i - centre) / max(0.5, centre)
            local = brightness * (1.0 - 0.35 * d) + mid * 0.05
            colors.append(tuple(int(c * clamp01(local)) for c in base))
        return colors


class BeatSpectrumRenderer(MusicRenderer):
    """Spectrum bars that liven up on a beat."""

    def _render_active(self, analysis: dict) -> List[RGB]:
        bands = analysis.get("bands", [0.0] * self.led_count)
        env = self.beat_envelope(analysis, decay=0.82)
        colors = []
        for i in range(self.led_count):
            level = clamp01(bands[i] if i < len(bands) else 0.0)
            level = clamp01(level + env * 0.45)
            if level < MIN_BRIGHTNESS_THRESHOLD:
                colors.append((0, 0, 0))
                continue
            t = i / max(1, self.led_count - 1)
            base = self.palette_rgb(t)
            # blend towards white at the peak of a beat for extra punch
            base = lerp_rgb(base, (255, 255, 255), env * 0.25)
            colors.append(tuple(int(c * level) for c in base))
        return colors


class RainbowAudioRenderer(MusicRenderer):
    """Hue rotation driven by treble; brightness follows bass."""

    def _render_active(self, analysis: dict) -> List[RGB]:
        bass = clamp01(analysis.get("bass", 0.0))
        treble = clamp01(analysis.get("treble", 0.0))
        if bass < MIN_BRIGHTNESS_THRESHOLD and treble < MIN_BRIGHTNESS_THRESHOLD:
            return [(0, 0, 0)] * self.led_count

        self._hue = (self._hue + 0.002 + treble * 0.02) % 1.0
        colors = []
        for i in range(self.led_count):
            t = (self._hue + i / self.led_count) % 1.0
            brightness = 0.2 + bass * 0.8
            c = gradient(PALETTES["default"], t)
            colors.append(tuple(int(v * brightness) for v in c))
        return colors


class MidFreqWaveRenderer(MusicRenderer):
    """A wave that travels outward from the centre on mid frequencies."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._phase = 0.0

    def _render_active(self, analysis: dict) -> List[RGB]:
        mid = clamp01(analysis.get("mid", 0.0))
        bass = clamp01(analysis.get("bass", 0.0))
        if mid < MIN_BRIGHTNESS_THRESHOLD and bass < MIN_BRIGHTNESS_THRESHOLD:
            return [(0, 0, 0)] * self.led_count

        self._phase += 0.05 + mid * 0.25
        colors = []
        centre = self.led_count / 2.0
        for i in range(self.led_count):
            distance = abs(i - centre) / centre
            wave = (math.sin(self._phase - distance * math.pi * 2) + 1.0) / 2.0
            brightness = wave * mid * 0.85 + bass * 0.2
            if brightness < MIN_BRIGHTNESS_THRESHOLD:
                colors.append((0, 0, 0))
            else:
                colors.append(tuple(int(c * clamp01(brightness))
                                    for c in self.palette_rgb(wave)))
        return colors


class EnergyRippleRenderer(MusicRenderer):
    """A ripple that travels along the strip; speed follows volume."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._position = 0.0

    def _render_active(self, analysis: dict) -> List[RGB]:
        vu = clamp01(analysis.get("vu_level", 0.0))
        bass = clamp01(analysis.get("bass", 0.0))
        if vu < MIN_BRIGHTNESS_THRESHOLD:
            return [(0, 0, 0)] * self.led_count

        self._position += 0.08 + vu * 0.5 + bass * 0.3
        colors = []
        for i in range(self.led_count):
            phase = (i / self.led_count) * math.pi * 2 - self._position
            intensity = ((math.sin(phase) + 1.0) / 2.0) ** 2
            brightness = intensity * vu
            if brightness < MIN_BRIGHTNESS_THRESHOLD:
                colors.append((0, 0, 0))
            else:
                colors.append(tuple(int(c * brightness)
                                    for c in self.palette_rgb(intensity)))
        return colors


class CometRenderer(MusicRenderer):
    """A single coloured comet runs along the strip and jumps on each beat."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pos = 0.0
        self._direction = 1.0

    def _render_active(self, analysis: dict) -> List[RGB]:
        vu = clamp01(analysis.get("vu_level", 0.0))
        env = self.beat_envelope(analysis, decay=0.9)

        if analysis.get("beat_detected"):
            self._pos = (self._pos + 2.5) % self.led_count
        else:
            self._pos = (self._pos + self._direction * (0.04 + vu * 0.35)) % self.led_count

        colors = []
        for i in range(self.led_count):
            d = min(abs(i - self._pos), self.led_count - abs(i - self._pos))
            glow = max(0.0, 1.0 - d / 2.2)
            brightness = glow * (0.35 + 0.65 * env) + env * 0.08
            if brightness < MIN_BRIGHTNESS_THRESHOLD:
                colors.append((0, 0, 0))
            else:
                colors.append(tuple(int(c * clamp01(brightness))
                                    for c in self.palette_rgb(glow)))
        return colors


class DuoToneRenderer(MusicRenderer):
    """Bass lights the left half, treble the right half, beats flash both."""

    def _render_active(self, analysis: dict) -> List[RGB]:
        bass = clamp01(analysis.get("bass", 0.0))
        treble = clamp01(analysis.get("treble", 0.0))
        env = self.beat_envelope(analysis, decay=0.85)

        left = self.palette_rgb(0.15)
        right = self.palette_rgb(0.85)
        half = self.led_count // 2

        colors = []
        for i in range(self.led_count):
            stem = bass if i < half else treble
            brightness = clamp01(0.05 + stem * 0.75 + env * 0.4)
            base = left if i < half else right
            colors.append(tuple(int(c * brightness) for c in base))
        return colors


# -------------------------------------------------------------------- registry

RENDERERS = {
    "spectrum": SpectrumRenderer,
    "mirror_spectrum": MirrorSpectrumRenderer,
    "bass_pulse": BassPulseRenderer,
    "vu_meter": VUMeterRenderer,
    "beat_flash": BeatFlashRenderer,
    "beat_pulse": BeatPulseRenderer,
    "beat_spectrum": BeatSpectrumRenderer,
    "rainbow": RainbowAudioRenderer,
    "mid_wave": MidFreqWaveRenderer,
    "energy_ripple": EnergyRippleRenderer,
    "comet": CometRenderer,
    "duo_tone": DuoToneRenderer,
}

RENDERER_LABELS = {
    "spectrum": "Spectrum bars",
    "mirror_spectrum": "Mirror spectrum",
    "bass_pulse": "Bass pulse",
    "vu_meter": "VU meter",
    "beat_flash": "Beat flash",
    "beat_pulse": "Beat pulse",
    "beat_spectrum": "Beat spectrum",
    "rainbow": "Rainbow",
    "mid_wave": "Mid-frequency wave",
    "energy_ripple": "Energy ripple",
    "comet": "Comet",
    "duo_tone": "Duo tone (bass / treble)",
}


def create_renderer(
    name: str,
    led_count: int = 8,
    color_palette: str = "default",
    color_from: Optional[Sequence[int]] = None,
    color_to: Optional[Sequence[int]] = None,
    wled_stops: Optional[Sequence[Sequence[int]]] = None,
    wled_tokens: Optional[Sequence[str]] = None,
) -> MusicRenderer:
    """Factory: build a renderer with the active palette."""
    cls = RENDERERS.get(name, SpectrumRenderer)
    kwargs = {}
    if color_from is not None:
        kwargs["color_from"] = color_from
    if color_to is not None:
        kwargs["color_to"] = color_to
    if wled_stops:
        kwargs["wled_stops"] = wled_stops
    if wled_tokens:
        kwargs["wled_tokens"] = wled_tokens
    return cls(led_count, color_palette, **kwargs)
