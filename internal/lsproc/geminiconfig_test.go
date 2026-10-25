package lsproc

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"
	"time"
)

// TestStartupSyncAuthTokensPathResolution reproduces and verifies the fix for the startup
// SyncAuthTokens bug where passing config.GeminiDir() (~/.gemini) previously resolved to
// ~/antigravity-cli/... instead of ~/.gemini/antigravity-cli/...
func TestStartupSyncAuthTokensPathResolution(t *testing.T) {
	tmpDir := t.TempDir()
	home := filepath.Join(tmpDir, "home")
	geminiDir := filepath.Join(home, ".gemini")

	cliTokenDir := filepath.Join(geminiDir, "antigravity-cli")
	if err := os.MkdirAll(cliTokenDir, 0o700); err != nil {
		t.Fatal(err)
	}
	cliTokenPath := filepath.Join(cliTokenDir, CLITokenFile)
	dummyToken := []byte("dummy-token-payload-xyz")
	if err := os.WriteFile(cliTokenPath, dummyToken, 0o600); err != nil {
		t.Fatal(err)
	}

	// Startup invocation passing ~/.gemini (as in cmd/agy-server/run.go)
	SyncAuthTokens(geminiDir)

	expectedDesktopTokenPath := filepath.Join(geminiDir, "antigravity", CLITokenFile)
	if data, err := os.ReadFile(expectedDesktopTokenPath); err != nil || string(data) != string(dummyToken) {
		t.Fatalf("startup SyncAuthTokens failed to synchronize CLI token to desktop path: %v", err)
	}

	expectedStandaloneTokenPath := filepath.Join(geminiDir, StandaloneTokenFile)
	if data, err := os.ReadFile(expectedStandaloneTokenPath); err != nil || string(data) != string(dummyToken) {
		t.Fatalf("startup SyncAuthTokens failed to synchronize CLI token to standalone path: %v", err)
	}

	if !HasStandaloneToken(geminiDir) {
		t.Fatal("expected HasStandaloneToken to return true after sync")
	}
}

// TestTokenCandidatePathsConsistency verifies that calling tokenCandidatePaths with
// ~/.gemini, ~/.gemini/antigravity, or ~/.gemini/antigravity-cli resolves consistent paths.
func TestTokenCandidatePathsConsistency(t *testing.T) {
	tmpDir := t.TempDir()
	home := filepath.Join(tmpDir, "home")
	geminiRoot := filepath.Join(home, ".gemini")
	geminiDesktop := filepath.Join(geminiRoot, "antigravity")
	geminiCLI := filepath.Join(geminiRoot, "antigravity-cli")

	pathsFromRoot := tokenCandidatePaths(geminiRoot)
	pathsFromDesktop := tokenCandidatePaths(geminiDesktop)
	pathsFromCLI := tokenCandidatePaths(geminiCLI)

	toSet := func(slice []string) map[string]bool {
		m := make(map[string]bool, len(slice))
		for _, s := range slice {
			m[s] = true
		}
		return m
	}

	setRoot := toSet(pathsFromRoot)
	setDesktop := toSet(pathsFromDesktop)
	setCLI := toSet(pathsFromCLI)

	cliTokenExpected := filepath.Join(geminiRoot, "antigravity-cli", CLITokenFile)
	desktopTokenExpected := filepath.Join(geminiRoot, "antigravity", CLITokenFile)
	standaloneTokenExpected := filepath.Join(geminiRoot, StandaloneTokenFile)

	for _, expected := range []string{cliTokenExpected, desktopTokenExpected, standaloneTokenExpected} {
		if !setRoot[expected] {
			t.Errorf("pathsFromRoot missing expected candidate %s", expected)
		}
		if !setDesktop[expected] {
			t.Errorf("pathsFromDesktop missing expected candidate %s", expected)
		}
		if !setCLI[expected] {
			t.Errorf("pathsFromCLI missing expected candidate %s", expected)
		}
	}

	if !reflect.DeepEqual(setRoot, setDesktop) {
		t.Errorf("expected setRoot and setDesktop to match, got root=%v desktop=%v", setRoot, setDesktop)
	}
	if !reflect.DeepEqual(setRoot, setCLI) {
		t.Errorf("expected setRoot and setCLI to match, got root=%v cli=%v", setRoot, setCLI)
	}
}

// TestTokenSync_AlternateLocations verifies token discovery when tokens are placed in alternate paths.
func TestTokenSync_AlternateLocations(t *testing.T) {
	locations := []string{
		"antigravity/" + CLITokenFile,
		StandaloneTokenFile,
		CLITokenFile,
		"antigravity/" + StandaloneTokenFile,
	}

	for _, loc := range locations {
		t.Run(loc, func(t *testing.T) {
			tmpDir := t.TempDir()
			geminiDir := filepath.Join(tmpDir, "home", ".gemini")
			target := filepath.Join(geminiDir, loc)
			if err := os.MkdirAll(filepath.Dir(target), 0o700); err != nil {
				t.Fatal(err)
			}
			tokenData := []byte("dummy-token-" + loc)
			if err := os.WriteFile(target, tokenData, 0o600); err != nil {
				t.Fatal(err)
			}

			SyncAuthTokens(geminiDir)

			cliDest := filepath.Join(geminiDir, "antigravity-cli", CLITokenFile)
			if data, err := os.ReadFile(cliDest); err != nil || string(data) != string(tokenData) {
				t.Fatalf("failed to sync token from %s to CLI path: %v", loc, err)
			}
		})
	}
}

// TestTokenSync_MissingTokens ensures that when no token files exist, SyncAuthTokens and
// HasStandaloneToken handle it cleanly without panic or errors.
func TestTokenSync_MissingTokens(t *testing.T) {
	tmpDir := t.TempDir()
	geminiDir := filepath.Join(tmpDir, "home", ".gemini")
	if err := os.MkdirAll(geminiDir, 0o700); err != nil {
		t.Fatal(err)
	}

	if HasStandaloneToken(geminiDir) {
		t.Fatal("expected HasStandaloneToken to report false when empty")
	}

	// Must not panic or create empty token files
	SyncAuthTokens(geminiDir)

	if HasStandaloneToken(geminiDir) {
		t.Fatal("expected HasStandaloneToken to still report false after sync on empty directory")
	}
}

// TestTokenSync_RepeatedAndIdempotent verifies that repeated syncs preserve contents and do not rewrite unchanged files.
func TestTokenSync_RepeatedAndIdempotent(t *testing.T) {
	tmpDir := t.TempDir()
	geminiDir := filepath.Join(tmpDir, "home", ".gemini")
	src := filepath.Join(geminiDir, "antigravity-cli", CLITokenFile)
	if err := os.MkdirAll(filepath.Dir(src), 0o700); err != nil {
		t.Fatal(err)
	}
	tokenData := []byte("dummy-token-idempotent")
	if err := os.WriteFile(src, tokenData, 0o600); err != nil {
		t.Fatal(err)
	}

	SyncAuthTokens(geminiDir)

	dst := filepath.Join(geminiDir, "antigravity", CLITokenFile)
	info1, err := os.Stat(dst)
	if err != nil {
		t.Fatal(err)
	}

	time.Sleep(10 * time.Millisecond)

	// Second sync should detect matching content and skip rewriting
	SyncAuthTokens(geminiDir)

	info2, err := os.Stat(dst)
	if err != nil {
		t.Fatal(err)
	}

	if info1.ModTime() != info2.ModTime() {
		t.Errorf("expected idempotent sync to preserve mod time, got %v vs %v", info1.ModTime(), info2.ModTime())
	}
}

// TestTokenSync_NewerModTimeWins verifies that updated/refreshed tokens overwrite older ones.
func TestTokenSync_NewerModTimeWins(t *testing.T) {
	tmpDir := t.TempDir()
	geminiDir := filepath.Join(tmpDir, "home", ".gemini")

	cliPath := filepath.Join(geminiDir, "antigravity-cli", CLITokenFile)
	desktopPath := filepath.Join(geminiDir, "antigravity", CLITokenFile)

	_ = os.MkdirAll(filepath.Dir(cliPath), 0o700)
	_ = os.MkdirAll(filepath.Dir(desktopPath), 0o700)

	oldToken := []byte("old-dummy-token")
	newToken := []byte("refreshed-newer-dummy-token")

	_ = os.WriteFile(desktopPath, oldToken, 0o600)
	time.Sleep(20 * time.Millisecond)
	_ = os.WriteFile(cliPath, newToken, 0o600)

	SyncAuthTokens(geminiDir)

	data, err := os.ReadFile(desktopPath)
	if err != nil {
		t.Fatal(err)
	}
	if string(data) != string(newToken) {
		t.Fatalf("expected newer token %q to overwrite older token, got %q", newToken, data)
	}
}

// TestTokenSync_RestrictivePermissions verifies that directories are created with 0700
// and token files are created with 0600.
func TestTokenSync_RestrictivePermissions(t *testing.T) {
	tmpDir := t.TempDir()
	geminiDir := filepath.Join(tmpDir, "home", ".gemini")
	src := filepath.Join(geminiDir, "antigravity-cli", CLITokenFile)
	_ = os.MkdirAll(filepath.Dir(src), 0o700)
	_ = os.WriteFile(src, []byte("dummy-token-perms"), 0o600)

	SyncAuthTokens(geminiDir)

	dst := filepath.Join(geminiDir, "antigravity", CLITokenFile)
	fi, err := os.Stat(dst)
	if err != nil {
		t.Fatal(err)
	}
	perm := fi.Mode().Perm()
	if perm != 0o600 {
		t.Errorf("expected file permissions 0600, got %o", perm)
	}

	dirFi, err := os.Stat(filepath.Dir(dst))
	if err != nil {
		t.Fatal(err)
	}
	dirPerm := dirFi.Mode().Perm()
	if dirPerm != 0o700 {
		t.Errorf("expected directory permissions 0700, got %o", dirPerm)
	}
}
