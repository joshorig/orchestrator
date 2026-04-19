#!/usr/bin/env python3
"""Serve the orchestrator dashboard over a private network / Tailscale."""

from __future__ import annotations

import argparse
import http.server
import json
import os
import socketserver
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orchestrator as o  # noqa: E402


def _content_type(path):
    if path.endswith(".json"):
        return "application/json; charset=utf-8"
    if path.endswith(".html"):
        return "text/html; charset=utf-8"
    return "text/plain; charset=utf-8"


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    server_version = "devmini-dashboard/1.0"

    def do_GET(self):
        client_ip = self.client_address[0]
        if not o.dashboard_client_allowed(client_ip, cfg=self.server.cfg):
            self.send_response(403)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"forbidden\n")
            return

        if self.path in ("/", "/index.html", "/orchestrator-dashboard.html"):
            self._send_file(o.DASHBOARD_HTML_PATH)
            return
        if self.path in ("/state/runtime/dashboard-feed.json", "/dashboard-feed.json"):
            self._send_file(o.DASHBOARD_FEED_PATH)
            return
        if self.path == "/healthz":
            body = json.dumps({"ok": True, "ts": o.now_iso()}, sort_keys=True).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"not found\n")

    def log_message(self, fmt, *args):
        return

    def _send_file(self, path):
        if not path.exists():
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"missing\n")
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", _content_type(path.name))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser(prog="dashboard_server")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args()

    cfg = o.dashboard_server_config()
    host = args.host or cfg["host"]
    port = args.port or cfg["port"]

    server = ThreadingHTTPServer((host, port), DashboardHandler)
    server.cfg = o.load_config()
    print(f"http://{host}:{port}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
