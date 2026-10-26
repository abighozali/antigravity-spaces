#!/usr/bin/env bash
set -euo pipefail

fail() {
  echo "WORKER_INIT_FAIL: $1" >&2
  exit 1
}

# 1. Enforce non-root execution
if [ "$(id -u)" -eq 0 ]; then
  fail "container must run as non-root user (expected UID 1000)"
fi

export HOME="${HOME:-/home/worker}"
export PORT="${PORT:-7860}"
export AGY_LS_DIR="${AGY_LS_DIR:-/opt/agy-hub}"
export AGY_HOME="${AGY_HOME:-${HOME}/.agy-remote}"
AGY_PERSIST_BIN="${AGY_PERSIST_BIN:-/usr/local/bin/agy-persist}"

# 2. Resolve isolated per-Space worker identity
if [ -n "${WORKER_ID:-}" ]; then
  RESOLVED_WORKER_ID="${WORKER_ID}"
elif [ -n "${SPACE_ID:-}" ]; then
  RESOLVED_WORKER_ID="${SPACE_ID##*/}"
else
  RESOLVED_WORKER_ID="agy-worker-local"
fi
export WORKER_ID="${RESOLVED_WORKER_ID}"

# 3. Verify persistent mount availability and fail-closed policy
if ! "${AGY_PERSIST_BIN}" check-mount; then
  fail "persistent storage mount check failed for worker_id=${WORKER_ID}"
fi

# 4. Restore and self-heal all durable state (credentials, OAuth tokens, installation_id,
#    SQLite databases, transcripts, summaries, proto indices, projects, settings, workspace)
if ! "${AGY_PERSIST_BIN}" restore; then
  fail "durable state restore failed for worker_id=${WORKER_ID}"
fi

STATUS_JSON="$("${AGY_PERSIST_BIN}" status)"
INSTALLATION_ID="$(printf '%s' "${STATUS_JSON}" | jq -r '.installation_id')"
STORAGE_MODE="$(printf '%s' "${STATUS_JSON}" | jq -r '.storage_mode')"
PERSIST_ROOT="$(printf '%s' "${STATUS_JSON}" | jq -r '.persist_root')"
export AGY_WORKSPACE="$(printf '%s' "${STATUS_JSON}" | jq -r '.workspace_dir')"

cd "${AGY_WORKSPACE}"

# 5. Map Hugging Face Space secrets (PASSWORD or WEBUI_PASSWORD) to AGY_PASSWORD if not already set
if [ -z "${AGY_PASSWORD:-}" ]; then
  if [ -n "${PASSWORD:-}" ]; then
    export AGY_PASSWORD="${PASSWORD}"
  elif [ -n "${WEBUI_PASSWORD:-}" ]; then
    export AGY_PASSWORD="${WEBUI_PASSWORD}"
  fi
fi

# Fail closed if neither an explicit password secret nor a persisted credentials.json verifier is present
if [ -z "${AGY_PASSWORD:-}" ] && [ ! -s "${AGY_HOME}/credentials.json" ]; then
  fail "PASSWORD or AGY_PASSWORD secret (or persisted credentials.json) is required via Hugging Face Space Secrets"
fi

# 6. Write worker identity metadata in workspace
AGY_VER="$(agy --version 2>/dev/null || echo 'unknown')"
cat > "${AGY_WORKSPACE}/.agy-worker-identity.json.tmp" <<EOF
{
  "worker_id": "${WORKER_ID}",
  "space_id": "${SPACE_ID:-local}",
  "installation_id": "${INSTALLATION_ID}",
  "storage_mode": "${STORAGE_MODE}",
  "agy_version": "${AGY_VER}",
  "uid": $(id -u),
  "gid": $(id -g)
}
EOF
mv -f "${AGY_WORKSPACE}/.agy-worker-identity.json.tmp" "${AGY_WORKSPACE}/.agy-worker-identity.json"

# 7. Perform initial transactional sync to persist any healed/reconciled state to bucket
"${AGY_PERSIST_BIN}" sync || true

# 8. Background sync daemon (every 2 seconds via SQLite Online Backup API & atomic fsync+rename)
(
  while true; do
    sleep 2
    "${AGY_PERSIST_BIN}" sync || true
  done
) &
SYNC_PID=$!

echo "AGY_WORKER_INIT_OK: worker_id=${WORKER_ID} installation_id=${INSTALLATION_ID} uid=$(id -u) storage=${STORAGE_MODE}"
echo "[entrypoint] Antigravity CLI version: ${AGY_VER}"
echo "[entrypoint] Starting self-hosted Antigravity Jetski WebUI server on 0.0.0.0:${PORT}"

/usr/local/bin/agy-server serve \
  --bind 0.0.0.0 \
  --port "${PORT}" \
  --language-server "${AGY_LS_DIR}/language_server" \
  --workspace-root "${AGY_WORKSPACE}" \
  --trusted-proxies "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.0/8" &
SERVER_PID=$!

graceful_stop_language_server() {
  if pkill -TERM -f "${AGY_LS_DIR}/language_server" 2>/dev/null; then
    for _ in $(seq 1 50); do
      if ! pgrep -f "${AGY_LS_DIR}/language_server" >/dev/null 2>&1; then
        break
      fi
      sleep 0.1
    done
  fi
}

SHUTTING_DOWN=0
cleanup() {
  if [ "${SHUTTING_DOWN}" -eq 1 ]; then
    return
  fi
  SHUTTING_DOWN=1
  kill -TERM "${SYNC_PID}" 2>/dev/null || true
  wait "${SYNC_PID}" 2>/dev/null || true
  kill -TERM "${SERVER_PID}" 2>/dev/null || true
  wait "${SERVER_PID}" 2>/dev/null || true
  graceful_stop_language_server
  "${AGY_PERSIST_BIN}" sync --final || true
}
trap cleanup SIGTERM SIGINT

wait "${SERVER_PID}" || true
EXIT_CODE=$?
cleanup
exit "${EXIT_CODE}"
