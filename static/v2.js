/* ==========================================================================
   LED Studio v2 — shared front-end logic
   ========================================================================== */

const LED = {
    config: {},
    deviceOnline: null,
    stats: {},
    frameStats: {},
    perfState: {},
};

/* ------------------------------------------------------------------ helpers */

async function apiGet(path) {
    const r = await fetch(path);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
}

async function apiPost(path, body) {
    const r = await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
}

/** Update a config value and apply it instantly (no save button). */
async function setConfig(patch, { silent = true } = {}) {
    Object.assign(LED.config, patch);
    try {
        await apiPost('/api/config', patch);
        if (!silent) toast('Saved', 'success');
    } catch (e) {
        toast('Failed to apply setting', 'error');
    }
}

let toastTimer = null;
function toast(msg, type = '') {
    const stack = document.getElementById('toastStack');
    if (!stack) return;
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.textContent = msg;
    stack.appendChild(el);
    setTimeout(() => {
        el.style.opacity = '0';
        el.style.transform = 'translateX(20px)';
        el.style.transition = 'all 0.25s';
        setTimeout(() => el.remove(), 260);
    }, 2600);
}

/* ------------------------------------------- performance colour ramps */

/**
 * Three-stop performance ramps (0 %, 50 %, 100 % load).
 *
 * Blending happens in HSV, not RGB, so mixing blue and yellow reaches green
 * halfway - the same result the controller's Python code produces.
 */
function perfRgbToHsv(rgb) {
    const r = rgb[0] / 255, g = rgb[1] / 255, b = rgb[2] / 255;
    const max = Math.max(r, g, b), min = Math.min(r, g, b);
    const d = max - min;
    let h = 0;
    if (d !== 0) {
        if (max === r) h = 60 * (((g - b) / d) % 6);
        else if (max === g) h = 60 * ((b - r) / d + 2);
        else h = 60 * ((r - g) / d + 4);
    }
    if (h < 0) h += 360;
    return { h, s: max === 0 ? 0 : d / max, v: max };
}

function perfHsvToRgb(h, s, v) {
    h = ((h % 360) + 360) % 360;
    const c = v * s;
    const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
    const m = v - c;
    let r = 0, g = 0, b = 0;
    if (h < 60) { r = c; g = x; }
    else if (h < 120) { r = x; g = c; }
    else if (h < 180) { g = c; b = x; }
    else if (h < 240) { g = x; b = c; }
    else if (h < 300) { r = x; b = c; }
    else { r = c; b = x; }
    return [
        Math.round((r + m) * 255),
        Math.round((g + m) * 255),
        Math.round((b + m) * 255),
    ];
}

function perfBlend(c1, c2, t) {
    t = Math.max(0, Math.min(1, t));
    if (t <= 0) return c1.slice();
    if (t >= 1) return c2.slice();

    const a = perfRgbToHsv(c1);
    const b = perfRgbToHsv(c2);
    if (a.s < 0.01) a.h = b.h;
    if (b.s < 0.01) b.h = a.h;

    let dh = b.h - a.h;
    if (dh > 180) dh -= 360;
    else if (dh < -180) dh += 360;

    return perfHsvToRgb(
        a.h + dh * t,
        a.s + (b.s - a.s) * t,
        a.v + (b.v - a.v) * t,
    );
}

const PERF_DEFAULT_RAMPS = {
    perf_cpu_colors: [[0, 220, 70], [255, 190, 0], [255, 30, 20]],
    perf_ram_colors: [[0, 60, 160], [0, 100, 220], [0, 140, 255]],
    perf_gpu_colors: [[70, 0, 140], [130, 35, 200], [190, 70, 255]],
};

function perfRampAt(colours, value) {
    const c = (Array.isArray(colours) && colours.length >= 3)
        ? colours
        : PERF_DEFAULT_RAMPS.perf_cpu_colors;
    const pct = Math.max(0, Math.min(100, Number(value) || 0));
    return pct <= 50
        ? perfBlend(c[0], c[1], pct / 50)
        : perfBlend(c[1], c[2], (pct - 50) / 50);
}

/**
 * Redraw a ramp as a CSS gradient, sampled finely enough that the CSS
 * interpolation tracks the HSV blend.
 */
function perfRampCss(colours, steps = 20) {
    const stops = [];
    for (let i = 0; i <= steps; i++) {
        const pct = i * 100 / steps;
        const rgb = perfRampAt(colours, pct);
        stops.push(`rgb(${rgb[0]},${rgb[1]},${rgb[2]}) ${pct.toFixed(1)}%`);
    }
    return `linear-gradient(90deg, ${stops.join(', ')})`;
}

/* -------------------------------------------------------------- power state */

function applyPowerUI(on) {
    const btn = document.getElementById('powerBtn');
    if (!btn) return;
    btn.classList.toggle('on', on);
    btn.classList.toggle('off', !on);
    btn.title = on ? 'Power on — click to turn LEDs off' : 'Power off — click to turn LEDs on';
}

async function togglePower() {
    const btn = document.getElementById('powerBtn');
    const next = !LED.config.power_on;

    // Reflect the new state right away. The controller fades the strip over
    // about a second, so waiting for the HTTP reply would feel unresponsive.
    LED.config.power_on = next;
    applyPowerUI(next);
    if (btn) btn.disabled = true;

    try {
        const res = await apiPost('/api/power', { power_on: next });
        LED.config.power_on = res.power_on;
        LED.config.mode = res.mode;
        applyPowerUI(res.power_on);
        if (typeof onModeChanged === 'function') onModeChanged(res.mode);
        toast(res.power_on ? 'LEDs on' : 'LEDs off', 'success');
        pollStatus();
    } catch (e) {
        LED.config.power_on = !next;
        applyPowerUI(!next);
        toast('Power toggle failed', 'error');
    } finally {
        if (btn) btn.disabled = false;
    }
}

/* ---------------------------------------------------------------- connection */

function applyConnectionUI(online, error) {
    const pill = document.getElementById('connPill');
    const text = document.getElementById('connText');
    if (!pill || !text) return;

    pill.classList.remove('online', 'offline');
    if (online === null || online === undefined) {
        text.textContent = 'Checking…';
    } else if (online) {
        pill.classList.add('online');
        text.textContent = 'Connected';
    } else {
        pill.classList.add('offline');
        text.textContent = 'Offline';
        if (error && !online) {
            pill.title = error;
        }
    }
}

/* ------------------------------------------------------------------- polling */

async function pollStatus() {
    try {
        const data = await apiGet('/api/stats');
        LED.config = data.config || {};
        LED.stats = data.stats || {};
        LED.frameStats = data.frame_stats || {};
        LED.perfState = data.perf_test_state || {};
        LED.deviceOnline = data.device_online;

        applyConnectionUI(data.device_online, data.device_error);
        applyPowerUI(LED.config.power_on !== false);

        if (typeof onStatusUpdate === 'function') onStatusUpdate(data);
    } catch (e) {
        applyConnectionUI(false, 'Backend unreachable');
        if (typeof onStatusUpdate === 'function') onStatusUpdate(null);
    }
}

/* --------------------------------------------------------------------- init */

document.addEventListener('DOMContentLoaded', async () => {
    // Remember this page so the desktop shortcut can reopen it next time
    const page = location.pathname.startsWith('/custom') ? 'custom'
        : location.pathname.startsWith('/settings') ? 'settings'
            : 'home';
    apiPost('/api/last-page', { page }).catch(() => {});

    const powerBtn = document.getElementById('powerBtn');
    if (powerBtn) powerBtn.addEventListener('click', togglePower);

    try {
        LED.config = await apiGet('/api/config');
        applyPowerUI(LED.config.power_on !== false);
        if (typeof onConfigLoaded === 'function') onConfigLoaded(LED.config);
    } catch (e) {
        toast('Could not load configuration', 'error');
    }

    pollStatus();
    setInterval(pollStatus, 700);
});
