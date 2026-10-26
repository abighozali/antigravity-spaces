---
title: Antigravity Space
emoji: 🪐
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# antigravity-spaces

Hugging Face Docker Spaces adaptation of [`AFSlayer/antigravity-server`](https://github.com/AFSlayer/antigravity-server) (Apache-2.0) serving the official local Antigravity Jetski WebUI (`language_server 2.26.0` + `agy 1.4.7`) with transactional state persistence on Hugging Face Storage Buckets.

> **Upstream & Trademark Notice**: This repository is a modified derivative of [`AFSlayer/antigravity-server`](https://github.com/AFSlayer/antigravity-server) (`33ccaeb8`). It is an independent community adaptation and is not affiliated with, endorsed by, or supported by Google LLC. See [`NOTICE`](NOTICE), [`DISCLAIMER.md`](DISCLAIMER.md), and [`LICENSE`](LICENSE).

---

## Overview

`antigravity-spaces` packages the `agy-server` reverse proxy and bundle rewriter to run inside rootless Hugging Face Docker Spaces (`UID 1000`, port `7860`) without external tunnels or remote-control relays:

1. **Official Local Jetski WebUI**: Launches Google's standalone `language_server` (`2.26.0-6546645232517120`) on loopback (`127.0.0.1`) and proxies its embedded React 19 / TanStack Router / Lexical / ConnectRPC web application behind an authenticated reverse proxy (`/__agy/login`).
2. **Self-Hosted Vendor Assets**: Rewrites references to Google-hosted KaTeX fonts and Material/Product icons in the web bundle to locally embedded `/__agy/vendor/...` assets and strips external font preconnects.
3. **Relay-Free Execution**: Enforces `"remoteControlEnabled": false` in both `~/.gemini/settings.json` and `~/.gemini/config/config.json` so `language_server` operates purely locally inside the container.
4. **Transactional Bucket Persistence (`agy-persist`)**: Protects SQLite databases, transcripts, project definitions, OAuth tokens, and server credentials across Hugging Face Space lifecycle events (Stop/Start, Pause/Resume, Sleep/Wake, Soft Restart, and Factory Rebuild).

---

## Architecture

```
HTTPS Request (Hugging Face Router :7860)
  │
  ▼
agy-server (Go reverse proxy & bundle rewriter)
  ├── /__agy/login    Argon2id password gate & HMAC-SHA256 session cookie
  ├── /__agy/signin   Headless Google OAuth flow (xdg-open shim -> Login RPC)
  ├── /__agy/upload   Multipart file upload into workspace
  ├── /__agy/rules    Global ~/.gemini/GEMINI.md editor
  └── /*              Pre-patched Jetski WebUI bundle & ConnectRPC proxy
                        │
                        ▼
              language_server (127.0.0.1:<random_port>, --app_data_dir antigravity-cli)
                        │
                        ├── Active POSIX runtime state (~/.gemini/**, ~/.agy-remote/**)
                        │     │
                        │     ▼  SQLite Online Backup API + atomic fsync/rename (every 2s & on SIGTERM)
                        └── Persistent Storage Backend (/buckets/private/${WORKER_ID} or /data/${WORKER_ID})
```

### Repository Structure

- [`cmd/agy-server/`](cmd/agy-server/) — Server CLI entrypoint (`serve`, `passwd`, `doctor`) and child `language_server` lifecycle management.
- [`agy-persist.py`](agy-persist.py) — Transactional persistence, SQLite Online Backup snapshotting, and startup self-healing engine (`/usr/local/bin/agy-persist`).
- [`agy_persist_test.py`](agy_persist_test.py) — Automated unit and regression test suite for `agy-persist.py`.
- [`entrypoint.sh`](entrypoint.sh) — Non-root container init (`UID 1000`), mount verification, state restore, periodic sync loop, and graceful `SIGTERM` shutdown sequence.
- [`Dockerfile`](Dockerfile) — Multi-stage build compiling `agy-server` (`golang:1.25-bookworm`) and assembling the runtime image (`debian:bookworm-slim`) with SHA-verified `agy` (`1.4.7`) and `language_server` (`2.26.0`).
- [`internal/lsproc/`](internal/lsproc/) — `language_server` discovery/launch, CSRF token management, OAuth token path synchronization, and `DisableRemoteControl`.
- [`internal/patches/`](internal/patches/) — Deterministic bundle rewriter (`18` mobile/touch UX rules + `5` local vendor asset rules).
- [`internal/auth/`](internal/auth/) — Password authentication (`Argon2id`), session store, rate limiting, and trusted proxy handling.
- [`internal/signin/`](internal/signin/) — Headless OAuth sign-in coordinator.
- [`internal/proxy/`](internal/proxy/), [`internal/upload/`](internal/upload/), [`internal/rules/`](internal/rules/), [`internal/ui/`](internal/ui/) — Reverse proxy, WebSocket relay, file uploader, rules endpoint, and HTML templates.
- [`docs/REA_ANALYSIS_AND_PARITY.md`](docs/REA_ANALYSIS_AND_PARITY.md) — Architecture comparison and ConnectRPC parity notes.

---

## Deploying on Hugging Face Docker Spaces

### 1. Create a Private Storage Bucket (Recommended)

Create a private Hugging Face Storage Bucket (e.g., `<owner>/<bucket-name>`) and attach it in the Space's `README.md` YAML frontmatter:

```yaml
---
title: AGY Worker 001
emoji: 🪐
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
volumes:
  - source: hf://buckets/<owner>/<bucket-name>
    target: /buckets/private
---
```

### 2. Configure Space Variables and Secrets

In the Space **Settings → Variables and secrets**:

| Type | Name | Required | Description |
| :--- | :--- | :--- | :--- |
| **Secret** | `AGY_PASSWORD` (or `PASSWORD`) | **Yes** (on first boot) | Password for unlocking `/__agy/login`. Once initialized, the Argon2id verifier is persisted in `agy-server/credentials.json`. |
| **Variable** | `WORKER_ID` | Recommended | Isolated namespace key under the storage mount (`/buckets/private/${WORKER_ID}`). Defaults to the Space slug (`${SPACE_ID##*/}`) or `agy-worker-local`. |
| **Variable** | `AGY_BUCKET_MOUNT` | Optional | Mount path for the HF Storage Bucket (default: `/buckets/private`). |
| **Variable** | `AGY_REQUIRE_BUCKET` | Optional | Set to `1` (default when `SPACE_ID` is set or `/buckets/private` exists) to fail closed if persistent storage is unavailable.Set to `0` only for intentionally ephemeral containers. |

---

## Bucket Persistence & Recovery Engineering (`agy-persist`)

### Why Direct SQLite Execution on FUSE Fails

Hugging Face Storage Buckets are mounted into Spaces via `hf-mount` (FUSE) backed by Xet object storage:
- `hf-mount` buffers writes in memory and commits objects upstream on file `fsync()` / `release()` (`close()`).
- Running SQLite databases (`conversation_summaries.db` and `conversations/<cid>.db`) in `WAL` mode directly over a FUSE symlink leaves long-lived open file handles without POSIX byte-range lock guarantees or continuous WAL frame flushing. Across an ungraceful container stop or Factory Rebuild, the `.db` header page count and `.db-wal` frames can desynchronize, resulting in `sqlite3.DatabaseError: database disk image is malformed`.
- Furthermore, if `language_server` encounters a malformed `.db` or a missing `agyhub_summaries_proto.pb` during startup reconciliation, it prunes the conversation from `conversation_summaries.db`.
- Finally, object stores do not persist empty directories (such as newly created empty project folders or `.git/objects/info` and `.git/refs/heads`) unless at least one object exists inside them.

### How `agy-persist` Protects Durable State

1. **Local POSIX SQLite Execution + Online Backup Snapshots**:
   - Active SQLite databases (`conversation_summaries.db` and `conversations/*.db`) and `brain/` directories always reside on the container's local POSIX filesystem (`~/.gemini/...`), never on FUSE symlinks.
   - `agy-persist sync` snapshots dirty SQLite databases to `/buckets/private/${WORKER_ID}/...` using SQLite's Online Backup API (`sqlite3.Connection.backup`), consolidating committed WAL frames into self-contained, checkpointed database files verified with `PRAGMA integrity_check`.
2. **Atomic Write-Temp → `fsync` → `close` → `os.replace`**:
   - Every file synced to persistent storage is written to a sibling temporary file, flushed with `os.fsync()`, closed (triggering `hf-mount` object upload), and atomically renamed into place.
3. **Rolling Known-Good Backups (`backups/prev/`)**:
   - Before overwriting any existing valid database or JSON file in persistent storage, `agy-persist` copies the previous verified version to `/buckets/private/${WORKER_ID}/backups/prev/...`.
   - On startup restore, if a primary file in the bucket is corrupt or truncated, `agy-persist restore` automatically falls back to `backups/prev/`.
4. **Startup Self-Healing & Transcript Reconciliation**:
   - Before `language_server` starts, `agy-persist restore`:
     - Repairs any damaged `conversations/<cid>.db` via `sqlite3 .recover`.
     - Backfills any uncheckpointed conversation steps from `brain/<cid>/.system_generated/logs/transcript_full.jsonl`.
     - Reconciles `conversation_summaries.db`, `conversations_fts` (FTS5 search index), `agyhub_summaries_proto.pb`, `jetbox_summaries_proto.pb`, `history.jsonl`, and `~/.gemini/config/projects/<project_id>.json`.
5. **Anti-Prune & Non-Regression Guards**:
   - Refuses to overwrite non-empty bucket files with `0`-byte files or invalid JSON.
   - Refuses to overwrite a bucket conversation `.db` with a local `.db` that has fewer steps.
   - Restores any summary row pruned from local `conversation_summaries.db` if its `.db` or `brain/<cid>` directory still exists (`preserve_unpruned_summaries`).
6. **Empty-Directory Sentinel Protection (`.agy-keep`)**:
   - Automatically maintains `.agy-keep` sentinel files inside `workspace/.git/{objects/info,objects/pack,refs/heads,refs/tags}` and empty workspace project directories so object storage remounts across Factory Rebuilds preserve directory structure and Git repository validity.
7. **Graceful `language_server` Shutdown**:
   - On `SIGTERM` / `SIGINT`, both `cmd/agy-server/run.go` and `entrypoint.sh` send `SIGTERM` to `language_server`, wait up to 5 seconds for clean SQLite/protobuf flush, and execute `agy-persist sync --final`.

### Durable State Inventory

| Runtime Path | Persistent Path (`/buckets/private/${WORKER_ID}/...`) | Description |
| :--- | :--- | :--- |
| `~/.agy-remote/credentials.json` | `agy-server/credentials.json` | Argon2id password verifier and salt (`0600`) |
| `~/.agy-remote/sessions.json`, `config.json`, `csrf-token.txt` | `agy-server/{sessions.json,config.json,csrf-token.txt}` | Active WebUI sessions, server config, and CSRF token |
| `~/.config/antigravity/**` | `config-antigravity/**` | File-based `plugin-secrets.json` store |
| `~/.gemini/config/**` | `gemini/config/**` | Project definitions (`projects/*.json`), `config.json` (`remoteControlEnabled: false`), `.migrated`, `google_accounts.json` |
| `~/.gemini/{,antigravity-cli/,antigravity/}installation_id` | `gemini/{root,antigravity-cli,antigravity}/installation_id` | Per-worker installation UUID |
| `~/.gemini/{,antigravity-cli/,antigravity/}*oauth-token*` | `gemini/{root,antigravity-cli,antigravity}/*oauth-token*` | Synchronized Google OAuth and Jetski standalone tokens (`0600`) |
| `~/.gemini/{antigravity-cli,antigravity}/conversations/*.db` | `gemini/{antigravity-cli,antigravity}/conversations/*.db` | Checkpointed per-conversation SQLite trajectory databases |
| `~/.gemini/{,antigravity-cli/,antigravity/}conversation_summaries.db` | `gemini/{root,antigravity-cli,antigravity}/conversation_summaries.db` | Conversation summary table and `conversations_fts` FTS5 search index |
| `~/.gemini/{antigravity-cli,antigravity}/{agyhub,jetbox}_summaries_proto.pb` | `gemini/{antigravity-cli,antigravity}/{agyhub,jetbox}_summaries_proto.pb` | Protobuf summary maps used by startup reconciliation and `JetboxSubscribeToSummaries` |
| `~/.gemini/{antigravity-cli,antigravity}/brain/<cid>/**` | `gemini/{antigravity-cli,antigravity}/brain/<cid>/**` | `transcript.jsonl`, `transcript_full.jsonl`, artifacts, and step logs |
| `~/.gemini/{antigravity-cli,antigravity}/{annotations,implicit,history.jsonl,*.pbtxt,GEMINI.md}` | `gemini/{antigravity-cli,antigravity}/...` | Annotations, implicit context, CLI `/resume` history, state protos, and global rules |
| `~/workspace/**` (`AGY_WORKSPACE`) | `workspace/**` | User projects, `.git/`, `.agy/rules.json`, and uploads |

---

## Authentication

1. **WebUI Password Gate (`/__agy/login`)**:
   - Every HTTP and WebSocket route (except static login assets) requires a valid HMAC-SHA256 session cookie obtained by authenticating at `/__agy/login`.
   - On first startup, `AGY_PASSWORD` (or `PASSWORD`) initializes `~/.agy-remote/credentials.json` using `Argon2id`. If neither `AGY_PASSWORD` nor a persisted `credentials.json` is present, startup fails closed (`WORKER_INIT_FAIL`).
2. **Headless Google OAuth (`/__agy/signin`)**:
   - When `language_server` is not yet signed into a Google account, `/` redirects to `/__agy/signin`.
   - Clicking **Start Sign-In** invokes `LanguageServerService/Login`, captures the `accounts.google.com` OAuth URL via an `xdg-open` shim, and lets the user paste the resulting `http://localhost:<port>/oauth-callback?...` redirect URL to complete token exchange locally inside the container.
   - OAuth tokens are never shared across workers.

---

## Guarantees, Limitations, and RPO Boundaries

- **Graceful Lifecycle Events (`RPO = 0` for completed steps)**:
  - Across Stop/Start, Pause/Resume, Sleep/Wake, Soft Restart, Factory Rebuild, Repository Commit Rebuild, and Secret/Variable updates where the platform delivers `SIGTERM` before container teardown, `entrypoint.sh` stops `language_server` gracefully and runs `agy-persist sync --final`.
- **Abrupt Unsignaled Termination (`SIGKILL` / OOM / Sudden Node Loss)**:
  - Active SQLite databases in the bucket are never corrupted by `SIGKILL` because they are updated exclusively via atomic SQLite Online Backups and `os.replace()`.
  - Because background sync runs every **2 seconds** and `hf-mount` flushes closed files asynchronously to Xet object storage, an abrupt `SIGKILL` without prior `SIGTERM` has a theoretical recovery-point window of **up to ~2 seconds of local writes** plus any unacknowledged `hf-mount` upload buffer.
  - If a step was written to `transcript_full.jsonl` (synced before `.db` files) prior to `SIGKILL` before the SQLite WAL checkpointed, `agy-persist restore` automatically backfills that step into `<cid>.db` on the next boot. Any in-flight token stream held purely in `language_server` memory at the instant of `SIGKILL` cannot be recovered.
- **Bucket Storage Scope**:
  - Hugging Face Storage Buckets are single-region, unversioned object stores. While `${PERSIST_ROOT}/backups/prev/` protects against single-file corruption or truncation, external deletion of the bucket itself requires off-bucket backups (`hf buckets sync`).

---

## Local Development & Testing

```bash
# Build agy-server
go build ./cmd/agy-server

# Run Go unit and patch regression tests
go test ./...

# Validate shell and Python syntax
bash -n entrypoint.sh
python3 -m py_compile agy-persist.py

# Run persistence & recovery unit tests
python3 -m unittest -v agy_persist_test.py
```

---

## License & Attribution

Licensed under the [Apache License, Version 2.0](LICENSE). Derived from [`AFSlayer/antigravity-server`](https://github.com/AFSlayer/antigravity-server); see [`NOTICE`](NOTICE) and [`DISCLAIMER.md`](DISCLAIMER.md) for full provenance and third-party asset attributions.
