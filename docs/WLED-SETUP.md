# WLED setup

The controller does not talk to the LED strip directly. It talks to **WLED**
running on an ESP32, which owns the strip. Set WLED up first, then point the
app at it.

## What you need

| Part | Notes |
|---|---|
| ESP32 board | Any ESP32 / ESP32-WROOM dev board |
| LED strip | WS2812B / SK6812, 5 V (or 12 V with the matching WLED setup) |
| Power supply | Sized for the strip: roughly 60 mA per LED at full white |
| USB cable | For the first flash and for the serial console |

Keep the ESP32 and the PC on the same network.

## 1. Flash WLED

1. Open <https://install.wled.me/> in Chrome or Edge.
2. Connect the ESP32 over USB.
3. Click **Install**, pick the port, and choose the current WLED release
   (0.16.x is what this project is tested with).
4. Wait for the flash to finish, then the board reboots.

If the web installer cannot see the board, use
[ESPHome Flasher](https://github.com/esphome/esphome-flasher/releases) or
`esptool.py` with the release binary from
<https://github.com/Aircoookie/WLED/releases>.

## 2. Connect it to Wi-Fi

On first boot, WLED creates its own access point:

1. On your phone or laptop, join **WLED-AP** (password `wled1234`, or no
   password on some builds).
2. Open <http://4.3.2.1>.
3. Go to **Config → WiFi Setup**, enter your network name and password, and
   save.
4. The ESP32 reboots and joins your network.

## 3. Configure the strip

In the WLED web UI:

1. **Config → LED Preferences**
2. Set **LED count** to the number of LEDs on your strip.
3. Set **Data GPIO** to the pin your strip is connected to (GPIO 2 and 16 are
   common defaults).
4. Set **Colour order** to match the strip. Most WS2812B strips are **GRB**;
   SK6812 RGBW strips need **GRBW**.
5. Set **Maximum current** to a safe value for your power supply.
6. Save.

Test the strip with one of WLED's built-in effects before involving this app.

## 4. Find the IP address

- The WLED **Info** page shows the current IP.
- Or check your router's DHCP client list for a device called `wled` / `esp32`.
- On Windows: `arp -a` and look for the ESP32's MAC prefix.

Give the ESP32 a **DHCP reservation** in your router if you can. It keeps the
IP stable across reboots, which makes the app's device watchdog less busy.

## 5. Point the app at it

1. Start the controller (`python app_v2.py` or the desktop shortcut).
2. Open <http://localhost:5000>.
3. Go to **Settings → Device**.
4. Enter the ESP32's IP, set **Number of LEDs**, and click **Test connection**.
5. The status pill at the top should turn green and say **Online**.

The IP and LED count are saved to `config.json` next to the app.

## 6. Realtime data (DDP)

Performance, music and screen modes stream pixels over **DDP on UDP port
4048**. WLED accepts this by default; no extra setting is needed in recent
releases. If nothing lights up in those modes but the built-in WLED effects
work:

- Allow `python.exe` through Windows Firewall for **private** networks.
- Make sure nothing else is holding the strip in realtime mode.
- Confirm the app shows the device as online under **Settings → Device**.

Custom mode is different: it hands control back to WLED over HTTP, so the
effect runs on the ESP32 itself.
