#!/usr/bin/env bash
set -euo pipefail

export HOME="${HOME:-/home/worker}"
export AGY_WORKSPACE="${AGY_WORKSPACE:-${HOME}/workspace}"
export PORT="${PORT:-7860}"
export AGY_LS_DIR="${AGY_LS_DIR:-/opt/agy-hub}"

# Map Hugging Face Space secrets (PASSWORD or WEBUI_PASSWORD) to AGY_PASSWORD if not already set
if [ -z "${AGY_PASSWORD:-}" ]; then
  if [ -n "${PASSWORD:-}" ]; then
    export AGY_PASSWORD="${PASSWORD}"
  elif [ -n "${WEBUI_PASSWORD:-}" ]; then
    export AGY_PASSWORD="${WEBUI_PASSWORD}"
  fi
fi

mkdir -p \
  "${AGY_WORKSPACE}" \
  "${HOME}/.local/share/agy-server" \
  "${HOME}/.gemini/antigravity/config" \
  "${HOME}/.gemini/antigravity-cli/config" \
  "${HOME}/.config/antigravity"

chmod 700 "${HOME}/.local/share/agy-server" "${HOME}/.gemini" "${HOME}/.gemini/antigravity" "${HOME}/.gemini/antigravity-cli" || true

# Ensure Remote Control relay is explicitly disabled
printf '{\n  "remoteControlEnabled": false\n}\n' > "${HOME}/.gemini/settings.json"

# Initialize default git workspace if empty so worktree/project features work cleanly
if [ ! -d "${AGY_WORKSPACE}/.git" ]; then
  git -C "${AGY_WORKSPACE}" init -b main >/dev/null 2>&1 || true
fi

echo "[entrypoint] Antigravity CLI version: $(agy --version 2>/dev/null || echo 'unknown')"
echo "[entrypoint] Starting self-hosted Antigravity Jetski WebUI server on 0.0.0.0:${PORT}"

exec /usr/local/bin/agy-server serve \
  --bind 0.0.0.0 \
  --port "${PORT}" \
  --language-server "${AGY_LS_DIR}/language_server" \
  --workspace-root "${AGY_WORKSPACE}" \
  --trusted-proxies "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.0/8"
