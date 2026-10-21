"""
Unified LED Controller - Flask application.

Four modes:
  1. system_stats  - CPU/RAM/GPU mapped to color/brightness/speed (original)
  2. music         - Real-time audio analysis → LED visuals
  3. screen        - Screen capture → ambient lighting
  4. off           - LEDs off, controller idle

All modes now use DDP for high frame rates. HTTP JSON is kept as a fallback
and for compatibility with the old system_stats implementation.
"""

import json
import logging
import threading
import time
from flask import Flask, render_template, jsonify, request

# Our modules
from system_monitor import get_all_stats
from wled_api import WLEDController
from wled_ddp import DDPController
from audio_capture import AudioCapture, list_audio_devices
from audio_analysis import AudioAnalyzer
from music_renderers import create_renderer, RENDERERS
from screen_capture import ScreenCapture

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

app = Flask(__name__)

# ============================================================================
# GLOBAL STATE
# ============================================================================

config = {}
ddp: DDPController = None
wled_http: WLEDController = None

# Mode-specific workers
audio_capture: AudioCapture = None
audio_analyzer: AudioAnalyzer = None
music_renderer = None
screen_capture: ScreenCapture = None

# Control thread
control_thread: threading.Thread = None
stop_event = threading.Event()

# Stats & errors
stats_cache = {}
last_error = None
frame_stats = {
    "fps": 0.0,
    "frames": 0,
    "last_frame_time": 0.0,
    "last_health_check": 0.0,
    "device_online": True,
    "device_error": None,
}


# ============================================================================
# CONFIG
# ============================================================================

def load_config():
    global config
    try:
        with open('config.json', 'r') as f:
            config = json.load(f)
        logging.info("Configuration loaded")
    except FileNotFoundError:
        config = {
            "esp32_ip": "192.168.1.39",
            "led_count": 8,
            "enabled": False,
            "mode": "off",
            
            # system_stats mode (legacy)
            "update_interval": 1.1,
            "brightness_min": 20,
            "brightness_max": 255,
            "high_usage_threshold": 90,
            
            # music mode
            "music_renderer": "spectrum",
            "music_source": "system",
            "music_device_id": None,
            "music_fps": 60,
            "music_color_palette": "default",
            
            # screen mode
            "screen_mode": "bands",
            "screen_monitor": 1,
            "screen_fps": 30,
        }
        save_config()
        logging.info("Created default configuration")


def save_config():
    with open('config.json', 'w') as f:
        json.dump(config, f, indent=2)
    logging.info("Configuration saved")


# ============================================================================
# SYSTEM STATS MODE (original implementation, unchanged)
# ============================================================================

def map_cpu_to_color(cpu_percent):
    if cpu_percent <= 30:
        ratio = cpu_percent / 30.0
        r = int(ratio * 100)
        g = 255
        b = 0
    elif cpu_percent <= 70:
        ratio = (cpu_percent - 30) / 40.0
        r = int(100 + ratio * 155)
        g = int(255 - ratio * 100)
        b = 0
    else:
        ratio = (cpu_percent - 70) / 30.0
        r = 255
        g = int(155 * (1 - ratio))
        b = 0
    return (r, g, b)


def map_gpu_to_brightness(gpu_percent, min_bri, max_bri):
    range_bri = max_bri - min_bri
    return int(min_bri + (gpu_percent / 100.0) * range_bri)


def map_ram_to_speed(ram_percent):
    return int(50 + (ram_percent / 100.0) * 205)


# ============================================================================
# UNIFIED CONTROL LOOP
# ============================================================================

def control_loop():
    global last_error, stats_cache, ddp, wled_http
    global audio_capture, audio_analyzer, music_renderer, screen_capture
    global frame_stats
    
    logging.info("Control loop started")
    
    frame_count = 0
    fps_t0 = time.time()
    
    while not stop_event.is_set():
        try:
            mode = config.get('mode', 'off')
            enabled = config.get('enabled', False)
            
            if not enabled or mode == 'off':
                # Release device from live mode when disabled
                if ddp is not None and ddp.frames_sent > 0:
                    ddp.release()
                    logging.info("Released device from live mode")
                time.sleep(0.5)
                continue
            
            # Ensure DDP controller exists
            ip = config.get('esp32_ip')
            led_count = config.get('led_count', 8)
            if ddp is None or ddp.ip_address != ip or ddp.led_count != led_count:
                ddp = DDPController(ip, led_count=led_count)
                logging.info(f"DDP controller initialized: {ip}, {led_count} LEDs")
            
            # ----------------------------------------------------------------
            # MODE: system_stats
            # ----------------------------------------------------------------
            if mode == 'system_stats':
                stats = get_all_stats()
                stats_cache = stats
                
                cpu = stats['cpu']
                ram = stats['ram']
                gpu = stats['gpu']
                
                threshold = config.get('high_usage_threshold', 90)
                if cpu >= threshold or gpu >= threshold:
                    # Red strobe warning
                    ddp.send_solid(255, 0, 0)
                else:
                    r, g, b = map_cpu_to_color(cpu)
                    if stats['gpu_available']:
                        brightness = map_gpu_to_brightness(
                            gpu,
                            config.get('brightness_min', 20),
                            config.get('brightness_max', 255)
                        )
                    else:
                        brightness = (config.get('brightness_min', 20) +
                                      config.get('brightness_max', 255)) // 2
                    
                    # Ensure minimum brightness to prevent blinking
                    brightness = max(brightness, 30)
                    
                    # Scale color by brightness
                    r = int(r * brightness / 255)
                    g = int(g * brightness / 255)
                    b = int(b * brightness / 255)
                    
                    ddp.send_solid(r, g, b)
                
                time.sleep(config.get('update_interval', 1.1))
            
            # ----------------------------------------------------------------
            # MODE: music
            # ----------------------------------------------------------------
            elif mode == 'music':
                # Start audio capture if not running
                if audio_capture is None or not audio_capture.is_running:
                    audio_capture = AudioCapture(
                        source=config.get('music_source', 'system'),
                        device_id=config.get('music_device_id'),
                        sample_rate=44100,
                    )
                    if not audio_capture.start():
                        last_error = audio_capture.last_error
                        time.sleep(1)
                        continue
                
                if audio_analyzer is None:
                    audio_analyzer = AudioAnalyzer(sample_rate=44100, fft_size=2048, num_bands=led_count)
                
                if music_renderer is None:
                    renderer_name = config.get('music_renderer', 'spectrum')
                    color_palette = config.get('music_color_palette', 'default')
                    music_renderer = create_renderer(renderer_name, led_count, color_palette)
                
                # Read audio and analyze
                samples = audio_capture.read(2048)
                if samples is not None:
                    analysis = audio_analyzer.analyze(samples)
                    colors = music_renderer.render(analysis)
                    ddp.send_pixels(colors)
                    
                    frame_count += 1
                    frame_stats['frames'] = frame_count
                    frame_stats['last_frame_time'] = time.time()
                else:
                    # Hold last frame if audio buffer is temporarily empty (prevents blinking)
                    pass
                
                target_fps = min(config.get('music_fps', 60), 120)  # Cap at 120 fps
                time.sleep(1.0 / target_fps)
            
            # ----------------------------------------------------------------
            # MODE: screen
            # ----------------------------------------------------------------
            elif mode == 'screen':
                # Start screen capture if not running
                if screen_capture is None or not screen_capture.is_running:
                    screen_capture = ScreenCapture(
                        led_count=led_count,
                        mode=config.get('screen_mode', 'bands'),
                        monitor=config.get('screen_monitor', 1),
                        fps=config.get('screen_fps', 30),
                    )
                    if not screen_capture.start():
                        last_error = screen_capture.last_error
                        time.sleep(1)
                        continue
                
                colors = screen_capture.get_colors()
                ddp.send_pixels(colors)
                
                frame_count += 1
                frame_stats['frames'] = frame_count
                frame_stats['last_frame_time'] = time.time()
                
                target_fps = config.get('screen_fps', 30)
                time.sleep(1.0 / target_fps)
            
            # ----------------------------------------------------------------
            # Update FPS
            # ----------------------------------------------------------------
            now = time.time()
            if now - fps_t0 >= 1.0:
                frame_stats['fps'] = frame_count / (now - fps_t0)
                frame_count = 0
                fps_t0 = now
        
        except Exception as e:
            logging.error(f"Control loop error: {e}")
            last_error = str(e)
            time.sleep(1)
    
    logging.info("Control loop stopped")


def start_control_thread():
    global control_thread, stop_event
    if control_thread is None or not control_thread.is_alive():
        stop_event.clear()
        control_thread = threading.Thread(target=control_loop, daemon=True)
        control_thread.start()
        logging.info("Control thread started")


def stop_workers():
    """Stop all capture workers."""
    global audio_capture, screen_capture
    if audio_capture is not None:
        audio_capture.stop()
        audio_capture = None
    if screen_capture is not None:
        screen_capture.stop()
        screen_capture = None


# ============================================================================
# FLASK ROUTES
# ============================================================================

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/stats')
def api_stats():
    """Get current system stats and status."""
    status = {
        'config': config,
        'last_error': last_error,
        'frame_stats': frame_stats,
    }
    
    # Add mode-specific stats
    if config.get('mode') == 'system_stats':
        status['stats'] = stats_cache
    elif config.get('mode') == 'music' and audio_capture is not None:
        status['audio'] = audio_capture.status()
    elif config.get('mode') == 'screen' and screen_capture is not None:
        status['screen'] = screen_capture.status()
    
    # Add DDP stats and device health
    if ddp is not None:
        status['ddp'] = ddp.stats()
        # Check device health every 5 seconds
        if time.time() - frame_stats.get('last_health_check', 0) > 5.0:
            online, error = ddp.check_device_online()
            status['device_online'] = online
            status['device_error'] = error
            frame_stats['last_health_check'] = time.time()
        else:
            status['device_online'] = frame_stats.get('device_online', True)
            status['device_error'] = frame_stats.get('device_error', None)
    
    return jsonify(status)


@app.route('/api/config', methods=['POST'])
def api_config():
    """Update configuration."""
    global config, music_renderer
    
    data = request.json
    old_mode = config.get('mode')
    
    # Update config
    for key in ['esp32_ip', 'led_count', 'enabled', 'mode', 'update_interval',
                'brightness_min', 'brightness_max', 'high_usage_threshold',
                'music_renderer', 'music_source', 'music_device_id', 'music_fps',
                'music_color_palette',
                'screen_mode', 'screen_monitor', 'screen_fps']:
        if key in data:
            config[key] = data[key]
    
    # Stop workers if mode changed
    new_mode = config.get('mode')
    if new_mode != old_mode:
        stop_workers()
        music_renderer = None  # force re-creation
    
    save_config()
    return jsonify({'success': True, 'config': config})


@app.route('/api/test_connection')
def api_test_connection():
    """Test connection to WLED device."""
    ip = config.get('esp32_ip')
    test_ddp = DDPController(ip)
    
    # First check if device is online
    online, error = test_ddp.check_device_online()
    if not online:
        test_ddp.close()
        return jsonify({'success': False, 'message': error or 'Device offline'})
    
    # Try sending a test packet
    success, error = test_ddp.send_solid(255, 0, 0)
    test_ddp.close()
    
    if success:
        return jsonify({'success': True, 'message': 'Connected via DDP'})
    else:
        return jsonify({'success': False, 'message': error or 'DDP failed'})


@app.route('/api/audio_devices')
def api_audio_devices():
    """List available audio devices."""
    devices = list_audio_devices()
    return jsonify(devices)


@app.route('/api/renderers')
def api_renderers():
    """List available music renderers."""
    return jsonify({'renderers': list(RENDERERS.keys())})


if __name__ == '__main__':
    load_config()
    start_control_thread()
    
    print("\n" + "="*70)
    print("  Unified LED Controller - Full Version")
    print("="*70)
    print(f"\n  Dashboard: http://localhost:5000")
    print(f"  ESP32 IP:  {config.get('esp32_ip')}")
    print(f"  LEDs:      {config.get('led_count')}")
    print(f"\n  Modes: system_stats | music | screen | off")
    print("="*70 + "\n")
    
    app.run(host='0.0.0.0', port=5000, debug=False)
