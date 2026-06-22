#!/usr/bin/env python3
"""
Repro server for proxify POST-body-drop bug.

Usage:
  1. Start proxify:    proxify -http-addr 127.0.0.1:8888 -output /tmp/proxify-test.jsonl
  2. Start this server: python3 proxify-repro-server.py
  3. Send a request:    curl -x http://127.0.0.1:8888 -X POST \
                              -d "username=admin&password=secret" \
                              http://127.0.0.1:9999/login

The server prints the full method, path, headers, and body for every request.
If the bug is present the body will be empty.
"""

import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


class DebugHandler(BaseHTTPRequestHandler):
    def _handle(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b""

        print(f"\n{'=' * 60}")
        print(f"{self.command} {self.path} {self.request_version}")
        for name, value in self.headers.items():
            print(f"  {name}: {value}")
        print(f"\nBody ({len(body)} bytes):")
        if body:
            print(f"  {body.decode(errors='replace')}")
        else:
            print("  <EMPTY — bug is present if a body was expected>")
        print(f"{'=' * 60}\n")
        sys.stdout.flush()

        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK\n")

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle


if __name__ == "__main__":
    addr = ("127.0.0.1", 9999)
    print(f"Listening on {addr[0]}:{addr[1]} ...")
    HTTPServer(addr, DebugHandler).serve_forever()
