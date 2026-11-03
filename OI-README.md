## Skin-Smith

**A lightweight skin engine for Open WebUI.** Feed it any [Theme Designer Pro](https://github.com/silentoplayz/theme-designer-pro-presets) theme JSON and Skin-Smith converts that theme into Open WebUI Custom CSS — applied cleanly for every user, persisted across restarts, from a single readable, self-contained function file. Keep your themes in the built-in library and switch between them anytime. No server, no frameworks, no runtime — and **no theme baked into the file**: the themes come from TDP's JSON, a source many of us already know and love.

> **Beta (v0.8.4).** Now supports **Open WebUI 0.11.0+**. New in this line: a **Theme Manager** — save up to 12 themes in a library, pick and apply any of them, all **persisted across Open WebUI restarts**; a **security update** (every state endpoint is admin-gated server-side, with token-revocation checks); and a full **UI/UX overhaul** of the `/skin` page. The conversion engine is proven; please report issues.

## Why I made this

[Theme Designer Pro](https://github.com/silentoplayz/theme-designer-pro-presets) is brilliant — and, like any full-featured extension, it carries a lot of runtime (SSE, Web Workers, Canvas FX, fonts, scripts) to get there. I wanted the **engine's result** on my Open WebUI with **one small file**: Skin-Smith takes what TDP produces from a theme JSON (palette, accents, manual overrides, gradient, custom CSS) and loads it as Open WebUI Custom CSS — that's all it does. Skin-Smith is the engine; TDP is the paint.

Skin-Smith is made **with love for the Theme Designer Pro team's work**. Every line of palette, accent, and gradient math is ported **verbatim** from their official code, with their documentation shipped alongside — and, as the MIT License asks, their notice (TDP `LICENSE`, (c) 2026 silentoplayz) is embedded in the file and shipped in this repo. We deliberately ship **no CSS library of our own** — the one exception is an Indigo sample draft that lights up the preview generator. The themes should come from the masters. The previewer is TDP's own previewer, too — I haven't gotten around to stripping it down to show only the CSS changes, so you'll see a bit more than the final CSS. Make your theme here:

**🎨 Theme Designer Pro Studio → https://silentoplayz.github.io/theme-designer-pro-presets/studio/**

Export the theme JSON, paste it into Skin-Smith, and your theme is live.

## Install

**Open WebUI ≥ 0.11.0 required.** Works on **SQLite** and **PostgreSQL** — persistence confirmed in the field on both.

- **Manual:** right-click your username → **Admin Panel → Functions** → **Import JSON** and Upload the function JSON (`skin-smith.json`) → **Save**.
- **One-click:** Click **Get** in the Open-WebUI Community Post and point it to your server → **Save**.

## Use

1. Make your theme in the [TDP Studio](https://silentoplayz.github.io/theme-designer-pro-presets/studio/) and export its JSON.
2. Sign in as an **admin** and open `http://<open-webui host>/skin` — Skin-Smith's converter page.
3. **Theme Manager:** name the theme and hit **Save as new** — it lands in the library (up to 12). Pick any saved theme from the dropdown to load it back, **Update slot** to overwrite, **Delete slot** to remove. The library **and** the applied theme are persisted across Open WebUI restarts.
4. Paste/upload the JSON (or hit **Load sample**), switch Dark / Light / OLED / Her to preview it live.
5. **Apply & Save** — the CSS is served as `/static/custom.css` for all users, persisted across restarts (function meta, with a direct DB fallback for builds where the functions model module is broken). Requires an **admin session**. **Restore default** clears it back to Open WebUI's own default.

## A note on running Skin-Smith together with Theme Designer Pro

Running Skin-Smith **and** Theme Designer Pro at the same time is redundant: both inject theme CSS, and the two effects can stack or conflict. We made our best effort to comply with TDP's shared-asset coexistence contract (their registry, their route, composed by order), and coexistence is technically safe — but for a clean result, **use one or the other**. Skin-Smith is built for people who want to run the TDP theme engine as lightly as possible — especially on older machines like mine.

## Links

- **GitHub repo:** https://github.com/xWaLeEdOoOx/skin-smith
- **Releases (download `skin-smith.py` + changelog):** https://github.com/xWaLeEdOoOx/skin-smith/releases
- **Open-WebUI Post:** https://openwebui.com/posts/c6c37581-8954-435a-9b3f-51504d368835

*Skin-Smith: Made by Waleed Kh. Tolbah (AKA xWaLeEdOoOx), released with respect and gratitude to the Theme Designer Pro team.*

*Portions of Skin-Smith are based on or inspired by Theme Designer Pro by @G30 / silentoplayz. Used and adapted with permission under the MIT License.*
