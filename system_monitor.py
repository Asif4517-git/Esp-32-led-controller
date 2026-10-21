"""
System monitoring module - reads CPU, RAM, and optional GPU stats.

CPU usage is read with a non-blocking psutil call. A dedicated sampler thread
in app_v2.py calls get_all_stats() once per second, so the percentage is always
"usage since the previous sample" without blocking the LED control loop.

GPU usage has two sources:

* NVML (pynvml / nvidia-ml-py) for NVIDIA cards.
* Windows "GPU Engine" performance counters, which work for every vendor
  (NVIDIA, AMD, Intel) and still work on older cards whose driver reports
  "Not Supported" for NVML utilisation - the GT 710 is one of those.

If neither is available the module reports gpu_available = False and
everything else keeps working.
"""

import ctypes
import logging
import os
from ctypes import wintypes

import psutil

# ============================================================================
# NVIDIA (NVML)
# ============================================================================

GPU_AVAILABLE = False
GPU_NAME = None
gpu_handle = None
_NVML_UTILISATION = False
_pynvml = None

try:
    import pynvml as _pynvml

    _pynvml.nvmlInit()
    gpu_handle = _pynvml.nvmlDeviceGetHandleByIndex(0)
    try:
        GPU_NAME = _pynvml.nvmlDeviceGetName(gpu_handle)
        if isinstance(GPU_NAME, bytes):
            GPU_NAME = GPU_NAME.decode("utf-8", "ignore")
    except Exception:
        GPU_NAME = "NVIDIA GPU"
    GPU_AVAILABLE = True
    try:
        _pynvml.nvmlDeviceGetUtilizationRates(gpu_handle)
        _NVML_UTILISATION = True
    except Exception:
        # Old Kepler cards such as the GT 710 answer "Not Supported" here.
        # The Windows counters below cover them instead.
        _NVML_UTILISATION = False
    logging.info(f"NVIDIA GPU detected: {GPU_NAME}")
except Exception as e:                                    # pragma: no cover
    GPU_NAME = None
    logging.info(f"NVML unavailable: {e}")

# ============================================================================
# WINDOWS GPU ENGINE COUNTERS (all vendors)
# ============================================================================
# Task Manager's GPU percentage comes from the same counters. We sum the 3D
# engine utilisation across processes and cap it at 100%.

_PDH_MORE_DATA = 0x800007D2
_PDH_FMT_DOUBLE = 0x00000200


class _PDH_COUNTER_VALUE(ctypes.Structure):
    _fields_ = [("CStatus", wintypes.DWORD),
                ("doubleValue", ctypes.c_double)]


class _PDH_COUNTER_ITEM(ctypes.Structure):
    _fields_ = [("szName", wintypes.LPWSTR),
                ("FmtValue", _PDH_COUNTER_VALUE)]


class _GpuEngineCounter:
    """Reads \\GPU Engine(*)\\Utilization Percentage through PDH."""

    def __init__(self):
        self.available = False
        self._pdh = None
        self._query = ctypes.c_void_p()
        self._counter = ctypes.c_void_p()
        if os.name != "nt":
            return
        try:
            pdh = ctypes.WinDLL("pdh.dll")
            if pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._query)) != 0:
                return
            add = getattr(pdh, "PdhAddEnglishCounterW", None)
            if add is None:
                return
            if add(self._query,
                   r"\GPU Engine(*)\Utilization Percentage",
                   0, ctypes.byref(self._counter)) != 0:
                return
            pdh.PdhCollectQueryData(self._query)
            self._pdh = pdh
            self.available = True
        except Exception as e:
            logging.info(f"GPU performance counters unavailable: {e}")

    def usage(self):
        """Current 3D utilisation (0-100), or None if it cannot be read."""
        if not self.available:
            return None
        try:
            if self._pdh.PdhCollectQueryData(self._query) != 0:
                return None
            size = wintypes.DWORD(0)
            count = wintypes.DWORD(0)
            status = self._pdh.PdhGetFormattedCounterArrayW(
                self._counter, _PDH_FMT_DOUBLE,
                ctypes.byref(size), ctypes.byref(count), None)
            # PDH returns Win32 errors as signed longs; normalise before
            # comparing, otherwise PDH_MORE_DATA looks like a failure.
            if (status & 0xFFFFFFFF) != _PDH_MORE_DATA:
                return None
            buf = ctypes.create_string_buffer(size.value)
            status = self._pdh.PdhGetFormattedCounterArrayW(
                self._counter, _PDH_FMT_DOUBLE,
                ctypes.byref(size), ctypes.byref(count), buf)
            if (status & 0xFFFFFFFF) != 0:
                return None

            items = ctypes.cast(buf, ctypes.POINTER(_PDH_COUNTER_ITEM))
            total = 0.0
            for i in range(count.value):
                item = items[i]
                name = item.szName or ""
                if "engtype_3D" not in name:
                    continue
                if item.FmtValue.CStatus == 0:
                    total += item.FmtValue.doubleValue
            return max(0.0, min(100.0, total))
        except Exception:
            return None


_engine_counter = _GpuEngineCounter()
if _engine_counter.available:
    GPU_AVAILABLE = True
    if not GPU_NAME:
        GPU_NAME = "GPU"
    logging.info("Windows GPU engine counters enabled")

# ============================================================================
# METRICS
# ============================================================================


def get_cpu_usage():
    """
    CPU usage percentage (0-100).

    interval=None is deliberate: it returns the load since the previous call,
    which is exactly what the 1 Hz sampler wants, and it never blocks.
    """
    return psutil.cpu_percent(interval=None)


def get_ram_usage():
    """RAM usage percentage (0-100)."""
    return psutil.virtual_memory().percent


def get_gpu_usage():
    """GPU usage percentage (0-100). Returns 0 when no GPU is available."""
    if _NVML_UTILISATION and gpu_handle is not None:
        try:
            return float(_pynvml.nvmlDeviceGetUtilizationRates(gpu_handle).gpu)
        except Exception:
            pass

    value = _engine_counter.usage() if _engine_counter is not None else None
    if value is not None:
        return value
    return 0.0


def get_cpu_temperature():
    """CPU temperature when the platform exposes one, otherwise None."""
    try:
        temps = psutil.sensors_temperatures()
        if temps:
            for name in ("coretemp", "cpu_thermal", "k10temp"):
                if name in temps and temps[name]:
                    return temps[name][0].current
        return None
    except Exception:
        return None


def get_all_stats():
    """Return every metric in one dictionary."""
    return {
        "cpu": round(float(get_cpu_usage()), 1),
        "ram": round(float(get_ram_usage()), 1),
        "gpu": round(float(get_gpu_usage()), 1),
        "temp": get_cpu_temperature(),
        "gpu_available": GPU_AVAILABLE,
        "gpu_name": GPU_NAME,
    }
