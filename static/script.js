// Unified LED Controller - Dashboard JavaScript

let currentMode = 'off';
let pollInterval = null;

// ============================================================================
// INITIALIZATION
// ============================================================================

document.addEventListener('DOMContentLoaded', () => {
    loadConfig();
    attachEventListeners();
    startPolling();
});

// ============================================================================
// EVENT LISTENERS
// ============================================================================

function attachEventListeners() {
    // Mode buttons
    document.querySelectorAll('.mode-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const mode = btn.dataset.mode;
            setMode(mode);
        });
    });

    // Enable toggle
    document.getElementById('enableToggle').addEventListener('change', (e) => {
        saveConfig();
    });

    // Save config button
    document.getElementById('saveConfig').addEventListener('click', () => {
        saveConfig();
    });

    // Test connection
    document.getElementById('testConnection').addEventListener('click', () => {
        testConnection();
    });

    // Sliders with live value display
    document.getElementById('brightnessMin').addEventListener('input', (e) => {
        document.getElementById('brightnessMinValue').textContent = e.target.value;
    });
    document.getElementById('brightnessMax').addEventListener('input', (e) => {
        document.getElementById('brightnessMaxValue').textContent = e.target.value;
    });

    // Mode-specific controls auto-save
    ['musicRenderer', 'musicSource', 'musicFps', 'musicColorPalette', 'screenMode', 'screenMonitor', 'screenFps'].forEach(id => {
        const el = document.getElementById(id);
        if (el) {
            el.addEventListener('change', () => saveConfig());
        }
    });
}

// ============================================================================
// CONFIG LOAD/SAVE
// ============================================================================

async function loadConfig() {
    try {
        const resp = await fetch('/api/stats');
        const data = await resp.json();
        const cfg = data.config;

        // General
        document.getElementById('esp32Ip').value = cfg.esp32_ip || '';
        document.getElementById('ledCount').value = cfg.led_count || 8;
        document.getElementById('enableToggle').checked = cfg.enabled || false;

        // System stats
        document.getElementById('updateInterval').value = cfg.update_interval || 1.1;
        document.getElementById('threshold').value = cfg.high_usage_threshold || 90;
        document.getElementById('brightnessMin').value = cfg.brightness_min || 20;
        document.getElementById('brightnessMax').value = cfg.brightness_max || 255;
        document.getElementById('brightnessMinValue').textContent = cfg.brightness_min || 20;
        document.getElementById('brightnessMaxValue').textContent = cfg.brightness_max || 255;

        // Music
        document.getElementById('musicRenderer').value = cfg.music_renderer || 'spectrum';
        document.getElementById('musicSource').value = cfg.music_source || 'system';
        document.getElementById('musicFps').value = cfg.music_fps || 60;
        document.getElementById('musicColorPalette').value = cfg.music_color_palette || 'default';

        // Screen
        document.getElementById('screenMode').value = cfg.screen_mode || 'bands';
        document.getElementById('screenMonitor').value = cfg.screen_monitor || 1;
        document.getElementById('screenFps').value = cfg.screen_fps || 30;

        // Set mode UI
        currentMode = cfg.mode || 'off';
        updateModeUI();

    } catch (err) {
        console.error('Failed to load config:', err);
        showError('Failed to load configuration');
    }
}

async function saveConfig() {
    const cfg = {
        esp32_ip: document.getElementById('esp32Ip').value,
        led_count: parseInt(document.getElementById('ledCount').value),
        enabled: document.getElementById('enableToggle').checked,
        mode: currentMode,

        update_interval: parseFloat(document.getElementById('updateInterval').value),
        high_usage_threshold: parseInt(document.getElementById('threshold').value),
        brightness_min: parseInt(document.getElementById('brightnessMin').value),
        brightness_max: parseInt(document.getElementById('brightnessMax').value),

        music_renderer: document.getElementById('musicRenderer').value,
        music_source: document.getElementById('musicSource').value,
        music_fps: parseInt(document.getElementById('musicFps').value),
        music_color_palette: document.getElementById('musicColorPalette').value,

        screen_mode: document.getElementById('screenMode').value,
        screen_monitor: parseInt(document.getElementById('screenMonitor').value),
        screen_fps: parseInt(document.getElementById('screenFps').value),
    };

    try {
        const resp = await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(cfg),
        });
        const result = await resp.json();
        if (result.success) {
            showStatus('Settings saved', 'success');
        }
    } catch (err) {
        console.error('Save failed:', err);
        showError('Failed to save settings');
    }
}

async function testConnection() {
    try {
        const resp = await fetch('/api/test_connection');
        const result = await resp.json();
        if (result.success) {
            showStatus(result.message, 'success');
        } else {
            showError(result.message);
        }
    } catch (err) {
        showError('Connection test failed');
    }
}

// ============================================================================
// MODE SWITCHING
// ============================================================================

function setMode(mode) {
    currentMode = mode;
    updateModeUI();
    saveConfig();
}

function updateModeUI() {
    // Update button states
    document.querySelectorAll('.mode-btn').forEach(btn => {
        if (btn.dataset.mode === currentMode) {
            btn.classList.add('active');
        } else {
            btn.classList.remove('active');
        }
    });

    // Show/hide mode-specific settings
    document.getElementById('settingsSystemStats').style.display = 
        currentMode === 'system_stats' ? 'block' : 'none';
    document.getElementById('settingsMusic').style.display = 
        currentMode === 'music' ? 'block' : 'none';
    document.getElementById('settingsScreen').style.display = 
        currentMode === 'screen' ? 'block' : 'none';

    // Update LED label
    const labels = {
        'off': 'LEDs Off',
        'system_stats': 'System Performance',
        'music': 'Music Reactive',
        'screen': 'Screen Mirror',
    };
    document.getElementById('ledLabel').textContent = labels[currentMode] || '8 LEDs';
}

// ============================================================================
// POLLING & STATUS UPDATE
// ============================================================================

function startPolling() {
    if (pollInterval) clearInterval(pollInterval);
    pollInterval = setInterval(updateStatus, 500);
    updateStatus();
}

async function updateStatus() {
    try {
        const resp = await fetch('/api/stats');
        const data = await resp.json();

        // Connection status
        const enabled = data.config.enabled;
        const mode = data.config.mode;
        const statusDot = document.getElementById('statusDot');
        const statusText = document.getElementById('statusText');

        if (enabled && mode !== 'off') {
            statusDot.className = 'status-dot status-connected';
            statusText.textContent = `Active - ${getModeLabel(mode)}`;
        } else {
            statusDot.className = 'status-dot status-idle';
            statusText.textContent = 'Idle';
        }

        // Frame stats
        if (data.frame_stats) {
            document.getElementById('fpsDisplay').textContent = 
                `${data.frame_stats.fps.toFixed(1)} fps`;
            document.getElementById('frameDisplay').textContent = 
                `${data.frame_stats.frames} frames`;
        }

        // Device health status
        if (data.device_online !== undefined) {
            const deviceStatusText = document.getElementById('deviceStatusText');
            if (data.device_online) {
                deviceStatusText.textContent = 'Online';
                deviceStatusText.style.color = '#10b981';
            } else {
                deviceStatusText.textContent = 'Offline';
                deviceStatusText.style.color = '#ef4444';
                if (data.device_error) {
                    showError(`Device: ${data.device_error}`);
                }
            }
        }

        // Errors
        if (data.last_error) {
            showError(data.last_error);
        } else if (data.device_online) {
            hideError();
        }

        // Mode-specific updates
        if (mode === 'system_stats' && data.stats) {
            updateSystemStats(data.stats);
        } else if (mode === 'music' && data.audio) {
            updateAudioStatus(data.audio);
        } else if (mode === 'screen' && data.screen) {
            updateScreenStatus(data.screen);
        }

    } catch (err) {
        console.error('Poll failed:', err);
        const statusDot = document.getElementById('statusDot');
        statusDot.className = 'status-dot status-error';
        document.getElementById('statusText').textContent = 'Disconnected';
    }
}

function getModeLabel(mode) {
    const labels = {
        'system_stats': 'System Stats',
        'music': 'Music Reactive',
        'screen': 'Screen Capture',
        'off': 'Off',
    };
    return labels[mode] || mode;
}

function updateSystemStats(stats) {
    const display = document.getElementById('systemStatsDisplay');
    display.style.display = 'block';
    
    document.getElementById('cpuStat').textContent = `${stats.cpu.toFixed(1)}%`;
    document.getElementById('ramStat').textContent = `${stats.ram.toFixed(1)}%`;
    
    if (stats.gpu_available) {
        document.getElementById('gpuStat').textContent = `${stats.gpu.toFixed(1)}%`;
    } else {
        document.getElementById('gpuStat').textContent = 'N/A';
    }
}

function updateAudioStatus(audio) {
    const status = document.getElementById('audioStatus');
    status.style.display = 'block';
    
    if (audio.running && audio.device_name) {
        document.getElementById('audioDevice').textContent = audio.device_name;
    } else {
        document.getElementById('audioDevice').textContent = 'Not running';
    }
}

function updateScreenStatus(screen) {
    const status = document.getElementById('screenStatus');
    status.style.display = 'block';
    
    document.getElementById('screenMonitorDisplay').textContent = screen.monitor || 1;
    document.getElementById('screenActualFps').textContent = 
        (screen.actual_fps || 0).toFixed(1);
}

// ============================================================================
// UI FEEDBACK
// ============================================================================

function showStatus(msg, type = 'info') {
    // Could implement a toast notification here
    console.log(`[${type}] ${msg}`);
}

function showError(msg) {
    const errorDisplay = document.getElementById('errorDisplay');
    errorDisplay.textContent = msg;
    errorDisplay.style.display = 'block';
}

function hideError() {
    const errorDisplay = document.getElementById('errorDisplay');
    errorDisplay.style.display = 'none';
}
