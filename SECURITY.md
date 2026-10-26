# Security Policy

## Deployment Threat Model

`antigravity-spaces` exposes the local Antigravity Jetski WebUI and an interactive coding agent (`language_server` + `agy` CLI) over HTTPS. An authenticated user has full read/write and command-execution access inside the container as the `worker` user (`UID 1000`).

Before deploying an instance on Hugging Face Spaces or any internet-reachable host:

1. **Space Visibility & Access Control**:
   - Keep both your Hugging Face Space and your attached Hugging Face Storage Bucket **Private** (`private: true`).
2. **Strong Password Authentication (`AGY_PASSWORD`)**:
   - Always configure a high-entropy `AGY_PASSWORD` (or `PASSWORD`) via Hugging Face **Space Secrets** (never in plain-text Space Variables, `Dockerfile`, or `README.md`).
   - `entrypoint.sh` fails closed (`WORKER_INIT_FAIL`) if neither `AGY_PASSWORD` nor a persisted Argon2id verifier (`credentials.json`) is present.
3. **Isolated Per-Worker Credentials**:
   - Never bake `credentials.json`, `sessions.json`, or `*oauth-token*` files into Docker images or Git repositories.
   - Never copy OAuth tokens or `installation_id` files across separate worker Spaces.
   - `agy-persist` enforces `0600` file permissions on all credentials, session stores, and OAuth tokens.
4. **Disabled Remote Control Relay**:
   - Both `agy-server` (`internal/lsproc/geminiconfig.go`) and `agy-persist` (`enforce_remote_control_disabled`) explicitly set `"remoteControlEnabled": false` in `~/.gemini/settings.json` and `~/.gemini/config/config.json`.

## Reporting a Vulnerability

- For vulnerabilities specific to the Hugging Face Spaces adaptation, `entrypoint.sh`, or `agy-persist.py` in this repository, please open a [GitHub Security Advisory](https://github.com/abighozali/antigravity-spaces/security/advisories/new) rather than a public issue.
- For vulnerabilities in the upstream `agy-server` proxy or authentication packages, please also report upstream to [`AFSlayer/antigravity-server`](https://github.com/AFSlayer/antigravity-server/security).
