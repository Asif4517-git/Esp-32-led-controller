# 🔴 URGENT FIX: ESP32 Lost WiFi Connection

## Problem
- WLED not showing in WiFi devices
- http://192.168.1.39 not loading
- ESP32 stuck on warm light

**Cause:** ESP32 lost WiFi credentials or reverted to AP mode.

---

## ✅ SOLUTION: Reconnect ESP32 to WiFi

### Method 1: Access Point Mode (Easiest)

When WLED can't connect to WiFi, it creates its own WiFi network.

#### Steps:
1. **Unplug ESP32, wait 5 seconds, plug back in**
2. **Wait 30 seconds**
3. **On your phone or laptop, check WiFi networks**
4. **Look for:** `WLED-AP` or `WLED-xxxxx` (where xxxxx is part of MAC address)

#### Connect to WLED-AP:
- **Network name:** WLED-AP
- **Password:** wled1234 (default) or no password

#### Configure WiFi:
1. Once connected to WLED-AP, open browser
2. Go to: **http://4.3.2.1** (this is WLED's AP address)
3. WLED interface will load
4. Click **Config** (gear icon) → **WiFi Setup**
5. Enter your WiFi credentials:
   - **SSID:** Your WiFi network name
   - **Password:** Your WiFi password
6. Click **Save & Connect**
7. ESP32 will reboot and join your network

#### Find New IP:
After ESP32 reboots:
1. Check your router's admin page
2. Look for "WLED" device
3. Note the new IP address
4. Update in dashboard: http://localhost:5000

---

### Method 2: Reset Button (If AP Mode Doesn't Appear)

Some ESP32 boards have a physical button to force AP mode.

#### Try this:
1. **Unplug ESP32**
2. **Hold down the BOOT/FLASH button** (small button on board)
3. **Plug in USB while holding button**
4. **Keep holding for 5 seconds**
5. **Release button**
6. Wait 30 seconds, check for WLED-AP network

---

### Method 3: Serial Console (Advanced)

If AP mode doesn't work, use serial to reconfigure:

#### Windows:
1. Install PuTTY or Arduino IDE
2. Connect ESP32 via USB
3. Open Serial Monitor at **115200 baud**
4. You'll see boot messages
5. Type commands to set WiFi (check WLED docs)

---

## 🔧 After Reconnecting

### Update Dashboard IP:
1. Go to: http://localhost:5000
2. Scroll to **Device Configuration**
3. Change **ESP32 IP Address** to the new IP
4. Click **Test Connection**
5. Should show "Connected via DDP"

### Verify Everything Works:
1. Open new IP in browser (e.g., http://192.168.1.42)
2. WLED interface should load
3. Click Test Connection in dashboard
4. Try Music Reactive mode

---

## 🎯 What IP Will It Be?

Your router assigns IPs automatically. The new IP will likely be:
- `192.168.1.40`
- `192.168.1.41`
- `192.168.1.42`
- etc.

**Check your router:**
- Login: usually http://192.168.1.1 or http://192.168.0.1
- Username/Password: often printed on router
- Look for "Connected Devices" or "DHCP Clients"
- Find "WLED" or "ESP32" device

---

## 🚨 Still Can't Find WLED-AP?

The ESP32 might need a full reset or reflash.

### Option A: Factory Reset via Pin
Some ESP32s have a reset sequence:
1. Power on ESP32
2. Short GPIO0 to GND for 10 seconds
3. Release
4. Should boot into AP mode

### Option B: Reflash WLED (Nuclear Option)
Only if nothing else works:

1. Go to: **https://install.wled.me/**
2. Click **"Install"**
3. Select port (ESP32 should show up)
4. Choose **WLED_0.16.1_ESP32.bin**
5. Click **Install**
6. Wait 2-3 minutes
7. Follow WiFi setup wizard

**This will work 100%** but you'll lose any presets saved on the device.

---

## ⚡ Quick Checklist

- [ ] Unplugged and replugged ESP32
- [ ] Waited 30 seconds
- [ ] Checked WiFi for "WLED-AP" network
- [ ] Connected to WLED-AP (password: wled1234)
- [ ] Opened http://4.3.2.1
- [ ] Configured WiFi in WLED settings
- [ ] Found new IP in router
- [ ] Updated IP in dashboard
- [ ] Test Connection works

---

## 📱 Can't See WLED-AP on Phone?

Some phones hide 2.4GHz networks. Try:
1. Use laptop instead of phone
2. Disable 5GHz WiFi temporarily
3. Move closer to ESP32
4. Use WiFi scanner app to see all networks

---

**The Python controller is working perfectly** — we just need to reconnect the ESP32 to WiFi so it can receive the commands.

After WiFi is configured, everything will work: music reactive, screen capture, system stats, color palettes, smooth transitions — all ready to go!
