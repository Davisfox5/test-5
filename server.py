"""Minimal static server for local marketing-site preview.

Production serving happens via FastAPI's StaticFiles mount; this script is
for demoing website/ without booting the full backend. Security headers
here mirror the SecurityHeadersMiddleware on the FastAPI side so local
testing matches production behavior.
"""

import http.server
import os
import socketserver

PORT = 8000
DIRECTORY = "website"


# Content-Security-Policy permissive enough for the demo site (Google Fonts,
# inline style attributes on a few elements) but tight on script execution.
# When legal pages / self-hosted fonts land, the googleapis / gstatic entries
# should be dropped.
_CSP = (
    "default-src 'self'; "
    "style-src 'self' https://fonts.googleapis.com 'unsafe-inline'; "
    "font-src 'self' https://fonts.gstatic.com; "
    "script-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def end_headers(self):
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=()",
        )
        # HSTS is HTTPS-only; SimpleHTTPRequestHandler is HTTP so we skip it here.
        # Production (FastAPI StaticFiles behind TLS) sets it via middleware.
        super().end_headers()

    def log_message(self, format, *args):
        print(f"[CallSight Server] {args[0]} - {args[1]}")


if __name__ == "__main__":
    if not os.path.exists(DIRECTORY):
        print(f"Error: '{DIRECTORY}' directory not found.")
        exit(1)

    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        print("==========================================")
        print("CallSight AI local preview server")
        print(f"URL: http://localhost:{PORT}")
        print(f"Serving from: ./{DIRECTORY}")
        print("==========================================")
        print("Press Ctrl+C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down server...")
            httpd.shutdown()
