"""
Audio analysis for music-reactive mode.

Takes raw audio samples and produces:
- 8 frequency bands (bass to treble)
- Bass/mid/treble energy levels
- Beat detection (kick drum detection)
- VU level (overall loudness)
- Adaptive per-band gain so quiet and loud tracks both look good

The FFT is computed over a sliding window with Hanning taper to reduce
spectral leakage. Bands are logarithmically spaced to match human hearing
(more resolution in bass, less in treble).

AGC (automatic gain control) gradually adjusts gain so the LEDs use their
full brightness range regardless of input level.
"""

import time
from collections import deque

import numpy as np
from typing import Optional


# Logarithmic frequency band edges for 8 bands, chosen to align roughly with:
# [0] sub-bass, [1] bass, [2] low-mid, [3] mid, [4] upper-mid,
# [5] presence, [6] brilliance, [7] air
# Hz boundaries chosen for musical relevance at 44.1 kHz sample rate
BAND_EDGES_HZ = [20, 60, 150, 400, 1000, 2500, 6000, 12000, 20000]


def _hz_to_bin(hz: float, sample_rate: int, fft_size: int) -> int:
    """Convert frequency in Hz to the nearest FFT bin index."""
    return int(round(hz * fft_size / sample_rate))


class AudioAnalyzer:
    """
    Real-time FFT analysis with AGC and beat detection.

    Usage:
        analyzer = AudioAnalyzer(sample_rate=44100, fft_size=2048, num_bands=8)
        result = analyzer.analyze(samples)  # numpy array of audio
        # result["bands"] is 8 floats [0..1]
        # result["bass"], result["mid"], result["treble"]
        # result["beat_detected"] is True when a kick hits
        # result["vu_level"] is overall loudness
    """

    def __init__(
        self,
        sample_rate: int = 44100,
        fft_size: int = 2048,
        num_bands: int = 8,
        agc_speed: float = 0.05,
        beat_threshold: float = 1.5,
    ):
        self.sample_rate = sample_rate
        self.fft_size = fft_size
        self.num_bands = num_bands
        self.agc_speed = agc_speed
        self.beat_threshold = beat_threshold

        # Pre-compute band bins
        self._band_bins = []
        for i in range(num_bands):
            lo = _hz_to_bin(BAND_EDGES_HZ[i], sample_rate, fft_size)
            hi = _hz_to_bin(BAND_EDGES_HZ[i + 1], sample_rate, fft_size)
            self._band_bins.append((lo, hi))

        # Hanning window for FFT
        self._window = np.hanning(fft_size).astype(np.float32)

        # AGC state: per-band peak trackers
        self._band_peaks = np.ones(num_bands, dtype=np.float32) * 0.01
        self._band_gains = np.ones(num_bands, dtype=np.float32)

        # Beat detection state
        self._bass_history = np.zeros(8, dtype=np.float32)
        self._bass_avg = 0.1
        self._beat_cooldown = 0

        # Onset detector: raw (pre-smoothing) bass energy compared against a
        # rolling window. This is what makes beats land on the kick instead of
        # trailing behind the smoothed level.
        self._bass_energy_hist = deque(maxlen=43)  # ~0.7 s at 60 fps
        self._last_beat_time = 0.0

        # VU meter state
        self._vu_smoothed = 0.0

        # Temporal smoothing (attack/release) — this is what stops the LEDs
        # from jittering frame to frame on noisy audio.
        self._smoothed_bands = np.zeros(num_bands, dtype=np.float32)
        self._smoothed_bass = 0.0
        self._smoothed_mid = 0.0
        self._smoothed_treble = 0.0
        self.attack = 0.55   # how fast a band may rise
        self.release = 0.28  # how fast a band may fall

    def analyze(self, samples: np.ndarray) -> dict:
        """
        Analyze a block of audio and return band levels, bass/mid/treble,
        beat detection, and VU level.

        samples: 1D float32 array, ideally length >= fft_size
        Returns: dict with keys "bands", "bass", "mid", "treble", "beat_detected", "vu_level"
        """
        if samples is None or len(samples) < self.fft_size:
            return self._empty_result()

        # Take the most recent fft_size samples
        block = samples[-self.fft_size:].astype(np.float32)

        # Apply window and compute magnitude spectrum
        windowed = block * self._window
        spectrum = np.abs(np.fft.rfft(windowed))

        # Sum bins into logarithmic bands
        raw_bands = np.zeros(self.num_bands, dtype=np.float32)
        for i, (lo, hi) in enumerate(self._band_bins):
            if hi > lo:
                raw_bands[i] = float(np.mean(spectrum[lo:hi]))

        # Noise gate: anything quieter than this in a band is treated as zero.
        # Without it the AGC multiplies the audio device's own noise floor.
        raw_bands[raw_bands < 1e-4] = 0.0

        # AGC: track peak in each band and adapt gain
        for i in range(self.num_bands):
            if raw_bands[i] > self._band_peaks[i]:
                self._band_peaks[i] = raw_bands[i]
            else:
                # Decay peak slowly
                self._band_peaks[i] *= (1.0 - self.agc_speed * 0.1)
                self._band_peaks[i] = max(self._band_peaks[i], 0.01)

            # Gain brings peak to 1.0
            target_gain = 1.0 / max(self._band_peaks[i], 0.01)
            self._band_gains[i] += (target_gain - self._band_gains[i]) * self.agc_speed

        # Apply gain and clip to [0, 1]
        bands = np.clip(raw_bands * self._band_gains, 0.0, 1.0)

        # Temporal smoothing: fast attack, slower release. Without this the
        # strip flickers because consecutive FFT frames differ sharply.
        for i in range(self.num_bands):
            target = float(bands[i])
            current = float(self._smoothed_bands[i])
            rate = self.attack if target > current else self.release
            self._smoothed_bands[i] = current + (target - current) * rate
        bands = self._smoothed_bands.copy()

        # Derived levels
        raw_bass = float(np.mean(bands[:2])) if self.num_bands >= 2 else float(bands[0])
        raw_mid = float(np.mean(bands[2:5])) if self.num_bands >= 5 else 0.0
        raw_treble = float(np.mean(bands[5:])) if self.num_bands > 5 else 0.0

        self._smoothed_bass += (raw_bass - self._smoothed_bass) * (
            self.attack if raw_bass > self._smoothed_bass else self.release)
        self._smoothed_mid += (raw_mid - self._smoothed_mid) * (
            self.attack if raw_mid > self._smoothed_mid else self.release)
        self._smoothed_treble += (raw_treble - self._smoothed_treble) * (
            self.attack if raw_treble > self._smoothed_treble else self.release)

        bass = float(self._smoothed_bass)
        mid = float(self._smoothed_mid)
        treble = float(self._smoothed_treble)

        # Beat detection: compare the *unsmoothed* low-band energy with its
        # recent rolling average. A kick drum is a sharp spike in that ratio,
        # which is far more reliable than watching the smoothed level.
        bass_energy = float(np.mean(raw_bands[:2])) if self.num_bands >= 2 else float(raw_bands[0])
        self._bass_energy_hist.append(bass_energy)

        beat_detected = False
        beat_strength = 0.0

        now = time.time()
        if self._beat_cooldown > 0:
            self._beat_cooldown -= 1
        elif len(self._bass_energy_hist) >= 8:
            hist = np.fromiter(self._bass_energy_hist, dtype=np.float32)
            avg = float(np.mean(hist))
            std = float(np.std(hist))
            floor = max(avg * 0.25, 1e-5)
            # threshold scales with both the mean and the variation
            trigger = avg + max(std, avg * 0.20) * max(0.2, self.beat_threshold - 1.0)
            if (bass_energy > trigger
                    and bass_energy > floor
                    and bass > 0.10):
                beat_detected = True
                self._last_beat_time = now
                # frame-based refractory period (~100 ms at 60 fps). Do not use
                # wall-clock time here: it desynchronises from the audio clock
                # whenever frames are processed faster or slower than realtime.
                self._beat_cooldown = 6
                ratio = bass_energy / (trigger + 1e-9)
                beat_strength = float(np.clip(0.45 + (ratio - 1.0) * 0.6, 0.3, 1.0))

        # Keep the legacy fields for anything that still reads them
        self._bass_avg = 0.7 * self._bass_avg + 0.3 * bass
        self._bass_history = np.roll(self._bass_history, 1)
        self._bass_history[0] = bass

        # VU level: RMS of the input block, smoothed
        rms = float(np.sqrt(np.mean(block ** 2)))
        self._vu_smoothed = 0.7 * self._vu_smoothed + 0.3 * rms
        vu_level = np.clip(self._vu_smoothed * 10.0, 0.0, 1.0)  # scale factor tuned

        return {
            "bands": bands.tolist(),
            "bass": bass,
            "mid": mid,
            "treble": treble,
            "beat_detected": beat_detected,
            "beat_strength": beat_strength,
            "vu_level": float(vu_level),
            "rms": rms,
            "spectrum_peak": float(np.max(spectrum)),
        }

    def _empty_result(self) -> dict:
        return {
            "bands": [0.0] * self.num_bands,
            "bass": 0.0,
            "mid": 0.0,
            "treble": 0.0,
            "beat_detected": False,
            "beat_strength": 0.0,
            "vu_level": 0.0,
            "rms": 0.0,
            "spectrum_peak": 0.0,
        }

    def reset(self):
        """Reset AGC and beat detection state."""
        self._band_peaks.fill(0.01)
        self._band_gains.fill(1.0)
        self._bass_history.fill(0.0)
        self._bass_avg = 0.1
        self._beat_cooldown = 0
        self._bass_energy_hist.clear()
        self._last_beat_time = 0.0
        self._vu_smoothed = 0.0
        self._smoothed_bands.fill(0.0)
        self._smoothed_bass = 0.0
        self._smoothed_mid = 0.0
        self._smoothed_treble = 0.0
