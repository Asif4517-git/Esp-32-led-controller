/* ==========================================================================
   ESP 32 RGB Controller — Custom page

   Colour wheel + WLED effect browser. Every control applies immediately.

   Two WLED details this page handles properly:
   * Each effect has its own recommended palette and slider values in
     /json/fxdata. Those are applied when you pick an effect, exactly like
     WLED's own UI does, otherwise many effects look "broken".
   * Effects that use WLED's custom sliders (c1..c3) get a Custom section,
     because a value of 0 there can make an effect render nothing.
   ========================================================================== */

const Custom = {
    hsv: { h: 0, s: 1, v: 1 },     // h 0..360, s 0..1, v 0..1
    rgb: [255, 0, 0],
    brightness: 128,
    effect: 0,
    speed: 128,
    intensity: 128,
    palette: 0,
    c1: 128,
    c2: 128,
    c3: 16,
    effects: [],
    defaults: {},
    palettes: [],
    dragging: false,
};

/* ------------------------------------------------------------ colour maths */

function hsvToRgb(h, s, v) {
    const c = v * s;
    const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
    const m = v - c;
    let r = 0, g = 0, b = 0;
    if (h < 60)       { r = c; g = x; b = 0; }
    else if (h < 120) { r = x; g = c; b = 0; }
    else if (h < 180) { r = 0; g = c; b = x; }
    else if (h < 240) { r = 0; g = x; b = c; }
    else if (h < 300) { r = x; g = 0; b = c; }
    else              { r = c; g = 0; b = x; }
    return [
        Math.round((r + m) * 255),
        Math.round((g + m) * 255),
        Math.round((b + m) * 255),
    ];
}

function rgbToHex([r, g, b]) {
    return '#' + [r, g, b].map(v => v.toString(16).padStart(2, '0')).join('').toUpperCase();
}

function hexToRgb(hex) {
    const m = /^#?([0-9a-f]{6})$/i.exec(String(hex).trim());
    if (!m) return null;
    const n = parseInt(m[1], 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function rgbToHsv([r, g, b]) {
    const rr = r / 255, gg = g / 255, bb = b / 255;
    const max = Math.max(rr, gg, bb), min = Math.min(rr, gg, bb);
    const d = max - min;
    let h = 0;
    if (d !== 0) {
        if (max === rr) h = 60 * (((gg - bb) / d) % 6);
        else if (max === gg) h = 60 * ((bb - rr) / d + 2);
        else h = 60 * ((rr - gg) / d + 4);
    }
    if (h < 0) h += 360;
    return { h, s: max === 0 ? 0 : d / max, v: max };
}

/* --------------------------------------------------------------- wheel draw */

function drawWheel() {
    const canvas = document.getElementById('colorWheel');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const size = canvas.width;
    const r = size / 2;
    const img = ctx.createImageData(size, size);
    const data = img.data;

    for (let y = 0; y < size; y++) {
        for (let x = 0; x < size; x++) {
            const dx = x - r + 0.5;
            const dy = y - r + 0.5;
            const dist = Math.sqrt(dx * dx + dy * dy);
            const idx = (y * size + x) * 4;

            if (dist > r) {
                data[idx + 3] = 0;
                continue;
            }

            let angle = Math.atan2(dy, dx) * 180 / Math.PI;
            if (angle < 0) angle += 360;

            const sat = Math.min(1, dist / r);
            const [rr, gg, bb] = hsvToRgb(angle, sat, 1);
            const edge = Math.min(1, (r - dist) / 2);

            data[idx] = rr;
            data[idx + 1] = gg;
            data[idx + 2] = bb;
            data[idx + 3] = 255 * edge;
        }
    }
    ctx.putImageData(img, 0, 0);
}

function updateCursor() {
    const cursor = document.getElementById('wheelCursor');
    const canvas = document.getElementById('colorWheel');
    if (!cursor || !canvas) return;
    const r = canvas.width / 2;
    const ang = Custom.hsv.h * Math.PI / 180;
    const dist = Custom.hsv.s * r;
    cursor.style.left = `${r + Math.cos(ang) * dist}px`;
    cursor.style.top = `${r + Math.sin(ang) * dist}px`;
}

/* ------------------------------------------------------------- apply colour */

let colorPushTimer = null;

function applyColor({ immediate = false, resetPalette = false } = {}) {
    Custom.rgb = hsvToRgb(Custom.hsv.h, Custom.hsv.s, Custom.hsv.v);
    const hex = rgbToHex(Custom.rgb);

    const hexInput = document.getElementById('hexInput');
    if (hexInput && document.activeElement !== hexInput) hexInput.value = hex;

    const chip = document.getElementById('colorChip');
    if (chip) chip.style.background = hex;

    updateCursor();

    const push = () => {
        const patch = {
            custom_color: Custom.rgb,
            custom_brightness: Custom.brightness,
            mode: 'custom',
            power_on: true,
        };

        // WLED palettes recolour most effects and take precedence over the
        // base colour. Touching the wheel means "use my colour", so drop back
        // to the Default palette or the change would look like it did nothing.
        if (resetPalette && Custom.effect !== 0 && Custom.palette !== 0) {
            Custom.palette = 0;
            patch.custom_palette = 0;
            highlightPalette(0);
            toast('Palette set to Default so your colour is visible', 'success');
        }

        setConfig(patch);
    };
    if (colorPushTimer) clearTimeout(colorPushTimer);
    if (immediate) push();
    else colorPushTimer = setTimeout(push, 60);
}

/* --------------------------------------------------------------- wheel input */

function wheelPointer(e) {
    const canvas = document.getElementById('colorWheel');
    const rect = canvas.getBoundingClientRect();
    const x = (e.clientX ?? e.touches?.[0]?.clientX) - rect.left;
    const y = (e.clientY ?? e.touches?.[0]?.clientY) - rect.top;
    const r = rect.width / 2;

    let dx = x - r;
    let dy = y - r;
    let dist = Math.sqrt(dx * dx + dy * dy);

    if (dist > r) {
        dx = dx / dist * r;
        dy = dy / dist * r;
        dist = r;
    }

    let angle = Math.atan2(dy, dx) * 180 / Math.PI;
    if (angle < 0) angle += 360;

    Custom.hsv.h = angle;
    Custom.hsv.s = Math.min(1, dist / r);
    applyColor({ resetPalette: true });
}

/* ------------------------------------------------------------------ effects */

function renderEffects(filter = '') {
    const list = document.getElementById('effectList');
    if (!list) return;
    const q = filter.trim().toLowerCase();

    const items = Custom.effects
        .map((name, i) => ({ name, i }))
        .filter(e => !q || e.name.toLowerCase().includes(q));

    if (items.length === 0) {
        list.innerHTML = '<div class="effect-loading">No matching effects</div>';
        return;
    }

    list.innerHTML = '';
    items.forEach(({ name, i }) => {
        const btn = document.createElement('button');
        btn.className = 'effect-item' + (i === Custom.effect ? ' active' : '');
        btn.dataset.id = i;
        btn.appendChild(document.createTextNode(name));

        // Hovering an effect shows the values it will start from.
        const def = effectDefaults(i);
        const reco = [];
        if (def.sx !== undefined) reco.push(`speed ${def.sx}`);
        if (def.ix !== undefined) reco.push(`intensity ${def.ix}`);
        btn.title = reco.length ? `${name} — recommended ${reco.join(', ')}` : name;

        btn.addEventListener('click', () => applyEffect(i, name));
        list.appendChild(btn);
    });
}

function highlightEffect(index) {
    document.querySelectorAll('.effect-item').forEach(el =>
        el.classList.toggle('active', Number(el.dataset.id) === index));
}

/**
 * Selecting an effect also applies WLED's recommended settings for it:
 * palette, speed, intensity and (when the effect uses them) c1..c3.
 * Everything stays adjustable afterwards.
 */
function applyEffect(index, name) {
    Custom.effect = index;
    highlightEffect(index);

    const def = Custom.defaults[String(index)] || {};
    const patch = { custom_effect: index, mode: 'custom', power_on: true };

    if (def.pal !== undefined) {
        Custom.palette = def.pal;
        patch.custom_palette = def.pal;
        highlightPalette(def.pal);
    }
    if (def.sx !== undefined) {
        Custom.speed = def.sx;
        patch.custom_effect_speed = def.sx;
        syncSpeed();
    }
    if (def.ix !== undefined) {
        Custom.intensity = def.ix;
        patch.custom_effect_intensity = def.ix;
        syncIntensity();
    }

    let usesCustom = false;
    ['c1', 'c2', 'c3'].forEach(key => {
        if (def[key] !== undefined) {
            Custom[key] = def[key];
            patch[`custom_${key}`] = def[key];
            usesCustom = true;
        }
    });
    syncCustom(usesCustom);
    syncRecommendation();

    setConfig(patch);
    toast(`Effect: ${name}`, 'success');
}

/* ---------------------------------------------------- recommended settings */

/**
 * WLED ships a recommended speed/intensity (and custom-slider values) for
 * many effects. Selecting an effect applies them automatically; the chips on
 * the sliders and the "Apply recommended look" button restore them after the
 * controls have been moved.
 */
function effectDefaults(index) {
    return Custom.defaults[String(index)] || {};
}

function effectLabel(index) {
    return Custom.effects[index] ?? `effect #${index}`;
}

function updateRecommendation() {
    const btn = document.getElementById('recommendBtn');
    const hint = document.getElementById('recommendHint');
    if (!btn || !hint) return;

    const def = effectDefaults(Custom.effect);
    const parts = [];
    if (def.sx !== undefined) parts.push(`speed ${def.sx}`);
    if (def.ix !== undefined) parts.push(`intensity ${def.ix}`);
    if (def.pal !== undefined) parts.push(`palette #${def.pal}`);
    ['c1', 'c2', 'c3'].forEach(key => {
        if (def[key] !== undefined) parts.push(`${key.toUpperCase()} ${def[key]}`);
    });

    const has = parts.length > 0;
    btn.disabled = !has;
    hint.textContent = has
        ? `${effectLabel(Custom.effect)}: ${parts.join(' · ')}`
        : 'This effect has no recommended values — use the sliders.';
}

/**
 * One chip per slider. It shows the effect's recommended value and puts that
 * value back with a single click; once the slider matches, the chip is ticked
 * and disabled.
 */
function updateRecoChip(kind, recommended, current) {
    const btn = document.getElementById(kind + 'Reco');
    const value = document.getElementById(kind + 'RecoValue');
    if (!btn || !value) return;

    if (recommended === undefined || recommended === null) {
        btn.hidden = true;
        return;
    }

    btn.hidden = false;
    const atRecommended = Number(current) === Number(recommended);
    btn.classList.toggle('active', atRecommended);
    btn.setAttribute('aria-pressed', String(atRecommended));
    value.textContent = atRecommended ? `${recommended} ✓` : recommended;
    btn.title = atRecommended
        ? `${recommended} is WLED's recommended ${kind} for ${effectLabel(Custom.effect)} — click to re-apply`
        : `Set ${kind} to ${recommended}, WLED's recommended value for ${effectLabel(Custom.effect)}`;
}

function syncRecommendation() {
    const def = effectDefaults(Custom.effect);
    updateRecoChip('speed', def.sx, Custom.speed);
    updateRecoChip('intensity', def.ix, Custom.intensity);
    updateRecommendation();
}

function applyRecommendedSpeed() {
    const def = effectDefaults(Custom.effect);
    if (def.sx === undefined) return;
    Custom.speed = def.sx;
    syncSpeed();
    syncRecommendation();
    setConfig({ custom_effect_speed: Custom.speed, mode: 'custom', power_on: true });
    toast(`Speed set to ${def.sx}`, 'success');
}

function applyRecommendedIntensity() {
    const def = effectDefaults(Custom.effect);
    if (def.ix === undefined) return;
    Custom.intensity = def.ix;
    syncIntensity();
    syncRecommendation();
    setConfig({ custom_effect_intensity: Custom.intensity, mode: 'custom', power_on: true });
    toast(`Intensity set to ${def.ix}`, 'success');
}

function applyRecommended() {
    const def = effectDefaults(Custom.effect);
    const patch = { mode: 'custom', power_on: true };
    const parts = [];

    if (def.pal !== undefined) {
        Custom.palette = def.pal;
        patch.custom_palette = def.pal;
        highlightPalette(def.pal);
        parts.push(`palette #${def.pal}`);
    }
    if (def.sx !== undefined) {
        Custom.speed = def.sx;
        patch.custom_effect_speed = def.sx;
        parts.push(`speed ${def.sx}`);
    }
    if (def.ix !== undefined) {
        Custom.intensity = def.ix;
        patch.custom_effect_intensity = def.ix;
        parts.push(`intensity ${def.ix}`);
    }

    let usesCustom = false;
    ['c1', 'c2', 'c3'].forEach(key => {
        if (def[key] !== undefined) {
            Custom[key] = def[key];
            patch[`custom_${key}`] = def[key];
            usesCustom = true;
        }
    });

    if (parts.length === 0) return;

    syncSpeed();
    syncIntensity();
    if (usesCustom) syncCustom(true);
    syncRecommendation();

    setConfig(patch);
    toast(`Recommended look applied — ${parts.join(', ')}`, 'success');
}

async function loadEffects() {
    try {
        const data = await apiGet('/api/effects');
        Custom.effects = data.effects || [];
        Custom.defaults = data.defaults || {};
        Custom.effect = data.current_effect ?? 0;
        Custom.palette = data.palette ?? 0;
        Custom.speed = data.speed ?? 128;
        Custom.intensity = data.intensity ?? 128;
        Custom.c1 = data.c1 ?? 128;
        Custom.c2 = data.c2 ?? 128;
        Custom.c3 = data.c3 ?? 16;

        renderEffects(document.getElementById('effectSearch')?.value || '');
        syncSpeed();
        syncIntensity();
        syncCustom(usesCustomSliders(Custom.effect));
        syncRecommendation();
        highlightPalette(Custom.palette);
    } catch (e) {
        document.getElementById('effectList').innerHTML =
            '<div class="effect-loading">Could not read effects from WLED</div>';
    }
}

/* ----------------------------------------------------------------- palettes */

/**
 * WLED's own palette list, with the same gradient bars the WLED UI draws.
 * The value stored in the config is the WLED palette index.
 */
async function loadPalettes() {
    const list = document.getElementById('paletteList');
    try {
        const data = await apiGet('/api/palettes');
        Custom.palettes = data.items || [];
        renderPalettes();
    } catch (e) {
        if (list) list.innerHTML = '<div class="effect-loading">Could not read palettes</div>';
    }
}

function renderPalettes() {
    const list = document.getElementById('paletteList');
    if (!list) return;
    list.innerHTML = '';

    Custom.palettes.forEach(entry => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'palette-item' + (entry.index === Custom.palette ? ' active' : '');
        btn.dataset.index = entry.index;
        btn.title = entry.name;

        const label = document.createElement('span');
        label.className = 'palette-name';
        label.textContent = entry.name;
        const bar = document.createElement('span');
        bar.className = 'palette-bar';
        bar.style.background = entry.gradient;

        btn.appendChild(label);
        btn.appendChild(bar);

        btn.addEventListener('click', () => {
            Custom.palette = entry.index;
            highlightPalette(entry.index);
            setConfig({ custom_palette: entry.index, mode: 'custom', power_on: true });
        });
        list.appendChild(btn);
    });
}

function highlightPalette(index) {
    document.querySelectorAll('#paletteList .palette-item').forEach(el => {
        el.classList.toggle('active', Number(el.dataset.index) === index);
    });
    const active = document.querySelector('#paletteList .palette-item.active');
    if (active && typeof active.scrollIntoView === 'function') {
        active.scrollIntoView({ block: 'nearest' });
    }
}

/* -------------------------------------------------------------- slider sync */

function syncSpeed() {
    const el = document.getElementById('speedSlider');
    const label = document.getElementById('speedValue');
    if (el) el.value = Custom.speed;
    if (label) label.textContent = Custom.speed;
}

function syncIntensity() {
    const el = document.getElementById('intensitySlider');
    const label = document.getElementById('intensityValue');
    if (el) el.value = Custom.intensity;
    if (label) label.textContent = Custom.intensity;
}

function syncCustom(visible) {
    const box = document.getElementById('customSliders');
    if (box) box.classList.toggle('hidden', !visible);
    ['c1', 'c2', 'c3'].forEach(key => {
        const el = document.getElementById(key + 'Slider');
        const label = document.getElementById(key + 'Value');
        if (el) el.value = Custom[key];
        if (label) label.textContent = Custom[key];
    });
}

function usesCustomSliders(index) {
    const def = effectDefaults(index);
    return ['c1', 'c2', 'c3'].some(key => def[key] !== undefined);
}

/* --------------------------------------------------------------------- init */

function onConfigLoaded(cfg) {
    Custom.brightness = cfg.custom_brightness ?? 128;
    Custom.effect = cfg.custom_effect ?? 0;
    Custom.speed = cfg.custom_effect_speed ?? 128;
    Custom.intensity = cfg.custom_effect_intensity ?? 128;
    Custom.palette = cfg.custom_palette ?? 0;
    Custom.c1 = cfg.custom_c1 ?? 128;
    Custom.c2 = cfg.custom_c2 ?? 128;
    Custom.c3 = cfg.custom_c3 ?? 16;

    if (Array.isArray(cfg.custom_color)) {
        Custom.rgb = cfg.custom_color.slice();
        Custom.hsv = rgbToHsv(Custom.rgb);
    }

    document.getElementById('briSlider').value = Custom.brightness;
    document.getElementById('briValue').textContent = Custom.brightness;
    syncSpeed();
    syncIntensity();
    syncCustom(false);
    syncRecommendation();

    drawWheel();
    updateCursor();

    const hexInput = document.getElementById('hexInput');
    if (hexInput) hexInput.value = rgbToHex(Custom.rgb);
    const chip = document.getElementById('colorChip');
    if (chip) chip.style.background = rgbToHex(Custom.rgb);
}

function buildSwatches() {
    const colors = ['#FF0000', '#FF8000', '#FFD400', '#FFFFFF', '#000000',
                    '#FF00FF', '#0000FF', '#00FFFF', '#00FF00', '#FF0066'];
    const box = document.getElementById('swatches');
    box.innerHTML = '';
    colors.forEach(hex => {
        const b = document.createElement('button');
        b.className = 'swatch';
        b.style.background = hex;
        b.title = hex;
        b.addEventListener('click', () => {
            const rgb = hexToRgb(hex);
            if (!rgb) return;
            Custom.hsv = rgbToHsv(rgb);
            applyColor({ immediate: true, resetPalette: true });
        });
        box.appendChild(b);
    });
}

document.addEventListener('DOMContentLoaded', () => {
    buildSwatches();

    const canvas = document.getElementById('colorWheel');
    canvas.addEventListener('mousedown', e => { Custom.dragging = true; wheelPointer(e); });
    window.addEventListener('mousemove', e => { if (Custom.dragging) wheelPointer(e); });
    window.addEventListener('mouseup', () => { Custom.dragging = false; });
    canvas.addEventListener('touchstart', e => { e.preventDefault(); wheelPointer(e); }, { passive: false });
    canvas.addEventListener('touchmove', e => { e.preventDefault(); wheelPointer(e); }, { passive: false });

    document.getElementById('hexInput').addEventListener('change', e => {
        const rgb = hexToRgb(e.target.value);
        if (!rgb) { toast('Use a hex value like #FF0000', 'error'); return; }
        Custom.hsv = rgbToHsv(rgb);
        applyColor({ immediate: true, resetPalette: true });
    });

    document.getElementById('briSlider').addEventListener('input', e => {
        Custom.brightness = Number(e.target.value);
        document.getElementById('briValue').textContent = Custom.brightness;
        applyColor();
    });

    document.getElementById('speedSlider').addEventListener('input', e => {
        Custom.speed = Number(e.target.value);
        document.getElementById('speedValue').textContent = Custom.speed;
        syncRecommendation();
        setConfig({ custom_effect_speed: Custom.speed });
    });

    document.getElementById('intensitySlider').addEventListener('input', e => {
        Custom.intensity = Number(e.target.value);
        document.getElementById('intensityValue').textContent = Custom.intensity;
        syncRecommendation();
        setConfig({ custom_effect_intensity: Custom.intensity });
    });

    ['c1', 'c2', 'c3'].forEach(key => {
        const el = document.getElementById(key + 'Slider');
        if (!el) return;
        el.addEventListener('input', e => {
            Custom[key] = Number(e.target.value);
            document.getElementById(key + 'Value').textContent = Custom[key];
            setConfig({ ['custom_' + key]: Custom[key] });
        });
    });

    document.getElementById('effectSearch').addEventListener('input', e => {
        renderEffects(e.target.value);
    });

    const recommendBtn = document.getElementById('recommendBtn');
    if (recommendBtn) recommendBtn.addEventListener('click', applyRecommended);

    const speedReco = document.getElementById('speedReco');
    if (speedReco) speedReco.addEventListener('click', applyRecommendedSpeed);

    const intensityReco = document.getElementById('intensityReco');
    if (intensityReco) intensityReco.addEventListener('click', applyRecommendedIntensity);

    loadEffects();
    loadPalettes();
});

function onStatusUpdate(data) {
    if (!data) return;
    if (data.config?.mode !== 'custom') return;
    if (data.preview_colors?.length) {
        const box = document.getElementById('ledPreview');
        if (box && box.dataset.count !== String(data.preview_colors.length)) {
            box.innerHTML = '';
            data.preview_colors.forEach(() => {
                const d = document.createElement('div');
                d.className = 'led-dot';
                box.appendChild(d);
            });
            box.dataset.count = String(data.preview_colors.length);
        }
        document.querySelectorAll('#ledPreview .led-dot').forEach((dot, i) => {
            const c = data.preview_colors[i];
            if (!c) return;
            dot.style.background = `rgb(${c[0]},${c[1]},${c[2]})`;
            dot.style.boxShadow = `0 0 14px rgba(${c[0]},${c[1]},${c[2]},0.7)`;
        });
    }
}
