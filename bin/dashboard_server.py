#!/usr/bin/env python3
"""Serve the orchestrator dashboard over a private network / Tailscale."""

from __future__ import annotations

import argparse
import http.server
import json
import os
import socketserver
import sys
import threading
import time
from http import HTTPStatus
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orchestrator as o  # noqa: E402
import dashboard_feed  # noqa: E402


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

        request_path = urlparse(self.path).path

        if request_path in ("/", "/index.html", "/orchestrator-dashboard.html"):
            self._send_file(o.DASHBOARD_HTML_PATH)
            return
        if request_path in ("/state/runtime/dashboard-feed.json", "/dashboard-feed.json", "/api/dashboard"):
            self._send_dashboard_json()
            return
        if request_path == "/api/dashboard/events":
            self._send_dashboard_sse()
            return
        if request_path == "/healthz":
            body = json.dumps({"ok": True, "ts": o.now_iso()}, sort_keys=True).encode()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(HTTPStatus.NOT_FOUND)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"not found\n")

    def log_message(self, fmt, *args):
        return

    def _send_file(self, path):
        if not path.exists():
            self.send_response(HTTPStatus.NOT_FOUND)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"missing\n")
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", _content_type(path.name))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_dashboard_json(self):
        body = self.server.dashboard_state.payload_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_dashboard_sse(self):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self._write_sse("ready", {"ts": o.now_iso()})
            while True:
                payload = self.server.dashboard_state.wait_for_update(timeout=5.0)
                if payload is None:
                    self._write_sse("ping", {"ts": o.now_iso()})
                    continue
                self._write_sse("dashboard", payload)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _write_sse(self, event_name, payload):
        message = f"event: {event_name}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n".encode("utf-8")
        self.wfile.write(message)
        self.wfile.flush()


class DashboardState:
    def __init__(self, refresh_interval=5.0):
        self.refresh_interval = max(float(refresh_interval), 1.0)
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._payload = {"timestamp": o.now_iso(), "error": "dashboard not initialized"}
        self._version = 0

    def start(self):
        self.refresh(force=True)
        thread = threading.Thread(target=self._loop, name="dashboard-state-refresh", daemon=True)
        thread.start()

    def _loop(self):
        while True:
            time.sleep(self.refresh_interval)
            try:
                self.refresh()
            except Exception:
                continue

    def refresh(self, *, force=False):
        payload = dashboard_feed.build_feed()
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        with self._condition:
            current = json.dumps(self._payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
            if force or encoded != current:
                self._payload = payload
                self._version += 1
                self._condition.notify_all()
            return self._payload

    def payload_bytes(self):
        with self._lock:
            return json.dumps(self._payload, separators=(",", ":"), sort_keys=True).encode("utf-8")

    def wait_for_update(self, timeout=5.0):
        deadline = time.time() + timeout
        with self._condition:
            version = self._version
            while version == self._version:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._condition.wait(timeout=remaining)
            return self._payload


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port


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
    server.dashboard_state = DashboardState(refresh_interval=5.0)
    server.dashboard_state.start()
    print(f"http://{host}:{port}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
