#!/usr/bin/env python3
"""Serve an adapted prompt plan locally using the existing remote YIG origin.

The original inference server remains untouched. Fetch its turns before the
experiment, then expose the adapted turn sequence and proxy image artifacts to
guests on this host.
"""

import argparse
import concurrent.futures
import json
import urllib.parse
import urllib.request
from pathlib import Path

from swebench_microvm_scale_experiment import (
    InferenceHTTPServer, TraceRequestError, TraceRequestHandler, TraceService,
)


def fetch_workflow(session, upstream):
    instance = session["instance_id"]
    session_id = "multi-prompt-source-" + instance
    rows = []
    for turn in range(1, session["source_turn_count"] + 1):
        payload = json.dumps({"instance_id": instance, "session_id": session_id,
                              "turn": turn}).encode()
        request = urllib.request.Request(upstream, data=payload,
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=60) as response:
            row = json.load(response)
        if row["turn"] != turn:
            raise ValueError(f"unexpected turn for {instance}: {row['turn']}")
        if row["done"] != (turn == session["source_turn_count"]):
            raise ValueError(f"source turn count does not match origin for {instance}")
        rows.append({"commands": row["commands"],
                     "inference_seconds": row.get("inference_seconds", 0.0)})
    return instance, rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--upstream", required=True,
                        help="original server's /v1/inference endpoint")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--source-cache", type=Path,
                        help="optional saved original turns for restart")
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    if plan.get("format") != "microvm-multi-prompt-plan-v1":
        parser.error("unsupported plan format")
    if args.workers < 1:
        parser.error("workers must be positive")
    if args.source_cache and args.source_cache.exists():
        workflows = json.loads(args.source_cache.read_text())
    else:
        workflows = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            source_sessions = {session["instance_id"]: session
                               for session in plan["sessions"]}
            futures = [pool.submit(fetch_workflow, session, args.upstream)
                       for session in source_sessions.values()]
            for future in concurrent.futures.as_completed(futures):
                instance, rows = future.result()
                workflows[instance] = rows
                print(f"fetched {instance}: {len(rows)} turns", flush=True)
        if args.source_cache:
            args.source_cache.parent.mkdir(parents=True, exist_ok=True)
            args.source_cache.write_text(json.dumps(workflows) + "\n")
    service = TraceService(workflows, plan)

    class Handler(TraceRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            if not parsed.path.startswith("/v1/artifacts/"):
                return super().do_GET()
            instance = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1])
            try:
                self.service.artifact_image(instance)  # Check plan membership.
                # The deployment host owns the SWE-bench Docker images. Stream
                # them directly instead of proxying to the retired :8080
                # artifact endpoint; only inference turns come from upstream.
                self.stream_artifact(self.service.artifact_image(instance))
            except TraceRequestError as exc:
                self._json(exc.status, {"error": str(exc)})
            except (BrokenPipeError, ConnectionResetError):
                pass

    Handler.service = service
    server = InferenceHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print(f"serving {len(service.workflows)} adapted workflows on "
          f"http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
