---
title: Agy Worker 2
emoji: 🪐
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# Antigravity Self-Hosted Jetski WebUI (`agy-server`)

Self-hosted reverse proxy and mobile/offline bundle rewriter for the **official local Antigravity Jetski WebUI** embedded in Google's `language_server` (`2.26.0-6546645232517120`) and `agy` CLI (`1.4.7-1776195724`), built on and extending the architecture of [`AFSlayer/antigravity-server`](https://github.com/AFSlayer/antigravity-server).

## Key Design Principles

1. **Official Local Jetski Frontend (Not a Custom Replacement UI)**
   - Serves the actual React 19 + TanStack Router + Lexical + Tailwind v4 + ConnectRPC WebUI embedded in the official Antigravity `language_server` binary (`index.html`, `/main.js`, `/jetbox.css`, `/compiled_tailwind.css`, `/prism_bundle.js`).
   - All `exa.language_server_pb.LanguageServerService/*` ConnectRPC endpoints are handled directly by the official local `language_server` process over loopback (`127.0.0.1`).

2. **Zero Google-Hosted Frontend Dependencies**
   - Strips `<link rel="preconnect">` tags to `fonts.googleapis.com`, `fonts.gstatic.com`, `www.gstatic.com`, and `ssl.gstatic.com` from `index.html`.
   - Rewrites hardcoded `https://www.gstatic.com/external_hosted/katex/dist/fonts/` URLs, `https://fonts.gstatic.com/s/i/short-term/release/googlesymbols/ipy_notebook/default/24px.svg`, and `https://www.gstatic.com/images/branding/productlogos/` in `main.js` to locally embedded assets under `/__agy/vendor/...`.
   - Strips runtime `@import url('https://fonts.googleapis.com/css2?...');` rules from inline diagram styles.
   - Verified with headless Chromium (Playwright): **0 browser requests** to `*.googleapis.com` or `*.gstatic.com`.

3. **Zero External Tunnels or Remote-Control Relays**
   - Served directly from Hugging Face Spaces' native HTTPS router (`port 7860`).
   - Explicitly writes `"remoteControlEnabled": false` in `~/.gemini/settings.json` so `language_server` never connects to Google's remote-control relay (`[RemoteControl] Staying disconnected: Remote Control user setting is off`).

4. **Shared Official `agy` CLI & `language_server` Runtime State**
   - Runs `language_server --standalone --subclient_type hub --app_data_dir antigravity-cli`.
   - Synchronizes OAuth tokens across `~/.gemini/antigravity-cli/antigravity-oauth-token`, `~/.gemini/antigravity/antigravity-oauth-token`, and `~/.gemini/jetski-standalone-oauth-token` (`0600` permissions) so `agy` CLI (`1.4.7`) and the Jetski WebUI share one login and one `~/.gemini/antigravity-cli/brain` conversation/artifact store.
   - Provides headless Google OAuth login at `/__agy/signin` via an `xdg-open` interception shim that captures the official `LanguageServerService/Login` OAuth URL and exchanges the pasted localhost callback URL on the server loop.

## Repository Layout

- `cmd/agy-server/` — CLI entrypoint (`run`, `status`, `stop`, `restart`, `logs`, `update`)
- `internal/lsproc/` — Launches and supervises the official `language_server` binary, extracts ephemeral port & CSRF token, disables Remote Control, and synchronizes OAuth tokens
- `internal/proxy/` — Reverse proxy (`/__agy/*` endpoints + CSRF injection + synchronous bundle pre-warming)
- `internal/patches/` — Deterministic byte-level asset rewriter (`23` total rules: `18` mobile/UX rules + `5` Google-dependency-removal rules)
- `internal/assets/` — Embedded client scripts (`mobile.js`, `mobile.css`) and self-hosted vendor assets (`internal/assets/vendor/katex/fonts/*.woff2`, `internal/assets/vendor/icons/*.svg`)
- `internal/signin/` — Headless Google OAuth flow (`/__agy/signin`) driving `LanguageServerService/Login`
- `internal/auth/` — Password gate (`/__agy/login`), HMAC-SHA256 session cookies, rate limiting, and security headers
- `internal/upload/` — Multipart file upload endpoint (`/__agy/upload`)
- `internal/rules/` — Global rules editor (`/__agy/rules` -> `~/.gemini/GEMINI.md`)
- `internal/term/` — Built-in WebSocket PTY terminal (`/__agy/term`)
- `docs/REA_ANALYSIS_AND_PARITY.md` — Full [`morluto/rea`](https://github.com/morluto/rea) reverse-engineering evidence, route/RPC mapping, and parity verification report
