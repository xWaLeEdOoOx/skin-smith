"""
title: Skin-Smith
author: Waleed Kh. Tolbah (AKA xWaLeEdOoOx)
description: a lightweight Open WebUI plugin that extracts TDP theme CSS and loads it as custom CSS.
required_open_webui_version: 0.12.0
version: 0.8.2
"""

# Portions of this file (the converter math, preview engine, and shared
# asset-registry contract) are ported verbatim from Theme Designer Pro:
# https://github.com/silentoplayz/theme-designer-pro-presets
# Copyright (c) 2026 silentoplayz — MIT License:
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

import hashlib
import re
from pathlib import Path
from typing import Any

# ===========================================================================
# Shared static-asset registry
# --- KEEP BYTE-IDENTICAL IN EVERY PLUGIN THAT USES IT ----------------------
# ---------------------------------------------------------------------------
# Each plugin publishes a fragment into one app.state registry and the route
# composes them per request, so load order is irrelevant. Per-process: each
# container serves what it has loaded.
#
# Contract:
#   * ASSET_REGISTRY_ATTR, ASSET_ROUTE_ATTR, the entry shape and the paths are
#     the interop surface. The route owner's copy serves everyone.
#   * Fragments are keyed by the plugin's module name (ASSET_KEY), which Open
#     WebUI keeps unique per install.
#   * Lower `order` composes first (default 0).
#   * Producers run on the event loop for every request, 304s included: return
#     a prebuilt string, no I/O, no locks.
#   * Return "" to withdraw. A deleted plugin never sees its own deletion; any
#     other plugin calling asset_withdraw_deleted() drops its fragments.
#   * Inline fragments only: <script src> / @import lands after hydration.
# ===========================================================================
LOADER_PATH = "/static/loader.js"
CUSTOM_CSS_PATH = "/static/custom.css"
SHARED_ASSET_TYPES = {
    LOADER_PATH: "application/javascript; charset=utf-8",
    CUSTOM_CSS_PATH: "text/css; charset=utf-8",
}
ASSET_REGISTRY_ATTR = "_owui_static_fragments"  # {path: {key: entry}}
ASSET_ROUTE_ATTR = "_owui_shared_asset"  # set to the path the route serves
ASSET_IMPL_ATTR = "_owui_shared_asset_impl"  # implementation version of the route
# Bump on any behaviour change: the newest copy takes over the route.
ASSET_IMPL_VERSION = 5
# "function_<id>" or "tool_<id>"; newer Open WebUI adds a random "_<hex>".
ASSET_KEY = re.sub(r"^((?:function|tool)_.+?)_[0-9a-f]{32}$", r"\1", __name__)

# Warn once per (path, key, exception type): compose runs on every page load.
_ASSET_WARNED: set = set()


def asset_fragments(app: Any, path: str) -> dict:
    registry = getattr(app.state, ASSET_REGISTRY_ATTR, None)
    if not isinstance(registry, dict):
        registry = {}
        app.state.__setattr__(ASSET_REGISTRY_ATTR, registry)
    bucket = registry.get(path)
    if not isinstance(bucket, dict):
        bucket = {}
        registry[path] = bucket
    return bucket


def asset_sort_key(item):
    """(order, key); a non-int order falls back to 0 so it cannot break sorted()."""
    key, entry = item
    try:
        order = int(entry.get("order", 0))
    except (TypeError, ValueError):
        order = 0
    return (order, key)


def asset_withdraw_deleted(app: Any, event: Any, event_name: Any) -> None:
    """Drop a deleted plugin's fragments; call it from your event handler."""
    if event_name not in ("function.deleted", "tool.deleted"):
        return
    kind = event_name.split(".")[0]
    subject_id = ((event or {}).get("subject") or {}).get("id")
    for bucket in getattr(app.state, ASSET_REGISTRY_ATTR, {}).values():
        bucket.pop(f"{kind}_{subject_id}", None)


def asset_strip_block(content: str, start_marker: str, end_marker: str) -> str:
    """Remove every marker-wrapped block, leaving other content untouched."""
    while start_marker in content:
        start = content.find(start_marker)
        end = content.find(end_marker, start)
        if end == -1:
            # No end marker: the block was appended last, so drop to EOF.
            content = content[:start]
            break
        end += len(end_marker)
        if content[end : end + 1] == "\n":
            end += 1
        content = content[:start] + content[end:]
    return content


def asset_compose(app: Any, path: str) -> str:
    """Disk file plus every registered fragment, in (order, key) order."""
    import logging

    try:
        from open_webui.env import STATIC_DIR

        target = Path(STATIC_DIR) / path.rsplit("/", 1)[-1]
        body = (
            ""
            if (target.is_symlink() or not target.is_file())
            else target.read_text(encoding="utf-8")
        )
    except Exception:
        body = ""

    ordered = sorted(asset_fragments(app, path).items(), key=asset_sort_key)
    # A bare newline lets one fragment run into the next: "a\n(b)" is a call.
    separator = "\n;\n" if path == LOADER_PATH else "\n\n"
    # Strip first: an older file-writing build may have left a block on disk.
    for _key, entry in ordered:
        body = asset_strip_block(body, entry["start"], entry["end"])
    body = body.rstrip()

    for key, entry in ordered:
        try:
            block = (entry["js"]() or "").strip()
        except Exception as exc:
            mark = (path, key, type(exc).__name__)
            if mark not in _ASSET_WARNED:
                if len(_ASSET_WARNED) > 256:
                    _ASSET_WARNED.clear()
                _ASSET_WARNED.add(mark)
                logging.getLogger("owui-shared-assets").warning(
                    "fragment %r failed for %s - it will be omitted",
                    key,
                    path,
                    exc_info=True,
                )
            continue
        if block:
            body = (body + separator if body else "") + block
    return body + "\n" if body else ""


def asset_register(
    app: Any, path: str, start: str, end: str, producer, order: int = 0
) -> None:
    """Publish a fragment and ensure the route exists. Idempotent."""
    from starlette.responses import Response
    from starlette.routing import Mount, Route

    asset_fragments(app, path)[ASSET_KEY] = {
        "start": start,
        "end": end,
        "js": producer,
        "order": order,
    }

    for existing in app.routes:
        if getattr(existing, ASSET_ROUTE_ATTR, None) != path:
            continue
        if getattr(existing, ASSET_IMPL_ATTR, 0) >= ASSET_IMPL_VERSION:
            return  # an equal or newer implementation already owns the route
        break  # ours is newer - fall through and replace it

    # Fragments live on app.state, so replacing the route loses nothing.
    app.routes[:] = [r for r in app.routes if getattr(r, "path", "") != path]
    media_type = SHARED_ASSET_TYPES.get(path, "text/plain; charset=utf-8")

    async def serve_asset(request):
        content = asset_compose(app, path)
        etag = (
            '"owui-'
            # Cache validator only; a bare md5() raises on FIPS hosts.
            + hashlib.md5(
                (path + "\x00" + content).encode("utf-8"), usedforsecurity=False
            ).hexdigest()
            + '"'
        )
        # no-cache, not no-store: no-store would disable the 304 below.
        headers = {
            "Cache-Control": "no-cache, must-revalidate, private",
            "ETag": etag,
        }
        # Gzipping proxies send the tag back weakened to W/"...".
        if request.headers.get("if-none-match", "").removeprefix("W/") == etag:
            return Response(status_code=304, headers=headers)
        # Starlette only adds charset for text/*.
        return Response(content, media_type=media_type, headers=headers)

    insert_at = len(app.routes)
    for position, existing in enumerate(app.routes):
        if isinstance(existing, Mount) and getattr(existing, "name", "") == "static":
            insert_at = position
            break
    shared = Route(path, serve_asset, methods=["GET"])
    setattr(shared, ASSET_ROUTE_ATTR, path)
    setattr(shared, ASSET_IMPL_ATTR, ASSET_IMPL_VERSION)
    app.routes.insert(insert_at, shared)


# =========================== end shared asset block ========================


import base64
from starlette.responses import HTMLResponse, Response

CONVERTER_SRC = r"""/* ============================================================
   Open WebUI Theme Converter — engine
   TDP theme JSON -> Open WebUI Custom CSS

   themeVars(), adjustHexIntensity(), gradientLayers(),
   accentColor(), STEPS, LIGHTNESS_MAP, ACCENTS are ported
   VERBATIM from silentoplayz/theme-designer-pro-presets
   (docs/assets/preview.js). Do not alter their math.
   ============================================================ */

(function () {
  'use strict';

  // ---- Verbatim palette math (preview.js) ----

  const STEPS = [50, 100, 200, 300, 400, 500, 600, 700, 800, 850, 900, 950];
  const LIGHTNESS_MAP = {
    50: 0.98, 100: 0.94, 200: 0.92, 300: 0.85, 400: 0.77, 500: 0.69,
    600: 0.51, 700: 0.42, 800: 0.32, 850: 0.27, 900: 0.2, 950: 0.16,
  };

  // The accents a theme's CSS reads: [mode field, custom property, hue shift]
  const ACCENTS = [['accent', '--tdp-accent', 0], ['accent2', '--tdp-accent-2', 40], ['accent3', '--tdp-accent-3', -40]];

  // The designer's generated accent (generatedAccent), c in thousandths
  function accentColor(h, c, dark, shift) {
    const ac = c > 0 ? Math.min(0.18, Math.max(0.06, c / 250)) : 0;
    return `oklch(${dark ? 0.74 : 0.56} ${ac.toFixed(3)} ${((h + shift + 360) % 360).toFixed(1)})`;
  }

  function themeVars(mode, modeKey) {
    const vars = {};
    const paletteOn = !mode || mode.paletteEnabled !== false;
    const h = mode && mode.h != null ? mode.h : 250;
    const c = paletteOn && mode ? (mode.c || 0) / 1000 : 0;
    const l = mode && mode.l != null ? mode.l / 100 : 0.2;
    const deltaL = paletteOn ? l - 0.2 : 0;
    const ov = (paletteOn && mode && mode.overrides) || {};

    vars['--tdp-hue'] = String(h);
    vars['--tdp-chroma'] = String(paletteOn && mode ? (mode.c || 0) : 0);
    STEPS.forEach((step) => {
      const targetL = Math.max(0, Math.min(0.98, LIGHTNESS_MAP[step] + deltaL));
      const computed = `oklch(${targetL.toFixed(3)} ${c.toFixed(3)} ${c === 0 ? 0 : h})`;
      vars[`--color-gray-${step}`] = ov[`--color-gray-${step}`] || computed;
    });

    const dark = modeKey !== 'light' && modeKey !== 'her';
    ACCENTS.forEach(([key, name, shift]) => {
      const set = mode && typeof mode[key] === 'string' && /^#[0-9a-fA-F]{6}$/.test(mode[key]) ? mode[key].toLowerCase() : '';
      vars[name] = set || accentColor(Number(h) || 0, paletteOn && mode ? Number(mode.c) || 0 : 0, dark, shift);
    });

    // Manual variable overrides win over the generated palette
    if (mode && mode.manualOverridesEnabled !== false && typeof mode.manualOverrides === 'string') {
      mode.manualOverrides.split('\n').forEach((line) => {
        const t = line.trim();
        if (t.startsWith('--') && t.includes(':') && !/[{}]/.test(t)) {
          const idx = t.indexOf(':');
          vars[t.slice(0, idx).trim()] = t.slice(idx + 1).replace(/;\s*$/, '').trim();
        }
      });
    }
    return vars;
  }

  function adjustHexIntensity(hex, intensity) {
    hex = String(hex || '').replace('#', '');
    if (hex.length === 3) hex = hex.split('').map((ch) => ch + ch).join('');
    if (!/^[0-9a-fA-F]{6}$/.test(hex)) return '#' + (hex || '000000');
    const factor = intensity / 100;
    const rgb = [0, 2, 4].map((i) => {
      const v = parseInt(hex.slice(i, i + 2), 16);
      return Math.max(0, Math.min(255, Math.round(128 + (v - 128) * factor)));
    });
    return '#' + rgb.map((v) => v.toString(16).padStart(2, '0')).join('');
  }

  function firstOf() {
    for (const v of arguments) if (v != null) return v;
  }

  // Accepts a full gradient preset object (raw file) or a theme mode's gradient* fields
  function gradientLayers(g) {
    const type = g.type || g.gradientType || 'linear';
    const intensity = firstOf(g.intensity, g.gradientIntensity, 85);
    const animated = !!(g.animation || g.gradientAnimation || g.animated);
    const speed = firstOf(g.speed, g.gradientAnimationSpeed, 8);

    if (type === 'mesh') {
      const points = g.meshPoints || g.gradientMeshPoints || [];
      if (points.length < 2) return null;
      const bg = g.meshBgColor || g.gradientMeshBgColor || '#0a0a12';
      const layers = points
        .map((p) => `radial-gradient(at ${p.x}% ${p.y}%, ${adjustHexIntensity(p.color, intensity)} 0%, transparent ${p.spread}%)`)
        .join(', ');
      return { background: bg, backgroundImage: layers, animated, speed };
    }

    const stops = g.stops || g.gradientStops || [];
    if (stops.length < 2) return null;
    const stopsStr = [...stops]
      .sort((a, b) => (a.position || 0) - (b.position || 0))
      .map((s) => `${adjustHexIntensity(s.color, intensity)} ${s.position || 0}%`)
      .join(', ');

    let fn;
    if (type === 'radial') {
      const shape = firstOf(g.radialShape, g.gradientRadialShape, 'ellipse');
      const size = firstOf(g.radialSize, g.gradientRadialSize, 'farthest-corner');
      const x = firstOf(g.radialPosX, g.gradientRadialPosX, 50);
      const y = firstOf(g.radialPosY, g.gradientRadialPosY, 50);
      fn = `radial-gradient(${shape} ${size} at ${x}% ${y}%, ${stopsStr})`;
    } else {
      const angle = firstOf(g.angle, g.gradientAngle, 135);
      fn = `linear-gradient(${angle}deg, ${stopsStr})`;
    }
    return { background: null, backgroundImage: fn, animated, speed };
  }

  // ---- Mode resolution (mirrors studio.js readMode) ----

  const MODES = ['dark', 'light', 'oled', 'her'];

  function clone(o) { return JSON.parse(JSON.stringify(o)); }

  function readMode(theme, k) {
    const from = theme[k]
      || (k === 'oled' ? Object.assign(clone(theme.dark || {}), { l: 0 }) : k === 'her' ? theme.light || theme.dark : null)
      || {};
    const m = Object.assign({}, from);
    // manualOverrides text present with no manualOverridesEnabled = treat as enabled
    if (m.manualOverridesEnabled === undefined && typeof m.manualOverrides === 'string' && m.manualOverrides.trim()) {
      m.manualOverridesEnabled = true;
    }
    // An accent is '#rrggbb', stored lowercase, or '' for the generated one
    ['accent', 'accent2', 'accent3'].forEach((f) => {
      m[f] = typeof m[f] === 'string' && /^#[0-9a-fA-F]{6}$/.test(m[f]) ? m[f].toLowerCase() : '';
    });
    return m;
  }

  // ---- CSS assembly ----

  // Open WebUI marks the theme with CLASSES on <html>, never data-theme:
  // app.html + Settings/General.svelte applyTheme():
  //   dark/system-dark -> .dark; light/system-light -> .light;
  //   her -> .her (and .light after a settings change);
  //   oled-dark -> .dark + inline true-black --color-gray-800..950 set
  //   directly on <html>'s style attribute (same order in both files).
  // The inline marker is OLED's fingerprint: plain .dark must NOT get the
  // OLED block (its !important blacks would leak), so OLED is scoped to the
  // style attribute, not to .dark.
  const SELECTORS = {
    dark: '.dark',
    light: '.light',
    her: '.her',
    oled: "html[style*='--color-gray-800: #101010']",
  };

  // OLED renders on true black in Open WebUI (Settings/General.svelte L182-186)
  const OLED_OVERRIDE = {
    '--color-gray-800': '#101010',
    '--color-gray-850': '#050505',
    '--color-gray-900': '#000000',
    '--color-gray-950': '#000000',
  };

  function varsBlock(selector, vars) {
    // !important beats Open WebUI's inline --color-gray-* set on
    // document.documentElement by app.html / Settings General.svelte applyTheme().
    const lines = Object.entries(vars).map(([k, v]) => `  ${k}: ${v} !important;`);
    return `${selector} {\n${lines.join('\n')}\n}`;
  }

  // Gradient block, mirroring buildMockSrcdoc's gradient section
  function gradientBlock(g) {
    const layers = gradientLayers(g);
    if (!layers) return '';
    const bg = layers.background ? `background-color: ${layers.background} !important;` : '';
    const anim = layers.animated
      ? `background-size: 300% 300% !important; animation: tdp-gradient-shift ${layers.speed}s ease infinite;`
      : '';
    let css = `body { ${bg} background-image: ${layers.backgroundImage} !important; background-attachment: fixed !important; ${anim} }`;
    if (layers.animated) {
      css += `\n@keyframes tdp-gradient-shift { 0% { background-position: 0% 50%; } 50% { background-position: 100% 50%; } 100% { background-position: 0% 50%; } }`;
    }
    return css;
  }

  // Full convert: theme JSON object -> one CSS string for Settings -> Interface -> Custom CSS
  function convert(theme) {
    if (typeof theme === 'string') theme = JSON.parse(theme);
    const parts = [];
    const name = (theme && theme.name) || 'Untitled theme';

    parts.push(`/* ${name} — Open WebUI Custom CSS`);
    parts.push(`   Generated by TDP Theme Converter (CSS only: palette, accents,`);
    parts.push(`   manual overrides, gradient, custom CSS).`);
    parts.push(`   Paste into Settings -> Interface -> Custom CSS. */`);

    // Mode blocks: palette + accents + manual overrides, scoped to Open WebUI's selectors
    MODES.forEach((k) => {
      const mode = readMode(theme, k);
      const vars = themeVars(mode, k);
      if (k === 'oled') Object.assign(vars, OLED_OVERRIDE);
      parts.push('');
      parts.push(`/* ${k.toUpperCase()} */`);
      parts.push(varsBlock(SELECTORS[k], vars));
    });

    // Gradient: the theme's gradient config, emitted once on body.
    // A mode's gradient* fields count when gradientEnabled is set; the first
    // enabled mode wins (dark first, as the designer applies it).
    let grad = null;
    for (const k of MODES) {
      const mode = readMode(theme, k);
      if (mode.gradientEnabled) { grad = gradientBlock(mode); break; }
    }
    if (grad) {
      parts.push('');
      parts.push('/* Gradient background */');
      parts.push(grad);
    }

    // Custom CSS: each mode's block, scoped to its selector, passed through as-is
    MODES.forEach((k) => {
      const mode = readMode(theme, k);
      if (mode.customCssEnabled && typeof mode.customCSS === 'string' && mode.customCSS.trim()) {
        parts.push('');
        parts.push(`/* Custom CSS (${k}) */`);
        parts.push(`${SELECTORS[k]} {\n${mode.customCSS.trim()}\n}`);
      }
    });

    return parts.join('\n');
  }

  // Preview feed: {modeKey, vars, customCSS, gradient, transparent} for buildMockSrcdoc
  function previewFeed(theme, modeKey) {
    const mode = readMode(theme, modeKey);
    const vars = themeVars(mode, modeKey);
    if (modeKey === 'oled') Object.assign(vars, OLED_OVERRIDE);
    const gradient = mode.gradientEnabled ? gradientLayers(mode) : null;
    const customCSS = mode.customCssEnabled && typeof mode.customCSS === 'string' ? mode.customCSS : '';
    return { modeKey, vars, customCSS, gradient, transparent: false };
  }

  window.TDPConverter = {
    STEPS, LIGHTNESS_MAP, ACCENTS,
    themeVars, adjustHexIntensity, gradientLayers, accentColor,
    readMode, convert, previewFeed,
    SELECTORS, OLED_OVERRIDE, gradientBlock,
  };
})();

"""
PREVIEW_SRC = r"""/* ========================================
   Theme Designer Pro website
   Live preview engine: gradients, Canvas FX,
   themes, and CSS presets
   ======================================== */

(function () {
  'use strict';

  // The mock's logo resolves against this script, so it loads from any page
  const LOGO = new URL('owui-logo.png', (document.currentScript && document.currentScript.src) || location.href).href;

  // ---- OKLCH palette math (mirrors the designer's generator) ----

  const STEPS = [50, 100, 200, 300, 400, 500, 600, 700, 800, 850, 900, 950];
  const LIGHTNESS_MAP = {
    50: 0.98, 100: 0.94, 200: 0.92, 300: 0.85, 400: 0.77, 500: 0.69,
    600: 0.51, 700: 0.42, 800: 0.32, 850: 0.27, 900: 0.2, 950: 0.16,
  };

  // The accents a theme's CSS reads: [mode field, custom property, hue shift]
  const ACCENTS = [['accent', '--tdp-accent', 0], ['accent2', '--tdp-accent-2', 40], ['accent3', '--tdp-accent-3', -40]];

  // The designer's generated accent (generatedAccent), c in thousandths
  function accentColor(h, c, dark, shift) {
    const ac = c > 0 ? Math.min(0.18, Math.max(0.06, c / 250)) : 0;
    return `oklch(${dark ? 0.74 : 0.56} ${ac.toFixed(3)} ${((h + shift + 360) % 360).toFixed(1)})`;
  }

  // modeKey ('dark', 'oled', 'light' or 'her', Dark when left out) only
  // decides how light the generated accents are
  function themeVars(mode, modeKey) {
    const vars = {};
    const paletteOn = !mode || mode.paletteEnabled !== false;
    const h = mode && mode.h != null ? mode.h : 250;
    const c = paletteOn && mode ? (mode.c || 0) / 1000 : 0;
    const l = mode && mode.l != null ? mode.l / 100 : 0.2;
    const deltaL = paletteOn ? l - 0.2 : 0;
    const ov = (paletteOn && mode && mode.overrides) || {};

    // Not colors: the frame reads these to give Canvas FX the theme's accents
    vars['--tdp-hue'] = String(h);
    vars['--tdp-chroma'] = String(paletteOn && mode ? (mode.c || 0) : 0);
    STEPS.forEach((step) => {
      const targetL = Math.max(0, Math.min(0.98, LIGHTNESS_MAP[step] + deltaL));
      const computed = `oklch(${targetL.toFixed(3)} ${c.toFixed(3)} ${c === 0 ? 0 : h})`;
      vars[`--color-gray-${step}`] = ov[`--color-gray-${step}`] || computed;
    });

    // The theme's accents for its CSS: a mode's own '#rrggbb', or the one
    // the designer generates from the hue (as buildCanvasTheme does)
    const dark = modeKey !== 'light' && modeKey !== 'her';
    ACCENTS.forEach(([key, name, shift]) => {
      const set = mode && typeof mode[key] === 'string' && /^#[0-9a-fA-F]{6}$/.test(mode[key]) ? mode[key].toLowerCase() : '';
      vars[name] = set || accentColor(Number(h) || 0, paletteOn && mode ? Number(mode.c) || 0 : 0, dark, shift);
    });

    // Manual variable overrides win over the generated palette
    if (mode && mode.manualOverridesEnabled !== false && typeof mode.manualOverrides === 'string') {
      mode.manualOverrides.split('\n').forEach((line) => {
        const t = line.trim();
        if (t.startsWith('--') && t.includes(':') && !/[{}]/.test(t)) {
          const idx = t.indexOf(':');
          vars[t.slice(0, idx).trim()] = t.slice(idx + 1).replace(/;\s*$/, '').trim();
        }
      });
    }
    return vars;
  }

  // ---- Gradient CSS (mirrors the designer's buildGradientCss) ----

  function adjustHexIntensity(hex, intensity) {
    hex = String(hex || '').replace('#', '');
    if (hex.length === 3) hex = hex.split('').map((ch) => ch + ch).join('');
    if (!/^[0-9a-fA-F]{6}$/.test(hex)) return '#' + (hex || '000000');
    const factor = intensity / 100;
    const rgb = [0, 2, 4].map((i) => {
      const v = parseInt(hex.slice(i, i + 2), 16);
      return Math.max(0, Math.min(255, Math.round(128 + (v - 128) * factor)));
    });
    return '#' + rgb.map((v) => v.toString(16).padStart(2, '0')).join('');
  }

  // Accepts a full gradient preset object (raw file) or a theme mode's gradient* fields
  function gradientLayers(g) {
    const type = g.type || g.gradientType || 'linear';
    const intensity = firstOf(g.intensity, g.gradientIntensity, 85);
    const animated = !!(g.animation || g.gradientAnimation || g.animated);
    const speed = firstOf(g.speed, g.gradientAnimationSpeed, 8);

    if (type === 'mesh') {
      const points = g.meshPoints || g.gradientMeshPoints || [];
      if (points.length < 2) return null;
      const bg = g.meshBgColor || g.gradientMeshBgColor || '#0a0a12';
      const layers = points
        .map((p) => `radial-gradient(at ${p.x}% ${p.y}%, ${adjustHexIntensity(p.color, intensity)} 0%, transparent ${p.spread}%)`)
        .join(', ');
      return { background: bg, backgroundImage: layers, animated, speed };
    }

    const stops = g.stops || g.gradientStops || [];
    if (stops.length < 2) return null;
    const stopsStr = [...stops]
      .sort((a, b) => (a.position || 0) - (b.position || 0))
      .map((s) => `${adjustHexIntensity(s.color, intensity)} ${s.position || 0}%`)
      .join(', ');

    let fn;
    if (type === 'radial') {
      const shape = firstOf(g.radialShape, g.gradientRadialShape, 'ellipse');
      const size = firstOf(g.radialSize, g.gradientRadialSize, 'farthest-corner');
      const x = firstOf(g.radialPosX, g.gradientRadialPosX, 50);
      const y = firstOf(g.radialPosY, g.gradientRadialPosY, 50);
      fn = `radial-gradient(${shape} ${size} at ${x}% ${y}%, ${stopsStr})`;
    } else {
      const angle = firstOf(g.angle, g.gradientAngle, 135);
      fn = `linear-gradient(${angle}deg, ${stopsStr})`;
    }
    return { background: null, backgroundImage: fn, animated, speed };
  }

  function firstOf() {
    for (const v of arguments) if (v != null) return v;
  }

  function applyGradientToEl(el, layers) {
    el.style.background = '';
    el.style.animation = '';
    if (layers.background) el.style.backgroundColor = layers.background;
    el.style.backgroundImage = layers.backgroundImage;
    if (layers.animated) {
      el.style.backgroundSize = '300% 300%';
      el.style.animation = `tdp-preview-gradient-shift ${layers.speed}s ease infinite`;
    }
  }

  // ---- Canvas FX engine (Worker-only, mirrors the designer's protocol) ----
  //
  // Scripts run in a Web Worker from a Blob URL with an OffscreenCanvas —
  // the same execution model the designer uses. There is deliberately no
  // main-thread eval fallback here: on browsers without OffscreenCanvas the
  // preview shows a notice instead of executing gallery code in page scope.

  function startCanvasEngine(scriptText, stage, statusFn) {
    if (typeof HTMLCanvasElement === 'undefined' || !HTMLCanvasElement.prototype.transferControlToOffscreen) {
      statusFn('Live Canvas FX preview needs a browser with OffscreenCanvas support (Chrome, Edge, Firefox, Safari 17+).');
      return null;
    }

    const canvas = document.createElement('canvas');
    canvas.className = 'preview-canvas';
    canvas.width = stage.clientWidth;
    canvas.height = stage.clientHeight;
    stage.appendChild(canvas);

    const blob = new Blob([scriptText], { type: 'application/javascript' });
    const objectURL = URL.createObjectURL(blob);
    let worker;
    try {
      worker = new Worker(objectURL);
    } catch (err) {
      URL.revokeObjectURL(objectURL);
      canvas.remove();
      statusFn('Could not start the animation worker: ' + err.message);
      return null;
    }

    worker.onerror = (e) => {
      statusFn('Script error: ' + (e.message || 'the animation failed to run.'));
    };

    const offscreen = canvas.transferControlToOffscreen();
    const post = (msg, transfer) => { try { worker.postMessage(msg, transfer); } catch (_) { /* worker gone */ } };
    post(
      {
        type: 'init',
        canvas: offscreen,
        width: stage.clientWidth,
        height: stage.clientHeight,
        env: { authToken: '', baseUrl: '', locale: navigator.language, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone },
      },
      [offscreen]
    );

    // rAF-coalesced pointer pump + immediate click/touch, per the engine contract
    let pendingMove = null;
    let rafId = null;
    const pump = () => {
      rafId = null;
      if (pendingMove) { post(pendingMove); pendingMove = null; }
    };
    const queueMove = (x, y) => {
      pendingMove = { type: 'mousemove', x, y };
      if (!rafId) rafId = requestAnimationFrame(pump);
    };

    const safeXY = (e) => { try { return { x: e.clientX, y: e.clientY }; } catch (_) { return null; } };
    const onMove = (e) => { const p = safeXY(e); if (p) queueMove(p.x, p.y); };
    const onClick = (e) => { const p = safeXY(e); if (p) post({ type: 'click', x: p.x, y: p.y }); };
    const onDown = (e) => { const p = safeXY(e); if (p) post({ type: 'mousedown', x: p.x, y: p.y }); };
    const onUp = (e) => { const p = safeXY(e); if (p) post({ type: 'mouseup', x: p.x, y: p.y }); };
    const touches = (e) => { try { return Array.from(e.touches.length ? e.touches : e.changedTouches).map((t) => ({ x: t.clientX, y: t.clientY })); } catch (_) { return []; } };
    const onTouchStart = (e) => {
      const c = touches(e);
      post({ type: 'touchstart', touches: c });
      if (c[0]) post({ type: 'mousedown', x: c[0].x, y: c[0].y });
    };
    const onTouchMove = (e) => {
      const c = touches(e);
      post({ type: 'touchmove', touches: c });
      if (c[0]) queueMove(c[0].x, c[0].y);
    };
    const onTouchEnd = (e) => {
      const c = touches(e);
      post({ type: 'touchend', touches: c });
      if (c[0]) post({ type: 'mouseup', x: c[0].x, y: c[0].y });
    };
    const onResize = () => post({ type: 'resize', width: stage.clientWidth, height: stage.clientHeight });
    const onBridge = (e) => {
      const d = e.data;
      if (!d || d.__tdpPreview !== true) return;
      switch (d.kind) {
        case 'mousemove': queueMove(d.x, d.y); break;
        case 'click': post({ type: 'click', x: d.x, y: d.y }); break;
        case 'mousedown': post({ type: 'mousedown', x: d.x, y: d.y }); break;
        case 'mouseup': post({ type: 'mouseup', x: d.x, y: d.y }); break;
        case 'touchstart':
          post({ type: 'touchstart', touches: d.touches });
          if (d.touches && d.touches[0]) post({ type: 'mousedown', x: d.touches[0].x, y: d.touches[0].y });
          break;
        case 'touchmove':
          post({ type: 'touchmove', touches: d.touches });
          if (d.touches && d.touches[0]) queueMove(d.touches[0].x, d.touches[0].y);
          break;
        case 'touchend':
          post({ type: 'touchend', touches: d.touches });
          if (d.touches && d.touches[0]) post({ type: 'mouseup', x: d.touches[0].x, y: d.touches[0].y });
          break;
      }
    };

    window.addEventListener('message', onBridge);
    window.addEventListener('mousemove', onMove);
    window.addEventListener('click', onClick, true);
    window.addEventListener('mousedown', onDown, true);
    window.addEventListener('mouseup', onUp, true);
    window.addEventListener('touchstart', onTouchStart, { passive: true });
    window.addEventListener('touchmove', onTouchMove, { passive: true });
    window.addEventListener('touchend', onTouchEnd, { passive: true });
    window.addEventListener('resize', onResize);

    return {
      sendContext(detail) {
        post(Object.assign({ type: 'context' }, detail));
      },
      destroy() {
        window.removeEventListener('message', onBridge);
        window.removeEventListener('mousemove', onMove);
        window.removeEventListener('click', onClick, true);
        window.removeEventListener('mousedown', onDown, true);
        window.removeEventListener('mouseup', onUp, true);
        window.removeEventListener('touchstart', onTouchStart);
        window.removeEventListener('touchmove', onTouchMove);
        window.removeEventListener('touchend', onTouchEnd);
        window.removeEventListener('resize', onResize);
        if (rafId) cancelAnimationFrame(rafId);
        worker.terminate();
        URL.revokeObjectURL(objectURL);
        canvas.remove();
      },
    };
  }

  // ---- Mock Open WebUI chat (inert sandboxed iframe) ----

  // Icon paths copied verbatim from Open WebUI's icon components so the mock
  // uses the same glyphs as the real app rather than lookalikes. Source paths:
  //   layout/Sidebar/icons/{EditPencil,Search,Notes,Workspace,Code,ChevronRight,Plus}.svelte
  //   icons/{Sidebar,PencilSquare,EllipsisHorizontal,Bolt,PlusAlt,Component,Mic,Voice}.svelte
  // Every one is a 24x24 stroke icon at stroke-width 1.5 (Voice overrides to 2.5).
  // Entries are path data strings; an entry may also be a raw element (Mic's
  // <rect>) so shapes stay byte-identical to the component they came from.
  function icon(shapes, opt) {
    const o = opt || {};
    return '<svg viewBox="0 0 24 24" aria-hidden="true" fill="' + (o.fill || 'none') +
      '" stroke="currentColor" stroke-width="' + (o.strokeWidth || '1.5') +
      '" stroke-linecap="round" stroke-linejoin="round">' +
      shapes.map(function (s) { return s.charAt(0) === '<' ? s : '<path d="' + s + '"/>'; }).join('') + '</svg>';
  }

  const SVG = {
    // Sidebar/icons/EditPencil.svelte — sidebar "New Chat"
    editPencil: icon(['M14.3632 5.65156L15.8431 4.17157C16.6242 3.39052 17.8905 3.39052 18.6716 4.17157L20.0858 5.58579C20.8668 6.36683 20.8668 7.63316 20.0858 8.41421L18.6058 9.8942M14.3632 5.65156L4.74749 15.2672C4.41542 15.5993 4.21079 16.0376 4.16947 16.5054L3.92738 19.2459C3.87261 19.8659 4.39148 20.3848 5.0115 20.33L7.75191 20.0879C8.21972 20.0466 8.65806 19.8419 8.99013 19.5099L18.6058 9.8942M14.3632 5.65156L18.6058 9.8942']),
    // Sidebar/icons/Search.svelte
    search: icon(['M17 17L21 21', 'M3 11C3 15.4183 6.58172 19 11 19C13.213 19 15.2161 18.1015 16.6644 16.6493C18.1077 15.2022 19 13.2053 19 11C19 6.58172 15.4183 3 11 3C6.58172 3 3 6.58172 3 11Z']),
    // Sidebar/icons/Notes.svelte
    notes: icon(['M4 19V5C4 3.89543 4.89543 3 6 3H19.4C19.7314 3 20 3.26863 20 3.6V16.7143', 'M6 17L20 17', 'M6 21L20 21', 'M6 21C4.89543 21 4 20.1046 4 19C4 17.8954 4.89543 17 6 17', 'M9 7L15 7']),
    // Sidebar/icons/Workspace.svelte
    workspace: icon(['M13.9922 17H16.9922M19.9922 17H16.9922M16.9922 17V14M16.9922 17V20', 'M4 9.4V4.6C4 4.26863 4.26863 4 4.6 4H9.4C9.73137 4 10 4.26863 10 4.6V9.4C10 9.73137 9.73137 10 9.4 10H4.6C4.26863 10 4 9.73137 4 9.4Z', 'M4 19.4V14.6C4 14.2686 4.26863 14 4.6 14H9.4C9.73137 14 10 14.2686 10 14.6V19.4C10 19.7314 9.73137 20 9.4 20H4.6C4.26863 20 4 19.7314 4 19.4Z', 'M14 9.4V4.6C14 4.26863 14.2686 4 14.6 4H19.4C19.7314 4 20 4.26863 20 4.6V9.4C20 9.73137 19.7314 10 19.4 10H14.6C14.2686 10 14 9.73137 14 9.4Z']),
    // Sidebar/icons/Code.svelte — "Playground"
    code: icon(['M13.5 6L10 18.5', 'M6.5 8.5L3 12L6.5 15.5', 'M17.5 8.5L21 12L17.5 15.5']),
    // Sidebar/icons/Plus.svelte — section "add" affordance
    plus: icon(['M6 12H12M18 12H12M12 12V6M12 12V18']),
    // Sidebar/icons/ChevronRight.svelte — "Pinned" disclosure
    chevron: icon(['M9 6L15 12L9 18']),
    // icons/ChevronDown.svelte — the model selector's trigger caret
    chevronDown: icon(['m19.5 8.25-7.5 7.5-7.5-7.5'], { strokeWidth: '2.5' }),
    // icons/Sidebar.svelte — sidebar collapse toggle
    panel: icon(['M19 21L5 21C3.89543 21 3 20.1046 3 19L3 5C3 3.89543 3.89543 3 5 3L19 3C20.1046 3 21 3.89543 21 5L21 19C21 20.1046 20.1046 21 19 21Z', 'M9.5 21V3', 'M5.5 10L7.25 12L5.5 14']),
    // icons/EllipsisHorizontal.svelte
    dots: icon(['M6.75 12a.75.75 0 1 1-1.5 0 .75.75 0 0 1 1.5 0ZM12.75 12a.75.75 0 1 1-1.5 0 .75.75 0 0 1 1.5 0ZM18.75 12a.75.75 0 1 1-1.5 0 .75.75 0 0 1 1.5 0Z']),
    // icons/Knobs.svelte — the navbar's Chat Controls button
    knobs: icon(['M10.5 6h9.75M10.5 6a1.5 1.5 0 1 1-3 0m3 0a1.5 1.5 0 1 0-3 0M3.75 6H7.5m3 12h9.75m-9.75 0a1.5 1.5 0 0 1-3 0m3 0a1.5 1.5 0 0 0-3 0m-3.75 0H7.5m9-6h3.75m-3.75 0a1.5 1.5 0 0 1-3 0m3 0a1.5 1.5 0 0 0-3 0m-9.75 0h9.75'], { strokeWidth: '1' }),
    // icons/ChatPlus.svelte — mobile navbar "New Chat"
    chatPlus: icon(['M9 12H12M15 12H12M12 12V9M12 12V15', 'M12 22C17.5228 22 22 17.5228 22 12C22 6.47715 17.5228 2 12 2C6.47715 2 2 6.47715 2 12C2 13.8214 2.48697 15.5291 3.33782 17L2.5 21.5L7 20.6622C8.47087 21.513 10.1786 22 12 22Z']),
    // icons/ChatBubbleDotted.svelte — Temporary Chat (dashes are part of the glyph)
    chatBubbleDotted: icon(['<path d="M12 22C17.5228 22 22 17.5228 22 12C22 6.47715 17.5228 2 12 2C6.47715 2 2 6.47715 2 12C2 13.8214 2.48697 15.5291 3.33782 17L2.5 21.5L7 20.6622C8.47087 21.513 10.1786 22 12 22Z" stroke-linecap="round" stroke-linejoin="round" stroke-dasharray="2.5 3.5"></path>']),
    // icons/Bolt.svelte — the "Suggested" marker on the chat placeholder
    bolt: icon(['m3.75 13.5 10.5-11.25L12 10.5h8.25L9.75 21.75 12 13.5H3.75Z']),
    // icons/PlusAlt.svelte — input bar "More"
    plusAlt: icon(['M6 12H12M18 12H12M12 12V6M12 12V18']),
    // icons/Component.svelte — input bar "Integrations"
    component: icon(['M5.21173 15.1113L2.52473 12.4243C2.29041 12.1899 2.29041 11.8101 2.52473 11.5757L5.21173 8.88873C5.44605 8.65442 5.82595 8.65442 6.06026 8.88873L8.74727 11.5757C8.98158 11.8101 8.98158 12.1899 8.74727 12.4243L6.06026 15.1113C5.82595 15.3456 5.44605 15.3456 5.21173 15.1113Z', 'M11.5757 21.475L8.88874 18.788C8.65443 18.5537 8.65443 18.1738 8.88874 17.9395L11.5757 15.2525C11.8101 15.0182 12.19 15.0182 12.4243 15.2525L15.1113 17.9395C15.3456 18.1738 15.3456 18.5537 15.1113 18.788L12.4243 21.475C12.19 21.7094 11.8101 21.7094 11.5757 21.475Z', 'M11.5757 8.7475L8.88874 6.06049C8.65443 5.82618 8.65443 5.44628 8.88874 5.21197L11.5757 2.52496C11.8101 2.29065 12.19 2.29065 12.4243 2.52496L15.1113 5.21197C15.3456 5.44628 15.3456 5.82618 15.1113 6.06049L12.4243 8.7475C12.19 8.98181 11.8101 8.98181 11.5757 8.7475Z', 'M17.9396 15.1113L15.2526 12.4243C15.0183 12.1899 15.0183 11.8101 15.2526 11.5757L17.9396 8.88873C18.174 8.65442 18.5539 8.65442 18.7882 8.88873L21.4752 11.5757C21.7095 11.8101 21.7095 12.1899 21.4752 12.4243L18.7882 15.1113C18.5539 15.3456 18.174 15.3456 17.9396 15.1113Z']),
    // icons/Mic.svelte — "Dictate"
    mic: icon(['<rect x="9" y="2" width="6" height="12" rx="3" />', 'M5 10v1a7 7 0 0 0 14 0v-1M12 18v4m0 0H9m3 0h3']),
    // icons/Voice.svelte — the filled circle button shown while the input is empty
    voice: icon(['M12 4L12 20', 'M8 9L8 15', 'M20 10L20 14', 'M4 10L4 14', 'M16 7L16 17'], { strokeWidth: '2.5' }),
    // Sidebar/icons/MoreHorizontal.svelte — the per-chat menu trigger
    moreHorizontal: icon([
      'M20 12.5C20.2761 12.5 20.5 12.2761 20.5 12C20.5 11.7239 20.2761 11.5 20 11.5C19.7239 11.5 19.5 11.7239 19.5 12C19.5 12.2761 19.7239 12.5 20 12.5Z',
      'M12 12.5C12.2761 12.5 12.5 12.2761 12.5 12C12.5 11.7239 12.2761 11.5 12 11.5C11.7239 11.5 11.5 11.7239 11.5 12C11.5 12.2761 11.7239 12.5 12 12.5Z',
      'M4 12.5C4.27614 12.5 4.5 12.2761 4.5 12C4.5 11.7239 4.27614 11.5 4 11.5C3.72386 11.5 3.5 11.7239 3.5 12C3.5 12.2761 3.72386 12.5 4 12.5Z',
    ], { fill: 'currentColor', strokeWidth: '2' }),
    // The input bar's menus (InputMenu.svelte, IntegrationsMenu.svelte,
    // ModelSelector/Selector.svelte) and their icons/*.svelte glyphs
    clip: icon(['M21.4383 11.6622L12.2483 20.8522C11.1225 21.9781 9.59552 22.6106 8.00334 22.6106C6.41115 22.6106 4.88418 21.9781 3.75834 20.8522C2.63249 19.7264 2 18.1994 2 16.6072C2 15.015 2.63249 13.4881 3.75834 12.3622L12.9483 3.17222C13.6989 2.42166 14.7169 2 15.7783 2C16.8398 2 17.8578 2.42166 18.6083 3.17222C19.3589 3.92279 19.7806 4.94077 19.7806 6.00222C19.7806 7.06368 19.3589 8.08166 18.6083 8.83222L9.40834 18.0222C9.03306 18.3975 8.52406 18.6083 7.99334 18.6083C7.46261 18.6083 6.95362 18.3975 6.57834 18.0222C6.20306 17.6469 5.99222 17.138 5.99222 16.6072C5.99222 16.0765 6.20306 15.5675 6.57834 15.1922L15.0683 6.71222']),
    camera: icon(['M2 19V9C2 7.89543 2.89543 7 4 7H4.5C5.12951 7 5.72229 6.70361 6.1 6.2L8.32 3.24C8.43331 3.08892 8.61115 3 8.8 3H15.2C15.3889 3 15.5667 3.08892 15.68 3.24L17.9 6.2C18.2777 6.70361 18.8705 7 19.5 7H20C21.1046 7 22 7.89543 22 9V19C22 20.1046 21.1046 21 20 21H4C2.89543 21 2 20.1046 2 19Z', 'M12 17C14.2091 17 16 15.2091 16 13C16 10.7909 14.2091 9 12 9C9.79086 9 8 10.7909 8 13C8 15.2091 9.79086 17 12 17Z']),
    globeAlt: icon(['M12 21a9.004 9.004 0 0 0 8.716-6.747M12 21a9.004 9.004 0 0 1-8.716-6.747M12 21c2.485 0 4.5-4.03 4.5-9S14.485 3 12 3m0 18c-2.485 0-4.5-4.03-4.5-9S9.515 3 12 3m0 0a8.997 8.997 0 0 1 7.843 4.582M12 3a8.997 8.997 0 0 0-7.843 4.582m15.686 0A11.953 11.953 0 0 1 12 10.5c-2.998 0-5.74-1.1-7.843-2.918m15.686 0A8.959 8.959 0 0 1 21 12c0 .778-.099 1.533-.284 2.253m0 0A17.919 17.919 0 0 1 12 16.5c-3.162 0-6.133-.815-8.716-2.247m0 0A9.015 9.015 0 0 1 3 12c0-1.605.42-3.113 1.157-4.418']),
    documentArrowUp: icon(['M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m6.75 12-3-3m0 0-3 3m3-3v6m-1.5-15H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z']),
    pageEdit: icon(['M20 12V5.74853C20 5.5894 19.9368 5.43679 19.8243 5.32426L16.6757 2.17574C16.5632 2.06321 16.4106 2 16.2515 2H4.6C4.26863 2 4 2.26863 4 2.6V21.4C4 21.7314 4.26863 22 4.6 22H11', 'M8 10H16M8 6H12M8 14H11', 'M17.9541 16.9394L18.9541 15.9394C19.392 15.5015 20.102 15.5015 20.5399 15.9394V15.9394C20.9778 16.3773 20.9778 17.0873 20.5399 17.5252L19.5399 18.5252M17.9541 16.9394L14.963 19.9305C14.8131 20.0804 14.7147 20.2741 14.6821 20.4835L14.4394 22.0399L15.9957 21.7973C16.2052 21.7646 16.3988 21.6662 16.5487 21.5163L19.5399 18.5252M17.9541 16.9394L19.5399 18.5252', 'M16 2V5.4C16 5.73137 16.2686 6 16.6 6H20']),
    database: icon(['M5 12V18C5 18 5 21 12 21C19 21 19 18 19 18V12', 'M5 6V12C5 12 5 15 12 15C19 15 19 12 19 12V6', 'M12 3C19 3 19 6 19 6C19 6 19 9 12 9C5 9 5 6 5 6C5 6 5 3 12 3Z']),
    clockRotateRight: icon(['M12 6L12 12L18 12', 'M21.8883 10.5C21.1645 5.68874 17.013 2 12 2C6.47715 2 2 6.47715 2 12C2 17.5228 6.47715 22 12 22C16.1006 22 19.6248 19.5318 21.1679 16', 'M17 16H21.4C21.7314 16 22 16.2686 22 16.6V21']),
    chevronRightAlt: icon(['m8.25 4.5 7.5 7.5-7.5 7.5']),
    chevronLeft: icon(['M15.75 19.5 8.25 12l7.5-7.5']),
    wrench: icon(['M21.75 6.75a4.5 4.5 0 0 1-4.884 4.484c-1.076-.091-2.264.071-2.95.904l-7.152 8.684a2.548 2.548 0 1 1-3.586-3.586l8.684-7.152c.833-.686.995-1.874.904-2.95a4.5 4.5 0 0 1 6.336-4.486l-3.276 3.276a3.004 3.004 0 0 0 2.25 2.25l3.276-3.276c.256.565.398 1.192.398 1.852Z', 'M4.867 19.125h.008v.008h-.008v-.008Z']),
    searchAlt: icon(['m21 21-5.197-5.197m0 0A7.5 7.5 0 1 0 5.196 5.196a7.5 7.5 0 0 0 10.607 10.607Z'], { strokeWidth: '2' }),
    photo: icon(['M21 7.6V20.4C21 20.7314 20.7314 21 20.4 21H7.6C7.26863 21 7 20.7314 7 20.4V7.6C7 7.26863 7.26863 7 7.6 7H20.4C20.7314 7 21 7.26863 21 7.6Z', 'M18 4H4.6C4.26863 4 4 4.26863 4 4.6V18', 'M7 16.8L12.4444 15L21 18', 'M16.5 13C15.6716 13 15 12.3284 15 11.5C15 10.6716 15.6716 10 16.5 10C17.3284 10 18 10.6716 18 11.5C18 12.3284 17.3284 13 16.5 13Z']),
    terminal: icon(['M13 16H18', 'M6 8L10 12L6 16', 'M2 18V6C2 4.89543 2.89543 4 4 4H20C21.1046 4 22 4.89543 22 6V18C22 19.1046 21.1046 20 20 20H4C2.89543 20 2 19.1046 2 18Z'], { strokeWidth: '1.75' }),
    check: icon(['m4.5 12.75 6 6 9-13.5']),
    keyframes: icon(['M13.8476 13.317L9.50515 18.2798C8.70833 19.1905 7.29167 19.1905 6.49485 18.2798L2.15238 13.317C1.49259 12.563 1.49259 11.437 2.15238 10.683L6.49485 5.72018C7.29167 4.80952 8.70833 4.80952 9.50515 5.72017L13.8476 10.683C14.5074 11.437 14.5074 12.563 13.8476 13.317Z', 'M13 19L17.8844 13.3016C18.5263 12.5526 18.5263 11.4474 17.8844 10.6984L13 5', 'M17 19L21.8844 13.3016C22.5263 12.5526 22.5263 11.4474 21.8844 10.6984L17 5'], { strokeWidth: '2' }),
    documentPage: icon(['M4 21.4V2.6C4 2.26863 4.26863 2 4.6 2H16.2515C16.4106 2 16.5632 2.06321 16.6757 2.17574L19.8243 5.32426C19.9368 5.43679 20 5.5894 20 5.74853V21.4C20 21.7314 19.7314 22 19.4 22H4.6C4.26863 22 4 21.7314 4 21.4Z', 'M8 10L16 10', 'M8 18L16 18', 'M8 14L12 14', 'M16 2V5.4C16 5.73137 16.2686 6 16.6 6H20']),
    // The feature pills draw their icons a little heavier (MessageInput.svelte)
    globePill: icon(['M12 21a9.004 9.004 0 0 0 8.716-6.747M12 21a9.004 9.004 0 0 1-8.716-6.747M12 21c2.485 0 4.5-4.03 4.5-9S14.485 3 12 3m0 18c-2.485 0-4.5-4.03-4.5-9S9.515 3 12 3m0 0a8.997 8.997 0 0 1 7.843 4.582M12 3a8.997 8.997 0 0 0-7.843 4.582m15.686 0A11.953 11.953 0 0 1 12 10.5c-2.998 0-5.74-1.1-7.843-2.918m15.686 0A8.959 8.959 0 0 1 21 12c0 .778-.099 1.533-.284 2.253m0 0A17.919 17.919 0 0 1 12 16.5c-3.162 0-6.133-.815-8.716-2.247m0 0A9.015 9.015 0 0 1 3 12c0-1.605.42-3.113 1.157-4.418'], { strokeWidth: '1.75' }),
    photoPill: icon(['M21 7.6V20.4C21 20.7314 20.7314 21 20.4 21H7.6C7.26863 21 7 20.7314 7 20.4V7.6C7 7.26863 7.26863 7 7.6 7H20.4C20.7314 7 21 7.26863 21 7.6Z', 'M18 4H4.6C4.26863 4 4 4.26863 4 4.6V18', 'M7 16.8L12.4444 15L21 18', 'M16.5 13C15.6716 13 15 12.3284 15 11.5C15 10.6716 15.6716 10 16.5 10C17.3284 10 18 10.6716 18 11.5C18 12.3284 17.3284 13 16.5 13Z'], { strokeWidth: '1.75' }),
    terminalPill: icon(['M13 16H18', 'M6 8L10 12L6 16', 'M2 18V6C2 4.89543 2.89543 4 4 4H20C21.1046 4 22 4.89543 22 6V18C22 19.1046 21.1046 20 20 20H4C2.89543 20 2 19.1046 2 18Z'], { strokeWidth: '2' }),
    wrenchPill: icon(['M21.75 6.75a4.5 4.5 0 0 1-4.884 4.484c-1.076-.091-2.264.071-2.95.904l-7.152 8.684a2.548 2.548 0 1 1-3.586-3.586l8.684-7.152c.833-.686.995-1.874.904-2.95a4.5 4.5 0 0 1 6.336-4.486l-3.276 3.276a3.004 3.004 0 0 0 2.25 2.25l3.276-3.276c.256.565.398 1.192.398 1.852Z', 'M4.867 19.125h.008v.008h-.008v-.008Z'], { strokeWidth: '1.75' }),
    // Navbar: Temporary Chat on (ChatBubbleDottedChecked), the placeholder's
    // EyeSlash, and Chat Controls' collapsible chevrons and close button
    chatBubbleDottedChecked: icon(['M8 12L11 15L16 10', '<path d="M12 22C17.5228 22 22 17.5228 22 12C22 6.47715 17.5228 2 12 2C6.47715 2 2 6.47715 2 12C2 13.8214 2.48697 15.5291 3.33782 17L2.5 21.5L7 20.6622C8.47087 21.513 10.1786 22 12 22Z" stroke-linecap="round" stroke-linejoin="round" stroke-dasharray="2.5 3.5"></path>']),
    eyeSlash: icon(['M3.98 8.223A10.477 10.477 0 0 0 1.934 12C3.226 16.338 7.244 19.5 12 19.5c.993 0 1.953-.138 2.863-.395M6.228 6.228A10.451 10.451 0 0 1 12 4.5c4.756 0 8.773 3.162 10.065 7.498a10.522 10.522 0 0 1-4.293 5.774M6.228 6.228 3 3m3.228 3.228 3.65 3.65m7.894 7.894L21 21m-3.228-3.228-3.65-3.65m0 0a3 3 0 1 0-4.243-4.243m4.242 4.242L9.88 9.88'], { strokeWidth: '2' }),
    chevronUpSm: icon(['m4.5 15.75 7.5-7.5 7.5 7.5'], { strokeWidth: '2' }),
    chevronDownSm: icon(['m19.5 8.25-7.5 7.5-7.5-7.5'], { strokeWidth: '2' }),
    chevronRightSm: icon(['m8.25 4.5 7.5 7.5-7.5 7.5'], { strokeWidth: '2' }),
    chevronRightBold: icon(['m8.25 4.5 7.5 7.5-7.5 7.5'], { strokeWidth: '3' }),
    close: icon(['M6 18 18 6M6 6l12 12']),
    // Sidebar/icons for the chat menu and the user menu
    share: icon(['M18 22C19.6569 22 21 20.6569 21 19C21 17.3431 19.6569 16 18 16C16.3431 16 15 17.3431 15 19C15 20.6569 16.3431 22 18 22Z', 'M18 8C19.6569 8 21 6.65685 21 5C21 3.34315 19.6569 2 18 2C16.3431 2 15 3.34315 15 5C15 6.65685 16.3431 8 18 8Z', 'M6 15C7.65685 15 9 13.6569 9 12C9 10.3431 7.65685 9 6 9C4.34315 9 3 10.3431 3 12C3 13.6569 4.34315 15 6 15Z', '<path d="M15.5 6.5L8.5 10.5" stroke-linecap="butt" stroke-linejoin="miter"/>', '<path d="M8.5 13.5L15.5 17.5" stroke-linecap="butt" stroke-linejoin="miter"/>']),
    download: icon(['M6 20L18 20', 'M12 4V16M12 16L15.5 12.5M12 16L8.5 12.5']),
    chatCheck: icon(['M8 12L11 15L16 10', 'M12 22C17.5228 22 22 17.5228 22 12C22 6.47715 17.5228 2 12 2C6.47715 2 2 6.47715 2 12C2 13.8214 2.48697 15.5291 3.33782 17L2.5 21.5L7 20.6622C8.47087 21.513 10.1786 22 12 22Z']),
    pin: icon(['M9.5 14.5L3 21', 'M5.00007 9.48528L14.1925 18.6777L15.8895 16.9806L15.4974 13.1944L21.0065 8.5211L15.1568 2.67141L10.4834 8.18034L6.69713 7.78823L5.00007 9.48528Z']),
    pinSlash: icon(['M9.5 14.5L3 21', 'M7.67602 7.8896L6.69713 7.78823L5.00007 9.48528L14.1925 18.6777L15.8895 16.9806L15.7879 16M11.4847 7L15.1568 2.67141L21.0065 8.5211L16.6991 12.175', 'M3 3L21 21']),
    copy: icon(['M19.4 20H9.6C9.26863 20 9 19.7314 9 19.4V9.6C9 9.26863 9.26863 9 9.6 9H19.4C19.7314 9 20 9.26863 20 9.6V19.4C20 19.7314 19.7314 20 19.4 20Z', 'M15 9V4.6C15 4.26863 14.7314 4 14.4 4H4.6C4.26863 4 4 4.26863 4 4.6V14.4C4 14.7314 4.26863 15 4.6 15H9']),
    folder: icon(['M2 11V4.6C2 4.26863 2.26863 4 2.6 4H8.77805C8.92127 4 9.05977 4.05124 9.16852 4.14445L12.3315 6.85555C12.4402 6.94876 12.5787 7 12.722 7H21.4C21.7314 7 22 7.26863 22 7.6V11M2 11V19.4C2 19.7314 2.26863 20 2.6 20H21.4C21.7314 20 22 19.7314 22 19.4V11M2 11H22']),
    archiveBox: icon(['m20.25 7.5-.625 10.632a2.25 2.25 0 0 1-2.247 2.118H6.622a2.25 2.25 0 0 1-2.247-2.118L3.75 7.5M10 11.25h4M3.375 7.5h17.25c.621 0 1.125-.504 1.125-1.125v-1.5c0-.621-.504-1.125-1.125-1.125H3.375c-.621 0-1.125.504-1.125 1.125v1.5c0 .621.504 1.125 1.125 1.125Z'], { strokeWidth: '1.7' }),
    trash: icon(['M20 9L18.005 20.3463C17.8369 21.3026 17.0062 22 16.0353 22H7.96474C6.99379 22 6.1631 21.3026 5.99496 20.3463L4 9', 'M21 6L15.375 6M3 6L8.625 6M8.625 6V4C8.625 2.89543 9.52043 2 10.625 2H13.375C14.4796 2 15.375 2.89543 15.375 4V6M8.625 6L15.375 6']),
    cog: icon(['M10.343 3.94c.09-.542.56-.94 1.11-.94h1.093c.55 0 1.02.398 1.11.94l.149.894c.07.424.384.764.78.93.398.164.855.142 1.205-.108l.737-.527a1.125 1.125 0 011.45.12l.773.774c.39.389.44 1.002.12 1.45l-.527.737c-.25.35-.272.806-.107 1.204.165.397.505.71.93.78l.893.15c.543.09.94.56.94 1.109v1.094c0 .55-.397 1.02-.94 1.11l-.893.149c-.425.07-.765.383-.93.78-.165.398-.143.854.107 1.204l.527.738c.32.447.269 1.06-.12 1.45l-.774.773a1.125 1.125 0 01-1.449.12l-.738-.527c-.35-.25-.806-.272-1.203-.107-.397.165-.71.505-.781.929l-.149.894c-.09.542-.56.94-1.11.94h-1.094c-.55 0-1.019-.398-1.11-.94l-.148-.894c-.071-.424-.384-.764-.781-.93-.398-.164-.854-.142-1.204.108l-.738.527c-.447.32-1.06.269-1.45-.12l-.773-.774a1.125 1.125 0 01-.12-1.45l.527-.737c.25-.35.273-.806.108-1.204-.165-.397-.505-.71-.93-.78l-.894-.15c-.542-.09-.94-.56-.94-1.109v-1.094c0-.55.398-1.02.94-1.11l.894-.149c.424-.07.765-.383.93-.78.165-.398.143-.854-.107-1.204l-.527-.738a1.125 1.125 0 01.12-1.45l.773-.773a1.125 1.125 0 011.45-.12l.737.527c.35.25.807.272 1.204.107.397-.165.71-.505.78-.929l.15-.894z', 'M15 12a3 3 0 11-6 0 3 3 0 016 0z']),
    logOut: icon(['M12 12H19M19 12L16 15M19 12L16 9', 'M19 6V5C19 3.89543 18.1046 3 17 3H7C5.89543 3 5 3.89543 5 5V19C5 20.1046 5.89543 21 7 21H17C18.1046 21 19 20.1046 19 19V18']),
    emojiFace: icon(['M12 22C6.47715 22 2 17.5228 2 12C2 6.47715 6.47715 2 12 2C17.5228 2 22 6.47715 22 12C22 17.5228 17.5228 22 12 22Z', 'M16.5 14.5C16.5 14.5 15 16.5 12 16.5C9 16.5 7.5 14.5 7.5 14.5', '<path fill="currentColor" d="M15.5 9C15.2239 9 15 8.77614 15 8.5C15 8.22386 15.2239 8 15.5 8C15.7761 8 16 8.22386 16 8.5C16 8.77614 15.7761 9 15.5 9Z"/>', '<path fill="currentColor" d="M8.5 9C8.22386 9 8 8.77614 8 8.5C8 8.22386 8.22386 8 8.5 8C8.77614 8 9 8.22386 9 8.5C9 8.77614 8.77614 9 8.5 9Z"/>']),
    user: icon(['M5 20V19C5 15.134 8.13401 12 12 12V12C15.866 12 19 15.134 19 19V20', 'M12 12C14.2091 12 16 10.2091 16 8C16 5.79086 14.2091 4 12 4C9.79086 4 8 5.79086 8 8C8 10.2091 9.79086 12 12 12Z']),
    // Channels: Hashtag, Lock, and the message toolbar's icons
    hashtag: icon(['M10 3L6 21', 'M20.5 16H2.5', 'M22 7H4', 'M18 3L14 21']),
    lock: icon(['M16 12H17.4C17.7314 12 18 12.2686 18 12.6V19.4C18 19.7314 17.7314 20 17.4 20H6.6C6.26863 20 6 19.7314 6 19.4V12.6C6 12.2686 6.26863 12 6.6 12H8M16 12V8C16 6.66667 15.2 4 12 4C8.8 4 8 6.66667 8 8V12M16 12H8'], { strokeWidth: '1.7' }),
    faceSmile: icon(['M15.182 15.182a4.5 4.5 0 0 1-6.364 0M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0ZM9.75 9.75c0 .414-.168.75-.375.75S9 10.164 9 9.75 9.168 9 9.375 9s.375.336.375.75Zm-.375 0h.008v.015h-.008V9.75Zm5.625 0c0 .414-.168.75-.375.75s-.375-.336-.375-.75.168-.75.375-.75.375.336.375.75Zm-.375 0h.008v.015h-.008V9.75Z']),
    arrowUpLeftAlt: icon(['M10.25 4.75L6.75 8.25L10.25 11.75', 'M6.75 8.25L12.75 8.25C14.9591 8.25 16.75 10.0409 16.75 12.25V19.25']),
    chatBubbleEllipsis: icon(['M8.625 12a.375.375 0 1 1-.75 0 .375.375 0 0 1 .75 0Zm0 0H8.25m4.125 0a.375.375 0 1 1-.75 0 .375.375 0 0 1 .75 0Zm0 0H12m4.125 0a.375.375 0 1 1-.75 0 .375.375 0 0 1 .75 0Zm0 0h-.375M21 12c0 4.556-4.03 8.25-9 8.25a9.764 9.764 0 0 1-2.555-.337A5.972 5.972 0 0 1 5.41 20.97a5.969 5.969 0 0 1-.474-.065 4.48 4.48 0 0 0 .978-2.025c.09-.457-.133-.901-.467-1.226C3.93 16.178 3 14.189 3 12c0-4.556 4.03-8.25 9-8.25s9 3.694 9 8.25Z']),
    pencil: icon(['M16.862 4.487l1.687-1.688a1.875 1.875 0 112.652 2.652L6.832 19.82a4.5 4.5 0 01-1.897 1.13l-2.685.8.8-2.685a4.5 4.5 0 011.13-1.897L16.863 4.487zm0 0L19.5 7.125']),
    garbageBin: icon(['M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0']),
    // channel/MessageInput.svelte's inline 20- and 16-unit filled glyphs
    plusSolid: '<svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path d="M10.75 4.75a.75.75 0 0 0-1.5 0v4.5h-4.5a.75.75 0 0 0 0 1.5h4.5v4.5a.75.75 0 0 0 1.5 0v-4.5h4.5a.75.75 0 0 0 0-1.5h-4.5v-4.5Z"/></svg>',
    micSolid: '<svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path d="M7 4a3 3 0 016 0v6a3 3 0 11-6 0V4z"/><path d="M5.5 9.643a.75.75 0 00-1.5 0V10c0 3.06 2.29 5.585 5.25 5.954V17.5h-1.5a.75.75 0 000 1.5h4.5a.75.75 0 000-1.5h-1.5v-1.546A6.001 6.001 0 0016 10v-.357a.75.75 0 00-1.5 0V10a4.5 4.5 0 01-9 0v-.357z"/></svg>',
    arrowUpSolid: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path fill-rule="evenodd" d="M8 14a.75.75 0 0 1-.75-.75V4.56L4.03 7.78a.75.75 0 0 1-1.06-1.06l4.5-4.5a.75.75 0 0 1 1.06 0l4.5 4.5a.75.75 0 0 1-1.06 1.06L8.75 4.56v8.69A.75.75 0 0 1 8 14Z" clip-rule="evenodd"/></svg>',
    // icons/XMark.svelte: a 20-unit filled glyph, unlike the stroked set
    xMark: '<svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path d="M6.28 5.22a.75.75 0 00-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 101.06 1.06L10 11.06l3.72 3.72a.75.75 0 101.06-1.06L11.06 10l3.72-3.72a.75.75 0 00-1.06-1.06L10 8.94 6.28 5.22z"/></svg>',
  };

  // ResponseMessage.svelte's action row — inline SVGs rather than components,
  // all w-4 h-4 at stroke-width 2.3. Read Aloud uses its idle (unmuted) state.
  const ACTION_SVG = {
    edit: icon(['M16.862 4.487l1.687-1.688a1.875 1.875 0 112.652 2.652L6.832 19.82a4.5 4.5 0 01-1.897 1.13l-2.685.8.8-2.685a4.5 4.5 0 011.13-1.897L16.863 4.487zm0 0L19.5 7.125'], { strokeWidth: '2.3' }),
    copy: icon(['M15.666 3.888A2.25 2.25 0 0013.5 2.25h-3c-1.03 0-1.9.693-2.166 1.638m7.332 0c.055.194.084.4.084.612v0a.75.75 0 01-.75.75H9a.75.75 0 01-.75-.75v0c0-.212.03-.418.084-.612m7.332 0c.646.049 1.288.11 1.927.184 1.1.128 1.907 1.077 1.907 2.185V19.5a2.25 2.25 0 01-2.25 2.25H6.75A2.25 2.25 0 014.5 19.5V6.257c0-1.108.806-2.057 1.907-2.185a48.208 48.208 0 011.927-.184'], { strokeWidth: '2.3' }),
    speak: icon(['M19.114 5.636a9 9 0 010 12.728M16.463 8.288a5.25 5.25 0 010 7.424M6.75 8.25l4.72-4.72a.75.75 0 011.28.53v15.88a.75.75 0 01-1.28.53l-4.72-4.72H4.51c-.88 0-1.704-.507-1.938-1.354A9.01 9.01 0 012.25 12c0-.83.112-1.633.322-2.396C2.806 8.756 3.63 8.25 4.51 8.25H6.75z'], { strokeWidth: '2.3' }),
    thumbUp: icon(['M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3'], { strokeWidth: '2.3' }),
    thumbDown: icon(['M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3zm7-13h2.67A2.31 2.31 0 0 1 22 4v7a2.31 2.31 0 0 1-2.33 2H17'], { strokeWidth: '2.3' }),
    continueResponse: icon(['M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z', 'M15.91 11.672a.375.375 0 0 1 0 .656l-5.603 3.113a.375.375 0 0 1-.557-.328V8.887c0-.286.307-.466.557-.327l5.603 3.112Z'], { strokeWidth: '2.3' }),
    // sibling pager chevrons (ResponseMessage.svelte ~L907/L978, size-3.5 at
    // stroke-width 2.5)
    prevMsg: icon(['M15.75 19.5 8.25 12l7.5-7.5'], { strokeWidth: '2.5' }),
    nextMsg: icon(['m8.25 4.5 7.5 7.5-7.5 7.5'], { strokeWidth: '2.5' }),
    // ResponseMessage.svelte "Generation Info" button (id="info-{id}"), shown
    // when message.usage exists, between Read Aloud and Good Response
    info: icon(['M11.25 11.25l.041-.02a.75.75 0 011.063.852l-.708 2.836a.75.75 0 001.063.853l.041-.021M21 12a9 9 0 11-18 0 9 9 0 0118 0zm-9-3.75h.008v.008H12V8.25z'], { strokeWidth: '2.3' }),
    regenerate: icon(['M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182m0-4.991v4.99'], { strokeWidth: '2.3' }),
    // UserMessage.svelte:591 — Delete is drawn at stroke-width 2, not 2.3
    trash: icon(['m14.74 9-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 0 1-2.244 2.077H8.084a2.25 2.25 0 0 1-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 0 0-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 0 1 3.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 0 0-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 0 0-7.5 0'], { strokeWidth: '2' }),
  };

  // Tooltip contents match the upstream <Tooltip content=...> wrappers, which
  // all use placement="bottom" on message action buttons.
  const ACTION_LABELS = {
    edit: 'Edit', copy: 'Copy', speak: 'Read Aloud', thumbUp: 'Good Response',
    thumbDown: 'Bad Response', continueResponse: 'Continue Response',
    regenerate: 'Regenerate', trash: 'Delete',
  };
  function actionBtn(k, extraClass) {
    return '<span class="msg-action' + (extraClass || '') + '" data-tip="' + ACTION_LABELS[k] +
      '" data-tip-placement="bottom">' + ACTION_SVG[k] + '</span>';
  }

  const CHAT_MENU = '<span class="chat-actions"><span class="chat-menu-btn" data-tip="More">' + SVG.moreHorizontal + '</span></span>';

  // UserMessage.svelte's Edit + Copy pair (same two glyphs the assistant row
  // uses), plus Delete — which L578 gates on `!isFirstMessage || siblings > 1`,
  // so the opening message of a conversation never carries it.
  function userActions(withDelete) {
    return '<div class="user-actions">' +
      ['edit', 'copy'].map(function (k) { return actionBtn(k); }).join('') +
      (withDelete ? actionBtn('trash', ' delete') : '') +
      '</div>';
  }
  const USER_ACTIONS = userActions(false);
  const USER_ACTIONS_DEL = userActions(true);

  // common/Collapsible.svelte with attributes.type === 'reasoning': the
  // "Thought for X seconds" trigger plus the reasoning body, which the
  // middleware emits as a blockquote. Collapsed by default like upstream
  // ($settings.expandDetails defaults false).
  function thoughtBlock(t) {
    if (!t) return '';
    return '<div class="thought" role="button">Thought for ' + t.seconds + ' seconds' + SVG.chevronDown + '</div>' +
      '<div class="thought-body markdown-prose" hidden><blockquote>' + t.body + '</blockquote></div>';
  }

  // Full message rows in Open WebUI's DOM shape (Message.svelte row >
  // UserMessage/ResponseMessage flex > chat-{role} block). Literal utility
  // class strings ride along so preset CSS written against the app's markup —
  // .user-message .rounded-3xl, [class*="bg-gray-"] — matches here too.
  function userRow(text, notFirst) {
    return '<div class="message-listitem group"><div class="msg user-message group">' +
      '<div class="chat-user w-full min-w-full">' +
      '<div class="bubble-row"><div class="bubble rounded-3xl max-w-[90%] px-4 py-1.5 bg-gray-50 dark:bg-gray-850"><div class="markdown-prose">' + text + '</div></div></div>' +
      userActions(!!notFirst) +
      '</div></div></div>';
  }
  // Sibling pager (ResponseMessage.svelte ~L900-995): chevron buttons around
  // a tracking-widest "i/n" count, leading the action row of each variant.
  function branchPager(i, n) {
    return '<div class="branch-pager">' +
      '<span class="branch-nav branch-prev">' + ACTION_SVG.prevMsg + '</span>' +
      '<span class="branch-count">' + (i + 1) + '/' + n + '</span>' +
      '<span class="branch-nav branch-next">' + ACTION_SVG.nextMsg + '</span>' +
      '</div>';
  }

  // A response with regenerated variants. Each variant carries its own stats
  // line, reasoning, body, actions (with per-variant Generation Info) and
  // follow-ups; only the first is visible until the pager switches.
  function aiRowMulti(variants) {
    var n = variants.length;
    return '<div class="message-listitem group"><div class="msg">' +
      '<div class="ai-avatar"><img src="' + LOGO + '" alt=""></div>' +
      '<div class="ai-col flex-auto w-0 pl-1 relative">' +
      '<div class="ai-model">Preview Model</div>' +
      '<div class="branches">' +
      variants.map(function (v, i) {
        return '<div class="branch' + (i === 0 ? ' active' : '') + '">' +
          '<div class="ai-stats">' + v.stats + '</div>' +
          '<div class="chat-assistant w-full min-w-full">' +
          thoughtBlock(v.thought) +
          '<div class="markdown-prose">' + v.body + '</div>' +
          msgActions(v.stats, n > 1 ? branchPager(i, n) : '') +
          followUps(v.fups || null) +
          '</div></div>';
      }).join('') +
      '</div></div></div></div>';
  }
  function aiRow(stats, body, fups, t) {
    return aiRowMulti([{ stats: stats, body: body, fups: fups, thought: t }]);
  }

  // FollowUps.svelte — hairline-separated rows under a "Follow up" heading
  function followUps(items) {
    if (!items || !items.length) return '';
    return '<div class="followups-wrap"><div class="followups">' +
      '<div class="followups-title">Follow up</div><div class="followups-list">' +
      items.map(function (q, i) {
        return '<div class="followup" data-tip="' + q + '">' + q + '</div>' + (i < items.length - 1 ? '<hr>' : '');
      }).join('') +
      '</div></div></div>';
  }

  // The row Open WebUI renders under a finished assistant reply, in source order.
  function statsTip(stats) {
    return String(stats || '').split(' | ').join('\n');
  }

  function msgActions(stats, pagerHtml) {
    return '<div class="msg-actions">' + (pagerHtml || '') +
      ['edit', 'copy', 'speak'].map(function (k) { return actionBtn(k); }).join('') +
      '<span class="msg-action" data-tip="' + esc(statsTip(stats)) + '" data-tip-placement="bottom">' + ACTION_SVG.info + '</span>' +
      ['thumbUp', 'thumbDown', 'continueResponse', 'regenerate'].map(function (k) { return actionBtn(k); }).join('') +
      '</div>';
  }
  // Template for the in-iframe script: it swaps the placeholder for each
  // message's own stats when building rows on chat switch.
  const MSG_ACTIONS_TPL = msgActions('__TDP_STATS__');

  // What Preview Model answers when a follow-up is clicked, in the order new
  // follow-ups are offered. Every follow-up in the mock chats has an answer.
  const CODE = (lang, code) => '<div class="code-block"><div class="code-block-header"><span>' + lang +
    '</span><span class="cb-action">Copy</span></div><pre>' + code + '</pre></div>';
  const MOCK_REPLIES = [
    ['How do I import one of these presets into my instance?',
      '<p>Three ways, all from the designer in Open WebUI:</p><ul>' +
      '<li><b>Install on my Open WebUI</b> on any gallery preset opens your designer with the import ready. Nothing installs until you confirm it.</li>' +
      '<li>Download the preset and drop the file anywhere on the designer.</li>' +
      '<li>Paste a preset\'s address into its tab\'s <b>Import</b> dialog and choose <b>Load URL</b>.</li></ul>' +
      '<p>A theme with a Canvas FX script asks before it runs, since the script runs for everyone who sees the theme.</p>'],
    ['Which presets look best with structural transparency enabled?',
      '<p>The ones with something behind the interface to show: a Canvas FX animation or a gradient. Turning either on brings in the structural layer, which makes the layout containers see-through so the background reaches the chat.</p>' +
      '<p>Dark, low-chroma palettes carry it best, because text stays readable over a busy background. The <b>Sidebar Transparency</b> and <b>Overlay Transparency</b> valves set how much of it shows through the sidebar and the menus.</p>'],
    ['Can I combine a Canvas FX script with a gradient background?',
      '<p>Yes. Each mode has its own gradient and its own Canvas FX, and both can be on. The gradient paints the page, and the animation draws on a canvas above it.</p>' +
      '<p>A script that clears its canvas each frame, rather than filling it, lets the gradient show through between its shapes. One that paints a full background covers it.</p>'],
    ['What happens to the palette in OLED mode?',
      '<p>OLED is a mode of its own, with its own palette. It starts at lightness <code>0</code>, so the darkest steps are pure black and the pixels behind them switch off.</p>' +
      '<p>A theme file without an OLED mode gets Dark\'s palette at lightness <code>0</code>, so every theme has one.</p>'],
    ['Do regenerated responses keep the same theme variables?',
      '<p>They do. The theme lives on the page, not in a message: every response, regenerated or not, reads the same <code>--color-gray-*</code> variables. Switch branches with the pager and nothing changes but the text.</p>'],
    ['Which mode should I preview first for a new preset?',
      '<p>The one most of your users use. On most instances that\'s Dark, so start there, then check Light: a palette that looks right in the dark can wash out text once the surfaces turn pale.</p>' +
      '<p>OLED and Her follow from Dark and Light if the preset leaves them out.</p>'],
    ['Why OKLCH instead of HSL for the ramp?',
      '<p>In OKLCH, equal steps of lightness look equal. In HSL a yellow and a blue at the same lightness look nothing alike, so a ramp built on it jumps in brightness as the hue moves.</p>' +
      '<p>Open WebUI\'s own grays are OKLCH too, so a ramp at the default hue and chroma matches them exactly, and every hue you pick keeps the same contrast between steps.</p>'],
    ['How do I keep contrast accessible across all twelve steps?',
      '<p>Watch the text-on-surface pairs rather than the steps one by one. The designer\'s contrast report rates each pair the theme actually uses against WCAG: 4.5 for AA and 7 for AAA, at normal text size.</p>' +
      '<p>Chroma is usually what breaks it. A strongly colored ramp can pass in Dark and fail in Light, so check both.</p>'],
    ['Can I pin a single step and regenerate the rest?',
      '<p>Yes: set the step\'s color in the variable grid and lock it with 🔒. Moving the hue, chroma or lightness sliders then regenerates every other step and leaves the locked one as you set it.</p>'],
    ['What data does the engine stream to my script?',
      '<p>Everything arrives through <code>self.onmessage</code>:</p><ul>' +
      '<li><code>init</code> with the canvas, its size and the theme\'s palette</li>' +
      '<li><code>resize</code>, and the mouse, click and touch events</li>' +
      '<li><code>theme</code> whenever the palette or the mode changes</li>' +
      '<li><code>context</code> as the chat grows: the message count, characters and estimated tokens, and the exact token count after each reply when the model reports usage</li></ul>' +
      CODE('javascript', 'self.onmessage = (e) =&gt; {\n  if (e.data.type === \'context\') grow(e.data.estimatedTokens);\n};') +
      '<p>This chat is sending it right now: each answer here adds to the count.</p>'],
    ['How expensive are these effects on a laptop GPU?',
      '<p>It depends on the script. They run in a Web Worker on an <code>OffscreenCanvas</code>, so drawing never blocks the chat itself, but the device still does the work.</p>' +
      '<p>Particle fields and line drawings are light. Full-screen WebGL shaders are the heaviest. If a device struggles, turn Canvas FX off for that mode, or switch it off for everyone with the <b>Enable Canvas FX</b> valve.</p>'],
    ['Can a script react to which model is selected?',
      '<p>Not from the engine: it doesn\'t send the model. With the <b>Enable Canvas Api Access</b> valve on, a script gets the user\'s token and the instance address, and can ask Open WebUI\'s API for anything the user can see, the models included.</p>' +
      '<p>Leave that valve off unless you trust every script you install.</p>'],
    ['How do I animate a gradient without it feeling busy?',
      '<p>Slow it down and narrow it. A longer animation speed (the slider is in seconds per cycle) and stops close together in hue read as a slow drift rather than motion.</p>' +
      '<p>Lowering the intensity helps too: the gradient stays behind the chat instead of competing with it.</p>'],
    ['Which gradients hold up in light mode?',
      '<p>Pale ones with little chroma. In Light, the surfaces are near white, so a saturated gradient shows through as a tint on everything, text included.</p>' +
      '<p>Keep a separate gradient for Light, or lower its intensity there. Each mode has its own.</p>'],
    ['Can I export a gradient I built back out as a preset?',
      '<p>Yes. Save it to the gradient gallery from the Gradient tab, and its export button downloads it as a preset file anyone can import. The whole gallery exports at once too.</p>'],
    ['What intensity works best for mesh gradients?',
      '<p>Lower than you\'d think. A mesh stacks several soft color points, so it reads stronger than a linear gradient at the same intensity.</p>' +
      '<p>Start in the middle of the slider and raise it until the colors reach the corners, then stop.</p>'],
    ['Can the gradient animate only when the chat is idle?',
      '<p>Not the gradient itself: once it animates, it runs all the time. A Canvas FX script can, though, since it hears about the chat through <code>context</code> messages. It can pause while the count is changing and drift once it settles.</p>'],
    ['Which containers get transparency applied?',
      '<p>The layout containers: the app\'s wrapper, the main area, and the gray panels inside them. Menus, dialogs and dropdowns sit outside those, so they keep their backgrounds, and the sign-in card stays solid.</p>' +
      '<p>Buttons, inputs and labels keep theirs too, so controls stay visible over any background.</p>'],
    ['How do I keep code blocks readable over an effect?',
      '<p>Give them a background of their own in the theme\'s custom CSS, a little lighter than the chat:</p>' +
      CODE('css', '.prose pre {\n  background: color-mix(in srgb, var(--color-gray-900) 85%, transparent);\n}') +
      '<p>The effect still shows around them, and the code reads on a calm surface.</p>'],
    ['Does the frosted sidebar cost much performance?',
      '<p>Some. A backdrop blur redraws whatever moves behind it, so it costs the most over an animation. On most devices it\'s fine.</p>' +
      '<p>If scrolling or the animation stutters, the <b>Sidebar Transparency</b> valve can make the sidebar solid.</p>'],
    ['How do I submit a preset to this gallery?',
      '<p>For a theme, <b>Submit to the gallery</b> in the Studio opens the submission form with your theme filled in. A bot checks it, screenshots it in every mode and links a preview of it, and it opens the pull request for you.</p>' +
      '<p>Canvas FX scripts, CSS snippets and gradients go in through a pull request on GitHub.</p>'],
    ['Can I preview a preset before importing it?',
      '<p>Yes. Every preset in the gallery opens a live preview like this one: themes on a mock Open WebUI in each mode, and animations you can move the mouse over. Start one in the Studio to change it before you take it home.</p>'],
    ['What makes a good Canvas FX submission?',
      '<p>One that runs well in a Web Worker and fits any theme:</p><ul>' +
      '<li>no <code>document</code> or <code>window</code>: the canvas comes with <code>init</code></li>' +
      '<li>a <code>requestAnimationFrame</code> loop, and a <code>heartbeat</code> message so the engine knows it\'s alive</li>' +
      '<li>handles <code>resize</code></li>' +
      '<li>its settings in a <code>CONFIG</code> object, so the designer can show them as sliders</li>' +
      '<li>colors from the theme\'s palette, so it suits every theme</li></ul>'],
  ].map(([q, body]) => ({ q, body }));

  function esc(str) {
    return String(str == null ? '' : str)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function buildMockSrcdoc(opts) {
    // opts: { modeKey, vars, customCSS, transparent }
    // Layout metrics mirror Open WebUI's components: 245px sidebar
    // (stores/index.ts), 13px sidebar rows, text-xs time-range labels,
    // max-w-3xl chat column, 0.9375rem prose (markdown-prose), size-7
    // rounded-2xl avatars, rounded-3xl gray-850 user bubbles, and the
    // translucent shadowed rounded-3xl input shell (MessageInput.svelte).
    const isLight = opts.modeKey === 'light' || opts.modeKey === 'her';
    const safeCSS = String(opts.customCSS || '').replace(/<\//g, '<\\/');
    const htmlClass = opts.modeKey === 'her' ? 'her' : isLight ? 'light' : 'dark';
    const dataTheme = opts.modeKey === 'oled' ? 'oled-dark' : htmlClass;
    // OLED renders on true black in Open WebUI (gray-800..950 forced to
    // #101010/#050505/#000/#000) — apply the same values over any palette.
    const varsOut = Object.assign({}, opts.vars);
    if (opts.modeKey === 'oled') {
      varsOut['--color-gray-800'] = '#101010';
      varsOut['--color-gray-850'] = '#050505';
      varsOut['--color-gray-900'] = '#000000';
      varsOut['--color-gray-950'] = '#000000';
    }
    const varLines = Object.entries(varsOut).map(([k, v]) => `  ${k}: ${v};`).join('\n');

    const bg = isLight ? '#ffffff' : 'var(--color-gray-900)';
    const sidebarBg = isLight ? 'var(--color-gray-50)' : 'var(--color-gray-950)';
    const textMain = isLight ? 'var(--color-gray-800)' : 'var(--color-gray-100)';
    const textSoft = isLight ? 'var(--color-gray-700)' : 'var(--color-gray-300)';
    const textMuted = isLight ? 'var(--color-gray-500)' : 'var(--color-gray-500)';
    const textFaint = isLight ? 'var(--color-gray-400)' : 'var(--color-gray-600)';
    const border = isLight ? 'var(--color-gray-100)' : 'var(--color-gray-800)';
    const borderSubtle = isLight
      ? 'color-mix(in srgb, var(--color-gray-100) 40%, transparent)'
      : 'color-mix(in srgb, var(--color-gray-850) 30%, transparent)';
    const bubble = isLight ? 'var(--color-gray-50)' : 'var(--color-gray-850)';
    const codeBg = isLight ? 'var(--color-gray-50)' : 'var(--color-gray-800)';
    const itemHover = isLight ? 'var(--color-gray-50)' : 'var(--color-gray-900)';
    const navHover = isLight
      ? 'color-mix(in srgb, var(--color-gray-50) 40%, transparent)'
      : 'color-mix(in srgb, var(--color-gray-800) 40%, transparent)';
    const activeItem = isLight ? 'rgb(0 0 0 / 0.035)' : 'rgb(255 255 255 / 0.045)';
    const iconHover = isLight ? 'var(--color-gray-100)' : 'var(--color-gray-800)';
    const ghostHover = isLight ? 'rgb(0 0 0 / 0.05)' : 'rgb(255 255 255 / 0.05)';
    // Chat prose colors are NOT themed in Open WebUI: the typography plugin
    // bakes its palette from @theme (tailwind.css:5-18) as literals at build
    // time, so --color-gray-* overrides never reach message text — only
    // utility classes (stats line, sidebar, follow-ups) follow the theme.
    // These literals are the baked values: prose body gray-700 / invert
    // body gray-300.
    const proseText = isLight ? 'oklch(0.42 0 0)' : 'oklch(0.85 0 0)';
    const proseBold = isLight ? 'oklch(0.2 0 0)' : '#fff';
    const ageText = isLight ? 'var(--color-gray-400)' : 'var(--color-gray-500)';
    const codeBlockBg = isLight ? '#ffffff' : '#000000';
    const inputBg = isLight
      ? 'color-mix(in srgb, #ffffff 5%, transparent)'
      : 'color-mix(in srgb, var(--color-gray-500) 5%, transparent)';

    const hasInnerFx = !!(opts.canvasScript || opts.gradient);
    const effectsOn = opts.transparent || hasInnerFx;
    const structural = effectsOn
      ? `
  ${opts.transparent ? 'body { background: transparent !important; }' : ''}
  .app, main, nav { background: transparent !important; }
  #sidebar { background: color-mix(in srgb, ${sidebarBg} 72%, transparent) !important; backdrop-filter: blur(14px); -webkit-backdrop-filter: blur(14px); }
  #message-input-container { background: color-mix(in srgb, ${isLight ? '#ffffff' : 'var(--color-gray-900)'} 72%, transparent) !important; }
  .chat-user .bubble { background: transparent; }`
      : '';
    // In-iframe gradient: body background layers, exactly like the designer's
    // gradient section (emitted before custom CSS so cascade order matches)
    const g = opts.gradient;
    const gradientCSS = g
      ? `body { ${g.background ? `background-color: ${g.background} !important;` : ''} background-image: ${g.backgroundImage} !important; background-attachment: fixed !important; ${g.animated ? `background-size: 300% 300% !important; animation: tdp-preview-gradient-shift ${g.speed}s ease infinite;` : ''} }
${g.animated ? '@keyframes tdp-preview-gradient-shift { 0% { background-position: 0% 50%; } 50% { background-position: 100% 50%; } 100% { background-position: 0% 50%; } }' : ''}`
      : '';

    return `<!DOCTYPE html>
<html class="${htmlClass}" data-theme="${dataTheme}">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
:root {
${varLines}
}
* { box-sizing: border-box; margin: 0; }
/* --app-text-scale is Open WebUI's UI Scale mechanism (app.css:27-36): the
   Interface-settings slider sets the var and html font-size scales every
   rem-based size. Px-based chrome (sidebar, nav) stays fixed, as upstream. */
:root { --app-text-scale: 1; }
html { font-size: calc(1rem * var(--app-text-scale, 1)); }
html, body { height: 100%; }
body { background: ${bg}; font-family: -apple-system, BlinkMacSystemFont, 'Inter', ui-sans-serif, 'Segoe UI', Roboto, sans-serif; color: ${proseText}; overflow: hidden; font-size: 0.9375rem; -webkit-font-smoothing: antialiased; }
svg { width: 1rem; height: 1rem; flex-shrink: 0; }
.app { display: flex; height: 100vh; position: relative; }

/* Sidebar.svelte: w-[var(--sidebar-width)]=245px, border-e, px-1 rows,
   rounded-xl px-2 py-1.5 nav items, size-4 icons, text-[13px] leading-5.
   Collapsing swaps it for the w-[42px] rail (Sidebar.svelte L806-974); the
   panel itself uses transition:slide{duration:250,axis:'x'}. */
/* Sidebar.svelte:985 — the expanded panel is bg-gray-50/70 dark:bg-gray-950/70
   on desktop (opaque only on mobile, see the media query below). Painting it
   fully opaque here made any theme-applied border-radius cut a hard wedge of
   body colour out of each corner. */
.sidebar-panel { width: 245px; flex-shrink: 0; background: color-mix(in srgb, ${sidebarBg} 70%, transparent); border-right: 1px solid ${borderSubtle}; padding: 6px 4px 4px; display: flex; flex-direction: column; overflow: hidden; transition: width 250ms ease, opacity 150ms ease; }
/* min-width:0 is required — flex items default to min-width:auto, which would
   floor the panel at its nowrap content width and defeat width:0 */
.app.collapsed .sidebar-panel { width: 0; min-width: 0; padding-left: 0; padding-right: 0; border-right-width: 0; opacity: 0; pointer-events: none; }

/* The collapsed rail: w-[42px] py-1 px-1, justify-between, border-e-[0.5px] */
/* Sidebar.svelte:808 — the rail carries no background class at all, just a
   hover tint, so whatever is behind the app shows straight through it */
.sidebar-rail { display: none; width: 42px; flex-shrink: 0; padding: 4px; flex-direction: column; justify-content: space-between; align-items: center; background: transparent; border-right: 0.5px solid ${borderSubtle}; color: ${isLight ? 'var(--color-gray-700)' : 'var(--color-gray-300)'}; overflow: hidden; cursor: pointer; transition: background 0.15s; }
.app.collapsed .sidebar-rail { display: flex; }
/* hover:bg-gray-50/30 dark:hover:bg-gray-800/30 — a tint, not a fill */
.sidebar-rail:hover { background: ${isLight ? 'color-mix(in srgb, var(--color-gray-50) 30%, transparent)' : 'color-mix(in srgb, var(--color-gray-800) 30%, transparent)'}; }
.rail-group { display: flex; flex-direction: column; align-items: center; }
/* size-8.5 / size-8 hit areas wrapping a size-[30px] rounded-lg hover target */
.rail-btn { width: 2rem; height: 2rem; display: flex; align-items: center; justify-content: center; cursor: pointer; }
.rail-btn.lg { width: 2.125rem; height: 2.125rem; }
.rail-btn > span { width: 1.875rem; height: 1.875rem; border-radius: 8px; display: flex; align-items: center; justify-content: center; transition: background 0.15s; }
.rail-btn:hover > span { background: ${navHover}; }
.rail-btn svg { width: 1rem; height: 1rem; }
/* the logo swaps to the sidebar-toggle glyph on hover (group-hover:hidden) */
.rail-logo img { width: 1.25rem; height: 1.25rem; border-radius: 50%; object-fit: cover; }
.rail-logo svg { display: none; }
.rail-logo:hover img { display: none; }
.rail-logo:hover svg { display: block; }
.rail-avatar { width: 1.375rem; height: 1.375rem; border-radius: 50%; background: linear-gradient(135deg, #6366f1, #ec4899); }
.brand { display: flex; align-items: center; gap: 2px; padding: 0 0 6px; color: ${textMain}; }
.brand-dot { width: 2.125rem; height: 2.125rem; border-radius: 12px; flex-shrink: 0; display: flex; align-items: center; justify-content: center; }
.brand-dot img { width: 1.25rem; height: 1.25rem; border-radius: 50%; object-fit: cover; }
.brand .brand-name { flex: 1; padding: 0 2px; font-size: 0.875rem; font-weight: 400; color: ${isLight ? 'var(--color-gray-700)' : 'var(--color-gray-200)'}; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.brand .panel-icon { width: 1.875rem; height: 1.875rem; border-radius: 8px; display: flex; align-items: center; justify-content: center; color: ${textMuted}; }
.brand .panel-icon svg { width: 1rem; height: 1rem; }
.brand-dot:hover, .brand .panel-icon:hover { background: ${navHover}; }
.side-item { display: flex; align-items: center; gap: 0.5rem; padding: 0.375rem 0.5rem; margin: 0 4px; border-radius: 12px; color: ${textSoft}; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-size: 0.8125rem; line-height: 1.25rem; }
.side-item svg { width: 1rem; height: 1rem; color: ${textSoft}; }
.side-item { cursor: pointer; transition: background 0.15s; }
.side-item:hover { background: ${navHover}; }
.side-scroll .side-item { padding: 6px 8px; }
.side-scroll .side-item:hover { background: ${itemHover}; }
.side-item.active, .side-scroll .side-item.active:hover { background: ${activeItem}; color: ${isLight ? 'var(--color-gray-800)' : 'var(--color-gray-200)'}; }
.side-item .label { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.side-item .age { flex-shrink: 0; padding-left: 8px; font-size: 0.625rem; color: ${ageText}; }
/* ChatItem.svelte: the menu sits absolute right-1 inset-y-0 mr-1.5 and is
   invisible until hover (or always, on the selected chat, which also swaps
   the time-ago indicator out and pads the title with pr-12) */
.side-scroll .side-item[data-chat] { position: relative; }
.chat-actions { position: absolute; top: 0; bottom: 0; right: 4px; margin-right: 6px; display: flex; align-items: center; visibility: hidden; }
.side-item:hover .chat-actions, .side-item.active .chat-actions { visibility: visible; }
.side-item:hover .age, .side-item.active .age { display: none; }
.side-item:hover .label, .side-item.active .label { padding-right: 20px; }
.chat-menu-btn { width: 1.25rem; height: 1.25rem; display: flex; align-items: center; justify-content: center; cursor: pointer; transition: color 0.15s; }
.chat-menu-btn svg { width: 0.875rem; height: 0.875rem; }
.chat-menu-btn:hover { color: ${isLight ? '#000' : '#fff'}; }
.side-item .add { margin-left: auto; color: ${textFaint}; }
.side-item .add svg { width: 0.75rem; height: 0.75rem; }
/* Section.svelte: text-xs gray-400/gray-500 header with a size-3.5 Plus */
.side-label { font-size: 0.75rem; color: ${isLight ? 'var(--color-gray-400)' : 'var(--color-gray-500)'}; padding: 0.75rem 0.25rem 0.25rem 0.625rem; display: flex; align-items: center; }
.side-label .add { margin-left: auto; color: inherit; }
.side-label .add svg { width: 0.875rem; height: 0.875rem; }
.side-label .add:hover { color: ${isLight ? 'var(--color-gray-500)' : 'var(--color-gray-400)'}; }
.side-pinned { display: flex; align-items: center; gap: 6px; margin: 0 4px; padding: 6px 8px; border-radius: 12px; color: ${textSoft}; font-size: 0.8125rem; line-height: 1.25rem; }
.side-pinned svg { width: 0.6875rem; height: 0.6875rem; }
.side-pinned:hover { background: ${itemHover}; }
.side-scroll { flex: 1; overflow-y: auto; overflow-x: hidden; display: flex; flex-direction: column; scrollbar-width: none; padding-top: 2px; }
.side-scroll::-webkit-scrollbar { display: none; }
.side-user { display: flex; align-items: center; gap: 8px; margin: 0 4px; padding: 6px 8px; border-radius: 12px; color: ${textMain}; font-weight: 400; font-size: 0.8125rem; line-height: 1.25rem; cursor: pointer; transition: background 0.15s; }
.side-user:hover { background: ${navHover}; }
.side-label .add, .side-pinned, .brand .panel-icon, .brand-dot { cursor: pointer; transition: background 0.15s, color 0.15s; }
.avatar { width: 1.375rem; height: 1.375rem; border-radius: 50%; background: linear-gradient(135deg, #6366f1, #ec4899); flex-shrink: 0; }
.avatar.sm { width: 1.25rem; height: 1.25rem; border-radius: 50%; }

main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
/* Navbar.svelte: pt-0.5 pb-1, pl-1.5 pr-1, title text-[15px] gray-700/300,
   size-6 rounded-lg icon buttons, and the -bottom-10 gradient scrim */
nav { padding: 2px 4px 4px 6px; display: flex; align-items: center; gap: 8px; color: ${textMain}; position: relative; z-index: 2; flex-shrink: 0; }
${opts.transparent ? '' : `nav::before { content: ''; position: absolute; inset: 0 0 -40px 0; background: linear-gradient(to bottom, color-mix(in srgb, ${bg} 90%, transparent), color-mix(in srgb, ${bg} 50%, transparent) 40%, transparent 97%); z-index: -1; pointer-events: none; }`}
#nav-title { padding: 4px 4px 4px 6px; font-size: 0.9375rem; font-weight: 400; color: ${isLight ? 'var(--color-gray-700)' : 'var(--color-gray-300)'}; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
nav .nav-btn { width: 1.5rem; height: 1.5rem; border-radius: 8px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; color: ${isLight ? 'var(--color-gray-500)' : 'var(--color-gray-400)'}; cursor: pointer; transition: background 0.15s, color 0.15s; }
nav .nav-btn svg { width: 1.125rem; height: 1.125rem; }
nav .nav-btn.controls svg { width: 1.25rem; height: 1.25rem; }
nav .nav-btn:hover { background: ${navHover}; color: ${isLight ? 'var(--color-gray-700)' : 'var(--color-gray-200)'}; }
nav .right { margin-left: auto; display: flex; align-items: center; gap: 8px; padding-right: 4px; }
/* Temporary Chat only renders when there's no chat id — i.e. on New Chat */
nav .right .temp-chat { display: none; }
nav .right.is-new .temp-chat { display: flex; }
/* Navbar.svelte:117 gates the title block (and the chat menu inside it) on
   chat?.id, so New Chat shows neither */
nav.is-new #nav-title, nav.is-new .nav-chat-menu { display: none; }

/* Messages.svelte: rows are max-w-[58rem] px-5 mb-3 inside a full-width scroller */
#messages-container { flex: 1; min-height: 0; overflow-x: hidden; overflow-y: auto; padding: 8px 0 6px; display: flex; flex-direction: column; width: 100%; scrollbar-width: thin; scrollbar-color: ${isLight ? 'var(--color-gray-300)' : 'var(--color-gray-700)'} transparent; }
/* A reply still streaming has no stats line or actions yet */
.message-listitem.streaming .ai-stats, .message-listitem.streaming .msg-actions { visibility: hidden; }
.tdp-wait { display: inline-flex; gap: 4px; padding: 8px 0; }
.tdp-wait i { width: 6px; height: 6px; border-radius: 9999px; background: currentColor; animation: tdp-typing 1.2s infinite ease-in-out; }
.tdp-wait i:nth-child(2) { animation-delay: 0.15s; }
.tdp-wait i:nth-child(3) { animation-delay: 0.3s; }
#messages-container > .message-listitem { max-width: 58rem; width: 100%; margin: 0 auto 12px; padding: 0 20px; }
/* The gallery's floating .preview-topbar overlays the stage, so the opening
   message has to start below it. The navbar deliberately stays put and sits
   under the bar. Only the first row is offset — the New Chat placeholder
   centres itself and must not be pushed off-centre. The value is measured at
   runtime (the bar wraps on narrow screens); this default covers desktop. */
#messages-container > .message-listitem:first-child { margin-top: var(--tdp-first-row-offset, 37px); }
/* Row anatomy mirrors the real DOM so preset CSS lands identically:
   Message.svelte row (.message-listitem) > flex wrapper (.msg, with
   UserMessage's .user-message class) > .chat-user / .chat-assistant, which
   upstream sit INSIDE the flex column (UserMessage:168, ResponseMessage:676)
   as w-full min-w-full blocks — not on the outer row. */
.msg { display: flex; width: 100%; }
.chat-user, .chat-assistant { width: 100%; min-width: 100%; }
.bubble-row { display: flex; justify-content: flex-end; padding-bottom: 4px; }
.chat-user .bubble { max-width: 90%; background: ${bubble}; border-radius: 24px; padding: 0.375rem 1rem; font-size: 1rem; line-height: 1.625; color: ${proseText}; }
/* UserMessage.svelte: Edit + Copy in a justify-end row. Upstream they are
   hover-gated (invisible group-hover:visible); the mock keeps them visible
   so the preview shows the full anatomy without interaction. */
.user-actions { display: flex; align-items: center; justify-content: flex-end; color: ${isLight ? 'var(--color-gray-600)' : 'var(--color-gray-500)'}; }
.ai-avatar { width: 1.75rem; height: 1.75rem; flex-shrink: 0; margin: 2px 8px 0 0; }
.ai-avatar img { width: 100%; height: 100%; border-radius: 16px; object-fit: cover; display: block; }
.ai-col { flex: 1; min-width: 0; padding-left: 4px; }
.ai-model { font-size: 1rem; font-weight: 400; color: ${isLight ? '#000' : '#fff'}; margin-bottom: 1px; line-height: 1.4; }
.ai-stats { font-size: 1rem; color: ${textFaint}; margin-bottom: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
/* common/Collapsible.svelte reasoning trigger: w-fit py-1 text-[0.9375rem]
   text-gray-500 hover:text-gray-700 dark:hover:text-gray-300, size-3 chevron
   at stroke-width 2.75; expanded slot content wrapped in mb-1.5 */
.thought { width: fit-content; padding: 4px 0; display: flex; align-items: center; gap: 8px; font-size: 1rem; color: var(--color-gray-500); cursor: pointer; transition: color 0.15s; }
.thought:hover { color: ${isLight ? 'var(--color-gray-700)' : 'var(--color-gray-300)'}; }
.thought svg { width: 0.75rem; height: 0.75rem; transform: translateY(1px); transition: transform 0.15s; }
.thought.open svg { transform: translateY(1px) rotate(180deg); }
.thought-body { margin-bottom: 6px; }
.thought-body[hidden] { display: none; }
/* The DOM class in Open WebUI is markdown-prose (app.css:124); "prose" is
   only a Tailwind @apply and never appears as a literal class. Preset CSS
   targeting .prose must miss here exactly as it misses in the app. */
.markdown-prose { font-size: 1rem; line-height: 1.625; color: ${proseText}; }
.markdown-prose p { margin-bottom: 8px; }
.markdown-prose p:last-child { margin-bottom: 0; }
.markdown-prose b { color: ${proseBold}; font-weight: 500; }
.markdown-prose code { background: ${codeBg}; padding: 2px 6px; border-radius: 6px; font-family: ui-monospace, 'JetBrains Mono', monospace; font-size: 0.8em; color: ${textMain}; }
/* CodeBlock.svelte: rounded-2xl border my-0.5, header py-1.5 px-3.5 text-xs */
.code-block { border-radius: 16px; border: 1px solid ${borderSubtle}; background: ${codeBlockBg}; margin: 12px 0; overflow: hidden; }
.code-block-header { display: flex; align-items: center; justify-content: space-between; padding: 6px 14px; font-size: 0.75rem; color: ${isLight ? '#000' : '#fff'}; }
.code-block-header .cb-action { cursor: pointer; padding: 2px 6px; border-radius: 6px; font-size: 0.75rem; }
.code-block-header .cb-action:hover { opacity: 0.7; }
.markdown-prose pre { background: ${codeBlockBg}; padding: 10px 20px 16px; font-family: ui-monospace, 'JetBrains Mono', monospace; font-size: 0.875rem; line-height: 1.5; overflow: hidden; margin: 0; color: ${proseText}; }
.markdown-prose ul, .markdown-prose ol { margin: 8px 0 8px 22px; }
.markdown-prose li { margin: 2px 0; }
.markdown-prose blockquote { border-left: 2px solid ${border}; padding: 2px 0 2px 12px; color: ${isLight ? 'oklch(0.2 0 0)' : 'oklch(0.94 0 0)'}; margin: 12px 0; }
.markdown-prose a { color: ${proseBold}; text-decoration: underline; cursor: pointer; }
.markdown-prose table { border-collapse: collapse; margin: 6px 0 10px; font-size: 0.78125rem; }
.markdown-prose th, .markdown-prose td { border: 1px solid ${border}; padding: 5px 10px; text-align: left; }
.markdown-prose th { background: ${codeBg}; color: ${proseBold}; font-weight: 600; }
.markdown-prose h4 { font-size: 0.9375rem; font-weight: 600; color: ${proseBold}; margin: 8px 0 4px; }
/* Placeholder.svelte: max-w-[58rem] translate-y-6 centered block, size-10
   rounded-2xl model image, text-2xl name; Suggestions.svelte rows are
   borderless px-2.5 py-1.5 rounded-lg with text-sm / text-xs lines */
.placeholder { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; max-width: 58rem; width: 100%; margin: 0 auto; padding: 0 8px 24px; transform: translateY(24px); }
/* Logo, model name and greeting left-align with the Suggested column — the
   head shares the suggestion block's width and padding so their edges meet */
.ph-head { width: min(42rem, 100%); padding: 0 20px; display: flex; flex-direction: column; align-items: flex-start; text-align: left; }
.ph-logo { width: 2.5rem; height: 2.5rem; margin-bottom: 2px; }
.ph-logo img { width: 100%; height: 100%; border-radius: 16px; object-fit: cover; display: block; }
.ph-model { font-size: 1.875rem; line-height: 2.25rem; font-weight: 400; color: ${isLight ? 'var(--color-gray-800)' : 'var(--color-gray-100)'}; max-width: 36rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ph-sub { font-size: 1.875rem; line-height: 2.25rem; font-weight: 400; color: ${isLight ? 'var(--color-gray-600)' : 'var(--color-gray-400)'}; margin: 0 0 22px; }
.ph-suggest { width: min(42rem, 100%); padding: 0 20px; }
.ph-label { font-size: 0.75rem; line-height: 1.4; color: ${isLight ? 'var(--color-gray-600)' : 'var(--color-gray-400)'}; margin-bottom: 4px; display: flex; align-items: center; gap: 4px; }
.ph-label svg { width: 0.875rem; height: 0.875rem; }
.ph-grid { display: grid; grid-template-columns: 1fr 1fr; align-items: start; }
.ph-card { border-radius: 8px; padding: 6px 10px; cursor: pointer; background: transparent; transition: color 0.15s; text-align: left; }
.ph-card b { display: block; font-size: 1rem; line-height: 1.375; font-weight: 400; color: ${isLight ? 'var(--color-gray-700)' : 'var(--color-gray-300)'}; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ph-card span { display: block; font-size: 0.8125rem; line-height: 1.375; font-weight: 400; color: ${isLight ? 'var(--color-gray-600)' : 'var(--color-gray-400)'}; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ph-card:hover b { color: ${isLight ? 'var(--color-gray-950)' : '#fff'}; }
.ph-card:hover span { color: ${isLight ? 'var(--color-gray-900)' : 'var(--color-gray-100)'}; }
/* ResponseMessage.svelte action row: text-gray-600 dark:text-gray-500 mt-0.5,
   buttons p-1.5 rounded-lg hover:bg-black/5 dark:hover:bg-white/5 */
.msg-actions { display: flex; align-items: center; justify-content: flex-start; margin-top: 2px; color: ${isLight ? 'var(--color-gray-600)' : 'var(--color-gray-500)'}; }
.msg-action { padding: 6px; border-radius: 8px; display: flex; cursor: pointer; transition: background 0.15s, color 0.15s; }
.msg-action:hover { background: ${ghostHover}; color: ${isLight ? '#000' : '#fff'}; }
.msg-actions svg { width: 1rem; height: 1rem; }
/* UserMessage.svelte:580 — Delete is p-1 rounded-sm and, unlike the p-1.5
   rounded-lg Edit/Copy beside it, takes no background on hover */
.msg-action.delete { padding: 4px; border-radius: 2px; }
.msg-action.delete:hover { background: transparent; color: ${isLight ? '#000' : '#fff'}; }

/* Sibling branches: one variant visible at a time, switched by the pager */
.branch { display: none; }
.branch.active { display: block; }
.branch-pager { display: flex; align-items: center; }
.branch-nav { display: flex; align-self: center; padding: 4px; border-radius: 6px; cursor: pointer; transition: background 0.15s, color 0.15s; }
.branch-nav:hover { background: ${ghostHover}; color: ${isLight ? '#000' : '#fff'}; }
.branch-nav svg { width: 0.875rem; height: 0.875rem; }
.branch-count { font-size: 0.875rem; letter-spacing: 0.1em; font-weight: 400; align-self: center; white-space: nowrap; color: ${isLight ? 'var(--color-gray-900)' : 'var(--color-gray-100)'}; }

/* ResponseMessage/FollowUps.svelte, rendered in a my-2.5 wrapper after the
   action row and only on the last message: an mt-4 block with a text-sm
   heading and text-sm rows separated by hairlines */
.followups-wrap { margin: 10px 0; }
.followups { margin-top: 16px; }
.followups-title { font-size: 0.9375rem; font-weight: 400; color: ${proseText}; }
.followups-list { display: flex; flex-direction: column; gap: 4px; margin-top: 6px; }
.followup { padding: 4px 0; font-size: 0.9375rem; text-align: left; color: var(--color-gray-500); cursor: pointer; transition: color 0.15s; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.followup:hover { color: ${isLight ? '#000' : '#fff'}; }
.followups-list hr { border: none; border-top: 1px solid ${borderSubtle}; margin: 0; }

/* MessageInput.svelte: max-w-[58rem] px-2.5 mx-auto shell, shadow-lg
   rounded-3xl border-gray-100/30 dark:border-gray-850/30 bg-white/5
   dark:bg-gray-500/5 backdrop-blur-sm */
.input-wrap { padding: 0 10px; max-width: 58rem; width: 100%; margin: 0 auto; }
.input-wrap > #message-input-container { background: ${inputBg}; backdrop-filter: blur(4px); -webkit-backdrop-filter: blur(4px); border: 1px solid ${borderSubtle}; border-radius: 24px; box-shadow: 0 10px 15px -3px rgb(0 0 0 / 0.1), 0 4px 6px -4px rgb(0 0 0 / 0.1); padding: 10px 10px 8px 18px; display: flex; flex-direction: column; gap: 8px; transition: border-color 0.15s; }
.input-wrap > #message-input-container:hover { border-color: ${isLight ? 'var(--color-gray-200)' : 'var(--color-gray-800)'}; }
/* The real chat input is a ProseMirror contenteditable (RichTextInput,
   id="chat-input", .input-prose = !text-[0.9375rem]) — NOT a <textarea>.
   Preset CSS that skins textarea/input fields therefore never touches the
   chat input in Open WebUI, and must not touch it here either. */
#chat-input { outline: none; width: 100%; min-height: 24px; padding-top: 2px; font-size: 1rem; line-height: 1.5; color: ${textMain}; }
#chat-input p { margin: 0; }
/* app.css:418 — placeholder is a ::before on the empty first paragraph */
.ProseMirror p.is-editor-empty:first-child::before { content: attr(data-placeholder); float: left; color: ${isLight ? '#676767' : '#757575'}; pointer-events: none; height: 0; }
/* MessageInput.svelte controls: PlusAlt/Component sit in size-[1.875rem]
   rounded-full buttons, Dictate is rounded-full p-1.5 mr-0.5, and with an
   empty prompt the trailing control is Voice mode — bg-black dark:bg-white
   rounded-full p-[5px] — not the submit arrow. */
.input-row { display: flex; align-items: center; gap: 2px; }
.input-row .icon-btn { width: 1.875rem; height: 1.875rem; border-radius: 9999px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; color: ${isLight ? 'var(--color-gray-700)' : '#fff'}; cursor: pointer; transition: background 0.15s; }
.input-row .icon-btn:hover { background: ${iconHover}; }
.input-row .icon-btn svg { width: 1.125rem; height: 1.125rem; }
.input-row .icon-btn:first-child svg { width: 1.25rem; height: 1.25rem; }
.input-row .spacer { flex: 1; }
/* ModelSelector in the input row (MessageInput.svelte L2223-2231): a
   max-w-[13rem] trigger — rounded-lg pl-2 pr-1.5 py-1 gap-1.5 text-[13px]
   text-gray-600 dark:text-gray-300 — with a truncating label and a
   size-2.5 ChevronDown at stroke-width 2.5 */
.model-select { display: flex; align-items: center; gap: 6px; min-width: 0; max-width: 13rem; padding: 4px 6px 4px 8px; border-radius: 8px; font-size: 0.8125rem; font-weight: 400; color: ${isLight ? 'var(--color-gray-600)' : 'var(--color-gray-300)'}; cursor: pointer; transition: background 0.1s, color 0.1s; }
.model-select:hover { background: ${navHover}; color: ${isLight ? 'var(--color-gray-700)' : 'var(--color-gray-200)'}; }
.model-select .name { min-width: 0; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.model-select svg { width: 0.625rem; height: 0.625rem; flex-shrink: 0; margin-left: 4px; }
.mic-btn { padding: 6px; margin-right: 2px; border-radius: 9999px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; color: ${isLight ? 'var(--color-gray-600)' : 'var(--color-gray-300)'}; cursor: pointer; transition: color 0.15s; }
.mic-btn svg { width: 1.125rem; height: 1.125rem; }
.mic-btn:hover { color: ${isLight ? 'var(--color-gray-700)' : 'var(--color-gray-200)'}; }
/* The real Voice mode button carries bg-black/dark:bg-white as literal
   classes (MessageInput.svelte:2306) — presets style it through those
   (e.g. Astral Projection's .bg-black gradient), so the mock's button
   must carry the same class string. Styled via .call-button so the class
   tokens stay purely selector bait, like the bubble's bg-gray classes. */
.call-button { padding: 5px; border-radius: 9999px; border: none; background: ${isLight ? '#000' : '#fff'}; color: ${isLight ? '#fff' : '#000'}; display: flex; align-items: center; justify-content: center; flex-shrink: 0; cursor: pointer; transition: background 0.15s; }
.call-button svg { width: 1.25rem; height: 1.25rem; }
.call-button:hover { background: ${isLight ? 'var(--color-gray-900)' : 'var(--color-gray-100)'}; }
/* The divider between More and Integrations (MessageInput.svelte):
   w-[0.0625rem] h-4 mx-1 bg-gray-200/50 dark:bg-gray-800/50 */
.input-divider { width: 1px; height: 1rem; margin: 0 4px; flex-shrink: 0; background: color-mix(in srgb, var(--color-gray-${isLight ? '200' : '800'}) 50%, transparent); }
.input-pills { display: flex; align-items: center; gap: 6px; margin-left: 4px; min-width: 0; }
.input-pills:empty { display: none; }
/* Selected tools: Wrench size-4 + count, px-1 gap-1 text-gray-600 dark:text-gray-300 */
.tools-pill { display: flex; align-items: center; gap: 4px; padding: 0 4px; border-radius: 8px; font-size: 0.875rem; color: ${isLight ? 'var(--color-gray-600)' : 'var(--color-gray-300)'}; cursor: pointer; transition: color 0.15s; }
.tools-pill:hover { color: ${isLight ? 'var(--color-gray-700)' : 'var(--color-gray-200)'}; }
.tools-pill svg { width: 1rem; height: 1rem; }
/* Web Search / Image / Code Interpreter while on: group p-[0.375rem]
   rounded-full border, text-sky-500 bg-sky-50 border-sky-200/40, dark
   text-sky-300 bg-sky-400/10 border-sky-500/20, with an XMark on hover */
.feature-pill { display: flex; align-items: center; gap: 6px; padding: 0.375rem; border-radius: 9999px; cursor: pointer; transition: background-color 0.3s, color 0.3s; ${isLight
    ? 'color: oklch(68.5% 0.169 237.323); background: oklch(97.7% 0.013 236.62); border: 1px solid oklch(90.1% 0.058 230.902 / 0.4);'
    : 'color: oklch(82.8% 0.111 230.318); background: oklch(74.6% 0.16 232.661 / 0.1); border: 1px solid oklch(68.5% 0.169 237.323 / 0.2);'} }
.feature-pill:hover { background: ${isLight ? 'oklch(95.1% 0.026 236.824)' : 'oklch(50% 0.134 242.749 / 0.1)'}; }
.feature-pill svg { width: 1rem; height: 1rem; }
.feature-pill .terminal-icon svg { width: 0.875rem; height: 0.875rem; }
.feature-pill .x { display: none; }
.feature-pill:hover .x { display: flex; }

/* DropdownMenu.svelte, portaled to <body> by Dropdown.svelte: rounded-xl p-0.5
   border border-gray-100 dark:border-gray-800 bg-white dark:bg-gray-850
   dark:text-white shadow-lg, fixed at z-9999, flyAndScale in over 200ms */
.tdp-menu { position: fixed; z-index: 9999; display: flex; flex-direction: column; padding: 2px; border-radius: 12px; border: 1px solid ${border}; background: ${isLight ? '#fff' : 'var(--color-gray-850)'}; color: ${isLight ? textMain : '#fff'}; box-shadow: 0 10px 15px -3px rgb(0 0 0 / 0.1), 0 4px 6px -4px rgb(0 0 0 / 0.1); overflow: hidden; font-size: 0.8125rem; opacity: 0; transform: translateY(-8px) scale(0.95); transition: opacity 0.2s cubic-bezier(0.33, 1, 0.68, 1), transform 0.2s cubic-bezier(0.33, 1, 0.68, 1); }
.tdp-menu.in { opacity: 1; transform: none; }
/* InputMenu w-70 max-h-72; IntegrationsMenu min-w-70 max-w-70 max-h-72 */
.tdp-menu.input-menu, .tdp-menu.integrations-menu { width: 17.5rem; max-height: 18rem; }
.tdp-view { display: flex; flex-direction: column; min-height: 0; max-height: calc(18rem - 4px); overflow-y: auto; overflow-x: hidden; scrollbar-width: thin; }
.tdp-view.from-left { animation: tdp-fly-left 0.15s cubic-bezier(0.33, 1, 0.68, 1); }
.tdp-view.from-right { animation: tdp-fly-right 0.15s cubic-bezier(0.33, 1, 0.68, 1); }
@keyframes tdp-fly-left { from { opacity: 0; transform: translateX(-20px); } }
@keyframes tdp-fly-right { from { opacity: 0; transform: translateX(20px); } }
/* Rows: flex gap-2 w-full items-center h-[1.6875rem] px-2 text-[0.8125rem]
   font-normal rounded-xl hover:bg-gray-50/40 dark:hover:bg-gray-800/40 */
.tdp-row { display: flex; align-items: center; gap: 8px; width: 100%; height: 1.6875rem; padding: 0 8px; flex-shrink: 0; border: 0; border-radius: 12px; background: transparent; color: inherit; font: inherit; font-size: 0.8125rem; font-weight: 400; text-align: left; cursor: pointer; user-select: none; }
.tdp-row:hover, .tdp-row.hl { background: ${navHover}; }
.tdp-row > svg, .tdp-row .ico svg { width: 1rem; height: 1rem; flex-shrink: 0; }
.tdp-row .ico { display: flex; flex-shrink: 0; }
.tdp-row .ico.sm svg, .tdp-row > svg.sm { width: 0.875rem; height: 0.875rem; }
.tdp-row .label { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tdp-row .count { margin-left: 2px; color: var(--color-gray-500); }
.tdp-row .chev { display: flex; color: var(--color-gray-500); }
.tdp-row .chev svg { width: 1rem; height: 1rem; }
.tdp-row.back { justify-content: flex-start; }
.tdp-row .doc svg { width: 0.875rem; height: 0.875rem; }
.tdp-row.item .label { color: ${isLight ? '#000' : 'var(--color-gray-100)'}; }
/* SearchInput.svelte: my-0.5 ml-2 mr-0.5 h-[1.6875rem] gap-2, Search size-3.5 */
.tdp-search { display: flex; align-items: center; gap: 8px; height: 1.6875rem; margin: 2px 2px 2px 8px; flex-shrink: 0; }
.tdp-search svg { width: 0.875rem; height: 0.875rem; flex-shrink: 0; }
.tdp-search input { width: 100%; min-width: 0; border: 0; outline: none; background: transparent; color: inherit; font: inherit; font-size: 0.8125rem; font-weight: 400; }
.tdp-search input::placeholder { color: ${isLight ? 'var(--color-gray-400)' : 'var(--color-gray-500)'}; }
.tdp-empty { text-align: center; font-size: 0.75rem; color: var(--color-gray-500); padding: 12px 0; }
.tdp-kb-files { display: flex; flex-direction: column; gap: 2px; padding-left: 12px; margin-bottom: 4px; }
.tdp-row .expand { display: flex; margin-left: 8px; opacity: 0.5; transition: opacity 0.15s; }
.tdp-row .expand:hover { opacity: 1; }
.tdp-row .expand svg { width: 0.75rem; height: 0.75rem; }
/* Switch.svelte: a 28x16 track, bg-gray-900 dark:bg-white when on and
   bg-gray-300 dark:bg-gray-700 when off, with a 12px thumb at 2px or 14px */
.tdp-switch { position: relative; width: 28px; height: 16px; margin: 0 1px; flex-shrink: 0; border-radius: 9999px; background: ${isLight ? 'var(--color-gray-300)' : 'var(--color-gray-700)'}; transition: background-color 0.15s; }
.tdp-switch::after { content: ''; position: absolute; top: 2px; left: 2px; width: 12px; height: 12px; border-radius: 9999px; background: ${isLight ? '#fff' : 'var(--color-gray-500)'}; transition: left 0.15s, background-color 0.15s; }
.tdp-switch.on { background: ${isLight ? 'var(--color-gray-900)' : '#fff'}; }
.tdp-switch.on::after { left: 14px; background: ${isLight ? '#fff' : '#000'}; }

/* ModelSelector/Selector.svelte: w-[20rem] rounded-xl border p-0.5 shadow-lg,
   opened above the trigger with its right edge on the trigger's (bottom-end,
   flipped, offset 2) */
.tdp-menu.model-menu { width: 20rem; max-width: calc(100vw - 1rem); }
.model-menu .compare { display: flex; align-items: center; justify-content: center; width: 1.375rem; height: 1.375rem; flex-shrink: 0; border-radius: 8px; color: ${isLight ? 'var(--color-gray-500)' : 'var(--color-gray-400)'}; cursor: pointer; transition: background-color 0.1s, color 0.1s; }
.model-menu .compare:hover { background: ${navHover}; color: ${isLight ? 'var(--color-gray-900)' : 'var(--color-gray-100)'}; }
.model-menu .compare.on { background: ${isLight ? 'var(--color-gray-50)' : 'color-mix(in srgb, var(--color-gray-800) 60%, transparent)'}; color: ${isLight ? 'var(--color-gray-700)' : 'var(--color-gray-200)'}; }
.model-menu .compare svg { width: 0.75rem; height: 0.75rem; }
.model-list { min-height: 0; max-height: 18rem; overflow-y: auto; scrollbar-width: thin; }
/* ModelItem.svelte: h-8 rounded-xl px-2 text-[0.8125rem] text-gray-700
   dark:text-gray-100, selected bg-gray-50/70 dark:bg-gray-800/60, a size-4
   round avatar, EllipsisHorizontal and a size-3 Check */
.model-item { display: flex; align-items: center; width: 100%; height: 2rem; padding: 0 8px; border-radius: 12px; font-size: 0.8125rem; color: ${isLight ? 'var(--color-gray-700)' : 'var(--color-gray-100)'}; cursor: pointer; user-select: none; transition: background-color 75ms; }
.model-item:hover { background: ${navHover}; }
.model-item.selected, .model-item.hl { background: color-mix(in srgb, var(--color-gray-${isLight ? '50' : '800'}) ${isLight ? '70' : '60'}%, transparent); }
.model-item .main { display: flex; align-items: center; gap: 8px; min-width: 0; flex: 1; overflow: hidden; }
.model-item img { width: 1rem; height: 1rem; border-radius: 9999px; flex-shrink: 0; }
.model-item .name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.model-item .size { flex-shrink: 0; font-size: 0.6875rem; color: ${isLight ? 'var(--color-gray-500)' : 'var(--color-gray-400)'}; }
.model-item .end { display: flex; align-items: center; gap: 6px; margin-left: auto; padding-left: 8px; flex-shrink: 0; }
.model-item .end svg { width: 1rem; height: 1rem; }
.model-item .end .tick svg { width: 0.75rem; height: 0.75rem; }
.model-empty { display: flex; align-items: center; min-height: 2rem; padding: 0 8px; border-radius: 12px; font-size: 0.8125rem; color: ${isLight ? 'var(--color-gray-700)' : 'var(--color-gray-100)'}; }
.model-foot { display: flex; justify-content: flex-end; padding: 4px 8px; line-height: 1; flex-shrink: 0; }
.model-foot span { font-size: 0.65rem; color: var(--color-gray-500); cursor: pointer; text-underline-offset: 2px; }
.model-foot span:hover { color: ${isLight ? 'var(--color-gray-700)' : 'var(--color-gray-300)'}; text-decoration: underline; }
.model-menu .pb { height: 4px; flex-shrink: 0; }

/* ---- Sidebar sections (Sidebar/Section.svelte) fold, with a chevron that
   shows on hover and turns 90deg while open ---- */
.side-section[data-section] > .side-label { cursor: pointer; transition: color 0.1s; }
.side-section > .side-label:hover { color: ${isLight ? 'var(--color-gray-500)' : 'var(--color-gray-400)'}; }
.sec-chev { display: flex; margin-left: 4px; opacity: 0; transition: opacity 0.1s, transform 0.1s; }
.sec-chev svg { width: 0.6875rem; height: 0.6875rem; }
.side-label:hover .sec-chev { opacity: 1; }
.side-section.open > .side-label .sec-chev { transform: rotate(90deg); }
.side-section > .side-section-body { display: none; }
.side-section.open > .side-section-body { display: block; }
.side-label .sec-more svg { width: 0.875rem; height: 0.875rem; }
.side-label .add + .add { margin-left: 2px; }
.side-label.time-label { cursor: default; }
.side-note { cursor: default; }
/* ChannelItem.svelte: p-1 rounded-xl, a size-3 Hashtag (size-3.5 Lock), the
   unread count badge and a Settings gear that shows on hover */
.channel-item { display: flex; align-items: center; gap: 4px; margin: 0 4px; padding: 4px; border-radius: 12px; font-size: 0.8125rem; line-height: 1.25rem; color: ${isLight ? 'var(--color-gray-600)' : 'var(--color-gray-400)'}; cursor: pointer; user-select: none; transition: background 0.15s; }
.channel-item:hover { background: ${itemHover}; }
.channel-item.active { background: ${isLight ? 'var(--color-gray-100)' : 'var(--color-gray-900)'}; }
.channel-item.unread { color: ${isLight ? '#000' : '#fff'}; }
.channel-item .ch-ico { width: 1rem; height: 1rem; margin-left: 4px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
.channel-item .ch-ico svg { width: 0.75rem; height: 0.75rem; }
.channel-item .ch-ico.lock svg { width: 0.875rem; height: 0.875rem; }
.channel-item .label { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.unread-badge { display: inline-flex; align-items: center; justify-content: center; height: 1rem; min-width: 1rem; padding: 0 4px; border-radius: 6px; flex-shrink: 0; font-size: 0.625rem; font-weight: 600; line-height: 1rem; ${isLight ? 'background: oklch(68.5% 0.169 237.323 / 0.1); color: oklch(58.8% 0.158 241.966);' : 'background: oklch(74.6% 0.16 232.661 / 0.1); color: oklch(82.8% 0.111 230.318);'} }
.channel-item .ch-settings { display: flex; padding: 2px; margin: 0 4px 0 2px; border-radius: 8px; opacity: 0; color: ${isLight ? 'inherit' : 'var(--color-gray-300)'}; transition: opacity 0.15s; }
.channel-item:hover .ch-settings { opacity: 1; }
.channel-item .ch-settings svg { width: 0.875rem; height: 0.875rem; }
/* RecursiveFolder.svelte: py-1 px-1.5 rounded-xl, the Folder icon turning
   into a chevron on hover, children behind the same left rule as Pinned */
.folder-row { display: flex; align-items: center; gap: 6px; margin: 0 4px; padding: 4px 6px; border-radius: 12px; font-size: 0.8125rem; line-height: 1.25rem; color: ${textSoft}; cursor: pointer; transition: background 0.15s; }
.folder-row:hover { background: ${itemHover}; }
.folder-row svg { width: 0.875rem; height: 0.875rem; flex-shrink: 0; }
.folder-row .folder-chev { display: none; }
.folder-row .folder-chev svg { width: 0.75rem; height: 0.75rem; transition: transform 0.15s; }
.folder-row:hover .folder-ico { display: none; }
.folder-row:hover .folder-chev { display: flex; width: 0.875rem; justify-content: center; }
.side-folder.open .folder-chev svg { transform: rotate(90deg); }
.folder-children, .pinned-list { margin-left: 16px; padding-left: 4px; margin-top: 1px; border-left: 1px solid ${isLight ? 'var(--color-gray-100)' : 'var(--color-gray-900)'}; display: none; flex-direction: column; }
.side-folder.open .folder-children, .side-pinned-wrap.open .pinned-list { display: flex; }
.folder-children .side-item, .pinned-list .side-item { margin: 0; }
/* common/Folder.svelte header: py-1.5 pl-2 gap-1.5 text-xs text-gray-500,
   a size-3 ChevronDown/ChevronRight swapped, not rotated */
.side-pinned { gap: 6px; padding: 6px 8px; font-size: 0.75rem; color: var(--color-gray-500); cursor: pointer; }
.side-pinned .pin-chev { display: flex; padding: 1px; }
.side-pinned .pin-chev svg { width: 0.75rem; height: 0.75rem; }
.side-pinned-wrap { margin-bottom: 4px; }
/* ChatItem.svelte: a size-1.5 sky dot for unread chats */
.unread-dot { width: 6px; height: 6px; border-radius: 9999px; background: oklch(68.5% 0.169 237.323); flex-shrink: 0; margin-right: 2px; }
.side-item.unread .label { color: ${isLight ? 'var(--color-gray-800)' : 'var(--color-gray-200)'}; }
.side-item.menu-open { background: ${activeItem}; }
.side-item.menu-open .chat-actions { visibility: visible; }
.side-item.menu-open .age { display: none; }
.ap-add { display: flex; align-items: center; justify-content: center; gap: 8px; width: 100%; margin: 4px 0 20px; cursor: pointer; }
.ap-add svg { width: 0.75rem; height: 0.75rem; }
.ap-switch { justify-content: space-between; margin-top: 4px; }
.side-item .rename { flex: 1; min-width: 0; margin-right: 40px; border: 0; outline: none; background: transparent; color: inherit; font: inherit; }
/* Avatars carry the green status dot (enable_user_status) */
.avatar-wrap { position: relative; display: flex; flex-shrink: 0; }
.status-dot { position: absolute; right: -2px; bottom: -2px; width: 10px; height: 10px; border-radius: 9999px; background: oklch(72.3% 0.219 149.579); border: 2px solid ${isLight ? '#fff' : 'var(--color-gray-900)'}; }
.side-user .avatar { width: 1.375rem; height: 1.375rem; }
.side-user { gap: 12px; }

/* ---- More dropdowns (common/DropdownMenu.svelte): ChatMenu, UserMenu, the
   Chats header's More, submenus and the reaction picker ---- */
.tdp-menu.chat-menu { min-width: 12.5rem; }
.tdp-menu.user-menu { font-size: 0.75rem; }
.tdp-menu.chats-more { min-width: 10.625rem; }
.tdp-menu.submenu { min-width: 10rem; max-width: 200px; z-index: 10000; }
.tdp-menu .mi { display: flex; align-items: center; gap: 8px; width: 100%; height: 1.6875rem; padding: 0 8px; border: 0; border-radius: 12px; background: transparent; color: inherit; font: inherit; font-size: 0.8125rem; text-align: left; cursor: pointer; user-select: none; flex-shrink: 0; transition: background 0.15s; }
.tdp-menu .mi svg { width: 0.875rem; height: 0.875rem; flex-shrink: 0; }
.tdp-menu .mi .label { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chat-menu .mi:hover, .submenu .mi:hover, .chats-more .mi:hover { background: ${isLight ? 'var(--color-gray-100)' : 'var(--color-gray-900)'}; }
.user-menu .mi:hover, .user-menu .mi.hl { background: color-mix(in srgb, var(--color-gray-${isLight ? '50' : '800'}) 60%, transparent); }
.chat-menu .mi.sub.hl { background: ${isLight ? 'var(--color-gray-100)' : 'var(--color-gray-900)'}; }
.tdp-menu hr { border: 0; border-top: 1px solid color-mix(in srgb, var(--color-gray-${isLight ? '50' : '800'}) 30%, transparent); margin: 2px 4px; }
.user-menu .mi.who, .user-menu .mi.status { font-size: 0.75rem; }
.user-menu .who img, .user-menu .who .avatar { width: 1.125rem; height: 1.125rem; }
.user-menu .who .ico, .user-menu .status .ico { width: 1.125rem; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
.user-menu .active-users { margin-left: auto; display: flex; align-items: center; gap: 4px; padding: 2px 6px; font-size: 0.6875rem; line-height: 1; color: ${isLight ? 'var(--color-gray-500)' : 'var(--color-gray-400)'}; }
.user-menu .active-users i { width: 6px; height: 6px; border-radius: 9999px; background: oklch(72.3% 0.219 149.579); }
.user-menu .status .clear { display: flex; align-items: center; justify-content: center; width: 1.25rem; height: 1.25rem; opacity: 0.5; }
.user-menu .status .clear:hover { opacity: 1; }
.tdp-menu.emoji-menu { flex-direction: row; gap: 2px; padding: 4px; }
.emoji-menu .em { width: 1.75rem; height: 1.75rem; display: flex; align-items: center; justify-content: center; border-radius: 8px; font-size: 1.05rem; cursor: pointer; }
.emoji-menu .em:hover { background: ${navHover}; }

/* ---- Temporary Chat: the checked icon, the placeholder's label, and a
   dashed message bar (MessageInput.svelte) ---- */
nav .temp-chat .on, nav .temp-chat.active .off { display: none; }
nav .temp-chat.active .on { display: flex; }
nav .temp-chat > span { display: flex; }
.ph-temp { display: none; align-items: center; gap: 6px; margin-top: 4px; margin-bottom: 2px; width: fit-content; font-size: 0.75rem; color: var(--color-gray-500); }
.ph-temp svg { width: 0.875rem; height: 0.875rem; }
body.temp-chat .ph-temp { display: flex; }
body.temp-chat .input-wrap > #message-input-container { border-style: dashed; border-color: ${isLight ? 'var(--color-gray-100)' : 'var(--color-gray-800)'}; }
body.temp-chat .input-wrap > #message-input-container:hover { border-color: ${isLight ? 'var(--color-gray-200)' : 'var(--color-gray-700)'}; }

/* ---- Side panes: ChatControls and Thread sit after a resizer's border ---- */
.side-pane { flex: none; height: 100%; display: flex; flex-direction: column; min-height: 0; background: ${isLight ? '#fff' : 'var(--color-gray-900)'}; border-left: 1px solid ${isLight ? 'var(--color-gray-50)' : 'color-mix(in srgb, var(--color-gray-850) 30%, transparent)'}; position: relative; z-index: 10; }
.side-pane[hidden] { display: none; }
.controls-pane { width: 350px; }
.thread-pane { width: 420px; }
@media (max-width: 1023px) {
  .side-pane { position: absolute; top: 0; right: 0; bottom: 0; z-index: 40; box-shadow: -10px 0 30px rgb(0 0 0 / 0.15); }
}
.cc-tabs { display: flex; align-items: center; justify-content: space-between; padding: 8px; flex-shrink: 0; }
.cc-tab { padding: 4px 10px; font-size: 0.875rem; border-radius: 8px; white-space: nowrap; background: color-mix(in srgb, var(--color-gray-${isLight ? '100' : '800'}) ${isLight ? '40' : '25'}%, transparent); color: ${isLight ? 'var(--color-gray-700)' : 'var(--color-gray-200)'}; }
.pane-close { display: flex; align-items: center; justify-content: center; padding: 4px; border-radius: 8px; color: ${isLight ? 'var(--color-gray-500)' : 'var(--color-gray-400)'}; cursor: pointer; }
.pane-close svg { width: 1rem; height: 1rem; }
.cc-body { flex: 1; min-height: 0; overflow-y: auto; padding: 4px 12px 16px; scrollbar-width: thin; font-size: 0.875rem; color: ${isLight ? 'inherit' : 'var(--color-gray-200)'}; }
/* Collapsible.svelte headers: py-1 text-xs text-gray-500, a size-2.5 chevron
   (up while open) at the right */
.cc-sec + .cc-sec { margin-top: 4px; }
.cc-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; width: 100%; padding: 4px 0; border: 0; background: none; font: inherit; font-size: 0.75rem; color: ${isLight ? 'var(--color-gray-500)' : 'var(--color-gray-400)'}; cursor: pointer; user-select: none; transition: color 0.15s; }
.cc-head:hover { color: ${isLight ? 'var(--color-gray-900)' : 'var(--color-gray-100)'}; }
.cc-head svg { width: 0.625rem; height: 0.625rem; transform: translateY(1px); }
.cc-head .up, .cc-sec.open .cc-head .down { display: none; }
.cc-sec.open .cc-head .up { display: flex; }
.cc-head .down { display: flex; }
.cc-content { display: none; padding: 4px 0; font-size: 0.75rem; }
.cc-sec.open .cc-content { display: block; }
.cc-content textarea { width: 100%; resize: vertical; border: 0; outline: none; background: transparent; color: inherit; font: inherit; font-size: 0.75rem; padding: 4px 0; }
.cc-content textarea::placeholder, .cc-content input::placeholder { color: ${isLight ? 'var(--color-gray-400)' : 'var(--color-gray-500)'}; }
.cc-content select { flex: 1; min-width: 0; padding: 4px; border: 0; border-radius: 4px; outline: none; background: transparent; color: inherit; font: inherit; font-size: 0.75rem; }
.cc-valves { display: flex; gap: 8px; }
/* AdvancedParams.svelte: a label and a "Default" button per row, then a
   slider and number box once set */
.ap { color: ${isLight ? 'var(--color-gray-900)' : 'var(--color-gray-100)'}; }
.ap-row { padding: 2px 0; }
.ap-line { display: flex; justify-content: space-between; align-items: center; }
.ap-label { font-size: 0.75rem; color: ${isLight ? 'var(--color-gray-600)' : 'var(--color-gray-400)'}; }
.ap-btn { padding: 4px 12px; font-size: 0.75rem; border-radius: 4px; flex-shrink: 0; cursor: pointer; }
.ap-custom { display: flex; gap: 8px; margin-top: 2px; align-items: center; }
.ap-custom input[type=range] { flex: 1; height: 8px; border-radius: 8px; appearance: none; -webkit-appearance: none; background: ${isLight ? 'var(--color-gray-200)' : 'var(--color-gray-700)'}; cursor: pointer; }
.ap-custom input[type=range]::-webkit-slider-thumb { -webkit-appearance: none; width: 14px; height: 14px; border-radius: 9999px; background: ${isLight ? 'var(--color-gray-900)' : '#fff'}; }
.ap-custom input[type=number], .ap-custom input[type=text] { background: transparent; border: 0; outline: none; color: inherit; font: inherit; font-size: 0.75rem; }
.ap-custom input[type=number] { width: 3.5rem; text-align: center; }
.ap-custom input[type=text] { width: 100%; font-size: 0.875rem; }

/* ---- Channel (channel/Channel.svelte, Navbar, Messages, Message) ---- */
main.channel-mode > nav, main.channel-mode > #messages-container, main.channel-mode > .input-wrap { display: none; }
.channel-view { flex: 1; min-height: 0; display: flex; flex-direction: column; }
.channel-view[hidden] { display: none; }
.ch-nav { display: flex; align-items: center; padding: 4px 12px; flex-shrink: 0; position: relative; z-index: 2; }
.ch-title { display: flex; align-items: center; gap: 6px; min-width: 0; flex: 1; margin-left: 4px; font-size: 0.875rem; font-weight: 500; line-height: 1.25rem; color: ${isLight ? 'var(--color-gray-700)' : 'var(--color-gray-300)'}; }
.ch-title svg { width: 1rem; height: 1rem; flex-shrink: 0; }
.ch-nav-btns { display: flex; align-items: center; gap: 4px; color: ${isLight ? 'var(--color-gray-500)' : 'var(--color-gray-400)'}; }
.ch-nav-btn { display: flex; align-items: center; gap: 4px; height: 1.75rem; min-width: 1.75rem; justify-content: center; padding: 0 6px; border-radius: 8px; cursor: pointer; transition: background 0.15s; }
.ch-nav-btn:hover { background: color-mix(in srgb, var(--color-gray-${isLight ? '50' : '800'}) 60%, transparent); }
.ch-nav-btn svg { width: 1rem; height: 1rem; }
.ch-nav-btn .n { font-size: 0.75rem; font-variant-numeric: tabular-nums; }
.ch-scroll { flex: 1; min-height: 0; overflow-y: auto; padding-bottom: 10px; scrollbar-width: none; }
.ch-intro { padding: 32px 16px 16px; display: flex; flex-direction: column; gap: 6px; }
.ch-intro .t { font-size: 1.125rem; font-weight: 500; line-height: 1.5rem; color: ${textMain}; }
.ch-intro .d { font-size: 0.75rem; line-height: 1.25rem; color: ${isLight ? 'var(--color-gray-500)' : 'var(--color-gray-400)'}; }
.ch-intro-rule { margin: 0 16px 8px; border: 0; border-top: 1px solid color-mix(in srgb, var(--color-gray-${isLight ? '100' : '800'}) ${isLight ? '60' : '40'}%, transparent); }
.cm { position: relative; padding: 0 16px; transition: background 0.1s; }
.cm.head { padding-top: 10px; padding-bottom: 2px; }
.cm:hover { background: color-mix(in srgb, var(--color-gray-${isLight ? '300' : '700'}) 5%, transparent); }
.cm.pinned { background: ${isLight ? 'oklch(97.3% 0.071 103.193 / 0.2)' : 'oklch(97.3% 0.071 103.193 / 0.05)'}; }
.cm.replying { border-left: 4px solid oklch(62.3% 0.214 259.815); padding-left: 12px; background: ${isLight ? 'oklch(93.2% 0.032 255.585 / 0.1)' : 'oklch(93.2% 0.032 255.585 / 0.05)'}; }
.cm-pin-label { display: flex; align-items: center; gap: 4px; margin-left: 36px; margin-bottom: 2px; font-size: 0.6875rem; line-height: 1rem; }
.cm-pin-label svg { width: 0.75rem; height: 0.75rem; color: ${isLight ? 'oklch(79.5% 0.184 86.047)' : 'oklch(90.5% 0.182 98.111)'}; }
.cm-pin-label span { color: var(--color-gray-500); }
.cm-row { display: flex; width: 100%; }
.cm-av { width: 1.5rem; flex-shrink: 0; }
.cm-av .av { width: 1.5rem; height: 1.5rem; margin-top: 2px; border-radius: 9999px; display: flex; align-items: center; justify-content: center; font-size: 0.6875rem; font-weight: 600; color: #fff; overflow: hidden; }
.cm-av .av img { width: 100%; height: 100%; object-fit: cover; }
.cm-av .t2 { margin: 4px -4px 0; transform: translateY(1px); text-align: center; font-size: 0.6875rem; line-height: 1rem; font-variant-numeric: tabular-nums; color: ${isLight ? 'var(--color-gray-500)' : 'var(--color-gray-400)'}; opacity: 0; transition: opacity 0.1s; }
.cm:hover .cm-av .t2 { opacity: 1; }
.cm-body { flex: 1; min-width: 0; padding-left: 12px; }
.cm-meta { display: flex; align-items: baseline; gap: 6px; line-height: 1.25rem; }
.cm-name { font-size: 0.8125rem; font-weight: 500; color: ${isLight ? 'var(--color-gray-900)' : 'var(--color-gray-100)'}; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cm-time { font-size: 0.6875rem; line-height: 1rem; color: ${isLight ? 'var(--color-gray-500)' : 'var(--color-gray-400)'}; transform: translateY(1px); flex-shrink: 0; }
.cm-text { font-size: 0.875rem; line-height: 1.375rem; color: ${proseText}; overflow-wrap: anywhere; }
.cm-text b { color: ${isLight ? 'var(--color-gray-900)' : '#fff'}; font-weight: 600; }
.cm-text code { font-size: 0.8125rem; padding: 1px 4px; border-radius: 6px; background: ${codeBg}; }
.cm-text .mention { color: ${isLight ? 'oklch(58.8% 0.158 241.966)' : 'oklch(82.8% 0.111 230.318)'}; }
.cm-text .edited { font-size: 0.625rem; color: var(--color-gray-500); padding-left: 4px; }
.cm-edit textarea { width: 100%; resize: none; border: 0; outline: none; background: transparent; color: inherit; font: inherit; font-size: 0.875rem; line-height: 1.375rem; }
.cm-edit .btns { display: flex; justify-content: flex-end; gap: 6px; margin: 8px 0 4px; font-size: 0.875rem; }
.cm-edit .btns span { padding: 6px 14px; border-radius: 24px; cursor: pointer; }
.cm-edit .cancel { background: ${isLight ? '#fff' : 'var(--color-gray-900)'}; color: ${isLight ? 'var(--color-gray-800)' : 'var(--color-gray-100)'}; }
.cm-edit .save { background: ${isLight ? 'var(--color-gray-900)' : '#fff'}; color: ${isLight ? 'var(--color-gray-100)' : 'var(--color-gray-800)'}; }
/* Reply-to quote above a reply */
.cm-quote { position: relative; font-size: 0.75rem; margin-bottom: 4px; }
.cm-quote::before { content: ''; position: absolute; left: 12px; top: 8px; width: 24px; height: 12px; border-top: 1.5px solid; border-left: 1.5px solid; border-top-left-radius: 8px; border-color: ${isLight ? 'var(--color-gray-200)' : 'var(--color-gray-700)'}; }
.cm-quote .q { margin-left: 36px; display: flex; align-items: center; gap: 6px; min-width: 0; }
.cm-quote .q .av { width: 1rem; height: 1rem; margin-left: 2px; border-radius: 9999px; font-size: 0.5rem; display: flex; align-items: center; justify-content: center; color: #fff; flex-shrink: 0; }
.cm-quote .q .who { flex-shrink: 0; color: ${textMain}; }
.cm-quote .q .what { color: ${isLight ? 'var(--color-gray-500)' : 'var(--color-gray-400)'}; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* The hover toolbar: rounded-lg bg-white dark:bg-gray-850 shadow-md p-0.5 */
.cm-tools { position: absolute; right: 4px; top: -12px; z-index: 30; display: flex; gap: 4px; padding: 2px; border-radius: 8px; background: ${isLight ? '#fff' : 'var(--color-gray-850)'}; border: 1px solid color-mix(in srgb, var(--color-gray-${isLight ? '100' : '850'}) 30%, transparent); box-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.1), 0 2px 4px -2px rgb(0 0 0 / 0.1); opacity: 0; pointer-events: none; transition: opacity 0.1s; color: ${textMain}; }
.cm:hover .cm-tools, .cm.tools-open .cm-tools { opacity: 1; pointer-events: auto; }
.cm-tools span { display: flex; padding: 4px; border-radius: 8px; cursor: pointer; transition: background 0.15s; }
.cm-tools span:hover { background: ${isLight ? 'var(--color-gray-100)' : 'var(--color-gray-800)'}; }
.cm-tools svg { width: 1rem; height: 1rem; }
/* Reactions: min-h-6 rounded-lg px-1.5, blue when you reacted */
.cm-reacts { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; margin: 4px 0; }
.cm-react { display: flex; align-items: center; gap: 6px; min-height: 1.5rem; padding: 2px 6px; border-radius: 8px; font-size: 0.875rem; line-height: 1rem; cursor: pointer; background: color-mix(in srgb, var(--color-gray-${isLight ? '300' : '500'}) 10%, transparent); outline: 1px solid transparent; transition: outline-color 0.15s; }
.cm-react:hover { outline-color: ${isLight ? 'color-mix(in srgb, var(--color-gray-700) 30%, transparent)' : 'color-mix(in srgb, var(--color-gray-300) 30%, transparent)'}; }
.cm-react.mine { background: oklch(80.9% 0.105 251.813 / 0.1); outline-color: oklch(62.3% 0.214 259.815 / 0.5); }
.cm-react .n { font-size: 0.6875rem; color: ${isLight ? 'var(--color-gray-500)' : 'var(--color-gray-400)'}; }
.cm-react-add { display: flex; padding: 4px; border-radius: 8px; cursor: pointer; background: color-mix(in srgb, var(--color-gray-500) 10%, transparent); color: ${isLight ? 'var(--color-gray-500)' : 'var(--color-gray-400)'}; outline: 1px solid transparent; }
.cm-react-add:hover { outline-color: ${isLight ? 'color-mix(in srgb, var(--color-gray-700) 30%, transparent)' : 'color-mix(in srgb, var(--color-gray-300) 30%, transparent)'}; }
.cm-react-add svg { width: 1rem; height: 1rem; }
/* "N Replies - Last reply ..." opens the thread */
.cm-replies { display: flex; margin: -2px 0 6px; }
.cm-replies span.btn { display: flex; align-items: center; min-width: 0; padding: 4px 0; font-size: 0.6875rem; color: ${isLight ? 'var(--color-gray-500)' : 'var(--color-gray-400)'}; cursor: pointer; transition: color 0.15s; }
.cm-replies span.btn:hover { color: ${isLight ? 'var(--color-gray-700)' : 'var(--color-gray-300)'}; }
.cm-replies b { font-weight: 500; margin-right: 4px; }
.cm-replies svg { width: 0.625rem; height: 0.625rem; margin-left: 4px; }
/* channel/MessageInput.svelte: one compact rounded-2xl card */
.ch-input-wrap { padding: 0 12px 12px; flex-shrink: 0; position: relative; }
.ch-typing { position: absolute; left: 12px; right: 12px; top: -26px; padding: 0 4px; font-size: 0.75rem; color: ${isLight ? 'var(--color-gray-500)' : 'var(--color-gray-400)'}; display: none; align-items: center; gap: 6px; }
.ch-typing.show { display: flex; }
.ch-typing b { font-weight: 400; color: ${isLight ? '#000' : '#fff'}; }
.ch-typing .dots { display: inline-flex; gap: 2px; }
.ch-typing .dots i { width: 4px; height: 4px; border-radius: 9999px; background: currentColor; animation: tdp-typing 1.2s infinite ease-in-out; }
.ch-typing .dots i:nth-child(2) { animation-delay: 0.15s; }
.ch-typing .dots i:nth-child(3) { animation-delay: 0.3s; }
@keyframes tdp-typing { 0%, 60%, 100% { opacity: 0.25; } 30% { opacity: 1; } }
.ch-input { display: flex; flex-direction: column; border-radius: 16px; border: 1px solid color-mix(in srgb, var(--color-gray-${isLight ? '100' : '800'}) ${isLight ? '60' : '50'}%, transparent); background: ${isLight ? '#fff' : 'color-mix(in srgb, var(--color-gray-400) 5%, transparent)'}; box-shadow: 0 1px 3px 0 rgb(0 0 0 / 0.1), 0 1px 2px -1px rgb(0 0 0 / 0.1); color: ${isLight ? 'inherit' : 'var(--color-gray-100)'}; transition: border-color 0.15s; }
.ch-input:hover, .ch-input:focus-within { border-color: ${isLight ? 'var(--color-gray-100)' : 'var(--color-gray-800)'}; }
.ch-reply-bar { display: none; align-items: center; justify-content: space-between; padding: 8px 12px 0; font-size: 0.75rem; color: ${isLight ? 'var(--color-gray-500)' : 'var(--color-gray-400)'}; }
.ch-input.replying .ch-reply-bar { display: flex; }
.ch-reply-bar .x { display: flex; align-items: center; justify-content: center; width: 1.5rem; height: 1.5rem; border-radius: 6px; cursor: pointer; }
.ch-reply-bar .x:hover { background: ${isLight ? 'var(--color-gray-50)' : 'var(--color-gray-800)'}; }
.ch-reply-bar .x svg { width: 0.875rem; height: 0.875rem; }
.ch-edit { margin: 0 8px; padding: 8px 4px 2px; min-height: 1.375rem; max-height: 8rem; overflow: auto; outline: none; font-size: 0.875rem; line-height: 1.375rem; position: relative; }
.ch-edit:empty::before { content: attr(data-placeholder); position: absolute; color: #676767; pointer-events: none; }
.ch-bar { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 2px 6px 6px; }
.ch-plus, .ch-mic { width: 1.75rem; height: 1.75rem; display: flex; align-items: center; justify-content: center; border-radius: 8px; cursor: pointer; flex-shrink: 0; transition: background 0.15s, color 0.15s; }
.ch-plus { color: ${isLight ? 'var(--color-gray-500)' : 'var(--color-gray-400)'}; }
.ch-plus:hover { background: ${isLight ? 'var(--color-gray-100)' : 'var(--color-gray-800)'}; }
.ch-mic { color: ${isLight ? 'var(--color-gray-600)' : 'var(--color-gray-300)'}; }
.ch-plus svg, .ch-mic svg, .ch-send svg { width: 1rem; height: 1rem; }
.ch-right { display: flex; align-items: center; gap: 4px; }
.ch-send { width: 1.75rem; height: 1.75rem; border-radius: 9999px; display: flex; align-items: center; justify-content: center; background: ${isLight ? 'var(--color-gray-200)' : 'var(--color-gray-700)'}; color: ${isLight ? '#fff' : 'var(--color-gray-900)'}; transition: background 0.15s; }
.ch-input.has-text .ch-send { background: ${isLight ? '#000' : '#fff'}; color: ${isLight ? '#fff' : '#000'}; cursor: pointer; }
.ch-input.has-text .ch-mic { display: none; }
/* Thread (channel/Thread.svelte) */
.th-head { display: flex; align-items: center; justify-content: space-between; min-height: 2.5rem; padding: 6px 16px; border-bottom: 1px solid color-mix(in srgb, var(--color-gray-${isLight ? '100' : '800'}) ${isLight ? '60' : '40'}%, transparent); flex-shrink: 0; }
.th-head .t { font-size: 0.875rem; font-weight: 500; color: ${textMain}; }
.th-head .pane-close { width: 1.75rem; height: 1.75rem; }
.th-head .pane-close:hover { background: color-mix(in srgb, var(--color-gray-${isLight ? '50' : '800'}) 60%, transparent); }
.thread-pane .ch-scroll { padding-top: 12px; }
.footer-note { text-align: center; font-size: 0.65625rem; color: ${textFaint}; padding: 4px 0 6px; }

/* Tooltip.svelte -> tippy theme 'dark' (app.css:379): rounded-lg bg-gray-950
   text-xs border border-gray-900 shadow-xl, content padding 5px 9px */
#tdp-tooltip { position: fixed; z-index: 300; max-width: 300px; padding: 5px 9px; border-radius: 8px; background: var(--color-gray-950); border: 1px solid var(--color-gray-900); color: #fff; font-size: 0.75rem; line-height: 1.35; box-shadow: 0 20px 25px -5px rgb(0 0 0 / 0.1), 0 8px 10px -6px rgb(0 0 0 / 0.1); pointer-events: none; opacity: 0; transition: opacity 0.1s; white-space: pre-line; }
/* Generation Info renders its usage block in a <pre> upstream */
#tdp-tooltip.pre { font-family: ui-monospace, 'JetBrains Mono', monospace; text-align: left; }
#tdp-tooltip.show { opacity: 1; }

/* Backdrop behind the mobile sidebar (Sidebar.svelte L767-776:
   fixed md:hidden z-40 inset-0 bg-black/60, closes on click) */
#backdrop { display: none; position: fixed; inset: 0; z-index: 40; background: rgb(0 0 0 / 0.6); }
/* Both are mobile-only upstream. Selectors must out-specify nav .nav-btn. */
nav .nav-toggle, nav .nav-newchat { display: none; }

/* --- Mobile: BREAKPOINT is 768 in routes/+layout.svelte --- */
@media (max-width: 767px) {
  /* no collapsed rail on mobile — it renders under {#if !$mobile && ...} */
  .sidebar-rail { display: none !important; }
  /* the panel becomes a fixed drawer, opaque rather than the 70% desktop wash */
  .sidebar-panel { position: fixed; top: 0; left: 0; height: 100%; z-index: 50; width: 245px; min-width: 245px;
    background: ${isLight ? 'var(--color-gray-50)' : 'var(--color-gray-950)'};
    transform: translateX(-100%); transition: transform 250ms ease; opacity: 1; pointer-events: auto;
    padding: 6px 4px 4px; border-right-width: 1px; }
  .app.collapsed .sidebar-panel { transform: translateX(-100%); }
  .app:not(.collapsed) .sidebar-panel { transform: translateX(0); }
  .app:not(.collapsed) #backdrop { display: block; }
  /* Navbar gains the sidebar toggle and a New Chat button, and px-2.5 pt-1.5 */
  nav { padding-left: 10px; padding-right: 10px; }
  nav .nav-toggle { display: flex; }
  .app:not(.collapsed) nav .nav-toggle { display: none; }
  nav .nav-newchat { display: flex; }
  #messages-container > .message-listitem { padding: 0 12px; }
  .input-wrap { padding: 0 6px; }
  .placeholder { padding-left: 8px; padding-right: 8px; }
  .ph-suggest { padding: 0 8px; }
  .ph-grid { grid-template-columns: 1fr; }
  .ph-model, .ph-sub { font-size: 1.5rem; line-height: 2rem; }
  .ph-logo { width: 2.25rem; height: 2.25rem; }
  .model-select { max-width: 8rem; }
}

${structural}
${gradientCSS}
</style>
<style>
${safeCSS}
</style>
</head>
<body data-initial-chat="${esc(opts.initialChat || 'default')}" data-sidebar-collapsed="${opts.initialCollapsed ? '1' : '0'}">
${opts.canvasScript ? '<canvas id="owui-theme-canvas-bg" style="position:fixed;top:0;left:0;width:100vw;height:100vh;z-index:0;pointer-events:none;"></canvas>' : ''}
<div class="app">
  <div class="sidebar-rail">
    <div class="rail-group">
      <span class="rail-btn lg rail-logo" data-tip="Open Sidebar" data-tip-placement="right"><span><img src="${LOGO}" alt="">${SVG.panel}</span></span>
      <span class="rail-btn rail-action" data-chat="new" data-tip="New Chat" data-tip-placement="right" style="margin-top:4px"><span>${SVG.editPencil}</span></span>
      <span class="rail-btn rail-action" data-tip="Search" data-tip-placement="right"><span>${SVG.search}</span></span>
      <span class="rail-btn rail-action" data-tip="Notes" data-tip-placement="right"><span>${SVG.notes}</span></span>
      <span class="rail-btn rail-action" data-tip="Workspace" data-tip-placement="right"><span>${SVG.workspace}</span></span>
      <span class="rail-btn rail-action" data-tip="Playground" data-tip-placement="right"><span>${SVG.code}</span></span>
    </div>
    <span class="rail-btn lg rail-action" id="rail-user" role="button" aria-label="User menu"><span><span class="avatar-wrap"><span class="rail-avatar"></span><span class="status-dot"></span></span></span></span>
  </div>
  <div id="backdrop"></div>
  <div class="sidebar-panel" id="sidebar">
    <div class="brand"><div class="brand-dot"><img src="${LOGO}" alt=""></div><span class="brand-name">Open WebUI</span><span class="panel-icon" data-tip="Close Sidebar">${SVG.panel}</span></div>
    <div class="side-item" data-chat="new">${SVG.editPencil} <span class="label">New Chat</span></div>
    <div class="side-item">${SVG.search} <span class="label">Search</span></div>
    <div class="side-item">${SVG.notes} <span class="label">Notes</span></div>
    <div class="side-item">${SVG.workspace} <span class="label">Workspace</span></div>
    <div class="side-item">${SVG.code} <span class="label">Playground</span></div>
    <div class="side-scroll">
      <!-- Sidebar/Section.svelte: each header folds its section, with a
           chevron that shows on hover; Channels and Folders start closed
           upstream, Channels opens here to show one off -->
      <div class="side-section open" data-section="models">
        <div class="side-label" data-section-toggle role="button" aria-expanded="true">Models <span class="sec-chev">${SVG.chevron}</span></div>
        <div class="side-section-body"><div class="side-item" data-chat="new"><span class="avatar sm"></span> <span class="label">Preview Model</span></div></div>
      </div>
      <div class="side-section" data-section="notes">
        <div class="side-label" data-section-toggle role="button" aria-expanded="false">Notes <span class="sec-chev">${SVG.chevron}</span><span class="add" data-tip="New Note">${SVG.plus}</span></div>
        <div class="side-section-body"><div class="side-item side-note">${SVG.notes} <span class="label">Theme ideas</span></div></div>
      </div>
      <div class="side-section open" data-section="channels">
        <div class="side-label" data-section-toggle role="button" aria-expanded="true">Channels <span class="sec-chev">${SVG.chevron}</span><span class="add" data-tip="Create Channel">${SVG.plus}</span></div>
        <div class="side-section-body">
          <div class="channel-item unread" data-chat="channel:general"><span class="ch-ico">${SVG.hashtag}</span><span class="label">general</span><span class="unread-badge" title="Unread">3</span><span class="ch-settings" data-tip="Edit">${SVG.cog}</span></div>
          <div class="channel-item" data-chat="channel:design"><span class="ch-ico">${SVG.hashtag}</span><span class="label">design</span><span class="ch-settings" data-tip="Edit">${SVG.cog}</span></div>
          <div class="channel-item" data-chat="channel:theme-lab"><span class="ch-ico lock">${SVG.lock}</span><span class="label">theme-lab</span><span class="ch-settings" data-tip="Edit">${SVG.cog}</span></div>
        </div>
      </div>
      <div class="side-section" data-section="folders">
        <div class="side-label" data-section-toggle role="button" aria-expanded="false">Folders <span class="sec-chev">${SVG.chevron}</span><span class="add" data-tip="New Folder">${SVG.plus}</span></div>
        <div class="side-section-body">
          <div class="side-folder" data-folder="Theme work"><div class="folder-row" role="button" aria-expanded="false"><span class="folder-ico">${SVG.folder}</span><span class="folder-chev">${SVG.chevron}</span><span class="label">Theme work</span></div>
            <div class="folder-children"><div class="side-item" data-chat="launch"><span class="label">Launch checklist</span><span class="age">5d</span>${CHAT_MENU}</div></div></div>
        </div>
      </div>
      <div class="side-section open" data-section="chats">
        <div class="side-label" data-section-toggle role="button" aria-expanded="true">Chats <span class="sec-chev">${SVG.chevron}</span><span class="add sec-more" data-tip="More" aria-label="More">${SVG.moreHorizontal}</span></div>
        <div class="side-section-body">
          <!-- common/Folder.svelte: Pinned is open by default, its chats
               indented behind a left rule -->
          <div class="side-pinned-wrap open">
            <div class="side-pinned" role="button" aria-expanded="true"><span class="pin-chev">${SVG.chevronDownSm}</span><span class="label">Pinned</span></div>
            <div class="pinned-list">
              <div class="side-item" data-chat="brand"><span class="label">Brand palette v2</span><span class="age">2w</span>${CHAT_MENU}</div>
              <div class="side-item" data-chat="digest"><span class="label">Weekly theme digest</span><span class="age">3w</span>${CHAT_MENU}</div>
            </div>
          </div>
          <div class="side-label time-label">Today</div>
          <div class="side-item active" data-chat="default"><span class="label">Theme preview chat</span>${CHAT_MENU}</div>
          <div class="side-label time-label">Previous 7 days</div>
          <div class="side-item" data-chat="oklch"><span class="label">OKLCH color ramps</span><span class="age">1d</span>${CHAT_MENU}</div>
          <div class="side-item" data-chat="fx"><span class="label">Canvas FX ideas</span><span class="age">2d</span>${CHAT_MENU}</div>
          <div class="side-item" data-chat="gradient"><span class="label">Gradient inspiration</span><span class="age">3d</span>${CHAT_MENU}</div>
          <div class="side-item" data-chat="transparency"><span class="label">Structural transparency</span><span class="age">4d</span>${CHAT_MENU}</div>
          <div class="side-item" data-chat="notes"><span class="label">Preset gallery notes</span><span class="age">6d</span>${CHAT_MENU}</div>
        </div>
      </div>
    </div>
    <div class="side-user" id="side-user" role="button" aria-label="User menu"><span class="avatar-wrap"><span class="avatar"></span><span class="status-dot"></span></span> You</div>
  </div>
  <main>
    <nav><span class="nav-btn nav-toggle" data-tip="Open Sidebar">${SVG.panel}</span><span id="nav-title">Theme preview chat</span><span class="nav-btn nav-chat-menu">${SVG.dots}</span><span class="right" id="nav-right"><span class="nav-btn nav-newchat" data-tip="New Chat">${SVG.chatPlus}</span><span class="nav-btn temp-chat" id="temporary-chat-button" role="button" aria-label="Temporary Chat" aria-pressed="false" data-tip="Temporary Chat"><span class="off">${SVG.chatBubbleDotted}</span><span class="on">${SVG.chatBubbleDottedChecked}</span></span><span class="nav-btn controls" id="chat-controls-button" role="button" aria-label="Controls" data-tip="Controls">${SVG.knobs}</span></span></nav>
    <div id="messages-container">
      ${userRow('Show me what this preset looks like on a real conversation.', false)}
      ${aiRow('Response Speed: 104.3 t/s | Total Duration: 2.418s | Prompt Evals: 147 | Eval Count: 236 | Session: 1854 tokens',
        '<p>Here you go: this mock chat mirrors Open WebUI\'s layout: <b>sidebar</b>, <b>message bubbles</b>, <code>inline code</code>, and the input bar below.</p>' +
        '<div class="code-block"><div class="code-block-header"><span>javascript</span><span class="cb-action">Copy</span></div><pre>const theme = \'applied\';</pre></div>' +
        '<p>Every surface is painted by the preset\'s <code>--color-gray-*</code> ramp, the same variables the designer generates.</p>',
        null,
        { seconds: 6, body: 'The user wants to see the preset on realistic chat content. I should show the layout pieces the theme actually paints: the sidebar, a user bubble, inline code, a fenced code block, and the input shell, so every themed surface is visible at once.' })}
      ${userRow('Nice. The palette applies to every surface?', true)}
      ${aiRowMulti([
        {
          stats: 'Response Speed: 98.7 t/s | Total Duration: 1.9s | Eval Count: 164 | Session: 2018 tokens',
          body: '<p><b>Exactly.</b> Backgrounds, borders, and text all come from the preset, and Canvas FX or gradients render behind the whole interface.</p>',
          fups: [
            'How do I import one of these presets into my instance?',
            'Which presets look best with structural transparency enabled?',
            'Can I combine a Canvas FX script with a gradient background?',
            'What happens to the palette in OLED mode?',
          ],
        },
        {
          stats: 'Response Speed: 101.2 t/s | Total Duration: 2.3s | Eval Count: 189 | Session: 2043 tokens',
          body: '<p>It does. The designer emits one <code>--color-gray-*</code> ramp per mode, and every surface here reads from it: backgrounds, borders, bubbles, even this pager. Regenerate a response and the palette holds.</p>',
          fups: [
            'Do regenerated responses keep the same theme variables?',
            'Which mode should I preview first for a new preset?',
          ],
        },
      ])}
    </div>
    <div class="input-wrap">
      <div id="message-input-container">
        <div id="chat-input-container">
          <div id="chat-input" class="ProseMirror input-prose" contenteditable="false"><p class="is-editor-empty" data-placeholder="Send a Message"><br></p></div>
        </div>
        <div class="input-row">
          <span class="icon-btn" id="input-menu-button" role="button" aria-label="More" aria-haspopup="true" data-tip="More">${SVG.plusAlt}</span>
          <span class="input-divider"></span>
          <span class="icon-btn" id="integration-menu-button" role="button" aria-label="Integrations" aria-haspopup="true" data-tip="Integrations">${SVG.component}</span>
          <span class="input-pills" id="input-pills"></span>
          <span class="spacer"></span>
          <span class="model-select" id="model-selector-model-button" role="button" aria-haspopup="listbox"><span class="name">Preview Model</span>${SVG.chevronDown}</span>
          <span class="mic-btn" data-tip="Dictate">${SVG.mic}</span>
          <button class="call-button bg-black text-white dark:bg-white dark:text-black rounded-full p-[5px] self-center" data-tip="Voice mode">${SVG.voice}</button>
        </div>
      </div>
      <div class="footer-note">Preview Model can make mistakes. Verify important information.</div>
    </div>
    <!-- channel/Channel.svelte: its own navbar, list and input (built by script) -->
    <div class="channel-view" id="channel-view" hidden></div>
  </main>
  <!-- ChatControls.svelte on desktop: a 350px pane after a resizer border -->
  <aside class="side-pane controls-pane" id="controls-pane" hidden aria-label="Controls"></aside>
  <!-- channel/Thread.svelte: a 420px pane beside the channel -->
  <aside class="side-pane thread-pane" id="thread-pane" hidden aria-label="Thread"></aside>
</div>
<script type="application/json" id="tdp-state">${JSON.stringify(opts.initialState || null).replace(/</g, '\\u003c')}</script>
<script>
// What you change in the mock (renamed, pinned, cloned or moved chats, your
// status, Chat Controls, channel messages) is sent up as you change it, and a
// rebuild for another mode or palette hands it back here, so switching modes
// keeps it. Each part registers a getter and reads its saved value on start.
(function () {
  var saved = null;
  try { saved = JSON.parse(document.getElementById('tdp-state').textContent); } catch (e) {}
  var parts = {}, queued = false;
  function post() {
    queued = false;
    var out = {};
    for (var k in parts) { try { out[k] = parts[k](); } catch (e) {} }
    try { parent.postMessage({ __tdpPreview: true, kind: 'mockstate', state: out }, '*'); } catch (e) {}
  }
  window.__tdpState = {
    saved: saved || {},
    add: function (name, get) { parts[name] = get; },
    save: function () { if (!queued) { queued = true; setTimeout(post, 0); } }
  };
  // The sidebar's rows are restored as markup before anything binds to them
  var side = document.querySelector('.sidebar-panel .side-scroll');
  if (side && saved && typeof saved.side === 'string') side.innerHTML = saved.side;
  if (side) {
    window.__tdpState.add('side', function () {
      var copy = side.cloneNode(true);
      copy.querySelectorAll('input.rename').forEach(function (i) { i.remove(); });
      copy.querySelectorAll('.label[hidden]').forEach(function (l) { l.hidden = false; });
      copy.querySelectorAll('.menu-open').forEach(function (r) { r.classList.remove('menu-open'); });
      return copy.innerHTML;
    });
    new MutationObserver(window.__tdpState.save).observe(side, { subtree: true, childList: true, attributes: true, characterData: true });
  }
})();
</script>
${opts.canvasScript ? `<script type="application/json" id="cfx-src">${JSON.stringify(opts.canvasScript).replace(/</g, '\\u003c')}</script>
<script>
(function () {
  var el = document.getElementById('cfx-src');
  var canvas = document.getElementById('owui-theme-canvas-bg');
  if (!el || !canvas) return;
  var report = function (text) { try { parent.postMessage({ __tdpPreview: true, kind: 'status', text: text }, '*'); } catch (e) {} };
  if (!canvas.transferControlToOffscreen) { report('Live Canvas FX preview needs a browser with OffscreenCanvas support.'); return; }
  var src;
  try { src = JSON.parse(el.textContent); } catch (e) { return; }
  // A frame that was just added can run this before it has a size, and a
  // script started at 0x0 can divide by it, so wait until it has one
  var px = null;
  var toRgb = function (value) {
    if (!px) { var c = document.createElement('canvas'); c.width = c.height = 1; px = c.getContext('2d', { willReadFrequently: true }); }
    px.clearRect(0, 0, 1, 1);
    px.fillStyle = '#808080';
    px.fillStyle = value;
    px.fillRect(0, 0, 1, 1);
    var d = px.getImageData(0, 0, 1, 1).data;
    return [d[0], d[1], d[2]];
  };
  // The same function as the designer's bootloader (theme_designer_pro_event.py)
  function buildCanvasTheme(readVar, toRgb, mode, hue, chroma, set) {
    var dark = mode === 'dark' || mode === 'oled';
    var hex = function(c) { return '#' + c.map(function(v) { return (v < 16 ? '0' : '') + v.toString(16); }).join(''); };
    var ramp = {}, rampRgb = {};
    [50, 100, 200, 300, 400, 500, 600, 700, 800, 850, 900, 950].forEach(function(s) {
      rampRgb[s] = toRgb(readVar('--color-gray-' + s) || '#808080');
      ramp[s] = hex(rampRgb[s]);
    });
    // Accents in the theme's hue, as colorful as the theme itself
    var ac = chroma > 0 ? Math.min(0.18, Math.max(0.06, chroma / 250)) : 0;
    var al = dark ? 0.74 : 0.56;
    var accent = function(dh) { return toRgb('oklch(' + al + ' ' + ac.toFixed(3) + ' ' + ((hue + dh + 360) % 360).toFixed(1) + ')'); };
    // A theme can set its own accents as '#rrggbb'. Checked again
    // here: the state can come from an older build or be edited.
    var pick = function(k, dh) {
      var v = set && set[k];
      return typeof v === 'string' && /^#[0-9a-fA-F]{6}$/.test(v) ? toRgb(v.toLowerCase()) : accent(dh);
    };
    var src = {
      background: rampRgb[dark ? 900 : 50], surface: rampRgb[dark ? 850 : 100],
      text: rampRgb[dark ? 100 : 900], muted: rampRgb[dark ? 400 : 500],
      accent: pick('accent', 0), accent2: pick('accent2', 40), accent3: pick('accent3', -40)
    };
    var colors = {}, rgb = {};
    Object.keys(src).forEach(function(k) {
      colors[k] = hex(src[k]);
      rgb[k] = src[k].map(function(v) { return Math.round(v / 2.55) / 100; });
    });
    return { type: 'theme', mode: mode, dark: dark, hue: hue, chroma: chroma / 1000, colors: colors, rgb: rgb, ramp: ramp };
  }
  // What the mock chat says about itself (see sendContext), kept until the
  // worker starts
  var toWorker = null, waiting = null;
  window.addEventListener('tdp-context', function (e) {
    var m = Object.assign({ type: 'context' }, e.detail);
    if (toWorker) toWorker(m);
    else if (!('exactTokens' in m)) waiting = m;
  });
  var started = false;
  var start = function () {
    if (started || !window.innerWidth || !window.innerHeight) return;
    started = true;
    window.removeEventListener('resize', start);
    canvas.width = window.innerWidth; canvas.height = window.innerHeight;
    var url = URL.createObjectURL(new Blob([src], { type: 'application/javascript' }));
    var worker;
    try { worker = new Worker(url); } catch (e) { report('Could not start the animation worker: ' + e.message); return; }
    worker.onerror = function (e) { report('Script error: ' + (e.message || 'the animation failed to run.')); };
    var off = canvas.transferControlToOffscreen();
    var wpost = function (m, t) { try { worker.postMessage(m, t); } catch (e) {} };
    // The palette channel, as the designer sends it: with init, then on change
    var lastTheme = '';
    var theme = function () {
      try {
        var cs = getComputedStyle(document.documentElement);
        var dt = document.documentElement.getAttribute('data-theme');
        var mode = dt === 'oled-dark' ? 'oled' : dt === 'her' || dt === 'light' ? dt : 'dark';
        // A set accent reaches the frame as a hex --tdp-accent, a generated one as oklch()
        var set = { accent: cs.getPropertyValue('--tdp-accent').trim(), accent2: cs.getPropertyValue('--tdp-accent-2').trim(), accent3: cs.getPropertyValue('--tdp-accent-3').trim() };
        var t = buildCanvasTheme(function (n) { return cs.getPropertyValue(n).trim(); }, toRgb, mode,
          parseFloat(cs.getPropertyValue('--tdp-hue')) || 0, parseFloat(cs.getPropertyValue('--tdp-chroma')) || 0, set);
        lastTheme = JSON.stringify(t);
        return t;
      } catch (e) { return null; }
    };
    wpost({ type: 'init', canvas: off, width: window.innerWidth, height: window.innerHeight, env: { authToken: '', baseUrl: '', locale: navigator.language, timezone: '' }, theme: theme() }, [off]);
    toWorker = wpost;
    if (waiting) { wpost(waiting); waiting = null; }
    window.addEventListener('tdp-palette', function () {
      var before = lastTheme, t = theme();
      if (t && lastTheme !== before) wpost(t);
    });
    var pend = null, raf = null;
    var pump = function () { raf = null; if (pend) { wpost(pend); pend = null; } };
    var qm = function (x, y) { pend = { type: 'mousemove', x: x, y: y }; if (!raf) raf = requestAnimationFrame(pump); };
    var xy = function (e) { try { return { x: e.clientX, y: e.clientY }; } catch (err) { return null; } };
    window.addEventListener('mousemove', function (e) { var p = xy(e); if (p) qm(p.x, p.y); }, { passive: true });
    window.addEventListener('click', function (e) { var p = xy(e); if (p) wpost({ type: 'click', x: p.x, y: p.y }); }, true);
    window.addEventListener('mousedown', function (e) { var p = xy(e); if (p) wpost({ type: 'mousedown', x: p.x, y: p.y }); }, true);
    window.addEventListener('mouseup', function (e) { var p = xy(e); if (p) wpost({ type: 'mouseup', x: p.x, y: p.y }); }, true);
    window.addEventListener('touchstart', function (e) { try { var t = e.touches[0]; if (t) wpost({ type: 'mousedown', x: t.clientX, y: t.clientY }); } catch (err) {} }, { passive: true });
    window.addEventListener('touchmove', function (e) { try { var t = e.touches[0]; if (t) qm(t.clientX, t.clientY); } catch (err) {} }, { passive: true });
    window.addEventListener('resize', function () { if (window.innerWidth && window.innerHeight) wpost({ type: 'resize', width: window.innerWidth, height: window.innerHeight }); });
  };
  window.addEventListener('resize', start);
  (function wait() { start(); if (!started) requestAnimationFrame(wait); })();
})();
</script>` : ''}
<script>
(function () {
  var post = function (kind, data) {
    try { parent.postMessage(Object.assign({ __tdpPreview: true, kind: kind }, data), '*'); } catch (e) {}
  };
  var xy = function (e) { try { return { x: e.clientX, y: e.clientY }; } catch (err) { return null; } };
  window.addEventListener('mousemove', function (e) { var p = xy(e); if (p) post('mousemove', p); }, { passive: true });
  window.addEventListener('click', function (e) { var p = xy(e); if (p) post('click', p); }, true);
  window.addEventListener('mousedown', function (e) { var p = xy(e); if (p) post('mousedown', p); }, true);
  window.addEventListener('mouseup', function (e) { var p = xy(e); if (p) post('mouseup', p); }, true);
  var touchList = function (e) {
    try {
      var list = e.touches.length ? e.touches : e.changedTouches;
      var out = [];
      for (var i = 0; i < list.length; i++) out.push({ x: list[i].clientX, y: list[i].clientY });
      return out;
    } catch (err) { return []; }
  };
  window.addEventListener('touchstart', function (e) { post('touchstart', { touches: touchList(e) }); }, { passive: true });
  window.addEventListener('touchmove', function (e) { post('touchmove', { touches: touchList(e) }); }, { passive: true });
  window.addEventListener('touchend', function (e) { post('touchend', { touches: touchList(e) }); }, { passive: true });
  window.addEventListener('keydown', function (e) { if (e.key === 'Escape') post('escape', {}); });
})();

(function () {
  var msgs = document.getElementById('messages-container');
  var navTitle = document.getElementById('nav-title');
  if (!msgs || !navTitle) return;
  var BOLT_SVG = ${JSON.stringify(SVG.bolt)};
  var EYE_SVG = ${JSON.stringify(SVG.eyeSlash)};

  var MSG_ACTIONS_TPL = ${JSON.stringify(MSG_ACTIONS_TPL)};
  function msgActions(stats, pagerHtml) {
    var tip = String(stats || '').split(' | ').join('\\n')
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    var row = MSG_ACTIONS_TPL.replace('__TDP_STATS__', tip);
    return pagerHtml ? row.replace('<div class="msg-actions">', '<div class="msg-actions">' + pagerHtml) : row;
  }

  function branchPager(i, n) {
    return '<div class="branch-pager">' +
      '<span class="branch-nav branch-prev">' + BRANCH_PREV + '</span>' +
      '<span class="branch-count">' + (i + 1) + '/' + n + '</span>' +
      '<span class="branch-nav branch-next">' + BRANCH_NEXT + '</span>' +
      '</div>';
  }
  var USER_ACTIONS = ${JSON.stringify(USER_ACTIONS)};
  var USER_ACTIONS_DEL = ${JSON.stringify(USER_ACTIONS_DEL)};
  var THOUGHT_CHEVRON = ${JSON.stringify(SVG.chevronDown)};
  var BRANCH_PREV = ${JSON.stringify(ACTION_SVG.prevMsg)};
  var BRANCH_NEXT = ${JSON.stringify(ACTION_SVG.nextMsg)};

  // Follow-ups only render on the last message upstream, and each of these
  // conversations has exactly one reply — so every ai() call may carry them.
  function followUps(items) {
    if (!items || !items.length) return '';
    return '<div class="followups-wrap"><div class="followups">' +
      '<div class="followups-title">Follow up</div><div class="followups-list">' +
      items.map(function (q, i) {
        return '<div class="followup" data-tip="' + q + '">' + q + '</div>' + (i < items.length - 1 ? '<hr>' : '');
      }).join('') +
      '</div></div></div>';
  }

  // Mirrors the outer thoughtBlock — reasoning collapsible, collapsed first
  function thought(t) {
    if (!t) return '';
    return '<div class="thought" role="button">Thought for ' + t.seconds + ' seconds' + THOUGHT_CHEVRON + '</div>' +
      '<div class="thought-body markdown-prose" hidden><blockquote>' + t.body + '</blockquote></div>';
  }
  function aiMulti(variants) {
    var n = variants.length;
    return '<div class="message-listitem group"><div class="msg">' +
      '<div class="ai-avatar"><img src="${LOGO}" alt=""></div>' +
      '<div class="ai-col flex-auto w-0 pl-1 relative">' +
      '<div class="ai-model">Preview Model</div>' +
      '<div class="branches">' +
      variants.map(function (v, i) {
        return '<div class="branch' + (i === 0 ? ' active' : '') + '">' +
          '<div class="ai-stats">' + v.stats + '</div>' +
          '<div class="chat-assistant w-full min-w-full">' + thought(v.thought) +
          '<div class="markdown-prose">' + v.body + '</div>' +
          msgActions(v.stats, n > 1 ? branchPager(i, n) : '') +
          followUps(v.fups || null) +
          '</div></div>';
      }).join('') +
      '</div></div></div></div>';
  }
  function ai(stats, body, fups, t) {
    return aiMulti([{ stats: stats, body: body, fups: fups, thought: t }]);
  }
  // notFirst mirrors UserMessage.svelte's !isFirstMessage gate on Delete
  function user(text, notFirst) {
    return '<div class="message-listitem group"><div class="msg user-message group">' +
      '<div class="chat-user w-full min-w-full">' +
      '<div class="bubble-row"><div class="bubble rounded-3xl max-w-[90%] px-4 py-1.5 bg-gray-50 dark:bg-gray-850"><div class="markdown-prose">' + text + '</div></div></div>' +
      (notFirst ? USER_ACTIONS_DEL : USER_ACTIONS) +
      '</div></div></div>';
  }

  var CHATS = {
    'default': { title: 'Theme preview chat', html: msgs.innerHTML },
    'oklch': {
      title: 'OKLCH color ramps',
      html: user('How does the 12-step gray ramp work?') +
        ai('Response Speed: 112.6 t/s | Total Duration: 3.1s | Eval Count: 301 | Session: 2410 tokens',
          '<p>Each mode generates twelve <code>--color-gray-*</code> steps from a single OKLCH base. Tables inherit the palette too:</p>' +
          '<table><thead><tr><th>Step</th><th>Lightness</th><th>Typical use</th></tr></thead><tbody>' +
          '<tr><td><code>50</code></td><td>98%</td><td>Light backgrounds</td></tr>' +
          '<tr><td><code>850</code></td><td>27%</td><td>Bubbles, panels</td></tr>' +
          '<tr><td><code>950</code></td><td>16%</td><td>Sidebar</td></tr>' +
          '</tbody></table>' +
          '<p>Shifting the base <b>lightness</b> slides the whole ramp while keeping the perceptual spacing intact.</p>',
          ['Why OKLCH instead of HSL for the ramp?',
           'How do I keep contrast accessible across all twelve steps?',
           'Can I pin a single step and regenerate the rest?'])
    },
    'fx': {
      title: 'Canvas FX ideas',
      html: user('Give me some Canvas FX concepts that react to the conversation.') +
        ai('Response Speed: 96.2 t/s | Total Duration: 4.7s | Eval Count: 412 | Session: 3188 tokens',
          '<p>A few <b>context-aware</b> favorites:</p>' +
          '<ul><li><b>Sunflower</b>: seed to full bloom as tokens accumulate</li>' +
          '<li><b>Mushroom farm</b>: mycelium spreads with every reply</li>' +
          '<li><b>Hourglass</b>: sand drains as the window fills</li></ul>' +
          '<blockquote>Any metaphor with a clear empty-to-full progression works. The engine streams live token counts to your script.</blockquote>' +
          '<p>All of them fall back to <code>estimatedTokens</code> when exact usage data is unavailable.</p>',
          ['What data does the engine stream to my script?',
           'How expensive are these effects on a laptop GPU?',
           'Can a script react to which model is selected?'])
    },
    'gradient': {
      title: 'Gradient inspiration',
      html: user('What kind of gradients look good behind a chat UI?') +
        aiMulti([
          {
            stats: 'Response Speed: 101.4 t/s | Total Duration: 2.2s | Eval Count: 198 | Session: 1544 tokens',
            body: '<p>Three directions worth trying:</p>' +
              '<ol><li><b>Deep linear</b>: near-black corners into a saturated core</li>' +
              '<li><b>Radial glow</b>: a soft light source behind the input bar</li>' +
              '<li><b>Mesh</b>: several drifting color points for an aurora feel</li></ol>' +
              '<p>Browse the <a>preset gallery</a> for ready-made packs. Every one imports with a single URL.</p>',
            fups: ['How do I animate a gradient without it feeling busy?',
              'Which gradients hold up in light mode?',
              'Can I export a gradient I built back out as a preset?'],
          },
          {
            stats: 'Response Speed: 97.8 t/s | Total Duration: 2.6s | Eval Count: 214 | Session: 1602 tokens',
            body: '<p>Regenerated take: anchor a <b>radial glow</b> behind the input bar first, then layer a slow mesh drift over it for depth without motion sickness.</p>' +
              '<p>Keep the darkest stop near the sidebar so text stays readable, and preview both <code>dark</code> and <code>light</code> before committing.</p>',
            fups: ['What intensity works best for mesh gradients?',
              'Can the gradient animate only when the chat is idle?'],
          },
        ])
    },
    'transparency': {
      title: 'Structural transparency',
      html: user('How do effects show through the interface?') +
        ai('Response Speed: 108.9 t/s | Total Duration: 2.8s | Eval Count: 264 | Session: 2035 tokens',
          '<h4>The structural layer</h4>' +
          '<p>When Canvas FX or a gradient is active, layout containers turn <code>transparent</code> so the effect shines through, while the sidebar keeps a frosted-glass backdrop for readability.</p>' +
          '<p>Portaled menus and dialogs live <b>outside</b> the app container, so they stay opaque and every dropdown stays readable.</p>',
          ['Which containers get transparency applied?',
           'How do I keep code blocks readable over an effect?',
           'Does the frosted sidebar cost much performance?'],
          { seconds: 12, body: 'They are asking how effects remain visible behind the UI. The key is the structural layer: it clears backgrounds on the layout wrappers while the sidebar keeps a frosted backdrop, and portaled overlays are deliberately left opaque so menus stay legible.' })
    },
    'notes': {
      title: 'Preset gallery notes',
      html: user('Remind me what this gallery can preview.') +
        ai('Response Speed: 99.1 t/s | Total Duration: 1.6s | Eval Count: 142 | Session: 1210 tokens',
          '<p>Everything, live:</p>' +
          '<ul><li><b>Themes</b>: full palette on this mock UI, per mode</li>' +
          '<li><b>Canvas FX</b>: real Web Worker execution behind the chat</li>' +
          '<li><b>CSS presets</b>: applied to genuine Open WebUI markup</li>' +
          '<li><b>Gradients</b>: exact designer math, animation included</li></ul>',
          ['How do I submit a preset to this gallery?',
           'Can I preview a preset before importing it?',
           'What makes a good Canvas FX submission?'])
    },
    brand: {
      title: 'Brand palette v2',
      html: user('Can you turn our brand teal into a full gray ramp?') +
        ai('Response Speed: 97.4 t/s | Total Duration: 1.9s | Eval Count: 171 | Session: 1402 tokens',
          '<p>Sure. Keep the hue and drop the chroma low, and the twelve steps stay readable in every mode:</p>' +
          '<ul><li><b>Hue</b>: 192, your teal</li><li><b>Chroma</b>: 0.022 for surfaces, the accent keeps its full color</li><li><b>Lightness</b>: 18% for Dark, 0% for OLED</li></ul>',
          ['Show it on Light mode', 'Pick an accent that pairs with it'])
    },
    digest: {
      title: 'Weekly theme digest',
      html: user('Summarize what changed in the gallery this week.') +
        ai('Response Speed: 102.6 t/s | Total Duration: 2.1s | Eval Count: 204 | Session: 1688 tokens',
          '<p>A busy week:</p>' +
          '<ul><li><b>Koi Pond</b> and <b>Midsummer</b> joined the themes</li>' +
          '<li>Twelve Canvas FX now <b>follow your theme</b></li>' +
          '<li>The Studio can tune an animation\\'s own settings</li></ul>',
          null)
    },
    launch: {
      title: 'Launch checklist',
      html: user('What should I check before I submit a theme?') +
        ai('Response Speed: 95.8 t/s | Total Duration: 1.7s | Eval Count: 158 | Session: 1290 tokens',
          '<ol><li>Each mode reads well: Dark, OLED, Light and Her</li>' +
          '<li>Text keeps its contrast over the background</li>' +
          '<li>The Canvas FX runs smoothly on a laptop</li>' +
          '<li>The preview link from the submission bot looks right</li></ol>',
          null)
    },
    'new': {
      title: 'New Chat',
      html: '<div class="placeholder">' +
        '<div class="ph-head">' +
        '<div class="ph-temp" data-tip="This chat won\\'t appear in history and your messages will not be saved.">' + EYE_SVG + 'Temporary Chat</div>' +
        '<div class="ph-logo"><img src="${LOGO}" alt=""></div>' +
        '<div class="ph-model">Preview Model</div>' +
        '<div class="ph-sub">How can I help you today?</div>' +
        '</div>' +
        '<div class="ph-suggest">' +
        '<div class="ph-label">' + BOLT_SVG + 'Suggested</div>' +
        '<div class="ph-grid">' +
        '<div class="ph-card"><b>Tell me a fun fact</b><span>about the Roman Empire</span></div>' +
        '<div class="ph-card"><b>Help me study</b><span>vocabulary for a college entrance exam</span></div>' +
        '<div class="ph-card"><b>Explain options trading</b><span>if I\\'m familiar with buying and selling stocks</span></div>' +
        '<div class="ph-card"><b>Overcome procrastination</b><span>give me tips</span></div>' +
        '<div class="ph-card"><b>Show me a code snippet</b><span>of a website\\'s sticky header</span></div>' +
        '<div class="ph-card"><b>Give me ideas</b><span>for what to do with my kids\\' art</span></div>' +
        '</div></div></div>'
    }
  };

  var mainEl = document.querySelector('main');
  function openChat(key) {
    var channel = key.indexOf('channel:') === 0;
    var chat = CHATS[key];
    if (!chat && !channel) return;
    stopReply();
    currentKey = key;
    document.querySelectorAll('.side-item.active, .channel-item.active').forEach(function (a) { a.classList.remove('active'); });
    if (key !== 'new') {
      document.querySelectorAll('.side-scroll [data-chat="' + key + '"]').forEach(function (item) {
        item.classList.add('active');
        item.classList.remove('unread');
        var dot = item.querySelector('.unread-dot, .unread-badge');
        if (dot) dot.remove();
      });
    }
    // A saved chat is never temporary; the Thread and Controls panes belong
    // to the view they were opened from
    if (key !== 'new' && window.__tdpTemp) window.__tdpTemp(false);
    if (window.__tdpPanes) window.__tdpPanes.closeAll(channel);
    mainEl.classList.toggle('channel-mode', channel);
    if (channel) {
      if (window.__tdpChannel) window.__tdpChannel.open(key.slice(8));
      sendContext(true);
      try { parent.postMessage({ __tdpPreview: true, kind: 'chatchange', chat: key }, '*'); } catch (e) {}
      return;
    }
    if (window.__tdpChannel) window.__tdpChannel.close();
    var row = document.querySelector('.side-scroll [data-chat="' + key + '"] .label');
    navTitle.textContent = key !== 'new' && row ? row.textContent : chat.title;
    // Navbar.svelte gates Temporary Chat on !chat?.id, so it shows only here
    var navRight = document.getElementById('nav-right');
    if (navRight) navRight.classList.toggle('is-new', key === 'new');
    // and :117 gates the title + chat menu on chat?.id — hidden on New Chat
    var navEl = document.querySelector('nav');
    if (navEl) navEl.classList.toggle('is-new', key === 'new');
    msgs.innerHTML = chat.html;
    msgs.scrollTop = 0;
    sendContext(true);
    // the offset is measured, so it has to be recomputed for the new content
    if (window.__tdpApplyInset) window.__tdpApplyInset();
    try { parent.postMessage({ __tdpPreview: true, kind: 'chatchange', chat: key }, '*'); } catch (e) {}
  }

  // Delegated, so rows the chat menu renames, clones or moves keep working
  document.addEventListener('click', function (e) {
    var el = e.target && e.target.closest ? e.target.closest('[data-chat]') : null;
    if (!el || e.target.closest('.chat-actions, .ch-settings, input')) return;
    openChat(el.getAttribute('data-chat'));
  });
  window.__tdpOpenChat = openChat;

  // Branch pager: show the previous/next regenerated variant. Bounds are
  // guarded like showPreviousMessage/showNextMessage upstream.
  msgs.addEventListener('click', function (e) {
    var nav = e.target && e.target.closest ? e.target.closest('.branch-nav') : null;
    if (!nav || !msgs.contains(nav)) return;
    var wrap = nav.closest('.branches');
    if (!wrap) return;
    var branches = [].slice.call(wrap.children);
    var cur = -1;
    branches.forEach(function (b, i) { if (b.classList.contains('active')) cur = i; });
    var next = nav.classList.contains('branch-next') ? cur + 1 : cur - 1;
    if (cur < 0 || next < 0 || next >= branches.length) return;
    branches[cur].classList.remove('active');
    branches[next].classList.add('active');
  });

  // Reasoning collapsibles toggle like Collapsible.svelte. Delegated from the
  // container so chat switches (which replace innerHTML) keep working.
  msgs.addEventListener('click', function (e) {
    var t = e.target && e.target.closest ? e.target.closest('.thought') : null;
    if (!t || !msgs.contains(t)) return;
    var body = t.nextElementSibling;
    if (!body || !body.classList.contains('thought-body')) return;
    var open = body.hasAttribute('hidden');
    if (open) body.removeAttribute('hidden'); else body.setAttribute('hidden', '');
    t.classList.toggle('open', open);
  });

  // Follow-ups get answered. Nothing typed is sent here (channels take
  // typing), but a follow-up on the last reply is sent as your message and
  // Preview Model streams an answer, with follow-ups of its own. The chat
  // grows like a real one, so a Canvas FX script that reacts to the chat has
  // something to react to.
  var REPLIES = ${JSON.stringify(MOCK_REPLIES)};
  var currentKey = 'default';
  var reply = null;

  function escText(t) { return String(t).split('&').join('&amp;').split('<').join('&lt;').split('>').join('&gt;'); }
  function textLength(html) { var d = document.createElement('div'); d.innerHTML = html; return (d.textContent || '').length; }
  function askedHere() {
    var out = {};
    msgs.querySelectorAll('.chat-user').forEach(function (el) { out[(el.textContent || '').trim()] = true; });
    return out;
  }
  // The next questions not yet asked in this chat, after the one just asked
  function nextFollowUps(from) {
    var asked = askedHere(), out = [];
    for (var n = 1; n <= REPLIES.length && out.length < 3; n++) {
      var r = REPLIES[(from + n) % REPLIES.length];
      if (!asked[r.q]) out.push(r.q);
    }
    return out;
  }
  // Tags whole, text a word at a time
  function pieces(html) {
    var out = [], i = 0;
    while (i < html.length) {
      var k;
      if (html.charAt(i) === '<') {
        k = html.indexOf('>', i);
        k = k < 0 ? html.length : k + 1;
      } else {
        k = i;
        while (k < html.length && html.charAt(k) !== '<' && html.charAt(k) !== ' ') k++;
        while (k < html.length && html.charAt(k) === ' ') k++;
      }
      out.push(html.slice(i, k));
      i = k;
    }
    return out;
  }
  function nearBottom() { return msgs.scrollHeight - msgs.scrollTop - msgs.clientHeight < 80; }

  function stopReply() {
    if (!reply) return;
    clearTimeout(reply.timer);
    reply = null;
  }

  function answer(q) {
    var at = -1;
    REPLIES.forEach(function (r, i) { if (r.q === q) at = i; });
    var body = at >= 0 ? REPLIES[at].body : '<p>Good question. Try it in the designer: every change shows here as you make it.</p>';
    msgs.querySelectorAll('.followups-wrap').forEach(function (w) { w.remove(); });
    msgs.insertAdjacentHTML('beforeend', user(escText(q), true));
    var fups = nextFollowUps(at);
    var promptTokens = Math.ceil(contextNow().chars / 4);
    var tokens = Math.ceil(textLength(body) / 4);
    var speed = 92 + Math.random() * 24;
    var stats = 'Response Speed: ' + speed.toFixed(1) + ' t/s | Total Duration: ' + (tokens / speed + 0.4).toFixed(2) +
      's | Eval Count: ' + tokens + ' | Session: ' + (promptTokens + tokens) + ' tokens';
    var done = ai(stats, body, fups.length ? fups : null);
    // The finished exchange is the chat's from now on, so switching chats or
    // modes mid-reply keeps it whole
    CHATS[currentKey].html = msgs.innerHTML + done;
    grown[currentKey] = true;
    if (window.__tdpState) window.__tdpState.save();

    msgs.insertAdjacentHTML('beforeend', ai('', '<span class="tdp-wait"><i></i><i></i><i></i></span>', null));
    var row = msgs.lastElementChild;
    row.classList.add('streaming');
    var prose = row.querySelector('.markdown-prose');
    msgs.scrollTop = msgs.scrollHeight;
    var parts = pieces(body), shown = 0, html = '';
    function finish() {
      reply = null;
      var stick = nearBottom();
      row.outerHTML = done;
      if (stick) msgs.scrollTop = msgs.scrollHeight;
      // What the socket hook hands a script once the reply ends: the exact count
      sendContext(true);
      window.dispatchEvent(new CustomEvent('tdp-context', { detail: {
        exactTokens: promptTokens + tokens, promptTokens: promptTokens, completionTokens: tokens, source: 'socket'
      } }));
    }
    function tick() {
      var stick = nearBottom();
      for (var n = 0; n < 2 && shown < parts.length; n++) html += parts[shown++];
      prose.innerHTML = html;
      if (stick) msgs.scrollTop = msgs.scrollHeight;
      if (shown < parts.length) reply.timer = setTimeout(tick, 32);
      else finish();
    }
    reply = { timer: setTimeout(tick, 550) };
  }

  msgs.addEventListener('click', function (e) {
    var f = e.target && e.target.closest ? e.target.closest('.followup') : null;
    if (!f || !msgs.contains(f) || reply) return;
    answer((f.getAttribute('data-tip') || f.textContent || '').trim());
  });

  // What the bootloader sends a Canvas FX script about the chat: the message
  // count, characters and estimated tokens, at most every 2 seconds while the
  // chat changes, and straight away for another chat. Nothing in a channel.
  function contextNow() {
    if (mainEl.classList.contains('channel-mode')) return { messages: 0, chars: 0, estimatedTokens: 0 };
    var chars = 0;
    msgs.querySelectorAll('.chat-user, .chat-assistant').forEach(function (el) { chars += (el.textContent || '').length; });
    return { messages: msgs.querySelectorAll('.message-listitem').length, chars: chars, estimatedTokens: Math.ceil(chars / 4) };
  }
  var lastContext = '', contextTimer = 0;
  function sendContext(now) {
    if (!now) {
      if (!contextTimer) contextTimer = setTimeout(function () { contextTimer = 0; sendContext(true); }, 2000);
      return;
    }
    var c = contextNow(), sig = JSON.stringify(c);
    if (sig === lastContext) return;
    lastContext = sig;
    window.dispatchEvent(new CustomEvent('tdp-context', { detail: c }));
  }
  new MutationObserver(function () { sendContext(false); }).observe(msgs, { childList: true, subtree: true, characterData: true });

  // A chat grown here comes back after a rebuild for another mode or palette
  var grown = {};
  var savedChats = window.__tdpState && window.__tdpState.saved.chats;
  if (savedChats && typeof savedChats === 'object') {
    Object.keys(savedChats).forEach(function (k) {
      if (!CHATS[k] || typeof savedChats[k] !== 'string') return;
      CHATS[k].html = savedChats[k];
      grown[k] = true;
    });
    if (grown['default']) msgs.innerHTML = CHATS['default'].html;
  }
  if (window.__tdpState) window.__tdpState.add('chats', function () {
    var out = {};
    Object.keys(grown).forEach(function (k) { out[k] = CHATS[k].html; });
    return out;
  });

  var initial = document.body.getAttribute('data-initial-chat');
  // Channels are set up by a later script, so they open once it has run
  if (initial && initial !== 'default' && (CHATS[initial] || initial.indexOf('channel:') === 0)) setTimeout(function () { openChat(initial); }, 0);
  else sendContext(true);
})();

// Sidebar collapse/expand. Open WebUI drives this from the showSidebar store:
// the expanded panel's header button closes it, and the collapsed rail (the
// whole column is a button) reopens it.
(function () {
  var app = document.querySelector('.app');
  var rail = document.querySelector('.sidebar-rail');
  var toggle = document.querySelector('.brand .panel-icon');
  if (!app || !rail || !toggle) return;

  var backdrop = document.getElementById('backdrop');
  var navToggle = document.querySelector('.nav-toggle');
  var MOBILE = 768; // routes/+layout.svelte BREAKPOINT

  // Sandboxed srcdoc frames run this script before layout, while innerWidth
  // is still 0 — which must not read as "mobile", or every mode-pill rebuild
  // would collapse an expanded sidebar.
  function isMobile() { return window.innerWidth > 0 && window.innerWidth < MOBILE; }

  var panel = document.querySelector('.sidebar-panel');

  // Upstream the rail (Sidebar.svelte:810) and the expanded panel (:983) are
  // separate elements behind an {#if}, but both carry id="sidebar" — so only
  // ever one exists and preset CSS written against #sidebar hits whichever is
  // showing. The mock keeps both in the DOM for the collapse transition, so
  // the id is moved to the visible one instead. Without this, a theme styling
  // #sidebar (rounding, borders, backgrounds) would skip the collapsed rail.
  function syncSidebarId(collapsed) {
    var active = collapsed ? rail : panel;
    var inactive = collapsed ? panel : rail;
    if (inactive) inactive.removeAttribute('id');
    if (active) active.id = 'sidebar';
  }

  function setCollapsed(on) {
    app.classList.toggle('collapsed', on);
    syncSidebarId(on);
    try { parent.postMessage({ __tdpPreview: true, kind: 'sidebarchange', collapsed: on }, '*'); } catch (e) {}
  }

  toggle.addEventListener('click', function (e) { e.stopPropagation(); setCollapsed(true); });

  // The rail expands when you click the logo or empty space, but its action
  // buttons don't — upstream each one calls stopImmediatePropagation so it
  // runs its own handler instead of the enclosing expand button. The New Chat
  // pencil therefore opens the New Chat view with the sidebar still collapsed,
  // and the user menu sits outside that button entirely.
  rail.addEventListener('click', function () { setCollapsed(false); });
  rail.querySelectorAll('.rail-action').forEach(function (el) {
    el.addEventListener('click', function (e) { e.stopPropagation(); });
  });
  if (navToggle) navToggle.addEventListener('click', function (e) { e.stopPropagation(); setCollapsed(false); });
  if (backdrop) backdrop.addEventListener('click', function () { setCollapsed(true); });

  // showSidebar defaults to false on mobile, and closes when crossing into it.
  // Applied twice: once synchronously (attr only, width may still be 0) and
  // once after first layout, when the mobile default can be judged for real.
  var attrCollapsed = document.body.getAttribute('data-sidebar-collapsed') === '1';
  var wasMobile = isMobile();
  function applyInitial() {
    var start = attrCollapsed || isMobile();
    app.classList.toggle('collapsed', start);
    syncSidebarId(start);
    wasMobile = isMobile();
  }
  applyInitial();
  requestAnimationFrame(applyInitial);
  window.addEventListener('resize', function () {
    var now = isMobile();
    if (now !== wasMobile) {
      wasMobile = now;
      if (now) setCollapsed(true);
    }
  });
})();

// Tippy-style tooltips (Tooltip.svelte, theme 'dark'). One fixed element,
// delegated hover, placements top (default) / bottom / right matching the
// upstream call sites. Delegation keeps injected chat HTML working.
(function () {
  var tip = document.createElement('div');
  tip.id = 'tdp-tooltip';
  document.body.appendChild(tip);
  var current = null;
  document.addEventListener('mouseover', function (e) {
    var el = e.target && e.target.closest ? e.target.closest('[data-tip]') : null;
    if (el === current) return;
    current = el;
    if (!el) { tip.classList.remove('show'); return; }
    tip.textContent = el.getAttribute('data-tip');
    tip.classList.toggle('pre', tip.textContent.indexOf('\\n') !== -1);
    tip.classList.add('show');
    var r = el.getBoundingClientRect();
    var p = el.getAttribute('data-tip-placement') || 'top';
    var tw = tip.offsetWidth, th = tip.offsetHeight, x, y;
    if (p === 'bottom') { x = r.left + r.width / 2 - tw / 2; y = r.bottom + 6; }
    else if (p === 'right') { x = r.right + 6; y = r.top + r.height / 2 - th / 2; }
    else { x = r.left + r.width / 2 - tw / 2; y = r.top - th - 6; }
    x = Math.max(4, Math.min(x, window.innerWidth - tw - 4));
    y = Math.max(4, Math.min(y, window.innerHeight - th - 4));
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
  });
  document.addEventListener('mouseleave', function () { current = null; tip.classList.remove('show'); }, true);
})();

// The input bar's menus as mocks that work: More (InputMenu.svelte),
// Integrations (IntegrationsMenu.svelte) and the model selector
// (ModelSelector/Selector.svelte). Each is portaled to <body> and opens above
// its button, as Dropdown.svelte flips up at the bottom of the screen, and
// carries the upstream class names so a preset's CSS reaches it too. They
// show what an admin sees with web search, image generation and three tools
// set up; a row that would upload, capture or fetch just closes the menu.
(function () {
  var moreBtn = document.getElementById('input-menu-button');
  var intBtn = document.getElementById('integration-menu-button');
  var modelBtn = document.getElementById('model-selector-model-button');
  var pills = document.getElementById('input-pills');
  if (!moreBtn || !intBtn || !modelBtn || !pills) return;
  var I = ${JSON.stringify({ share: SVG.share, download: SVG.download, editPencil: SVG.editPencil, chatCheck: SVG.chatCheck, pin: SVG.pin, pinSlash: SVG.pinSlash, copy: SVG.copy, folder: SVG.folder, archiveBox: SVG.archiveBox, trash: SVG.trash, cog: SVG.cog, logOut: SVG.logOut, emojiFace: SVG.emojiFace, user: SVG.user, workspace: SVG.workspace, notes: SVG.notes, code: SVG.code, checkAlt: SVG.check, clip: SVG.clip, camera: SVG.camera, globeAlt: SVG.globeAlt, documentArrowUp: SVG.documentArrowUp, pageEdit: SVG.pageEdit, database: SVG.database, clockRotateRight: SVG.clockRotateRight, chevronRightAlt: SVG.chevronRightAlt, chevronLeft: SVG.chevronLeft, chevronDown: SVG.chevronDown, wrench: SVG.wrench, searchAlt: SVG.searchAlt, photo: SVG.photo, terminal: SVG.terminal, check: SVG.check, keyframes: SVG.keyframes, documentPage: SVG.documentPage, dots: SVG.dots, globePill: SVG.globePill, photoPill: SVG.photoPill, terminalPill: SVG.terminalPill, wrenchPill: SVG.wrenchPill, xMark: SVG.xMark })};
  var LOGO_SRC = ${JSON.stringify(LOGO)};

  var TOOLS = ['Web Scraper', 'Calculator', 'Weather'];
  var FILES = ['palette-notes.md', 'brand-guidelines.pdf', 'dark-mode-screenshot.png', 'theme-export.json'];
  var NOTES = ['Theme ideas', 'Font pairings'];
  var KNOWLEDGE = [
    { name: 'Design System', files: ['tokens.json', 'components.md'] },
    { name: 'Product Docs', files: ['getting-started.md', 'faq.md'] }
  ];
  var MODELS = [
    { name: 'Preview Model' }, { name: 'Preview Model Mini' }, { name: 'Reasoning Model' },
    { name: 'Vision Model' }, { name: 'Local Model', size: '8B' }
  ];
  var state = { webSearch: false, image: false, code: false, tools: [false, false, false], model: 0, compare: false };
  var open = null;

  function chats() {
    var out = [];
    document.querySelectorAll('.side-scroll [data-chat] .label').forEach(function (el) {
      if (el.closest('[data-chat="new"]')) return;
      if (out.indexOf(el.textContent) === -1) out.push(el.textContent);
    });
    return out;
  }
  function esc(t) { return String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;'); }
  function chev() { return '<span class="chev">' + I.chevronRightAlt + '</span>'; }
  function sw(on) { return '<span class="tdp-switch' + (on ? ' on' : '') + '" role="switch" aria-checked="' + on + '"></span>'; }
  function row(act, ico, label, more) {
    return '<button type="button" class="tdp-row" data-act="' + act + '">' + ico + '<span class="label">' + label + '</span>' + (more || '') + '</button>';
  }
  function toggleRow(key, ico, label) {
    return '<button type="button" class="tdp-row" data-act="toggle:' + key + '" aria-pressed="' + state[key] + '"><span class="ico' + (key === 'webSearch' ? '' : ' sm') + '">' + ico + '</span><span class="label">' + label + '</span>' + sw(state[key]) + '</button>';
  }
  function isNewChat() { var n = document.querySelector('nav'); return !!(n && n.classList.contains('is-new')); }

  // ---- Opening, placing and closing ----

  function hideTip() { var t = document.getElementById('tdp-tooltip'); if (t) t.classList.remove('show'); }

  function close() {
    if (!open) return;
    closeSub();
    open.el.remove();
    open.trigger.setAttribute('aria-expanded', 'false');
    if (open.ctx && open.ctx.onClose) open.ctx.onClose();
    open = null;
  }

  // Where each menu sits: Dropdown.svelte puts a menu's left edge (start) or
  // right edge (end) on its trigger, 4px off it and 16px from the window's
  // edges; Selector.svelte uses end, 2px and 8px
  var SPECS = {
    more: { align: 'start', gap: 4, pad: 16 }, integrations: { align: 'start', gap: 4, pad: 16 },
    model: { align: 'end', gap: 2, pad: 8 }, chat: { align: 'start', gap: 4, pad: 16 },
    user: { align: 'end', gap: 4, pad: 16 }, chatsMore: { align: 'end', gap: 4, pad: 16 },
    emoji: { align: 'start', gap: 4, pad: 16 }
  };

  function show(kind, trigger, view, ctx) {
    var was = open && open.kind, wasTrigger = open && open.trigger;
    close();
    if (was === kind && wasTrigger === trigger && !view) return;
    hideTip();
    var el = document.createElement('div');
    open = { kind: kind, el: el, trigger: trigger, ctx: ctx || {} };
    trigger.setAttribute('aria-expanded', 'true');
    document.body.appendChild(el);
    if (kind === 'more') renderMore(view || 'root', 'from-left');
    else if (kind === 'integrations') renderIntegrations(view || 'root', 'from-left');
    else if (kind === 'model') renderModels();
    else RENDER[kind](el, open.ctx);
    place();
    el.addEventListener('click', onMenuClick);
    el.addEventListener('mouseover', onMenuHover);
    requestAnimationFrame(function () { if (open && open.el === el) el.classList.add('in'); });
  }

  // Both open downward unless there is more room above (the input bar's and
  // the user menu flip up; a chat's opens down)
  function place() {
    if (!open) return;
    var el = open.el, r = open.trigger.getBoundingClientRect();
    var spec = SPECS[open.kind];
    var w = el.offsetWidth, h = el.offsetHeight;
    // An end-aligned menu is placed by its right edge, so only that side
    // keeps the gap from the window
    var left = spec.align === 'end' ? r.right - w : r.left;
    left = Math.min(left, window.innerWidth - w - spec.pad);
    if (spec.align !== 'end') left = Math.max(spec.pad, left);
    left = Math.max(4, left);
    var below = window.innerHeight - r.bottom;
    var top = below >= r.top ? r.bottom + spec.gap : r.top - spec.gap - h;
    el.style.left = left + 'px';
    el.style.top = Math.max(8, top) + 'px';
  }

  // DropdownSub.svelte: opens on hover to the right of its row, 8px off,
  // flipping left when there is no room
  function openSub(row, html) {
    if (open.sub && open.sub.row === row) return;
    closeSub();
    var sub = document.createElement('div');
    sub.className = 'tdp-menu submenu app-dropdown-menu in';
    sub.setAttribute('role', 'menu');
    sub.innerHTML = html;
    document.body.appendChild(sub);
    var mr = open.el.getBoundingClientRect(), rr = row.getBoundingClientRect();
    sub.style.minWidth = mr.width + 'px';
    var left = mr.right + 8;
    if (left + sub.offsetWidth > window.innerWidth - 8) left = mr.left - 8 - sub.offsetWidth;
    sub.style.left = Math.max(8, left) + 'px';
    sub.style.top = Math.max(8, Math.min(rr.top - 3, window.innerHeight - sub.offsetHeight - 8)) + 'px';
    sub.addEventListener('click', onMenuClick);
    open.sub = { el: sub, row: row };
    row.classList.add('hl');
  }
  function closeSub() {
    if (!open || !open.sub) return;
    open.sub.row.classList.remove('hl');
    open.sub.el.remove();
    open.sub = null;
  }

  document.addEventListener('mousedown', function (e) {
    if (!open || open.el.contains(e.target) || open.trigger.contains(e.target)) return;
    if (open.sub && open.sub.el.contains(e.target)) return;
    close();
  }, true);
  // Escape closes the menu, and goes no further: the page around the
  // preview would take it as closing the whole preview
  window.addEventListener('keydown', function (e) {
    if (open && e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); }
  }, true);
  window.addEventListener('resize', place);

  moreBtn.addEventListener('click', function () { show('more', moreBtn); });
  intBtn.addEventListener('click', function () { show('integrations', intBtn); });
  modelBtn.addEventListener('click', function () { show('model', modelBtn); });

  // ---- More ----

  function renderMore(view, fly) {
    var el = open.el;
    el.className = 'tdp-menu input-menu app-dropdown-menu rounded-xl border border-gray-100 dark:border-gray-800 z-50 bg-white dark:bg-gray-850 dark:text-white shadow-lg' + (open.el.classList.contains('in') ? ' in' : '');
    el.setAttribute('role', 'menu');
    open.view = view;
    if (view === 'root') {
      el.innerHTML = '<div class="tdp-view ' + fly + '">' +
        row('close', I.clip, 'Upload Files') +
        row('close', I.camera, 'Capture') +
        row('close', I.globeAlt, 'Attach Webpage') +
        row('tab:files', I.documentArrowUp, 'Attach Files', chev()) +
        row('tab:notes', I.pageEdit, 'Attach Notes', chev()) +
        row('tab:knowledge', I.database, 'Attach Knowledge', chev()) +
        row('tab:chats', I.clockRotateRight, 'Reference Chats', chev()) +
        '</div>';
    } else {
      var t = SUBTABS[view];
      el.innerHTML = '<div class="tdp-view ' + fly + '">' +
        '<button type="button" class="tdp-row back" data-act="back">' + I.chevronLeft + '<span class="label">' + t.title + '</span></button>' +
        '<div class="tdp-search">' + I.searchAlt + '<input placeholder="' + t.placeholder + '" autocomplete="off"></div>' +
        '<div class="tdp-list"></div></div>';
      var input = el.querySelector('input');
      input.addEventListener('input', function () { fillList(view, input.value); });
      fillList(view, '');
    }
    place();
  }

  var SUBTABS = {
    files: { title: 'Files', placeholder: 'Search Files', empty: 'No files found', items: function () { return FILES; } },
    notes: { title: 'Notes', placeholder: 'Search Notes', empty: 'No notes found', items: function () { return NOTES; } },
    knowledge: { title: 'Knowledge', placeholder: 'Search Knowledge', empty: 'No knowledge bases found.', items: function () { return KNOWLEDGE.map(function (k) { return k.name; }); } },
    chats: { title: 'Chats', placeholder: 'Search Chats', empty: 'No chats found', items: chats }
  };

  function fillList(view, q) {
    var list = open.el.querySelector('.tdp-list');
    var t = SUBTABS[view];
    var items = t.items().filter(function (n) { return n.toLowerCase().indexOf(q.trim().toLowerCase()) !== -1; });
    if (!items.length) { list.innerHTML = '<div class="tdp-empty">' + t.empty + '</div>'; place(); return; }
    list.innerHTML = items.map(function (name, i) {
      var hl = i === 0 ? ' hl' : '';
      if (view === 'knowledge') {
        var kb = KNOWLEDGE.filter(function (k) { return k.name === name; })[0];
        return '<div class="tdp-row item' + hl + '" data-act="close"><span class="ico sm">' + I.database + '</span><span class="label">' + esc(name) + '</span>' +
          '<span class="expand" data-act="expand" role="button" aria-label="Show files">' + I.chevronRightAlt + '</span></div>' +
          '<div class="tdp-kb-files" hidden>' + kb.files.map(function (f) {
            return '<div class="tdp-row item" data-act="close"><span class="doc">' + I.documentPage + '</span><span class="label">' + esc(f) + '</span></div>';
          }).join('') + '</div>';
      }
      var ico = view === 'chats' ? '' : '<span class="doc">' + (view === 'notes' ? I.pageEdit : I.documentPage) + '</span>';
      return '<button type="button" class="tdp-row item' + hl + '" data-act="close">' + ico + '<span class="label">' + esc(name) + '</span></button>';
    }).join('');
    place();
  }

  // ---- Integrations ----

  function renderIntegrations(view, fly) {
    var el = open.el;
    el.className = 'tdp-menu integrations-menu app-dropdown-menu rounded-xl border border-gray-100 dark:border-gray-800 z-50 bg-white dark:bg-gray-850 dark:text-white shadow-lg' + (open.el.classList.contains('in') ? ' in' : '');
    el.setAttribute('role', 'menu');
    open.view = view;
    if (view === 'root') {
      el.innerHTML = '<div class="tdp-view ' + fly + '">' +
        row('tab:tools', I.wrench, 'Tools <span class="count">' + TOOLS.length + '</span>', chev()) +
        toggleRow('webSearch', I.globeAlt, 'Web Search') +
        toggleRow('image', I.photo, 'Image') +
        toggleRow('code', I.terminal, 'Code Interpreter') +
        '</div>';
    } else {
      el.innerHTML = '<div class="tdp-view ' + fly + '">' +
        '<button type="button" class="tdp-row back" data-act="back">' + I.chevronLeft + '<span class="label">Tools <span class="count">' + TOOLS.length + '</span></span></button>' +
        '<div class="tdp-search">' + I.searchAlt + '<input placeholder="Search tools" autocomplete="off"></div>' +
        '<div class="tdp-list"></div></div>';
      var input = el.querySelector('input');
      input.addEventListener('input', function () { fillTools(input.value); });
      fillTools('');
    }
    place();
  }

  function fillTools(q) {
    var list = open.el.querySelector('.tdp-list');
    var rows = TOOLS.map(function (name, i) {
      if (name.toLowerCase().indexOf(q.trim().toLowerCase()) === -1) return '';
      return '<button type="button" class="tdp-row" data-act="tool:' + i + '" aria-pressed="' + state.tools[i] + '"><span class="ico">' + I.wrench + '</span><span class="label">' + esc(name) + '</span>' + sw(state.tools[i]) + '</button>';
    }).join('');
    list.innerHTML = rows || '<div class="tdp-empty">No tools found</div>';
    place();
  }

  // The pills after Integrations: selected tools, then each feature that is on
  function renderPills() {
    var n = state.tools.filter(Boolean).length, h = '';
    if (n) h += '<span class="tools-pill" data-pill="tools" role="button" aria-label="Available Tools" data-tip="' + n + ' Available Tools">' + I.wrenchPill + '<span>' + n + '</span></span>';
    if (state.webSearch) h += pill('webSearch', 'Web Search', I.globePill);
    if (state.image) h += pill('image', 'Image', I.photoPill);
    if (state.code) h += pill('code', 'Code Interpreter', '<span class="terminal-icon">' + I.terminalPill + '</span>');
    pills.innerHTML = h;
  }
  function pill(key, tip, ico) {
    return '<span class="feature-pill group" data-pill="' + key + '" role="button" aria-pressed="true" data-tip="' + tip + '">' + ico + '<span class="x">' + I.xMark + '</span></span>';
  }
  pills.addEventListener('click', function (e) {
    var p = e.target.closest('[data-pill]');
    if (!p) return;
    hideTip();
    if (p.dataset.pill === 'tools') { show('integrations', intBtn, 'tools'); return; }
    state[p.dataset.pill] = false;
    renderPills();
    if (open && open.kind === 'integrations' && open.view === 'root') renderIntegrations('root', '');
  });

  // ---- Model selector ----

  function renderModels() {
    var el = open.el;
    el.className = 'tdp-menu model-menu z-40 w-[20rem] rounded-xl border border-gray-100 bg-white p-0.5 shadow-lg dark:border-gray-800 dark:bg-gray-850 dark:text-white';
    el.innerHTML = '<div class="tdp-search">' + I.searchAlt +
      '<input id="model-search-input" placeholder="Search a model" autocomplete="off" aria-label="Search In Models">' +
      '<span class="compare' + (state.compare ? ' on' : '') + '" data-act="compare" role="button" aria-label="Compare">' + I.keyframes + '</span></div>' +
      '<div class="model-list" role="listbox" aria-label="Available models"></div>' +
      (isNewChat() ? '<div class="model-foot"><span data-act="close">Set as default</span></div>' : '<div class="pb"></div>');
    var input = el.querySelector('input');
    open.hl = state.model;
    input.addEventListener('input', function () { open.hl = -1; fillModels(input.value); });
    input.addEventListener('keydown', function (e) {
      var rows = [].slice.call(open.el.querySelectorAll('.model-item'));
      if (!rows.length) return;
      var at = rows.findIndex(function (r) { return r.classList.contains('hl'); });
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        at = e.key === 'ArrowDown' ? Math.min(rows.length - 1, at + 1) : Math.max(0, at - 1);
        rows.forEach(function (r, i) { r.classList.toggle('hl', i === at); });
        rows[at].scrollIntoView({ block: 'nearest' });
      } else if (e.key === 'Enter') {
        e.preventDefault();
        pickModel(Number((rows[at] || rows[0]).dataset.model));
      }
    });
    fillModels('');
    input.focus();
  }

  function fillModels(q) {
    var list = open.el.querySelector('.model-list');
    var shown = MODELS.map(function (m, i) { return i; }).filter(function (i) { return MODELS[i].name.toLowerCase().indexOf(q.trim().toLowerCase()) !== -1; });
    // A search highlights its first result, for the arrow keys and Enter
    if (shown.indexOf(open.hl) === -1) open.hl = shown.length ? shown[0] : -1;
    var rows = MODELS.map(function (m, i) {
      if (shown.indexOf(i) === -1) return '';
      var sel = i === state.model;
      return '<div class="model-item' + (sel ? ' selected' : '') + (i === open.hl ? ' hl' : '') + '" role="option" aria-selected="' + sel + '" data-model="' + i + '">' +
        '<div class="main"><img src="' + LOGO_SRC + '" alt=""><span class="name">' + esc(m.name) + '</span>' + (m.size ? '<span class="size">' + m.size + '</span>' : '') + '</div>' +
        '<div class="end"><span aria-label="More Options">' + I.dots + '</span>' + (sel ? '<span class="tick">' + I.check + '</span>' : '') + '</div></div>';
    }).join('');
    list.innerHTML = rows || '<div class="model-empty">No results found</div>';
    place();
  }

  function pickModel(i) {
    state.model = i;
    modelBtn.querySelector('.name').textContent = MODELS[i].name;
    close();
  }

  // ---- Sidebar menus, the Chats header's More and the reaction picker ----

  function mi(act, ico, label, more) {
    return '<button type="button" class="mi" data-act="do:' + act + '">' + (ico || '') + '<span class="label">' + label + '</span>' + (more || '') + '</button>';
  }
  var RENDER = {
    // Sidebar/ChatMenu.svelte, for an admin with one folder
    chat: function (el, ctx) {
      el.className = 'tdp-menu chat-menu app-dropdown-menu select-none rounded-xl border border-gray-100 dark:border-gray-800 bg-white dark:bg-gray-850 dark:text-white shadow-lg';
      el.setAttribute('role', 'menu');
      var pinned = ctx.pinned();
      el.innerHTML =
        mi('none', I.share, 'Share') +
        '<button type="button" class="mi sub" data-act="do:download" data-sub="download">' + I.download + '<span class="label">Download</span></button>' +
        mi('rename', I.editPencil, 'Rename') +
        mi('unread', I.chatCheck, 'Mark as unread') +
        '<hr>' +
        mi('pin', pinned ? I.pinSlash : I.pin, pinned ? 'Unpin' : 'Pin') +
        mi('clone', I.copy, 'Clone') +
        '<button type="button" class="mi sub" data-act="do:move" data-sub="move">' + I.folder + '<span class="label">Move</span></button>' +
        mi('archive', I.archiveBox, 'Archive') +
        mi('delete', I.trash, 'Delete');
    },
    // Sidebar/UserMenu.svelte, an admin with user status on
    user: function (el, ctx) {
      el.className = 'tdp-menu user-menu app-dropdown-menu font-sans text-xs rounded-xl border border-gray-100 dark:border-gray-800 bg-white dark:bg-gray-850 dark:text-white shadow-lg';
      el.setAttribute('role', 'menu');
      el.style.width = ctx.width + 'px';
      var st = ctx.status();
      el.innerHTML =
        '<button type="button" class="mi who" data-act="close"><span class="ico"><span class="avatar"></span></span><span class="label">You</span><span class="active-users" data-tip="Active Users"><i></i>1</span></button>' +
        (st
          ? '<button type="button" class="mi status" data-act="close"><span class="ico">' + st.emoji + '</span><span class="label">' + st.text + '</span><span class="clear" data-act="do:clear-status" data-tip="Clear status">' + I.xMark + '</span></button>'
          : '<button type="button" class="mi status" data-act="do:status"><span class="ico">' + I.emojiFace + '</span><span class="label">Update your status</span></button>') +
        '<hr>' +
        mi('none', I.workspace, 'Workspace') + mi('none', I.notes, 'Notes') + mi('none', I.code, 'Playground') +
        '<hr>' +
        mi('none', I.user, 'Admin Panel') + mi('none', I.cog, 'Settings') + mi('none', I.logOut, 'Sign Out');
    },
    chatsMore: function (el) {
      el.className = 'tdp-menu chats-more app-dropdown-menu rounded-xl border border-gray-100 dark:border-gray-800 bg-white dark:bg-gray-850 dark:text-white shadow-lg';
      el.innerHTML = mi('read-all', I.checkAlt, 'Mark all as read');
    },
    emoji: function (el) {
      el.className = 'tdp-menu emoji-menu rounded-xl border border-gray-100 dark:border-gray-800 bg-white dark:bg-gray-850 shadow-lg';
      el.innerHTML = ['\ud83d\udc4d', '\u2764\ufe0f', '\ud83d\ude02', '\ud83c\udf89', '\ud83d\udc40', '\ud83c\udfa8'].map(function (em) {
        return '<span class="em" data-act="do:emoji" data-emoji="' + em + '">' + em + '</span>';
      }).join('');
    }
  };
  window.__tdpMenus = { show: show, close: close, item: mi };

  // ---- Clicks inside a menu ----

  function onMenuHover(e) {
    var item = e.target.closest('.mi');
    if (item && open.ctx.subs) {
      if (item.dataset.sub) openSub(item, open.ctx.subs[item.dataset.sub]());
      else closeSub();
    }
    var r = e.target.closest('.tdp-list .tdp-row.item, .model-item');
    if (!r) return;
    open.el.querySelectorAll('.hl').forEach(function (x) { if (x !== r) x.classList.remove('hl'); });
    r.classList.add('hl');
  }

  function onMenuClick(e) {
    var model = e.target.closest('.model-item');
    if (model) { pickModel(Number(model.dataset.model)); return; }
    var a = e.target.closest('[data-act]');
    if (!a) return;
    var act = a.dataset.act;
    if (act === 'close') { close(); return; }
    if (act.indexOf('do:') === 0) {
      var ctx = open.ctx;
      if (a.dataset.sub) { openSub(a, ctx.subs[a.dataset.sub]()); return; }
      var keep = ctx.onAct && ctx.onAct(act.slice(3), a);
      if (!keep) close();
      return;
    }
    if (act === 'expand') {
      e.stopPropagation();
      var files = a.closest('.tdp-row').nextElementSibling;
      files.hidden = !files.hidden;
      a.innerHTML = files.hidden ? I.chevronRightAlt : I.chevronDown;
      place();
      return;
    }
    if (act === 'compare') { state.compare = !state.compare; a.classList.toggle('on', state.compare); return; }
    if (act === 'back') {
      if (open.kind === 'more') renderMore('root', 'from-left'); else renderIntegrations('root', 'from-left');
      return;
    }
    if (act.indexOf('tab:') === 0) {
      var view = act.slice(4);
      if (open.kind === 'more') renderMore(view, 'from-right'); else renderIntegrations(view, 'from-right');
      var input = open.el.querySelector('input');
      if (input) input.focus();
      return;
    }
    if (act.indexOf('toggle:') === 0) {
      var key = act.slice(7);
      state[key] = !state[key];
      a.setAttribute('aria-pressed', state[key]);
      a.querySelector('.tdp-switch').classList.toggle('on', state[key]);
      renderPills();
      return;
    }
    if (act.indexOf('tool:') === 0) {
      var i = Number(act.slice(5));
      state.tools[i] = !state.tools[i];
      a.setAttribute('aria-pressed', state.tools[i]);
      a.querySelector('.tdp-switch').classList.toggle('on', state.tools[i]);
      renderPills();
    }
  }
})();

// The sidebar's moving parts: sections, Pinned and folders fold
// (Sidebar/Section.svelte, common/Folder.svelte, RecursiveFolder.svelte);
// each chat's More menu (Sidebar/ChatMenu.svelte) renames, pins, clones,
// moves, archives and deletes rows in the mock; the user menu
// (Sidebar/UserMenu.svelte); Temporary Chat (Navbar.svelte) and the Chat
// Controls pane (ChatControls.svelte, Controls/Controls.svelte).
(function () {
  var M = window.__tdpMenus;
  var side = document.querySelector('.sidebar-panel .side-scroll');
  if (!M || !side) return;
  var I = ${JSON.stringify({ chevronDownSm: SVG.chevronDownSm, chevronRightSm: SVG.chevronRightSm, chevronUpSm: SVG.chevronUpSm, folder: SVG.folder, close: SVG.close, plusAlt: SVG.plusAlt })};

  function closest(e, sel) { return e.target && e.target.closest ? e.target.closest(sel) : null; }
  function esc(t) { return String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;'); }

  // ---- Folding ----

  function fold(box, head, on) {
    box.classList.toggle('open', on);
    head.setAttribute('aria-expanded', on);
  }

  side.addEventListener('click', function (e) {
    var btn = closest(e, '.chat-menu-btn');
    if (btn) { chatMenu(btn); return; }
    var add = closest(e, '.side-label .add');
    if (add) {
      if (add.classList.contains('sec-more')) {
        M.show('chatsMore', add, null, { onAct: function (act) { if (act === 'read-all') markAllRead(); } });
      }
      return;
    }
    var head = closest(e, '[data-section-toggle]');
    if (head) { fold(head.parentElement, head, !head.parentElement.classList.contains('open')); return; }
    var pin = closest(e, '.side-pinned');
    if (pin) {
      var on = !pin.parentElement.classList.contains('open');
      fold(pin.parentElement, pin, on);
      pin.querySelector('.pin-chev').innerHTML = on ? I.chevronDownSm : I.chevronRightSm;
      return;
    }
    var folder = closest(e, '.folder-row');
    if (folder) fold(folder.parentElement, folder, !folder.parentElement.classList.contains('open'));
  });

  // ---- A chat's More menu ----

  var FOLDERS = function () { return [].map.call(side.querySelectorAll('.side-folder'), function (f) { return f.dataset.folder; }); };
  var pinnedWrap = side.querySelector('.side-pinned-wrap');
  var pinnedList = side.querySelector('.pinned-list');

  function chatMenu(btn) {
    var row = btn.closest('.side-item');
    row.classList.add('menu-open');
    M.show('chat', btn, null, {
      pinned: function () { return !!row.closest('.pinned-list'); },
      subs: {
        download: function () { return M.item('close-only', '', 'Export chat (.json)') + M.item('close-only', '', 'Plain text (.txt)') + M.item('close-only', '', 'PDF document (.pdf)'); },
        move: function () {
          return FOLDERS().map(function (n) {
            return '<button type="button" class="mi" data-act="do:move-to" data-folder="' + esc(n) + '">' + I.folder + '<span class="label">' + esc(n) + '</span></button>';
          }).join('');
        }
      },
      onAct: function (act, el) { chatAct(row, act, el); },
      onClose: function () { row.classList.remove('menu-open'); }
    });
  }

  function todayAnchor() {
    var labels = side.querySelectorAll('.time-label');
    for (var i = 0; i < labels.length; i++) if (labels[i].textContent === 'Today') return labels[i];
    return labels[0];
  }
  function syncPinned() { pinnedWrap.hidden = !pinnedList.children.length; }

  function chatAct(row, act, el) {
    if (act === 'rename') rename(row);
    else if (act === 'unread') {
      if (!row.querySelector('.unread-dot')) row.insertAdjacentHTML('afterbegin', '<span class="unread-dot"></span>');
      row.classList.add('unread');
    } else if (act === 'pin') {
      if (row.closest('.pinned-list')) todayAnchor().after(row);
      else { pinnedList.prepend(row); fold(pinnedWrap, pinnedWrap.querySelector('.side-pinned'), true); pinnedWrap.querySelector('.pin-chev').innerHTML = I.chevronDownSm; }
      syncPinned();
    } else if (act === 'clone') {
      var copy = row.cloneNode(true);
      copy.classList.remove('active', 'unread', 'menu-open');
      ['.age', '.unread-dot'].forEach(function (s) { var x = copy.querySelector(s); if (x) x.remove(); });
      copy.querySelector('.label').textContent = 'Clone of ' + row.querySelector('.label').textContent;
      todayAnchor().after(copy);
    } else if (act === 'move-to') {
      var f = side.querySelector('.side-folder[data-folder="' + el.dataset.folder + '"]');
      if (!f) return;
      f.querySelector('.folder-children').appendChild(row);
      fold(f, f.querySelector('.folder-row'), true);
      var sec = f.closest('.side-section');
      fold(sec, sec.querySelector('[data-section-toggle]'), true);
      syncPinned();
    } else if (act === 'archive' || act === 'delete') {
      if (row.classList.contains('active') && window.__tdpOpenChat) window.__tdpOpenChat('new');
      row.remove();
      syncPinned();
    }
  }

  // ChatItem.svelte: the title becomes an input in place; Enter or leaving
  // it saves, Escape cancels
  function rename(row) {
    var label = row.querySelector('.label');
    var old = label.textContent;
    var input = document.createElement('input');
    input.className = 'rename';
    input.value = old;
    label.hidden = true;
    label.after(input);
    row.classList.add('menu-open');
    input.focus();
    input.select();
    var done = false;
    function finish(save) {
      if (done) return;
      done = true;
      var v = input.value.trim();
      if (save && v) {
        label.textContent = v;
        if (row.classList.contains('active')) document.getElementById('nav-title').textContent = v;
      }
      input.remove();
      label.hidden = false;
      row.classList.remove('menu-open');
    }
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); finish(true); }
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); finish(false); }
    });
    input.addEventListener('blur', function () { finish(true); });
  }

  function markAllRead() {
    side.querySelectorAll('.side-item.unread').forEach(function (r) {
      r.classList.remove('unread');
      var d = r.querySelector('.unread-dot');
      if (d) d.remove();
    });
  }

  // ---- The user menu: w-[calc(var(--sidebar-width)-1rem)] from the panel,
  // w-[15rem] from the rail ----

  var ST = window.__tdpState;
  var status = ST.saved.status || null;
  ST.add('status', function () { return status; });
  function userMenu(trigger, width) {
    M.show('user', trigger, null, {
      width: width,
      status: function () { return status; },
      onAct: function (act) {
        if (act === 'status') status = { emoji: '🎨', text: 'Designing a theme' };
        if (act === 'clear-status') status = null;
        ST.save();
      }
    });
  }
  var sideUser = document.getElementById('side-user');
  var railUser = document.getElementById('rail-user');
  if (sideUser) sideUser.addEventListener('click', function () { userMenu(sideUser, sideUser.parentElement.offsetWidth - 16); });
  if (railUser) railUser.addEventListener('click', function (e) { e.stopPropagation(); userMenu(railUser, 240); });

  // ---- Temporary Chat: only the icon changes on the button; the
  // placeholder gains its label and the message bar a dashed border ----

  var tempBtn = document.getElementById('temporary-chat-button');
  function setTemp(on) {
    document.body.classList.toggle('temp-chat', on);
    if (!tempBtn) return;
    tempBtn.classList.toggle('active', on);
    tempBtn.setAttribute('aria-pressed', on);
  }
  window.__tdpTemp = setTemp;
  if (tempBtn) tempBtn.addEventListener('click', function () { setTemp(!document.body.classList.contains('temp-chat')); ST.save(); });
  if (ST.saved.temp) setTemp(true);
  ST.add('temp', function () { return document.body.classList.contains('temp-chat'); });

  // ---- Chat Controls ----

  var pane = document.getElementById('controls-pane');
  var thread = document.getElementById('thread-pane');
  var ctrlBtn = document.getElementById('chat-controls-button');
  window.__tdpPanes = {
    closeAll: function (all) {
      if (thread) thread.hidden = true;
      if (all && pane) pane.hidden = true;
    }
  };

  // AdvancedParams.svelte for an admin, in order: cycling rows step through
  // their values, the rest turn from Default to a slider, a box or a switch
  var PARAMS = [
    ['Stream Chat Response', 'cycle', ['On', 'Off']],
    ['Stream Delta Chunk Size', 'range', [1, 128, 1, 1]],
    ['Context Compaction Threshold', 'text', ['Enter token threshold', '80000']],
    ['Function Calling', 'cycle', ['Native', 'Legacy']],
    ['Reasoning Tags', 'cycle', ['Custom', 'Enabled', 'Disabled']],
    ['Seed', 'text', ['Enter Seed', '0']],
    ['Stop Sequence', 'text', ['Enter stop sequence', '']],
    ['Temperature', 'range', [0, 2, 0.05, 0.8]],
    ['Reasoning Effort', 'text', ['Enter reasoning effort', 'medium']],
    ['logit_bias', 'text', ['Enter comma-separated "token:bias_value" pairs (example: 5432:100, 413:-100)', '']],
    ['max_tokens', 'range', [-2, 131072, 1, 128]],
    ['top_k', 'range', [0, 1000, 1, 40]],
    ['top_p', 'range', [0, 1, 0.05, 0.9]],
    ['min_p', 'range', [0, 1, 0.05, 0]],
    ['frequency_penalty', 'range', [-2, 2, 0.05, 1.1]],
    ['presence_penalty', 'range', [-2, 2, 0.05, 0]],
    ['mirostat', 'range', [0, 2, 1, 0]],
    ['mirostat_eta', 'range', [0, 1, 0.05, 0.1]],
    ['mirostat_tau', 'range', [0, 10, 0.5, 5]],
    ['repeat_last_n', 'range', [-1, 128, 1, 64]],
    ['tfs_z', 'range', [0, 2, 0.05, 1]],
    ['repeat_penalty', 'range', [-2, 2, 0.05, 1.1]],
    ['use_mmap', 'switch', []],
    ['use_mlock', 'switch', []],
    ['think (Ollama)', 'cycle', ['On', 'Custom', 'Off']],
    ['format (Ollama)', 'cycle', ['JSON']],
    ['num_keep (Ollama)', 'range', [-1, 10240000, 1, 24]],
    ['num_ctx (Ollama)', 'range', [-1, 10240000, 1, 2048]],
    ['num_batch (Ollama)', 'range', [256, 8192, 256, 512]],
    ['num_thread (Ollama)', 'range', [1, 256, 1, 2]],
    ['num_gpu (Ollama)', 'range', [0, 256, 1, 0]],
    ['keep_alive (Ollama)', 'text', ["e.g. '30s','10m'. Valid time units are 's', 'm', 'h'.", '5m']]
  ];

  function section(key, title, open, body) {
    return '<div class="cc-sec' + (open ? ' open' : '') + '" data-sec="' + key + '"><button type="button" class="cc-head" aria-expanded="' + open + '"><span>' + title + '</span><span class="down">' + I.chevronDownSm + '</span><span class="up">' + I.chevronUpSm + '</span></button><div class="cc-content">' + body + '</div></div>';
  }

  function buildControls() {
    var rows = PARAMS.map(function (p, i) {
      return '<div class="ap-row" data-p="' + i + '"><div class="ap-line"><div class="ap-label">' + esc(p[0]) + '</div><span class="ap-btn" role="button">Default</span></div><div class="ap-extra"></div></div>';
    }).join('');
    pane.innerHTML =
      '<div class="cc-tabs"><div class="cc-tab">Controls</div><span class="pane-close" role="button" aria-label="Close">' + I.close + '</span></div>' +
      '<div class="cc-body">' +
      section('valves', 'Valves', false, '<div class="cc-valves"><select><option>Tools</option><option>Functions</option></select><select><option disabled selected>Select a tool</option><option>Web Scraper</option><option>Calculator</option><option>Weather</option></select></div>') +
      section('system', 'System Prompt', true, '<textarea rows="3" placeholder="Enter system prompt"></textarea>') +
      section('params', 'Advanced Params', true, '<div class="ap">' + rows + '<div class="ap-add" role="button">' + I.plusAlt + '<span>Add Custom Parameter</span></div></div>') +
      '</div>';
  }

  function paramClick(row) {
    var p = PARAMS[Number(row.dataset.p)];
    var btn = row.querySelector('.ap-btn');
    var extra = row.querySelector('.ap-extra');
    if (p[1] === 'cycle') {
      var steps = ['Default'].concat(p[2]);
      btn.textContent = steps[(steps.indexOf(btn.textContent) + 1) % steps.length];
      return;
    }
    var on = btn.textContent === 'Default';
    btn.textContent = on ? 'Custom' : 'Default';
    if (!on) { extra.innerHTML = ''; return; }
    var a = p[2];
    if (p[1] === 'range') {
      extra.innerHTML = '<div class="ap-custom"><input type="range" min="' + a[0] + '" max="' + a[1] + '" step="' + a[2] + '" value="' + a[3] + '"><input type="number" min="' + a[0] + '" max="' + a[1] + '" step="any" value="' + a[3] + '"></div>';
    } else if (p[1] === 'text') {
      extra.innerHTML = '<div class="ap-custom"><input type="text" value="' + esc(a[1]) + '" placeholder="' + esc(a[0]) + '"></div>';
    } else {
      extra.innerHTML = '<div class="ap-custom ap-switch"><div class="ap-label">Enabled</div><span class="tdp-switch on" role="switch" aria-checked="true"></span></div>';
    }
  }

  // The pane is saved as markup, with what was typed, picked or slid
  // written back into it
  function controlsHtml() {
    var copy = pane.cloneNode(true);
    var live = pane.querySelectorAll('input, textarea, select');
    copy.querySelectorAll('input, textarea, select').forEach(function (el, i) {
      var v = live[i];
      if (el.tagName === 'TEXTAREA') el.textContent = v.value;
      else if (el.tagName === 'SELECT') [].forEach.call(el.options, function (o, j) { if (j === v.selectedIndex) o.setAttribute('selected', ''); else o.removeAttribute('selected'); });
      else el.setAttribute('value', v.value);
    });
    return copy.innerHTML;
  }

  if (pane && ctrlBtn) {
    if (ST.saved.controls && ST.saved.controls.html) pane.innerHTML = ST.saved.controls.html;
    else buildControls();
    if (ST.saved.controls && ST.saved.controls.open) pane.hidden = false;
    ST.add('controls', function () { return { open: !pane.hidden, html: controlsHtml() }; });
    new MutationObserver(ST.save).observe(pane, { subtree: true, childList: true, attributes: true, characterData: true });
    pane.addEventListener('change', ST.save);
    pane.addEventListener('input', function (e) {
      var t = e.target, other = null;
      if (t.type === 'range') other = t.nextElementSibling;
      else if (t.type === 'number') other = t.previousElementSibling;
      if (other) other.value = t.value;
      ST.save();
    });
    ctrlBtn.addEventListener('click', function () {
      pane.hidden = !pane.hidden;
      if (thread) thread.hidden = true;
    });
    pane.addEventListener('click', function (e) {
      if (closest(e, '.pane-close')) { pane.hidden = true; return; }
      var head = closest(e, '.cc-head');
      if (head) { var sec = head.parentElement; var on = !sec.classList.contains('open'); sec.classList.toggle('open', on); head.setAttribute('aria-expanded', on); return; }
      var b = closest(e, '.ap-btn');
      if (b) { paramClick(b.closest('.ap-row')); return; }
      var sw = closest(e, '.ap-switch .tdp-switch');
      if (sw) {
        var on2 = !sw.classList.contains('on');
        sw.classList.toggle('on', on2);
        sw.setAttribute('aria-checked', on2);
        sw.previousElementSibling.textContent = on2 ? 'Enabled' : 'Disabled';
      }
    });
  }
})();

// Channels (channel/Channel.svelte, Navbar.svelte, Messages.svelte,
// Messages/Message.svelte, MessageInput.svelte) and threads (Thread.svelte)
// as a mock that works: messages group by author, the hover toolbar reacts,
// replies, pins, opens a thread, edits and deletes, and you can type into
// the channel and the thread. Times read like dayjs in English: "2:14 PM"
// for today, "14:14" on a grouped row.
(function () {
  var view = document.getElementById('channel-view');
  var threadPane = document.getElementById('thread-pane');
  var M = window.__tdpMenus;
  if (!view || !threadPane || !M) return;
  var I = ${JSON.stringify({ faceSmile: SVG.faceSmile, arrowUpLeftAlt: SVG.arrowUpLeftAlt, pin: SVG.pin, pinSlash: SVG.pinSlash, chatBubbleEllipsis: SVG.chatBubbleEllipsis, pencil: SVG.pencil, garbageBin: SVG.garbageBin, chevronRightBold: SVG.chevronRightBold, xMark: SVG.xMark, plusSolid: SVG.plusSolid, micSolid: SVG.micSolid, arrowUpSolid: SVG.arrowUpSolid, lock: SVG.lock, hashtag: SVG.hashtag, user: SVG.user })};
  var LOGO_SRC = ${JSON.stringify(LOGO)};

  var USERS = {
    maya: { name: 'Maya', bg: 'linear-gradient(135deg, #f59e0b, #ef4444)' },
    jonas: { name: 'Jonas', bg: 'linear-gradient(135deg, #10b981, #3b82f6)' },
    you: { name: 'You', bg: 'linear-gradient(135deg, #6366f1, #ec4899)' },
    model: { name: 'Preview Model', model: true }
  };
  var DAY = 'Sunday, October 11, 2026 ';
  var nextId = 100;
  function m(u, t, hm, text, more) {
    var o = { id: nextId++, u: u, t: t, hm: hm, text: text, reacts: [], thread: [] };
    for (var k in more) o[k] = more[k];
    return o;
  }
  var CHANNELS = {
    general: {
      members: 4, created: 'October 1, 2026',
      msgs: [
        m('maya', '2:14 PM', '14:14', 'Morning! The new <b>Koi Pond</b> theme just landed in the gallery 🎏', { reacts: [{ e: '👍', n: 3, mine: true }, { e: '🎨', n: 2 }] }),
        m('maya', '2:15 PM', '14:15', 'Light mode keeps the pond-water gradient; Dark and OLED run the canvas.'),
        m('jonas', '2:20 PM', '14:20', 'Looks great. Does the canvas follow the theme hue now?', {
          thread: [
            m('maya', '2:22 PM', '14:22', 'It does. Twelve of the gallery animations pick up the accent now.'),
            m('you', '2:24 PM', '14:24', 'And they recolor live while you drag the hue slider.'),
            m('maya', '2:40 PM', '14:40', 'Even OLED gets its own palette ✨')
          ], last: '5 minutes ago'
        }),
        m('you', '2:31 PM', '14:31', '<span class="mention">@Preview Model</span> suggest an accent for a teal palette?'),
        m('model', '2:31 PM', '14:31', 'Try a warm coral around <code>oklch(0.72 0.14 30)</code>: it sits across the wheel from teal and stays readable in every mode.'),
        m('jonas', '2:38 PM', '14:38', 'Pinning the release checklist: palette, OLED pass, light-mode contrast, submit.', { pinned: true })
      ],
      typing: { u: 'maya', t: '2:45 PM', hm: '14:45', text: 'Shipping it after lunch 🚀' }
    },
    design: {
      members: 6, created: 'September 12, 2026',
      msgs: [
        m('jonas', '11:02 AM', '11:02', 'Sidebar transparency or opaque for the new dashboard theme?'),
        m('maya', '11:05 AM', '11:05', 'Translucent. The frosted glass reads well over the gradient.', { reacts: [{ e: '💯', n: 2 }] })
      ]
    },
    'theme-lab': {
      members: 2, private: true, created: 'October 8, 2026',
      msgs: [
        m('you', '9:12 AM', '09:12', 'Private sandbox for experiments before they hit the gallery.'),
        m('maya', '9:20 AM', '09:20', 'Testing a context-aware sunflower that grows with the chat 🌻')
      ]
    }
  };

  var current = null, threadOf = null, replyTo = null, typingTimers = [], typedOnce = {};
  var ST = window.__tdpState, was = ST.saved.channels;
  if (was && was.data) { CHANNELS = was.data; nextId = was.nextId; typedOnce = was.typed || {}; }
  ST.add('channels', function () {
    return { data: CHANNELS, nextId: nextId, typed: typedOnce, current: current, thread: threadOf ? threadOf.id : null, replyTo: replyTo };
  });
  new MutationObserver(ST.save).observe(view, { childList: true, subtree: true });
  new MutationObserver(ST.save).observe(threadPane, { childList: true, subtree: true });

  function esc(t) { return String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
  function now() {
    var d = new Date(), h = d.getHours(), mm = String(d.getMinutes()).padStart(2, '0');
    return { t: ((h % 12) || 12) + ':' + mm + (h < 12 ? ' AM' : ' PM'), hm: String(h).padStart(2, '0') + ':' + mm };
  }
  function avatar(u) {
    var user = USERS[u];
    if (user.model) return '<div class="av"><img src="' + LOGO_SRC + '" alt=""></div>';
    return '<div class="av" style="background:' + user.bg + '">' + user.name.charAt(0) + '</div>';
  }
  function find(list, id) {
    for (var i = 0; i < list.length; i++) if (list[i].id === id) return list[i];
    return null;
  }
  function msgById(id) {
    var ch = CHANNELS[current];
    var hit = find(ch.msgs, id);
    if (hit) return { msg: hit, list: ch.msgs };
    for (var i = 0; i < ch.msgs.length; i++) {
      var t = find(ch.msgs[i].thread, id);
      if (t) return { msg: t, list: ch.msgs[i].thread, parent: ch.msgs[i] };
    }
    return null;
  }

  // ---- Rendering ----

  function messageHtml(msg, prev, inThread) {
    var head = !prev || prev.u !== msg.u || msg.replyTo;
    var user = USERS[msg.u];
    var tools = '<div class="cm-tools">' +
      '<span data-do="react" data-tip="Add Reaction">' + I.faceSmile + '</span>' +
      '<span data-do="reply" data-tip="Reply" aria-label="Reply">' + I.arrowUpLeftAlt + '</span>' +
      '<span data-do="pin" data-tip="' + (msg.pinned ? 'Unpin' : 'Pin') + '">' + (msg.pinned ? I.pinSlash : I.pin) + '</span>' +
      (inThread ? '' : '<span data-do="thread" data-tip="Reply in Thread" aria-label="Reply in Thread">' + I.chatBubbleEllipsis + '</span>') +
      '<span data-do="edit" data-tip="Edit">' + I.pencil + '</span>' +
      '<span data-do="delete" data-tip="Delete">' + I.garbageBin + '</span></div>';
    var pin = msg.pinned ? '<div class="cm-pin-label">' + I.pin + '<span>Pinned</span></div>' : '';
    var quote = '';
    if (msg.replyTo) {
      var q = msgById(msg.replyTo);
      if (q) quote = '<div class="cm-quote"><div class="q">' + avatar(q.msg.u) + '<span class="who">' + USERS[q.msg.u].name + '</span><span class="what">' + q.msg.text.replace(/<[^>]+>/g, '') + '</span></div></div>';
    }
    var reacts = '';
    if (msg.reacts.length) {
      reacts = '<div class="cm-reacts">' + msg.reacts.map(function (r, i) {
        return '<span class="cm-react' + (r.mine ? ' mine' : '') + '" data-react="' + i + '" data-tip="' + (r.mine ? (r.n > 1 ? 'You and ' + (r.n - 1) + ' others' : 'You') : r.n + ' people') + ' reacted with ' + r.e + '"><span>' + r.e + '</span><span class="n">' + r.n + '</span></span>';
      }).join('') + '<span class="cm-react-add" data-do="react" data-tip="Add Reaction">' + I.faceSmile + '</span></div>';
    }
    var replies = '';
    if (!inThread && msg.thread.length) {
      replies = '<div class="cm-replies"><span class="btn" data-do="thread"><b>' + msg.thread.length + ' Replies</b><span> - Last reply ' + (msg.last || 'a few seconds ago') + '</span>' + I.chevronRightBold + '</span></div>';
    }
    var body = msg.editing
      ? '<div class="cm-edit"><textarea rows="2">' + esc(msg.text.replace(/<[^>]+>/g, '')) + '</textarea><div class="btns"><span class="cancel" data-do="edit-cancel">Cancel</span><span class="save" data-do="edit-save">Save</span></div></div>'
      : '<div class="cm-text">' + msg.text + (msg.edited ? '<span class="edited">(edited)</span>' : '') + '</div>';
    return '<div class="cm' + (head ? ' head' : '') + (msg.pinned ? ' pinned' : '') + (replyTo === msg.id ? ' replying' : '') + '" data-id="' + msg.id + '">' + tools + pin + quote +
      '<div class="cm-row"><div class="cm-av">' + (head ? avatar(msg.u) : '<div class="t2" data-tip="' + DAY + msg.t + '">' + msg.hm + '</div>') + '</div>' +
      '<div class="cm-body">' + (head ? '<div class="cm-meta"><span class="cm-name">' + user.name + '</span><span class="cm-time" data-tip="' + DAY + msg.t + '">' + msg.t + '</span></div>' : '') +
      body + reacts + replies + '</div></div></div>';
  }

  function listHtml(list, inThread) {
    return list.map(function (msg, i) { return messageHtml(msg, list[i - 1], inThread); }).join('');
  }

  function inputHtml(placeholder) {
    return '<div class="ch-input-wrap"><div class="ch-typing"></div><div class="ch-input" id="message-input-container">' +
      '<div class="ch-reply-bar"><span class="who"></span><span class="x" data-do="reply-cancel" role="button" aria-label="Cancel">' + I.xMark + '</span></div>' +
      '<div class="ch-edit" contenteditable="true" data-placeholder="' + placeholder + '"></div>' +
      '<div class="ch-bar"><span class="ch-plus" id="input-menu-button" role="button" aria-label="More">' + I.plusSolid + '</span>' +
      '<div class="ch-right"><span class="ch-mic" id="voice-input-button" role="button" aria-label="Voice Input" data-tip="Record voice">' + I.micSolid + '</span>' +
      '<span class="ch-send" id="send-message-button" role="button" aria-label="Send message" data-tip="Send message">' + I.arrowUpSolid + '</span></div></div></div></div>';
  }

  function renderChannel(scroll) {
    var ch = CHANNELS[current];
    var box = view.querySelector('.ch-scroll');
    var keep = box && !scroll ? box.scrollTop : null;
    box.innerHTML = '<div class="ch-intro"><div class="t">' + current + '</div><div class="d">This channel was created on ' + ch.created + '. This is the very beginning of the ' + current + ' channel.</div></div>' +
      (ch.msgs.length ? '<hr class="ch-intro-rule">' : '') + listHtml(ch.msgs, false);
    box.scrollTop = keep === null ? box.scrollHeight : keep;
  }

  function renderThread(scroll) {
    if (!threadOf) return;
    var box = threadPane.querySelector('.ch-scroll');
    var keep = !scroll ? box.scrollTop : null;
    box.innerHTML = listHtml([threadOf].concat(threadOf.thread), true);
    box.scrollTop = keep === null ? box.scrollHeight : keep;
  }

  function rerender() { renderChannel(false); renderThread(false); }

  // ---- Opening ----

  function open(name) {
    if (!CHANNELS[name]) return;
    close();
    current = name;
    var ch = CHANNELS[name];
    view.hidden = false;
    view.innerHTML = '<div class="ch-nav"><div class="ch-title">' + (ch.private ? I.lock : I.hashtag) + '<span>' + name + '</span></div>' +
      '<div class="ch-nav-btns"><span class="ch-nav-btn" role="button" aria-label="Pinned Messages" data-tip="Pinned Messages">' + I.pin + '</span>' +
      '<span class="ch-nav-btn" role="button" aria-label="User Count" data-tip="Users">' + I.user + '<span class="n">' + ch.members + '</span></span></div></div>' +
      '<div class="ch-scroll"></div>' + inputHtml('Type here...');
    renderChannel(true);
    document.getElementById('nav-title').textContent = name;
    if (ch.typing && !typedOnce[name]) simulateTyping(name);
  }

  function close() {
    typingTimers.forEach(clearTimeout);
    typingTimers = [];
    closeThread();
    replyTo = null;
    current = null;
    view.hidden = true;
    view.innerHTML = '';
  }

  // Someone else in the channel: the typing line, then their message
  function simulateTyping(name) {
    var ch = CHANNELS[name], ty = ch.typing;
    var line = view.querySelector('.ch-typing');
    typingTimers.push(setTimeout(function () {
      if (current !== name) return;
      line.innerHTML = '<span class="dots"><i></i><i></i><i></i></span><span><b>' + USERS[ty.u].name + '</b> is typing...</span>';
      line.classList.add('show');
    }, 1400));
    typingTimers.push(setTimeout(function () {
      if (current !== name) return;
      line.classList.remove('show');
      typedOnce[name] = true;
      ch.msgs.push(m(ty.u, ty.t, ty.hm, ty.text));
      renderChannel(true);
    }, 4200));
  }

  function openThread(msg) {
    threadOf = msg;
    var pane = document.getElementById('controls-pane');
    if (pane) pane.hidden = true;
    threadPane.hidden = false;
    threadPane.innerHTML = '<div class="th-head"><div class="t">Thread</div><span class="pane-close" role="button" aria-label="Close">' + I.xMark + '</span></div>' +
      '<div class="ch-scroll"></div>' + inputHtml('Reply to thread...');
    renderThread(true);
  }
  function closeThread() {
    threadOf = null;
    threadPane.hidden = true;
    threadPane.innerHTML = '';
  }

  // ---- Acting on messages ----

  function setReply(id) {
    replyTo = id;
    var bar = view.querySelector('.ch-input');
    var hit = id && msgById(id);
    bar.classList.toggle('replying', !!hit);
    if (hit) bar.querySelector('.who').textContent = 'Replying to ' + USERS[hit.msg.u].name;
    renderChannel(false);
    if (hit) bar.querySelector('.ch-edit').focus();
  }

  function addReaction(msg, e) {
    for (var i = 0; i < msg.reacts.length; i++) {
      if (msg.reacts[i].e === e) {
        if (!msg.reacts[i].mine) { msg.reacts[i].mine = true; msg.reacts[i].n++; }
        rerender();
        return;
      }
    }
    msg.reacts.push({ e: e, n: 1, mine: true });
    rerender();
  }

  function onClick(e) {
    var host = e.currentTarget;
    var el = e.target.closest('[data-do], [data-react], .pane-close, .ch-plus, .ch-send');
    if (!el) return;
    if (el.classList.contains('pane-close')) { closeThread(); return; }
    if (el.classList.contains('ch-plus')) { M.show('more', el); return; }
    if (el.classList.contains('ch-send')) { send(host); return; }
    var row = el.closest('.cm');
    var hit = row && msgById(Number(row.dataset.id));
    var act = el.dataset.do;
    if (act === 'reply-cancel') { setReply(null); return; }
    if (!hit) return;
    var msg = hit.msg;
    if (el.dataset.react) {
      var r = msg.reacts[Number(el.dataset.react)];
      r.mine = !r.mine;
      r.n += r.mine ? 1 : -1;
      if (!r.n) msg.reacts.splice(Number(el.dataset.react), 1);
      rerender();
      return;
    }
    if (act === 'react') {
      row.classList.add('tools-open');
      M.show('emoji', el, null, {
        onAct: function (a, item) { if (a === 'emoji') addReaction(msg, item.dataset.emoji); },
        onClose: function () { row.classList.remove('tools-open'); }
      });
    } else if (act === 'reply') {
      if (host === threadPane) { threadPane.querySelector('.ch-edit').focus(); return; }
      setReply(msg.id);
    } else if (act === 'pin') { msg.pinned = !msg.pinned; rerender(); }
    else if (act === 'thread') openThread(hit.parent || msg);
    else if (act === 'edit') { msg.editing = true; rerender(); var ta = host.querySelector('.cm[data-id="' + msg.id + '"] textarea'); if (ta) ta.focus(); }
    else if (act === 'edit-cancel') { msg.editing = false; rerender(); }
    else if (act === 'edit-save') {
      var v = row.querySelector('textarea').value.trim();
      msg.editing = false;
      if (v) { msg.text = esc(v); msg.edited = true; }
      rerender();
    } else if (act === 'delete') {
      hit.list.splice(hit.list.indexOf(msg), 1);
      if (threadOf === msg) closeThread();
      if (replyTo === msg.id) setReply(null);
      rerender();
    }
  }

  function send(host) {
    var edit = host.querySelector('.ch-edit');
    var text = edit.innerText.trim();
    if (!text) return;
    var t = now();
    var msg = m('you', t.t, t.hm, esc(text));
    if (host === threadPane) {
      threadOf.thread.push(msg);
      threadOf.last = 'a few seconds ago';
    } else {
      if (replyTo) msg.replyTo = replyTo;
      CHANNELS[current].msgs.push(msg);
      replyTo = null;
      host.querySelector('.ch-input').classList.remove('replying');
    }
    edit.innerHTML = '';
    host.querySelector('.ch-input').classList.remove('has-text');
    renderChannel(host === view);
    renderThread(true);
  }

  function onKey(e) {
    var edit = e.target.closest && e.target.closest('.ch-edit');
    if (!edit) return;
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(e.currentTarget); }
    if (e.key === 'Escape' && replyTo) { e.preventDefault(); e.stopPropagation(); setReply(null); }
  }
  function onInput(e) {
    var edit = e.target.closest && e.target.closest('.ch-edit');
    if (!edit) return;
    if (!edit.innerText.trim()) edit.innerHTML = '';
    edit.closest('.ch-input').classList.toggle('has-text', !!edit.innerText.trim());
  }

  [view, threadPane].forEach(function (host) {
    host.addEventListener('click', onClick);
    host.addEventListener('keydown', onKey);
    host.addEventListener('input', onInput);
  });

  window.__tdpChannel = { open: open, close: close };

  // After the first chat opens (openChat defers it the same way), bring back
  // the open thread and the reply in progress
  if (was && was.current) setTimeout(function () {
    if (current !== was.current) return;
    if (was.thread) { var t = msgById(was.thread); if (t) openThread(t.parent || t.msg); }
    if (was.replyTo && msgById(was.replyTo)) setReply(was.replyTo);
  }, 0);
})();

// The floating preview toolbar lives in the parent and can change height when
// it wraps, so ask for its real height instead of assuming one. Only the first
// message clears it — the navbar intentionally stays under the bar.
(function () {
  var root = document.documentElement;
  var inset = null;

  // Measure where the first row lands with no offset, then add exactly enough
  // to reach the inset. Derived rather than hardcoded so it survives changes
  // to the navbar's height and the container's padding.
  function apply() {
    if (inset === null) return;
    var row = document.querySelector('#messages-container > .message-listitem');
    if (!row) return; // the New Chat placeholder needs no offset
    root.style.setProperty('--tdp-first-row-offset', '0px');
    var natural = row.getBoundingClientRect().top;
    var needed = Math.max(0, Math.round(inset - natural));
    root.style.setProperty('--tdp-first-row-offset', needed + 'px');
  }

  window.__tdpApplyInset = apply;

  window.addEventListener('message', function (e) {
    var d = e.data;
    if (!d || d.__tdpPreview !== true) return;
    if (d.kind === 'topinset') {
      var px = Number(d.px);
      if (!isFinite(px) || px < 0) return;
      inset = px;
      apply();
    } else if (d.kind === 'textscale') {
      // Interface.svelte's UI Scale slider — setTextScale(scale) upstream
      var sc = Number(d.scale);
      if (!isFinite(sc) || sc < 1 || sc > 1.5) return;
      root.style.setProperty('--app-text-scale', String(sc));
      apply(); // content height changed, so the first-row offset must too
    } else if (d.kind === 'vars' && d.vars && typeof d.vars === 'object') {
      // The home page's hue slider recolors the mock without rebuilding it
      Object.keys(d.vars).forEach(function (k) {
        var v = d.vars[k];
        if (/^--(color-gray-\\d+|tdp-accent(-2|-3)?)$/.test(k) && typeof v === 'string' && !/[;{}<]/.test(v)) root.style.setProperty(k, v);
        else if (/^--tdp-(hue|chroma)$/.test(k) && isFinite(Number(v))) root.style.setProperty(k, String(Number(v)));
      });
      window.dispatchEvent(new Event('tdp-palette'));
    }
  });

  window.addEventListener('resize', apply);
  try { parent.postMessage({ __tdpPreview: true, kind: 'ready' }, '*'); } catch (e) {}
})();
</script>
</body>
</html>`;
  }

  // ---- Export for the converter tool (verbatim from the repo) ----

  window.TDPEngine = {
    themeVars,
    gradientLayers,
    adjustHexIntensity,
    buildMockSrcdoc,
    modeVars(mode, modeKey) {
      const vars = themeVars(mode, modeKey);
      if (modeKey === 'oled') {
        vars['--color-gray-800'] = '#101010';
        vars['--color-gray-850'] = '#050505';
        vars['--color-gray-900'] = '#000000';
        vars['--color-gray-950'] = '#000000';
      }
      return vars;
    },
  };
})();
"""
LOGO_B64 = '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'
THEME_PAGE_HTML = '<!doctype html><html lang=en><head><meta charset=utf-8>\n<meta name=viewport content="width=device-width,initial-scale=1">\n<title>Skin-Smith</title>\n<style>\n:root{color-scheme:dark;--skin-bg:#121214;--skin-surface:#1b1b1f;--skin-raised:#212126;--skin-text:#e9e9ec;--skin-muted:#9b9ba3;--skin-faint:#6e6e76;--skin-accent:#2563eb;--skin-danger:#f87171;--skin-success:#4ade80;--skin-hairline:color-mix(in srgb,var(--skin-text) 12%,transparent);--skin-hairline-2:color-mix(in srgb,var(--skin-text) 20%,transparent);--skin-accent-soft:color-mix(in srgb,var(--skin-accent) 18%,transparent);--skin-accent-glow:color-mix(in srgb,var(--skin-accent) 26%,transparent);--skin-danger-soft:color-mix(in srgb,var(--skin-danger) 14%,transparent);--skin-glass:color-mix(in srgb,var(--skin-surface) 78%,transparent);--skin-well:color-mix(in srgb,black 28%,transparent);--r-card:14px;--r-ctrl:10px;--r-chip:999px;--sp2:8px;--sp3:12px;--sp4:16px;--shadow-1:0 1px 2px rgba(0,0,0,.45);--shadow-2:0 16px 40px rgba(0,0,0,.35);--ease:cubic-bezier(.2,.7,.3,1)}\nbody{font-family:system-ui,sans-serif;font-size:14px;margin:0;padding:0;background:var(--skin-bg);color:var(--skin-text)}\nbody::before{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;background:radial-gradient(900px 520px at 12% -12%,color-mix(in srgb,var(--skin-accent) 11%,transparent),transparent 62%),radial-gradient(760px 440px at 94% 6%,color-mix(in srgb,var(--skin-accent) 7%,transparent),transparent 58%),radial-gradient(1300px 900px at 50% 128%,color-mix(in srgb,white 4%,transparent),transparent 60%)}\n.shell{max-width:1180px;margin:0 auto;padding:0 20px 44px}\n.topbar{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:12px;margin:0 -20px;padding:14px 20px;background:color-mix(in srgb,var(--skin-bg) 84%,transparent);backdrop-filter:blur(12px) saturate(1.15);-webkit-backdrop-filter:blur(12px) saturate(1.15);border-bottom:1px solid var(--skin-hairline)}\n.topbar h1{font-size:15px;font-weight:650;letter-spacing:.02em;margin:0}\n#applied{font-size:13px;color:var(--skin-muted);padding:6px 12px;border:1px solid var(--skin-hairline);border-radius:var(--r-chip);background:color-mix(in srgb,var(--skin-surface) 60%,transparent)}\n#applied::before{content:"\\25CF";margin-right:7px;color:var(--skin-faint)}\n#applied[data-s=on]{color:#cfd6e6}\n#applied[data-s=on]::before{color:var(--skin-accent)}\n.note{color:var(--skin-muted);font-size:13.5px;line-height:1.5;margin:16px 0}\n.grid{display:grid;gap:16px}\n.card{position:relative;background:var(--skin-glass);border:1px solid var(--skin-hairline);border-radius:var(--r-card);box-shadow:var(--shadow-1),var(--shadow-2);backdrop-filter:blur(12px) saturate(1.15);-webkit-backdrop-filter:blur(12px) saturate(1.15)}\n.card::before{content:"";position:absolute;left:1px;right:1px;top:0;height:1px;border-radius:var(--r-card);background:linear-gradient(90deg,transparent,color-mix(in srgb,white 13%,transparent),transparent)}\n.cardhead{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:10px;padding:12px 16px;border-bottom:1px solid var(--skin-hairline)}\n.label{font-size:11px;text-transform:uppercase;letter-spacing:.12em;color:var(--skin-faint);font-weight:600}\n.cardbody{padding:16px}\nbutton{height:38px;padding:0 14px;border-radius:var(--r-ctrl);font:inherit;font-size:13.5px;border:1px solid var(--skin-hairline-2);background:transparent;color:var(--skin-text);cursor:pointer;transition:background .16s var(--ease),border-color .16s var(--ease),box-shadow .16s var(--ease),color .16s var(--ease)}\nbutton:hover{background:color-mix(in srgb,white 6%,transparent);border-color:color-mix(in srgb,white 26%,transparent)}\nbutton:focus-visible{outline:2px solid var(--skin-accent);outline-offset:2px}\nbutton:disabled{opacity:.4;cursor:not-allowed}\n.ghost{color:var(--skin-muted)}\n.ghost:hover{color:var(--skin-text)}\n.danger{color:var(--skin-danger);border-color:color-mix(in srgb,var(--skin-danger) 34%,transparent)}\n.danger:hover{background:var(--skin-danger-soft);border-color:color-mix(in srgb,var(--skin-danger) 55%,transparent)}\n#apply{background:var(--skin-accent);border-color:color-mix(in srgb,var(--skin-accent) 60%,white 40%);color:white;font-weight:600;box-shadow:var(--shadow-1),0 8px 22px var(--skin-accent-glow)}\n#apply:hover{background:color-mix(in srgb,var(--skin-accent) 86%,white 14%)}\n.cardhead .ghost{height:30px;font-size:12.5px;padding:0 11px}\nselect{appearance:none;-webkit-appearance:none;height:38px;padding:0 34px 0 12px;border-radius:var(--r-ctrl);border:1px solid var(--skin-hairline-2);background:transparent;color:var(--skin-text);font:inherit;font-size:13.5px;background-image:url("data:image/svg+xml,<svg xmlns=\'http://www.w3.org/2000/svg\' width=\'12\' height=\'12\' viewBox=\'0 0 12 12\'><path d=\'M2.5 4.5 6 8l3.5-3.5\' fill=\'none\' stroke=\'%239b9ba3\' stroke-width=\'1.4\'/></svg>");background-repeat:no-repeat;background-position:right 10px center}\nselect:focus-visible{outline:2px solid var(--skin-accent);outline-offset:2px}\nselect option{background:var(--skin-raised);color:var(--skin-text)}\ninput.name{height:38px;padding:0 12px;border-radius:var(--r-ctrl);border:1px solid var(--skin-hairline-2);background:var(--skin-well);color:var(--skin-text);font:inherit;font-size:13.5px;min-width:14rem}\ninput.name::placeholder{color:var(--skin-faint)}\ninput.name:focus-visible{outline:2px solid var(--skin-accent);outline-offset:2px}\n.librow{display:flex;flex-wrap:wrap;align-items:center;gap:var(--sp2)}\n#slot{flex:1 1 16rem;min-width:12rem}\ntextarea{width:100%;min-height:240px;font-family:ui-monospace,monospace;font-size:13px;line-height:1.55;padding:14px;border-radius:var(--r-ctrl);border:1px solid var(--skin-hairline);background:var(--skin-well);color:var(--skin-text);caret-color:var(--skin-accent);box-shadow:inset 0 1px 3px rgba(0,0,0,.4);resize:vertical;transition:border-color .16s var(--ease)}\ntextarea:focus-visible{outline:2px solid var(--skin-accent);outline-offset:2px}\ntextarea::-webkit-scrollbar{width:10px}\ntextarea::-webkit-scrollbar-thumb{background:color-mix(in srgb,white 14%,transparent);border-radius:999px}\n#tabs{display:inline-flex;padding:3px;gap:2px;border-radius:var(--r-chip);border:1px solid var(--skin-hairline);background:var(--skin-well)}\n.tab{height:30px;padding:0 14px;border-radius:var(--r-chip);border:0;background:transparent;color:var(--skin-muted);font:inherit;font-size:13px;cursor:pointer;transition:background .16s var(--ease),color .16s var(--ease),box-shadow .16s var(--ease)}\n.tab:hover{color:var(--skin-text)}\n.tab.active{background:var(--skin-accent-soft);color:#cfd6e6;box-shadow:inset 0 1px 0 color-mix(in srgb,white 12%,transparent),0 4px 12px var(--skin-accent-glow)}\niframe{width:100%;height:420px;border:1px solid var(--skin-hairline);border-radius:var(--r-ctrl);background:white;box-shadow:var(--shadow-1)}\n#status{min-height:38px;display:flex;align-items:center;padding:0 12px;margin:12px 0;border-radius:var(--r-chip);border:1px solid var(--skin-hairline);background:color-mix(in srgb,var(--skin-accent) 10%,transparent);color:#cfd6e6;font-size:13px}\n#status:empty{display:none}\n#commit{display:flex;align-items:center;gap:var(--sp2);padding-top:14px;border-top:1px solid var(--skin-hairline)}\n#reset{margin-left:auto}\npre#cssbox{white-space:pre-wrap;background:var(--skin-well);border:1px solid var(--skin-hairline);border-radius:var(--r-ctrl);padding:14px;max-height:300px;overflow:auto;font-family:ui-monospace,monospace;font-size:12.5px;margin:12px 0;color:var(--skin-muted)}\n@media(min-width:1000px){.grid{grid-template-columns:minmax(0,45fr) minmax(0,55fr);grid-template-areas:"lib lib" "ed pv";column-gap:16px}#lib{grid-area:lib}#ed{grid-area:ed}#pv{grid-area:pv}textarea{min-height:460px}}\n@media(prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}\n@media(prefers-reduced-transparency:reduce){.card,.topbar,#applied,#status,.tab.active{backdrop-filter:none;-webkit-backdrop-filter:none;background:var(--skin-raised)}}\n</style></head><body>\n<div class=shell>\n<div class=topbar>\n<h1>Skin-Smith</h1>\n<div id=applied>loading library...</div>\n</div>\n<p class=note>Pick a saved theme from the library, preview it in every mode, then <b>Apply &amp; Save</b>. Applied as <code>/static/custom.css</code> for all users and persisted across restarts.</p>\n<div class=grid>\n<div id=lib class=card>\n<div class=cardhead><span class=label>Library</span></div>\n<div class=librow>\n<select id=slot aria-label="Theme library"></select>\n<input class=name id=slotname placeholder="Theme name" maxlength=40 spellcheck=false>\n<button class=ghost id=slotadd>Save as new</button>\n<button class=ghost id=slotupdate>Update slot</button>\n<button class=danger id=slotdelete>Delete slot</button>\n</div>\n</div>\n<div id=ed class=card>\n<div class=cardhead><span class=label>Source</span><span><button class=ghost id=sample>Load sample</button> <button class=ghost id=upload>Upload JSON</button></span></div>\n<div class=cardbody>\n<textarea id=json spellcheck=false placeholder="Paste a TDP theme JSON export here"></textarea>\n<input type=file id=file accept=".json,application/json" style="display:none">\n</div>\n</div>\n<div id=pv class=card>\n<div class=cardhead><span class=label>Preview</span><nav id=tabs>\n<button class="tab active" data-m=dark>Dark</button>\n<button class=tab data-m=light>Light</button>\n<button class=tab data-m=oled>OLED</button>\n<button class=tab data-m=her>Her</button></nav></div>\n<div class=cardbody>\n<iframe id=prev sandbox="allow-scripts" title="preview"></iframe>\n<div id=status></div>\n<div id=commit>\n<button id=apply>Apply &amp; Save</button>\n<button class=ghost id=cssbtn>View CSS</button>\n<button class=danger id=reset>Restore default</button>\n</div>\n<pre id=cssbox style="display:none"></pre>\n</div>\n</div>\n</div>\n</div>\n<script src="/skin/converter.js"></script>\n<script src="/skin/preview.js"></script>\n<script>(function(){var t=localStorage.getItem("token")||"";window.__skin_token=t;var block=function(){document.body.innerHTML="<h1>Skin-Smith</h1><p>Sign in as an admin to use this converter.</p>";};if(!t){block();return;}fetch("/api/v1/auths/",{headers:{Authorization:"Bearer "+t}}).then(r=>r.ok?r.json():null).then(u=>{if(!u||u.role!=="admin")block();}).catch(()=>block());})();</script>\n<script>\n(function(){if(!document.querySelector("#json"))return;\nconst $=(s)=>document.querySelector(s);\nlet mode="dark",slots=[],applied=null,live=false,cur="";\nconst SAMPLE="{\\n  \\"name\\": \\"Sample Indigo Drift\\",\\n  \\"description\\": \\"Made in the Theme Designer Pro Studio.\\",\\n  \\"version\\": \\"1.0.0\\",\\n  \\"dark\\": {\\n    \\"h\\": 250,\\n    \\"c\\": 22,\\n    \\"l\\": 18,\\n    \\"paletteEnabled\\": true,\\n    \\"accent\\": \\"\\",\\n    \\"accent2\\": \\"#38bdf8\\",\\n    \\"accent3\\": \\"\\",\\n    \\"manualOverrides\\": \\"--radius-lg: 10px;\\",\\n    \\"manualOverridesEnabled\\": true,\\n    \\"customCSS\\": \\"--color-primary-500: oklch(0.68 0.17 250);\\",\\n    \\"customCssEnabled\\": true,\\n    \\"gradientEnabled\\": true,\\n    \\"gradientType\\": \\"linear\\",\\n    \\"gradientAngle\\": 135,\\n    \\"gradientStops\\": [\\n      { \\"color\\": \\"#1e1b4b\\", \\"position\\": 0 },\\n      { \\"color\\": \\"#0c0a1d\\", \\"position\\": 55 },\\n      { \\"color\\": \\"#172a4a\\", \\"position\\": 100 }\\n    ],\\n    \\"gradientIntensity\\": 85,\\n    \\"gradientAnimation\\": true,\\n    \\"gradientAnimationSpeed\\": 12\\n  },\\n  \\"light\\": {\\n    \\"h\\": 250,\\n    \\"c\\": 14,\\n    \\"l\\": 97,\\n    \\"paletteEnabled\\": true,\\n    \\"accent\\": \\"\\",\\n    \\"accent2\\": \\"\\",\\n    \\"accent3\\": \\"\\",\\n    \\"manualOverrides\\": \\"\\",\\n    \\"manualOverridesEnabled\\": false,\\n    \\"customCSS\\": \\"\\",\\n    \\"customCssEnabled\\": false,\\n    \\"gradientEnabled\\": true,\\n    \\"gradientType\\": \\"radial\\",\\n    \\"gradientRadialShape\\": \\"ellipse\\",\\n    \\"gradientRadialSize\\": \\"farthest-corner\\",\\n    \\"gradientRadialPosX\\": 50,\\n    \\"gradientRadialPosY\\": 0,\\n    \\"gradientStops\\": [\\n      { \\"color\\": \\"#e0e7ff\\", \\"position\\": 0 },\\n      { \\"color\\": \\"#ffffff\\", \\"position\\": 70 }\\n    ],\\n    \\"gradientIntensity\\": 40,\\n    \\"gradientAnimation\\": false\\n  }\\n}\\n";\nconst H=()=>({Authorization:"Bearer "+(window.__skin_token||"")});\nconst esc=(s)=>String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/"/g,"&quot;");\nfunction getTheme(){try{return JSON.parse($("#json").value)}catch(e){$("#status").textContent="Invalid JSON: "+e.message;return null}}\nfunction preview(){const t=getTheme();if(!t)return;$("#prev").srcdoc=window.TDPEngine.buildMockSrcdoc(window.TDPConverter.previewFeed(t,mode))}\nfunction convert(){const t=getTheme();if(!t)return null;return window.TDPConverter.convert(t)}\nfunction post(u,b){return fetch(u,{method:"POST",headers:H(),body:new URLSearchParams(b)}).then(r=>r.text())}\nfunction sync(j){slots=j.slots||[];applied=j.applied||null;live=!!j.live;if(!slots.some(s=>s.name===cur))cur="";}\nfunction setApplied(t,on){$("#applied").textContent=t;$("#applied").dataset.s=on?"on":"off";}\nfunction render(){\n$("#slot").innerHTML=\'<option value="">— Manual edit —</option>\'+slots.map(s=>\'<option value="\'+esc(s.name)+\'">\'+esc(s.name)+(s.name===applied?" ●":"")+"</option>").join("");\n$("#slot").value=cur;\nsetApplied(applied?"applied: "+applied:(live?"applied: custom CSS (not in library)":"nothing applied"),!!applied||live);\nconst d=!slots.length;\n$("#slotupdate").disabled=d||!cur;\n$("#slotdelete").disabled=d||!cur;\n}\nfunction refresh(){return fetch("/skin/slots",{headers:H()}).then(r=>r.json()).then(j=>{sync(j);render()}).catch(()=>{setApplied("library unavailable",false)});}\nfunction slotWrite(op){\nconst name=$("#slotname").value.trim();\nif(!name){$("#status").textContent="Type a theme name first.";return}\nconst css=convert();if(!css){$("#status").textContent="Fix JSON first.";return}\n$("#status").textContent="Saving library...";\npost("/skin/slots/save",{op,name,json:$("#json").value,css}).then(t=>{\nlet j;try{j=JSON.parse(t)}catch(e){$("#status").textContent=t;return}\nif(!j.ok){$("#status").textContent=j.error||"Save failed.";return}\nsync(j);if(op!=="delete")cur=name;render();\n$("#status").textContent=(op==="delete"?"Deleted: "+name:"Saved: "+name)+(j.applied?" · applied: "+j.applied:"");\n});\n}\nlet armDel=false;\n$("#slotdelete").addEventListener("click",()=>{\nif(!armDel){armDel=true;$("#slotdelete").textContent="Confirm delete?";setTimeout(()=>{armDel=false;$("#slotdelete").textContent="Delete slot"},3000);return}\narmDel=false;$("#slotdelete").textContent="Delete slot";\nconst name=cur;\npost("/skin/slots/save",{op:"delete",name}).then(t=>{\nlet j;try{j=JSON.parse(t)}catch(e){$("#status").textContent=t;return}\nif(!j.ok){$("#status").textContent=j.error||"Delete failed.";return}\nsync(j);if(cur===name)cur="";render();$("#status").textContent="Deleted: "+name;\n});\n});\n$("#slot").addEventListener("change",()=>{cur=$("#slot").value;if(!cur)return;const s=slots.find(x=>x.name===cur);if(!s)return;$("#json").value=s.json;$("#slotname").value=cur;preview();render();});\n$("#slotadd").addEventListener("click",()=>slotWrite("add"));\n$("#slotupdate").addEventListener("click",()=>slotWrite("update"));\n$("#sample").addEventListener("click",()=>{$("#json").value=SAMPLE;cur="";preview();render();});\n$("#upload").addEventListener("click",()=>$("#file").click());\n$("#file").addEventListener("change",e=>{const f=e.target.files[0];if(f){const r=new FileReader();r.onload=()=>{$("#json").value=r.result;cur="";preview();render();};r.readAsText(f);}});\n$("#tabs").addEventListener("click",e=>{const b=e.target.closest("button[data-m]");if(!b)return;document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active"));b.classList.add("active");mode=b.dataset.m;preview();});\n$("#json").addEventListener("input",preview);\n$("#cssbtn").addEventListener("click",()=>{const box=$("#cssbox");const show=box.style.display==="none";if(show){const c=convert();if(c===null){$("#status").textContent="Fix JSON first.";return}box.textContent=c;}box.style.display=show?"block":"none";$("#cssbtn").textContent=show?"Hide CSS":"View CSS";});\n$("#apply").addEventListener("click",()=>{const css=convert();if(!css){$("#status").textContent="Fix JSON first.";return}$("#status").textContent="Saving...";fetch("/skin/save",{method:"POST",headers:H(),body:new URLSearchParams({css})}).then(r=>r.text()).then(t=>{$("#status").textContent=t;refresh();});});\n$("#reset").addEventListener("click",()=>{$("#status").textContent="Restoring default...";fetch("/skin/save",{method:"POST",headers:H(),body:new URLSearchParams({css:""})}).then(r=>r.text()).then(t=>{$("#status").textContent=t;refresh();});});\n$("#json").value=SAMPLE;preview();refresh();})();\n</script></body></html>'

def _active_css(app):
    # Paste override wins; falls back to the CSS persisted in the function meta.
    v = getattr(app.state, "_tdp_css", None)
    if v:
        return v
    return getattr(app.state, "_tdp_persisted", "")

SLOT_CAP = 12         # theme slots in the library
SLOT_NAME_MAX = 40    # slot display name length

def _meta_engine():
    # Open WebUI's own SQLAlchemy engine, without importing the models
    # package (its module layout differs between versions).
    eng = None
    for mod in ("open_webui.internal.db", "open_webui.internal.sqlalchemy"):
        try:
            eng = __import__(mod, fromlist=["SQLALCHEMY_ENGINE"]).SQLALCHEMY_ENGINE
            break
        except Exception:
            pass
    if eng is None:
        import sqlalchemy
        env = None
        try:
            env = __import__("open_webui.env", fromlist=["SQLALCHEMY_DATABASE_URL"])
        except Exception:
            pass
        # SQLALCHEMY_DATABASE_URL is the app's true DB (SQLite or Postgres);
        # SQLITE_URL alone could point at a file a Postgres app never reads.
        url = None
        if env is not None:
            for name in ("SQLALCHEMY_DATABASE_URL", "DATABASE_URL", "SQLITE_URL"):
                url = getattr(env, name, None)
                if url:
                    break
        if not url:
            return None
        try:
            eng = sqlalchemy.create_engine(url)
        except Exception:
            return None
    return eng

def _meta_read_key(fn_id, key, default):
    # One key from the function's meta column, via a direct DB read.
    import json
    from sqlalchemy import table, column, JSON, select
    eng = _meta_engine()
    if eng is None:
        return default
    try:
        t = table("function", column("id"), column("meta", JSON))
        with eng.connect() as conn:
            row = conn.execute(select(t.c.meta).where(t.c.id == fn_id)).first()
    except Exception:
        return default
    m = row[0] if row else None
    if isinstance(m, str):
        try:
            m = json.loads(m)
        except Exception:
            return default
    return (m or {}).get(key, default)

def _meta_read(fn_id):
    # tdp_css from the function's meta column, via a direct DB read.
    return _meta_read_key(fn_id, "tdp_css", "")

def _meta_write_key(fn_id, key, value):
    # Merge ONE key into the function's meta column, via a direct DB write.
    # Never clobbers the other keys (tdp_css and tdp_slots coexist).
    import json
    from sqlalchemy import table, column, JSON, select, update
    eng = _meta_engine()
    if eng is None:
        return
    try:
        t = table("function", column("id"), column("meta", JSON))
        with eng.begin() as conn:
            row = conn.execute(select(t.c.meta).where(t.c.id == fn_id)).first()
            m = row[0] if row else None
            if isinstance(m, str):
                try:
                    m = json.loads(m)
                except Exception:
                    m = {}
            m = dict(m or {})
            m[key] = value
            conn.execute(update(t).where(t.c.id == fn_id).values(meta=m))
    except Exception:
        pass

def _meta_write(fn_id, css):
    # Merge tdp_css into the function's meta column, via a direct DB write.
    _meta_write_key(fn_id, "tdp_css", css)

def _norm_slots(raw):
    # Slot list -> clean [{name, json, css}]. Drops unnamed/empty-css entries,
    # caps the library, trims names.
    out = []
    for s in raw if isinstance(raw, list) else []:
        if len(out) >= SLOT_CAP:
            break
        if not isinstance(s, dict):
            continue
        name = str(s.get("name", "")).strip()[:SLOT_NAME_MAX]
        css = s.get("css", "")
        js = s.get("json", "")
        if not isinstance(css, str) or not isinstance(js, str):
            continue
        if not name or not css:
            continue
        out.append({"name": name, "json": js, "css": css})
    return out

def _slots_read(fn_id):
    # tdp_slots from the function's meta column, via a direct DB read.
    return _norm_slots(_meta_read_key(fn_id, "tdp_slots", []))

async def _is_admin(request):
    # Admin gate, version-agnostic. Open WebUI auth layout drifts:
    # cookie "token" (latest, set Secure so http installs never see it),
    # cookie "session_token" (legacy), Authorization Bearer (the real path
    # on http installs — the SPA stores localStorage.token and fetches with
    # it), session util signature drift, env secret name drift.
    import inspect
    token = request.cookies.get("token") or request.cookies.get("session_token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    # 1) latest OWUI official helper (async; handles header, cookie, state)
    if token:
        try:
            m = __import__("open_webui.utils.auth", fromlist=["get_optional_verified_user_from_request"])
            f = getattr(m, "get_optional_verified_user_from_request", None)
            if f is not None:
                u = f(request)
                if inspect.iscoroutine(u):
                    u = await u
                if u is not None and getattr(u, "role", None) == "admin":
                    return True
        except Exception:
            pass
    if not token:
        return False
    # 2) legacy session util probes
    for modpath in ("open_webui.apps.webui.utils", "open_webui.utils.auth"):
        try:
            m = __import__(modpath, fromlist=["get_user_from_session"])
            f = getattr(m, "get_user_from_session", None)
            if f is None:
                continue
            user = None
            for args in ((request,), (request, None), (request, None, None)):
                try:
                    user = f(*args)
                    break
                except Exception:
                    pass
            if inspect.iscoroutine(user):
                user = await user
            if user is None:
                continue
            if getattr(user, "role", None) == "admin" or getattr(user, "is_admin", False):
                return True
        except Exception:
            continue
    # 3) direct JWT decode + user row read
    try:
        import jwt
        env = __import__("open_webui.env", fromlist=["WEBUI_SECRET_KEY"])
        secret = None
        for name in ("WEBUI_SECRET_KEY", "JWT_SECRET_KEY", "JWT_SECRET", "SECRET_KEY"):
            secret = getattr(env, name, None)
            if secret:
                break
        if not secret:
            return False
        payload = jwt.decode(token, secret, algorithms=["HS256"])
        uid = payload.get("id")
        if not uid:
            return False
        # Revocation check (latest OWUI: session_stamp + Redis per-token/
        # per-user revocation). A leaked admin token must NOT keep working
        # after the session is revoked. If the revocation helper exists, it
        # must pass; old builds have no revocation system and fall through.
        # Open WebUI treats a raise here (missing/inactive account, DB error)
        # as a refusal, so when the helper exists a raise denies too.
        f = None
        try:
            m = __import__("open_webui.utils.auth", fromlist=["is_valid_token"])
            f = getattr(m, "is_valid_token", None)
        except Exception:
            f = None
        if f is not None:
            try:
                state = getattr(getattr(request, "app", None), "state", None)
                redis = getattr(state, "redis", None) if state is not None else None
                ok = f(payload, redis)
                if inspect.iscoroutine(ok):
                    ok = await ok
                if not ok:
                    return False
            except Exception:
                return False
        try:
            from open_webui.models.users import Users
            u = Users.get_user_by_id(uid)
            if inspect.iscoroutine(u):
                u = await u
            if u is not None and (getattr(u, "role", None) == "admin" or getattr(u, "is_admin", False)):
                return True
        except Exception:
            pass
        eng = _meta_engine()
        if eng is None:
            return False
        from sqlalchemy import table, column, select
        t = table("user", column("id"), column("role"), column("is_admin"))
        with eng.connect() as conn:
            try:
                row = conn.execute(select(t.c.role).where(t.c.id == uid)).first()
                if row and row[0] == "admin":
                    return True
            except Exception:
                pass
            try:
                row = conn.execute(select(t.c.is_admin).where(t.c.id == uid)).first()
                if row and row[0] in (True, 1, "true", "1"):
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False

def _ensure_pages(app):
    # Define handlers first (insert() references them), then insert the
    # routes BEFORE the SPA catch-all so it never shadows /skin.
    async def theme_page(request):
        # The page itself is open; the page script gates non-admins client-side
        # (localStorage user.info), and every fetch sends the Bearer token so
        # the state-changing /skin/save stays server-gated. Direct navigation
        # carries no Authorization header, so a server-only gate can never see
        # the session on http installs (Secure cookie dropped by browsers).
        return HTMLResponse(THEME_PAGE_HTML)

    async def theme_converter_js(request):
        return Response(CONVERTER_SRC.encode("utf-8"), media_type="application/javascript; charset=utf-8")

    async def theme_preview_js(request):
        return Response(PREVIEW_SRC.encode("utf-8"), media_type="application/javascript; charset=utf-8")

    async def theme_logo(request):
        return Response(base64.b64decode(LOGO_B64), media_type="image/png")

    async def theme_css(request):
        return Response(_active_css(app), media_type="text/css; charset=utf-8")

    async def persist_meta(fn_id, key, value):
        # Merge ONE meta key. Functions.update_function_metadata_by_id REPLACES
        # meta in some Open WebUI versions, so read-merge-write keeps tdp_css
        # and tdp_slots coexisting; falls back to the direct DB merge.
        try:
            from open_webui.models.functions import Functions
            fn = await Functions.get_function_by_id(fn_id)
            merged = dict(getattr(fn, "meta", None) or {})
            merged[key] = value
            await Functions.update_function_metadata_by_id(fn_id, merged)
        except Exception:
            _meta_write_key(fn_id, key, value)

    def slots_payload():
        # Library + which slot's CSS is the live theme right now (no extra
        # state: compare each slot css to _active_css).
        import json
        slots = getattr(app.state, "_tdp_slots", [])
        live = _active_css(app)
        applied = next((s["name"] for s in slots if live and s["css"] == live), None)
        return json.dumps({
            "ok": True,
            "slots": [{"name": s["name"], "json": s["json"]} for s in slots],
            "applied": applied,
            "live": bool(live),
        })

    async def theme_slots_get(request):
        # Library listing is admin-gated too: the /skin page is already
        # client-gated, so an open read buys nothing — the whole library stays
        # admin-only. /skin is an admin tool; no non-admin read path exists.
        if not await _is_admin(request):
            return Response("Not allowed. Admin session required.", status_code=403, media_type="text/plain")
        return Response(slots_payload(), media_type="application/json; charset=utf-8")

    async def theme_slots_save(request):
        # Library mutation (add/update/delete) — same admin gate as /skin/save.
        import json
        if not await _is_admin(request):
            return Response("Not allowed. Admin session required.", status_code=403, media_type="text/plain")
        form = await request.form()
        op = str(form.get("op", ""))
        name = str(form.get("name", "")).strip()[:SLOT_NAME_MAX]
        js = str(form.get("json", ""))
        css = str(form.get("css", ""))
        slots = list(getattr(app.state, "_tdp_slots", []))
        low = name.lower()
        if not name:
            return Response(json.dumps({"ok": False, "error": "Slot name required."}), status_code=400, media_type="application/json; charset=utf-8")
        if op == "add":
            if any(s["name"].lower() == low for s in slots):
                return Response(json.dumps({"ok": False, "error": "Slot name already used."}), status_code=400, media_type="application/json; charset=utf-8")
            if len(slots) >= SLOT_CAP:
                return Response(json.dumps({"ok": False, "error": "Library is full (12 slots)."}), status_code=400, media_type="application/json; charset=utf-8")
            slots.append({"name": name, "json": js, "css": css})
        elif op == "update":
            hit = [s for s in slots if s["name"].lower() == low]
            if not hit:
                return Response(json.dumps({"ok": False, "error": "Slot not found."}), status_code=400, media_type="application/json; charset=utf-8")
            hit[0]["json"] = js
            hit[0]["css"] = css
        elif op == "delete":
            rest = [s for s in slots if s["name"].lower() != low]
            if len(rest) == len(slots):
                return Response(json.dumps({"ok": False, "error": "Slot not found."}), status_code=400, media_type="application/json; charset=utf-8")
            slots = rest
        else:
            return Response(json.dumps({"ok": False, "error": "Unknown operation."}), status_code=400, media_type="application/json; charset=utf-8")
        slots = _norm_slots(slots)
        app.state._tdp_slots = slots  # live effect for the running instance
        fn_id = getattr(app.state, "_tdp_id", None)
        if fn_id:
            # Persist the library so it survives restarts, server-wide.
            await persist_meta(fn_id, "tdp_slots", slots)
        return Response(slots_payload(), media_type="application/json; charset=utf-8")

    async def theme_save(request):
        if not await _is_admin(request):
            return Response("Not allowed. Admin session required.", status_code=403, media_type="text/plain")
        form = await request.form()
        css = form.get("css", "")
        app.state._tdp_css = css  # immediate effect for the running instance
        app.state._tdp_persisted = css
        fn_id = getattr(app.state, "_tdp_id", None)
        if fn_id:
            # Persist to the function meta so it survives restarts, server-wide.
            await persist_meta(fn_id, "tdp_css", css)
        return Response("Success. Reload Open-WebUI's Page.", media_type="text/plain")

    def spa_index(routes):
        for i, r in enumerate(routes):
            if getattr(r, "path", "") in ("", "/"):
                return i
        return len(routes)
    routes_spec = [("/skin", theme_page, "GET"), ("/skin/converter.js", theme_converter_js, "GET"), ("/skin/preview.js", theme_preview_js, "GET"), ("/skin/owui-logo.png", theme_logo, "GET"), ("/skin/css", theme_css, "GET"), ("/skin/slots", theme_slots_get, "GET"), ("/skin/slots/save", theme_slots_save, "POST"), ("/skin/save", theme_save, "POST")]
    paths = tuple(p for p, h, m in routes_spec)
    # Always re-register: a Starlette Route binds dispatch at construction,
    # so a function edit MUST rebuild the Route objects around the fresh
    # handler closures (mutating .endpoint on old Routes does nothing).
    app.routes[:] = [r for r in app.routes if getattr(r, "path", "") not in paths]
    idx = spa_index(app.routes)
    from starlette.routing import Route
    for p, h, m in routes_spec:
        app.routes.insert(idx, Route(p, h, methods=[m]))
        idx += 1

class Event:
    async def event(self, event: dict, __event_name__: str = None,
                     __id__: str = None, __app__=None, **kwargs) -> None:
        if __app__ is None:
            return
        if __id__:
            __app__.state._tdp_id = __id__
            try:
                from open_webui.models.functions import Functions
                fn = await Functions.get_function_by_id(__id__)
                meta = getattr(fn, "meta", None) or {}
                __app__.state._tdp_persisted = meta.get("tdp_css", "")
                __app__.state._tdp_slots = _norm_slots(meta.get("tdp_slots", []))
            except Exception:
                __app__.state._tdp_persisted = _meta_read(__id__)
                __app__.state._tdp_slots = _slots_read(__id__)
        asset_register(__app__, CUSTOM_CSS_PATH,
                       "/* SKINSMITH-START */", "/* SKINSMITH-END */",
                       lambda: _active_css(__app__), order=1)
        _ensure_pages(__app__)
