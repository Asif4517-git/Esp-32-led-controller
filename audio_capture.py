"""
Audio capture for the music-reactive mode.

Primary path is WASAPI loopback: we open the *speaker* as an input device so we
capture exactly what Windows is playing, digitally, with no microphone and no
room noise. Verified working on this machine with `soundcard`.

A real microphone is also supported for when the music is coming from a phone
or another device rather than from this PC.

Capture runs on its own thread and writes into a ring buffer. The analyser then
pulls the most recent window whenever it is ready for a new frame, so a slow
LED update can never cause audio to back up.
"""

import logging
import threading
import time
from typing import Dict, List, Optional

import numpy as np

try:
    import soundcard as sc
    SOUNDCARD_AVAILABLE = True
    SOUNDCARD_IMPORT_ERROR = None
except Exception as e:                                    # pragma: no cover
    sc = None
    SOUNDCARD_AVAILABLE = False
    SOUNDCARD_IMPORT_ERROR = str(e)

DEFAULT_SAMPLE_RATE = 44100
DEFAULT_BLOCK_SIZE = 1024  # 23 ms per block: small enough to avoid overruns


class AudioDeviceError(RuntimeError):
    """Raised when no usable capture device could be opened."""


def list_audio_devices() -> Dict[str, List[dict]]:
    """
    Enumerate capture options for the dashboard.

    Returns {"system": [...], "microphone": [...]} where each entry has
    id/name/is_default/is_loopback.
    """
    result = {"system": [], "microphone": []}
    if not SOUNDCARD_AVAILABLE:
        return result

    try:
        default_spk = sc.default_speaker()
        default_spk_name = default_spk.name if default_spk else None
    except Exception:
        default_spk_name = None

    try:
        default_mic = sc.default_microphone()
        default_mic_name = default_mic.name if default_mic else None
    except Exception:
        default_mic_name = None

    try:
        for m in sc.all_microphones(include_loopback=True):
            entry = {
                "id": str(m.id),
                "name": str(m.name),
                "is_loopback": bool(getattr(m, "isloopback", False)),
                "is_default": False,
            }
            if entry["is_loopback"]:
                entry["is_default"] = (m.name == default_spk_name)
                result["system"].append(entry)
            else:
                entry["is_default"] = (m.name == default_mic_name)
                result["microphone"].append(entry)
    except Exception as e:
        logging.warning(f"Could not enumerate audio devices: {e}")

    return result


def _find_loopback_device(device_id: Optional[str] = None):
    """Resolve the capture device to use for system-audio (loopback) mode."""
    if not SOUNDCARD_AVAILABLE:
        raise AudioDeviceError(
            f"soundcard library unavailable: {SOUNDCARD_IMPORT_ERROR}"
        )

    mics = sc.all_microphones(include_loopback=True)

    if device_id:
        for m in mics:
            if str(m.id) == str(device_id):
                return m

    # Otherwise prefer the loopback mirror of the default speaker
    try:
        default_spk = sc.default_speaker()
        if default_spk is not None:
            for m in mics:
                if getattr(m, "isloopback", False) and m.name == default_spk.name:
                    return m
    except Exception:
        pass

    # Fall back to any loopback device at all
    for m in mics:
        if getattr(m, "isloopback", False):
            return m

    raise AudioDeviceError(
        "No WASAPI loopback device found. Is an output device enabled?"
    )


def _find_microphone(device_id: Optional[str] = None):
    """Resolve the capture device to use for microphone mode."""
    if not SOUNDCARD_AVAILABLE:
        raise AudioDeviceError(
            f"soundcard library unavailable: {SOUNDCARD_IMPORT_ERROR}"
        )

    if device_id:
        for m in sc.all_microphones(include_loopback=False):
            if str(m.id) == str(device_id):
                return m

    mic = sc.default_microphone()
    if mic is None:
        raise AudioDeviceError("No microphone found.")
    return mic


class AudioCapture:
    """
    Continuously captures audio into a ring buffer.

    Usage:
        cap = AudioCapture(source="system")
        cap.start()
        samples = cap.read(2048)      # latest 2048 mono samples
        cap.stop()
    """

    def __init__(
        self,
        source: str = "system",
        device_id: Optional[str] = None,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        block_size: int = DEFAULT_BLOCK_SIZE,
        buffer_seconds: float = 2.0,  # Increased from 1.0 to prevent discontinuity
    ):
        self.source = source if source in ("system", "mic") else "system"
        self.device_id = device_id
        self.sample_rate = int(sample_rate)
        self.block_size = int(block_size)

        capacity = int(self.sample_rate * buffer_seconds)
        self._capacity = max(capacity, self.block_size * 4)
        self._buffer = np.zeros(self._capacity, dtype=np.float32)
        self._write_index = 0
        self._filled = 0

        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self.device_name: Optional[str] = None
        self.last_error: Optional[str] = None
        self.blocks_captured = 0
        self.last_block_time = 0.0
        self._peak_since_read = 0.0
        self._rms_since_read = 0.0
        self._running = False

    # ----------------------------------------------------------------- control

    def _open_device(self):
        if self.source == "mic":
            return _find_microphone(self.device_id)
        return _find_loopback_device(self.device_id)

    def start(self) -> bool:
        """Start the capture thread. Returns True if the device opened."""
        if self._running:
            return True

        self._stop_event.clear()
        try:
            device = self._open_device()
            self.device_name = str(device.name)
        except AudioDeviceError as e:
            self.last_error = str(e)
            logging.error(f"Audio capture unavailable: {e}")
            return False
        except Exception as e:
            self.last_error = str(e)
            logging.error(f"Audio capture failed to open: {e}")
            return False

        self._thread = threading.Thread(
            target=self._capture_loop, args=(device,), daemon=True
        )
        self._thread.start()
        self._running = True
        logging.info(
            f"Audio capture started: source={self.source} device={self.device_name!r} "
            f"@ {self.sample_rate} Hz"
        )
        return True

    def stop(self):
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
        self._running = False
        logging.info("Audio capture stopped")

    @property
    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    def _capture_loop(self, device):
        """Blocking capture loop, runs on its own thread."""
        consecutive_errors = 0
        recorder = None

        try:
            recorder = device.recorder(
                samplerate=self.sample_rate,
                channels=2,
                blocksize=self.block_size,
            )
            recorder.__enter__()

            while not self._stop_event.is_set():
                try:
                    block = recorder.record(numframes=self.block_size)
                    if block is None or len(block) == 0:
                        continue

                    block = np.asarray(block, dtype=np.float32)
                    if block.ndim > 1:
                        block = block.mean(axis=1)

                    self._push(block)
                    self.blocks_captured += 1
                    self.last_block_time = time.time()
                    consecutive_errors = 0

                except Exception as e:
                    consecutive_errors += 1
                    self.last_error = str(e)
                    if consecutive_errors == 1:
                        logging.warning(f"Audio block error: {e}")
                    if consecutive_errors > 50:
                        logging.error("Too many audio errors, stopping capture")
                        break
                    time.sleep(0.05)

        except Exception as e:
            self.last_error = f"Capture device error: {e}"
            logging.error(self.last_error)
        finally:
            if recorder is not None:
                try:
                    recorder.__exit__(None, None, None)
                except Exception:
                    pass
            self._running = False

    # ------------------------------------------------------------ ring buffer

    def _push(self, block: np.ndarray):
        with self._lock:
            n = len(block)
            if n >= self._capacity:
                self._buffer[:] = block[-self._capacity:]
                self._write_index = 0
                self._filled = self._capacity
            else:
                end = self._write_index + n
                if end <= self._capacity:
                    self._buffer[self._write_index:end] = block
                else:
                    split = self._capacity - self._write_index
                    self._buffer[self._write_index:] = block[:split]
                    self._buffer[:end - self._capacity] = block[split:]
                self._write_index = end % self._capacity
                self._filled = min(self._filled + n, self._capacity)

            self._peak_since_read = max(self._peak_since_read, float(np.max(np.abs(block))))
            self._rms_since_read = float(np.sqrt(np.mean(block ** 2)))

    def read(self, num_samples: int) -> Optional[np.ndarray]:
        """
        Return the most recent `num_samples` mono samples, oldest first.
        Returns None if the buffer is not yet full enough.
        """
        with self._lock:
            if self._filled < num_samples:
                return None
            start = (self._write_index - num_samples) % self._capacity
            if start + num_samples <= self._capacity:
                out = self._buffer[start:start + num_samples].copy()
            else:
                first = self._capacity - start
                out = np.concatenate(
                    (self._buffer[start:], self._buffer[:num_samples - first])
                )
            return out

    def read_metrics(self) -> dict:
        """Consume the peak/RMS accumulators since the last call."""
        with self._lock:
            peak = self._peak_since_read
            rms = self._rms_since_read
            self._peak_since_read = 0.0
        return {"peak": peak, "rms": rms}

    def status(self) -> dict:
        return {
            "available": SOUNDCARD_AVAILABLE,
            "running": self.is_running,
            "source": self.source,
            "device_name": self.device_name,
            "sample_rate": self.sample_rate,
            "blocks_captured": self.blocks_captured,
            "last_error": self.last_error,
        }
