# Contributing

Contributions that improve reliability, persistence safety, or compatibility with official Antigravity releases are welcome.

## Prerequisites

- **Go**: `1.25+`
- **Python**: `3.10+`
- **SQLite CLI**: `sqlite3` (with `.recover` support)
- **Git**: `2.30+`

## Verification Checklist

Before opening a pull request, run the full build and test suite locally:

```bash
# 1. Go static analysis, build, and tests
go vet ./...
go build ./cmd/agy-server
go test ./...

# 2. Entrypoint and persistence engine syntax checks
bash -n entrypoint.sh
python3 -m py_compile agy-persist.py

# 3. Persistence & self-healing regression tests
python3 -m unittest -v agy_persist_test.py
```

## Guidelines

1. **No Secrets or Live State**: Never commit `.env` files, `credentials.json`, `*oauth-token*`, SQLite `.db` files, `.jsonl` transcripts, or worker-specific URLs/identifiers.
2. **Persistence Safety**: Any changes to `agy-persist.py` or `entrypoint.sh` must preserve atomic write-temp -> `fsync` -> `os.replace` semantics, fail-closed mount checks, and non-destructive recovery guards, with corresponding test coverage in `agy_persist_test.py`.
3. **Zero External CDN Dependencies**: Do not introduce external font, script, or icon CDN requests (`*.googleapis.com`, `*.gstatic.com`) into UI templates or bundle patches.
4. **Apache-2.0 Attribution Hygiene**: Preserve `LICENSE` and `DISCLAIMER.md`, and update `NOTICE` when adding or modifying files relative to upstream [`AFSlayer/antigravity-server`](https://github.com/AFSlayer/antigravity-server).
