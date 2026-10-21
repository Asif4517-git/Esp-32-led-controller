/* ==========================================================================
   Asif Lighting Control — Settings page
   ========================================================================== */

/* ------------------------------------------------------------------ tabs */

document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
        tab.classList.add('active');
        document.getElementById(`tab-${tab.dataset.tab}`)?.classList.add('active');
    });
});

/* --------------------------------------------------------------- helpers */

const saveDebounce = {};

function pushSetting(key, value, keyForDebounce = key) {
    if (saveDebounce[keyForDebounce]) clearTimeout(saveDebounce[keyForDebounce]);
    saveDebounce[keyForDebounce] = setTimeout(() => {
        setConfig({ [key]: value });
    }, 150);
}

function bindText(id, key, transform = v => v) {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener('change', () => pushSetting(key, transform(el.value)));
}

function bindSelect(id, key, transform = v => v) {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener('change', () => {
        setConfig({ [key]: transform(el.value) });
        toast('Setting applied', 'success');
    });
}

function bindRange(id, key, labelId, transform = v => Number(v), fmt = v => v) {
    const el = document.getElementById(id);
    const label = document.getElementById(labelId);
    if (!el) return;
    el.addEventListener('input', () => {
        if (label) label.textContent = fmt(el.value);
        pushSetting(key, transform(el.value), key + '-range');
    });
}

/* ---------------------------------------------------------------- colours */

function hexToRgb(hex) {
    const m = /^#?([0-9a-f]{6})$/i.exec(String(hex).trim());
    if (!m) return [0, 0, 0];
    const n = parseInt(m[1], 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function rgbToHex(rgb) {
    return '#' + rgb.map(v => Math.max(0, Math.min(255, Number(v) || 0))
        .toString(16).padStart(2, '0')).join('').toUpperCase();
}

function toggleCustomPalette(palette) {
    const box = document.getElementById('customPaletteFields');
    if (!box) return;
    box.classList.toggle('hidden-field', palette !== 'custom');
}

/* ------------------------------------------------- performance colour ramps */

const RAMP_METRICS = {
    cpu: {
        key: 'perf_cpu_colors',
        ids: ['cpuColor0', 'cpuColor50', 'cpuColor100'],
        preview: 'cpuRampPreview',
    },
    ram: {
        key: 'perf_ram_colors',
        ids: ['ramColor0', 'ramColor50', 'ramColor100'],
        preview: 'ramRampPreview',
    },
    gpu: {
        key: 'perf_gpu_colors',
        ids: ['gpuColor0', 'gpuColor50', 'gpuColor100'],
        preview: 'gpuRampPreview',
    },
};

function readRamp(metric) {
    const spec = RAMP_METRICS[metric];
    return spec.ids.map((id, i) => {
        const input = document.getElementById(id);
        return input ? hexToRgb(input.value) : PERF_DEFAULT_RAMPS[spec.key][i];
    });
}

/** Redraw the gradient bar and return the three stops. */
function paintRampPreview(metric) {
    const spec = RAMP_METRICS[metric];
    const colours = readRamp(metric);
    const preview = document.getElementById(spec.preview);
    if (preview) preview.style.background = perfRampCss(colours);
    return colours;
}

function loadRamp(metric, colours) {
    const spec = RAMP_METRICS[metric];
    const defaults = PERF_DEFAULT_RAMPS[spec.key];
    spec.ids.forEach((id, i) => {
        const input = document.getElementById(id);
        if (!input) return;
        const rgb = (Array.isArray(colours) && colours.length > i)
            ? colours[i] : defaults[i];
        input.value = rgbToHex(rgb);
    });
    paintRampPreview(metric);
}

/* ---------------------------------------------------- palette picker list */

const BUILTIN_PALETTES = [
    ['default', 'Spectrum', 'linear-gradient(90deg,#005aff,#00dcdc,#ffd200,#ff2850)'],
    ['blue_red', 'Blue → Red', 'linear-gradient(90deg,#003cff,#5a00ff,#ff0046)'],
    ['purple_yellow', 'Purple → Yellow', 'linear-gradient(90deg,#9600ff,#ff46c8,#ffdc00)'],
    ['green_red', 'Green → Red', 'linear-gradient(90deg,#00dc46,#bee600,#ff1e14)'],
    ['ocean', 'Ocean', 'linear-gradient(90deg,#001ea0,#0096ff,#00ffcd)'],
    ['fire', 'Fire', 'linear-gradient(90deg,#aa0000,#ff5000,#ffdc00)'],
    ['mono', 'Monochrome', 'linear-gradient(90deg,#ffffff,#ffffff)'],
];

let palettesPromise = null;

function loadWledPalettes() {
    if (!palettesPromise) {
        palettesPromise = apiGet('/api/palettes')
            .then(data => data.items || [])
            .catch(() => []);
    }
    return palettesPromise;
}

function buildPaletteList(cfg) {
    const list = document.getElementById('musicPaletteList');
    if (!list) return;

    const current = cfg.music_color_palette || 'default';
    list.innerHTML = '';

    const addItem = (value, name, gradient) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'palette-item' + (value === current ? ' active' : '');
        btn.title = name;
        btn.dataset.value = value;

        const label = document.createElement('span');
        label.className = 'palette-name';
        label.textContent = name;
        const bar = document.createElement('span');
        bar.className = 'palette-bar';
        bar.style.background = gradient;

        btn.appendChild(label);
        btn.appendChild(bar);

        btn.addEventListener('click', () => {
            list.querySelectorAll('.palette-item').forEach(x => x.classList.remove('active'));
            btn.classList.add('active');
            toggleCustomPalette(value);
            setConfig({ music_color_palette: value });
            toast(`Palette: ${name}`, 'success');
        });
        list.appendChild(btn);
        return btn;
    };

    BUILTIN_PALETTES.forEach(([v, n, g]) => addItem(v, n, g));

    const from = hexToRgb(document.getElementById('musicColorFrom')?.value || '#0078FF');
    const to = hexToRgb(document.getElementById('musicColorTo')?.value || '#FF2850');
    addItem('custom', 'Custom two colours',
        `linear-gradient(90deg, rgb(${from.join(',')}), rgb(${to.join(',')}))`);

    const header = document.createElement('div');
    header.className = 'palette-group';
    header.textContent = 'WLED palettes';
    list.appendChild(header);

    loadWledPalettes().then(items => {
        items.forEach(entry => addItem(entry.value, entry.name, entry.gradient));
    });
}

/* -------------------------------------------------------------- load state */

function onConfigLoaded(cfg) {
    document.getElementById('ipInput').value = cfg.esp32_ip || '';
    document.getElementById('ledCount').value = cfg.led_count || 8;

    document.getElementById('cycleSlider').value = cfg.perf_test_cycle_time || 5;
    document.getElementById('cycleValue').textContent =
        Number(cfg.perf_test_cycle_time || 5).toFixed(1);

    document.getElementById('thresholdSlider').value = cfg.high_usage_threshold || 90;
    document.getElementById('thresholdValue').textContent =
        `${cfg.high_usage_threshold || 90}%`;

    document.getElementById('briMin').value = cfg.brightness_min ?? 20;
    document.getElementById('briMinValue').textContent = cfg.brightness_min ?? 20;
    document.getElementById('briMax').value = cfg.brightness_max ?? 255;
    document.getElementById('briMaxValue').textContent = cfg.brightness_max ?? 255;

    ['cpu', 'ram', 'gpu'].forEach(metric => {
        loadRamp(metric, cfg[RAMP_METRICS[metric].key]);
    });
    document.getElementById('dimWithLoad').checked = !!cfg.perf_dim_with_load;

    document.getElementById('musicRenderer').value = cfg.music_renderer || 'spectrum';
    document.getElementById('musicFps').value = cfg.music_fps || 60;
    document.getElementById('musicFpsValue').textContent = cfg.music_fps || 60;
    document.getElementById('musicSens').value = cfg.music_sensitivity || 1.6;
    document.getElementById('musicSensValue').textContent =
        Number(cfg.music_sensitivity || 1.6).toFixed(1);
    document.getElementById('musicSource').value = cfg.music_source || 'system';

    const from = cfg.music_color_from || [0, 120, 255];
    const to = cfg.music_color_to || [255, 40, 80];
    document.getElementById('musicColorFrom').value = rgbToHex(from);
    document.getElementById('musicColorTo').value = rgbToHex(to);
    toggleCustomPalette(cfg.music_color_palette);
    buildPaletteList(cfg);

    document.getElementById('screenMode').value = cfg.screen_mode || 'dominant';
    document.getElementById('screenMonitor').value = cfg.screen_monitor || 1;
    document.getElementById('screenFps').value = cfg.screen_fps || 30;
    document.getElementById('screenFpsValue').textContent = cfg.screen_fps || 30;
}

/* -------------------------------------------------------------- bind events */

document.addEventListener('DOMContentLoaded', () => {
    // Deep link: /settings#performance opens that tab directly
    const hash = location.hash.replace('#', '');
    if (hash) document.querySelector(`.tab[data-tab="${hash}"]`)?.click();

    // Device
    bindText('ipInput', 'esp32_ip');
    bindText('ledCount', 'led_count', v => parseInt(v, 10) || 8);

    document.getElementById('testBtn').addEventListener('click', async () => {
        const btn = document.getElementById('testBtn');
        btn.disabled = true;
        btn.textContent = 'Testing…';
        try {
            const res = await apiGet('/api/test');
            if (res.success) toast(res.message, 'success');
            else toast(res.message || 'Connection failed', 'error');
        } catch (e) {
            toast('Connection test failed', 'error');
        } finally {
            btn.disabled = false;
            btn.textContent = 'Test connection';
        }
    });

    document.getElementById('detectBtn').addEventListener('click', async () => {
        const btn = document.getElementById('detectBtn');
        btn.disabled = true;
        btn.textContent = 'Searching…';
        try {
            const res = await apiGet('/api/autodetect');
            if (res.success) {
                document.getElementById('ipInput').value = res.ip;
                toast(`Found device at ${res.ip}`, 'success');
            } else {
                toast(res.message || 'Device not found', 'error');
            }
        } catch (e) {
            toast('Auto-detect failed', 'error');
        } finally {
            btn.disabled = false;
            btn.textContent = 'Auto-detect';
        }
    });

    // Performance
    bindRange('cycleSlider', 'perf_test_cycle_time', 'cycleValue',
        v => parseFloat(v), v => Number(v).toFixed(1));
    bindRange('thresholdSlider', 'high_usage_threshold', 'thresholdValue',
        v => parseInt(v, 10), v => `${v}%`);
    bindRange('briMin', 'brightness_min', 'briMinValue', v => parseInt(v, 10));
    bindRange('briMax', 'brightness_max', 'briMaxValue', v => parseInt(v, 10));

    // Three-point colour ramps: every colour input updates its metric.
    Object.entries(RAMP_METRICS).forEach(([metric, spec]) => {
        spec.ids.forEach(id => {
            const input = document.getElementById(id);
            if (!input) return;
            input.addEventListener('input', () => {
                const colours = paintRampPreview(metric);
                pushSetting(spec.key, colours, spec.key);
            });
        });
    });

    const dimToggle = document.getElementById('dimWithLoad');
    if (dimToggle) {
        dimToggle.addEventListener('change', e =>
            setConfig({ perf_dim_with_load: e.target.checked }));
    }

    // Music
    bindSelect('musicRenderer', 'music_renderer');
    bindSelect('musicSource', 'music_source');
    bindRange('musicFps', 'music_fps', 'musicFpsValue', v => parseInt(v, 10));
    bindRange('musicSens', 'music_sensitivity', 'musicSensValue',
        v => parseFloat(v), v => Number(v).toFixed(1));

    document.getElementById('musicColorFrom').addEventListener('input', e => {
        pushSetting('music_color_from', hexToRgb(e.target.value), 'music-from');
    });
    document.getElementById('musicColorTo').addEventListener('input', e => {
        pushSetting('music_color_to', hexToRgb(e.target.value), 'music-to');
    });

    // Screen
    bindSelect('screenMode', 'screen_mode');
    bindText('screenMonitor', 'screen_monitor', v => parseInt(v, 10) || 1);
    bindRange('screenFps', 'screen_fps', 'screenFpsValue', v => parseInt(v, 10));
});

/* ------------------------------------------------------------ status updates */

function onStatusUpdate(data) {
    if (!data) return;
    const cfg = data.config || {};
    const info = data.device_info || {};

    // Device state
    const state = document.getElementById('devState');
    if (state) {
        if (data.device_online === null) {
            state.textContent = 'Checking…';
            state.style.color = '';
        } else if (data.device_online) {
            state.textContent = 'Online';
            state.style.color = 'var(--green)';
        } else {
            state.textContent = 'Offline';
            state.style.color = 'var(--red)';
        }
    }

    document.getElementById('devVersion').textContent = info.version || '—';
    document.getElementById('devLeds').textContent = info.leds ?? '—';
    document.getElementById('devSignal').textContent =
        info.wifi_signal != null ? `${info.wifi_signal}%` : '—';
    document.getElementById('devUptime').textContent = formatUptime(info.uptime);

    const errEl = document.getElementById('devError');
    if (errEl) errEl.textContent = data.device_error || '';

    // Only overwrite the IP box when the user is not editing it
    const ipInput = document.getElementById('ipInput');
    if (ipInput && document.activeElement !== ipInput) {
        ipInput.value = cfg.esp32_ip || '';
    }

    // Audio panel
    const audio = document.getElementById('audioDevice');
    if (audio) {
        audio.textContent = data.audio?.device_name
            || (cfg.music_source === 'mic' ? 'Microphone' : 'System loopback');
    }
    const audioState = document.getElementById('audioState');
    if (audioState) {
        const running = data.audio?.running;
        audioState.textContent = running ? 'Capturing'
            : (cfg.mode === 'music' ? 'Starting…' : 'Idle');
        audioState.style.color = running ? 'var(--green)' : '';
    }

    const analysis = data.audio_analysis || {};
    ['bass', 'mid', 'treble'].forEach(band => {
        const node = document.getElementById(`audio${band[0].toUpperCase()}${band.slice(1)}`);
        if (node) node.textContent = `${Math.round((analysis[band] || 0) * 100)}%`;
    });

    // Screen panel
    const sc = data.screen;
    const screenState = document.getElementById('screenState');
    if (screenState) {
        if (sc) {
            screenState.textContent = sc.running ? 'Capturing' : 'Idle';
            document.getElementById('screenActual').textContent =
                `${(sc.actual_fps || 0).toFixed(1)} fps`;
            document.getElementById('screenFrames').textContent =
                sc.frames_captured ?? '—';
        } else {
            screenState.textContent = cfg.mode === 'screen' ? 'Starting…' : 'Idle';
            document.getElementById('screenActual').textContent = '—';
            document.getElementById('screenFrames').textContent = '—';
        }
    }
}

function formatUptime(seconds) {
    if (seconds == null) return '—';
    const s = Math.max(0, Math.floor(seconds));
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    if (d > 0) return `${d}d ${h}h`;
    if (h > 0) return `${h}h ${m}m`;
    return `${m}m ${s % 60}s`;
}
