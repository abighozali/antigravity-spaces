// Package assets serves the icons and local vendor fonts/SVGs so the WebUI has
// zero Google-hosted frontend dependencies (no gstatic.com or googleapis.com
// requests from the browser).
package assets

import (
	"embed"
	"net/http"
	"path"
	"strings"
	"time"
)

//go:embed files vendor
var files embed.FS

var modTime = time.Now()

type asset struct {
	path        string
	contentType string
}

var routes = map[string]asset{
	"/favicon.ico":                       {"files/favicon.ico", "image/x-icon"},
	"/apple-touch-icon.png":              {"files/apple-touch-icon.png", "image/png"},
	"/apple-touch-icon-precomposed.png":  {"files/apple-touch-icon.png", "image/png"},
	"/assets/image/antigravity-logo.png": {"files/logo.png", "image/png"},
}

const VendorPrefix = "/__agy/vendor/"

// IsPublicPath reports whether a path may be served without authentication.
func IsPublicPath(p string) bool {
	if _, ok := routes[p]; ok {
		return true
	}
	return strings.HasPrefix(p, VendorPrefix)
}

// Paths lists every route prefix this package serves, for mounting on a mux.
func Paths() []string {
	out := make([]string, 0, len(routes)+1)
	for p := range routes {
		out = append(out, p)
	}
	out = append(out, VendorPrefix)
	return out
}

func contentTypeForExt(name string) string {
	switch strings.ToLower(path.Ext(name)) {
	case ".woff2":
		return "font/woff2"
	case ".woff":
		return "font/woff"
	case ".ttf":
		return "font/ttf"
	case ".svg":
		return "image/svg+xml"
	case ".png":
		return "image/png"
	case ".css":
		return "text/css; charset=utf-8"
	case ".js":
		return "application/javascript; charset=utf-8"
	default:
		return "application/octet-stream"
	}
}

// Handler serves the embedded icons and local vendor assets.
func Handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasPrefix(r.URL.Path, VendorPrefix) {
			rel := strings.TrimPrefix(r.URL.Path, VendorPrefix)
			clean := path.Clean(rel)
			if strings.HasPrefix(clean, "..") || clean == "." || clean == "" {
				http.NotFound(w, r)
				return
			}
			embedPath := "vendor/" + clean
			data, err := files.ReadFile(embedPath)
			if err != nil && (strings.HasSuffix(embedPath, ".woff") || strings.HasSuffix(embedPath, ".ttf")) {
				// Fallback .woff/.ttf requests to the bundled .woff2 font
				base := strings.TrimSuffix(strings.TrimSuffix(embedPath, ".woff"), ".ttf") + ".woff2"
				data, err = files.ReadFile(base)
			}
			if err != nil {
				http.NotFound(w, r)
				return
			}
			w.Header().Set("Content-Type", contentTypeForExt(embedPath))
			w.Header().Set("Cache-Control", "public, max-age=86400")
			http.ServeContent(w, r, embedPath, modTime, strings.NewReader(string(data)))
			return
		}

		a, ok := routes[r.URL.Path]
		if !ok {
			http.NotFound(w, r)
			return
		}

		data, err := files.ReadFile(a.path)
		if err != nil {
			http.NotFound(w, r)
			return
		}

		w.Header().Set("Content-Type", a.contentType)
		w.Header().Set("Cache-Control", "public, max-age=86400")
		http.ServeContent(w, r, a.path, modTime, strings.NewReader(string(data)))
	})
}
