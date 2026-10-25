# REA Reverse-Engineering Analysis & Official Jetski WebUI Parity Report

## 1. Inspected Official Targets & Evidence Digests

Using [`morluto/rea`](https://github.com/morluto/rea) (`rea@6.7.0`), we extracted and analyzed both the official local Jetski frontend embedded in Google's Antigravity `language_server` (`2.26.0-6546645232517120`) and the installed official `agy` CLI (`1.4.7-1776195724`).

| Target | Version / Source | File Size | SHA-256 Digest | REA Evidence ID |
| :--- | :--- | :---: | :--- | :--- |
| Official `language_server` binary | `2.26.0-6546645232517120` (`linux-x64`) | `164,877,736` B | `4711bd2c175ee9868e67f30c3dbf5dcef36866a6c89d4f6feb14eac7a46dd6d8` | `ev_7785ebfa7f4cdb1bf0f5529532f0eb551c8d65714272ddcc33ace4281d42a8a6` |
| Official `agy` CLI binary | `1.4.7-1776195724` (`linux-x64`) | `169,240,112` B | `40349135371a8ed1f1af1e9f8b741de99174ae9fce6380b96c2d1f543480af7a` | `ev_8cc6276ec8ba727e9ac256e25080b505b131cee8fc5513a8d9993be723722318` |
| Embedded Jetski JS/CSS bundle (`2.26.0`) | `standalone_server` embedded FS | `10,952,278` B | See per-asset table below | `ev_ff2df4c953be58b9b52221b12952c4366840826e07233986b6120d3eefa9a9bd` |

### Official Embedded Jetski WebUI Assets (`language_server 2.26.0-6546645232517120`)

| Asset Route | Size (Bytes) | SHA-256 Digest | Role |
| :--- | :---: | :--- | :--- |
| `/` (`index.html`) | `3,892` | `6e21a668cdb26929b846bb500ec1a65e1fc5a0424bcaff5d31de61a9e98de981` | HTML shell, Tailwind theme loader, entry script tags |
| `/main.js` | `10,461,461` | `94abc09ac34dcae3a98f5e0fb74b66c87f8a8ad6ad214136544f924da2099e5e` | React 19 + TanStack Router + Lexical + ConnectRPC + KaTeX + Mermaid bundle |
| `/compiled_tailwind.css` | `249,475` | `5ba613f8e8f082f7fa8f96d5125a6602460ef607ce7f6333c0900e89fd07e16f` | Compiled Tailwind CSS v4 utility sheet (`--color-ide-*` variables) |
| `/jetbox.css` | `95,487` | `82e93aba1ce9799de626ab06fa174c37d74259eea43a5f0160fd1ab0ead13359` | Jetbox component styles, KaTeX math layout, syntax highlighting |
| `/prism_bundle.js` | `141,963` | `94c46cdf8a570db44a8a01b6a49b0c033cbb2788c174f70c4782d939bae8f2e2` | PrismJS syntax grammar bundle |

---

## 2. Mapped Routes, ConnectRPC Surface & External Dependencies

### 2.1 TanStack Router Routes (`main.js`)
The official Jetski frontend registers 24 client routes under TanStack Router:
- `/` — Home / New Conversation workspace selector
- `/c/$cascadeId` — Active conversation stream (`CascadeView`), tool approvals, terminal outputs, inline file diff review, and artifacts
- `/inbox` — Multi-agent notification & pending-action inbox
- `/ChangesOverview` — Workspace-wide Git changes & diff review
- `/Workspaces` — Workspace / folder switcher and repository browser
- `/Playground` — Scratchpad playground conversations
- `/BrowserView` — Subagent browser view stream
- `/AgentManager` — Subagent & background worker manager
- `/Knowledge`, `/Customization`, `/Skills`, `/Servers`, `/Plugins`, `/Settings`, `/Onboarding`, `/Shortcuts`, `/Panopticon`, `/Flowdex`, `/Chronicle`

### 2.2 ConnectRPC Endpoints (`exa.language_server_pb.LanguageServerService/*`)
- The Go binary exposes **352** ConnectRPC methods under `/exa.language_server_pb.LanguageServerService/*`.
- The Jetski frontend invokes **62** distinct methods via `lsClient`, including:
  - Session & Auth: `GetStatus`, `Login`, `Logout`, `GetUserStatus`, `GetCascadeModelConfigData`
  - Conversations & Streaming: `StartCascade`, `SendUserCascadeMessage`, `StreamCascadeReactiveUpdates`, `StreamCascadeSummariesReactiveUpdates`, `StreamAgentStateUpdates`, `CancelCascadeInvocation`, `UndoCascadeStep`, `DeleteCascadeTrajectory`
  - Tool & Permission Approvals: `HandleCascadeUserInteraction` (approves/denies file edits, terminal commands, browser actions, and `ask_question` prompts)
  - Workspaces & Git: `GetWorkspaceFolders`, `SetWorkingDirectories`, `AddTrackedWorkspace`, `ListBranches`, `CreateWorktree`, `GetPatchAndCodeChangeMetrics`
  - File & Media Access: `ArtifactView`, `OpenFilePointer`, `SaveMediaAsArtifact`, `LoadTrajectory`

### 2.3 External Google Frontend References Mapped & Removed
Static inspection of `index.html` and `main.js` identified every browser-side reference to Google-hosted static domains:
1. `index.html` lines 6–9:
   - `<link rel="preconnect" href="https://fonts.googleapis.com">`
   - `<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>`
   - `<link rel="preconnect" href="https://www.gstatic.com">`
   - `<link rel="preconnect" href="https://ssl.gstatic.com">`
2. `main.js`:
   - `https://www.gstatic.com/external_hosted/katex/dist/fonts/` (20 KaTeX `.woff2` font files)
   - `https://fonts.gstatic.com/s/i/short-term/release/googlesymbols/ipy_notebook/default/24px.svg`
   - `https://www.gstatic.com/images/branding/productlogos/` (`drive_2020q4_48dp.svg`, `docs_2020q4_48dp.svg`, `sheets_2020q4_48dp.svg`, `slides_2020q4_48dp.svg`)
   - `@import url('https://fonts.googleapis.com/css2?...');` in inline diagram stylesheet helper

All 5 categories are rewritten at startup by `internal/patches/registry.go` (`strip-google-preconnects`, `local-katex-fonts`, `local-google-icons`, `local-google-productlogos`, `strip-google-fonts-import`) and served locally from `/__agy/vendor/...` embedded in the `agy-server` binary.

---

## 3. Why `language_server` (`2.26.0`) Is Used Instead of `agy --hub` (`1.4.7`) Alone

Both `agy --hub` (`1.4.7`) and `language_server --standalone --subclient_type hub` (`2.26.0`) serve the official Jetski WebUI. However:
1. **`agy --hub` disables interactive OAuth RPC**: Calling `LanguageServerService/Login` on `agy --hub` returns `failed_precondition: interactive auth is only supported in antigravity-hub mode and is unavailable in cli, standalone, or stubby-auth modes`.
2. **`language_server --standalone --subclient_type hub --app_data_dir antigravity-cli` enables interactive OAuth RPC AND shares the CLI state**:
   - Supports `LanguageServerService/Login` so `/__agy/signin` works end-to-end on a headless container.
   - Shares `~/.gemini/antigravity-cli/antigravity-oauth-token` and `~/.gemini/antigravity-cli/brain` with the installed `agy 1.4.7` CLI.
   - Matches 100% (`21/21` enabled rules, `0` missing) of the responsive mobile layout and offline vendor patches.

---

## 4. Concrete Blockers & Honest Limitations

1. **Hosted `antigravity.google.com` vs Official Local Jetski Hub UI**:
   - The hosted `https://antigravity.google.com` web app is Google's cloud remote-control client, which connects to local workers over Google's proprietary Remote Control relay (`remoteControlEnabled: true`).
   - Because remote-control relays and external tunnels are prohibited for this deployment, Worker 2 serves the **official local Jetski WebUI** embedded in Google's `language_server` directly over Hugging Face Spaces' native HTTPS router (`https://abighozali-agy-worker-2.hf.space`) with `remoteControlEnabled: false`.
2. **Per-Worker Independent Google OAuth Session**:
   - Per requirement ("do not copy OAuth sessions between workers"), Worker 2 does not contain the Codespace's Google OAuth token baked into its Docker image or HF Space repository.
   - After unlocking `/__agy/login` on Worker 2, the user completes Google sign-in once at `/__agy/signin` (which invokes the official `LanguageServerService/Login` RPC, displays the official `accounts.google.com` authorization link, and exchanges the pasted `http://localhost:<port>/oauth-callback?...` redirect URL on Worker 2).
3. **Ephemeral Free-Tier HF Storage Across Container Rebuilds**:
   - On free-tier Docker Spaces without paid persistent storage (`hf_oauth` / persistent `/data` volume), `~/.gemini` persists across process restarts inside the running container, and can be backed up via HF Secrets, but a full Space container rebuild resets container disk state requiring re-authentication at `/__agy/signin` unless the token is stored in an HF Secret.
