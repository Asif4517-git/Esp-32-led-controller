"""
DDP (Distributed Display Protocol) transport for WLED.

This is the fast path: instead of POSTing JSON over HTTP once a second, we
push a binary packet straight at the ESP32 over UDP whenever we have a new
frame ready. That is what makes 30-60 fps per-LED animation possible.

Protocol summary (per 3waylabs, as implemented by WLED):
  WLED listens on UDP port 4048.
  Header is 10 bytes, big-endian:
    byte 0      flags   - 0x41 = version 1 | PUSH
    byte 1      sequence counter (wraps at 256)
    byte 2      data type - 1 = RGB (3 bytes/pixel)
    byte 3      destination id - 1 (WLED ignores anything else)
    bytes 4-7   data offset, uint32
    bytes 8-9   data length, uint16
  Followed by the raw payload: 3 bytes per LED.

The device answers no acknowledgement (it is UDP), so the only way to know it
landed is to poll /json/info and look for live=true. `is_live()` does that.
"""

import logging
import socket
import struct
import threading
import time
from typing import List, Optional, Sequence, Tuple

RGB = Tuple[int, int, int]

# DDP flag bits
_DDP_PUSH = 0x01
_DDP_VER1 = 0x40
_DDP_FLAGS = _DDP_VER1 | _DDP_PUSH  # 0x41

# Data type codes
_DDP_TYPE_RGB = 1
_DDP_TYPE_RGBW = 3

# DDP spec caps a single packet payload at 1440 bytes. Anything longer has to
# be split across packets using the offset field.
_MAX_PAYLOAD = 1440


class DDPController:
    """Sends per-LED colour frames to a WLED device over DDP/UDP."""

    def __init__(
        self,
        ip_address: str,
        port: int = 4048,
        led_count: int = 8,
        rgbw: bool = False,
        timeout: float = 1.0,
    ):
        self.ip_address = self._clean_ip(ip_address)
        self.port = port
        self.led_count = max(1, int(led_count))
        self.rgbw = rgbw
        self.timeout = timeout

        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._seq = 0
        self._bytes_per_pixel = 4 if rgbw else 3

        # Lightweight stats for the dashboard
        self.frames_sent = 0
        self.bytes_sent = 0
        self.last_send_time = 0.0
        self.last_error: Optional[str] = None

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _clean_ip(ip: str) -> str:
        ip = (ip or "").strip()
        for prefix in ("http://", "https://"):
            if ip.startswith(prefix):
                ip = ip[len(prefix):]
        return ip.rstrip("/").split("/")[0].split(":")[0]

    def _ensure_socket(self) -> socket.socket:
        if self._sock is None:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 18)
            s.settimeout(self.timeout)
            self._sock = s
        return self._sock

    def _build_packet(self, payload: bytes, offset: int, sequence: int) -> bytes:
        data_type = _DDP_TYPE_RGBW if self.rgbw else _DDP_TYPE_RGB
        header = struct.pack(
            ">BBBBIH",
            _DDP_FLAGS,
            sequence & 0xFF,
            data_type,
            1,               # destination id
            offset,
            len(payload),
        )
        return header + payload

    @staticmethod
    def _clamp(value) -> int:
        try:
            v = int(value)
        except (TypeError, ValueError):
            return 0
        if v < 0:
            return 0
        if v > 255:
            return 255
        return v

    # ------------------------------------------------------------------- send

    def send_pixels(self, colors: Sequence[Sequence[int]]) -> Tuple[bool, Optional[str]]:
        """
        Push one frame. `colors` is a sequence of (r, g, b) triples, one per LED.
        Shorter sequences are padded with black; longer ones are clipped to
        led_count so a miscount can never corrupt the strip.
        """
        try:
            flat = bytearray()
            count = 0
            for rgb in colors:
                if count >= self.led_count:
                    break
                r = self._clamp(rgb[0] if len(rgb) > 0 else 0)
                g = self._clamp(rgb[1] if len(rgb) > 1 else 0)
                b = self._clamp(rgb[2] if len(rgb) > 2 else 0)
                if self.rgbw:
                    w = self._clamp(rgb[3]) if len(rgb) > 3 else 0
                    flat += bytes((r, g, b, w))
                else:
                    flat += bytes((r, g, b))
                count += 1

            # Pad any missing LEDs with black
            while count < self.led_count:
                flat += b"\x00" * self._bytes_per_pixel
                count += 1

            payload = bytes(flat)

            with self._lock:
                sock = self._ensure_socket()
                for offset in range(0, len(payload), _MAX_PAYLOAD):
                    chunk = payload[offset:offset + _MAX_PAYLOAD]
                    self._seq = (self._seq + 1) & 0xFF
                    sock.sendto(
                        self._build_packet(chunk, offset, self._seq),
                        (self.ip_address, self.port),
                    )
                self.frames_sent += 1
                self.bytes_sent += len(payload) + 10
                self.last_send_time = time.time()

            self.last_error = None
            return True, None

        except socket.timeout:
            self.last_error = "DDP timeout"
            return False, self.last_error
        except socket.gaierror:
            self.last_error = f"Cannot resolve {self.ip_address}"
            return False, self.last_error
        except OSError as e:
            self.last_error = f"Network error: {e}"
            return False, self.last_error
        except Exception as e:
            self.last_error = str(e)
            return False, self.last_error

    def send_solid(self, r: int, g: int, b: int) -> Tuple[bool, Optional[str]]:
        """Convenience: paint every LED the same colour."""
        return self.send_pixels([(r, g, b)] * self.led_count)

    # -------------------------------------------------------------- diagnostics

    def is_live(self, http_ip: Optional[str] = None) -> bool:
        """
        Ask the device whether it is currently in realtime (live) mode.
        A False result means our packets are not being accepted.
        """
        import json
        import urllib.request

        host = http_ip or self.ip_address
        try:
            with urllib.request.urlopen(f"http://{host}/json/info", timeout=2) as r:
                info = json.load(r)
            return bool(info.get("live"))
        except Exception as e:
            self.last_error = f"Device check failed: {e}"
            return False
    
    def check_device_online(self, http_ip: Optional[str] = None) -> Tuple[bool, Optional[str]]:
        """
        Check if the WLED device is reachable and responding.
        Returns (online: bool, error_message: Optional[str])
        """
        import json
        import urllib.request

        host = http_ip or self.ip_address
        try:
            with urllib.request.urlopen(f"http://{host}/json/info", timeout=2) as r:
                info = json.load(r)
            # Device is online if we got a valid response
            return True, None
        except urllib.error.URLError as e:
            return False, f"Device offline or unreachable: {e.reason}"
        except Exception as e:
            return False, f"Connection error: {str(e)}"

    def release(self, http_ip: Optional[str] = None) -> Tuple[bool, Optional[str]]:
        """
        Hand control back to WLED's normal engine. WLED drops out of live mode
        on its own after a short timeout, but we can force it immediately.
        """
        import json
        import urllib.request

        host = http_ip or self.ip_address
        try:
            req = urllib.request.Request(
                f"http://{host}/json/state",
                data=json.dumps({"live": False}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as r:
                return r.status == 200, None
        except Exception as e:
            return False, str(e)

    def stats(self) -> dict:
        return {
            "frames_sent": self.frames_sent,
            "bytes_sent": self.bytes_sent,
            "last_send_time": self.last_send_time,
            "last_error": self.last_error,
        }

    def close(self):
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None
