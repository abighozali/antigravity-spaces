## Summary

<!-- Concise description of the change and its motivation. -->

## Verification

- [ ] `go vet ./... && go build ./cmd/agy-server && go test ./...` pass
- [ ] `bash -n entrypoint.sh && python3 -m py_compile agy-persist.py` pass
- [ ] `python3 -m unittest -v agy_persist_test.py` passes
- [ ] No credentials, tokens, SQLite databases, or worker-specific state included
- [ ] `NOTICE` updated if files were added or modified relative to upstream
