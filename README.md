<img width="1672" height="941" alt="cover" src="assets/cover.png" />


## Skin-Smith

**A lightweight skin engine for Open WebUI.** Feed it any [Theme Designer Pro](https://github.com/silentoplayz/theme-designer-pro-presets) theme JSON and Skin-Smith converts that theme into Open WebUI Custom CSS — applied cleanly for every user, persisted across restarts, from a single readable, self-contained function file. Keep your themes in the built-in library and switch between them anytime. No server, no frameworks, no runtime — and **no theme baked into the file**: the themes come from TDP's JSON, a source many of us already know and love.

> **v0.9.5 — your theme now looks the way the Studio preview looks.** Every mode — Dark, Light, OLED, Her — gets its own background. The gradient fills the page and Open WebUI's panels no longer hide it; the sidebar takes a matching tint, and your bubbles, highlights and accents all land in the theme's own colours. Your theme's custom CSS applies in the mode it belongs to, and all of it survives restarts. Same one small file, nothing new to install — update the function, **Apply & Save**.

> **Beta (v0.8.4).** Now supports **Open WebUI 0.11.0+**. New in this line: a **Theme Manager** — save up to 12 themes in a library, pick and apply any of them, all **persisted across Open WebUI restarts**; a **security update** (every state endpoint is admin-gated server-side, with token-revocation checks); and a full **UI/UX overhaul** of the `/skin` page. The conversion engine is proven; please report issues.

## What it is

- **One file, one function.** `skin-smith.py` is the entire extension: a self-contained Open WebUI Event-function loader. It is identical for every theme — no CSS embedded, no valve, no logging.
- **An engine, not a theme pack.** Skin-Smith takes what TDP produces from a theme JSON (palette, accents, manual overrides, gradient, custom CSS) and loads it as Open WebUI Custom CSS — that's all it does. Skin-Smith is the engine; TDP is the paint.
- **A theme manager on top of the engine.** A 12-slot library lives in the function meta: save, name, switch, update, delete — and the library **and** the applied theme survive Open WebUI restarts. Themes still come from TDP's JSON, never baked into the file.
- **Every line of palette, accent, and gradient math is ported verbatim** from the Theme Designer Pro team's official code, with respect and gratitude to them. We ship no CSS library of our own — the one exception is an Indigo sample draft that lights up the preview generator.

## What Skin-Smith extracts

From the TDP theme JSON, Skin-Smith emits, per mode (Dark / Light / OLED / Her), CSS scoped to Open WebUI's real mode classes — `.dark`, `.light`, `.her`, and the OLED inline true-black marker:

- **Palette** — the mode's hue, chroma and lightness drive Open WebUI's whole gray scale (`--color-gray-50` … `--color-gray-950`). TDP's math, verbatim.
- **Accent** — the mode's accent becomes `--tdp-accent` plus its two derived accents. Verbatim.
- **Manual overrides** — the mode's `manualOverrides` block, verbatim.
- **Custom CSS** — auto-scoped to the mode the way TDP scopes it: `:root` and `html` blocks are rewritten to the parent selector so they actually apply, `@keyframes` are hoisted out of the scoped block, and a theme with `autoScope: false` passes through raw.
- **Gradient** — every mode gets **its own**: linear (angle), radial (shape, size, position), **conic** (`conic-gradient(from Xdeg, …)`), and mesh (points, spread, background) — intensity-adjusted stops, animation at the mode's own speed.
- **Structural layer** — the body and sidebar base colours, and for gradient modes the clearing of Open WebUI's opaque panels so the gradient is visible: app shell, main, sidebar, message content, nav, sticky bars and background utilities, with the chat overlay and sidebar tinted with the gradient's base colour. The guard list keeps buttons, links, inputs, labels, spans and your own message bubble intact.
- **OLED** — true-black palette overrides, scoped to Open WebUI's OLED marker; plain Dark never leaks true-black.

In the JSON, two things carry no effect in Skin-Smith, and we say so honestly:

- `--tdp-hue` / `--tdp-chroma` are emitted for TDP Canvas FX interop. Skin-Smith ships no canvas runtime, so they are inert.
- Radii, blur, spacing, font size, transition and transparency sliders are TDP **designer UI valves** — they are not export fields. What they produce arrives as overrides, manual overrides or custom CSS, which are already emitted.

## What it is not

- Not a full TDP extension. TDP's runtime (SSE, Web Workers, Canvas FX, fonts, scripts) is deliberately **not** replicated — Skin-Smith delivers the CSS result.
- Not a theme collection. Make your theme in the [TDP Studio](https://silentoplayz.github.io/theme-designer-pro-presets/studio/), export the JSON, and Skin-Smith brings it to life.

**🎨 Theme Designer Pro Studio → https://silentoplayz.github.io/theme-designer-pro-presets/studio/**

## Requirements

- **Open WebUI ≥ 0.11.0**
- **SQLite** and **PostgreSQL** — persistence confirmed in the field on both (PostgreSQL on a live Postgres-backed Open WebUI, SQLite on a SQLite install).

## Install (manual)

1. Get the file:

   ```
   git clone https://github.com/xWaLeEdOoOx/skin-smith.git
   ```

   (or just download `skin-smith.py` from the repo — the latest release, with
   the full changelog, is [here](https://github.com/xWaLeEdOoOx/skin-smith/releases))

2. In Open WebUI: right-click your username → **Admin Panel → Functions** → **Create** → paste the full contents of `skin-smith.py` → **Set as EVENT not Filter** → **Save**.

3. Open `http://<open-webui host>/skin` — Skin-Smith's converter page is live.

## Use

1. Make your theme in the [TDP Studio](https://silentoplayz.github.io/theme-designer-pro-presets/studio/) and export its JSON.
2. Sign in as an **admin** and open `http://<open-webui host>/skin`.
3. **Theme Manager:** name the theme and hit **Save as new** — it lands in the library (up to 12). Pick any saved theme from the dropdown to load it back, **Update slot** to overwrite, **Delete slot** to remove. The library **and** the applied theme are persisted across Open WebUI restarts.
4. Paste/upload the JSON (or hit **Load sample**), switch Dark / Light / OLED / Her to preview it live.
5. **Apply & Save** — the CSS is served as `/static/custom.css` for all users, persisted across restarts (function meta, with a direct DB fallback for builds where the functions model module is broken). Requires an **admin session**. **Restore default** clears it back to Open WebUI's own default.

## Running Skin-Smith together with Theme Designer Pro

Running Skin-Smith **and** Theme Designer Pro at the same time is redundant: both inject theme CSS, and the two effects can stack or conflict. We made our best effort to comply with TDP's shared-asset coexistence contract (their registry, their route, composed by order), and coexistence is technically safe — but for a clean result, **use one or the other**. Skin-Smith is built for people who want to run the TDP theme engine as lightly as possible — especially on older machines.

## Files in this repo

| File | Purpose |
|------|---------|
| `skin-smith.py` | The entire extension. Paste into Open WebUI → Functions. |
| `LICENSE` | Theme Designer Pro's MIT License — the palette/gradient math and the shared-asset registry contract are ported verbatim from TDP, (c) 2026 silentoplayz. The MIT notice is also embedded in `skin-smith.py`. |
| `OI-README.md` | The Open WebUI community post copy. |
| `CHANGELOG.md` | Version history of Skin-Smith, newest first. |
| `assets/cover.png` | The README cover image. |

## Links

- **This repo:** https://github.com/xWaLeEdOoOx/skin-smith
- **Theme Designer Pro:** https://github.com/silentoplayz/theme-designer-pro-presets
- **TDP Studio (make your theme):** https://silentoplayz.github.io/theme-designer-pro-presets/studio/
- **Open WebUI:** https://github.com/open-webui/open-webui

*Skin-Smith: Made by Waleed Kh. Tolbah (AKA xWaLeEdOoOx), released with respect and gratitude to the Theme Designer Pro team and Open-WebUI for their great efforts and amazing work*

*Portions of Skin-Smith are based on or inspired by Theme Designer Pro by @G30 / silentoplayz. Used and adapted with permission under the MIT License.*
