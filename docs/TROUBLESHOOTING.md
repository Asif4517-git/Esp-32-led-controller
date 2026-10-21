# Troubleshooting

Start with the status pill at the top right of the dashboard. **Online** means
the app can reach the ESP32; anything else points at the network or the IP
setting. Application logs live next to `app_v2.py`:

- `controller.log` — app events, mode changes, errors
- `controller_console.log` — everything the background process printed

## The dashboard will not open

- The app listens on <http://localhost:5000>. If the page is blank, check the
  process is alive: `Get-Process pythonw`.
- **"Another controller is already running"** means a second copy was started.
  Only one can hold `controller.lock`. Close the other copy, or end the
  `pythonw` process and start again.
- If port 5000 is taken by something else, stop that program or change the
  port in `app_v2.py` (`app.run(..., port=5000)`).
- A stale page can hide new settings. Press `Ctrl + Shift + R`.

## The ESP32 shows offline

1. Open the WLED web UI in a browser using the same IP. If that fails, the
   problem is the device or its network, not this app.
2. Power-cycle the ESP32 and wait about 30 seconds for Wi-Fi.
3. Check the IP in **Settings → Device**. Routers hand out new addresses after
   a reboot; a DHCP reservation avoids this.
4. Ping it: `ping <ip>`. No reply means wrong IP, different subnet, or Wi-Fi
   down. See [WLED-SETUP.md](WLED-SETUP.md) for reconnecting the board.
5. Allow Python through Windows Firewall on **private** networks. DDP uses
   **UDP 4048**; HTTP uses TCP 80.
6. If the WLED UI works but the app still says offline, click **Test
   connection**; the error text under the field names the failing step.

## The ESP32 has dropped off Wi-Fi

When the board cannot reach the saved network it falls back to its own access
point, and the strip often sits on a warm white glow. Reconnect it like this:

1. Unplug the ESP32, wait five seconds, plug it back in, then give it about
   30 seconds to boot.
2. On a laptop, look for a network called **WLED-AP** (sometimes
   `WLED-xxxx`, where the suffix is part of the MAC address).
3. Join it. The password is `wled1234`, or there is no password on some builds.
4. Open <http://4.3.2.1> and go to **Config → WiFi Setup**.
5. Enter your network name and password and save. The board reboots and rejoins
   your LAN.
6. Find the new address in the router's DHCP client list, or on the WLED
   **Info** page, then update **Settings → Device** in the dashboard.

If **WLED-AP** never appears, hold the **BOOT** button while plugging the board
in, or reflash it from <https://install.wled.me/> — a reflash clears any presets
stored on the device. Some phones hide 2.4 GHz networks, so a laptop is easier
to test with. A DHCP reservation in the router stops the address changing
again.

## LEDs light up but the colours are wrong

- **LED count** must match the strip exactly. Too many LEDs leaves the extra
  ones dark; too few cuts the end off. Set it under **Settings → Device**.
- **Colour order** is set in WLED under **Config → LED Preferences**. Most
  WS2812B strips are **GRB**. If red and green are swapped, that setting is
  wrong, not the app.
- WLED's own **brightness** multiplies everything the app sends. Turn it up to
  full while testing.
- Long strips browning out at high brightness need power injection; that shows
  as colour shift toward the far end.

## Performance mode

- The strip shows one metric at a time and rotates **CPU → RAM → GPU** every
  *Seconds per metric*. That is deliberate; it is not a bug.
- **GPU n/a** means no supported GPU was found. GPU load comes from NVIDIA's
  NVML, so AMD and Intel GPUs are skipped and the rotation uses CPU and RAM.
- The three colour stops per metric (0 %, 50 %, 100 %) are under
  **Settings → Performance → Metric colours**. Values between stops are
  interpolated in HSV, so blue → yellow passes through green.
- Above *High-usage warning* the strip turns solid red.
- If the colour looks dimmer than the picked value, check **Dim brightness
  with load**: when it is on, brightness is also scaled by load.

## Music mode

- Use **System audio (loopback)** to capture what the PC is playing. It works
  with headphones and does not need a microphone.
- Silence is intentional: with no audio the strip goes dark. Play something
  and watch the meters on the Home page.
- If the meters move but the strip does not, the mode is working and the
  output path is the problem; check the device status.
- Microphone mode is for music from a phone or another room. Raise **Input
  sensitivity** if the bars barely move.
- Very quiet audio can stay below the noise gate. Raise the source volume or
  the sensitivity.

## Screen mode

- **Dominant colour** deliberately ignores white, grey and black. A uniformly
  white page has no colour to report, so the strip stays white or grey; that
  is expected. A green heading on a white page reports green.
- If the colours feel wrong for the content, try **Average** (whole-screen
  ambient tone) or **Bands** (one colour per screen slice).
- **Monitor** is 1-based, the same numbering as Windows Display Settings.
- Capture runs at roughly 25–30 fps on a 1600×900 display because grabbing a
  frame is the expensive part; the easing still makes changes look smooth.
- HDR screens can report desaturated colours to capture APIs. Disabling HDR
  for the monitor improves colour accuracy.
- Screen capture only runs while Screen mode is selected.

## Starting over

Stop the app, delete `config.json`, and start it again. Defaults are recreated
on launch and the settings page repopulates. `config.example.json` shows every
key with its default value.

## Still stuck

Open an issue on GitHub with:

- what you expected and what happened,
- the mode you were in,
- the last 20 lines of `controller.log`.
