"""
Read WLED's own palette definitions from the device.

WLED does not expose palette colours in /json/palettes (names only). The web
UI gets the colours from /json/palx?page=N, which returns:

    {"p": {"9": [[0,0,32,64],[64,0,128,255], ...]}, "m": 9}

Each entry is a list of [position 0..255, r, g, b] stops. A handful of
palettes are "dynamic" instead of fixed:

    "c1"                       -> the segment's first colour
    "c2"                       -> the segment's second colour
    "c3"                       -> the segment's third colour
    ["r","r","r","r"]          -> random colours

We keep those as tokens and let the renderer substitute the user's two picked
music colours, which is the closest sensible equivalent without a segment.

Everything is cached; a failed fetch simply returns the cached copy (or an
empty list), never raises.
"""

import logging
import random
import threading
import time
from typing import Dict, List, Optional

import requests

CACHE_SECONDS = 300

_lock = threading.Lock()
_cache = {
    "ts": 0.0,
    "host": None,
    "palettes": [],       # list of {index, name, stops, tokens}
}

# Fallback gradient used when the device cannot be reached
_FALLBACK = [(0, 0, 90, 255), (128, 0, 220, 220), (255, 255, 40, 80)]


def _normalise_stops(value) -> Optional[List[tuple]]:
    """Turn a /json/palx entry into sorted (position, r, g, b) tuples."""
    if not isinstance(value, list) or not value:
        return None
    stops = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) < 4:
            return None
        try:
            pos, r, g, b = (int(item[0]), int(item[1]), int(item[2]), int(item[3]))
        except (TypeError, ValueError):
            return None
        stops.append((max(0, min(255, pos)),
                      max(0, min(255, r)),
                      max(0, min(255, g)),
                      max(0, min(255, b))))
    stops.sort(key=lambda s: s[0])
    if len(stops) == 1:
        stops.append((255, stops[0][1], stops[0][2], stops[0][3]))
    return stops


def _normalise_tokens(value) -> Optional[List[str]]:
    """Dynamic palettes: ['c1', 'c2'] or ['r', 'r', ...]."""
    if isinstance(value, str):
        return [value] * 4
    if not isinstance(value, list) or not value:
        return None
    if not all(isinstance(v, str) for v in value):
        return None
    return list(value)


def fetch_palettes(host: str, max_age: float = CACHE_SECONDS) -> List[dict]:
    """
    Return [{'index', 'name', 'stops', 'tokens'}] for every palette.

    Never raises: on failure the previous cache is returned (possibly empty).
    """
    if not host:
        return _cache["palettes"]

    with _lock:
        fresh = (time.time() - _cache["ts"] < max_age
                 and _cache["host"] == host
                 and _cache["palettes"])
        if fresh:
            return _cache["palettes"]

    try:
        base = f"http://{host}"
        names = requests.get(f"{base}/json/palettes", timeout=3).json()
        if not isinstance(names, list) or not names:
            raise ValueError("empty palette list")

        index_data: Dict[int, object] = {}
        page = 0
        max_page = 0
        while page <= 32:
            resp = requests.get(f"{base}/json/palx?page={page}", timeout=3)
            if resp.status_code != 200:
                break
            payload = resp.json() or {}
            chunk = payload.get("p") or {}
            for key, value in chunk.items():
                try:
                    index_data[int(key)] = value
                except (TypeError, ValueError):
                    continue
            max_page = int(payload.get("m", page) or page)
            if page >= max_page:
                break
            page += 1

        palettes = []
        for index, name in enumerate(names):
            raw = index_data.get(index)
            stops = _normalise_stops(raw)
            tokens = None if stops else _normalise_tokens(raw)
            palettes.append({
                "index": index,
                "name": str(name),
                "stops": stops,
                "tokens": tokens,
            })

        with _lock:
            _cache.update(ts=time.time(), host=host, palettes=palettes)
        logging.info(f"Loaded {len(palettes)} WLED palettes from {host}")
        return palettes

    except Exception as e:
        logging.warning(f"Could not read WLED palettes: {e}")
        return _cache["palettes"]


def get_palette(host: str, index: int) -> Optional[dict]:
    """Look up one palette by WLED index."""
    for entry in fetch_palettes(host):
        if entry["index"] == index:
            return entry
    return None


def find_palette(host: str, ident) -> Optional[dict]:
    """
    Resolve a palette from either an index, a WLED name, or 'wled:<n>' /
    'wled:<name>'.
    """
    if ident is None:
        return None
    text = str(ident)
    if text.startswith("wled:"):
        text = text[5:]

    palettes = fetch_palettes(host)
    if not palettes:
        return None

    try:
        index = int(text)
    except (TypeError, ValueError):
        index = None

    if index is not None:
        for entry in palettes:
            if entry["index"] == index:
                return entry
        return None

    lowered = text.strip().lower()
    for entry in palettes:
        if entry["name"].strip().lower() == lowered:
            return entry
        # WLED marks dynamic palettes with a leading "* "
        if entry["name"].lstrip("* ").strip().lower() == lowered:
            return entry
    return None


def sample(entry: dict, t: float, color_from=(0, 120, 255), color_to=(255, 40, 80)):
    """
    Return the RGB colour at position t (0..1) of a palette.

    Used by the dashboard preview so it can show roughly what a palette-based
    WLED effect is doing instead of one flat colour.
    """
    if not entry:
        return None

    stops = entry.get("stops")
    if stops:
        pos = max(0.0, min(1.0, float(t))) * 255.0
        if pos <= stops[0][0]:
            return tuple(stops[0][1:4])
        if pos >= stops[-1][0]:
            return tuple(stops[-1][1:4])
        for i in range(len(stops) - 1):
            p0, r0, g0, b0 = stops[i]
            p1, r1, g1, b1 = stops[i + 1]
            if p0 <= pos <= p1:
                k = (pos - p0) / max(1, p1 - p0)
                return (
                    int(r0 + (r1 - r0) * k),
                    int(g0 + (g1 - g0) * k),
                    int(b0 + (b1 - b0) * k),
                )
        return tuple(stops[-1][1:4])

    tokens = entry.get("tokens") or []
    if tokens:
        idx = int(round(max(0.0, min(1.0, float(t))) * (len(tokens) - 1)))
        token = tokens[max(0, min(len(tokens) - 1, idx))]
        if token == "c2":
            return tuple(color_to)
        if token == "c3":
            return tuple((a + b) // 2 for a, b in zip(color_from, color_to))
        return tuple(color_from)

    return None


def css_gradient(entry: dict, color_from=(0, 120, 255), color_to=(255, 40, 80)) -> str:
    """Build a CSS gradient string so the dashboard can preview a palette."""
    stops = entry.get("stops")
    if stops:
        parts = []
        for pos, r, g, b in stops:
            pct = round(pos / 255 * 100, 1)
            parts.append(f"rgb({r},{g},{b}) {pct}%")
        return "linear-gradient(90deg," + ",".join(parts) + ")"

    tokens = entry.get("tokens") or []
    if tokens:
        colours = []
        for token in tokens:
            if token == "r":
                colours.append(tuple(random.randint(40, 255) for _ in range(3)))
            elif token == "c1":
                colours.append(tuple(color_from))
            elif token == "c2":
                colours.append(tuple(color_to))
            elif token == "c3":
                colours.append(tuple((a + b) // 2 for a, b in zip(color_from, color_to)))
            else:
                colours.append((120, 120, 120))
        parts = []
        for i, c in enumerate(colours):
            pct = round(i / max(1, len(colours) - 1) * 100, 1)
            parts.append(f"rgb({c[0]},{c[1]},{c[2]}) {pct}%")
        return "linear-gradient(90deg," + ",".join(parts) + ")"

    # Unknown palette: neutral placeholder
    return "linear-gradient(90deg,#1e293b,#334155)"


def fallback_stops():
    return list(_FALLBACK)
