#!/usr/bin/env python3
"""Transparent OpenAI proxy that records request timing as JSONL spans."""

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock

UPSTREAM = "https://api.openai.com"
WRITE_LOCK = Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:
        return

    def _forward(self) -> None:
        started_wall = time.time()
        started_ns = time.perf_counter_ns()
        body = self.rfile.read(int(self.headers.get("content-length", "0")))
        model = None
        try:
            model = json.loads(body).get("model") if body else None
        except (ValueError, AttributeError):
            pass

        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in {"host", "content-length", "connection", "accept-encoding"}
        }
        request = urllib.request.Request(UPSTREAM + self.path, data=body or None, headers=headers, method=self.command)
        status = 502
        response_body = b""
        response_headers = {}
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                status = response.status
                response_body = response.read()
                response_headers = dict(response.headers.items())
        except urllib.error.HTTPError as error:
            status = error.code
            response_body = error.read()
            response_headers = dict(error.headers.items())
        finally:
            ended_ns = time.perf_counter_ns()
            event = {
                "kind": "llm",
                "started_at": started_wall,
                "duration_ms": round((ended_ns - started_ns) / 1_000_000, 3),
                "method": self.command,
                "path": self.path,
                "model": model,
                "status": status,
            }
            with WRITE_LOCK, self.server.trace_path.open("a", encoding="utf-8") as stream:  # type: ignore[attr-defined]
                stream.write(json.dumps(event, separators=(",", ":")) + "\n")

        self.send_response(status)
        for key, value in response_headers.items():
            if key.lower() not in {"transfer-encoding", "content-length", "connection", "content-encoding"}:
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        self.wfile.write(response_body)

    do_GET = do_POST = do_DELETE = do_PATCH = do_PUT = _forward


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--trace", type=Path, required=True)
    args = parser.parse_args()
    args.trace.parent.mkdir(parents=True, exist_ok=True)
    args.trace.write_text("")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.trace_path = args.trace  # type: ignore[attr-defined]
    server.serve_forever()


if __name__ == "__main__":
    main()
