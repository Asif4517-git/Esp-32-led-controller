# 🔧 TROUBLESHOOTING GUIDE

## Issue: Device Not Responding + Warm Light Stuck

### Current Status
- **Backend:** Running and healthy ✓
- **Test Button:** Working (returns success) ✓
- **Device HTTP:** Not responding ✗
- **LED State:** Stuck on warm light ✗

---

## 🔍 Diagnosis

The ESP32 is either:
1. **Not powered on** or in a bad state
2. **Running old firmware** without WLED
3. **Network connection lost** (wrong IP or WiFi disconnect)
4. **Crashed** and needs a restart

---

## ✅ Fix Steps (Try in Order)

### Step 1: Power Cycle the ESP32
```
1. Unplug the USB cable from ESP32
2. Wait 5 seconds
3. Plug it back in
4. Wait 30 seconds for WiFi to connect
5. Try the Test Connection button in dashboard
```

### Step 2: Check if WLED is Actually Running
Open in browser: **http://192.168.1.39**

**Expected:** WLED web interface should load  
**If it doesn't load:** WLED firmware is not running

### Step 3: Find the Correct IP Address
The ESP32 might have gotten a new IP from your router.

**Windows:**
```powershell
# Check your router's DHCP table or use:
arp -a | Select-String "192.168.1"
```

Look for a device with MAC address starting with `a4:cf:12` or similar ESP32 prefix.

### Step 4: Reflash WLED (If Needed)

**Only if Steps 1-3 don't work.**

#### Quick Flash via Web (Recommended):
1. Go to: **https://install.wled.me/**
2. Plug ESP32 into USB
3. Click "Install"
4. Select "WLED 16.0.1" (or latest)
5. Follow prompts
6. After flash, configure WiFi in WLED's setup

#### Manual Flash via ESPHome Flasher:
1. Download WLED binary from: https://github.com/Aircoookie/WLED/releases
2. Use ESPHome Flasher or esptool
3. Flash at 115200 baud

---

## 🌐 Fix the Dashboard (Color Options Not Showing)

Your browser cached the old HTML. Force refresh:

### Windows (Chrome/Edge):
```
Ctrl + Shift + R
```

### Or Clear Cache:
```
1. Press Ctrl + Shift + Delete
2. Check "Cached images and files"
3. Click "Clear data"
4. Reload: http://localhost:5000
```

### If Still Not Showing:
The Flask server might not have restarted with new templates.

**Restart the backend:**
```powershell
# In your terminal running app.py:
Ctrl + C

# Then restart:
python app.py
```

---

## 🎯 Expected After Fixes

### Working Device:
- WLED web UI loads at http://192.168.1.39
- Test Connection returns "success"
- LEDs respond to DDP commands

### Dashboard with New Features:
- **Device Status** shows "Online" in green
- **Color Palette** dropdown in Music Settings
- Smooth mode switching

---

## 🔴 DO NOT Reflash If:
- You can access http://192.168.1.39 WLED interface
- LEDs respond to WLED's built-in controls
- Test button works

**Just fix the IP or power cycle first.**

---

## 📝 Quick Test Checklist

After power cycle:
- [ ] Can access http://192.168.1.39 in browser
- [ ] WLED interface loads
- [ ] Test Connection button shows "Connected via DDP"
- [ ] LEDs change color when you click Test
- [ ] Dashboard shows "Device: Online"

If all ✓ → System is working, just hard refresh browser  
If any ✗ → Follow steps above

---

**Current Working Features (once device connects):**
✅ Music goes dark when silent  
✅ No blinking  
✅ 7 effects  
✅ Color palettes  
✅ Device health monitoring  
✅ Smooth transitions  
