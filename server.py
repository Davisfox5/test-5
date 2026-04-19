import http.server
import socketserver
import os

PORT = 8000
DIRECTORY = "website"

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def log_message(self, format, *args):
        # Clean up logs to be less noisy
        print(f"[LINDA Server] {args[0]} - {args[1]}")

if __name__ == "__main__":
    if not os.path.exists(DIRECTORY):
        print(f"Error: '{DIRECTORY}' directory not found.")
        exit(1)

    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        print(f"==========================================")
        print(f"🚀 LINDA Live Website is running!")
        print(f"🔗 URL: http://localhost:{PORT}")
        print(f"📂 Serving from: ./{DIRECTORY}")
        print(f"==========================================")
        print("Press Ctrl+C to stop the server.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down server...")
            httpd.shutdown()
