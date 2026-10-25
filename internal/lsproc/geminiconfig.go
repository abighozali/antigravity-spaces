package lsproc

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
)

const StandaloneTokenFile = "jetski-standalone-oauth-token"
const CLITokenFile = "antigravity-oauth-token"

// TokenPath is where the standalone OAuth token lives.
func TokenPath(geminiDir string) string {
	return filepath.Join(geminiDir, StandaloneTokenFile)
}

// HasStandaloneToken reports whether this machine has signed in to Antigravity.
func HasStandaloneToken(geminiDir string) bool {
	for _, p := range tokenCandidatePaths(geminiDir) {
		if info, err := os.Stat(p); err == nil && info.Size() > 0 {
			return true
		}
	}
	return false
}

func tokenCandidatePaths(geminiDir string) []string {
	clean := filepath.Clean(geminiDir)
	base := filepath.Base(clean)
	var baseGemini string
	if base == "antigravity" || base == "antigravity-cli" {
		baseGemini = filepath.Dir(clean)
	} else {
		baseGemini = clean
	}
	userHome := filepath.Dir(baseGemini)

	raw := []string{
		filepath.Join(baseGemini, "antigravity-cli", CLITokenFile),
		filepath.Join(baseGemini, "antigravity", CLITokenFile),
		filepath.Join(baseGemini, StandaloneTokenFile),
		filepath.Join(baseGemini, CLITokenFile),
		filepath.Join(baseGemini, "antigravity", StandaloneTokenFile),
		filepath.Join(baseGemini, "antigravity-cli", StandaloneTokenFile),
		filepath.Join(clean, StandaloneTokenFile),
		filepath.Join(clean, CLITokenFile),
	}
	if userHome != "" && userHome != "." && userHome != "/" {
		raw = append(raw, filepath.Join(userHome, StandaloneTokenFile))
	}

	seen := make(map[string]bool, len(raw))
	var out []string
	for _, p := range raw {
		cp := filepath.Clean(p)
		if !seen[cp] {
			seen[cp] = true
			out = append(out, cp)
		}
	}
	return out
}

// SyncAuthTokens ensures that a valid official Google OAuth token created by
// either `agy` CLI (~/.gemini/antigravity-cli/antigravity-oauth-token) or
// `language_server` (~/.gemini/antigravity/antigravity-oauth-token or
// ~/.gemini/jetski-standalone-oauth-token) is synchronized across all official
// paths with 0600 permissions so both the CLI runtime and Jetski WebUI share
// one login.
func SyncAuthTokens(geminiDir string) {
	candidates := tokenCandidatePaths(geminiDir)
	var srcPath string
	var srcData []byte
	var newestMod int64

	for _, p := range candidates {
		info, err := os.Stat(p)
		if err != nil || info.Size() == 0 {
			continue
		}
		data, err := os.ReadFile(p)
		if err != nil || len(data) == 0 {
			continue
		}
		if m := info.ModTime().UnixNano(); m > newestMod {
			newestMod = m
			srcPath = p
			srcData = data
		}
	}
	if len(srcData) == 0 {
		return
	}

	for _, dst := range candidates {
		if dst == srcPath {
			continue
		}
		_ = os.MkdirAll(filepath.Dir(dst), 0o700)
		existing, err := os.ReadFile(dst)
		if err == nil && string(existing) == string(srcData) {
			continue
		}
		_ = os.WriteFile(dst, srcData, 0o600)
	}
}

var ErrConfigMissing = errors.New("antigravity settings file does not exist yet")

// DisableRemoteControl ensures remoteControlEnabled is false in Antigravity's
// settings so no external relay or tunnel is ever activated automatically.
func DisableRemoteControl(geminiDir string) (changed bool, err error) {
	path := filepath.Join(geminiDir, "config", "config.json")

	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return false, ErrConfigMissing
		}
		return false, err
	}

	var cfg map[string]any
	if err := json.Unmarshal(data, &cfg); err != nil {
		return false, fmt.Errorf("parse %s: %w", path, err)
	}

	settings, ok := cfg["userSettings"].(map[string]any)
	if !ok {
		return false, nil
	}

	if enabled, ok := settings["remoteControlEnabled"].(bool); !ok || !enabled {
		return false, nil
	}

	settings["remoteControlEnabled"] = false
	out, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return false, err
	}
	if err := os.WriteFile(path, append(out, '\n'), 0o644); err != nil {
		return false, err
	}
	return true, nil
}
