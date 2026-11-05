# Changelog

All notable changes to Skin-Smith, newest first. The version number lives in the
function docstring of `skin-smith.py` (`version:`).

## v0.9.9 — 2026-11-05

- **User text cannot carry the `SKINSMITH-START` / `SKINSMITH-END` markers**
  (B6). The served `/static/custom.css` is wrapped in those markers, and the
  shared-asset registry composes every loader's block **by the markers** — so a
  theme whose custom CSS, manual overrides, palette overrides or name contains a
  marker string would close the injected block early, letting the rest of it
  escape the wrapper and collide with another loader's block. Any `SKINSMITH-`
  in user-supplied text is neutralized to `SKINSMITH_` before it enters the CSS:
  not a marker, legal inside a CSS comment, an identifier and a custom-property
  value, and the user's own rules survive intact. Comment delimiters in the
  theme **name** are stripped too, so a name can't close the header comment
  early.
- Converter-side sanitization only: the shared-asset registry block stays
  byte-identical to TDP, the converter math is verbatim, the preview engine is
  untouched, and the page's element-ID contract, the admin gate, the persistence
  layer and the exact success message are untouched. **Proven against the user's
  real everyday export: the emitted CSS is byte-identical** (`80e1973e5b044a95`,
  14771 chars) — a theme without markers converts exactly as before.

## v0.9.8 — 2026-11-05

- **The converter clamps theme values the way TDP clamps them** (ported from the
  generator's `_MODE_NUM_BOUNDS` / `_num` / `_toFiniteNumber` / `_cssTok`,
  `theme_designer_pro_event.py` L9085–9121). A theme JSON that never passed
  through TDP's designer used to reach the CSS as written: a non-numeric
  `gradientAngle` emitted `linear-gradient(abcdeg, …)` (invalid CSS), an
  out-of-range `h` / `c` / `l` / intensity / radial position / speed emitted its
  own value, a junk `gradientType` / `gradientRadialShape` / `gradientRadialSize`
  fell through, a `url()` mesh background landed in the CSS, and non-string
  `customCSS` / `manualOverrides` were coerced into `[object Object]`. Every
  present field now takes **TDP's default** when it is out of range or the wrong
  type — bounds `h [0,360]`, `c [0,150]`, `l [0,100]`, `gradientAngle [0,360]`,
  `gradientIntensity [0,100]`, `gradientRadialPosX/Y [0,100]`,
  `gradientAnimationSpeed [2,30]` (TDP's note: speed 0 freezes a 3×-zoomed
  gradient instead of animating it), enums `linear|radial|conic|mesh`,
  `circle|ellipse`, and the four radial sizes. TDP's `_toFiniteNumber` rule is
  kept: `null` and `''` are **unset**, never zero — TDP's recorded bug is a theme
  serialized with nulls becoming 0deg angle and intensity 0, flattening every
  gradient stop to grey. The raw preset-file aliases (`type` / `angle` /
  `intensity` / `speed` / `radialShape` / …) go through the same bounds, so an
  alias cannot smuggle an invalid value past.
- **Only present keys are touched**, so Skin-Smith's own mode fallbacks (missing
  `oled` = dark with `l=0`, missing `her` = light, `manualOverrides` text without
  `manualOverridesEnabled` = enabled, accent not `#rrggbb` = generated) stay
  exactly as they are. **Proven against the user's real everyday export: the
  emitted CSS is byte-identical before and after** (`80e1973e5b044a95`, 14771
  chars) — studio.js exports are already clamped by TDP's designer, so this pass
  is a no-op for them.
- Converter-only: the preview engine is verbatim, and the converter math, the
  shared-asset registry block, the page's element-ID contract, the admin gate,
  the persistence layer and the exact success message are untouched.

## v0.9.5 — 2026-11-03

- **Conic gradients are real `conic-gradient()`s.** The theme JSON admits the
  `conic` gradient type; TDP's own generator emits
  `conic-gradient(from Xdeg, stops)`. Skin-Smith fell conic through to
  `linear-gradient`, so a conic theme shipped the wrong shape. The converter
  now emits the real conic — same stops, same intensity math, angle default
  135 — matching what TDP serves. The preview engine is untouched (TDP math
  verbatim); the emitted CSS is now TDP-faithful for conic themes.

## v0.9.4 — 2026-11-03

- **`autoScope: false` verified end-to-end** (the raw pass-through TDP ships):
  a theme that turns auto-scoping off emits its custom CSS **raw**, with
  `:root` rewritten to `:is(selector)` and every other rule left exactly as
  written — including an `html` block, which TDP's raw branch deliberately
  does not rewrite (the user opted out of scoping). The path shipped with
  v0.9.1; this version pins it with explicit assertions: raw text outside any
  scope wrapper, `:root` → `:is(.her)`, `html` verbatim, and the scoped-mode
  check that no bare `html` block survives the scoped transform.
- No emission change: the converter math, the shared-asset registry block,
  the page's element-ID contract, the admin gate, the persistence layer and
  the exact success message are untouched.

## v0.9.3 — 2026-11-03

- **The gradient is now visible.** Open WebUI paints opaque panels
  (`.app`, `main`, every `bg-gray-*` utility, message content, sticky bars,
  the chat overlay) on top of the page, so a scoped gradient sat **hidden**
  behind them. Skin-Smith now emits TDP's structural layer: the body and
  sidebar base colour for every mode, and for modes with a gradient the full
  transparency clear — `.app` cleared with `position: relative`, `main`,
  `.app > div`, the `bg-gray-*` utilities (with TDP's guard list: buttons,
  links, inputs, labels, spans and the user's own message bubble keep their
  backgrounds), `.message-content`, `nav` and sticky bars — plus the gradient
  section's own extras: Open WebUI's `body::before` removed, the chat overlay
  and the sidebar tinted with the gradient's base colour (the darkest stop),
  sidebar blur. TDP's designer/auth selectors are dropped (Skin-Smith has no
  designer page).
- Emission-only change: the converter math, the shared-asset registry block,
  the page's element-ID contract, the admin gate, the persistence layer and
  the exact success message are untouched.

## v0.9.2 — 2026-11-03

- **Every mode gets its own gradient.** The theme JSON carries a full gradient
  per mode (type, angle, stops, intensity, animation, radial geometry, mesh
  points), and the preview has always shown the active mode's. The emitted CSS
  used to ship only the first enabled mode's gradient, on a bare `body` — so
  Light, Her and OLED users saw the dark theme's background. Gradients are now
  scoped to Open WebUI's real classes: `.dark body`, `.light body`, `.her body`,
  and the OLED inline true-black marker (never `.dark`), emitted in dark →
  light → oled → her order so `.her body` wins when `<html>` carries `.her` plus
  `.light`. One shared `@keyframes` is emitted once. Pages with no mode class
  keep Open WebUI's own background.

## v0.9.1 — 2026-11-03

- **Custom CSS now scopes exactly as TDP does.** A theme whose custom CSS starts
  with `:root` (the common case) used to ship **inert** CSS: wrapping it inside a
  mode block compiles to `.dark :root` / `.dark html`, which never matches. TDP's
  generator maps `:root` and `html` to `&` (the parent selector) and hoists
  `@keyframes` out of the scoped block; `autoScope: false` emits the text raw
  with `:root` rewritten to `:is(selector)`. All three paths are ported, so the
  theme's variables and animation keyframes now apply.
- Emission-only change: the converter math, the shared-asset registry block, the
  page's element-ID contract, the admin gate, the persistence layer and the exact
  success message are untouched.

## v0.8.4 — 2026-11-03

- **Theme Manager** (new in the 0.8 line): up to 12 themes stored in the function
  meta key `tdp_slots`; a dropdown on `/skin` with full CRUD — Save as new,
  Update slot, Delete slot. Applying a saved theme reuses `/skin/save`, so the
  admin gate, persistence, and the exact success message are unchanged. The
  library **and** the applied theme survive Open WebUI restarts.
- **Security**: `GET /skin/slots` is gated by the same admin gate as the slot
  writes and `/skin/save` — the theme library has no unauthenticated read path.
  `/skin/css` (the engine's own CSS output) stays public by design, as
  `/static/custom.css` is served to every user.
- **UI/UX overhaul** of the `/skin` page (frosted slate): design tokens in
  `:root`, sticky glass top bar with the applied-theme chip, Library / Source /
  Preview cards, segmented mode tabs in the preview header, one unified button
  family, styled select, mono editor, full-width layout, `box-sizing` fix so the
  editor never overflows its card, `prefers-reduced-motion` and
  `prefers-reduced-transparency` fallbacks.
- **Feel**: the status chip is a state chip (neutral while saving, green
  success, red failure, 160ms slide-in); `Apply & Save` shows a saving state and
  restores; `Ctrl+Enter` / `Cmd+Enter` applies the current JSON; the preview
  cross-fades on every re-render; empty-library message in the applied chip; an
  `n / 12` library counter that turns red at the cap.
- **Compatibility**: floor lowered to **Open WebUI 0.11.0** (field-confirmed), so
  more installs can run Skin-Smith.
- Persistence confirmed in the field on **PostgreSQL** and **SQLite**.

## v0.7.1 — 2026-11-02

- OLED scoped to its real fingerprint:
  `html[style*='--color-gray-800: #101010']`. Open WebUI sets the true-black
  variables inline on `<html>` only for OLED Dark, so riding OLED on `.dark` no
  longer leaks the true-black palette into plain Dark. Light and Her unchanged.

## v0.6.14 — 2026-11-02

- Mode blocks scope to Open WebUI's real `<html>` classes (`.dark` / `.light` /
  `.her`). Open WebUI never sets a `data-theme` attribute, so the Light / OLED /
  Her blocks never matched and only Dark applied. The preview keeps TDP's own
  `data-theme` convention, unchanged.

## v0.6.13 — 2026-11-02

- Revocation hardening: when Open WebUI's `is_valid_token` helper **raises**
  (missing/inactive account, DB error) the gate denies, matching Open WebUI.
  Reported by the TDP author.
- TDP's MIT `LICENSE` added to the repo, with the notice embedded in
  `skin-smith.py`.

## v0.6.12 — 2026-11-02

- Revocation check in the gate's JWT path: `open_webui.utils.auth.is_valid_token`
  is probed after decoding, so a revoked admin token (session stamp change,
  password change, Redis revocation) is denied immediately. Builds without the
  revocation system fall through to the role check.

## v0.6.11 — 2026-11-02

- Temporary `/skin/debug` route removed (the gate confirmed in the field).

## v0.6.10 — 2026-11-02

- Gate aligned with Open WebUI's current auth: the session cookie is named
  `token` (Secure-flagged, so absent on http), the official
  `get_optional_verified_user_from_request` probe is used, the env secret is
  `WEBUI_SECRET_KEY`, and the page sends `Authorization: Bearer` from
  `localStorage.token`.

## v0.6.9 — 2026-11-02

- Server gate accepts the session cookie **or** `Authorization: Bearer`; the
  `/skin` page is gated client-side by role, and its fetches carry the bearer
  token.

## v0.6.8 — 2026-11-02

- The admin gate for `/skin/save` (Apply & Save and Restore default) added,
  after the TDP authors' private report. Non-admin POSTs get
  `Not allowed. Admin session required.` with 403.

## v0.6.6 — 2026-11-02

- Route re-registration fix: `_ensure_pages` always removes the `/skin*` routes
  and inserts fresh `Route` objects before the SPA catch-all, so pasting a new
  function version takes effect immediately (Starlette binds dispatch at
  construction — mutating an existing endpoint is a no-op).

## Earlier

The first line (v0.1 → v0.6.7) built the converter (TDP math ported verbatim),
the `/skin` page, the previewer, persistence via function meta with a direct DB
fallback, and `/static/custom.css` serving. See the commit history for the
step-by-step.
