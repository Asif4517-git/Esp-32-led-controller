"""Verify music mode: silent = dark, sound = lit, transitions smooth."""
import json
import threading
import time
import urllib.request

import numpy as np
import soundcard as sc

BASE = "http://127.0.0.1:5000"


def post(path, body):
    req = urllib.request.Request(
        f"{BASE}{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=10))


def stats():
    return json.load(urllib.request.urlopen(f"{BASE}/api/stats", timeout=10))


def sample_colors(seconds=2.0, interval=0.1):
    out = []
    t0 = time.time()
    while time.time() - t0 < seconds:
        s = stats()
        out.append([tuple(c) for c in (s.get("preview_colors") or [])])
        time.sleep(interval)
    return out


def max_channel(frames):
    m = 0
    for f in frames:
        for c in f:
            m = max(m, max(c))
    return m


def play_tone(freq, seconds, amp=0.35):
    """Play a tone through the default speaker so loopback can capture it."""
    spk = sc.default_speaker()
    sr = 44100
    t = np.linspace(0, seconds, int(sr * seconds), endpoint=False)
    sig = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    stereo = np.stack([sig, sig], axis=1)
    with spk.player(samplerate=sr, channels=2, blocksize=1024) as p:
        p.play(stereo)


print("=" * 66)
print("MUSIC MODE BEHAVIOUR TEST")
print("=" * 66)

post("/api/config", {"mode": "music", "power_on": True})
print("switched to music mode, waiting for the blue/red confirmation…")
time.sleep(3.5)

# ---------------------------------------------------------------- silence
print("\n1. SILENCE (nothing playing)")
frames = sample_colors(seconds=2.5)
peak = max_channel(frames)
print(f"   frames sampled: {len(frames)}")
print(f"   brightest channel value: {peak}")
if peak <= 2:
    print("   PASS - strip is completely dark in silence")
else:
    print("   FAIL - strip is still lit with no audio")

# ---------------------------------------------------------------- bass
print("\n2. BASS TONE (200 Hz)")
threading.Thread(target=play_tone, args=(200, 2.5), daemon=True).start()
time.sleep(0.4)
frames = sample_colors(seconds=2.0)
peak = max_channel(frames)
print(f"   brightest channel value: {peak}")
if peak > 25:
    print("   PASS - strip lights up on bass")
else:
    print("   FAIL - bass did not light the strip")

# ---------------------------------------------------------------- treble
print("\n3. TREBLE TONE (4 kHz)")
threading.Thread(target=play_tone, args=(4000, 2.5), daemon=True).start()
time.sleep(0.4)
frames = sample_colors(seconds=2.0)
peak = max_channel(frames)
print(f"   brightest channel value: {peak}")
if peak > 15:
    print("   PASS - strip reacts to treble")
else:
    print("   FAIL - treble did not light the strip")

# ------------------------------------------------------- smoothness check
print("\n4. SMOOTHNESS (frame-to-frame change)")
time.sleep(2.5)  # let the tone finish
post("/api/config", {"mode": "music"})
threading.Thread(target=play_tone, args=(120, 4.0), daemon=True).start()
time.sleep(0.5)
frames = sample_colors(seconds=3.0, interval=0.05)

jumps = []
prev = None
for f in frames:
    # average channel value across the strip
    avg = np.mean([np.mean(c) for c in f]) if f else 0.0
    if prev is not None:
        jumps.append(abs(avg - prev))
    prev = avg

if jumps:
    avg_jump = float(np.mean(jumps))
    max_jump = float(np.max(jumps))
    print(f"   average frame-to-frame change: {avg_jump:.1f}")
    print(f"   largest frame-to-frame change: {max_jump:.1f}")
    if max_jump < 90:
        print("   PASS - no harsh jumps, transitions are smooth")
    else:
        print("   WARN - some large jumps detected")

# ------------------------------------------------------------- return dark
print("\n5. BACK TO SILENCE")
time.sleep(4.0)
frames = sample_colors(seconds=2.0)
peak = max_channel(frames)
print(f"   brightest channel value: {peak}")
if peak <= 2:
    print("   PASS - strip returned to dark")
else:
    print("   FAIL - strip stayed lit after audio stopped")

print("\n" + "=" * 66)
