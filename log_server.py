"""
log_server.py — HTTP log viewer for Claude.
Serves the last 200 Larry log lines at GET /
so Claude can WebFetch it without needing GitHub MCP.

Railway exposes this automatically when PORT is set.
Public URL is logged at startup (RAILWAY_PUBLIC_DOMAIN env var).
"""

import os
import threading
import logging
from collections import deque
from http.server import HTTPServer, BaseHTTPRequestHandler

_buf: deque = deque(maxlen=200)
_buf_lock = threading.Lock()

log = logging.getLogger(__name__)


class LogBufferHandler(logging.Handler):
    """Captures all log lines into a circular buffer for HTTP serving."""

    def emit(self, record: logging.LogRecord):
        try:
            line = self.format(record)
            with _buf_lock:
                _buf.append(line)
        except Exception:
            pass


class _HTTPHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        with _buf_lock:
            body = "\n".join(_buf).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # silence HTTP access logs


def start():
    """Start the log HTTP server in a daemon thread. Call once from main.py."""
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), _HTTPHandler)
    t = threading.Thread(target=server.serve_forever, name="LogServer", daemon=True)
    t.start()

    # Log the public URL so it appears in GitHub Issue #2 at startup
    public_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if public_domain:
        log.info(f"📡 Log server: https://{public_domain}  (Claude can WebFetch this)")
    else:
        log.info(f"📡 Log server started on port {port} (no public domain yet)")
