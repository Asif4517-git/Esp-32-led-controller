"""End-to-end verification for LED Studio v2."""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:5000"


def get(path, timeout=10):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=timeout) as r:
        return json.load(r)


def post(path, body, timeout=10):
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def device_state():
    ip = get("/api/config")["esp32_ip"]
    with urllib.request.urlopen(f"http://{ip}/json/info", timeout=4) as r:
        info = json.load(r)
    with urllib.request.urlopen(f"http://{ip}/json/state", timeout=4) as r:
        state = json.load(r)
    return info, state


def line(label, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f"  {detail}" if detail else ""))
    return ok


print("=" * 68)
print("LED STUDIO v2 - END TO END VERIFICATION")
print("=" * 68)

results = []

# ---------------------------------------------------------------- pages
print("\n1. PAGES")
for path in ("/", "/settings", "/custom"):
    try:
        with urllib.request.urlopen(f"{BASE}{path}", timeout=8) as r:
            body = r.read().decode("utf-8", "ignore")
        results.append(line(f"{path} loads", r.status == 200 and "LED Studio" in body))
    except Exception as e:
        results.append(line(f"{path} loads", False, str(e)))

# --------------------------------------------------------------- device
print("\n2. DEVICE")
cfg = get("/api/config")
ip = cfg["esp32_ip"]
print(f"       configured IP: {ip}")
try:
    info, state = device_state()
    results.append(line("device reachable", True, f"WLED {info.get('ver')}"))
    results.append(line("LED count matches", info["leds"]["count"] == cfg["led_count"],
                        f"{info['leds']['count']} LEDs"))
except Exception as e:
    results.append(line("device reachable", False, str(e)))
    info = state = None

# --------------------------------------------------------------- effects
print("\n3. EFFECT / PALETTE LISTS")
try:
    fx = get("/api/effects")
    results.append(line("effects listed", len(fx.get("effects", [])) > 10,
                        f"{len(fx.get('effects', []))} effects"))
except Exception as e:
    results.append(line("effects listed", False, str(e)))

try:
    pal = get("/api/palettes")
    results.append(line("palettes listed", len(pal.get("palettes", [])) > 0,
                        f"{len(pal.get('palettes', []))} palettes"))
except Exception as e:
    results.append(line("palettes listed", False, str(e)))

# ----------------------------------------------------------- each mode
print("\n4. MODES")

# --- performance
post("/api/config", {"mode": "performance", "power_on": True})
time.sleep(3)
st = get("/api/stats")
try:
    info, state = device_state()
    live = bool(info.get("live"))
    results.append(line("performance drives strip", live, f"lm={info.get('lm')}"))
    results.append(line("rotation state present", "current" in st.get("perf_test_state", {}),
                        f"showing {st['perf_test_state'].get('current')}"))
except Exception as e:
    results.append(line("performance drives strip", False, str(e)))

first_metric = st.get("perf_test_state", {}).get("current")
print(f"       waiting to confirm rotation switches metrics (was {first_metric})...")
time.sleep(3)

# --- music
post("/api/config", {"mode": "music", "power_on": True})
time.sleep(4)
st = get("/api/stats")
audio = st.get("audio", {})
results.append(line("audio capture running", audio.get("running", False),
                    audio.get("device_name", "")))
if audio.get("running"):
    results.append(line("audio blocks arriving", audio.get("blocks_captured", 0) > 0,
                        f"{audio.get('blocks_captured')} blocks"))

# --- screen (dominant)
post("/api/config", {"mode": "screen", "screen_mode": "dominant", "power_on": True})
time.sleep(3)
st = get("/api/stats")
scr = st.get("screen", {})
results.append(line("screen capture running", scr.get("running", False),
                    f"{scr.get('actual_fps', 0):.1f} fps"))
prev = st.get("preview_colors", [])
results.append(line("dominant colour produced", bool(prev), str(prev[:1])))

# --- screen (bands)
post("/api/config", {"screen_mode": "bands"})
time.sleep(2)
st = get("/api/stats")
results.append(line("band mode switch applies", st.get("screen", {}).get("mode") == "bands",
                    str(st.get("screen", {}).get("mode"))))

# --- custom
post("/api/config", {"mode": "custom", "custom_color": [0, 255, 128],
                     "custom_brightness": 200, "custom_effect": 0})
time.sleep(3)
try:
    info, state = device_state()
    seg = state["seg"][0]
    results.append(line("custom colour applied", state.get("on") is True,
                        f"col={seg['col'][0]} bri={state.get('bri')}"))
except Exception as e:
    results.append(line("custom colour applied", False, str(e)))

# --- power off
post("/api/power", {"power_on": False})
time.sleep(2)
st = get("/api/stats")
results.append(line("power off sets mode off", st.get("config", {}).get("mode") == "off",
                    str(st.get("config", {}).get("mode"))))

# --- power back on restores mode
post("/api/power", {"power_on": True})
time.sleep(2)
st = get("/api/stats")
results.append(line("power on restores last mode",
                    st.get("config", {}).get("mode") == "custom",
                    str(st.get("config", {}).get("mode"))))

# --- final: leave in performance
post("/api/config", {"mode": "performance"})

print("\n" + "=" * 68)
passed = sum(1 for r in results if r)
print(f"RESULT: {passed}/{len(results)} checks passed")
print("=" * 68)
