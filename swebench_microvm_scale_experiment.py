#!/usr/bin/env python3
"""SWE-bench microVM scale experiment with remote trace-backed inference.

Runs 1, 2, 4, 8, 16, 32, 64, and 128 simultaneous Cloud Hypervisor sessions.
The VM contains only the official SWE-bench environment.  At runtime its guest
agent requests one turn at a time from a remote inference service, executes the
returned tool calls locally, and powers itself off when the service marks the
trajectory complete.  No trajectory, replay manifest, or simulated inference
delay is copied into a VM or the shared rootfs.

The remote service contract is intentionally small and trace-friendly:

  POST /v1/inference
  {"instance_id": "...", "session_id": "...", "turn": 1}
  {"turn": 1, "commands": ["..."], "done": false,
   "inference_seconds": 0.0}

``inference_seconds`` is optional metadata for measurement; the guest never
sleeps on it.  The network round trip is the inference work in this experiment.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import ipaddress
import json
import os
import random
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_STAGES = (1, 2, 4, 8, 16, 32, 64, 128)
DEFAULT_INFERENCE_ENDPOINT = "http://130.127.133.251:8080/v1/inference"
DEFAULT_TRACE_ROOT = ROOT / "yig"


class MemoryGuard(Exception):
    """Admission stopped before the host reached the configured OOM guard."""
GUEST_INFERENCE = r'''#!/usr/bin/env python3
import base64, json, os, subprocess, sys, tempfile, time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
MAX_CAPTURE = 65536
def command(cmd):
    begun = time.time()
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        result = subprocess.run(["chroot", "/rw", "/bin/bash", "-lc", "cd /testbed && " + cmd], stdin=subprocess.DEVNULL, stdout=out, stderr=err, check=False)
        osize, esize = out.tell(), err.tell(); out.seek(0); err.seek(0)
        return {'command': cmd, 'started_at': begun, 'finished_at': time.time(), 'elapsed_seconds': time.time() - begun, 'returncode': result.returncode, 'stdout': out.read(MAX_CAPTURE).decode('utf-8', 'replace'), 'stderr': err.read(MAX_CAPTURE).decode('utf-8', 'replace'), 'stdout_truncated': osize > MAX_CAPTURE, 'stderr_truncated': esize > MAX_CAPTURE}
def request_turn(endpoint, instance_id, session_id, turn, timeout):
    payload = json.dumps({'instance_id': instance_id, 'session_id': session_id, 'turn': turn}).encode()
    request = Request(endpoint, data=payload, headers={'Content-Type': 'application/json', 'Accept': 'application/json'}, method='POST')
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode('utf-8'))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        raise RuntimeError('remote inference request failed: ' + str(exc)) from exc
    if not isinstance(result, dict):
        raise RuntimeError('remote inference response must be a JSON object')
    if int(result.get('turn', turn)) != turn:
        raise RuntimeError('remote inference returned an unexpected turn')
    commands = result.get('commands', [])
    if not isinstance(commands, list) or not all(isinstance(item, str) for item in commands):
        raise RuntimeError('remote inference response commands must be a string list')
    return result
def main():
    instance_id = os.environ['MICROVM_INSTANCE_ID']; session_id = os.environ['MICROVM_SESSION_ID']
    endpoint = os.environ['MICROVM_INFERENCE_ENDPOINT']; timeout = float(os.environ.get('MICROVM_INFERENCE_TIMEOUT', '120'))
    max_turns = int(os.environ.get('MICROVM_MAX_TURNS', '1000'))
    replay = Path('/rw/replay'); replay.mkdir(parents=True, exist_ok=True)
    tool_records = (replay / 'guest-tools.jsonl').open('w'); turn_records = (replay / 'guest-turns.jsonl').open('w')
    failures = tools = 0; turn = 1
    serial = open('/dev/ttyS0', 'w', buffering=1)
    try:
      while turn <= max_turns:
        started = time.time(); inference_started = time.monotonic()
        response = request_turn(endpoint, instance_id, session_id, turn, timeout)
        inference_elapsed = time.monotonic() - inference_started
        turn_failures = 0
        for action, cmd in enumerate(response.get('commands', []), 1):
            record = command(cmd); record.update({'turn': turn, 'action': action}); tool_records.write(json.dumps(record) + '\n'); tool_records.flush()
            tools += 1; turn_failures += record['returncode'] != 0
        failures += turn_failures
        turn_record = {'turn': turn, 'started_at': started, 'finished_at': time.time(), 'inference_elapsed_seconds': inference_elapsed, 'server_inference_seconds': response.get('inference_seconds'), 'tool_commands': len(response.get('commands', [])), 'tool_failures': turn_failures}
        turn_records.write(json.dumps(turn_record) + '\n'); turn_records.flush()
        serial.write('REPLAY_TURN ' + base64.b64encode(json.dumps(turn_record).encode()).decode() + '\n')
        if response.get('done', False): break
        turn += 1
      else:
        raise RuntimeError('remote inference exceeded MICROVM_MAX_TURNS')
    except Exception as exc:
      error = {'instance_id': instance_id, 'turn': turn, 'error': str(exc)}
      serial.write('REPLAY_ERROR ' + base64.b64encode(json.dumps(error).encode()).decode() + '\n')
      raise
    finally:
      tool_records.close(); turn_records.close()
    serial.write('REPLAY_DONE\n'); serial.close()
    (replay / 'summary.json').write_text(json.dumps({'instance_id': instance_id, 'session_id': session_id, 'inference_endpoint': endpoint, 'tools': tools, 'tool_failures': failures, 'turns': turn}, indent=2) + '\n'); os.sync()
if __name__ == '__main__': main()
'''
GUEST_INIT = r'''#!/bin/bash
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mkdir -p /run /tmp /lower /overlay /rw
cmdline=$(cat /proc/cmdline)
getarg() { printf '%s\n' "$cmdline" | sed -n "s/.* $1=\([^ ]*\).*/\1/p"; }
instance_id=$(getarg microvm_instance)
session_id=$(getarg microvm_session)
inference_endpoint=$(getarg microvm_inference)
guest_ip=$(getarg microvm_ip)
gateway_ip=$(getarg microvm_gateway)
inference_timeout=$(getarg microvm_timeout)
max_turns=$(getarg microvm_max_turns)
netdev=
for netpath in /sys/class/net/*; do
    name=${netpath##*/}
    if [ "$name" != lo ]; then netdev=$name; break; fi
done
if [ -z "$netdev" ] || [ -z "$guest_ip" ] || [ -z "$gateway_ip" ]; then
    echo "missing guest network configuration" >&2
    /bin/busybox poweroff -f
    exit 2
fi
ip_bin=/sbin/ip
[ -x "$ip_bin" ] || ip_bin=/usr/sbin/ip
if ! "$ip_bin" link set lo up || ! "$ip_bin" link set "$netdev" up || ! "$ip_bin" addr add "$guest_ip/24" dev "$netdev" || ! "$ip_bin" route add default via "$gateway_ip"; then
    echo "guest network setup failed" >&2
    /bin/busybox poweroff -f
    exit 3
fi
mount -t ext4 -o ro /dev/vdb /lower
mount -t tmpfs -o nosuid,nodev tmpfs /overlay
mkdir -p /overlay/upper /overlay/work /overlay/upper/replay
if ! mount -t overlay overlay -o lowerdir=/lower,upperdir=/overlay/upper,workdir=/overlay/work /rw; then
    echo "OVERLAYFS_FAILED" > /dev/ttyS0
    echo "overlayfs is required; refusing the full-image tmpfs fallback" >&2
    /bin/busybox poweroff -f
    exit 3
fi
echo "OVERLAYFS_READY" > /dev/ttyS0
mount -t tmpfs -o nosuid,nodev tmpfs /tmp
MICROVM_INSTANCE_ID="$instance_id" MICROVM_SESSION_ID="$session_id" MICROVM_INFERENCE_ENDPOINT="$inference_endpoint" MICROVM_INFERENCE_TIMEOUT="${inference_timeout:-120}" MICROVM_MAX_TURNS="${max_turns:-1000}" /usr/bin/python3 /usr/local/bin/swebench-remote-inference.py
status=$?
sync
/bin/busybox poweroff -f
exit "$status"
'''


def call(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(list(map(str, args)), check=True, text=True, **kwargs)


def sudo(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return call("sudo", "-n", *args, **kwargs)


def parse_cpu_list(raw: str) -> list[int]:
    result: list[int] = []
    for part in raw.split(","):
        if "-" in part:
            first, last = map(int, part.split("-", 1)); result.extend(range(first, last + 1))
        else:
            result.append(int(part))
    return sorted(set(result))


def cpuset() -> list[int]:
    """Return all CPUs available to this process unless explicitly overridden."""
    override = os.environ.get("MICROVM_CPUSET")
    if override:
        return parse_cpu_list(override)
    online = Path("/sys/devices/system/cpu/online")
    cpus = parse_cpu_list(online.read_text().strip()) if online.exists() else []
    affinity = sorted(os.sched_getaffinity(0))
    return sorted(set(cpus).intersection(affinity)) if cpus else affinity


def format_cpu_list(cpus: list[int]) -> str:
    if not cpus:
        raise RuntimeError("no CPUs are available for the microVM")
    ranges: list[str] = []
    start = previous = cpus[0]
    for cpu in cpus[1:]:
        if cpu == previous + 1:
            previous = cpu
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = cpu
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


def numa_nodes() -> list[Path]:
    nodes = sorted(Path("/sys/devices/system/node").glob("node[0-9]*"),
                   key=lambda p: int(p.name[4:]))
    if not nodes:
        raise RuntimeError("could not determine host NUMA nodes")
    return nodes


def _node_meminfo(node: Path) -> tuple[int, int]:
    text = (node / "meminfo").read_text()
    prefix = re.escape(node.name.replace("node", "Node "))
    total = re.search(rf"^{prefix} MemTotal:\s+(\d+) kB", text, re.M)
    available = re.search(rf"^{prefix} MemAvailable:\s+(\d+) kB", text, re.M)
    used = re.search(rf"^{prefix} MemUsed:\s+(\d+) kB", text, re.M)
    free = re.search(rf"^{prefix} MemFree:\s+(\d+) kB", text, re.M)
    if not total:
        raise RuntimeError(f"could not determine memory for {node.name}")
    total_bytes = int(total.group(1)) * 1024
    if available:
        used_bytes = total_bytes - int(available.group(1)) * 1024
    elif used:
        used_bytes = int(used.group(1)) * 1024
    elif free:
        used_bytes = total_bytes - int(free.group(1)) * 1024
    else:
        raise RuntimeError(f"could not determine used memory for {node.name}")
    return total_bytes, used_bytes


def host_memory_bytes() -> int:
    return sum(_node_meminfo(node)[0] for node in numa_nodes())


def host_used_bytes() -> int:
    return sum(_node_meminfo(node)[1] for node in numa_nodes())


def swap_bytes() -> int:
    """Return configured swap capacity in bytes."""
    total = 0
    for line in Path("/proc/swaps").read_text().splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 3:
            total += int(fields[2]) * 1024
    return total


def initramfs_contains_overlay(initramfs: Path) -> bool:
    """Return whether an initramfs contains the guest OverlayFS module."""
    probe = subprocess.run(["lsinitramfs", str(initramfs)], text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           check=False)
    return probe.returncode == 0 and bool(re.search(r"(?:^|/)overlay\.ko(?:$|[./])", probe.stdout))


def ensure_overlay_initramfs(kernel: Path) -> Path:
    """Build a private initramfs that explicitly carries overlay.ko.

    The host's generic initramfs does not currently include the OverlayFS
    module even though the host kernel has it installed.  Cloud Hypervisor
    boots that initramfs as the guest initramfs, so the module must be put in
    the image explicitly.  A private image keeps the experiment independent
    of the host boot image.
    """
    requested = os.environ.get("MICROVM_INITRAMFS")
    if requested:
        initramfs = Path(requested).expanduser().resolve()
        if not initramfs.is_file() or not initramfs_contains_overlay(initramfs):
            raise SystemExit(f"MICROVM_INITRAMFS must contain overlay.ko: {initramfs}")
        return initramfs

    kernel_release = os.uname().release
    target = ROOT / "microvm-assets" / f"initrd-overlay-{kernel_release}.img"
    if target.is_file() and initramfs_contains_overlay(target):
        return target
    if not shutil.which("mkinitramfs"):
        raise SystemExit("mkinitramfs is required to build the guest OverlayFS initramfs")

    with tempfile.TemporaryDirectory(prefix="swebench-initramfs-") as temp_name:
        temp = Path(temp_name)
        config = temp / "initramfs-tools"
        shutil.copytree("/etc/initramfs-tools", config)
        modules = config / "modules"
        existing = modules.read_text() if modules.exists() else ""
        if not re.search(r"(?m)^overlay(?:\s|$)", existing):
            modules.write_text(existing.rstrip() + "\noverlay\n")
        built = temp / target.name
        call("mkinitramfs", "-d", str(config), "-o", str(built), kernel_release)
        if not initramfs_contains_overlay(built):
            raise RuntimeError(f"mkinitramfs produced an image without overlay.ko: {built}")
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(built, target)
    return target


def filesystem_type(path: Path) -> str:
    return subprocess.check_output(["findmnt", "-T", str(path), "-no", "FSTYPE"], text=True).strip()


def cgroup_value(path: Path, field: str) -> int:
    try:
        return int((path / field).read_text().strip())
    except (FileNotFoundError, PermissionError, ValueError):
        return 0


def cpu_usage_usec(path: Path) -> int:
    try:
        match = re.search(r"^usage_usec\s+(\d+)$", (path / "cpu.stat").read_text(), re.M)
        return int(match.group(1)) if match else 0
    except (FileNotFoundError, PermissionError):
        return 0


def pss_bytes(path: Path) -> int:
    try:
        pids = [int(x) for x in (path / "cgroup.procs").read_text().split()]
    except (FileNotFoundError, PermissionError):
        return 0
    total = 0
    for pid in pids:
        try:
            match = re.search(r"^Pss:\s+(\d+) kB", Path(f"/proc/{pid}/smaps_rollup").read_text(), re.M)
            total += int(match.group(1)) * 1024 if match else 0
        except (FileNotFoundError, PermissionError):
            pass
    return total


def image_name(instance_id: str) -> str:
    return f"swebench/sweb.eval.x86_64.{instance_id.replace('__', '_1776_').lower()}:latest"


def trace_workflow(trace: Path) -> tuple[str, list[dict]]:
    """Load one trajectory for the server; its contents never leave the server."""
    data = json.loads(trace.read_text())
    instance_id = data["instance_id"]
    assistant_turns = [message for message in data.get("messages", [])
                       if message.get("role") == "assistant" and message.get("tool_calls")]
    turns = []
    for index, timing in enumerate(data.get("step_timings", [])):
        commands = []
        calls = assistant_turns[index].get("tool_calls", []) if index < len(assistant_turns) else []
        for call_data in calls:
            function = call_data.get("function", {})
            if function.get("name") != "bash":
                continue
            arguments = function.get("arguments", {})
            try:
                arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
                command = arguments["command"]
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid bash call in {trace}") from exc
            if isinstance(command, str):
                commands.append(command)
        turns.append({
            "commands": commands,
            "inference_seconds": float(timing.get("inference", {}).get("elapsed_s", 0.0)),
        })
    if not turns:
        raise ValueError(f"trajectory has no timed turns: {trace}")
    return instance_id, turns


def load_server_workflows(trace_root: Path) -> dict[str, list[dict]]:
    """Index one trace per instance ID for the remote trace-serving process."""
    workflows: dict[str, list[dict]] = {}
    for trace in sorted(trace_root.glob("*/results/*/*.traj.json")):
        instance_id, turns = trace_workflow(trace)
        workflows.setdefault(instance_id, turns)
    if not workflows:
        raise SystemExit(f"no SWE-bench trajectories found under {trace_root}")
    return workflows


class TraceRequestError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class TraceService:
    """Thread-safe, session-pinned access to the server's YIG trajectories."""

    def __init__(self, workflows: dict[str, list[dict]]) -> None:
        self.workflows = workflows
        self.sessions: dict[str, dict] = {}
        self.lock = threading.Lock()

    def instances(self) -> list[str]:
        return sorted(self.workflows)

    def inference(self, request: object) -> dict:
        if not isinstance(request, dict):
            raise TraceRequestError(400, "request must be a JSON object")
        session_id = request.get("session_id")
        requested_instance = request.get("instance_id")
        turn = request.get("turn")
        if not isinstance(session_id, str) or not session_id:
            raise TraceRequestError(400, "session_id is required")
        if requested_instance is not None and not isinstance(requested_instance, str):
            raise TraceRequestError(400, "instance_id must be a string")
        if not isinstance(turn, int) or isinstance(turn, bool) or turn < 1:
            raise TraceRequestError(400, "turn must be a positive integer")
        with self.lock:
            state = self.sessions.get(session_id)
            if state is None:
                instance_id = requested_instance
                if not instance_id or instance_id not in self.workflows:
                    raise TraceRequestError(404, "unknown instance_id")
                state = {"instance_id": instance_id, "next_turn": 1, "responses": {}}
                self.sessions[session_id] = state
            elif requested_instance and requested_instance != state["instance_id"]:
                raise TraceRequestError(409, "session is already bound to another instance_id")
            if turn in state["responses"]:
                return state["responses"][turn]
            if turn != state["next_turn"]:
                raise TraceRequestError(409, f"expected turn {state['next_turn']}")
            turns = self.workflows[state["instance_id"]]
            if turn > len(turns):
                raise TraceRequestError(409, "trajectory is already complete")
            saved = turns[turn - 1]
            response = {
                "turn": turn,
                "commands": saved["commands"],
                "done": turn == len(turns),
                "inference_seconds": saved["inference_seconds"],
            }
            state["responses"][turn] = response
            state["next_turn"] += 1
            return response


class TraceRequestHandler(http.server.BaseHTTPRequestHandler):
    service: TraceService

    def _json(self, status: int, payload: object) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._json(200, {"ok": True, "instances": len(self.service.workflows)})
        elif self.path == "/v1/instances":
            self._json(200, {"instances": self.service.instances()})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/v1/inference":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if length < 0 or length > 1_048_576:
                raise TraceRequestError(413, "request body is missing or too large")
            request = json.loads(self.rfile.read(length).decode("utf-8"))
            self._json(200, self.service.inference(request))
        except TraceRequestError as exc:
            self._json(exc.status, {"error": str(exc)})
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._json(400, {"error": f"invalid JSON request: {exc}"})
        except Exception as exc:
            self._json(500, {"error": f"server error: {exc}"})

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}", flush=True)


def serve_traces(trace_root: Path, host: str, port: int) -> None:
    """Run the multithreaded origin service used by the microVM clients."""
    service = TraceService(load_server_workflows(trace_root))

    class Handler(TraceRequestHandler):
        pass

    Handler.service = service
    server = http.server.ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    print(f"serving {len(service.workflows)} YIG workflows on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def catalog_url(inference_endpoint: str) -> str:
    """Derive the default instance catalog URL from the inference endpoint."""
    parsed = urllib.parse.urlsplit(inference_endpoint)
    path = parsed.path.rsplit("/", 1)[0] + "/instances"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def validate_inference_endpoint(endpoint: str) -> None:
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or any(char.isspace() for char in endpoint):
        raise SystemExit("--inference-endpoint must be an http(s) URL without whitespace")


def fetch_json(url: str) -> object:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"could not fetch remote inference metadata from {url}: {exc}") from exc


def load_instance_ids(inference_endpoint: str, catalog: str | None,
                      ids_file: Path | None, explicit_ids: list[str]) -> list[str]:
    """Load only instance identifiers; trajectory contents stay on the remote node."""
    values = list(explicit_ids)
    if ids_file:
        values.extend(line.strip() for line in ids_file.read_text().splitlines()
                      if line.strip() and not line.lstrip().startswith("#"))
    if not values:
        payload = fetch_json(catalog or catalog_url(inference_endpoint))
        if isinstance(payload, dict):
            payload = payload.get("instances", payload.get("instance_ids"))
        if not isinstance(payload, list):
            raise SystemExit("remote instance catalog must be a JSON list or an object with an 'instances' list")
        values = payload
    if not all(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", value) for value in values):
        raise SystemExit("remote instance identifiers must contain only letters, digits, '.', '_' or '-'")
    unique = list(dict.fromkeys(values))
    return unique


def prepare_guest(rootfs_tree: Path, rootfs: Path) -> None:
    """Install the networked inference runner and rebuild the shared base disk."""
    with tempfile.TemporaryDirectory(prefix="swebench-guest-") as temp:
        temp = Path(temp)
        runner, init = temp / "swebench-remote-inference.py", temp / "init"
        runner.write_text(GUEST_INFERENCE); init.write_text(GUEST_INIT)
        sudo("rm", "-f", str(rootfs_tree / "usr/local/bin/swebench-local-replay.py"))
        sudo("rm", "-rf", str(rootfs_tree / "replay-manifests"))
        sudo("install", "-m", "0755", str(runner), str(rootfs_tree / "usr/local/bin/swebench-remote-inference.py"))
        sudo("install", "-m", "0755", str(init), str(rootfs_tree / "sbin/init"))
        for mountpoint in ("/lower", "/overlay", "/rw", "/tmp"):
            sudo("install", "-d", "-m", "0755", str(rootfs_tree / mountpoint.lstrip("/")))
        if not (rootfs_tree / "sbin/ip").is_file() and not (rootfs_tree / "usr/sbin/ip").is_file():
            raise SystemExit("the guest rootfs needs iproute2 (/sbin/ip or /usr/sbin/ip) for remote inference networking")
        sudo("mkfs.ext4", "-F", "-q", "-d", str(rootfs_tree), str(rootfs))


def default_uplink() -> str:
    for line in subprocess.check_output(["ip", "-4", "route", "show", "default"], text=True).splitlines():
        fields = line.split()
        if "dev" in fields:
            return fields[fields.index("dev") + 1]
    raise SystemExit("could not determine the host uplink; pass --network-interface")


class StageNetwork:
    """Give stage VMs a private bridge with NAT limited to the inference host."""

    def __init__(self, subnet: str, inference_endpoint: str, interface: str | None,
                 pid: int, sessions: list[int]) -> None:
        self.network = ipaddress.ip_network(subnet, strict=True)
        self.endpoint_ip = urllib.parse.urlsplit(inference_endpoint).hostname
        if not self.endpoint_ip:
            raise SystemExit("inference endpoint must include a host IP address")
        try:
            ipaddress.ip_address(self.endpoint_ip)
        except ValueError as exc:
            raise SystemExit("inference endpoint hostname must be an IP address so guests need no DNS") from exc
        hosts = list(self.network.hosts())
        if len(hosts) < len(sessions) + 1:
            raise SystemExit(f"--network-subnet {subnet} does not have enough guest addresses")
        self.gateway_ip = str(hosts[0])
        self.sessions = sessions
        self.uplink = interface or default_uplink()
        self.bridge = f"swebr{pid % 100000000:08d}"[:15]
        self.taps = {session: f"swet{pid % 1000000:06d}{session:03d}"[:15] for session in sessions}
        self.guest_ips = {session: str(hosts[index + 1]) for index, session in enumerate(sessions)}
        self.forwarding_before = Path("/proc/sys/net/ipv4/ip_forward").read_text().strip()
        self.rules: list[list[str]] = []
        self.ready = False

    def setup(self) -> None:
        prefix = self.network.prefixlen
        sudo("ip", "link", "add", self.bridge, "type", "bridge")
        sudo("ip", "addr", "add", f"{self.gateway_ip}/{prefix}", "dev", self.bridge)
        sudo("ip", "link", "set", self.bridge, "up")
        self.ready = True
        for session in self.sessions:
            tap = self.taps[session]
            sudo("ip", "tuntap", "add", "dev", tap, "mode", "tap", "user", str(os.getuid()))
            sudo("ip", "link", "set", tap, "master", self.bridge)
            sudo("ip", "link", "set", tap, "up")
        sudo("sysctl", "-w", "net.ipv4.ip_forward=1")
        nat_rule = ["-t", "nat", "-A", "POSTROUTING", "-s", str(self.network), "-d", self.endpoint_ip,
                    "-o", self.uplink, "-j", "MASQUERADE"]
        forward_out = ["-A", "FORWARD", "-i", self.bridge, "-o", self.uplink, "-d", self.endpoint_ip, "-j", "ACCEPT"]
        forward_in = ["-A", "FORWARD", "-i", self.uplink, "-o", self.bridge, "-s", self.endpoint_ip,
                      "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"]
        for rule in (nat_rule, forward_out, forward_in):
            sudo("iptables", *rule)
            self.rules.append(rule)

    def config(self, session: int) -> dict[str, str]:
        # Cloud Hypervisor's virtio-net device gets the first non-loopback name.
        return {"tap": self.taps[session], "ip": self.guest_ips[session],
                "gateway": self.gateway_ip, "mac": f"02:fc:00:{session // 256:02x}:{session % 256:02x}:01"}

    def close(self) -> None:
        if not self.ready:
            return
        for rule in reversed(self.rules):
            delete = rule.copy()
            delete[delete.index("-A")] = "-D"
            try:
                sudo("iptables", *delete)
            except subprocess.CalledProcessError:
                pass
        for tap in self.taps.values():
            try:
                sudo("ip", "link", "delete", tap)
            except subprocess.CalledProcessError:
                pass
        try:
            sudo("ip", "link", "delete", self.bridge)
        except subprocess.CalledProcessError:
            pass
        try:
            sudo("sysctl", "-w", f"net.ipv4.ip_forward={self.forwarding_before}")
        except subprocess.CalledProcessError:
            pass
        self.ready = False


class VM:
    def __init__(self, run_dir: Path, image_root: Path, session: int, manifest: dict,
                 memory_ceiling_mib: int, allow_swap: bool) -> None:
        self.run_dir, self.session, self.manifest = run_dir, session, manifest
        self.vm_dir = run_dir / "vms" / f"session-{session:03d}"; self.vm_dir.mkdir(parents=True)
        image = image_name(manifest["instance_id"])
        image_key = hashlib.sha256(image.encode()).hexdigest()[:24]
        self.disk = image_root / f"{image_key}.raw"
        self.cgroup = Path(f"/sys/fs/cgroup/swebench-microvm-{os.getpid()}-{session}")
        self.memory_ceiling_mib = memory_ceiling_mib
        self.allow_swap = allow_swap
        self.proc: subprocess.Popen | None = None

    def build_disk(self, staging: Path, memory_threshold_bytes: int) -> None:
        if self.disk.exists():
            return
        image = image_name(self.manifest["instance_id"])
        image_key = hashlib.sha256(image.encode()).hexdigest()[:24]
        root = staging / f"image-{image_key}"
        root.mkdir(parents=True, exist_ok=False)
        container = subprocess.check_output(["sudo", "-n", "docker", "create", image, "/bin/true"], text=True).strip()
        try:
            exported = subprocess.Popen(["sudo", "-n", "docker", "export", container], stdout=subprocess.PIPE)
            extracted = subprocess.run(["sudo", "-n", "tar", "-C", str(root), "-xpf", "-"], stdin=exported.stdout, check=False)
            assert exported.stdout is not None; exported.stdout.close()
            if exported.wait() or extracted.returncode:
                raise RuntimeError(f"failed to export {image}")
            kib = int(subprocess.check_output(["sudo", "-n", "du", "-sk", str(root)], text=True).split()[0])
            if host_used_bytes() >= memory_threshold_bytes:
                raise MemoryGuard(f"shared image for session {self.session} reached the {memory_threshold_bytes} byte host-memory guard")
            self.disk.parent.mkdir(parents=True, exist_ok=True)
            call("truncate", "-s", f"{kib + 1024 * 1024}K", str(self.disk))
            sudo("mkfs.ext4", "-F", "-q", "-d", str(root), str(self.disk))
        finally:
            subprocess.run(["sudo", "-n", "docker", "rm", "-f", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["sudo", "-n", "rm", "-rf", str(root)], check=True)

    def prepare_cgroup(self) -> None:
        sudo("mkdir", "-p", str(self.cgroup))
        swap_limit = self.cgroup / "memory.swap.max"
        if swap_limit.exists() and not self.allow_swap:
            sudo("sh", "-c", f"echo 0 > {swap_limit}")

    def start(self, rootfs: Path, kernel: Path, initramfs: Path,
              network: dict[str, str], inference_endpoint: str,
              inference_timeout: float, max_turns: int) -> None:
        cpus = cpuset()
        cpu_arg = format_cpu_list(cpus)
        command = ["taskset", "-c", cpu_arg, "numactl", "--interleave=all", "cloud-hypervisor",
                   "--kernel", str(kernel), "--initramfs", str(initramfs),
                   "--disk", f"path={rootfs},image_type=raw,readonly=on", "--disk", f"path={self.disk},image_type=raw,readonly=on",
                   "--net", f"tap={network['tap']},mac={network['mac']}",
                   "--memory", "size=0", "--memory-zone", f"size={self.memory_ceiling_mib}M,id=mem0",
                   "--numa", f"guest_numa_id=0,cpus={cpu_arg},memory_zones=mem0", "--cpus", f"boot={len(cpus)},max={len(cpus)}",
                   "--console", "off", "--serial", f"file={self.vm_dir / 'serial.log'}",
                   "--cmdline", f"root=/dev/vda rootfstype=ext4 ro fsck.mode=skip init=/sbin/init console=ttyS0 panic=-1 microvm_instance={self.manifest['instance_id']} microvm_session={self.session} microvm_inference={inference_endpoint} microvm_ip={network['ip']} microvm_gateway={network['gateway']} microvm_timeout={inference_timeout} microvm_max_turns={max_turns}"]
        self.proc = subprocess.Popen(command, stdout=(self.vm_dir / "vmm.stdout").open("w"), stderr=subprocess.STDOUT, start_new_session=True)
        sudo("sh", "-c", f"echo {self.proc.pid} > {self.cgroup}/cgroup.procs")

    def sample(self) -> dict:
        return {"timestamp": time.time(), "session": self.session, "instance_id": self.manifest["instance_id"],
                "cgroup_memory_bytes": cgroup_value(self.cgroup, "memory.current"), "pss_bytes": pss_bytes(self.cgroup),
                "cpu_usage_usec": cpu_usage_usec(self.cgroup)}

    def done(self) -> bool:
        return self.proc is not None and self.proc.poll() is not None

    def read_jsonl(self, guest_path: str) -> list[dict]:
        if guest_path != "/replay/guest-turns.jsonl":
            return []
        rows = []
        try:
            text = (self.vm_dir / "serial.log").read_text(errors="replace")
            for encoded in re.findall(r"REPLAY_TURN ([A-Za-z0-9+/=]+)", text):
                rows.append(json.loads(base64.b64decode(encoded).decode()))
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            pass
        return rows

    def overlay_ready(self) -> bool:
        try:
            serial = (self.vm_dir / "serial.log").read_text(errors="replace")
        except FileNotFoundError:
            return False
        return "OVERLAYFS_READY" in serial and "OVERLAYFS_FAILED" not in serial

    def replay_complete(self) -> bool:
        try:
            return "REPLAY_DONE" in (self.vm_dir / "serial.log").read_text(errors="replace")
        except FileNotFoundError:
            return False

    def replay_error(self) -> str | None:
        try:
            serial = (self.vm_dir / "serial.log").read_text(errors="replace")
            encoded = re.findall(r"REPLAY_ERROR ([A-Za-z0-9+/=]+)", serial)
            if encoded:
                return json.loads(base64.b64decode(encoded[-1]).decode()).get("error", "unknown guest error")
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            pass
        return None

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try: self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired: os.killpg(self.proc.pid, signal.SIGKILL)
        try: sudo("rmdir", str(self.cgroup))
        except subprocess.CalledProcessError: pass


def nearest_sample(samples: list[dict], timestamp: float, before: bool) -> dict | None:
    candidates = [row for row in samples if (row["timestamp"] <= timestamp) == before]
    return (max(candidates, key=lambda row: row["timestamp"]) if before else min(candidates, key=lambda row: row["timestamp"], default=None)) if candidates else None


def summarize_turns(vm: VM, samples: list[dict]) -> list[dict]:
    rows = []
    for turn in vm.read_jsonl("/replay/guest-turns.jsonl"):
        first, last = nearest_sample(samples, turn["started_at"], True), nearest_sample(samples, turn["finished_at"], False)
        within = [row for row in samples if turn["started_at"] <= row["timestamp"] <= turn["finished_at"]]
        rows.append({"session": vm.session, "instance_id": vm.manifest["instance_id"], **turn,
                     "cpu_seconds": ((last["cpu_usage_usec"] - first["cpu_usage_usec"]) / 1e6 if first and last else None),
                     "peak_cgroup_memory_bytes": max((r["cgroup_memory_bytes"] for r in within), default=None),
                     "peak_pss_bytes": max((r["pss_bytes"] for r in within), default=None),
                     "sample_count": len(within)})
    return rows


def sessions_for_stage(unique: list[dict], count: int, rng: random.Random) -> list[dict]:
    if count <= len(unique):
        return [dict(manifest, repetition=0) for manifest in unique[:count]]
    result = [dict(manifest, repetition=0) for manifest in unique]
    for manifest in rng.choices(unique, k=count - len(unique)):
        copy = dict(manifest)
        copy["repetition"] = 1
        result.append(copy)
    return result


def run_stage(stage_dir: Path, manifests: list[dict], args, rootfs: Path, kernel: Path,
              initramfs: Path,
              memory_threshold_bytes: int) -> dict:
    stage_dir.mkdir(parents=True)
    scratch_dir = args.scratch_root / f"swebench-scale-scratch-{os.getpid()}-{len(manifests)}"
    staging = scratch_dir / "staging"; staging.mkdir(parents=True)
    memory_ceiling = args.guest_memory_ceiling_mib or host_memory_bytes() // 1024**2
    vms = [VM(stage_dir, args.shared_image_root, index + 1, manifest, memory_ceiling, args.allow_swap)
           for index, manifest in enumerate(manifests)]
    (stage_dir / "run-plan.json").write_text(json.dumps({"concurrency": len(vms), "sessions": [v.manifest for v in vms], "seed": args.seed,
        "guest_visible_vcpus": len(cpuset()), "guest_memory_ceiling_mib": memory_ceiling,
        "resource_policy": "no per-session CPU or memory quota; shared all-NUMA host capacity",
        "host_memory_threshold_percent": args.memory_threshold_percent,
        "swap_policy": "allow" if args.allow_swap else "disabled",
        "guest_storage_policy": "shared read-only image plus per-VM guest tmpfs overlay",
        "host_numa_nodes": [node.name for node in numa_nodes()]}, indent=2) + "\n")
    try:
        for image in sorted({image_name(v.manifest["instance_id"]) for v in vms}):
            if host_used_bytes() >= memory_threshold_bytes:
                raise MemoryGuard("host memory guard reached while preparing images")
            sudo("docker", "pull", image)
        for vm in vms: vm.build_disk(staging, memory_threshold_bytes)
    except BaseException:
        shutil.rmtree(scratch_dir, ignore_errors=True)
        raise
    samples: dict[int, list[dict]] = {vm.session: [] for vm in vms}; stop = threading.Event()
    oom_guard = threading.Event()
    peak_host_used = [host_used_bytes()]
    def monitor() -> None:
        with (stage_dir / "session-samples.jsonl").open("w") as stream:
            while not stop.wait(args.sample_interval_seconds):
                for vm in vms:
                    row = vm.sample(); samples[vm.session].append(row); stream.write(json.dumps(row) + "\n")
                stream.flush()
                used = host_used_bytes()
                peak_host_used[0] = max(peak_host_used[0], used)
                if used >= memory_threshold_bytes:
                    oom_guard.set()
                    stream.write(json.dumps({"timestamp": time.time(), "event": "memory_guard", "host_used_bytes": used,
                                             "threshold_bytes": memory_threshold_bytes}) + "\n")
                    stream.flush()
    for vm in vms: vm.prepare_cgroup()
    network = StageNetwork(args.network_subnet, args.inference_endpoint, args.network_interface,
                           os.getpid(), [vm.session for vm in vms])
    try:
        network.setup()
    except BaseException:
        network.close()
        raise
    monitor_thread = threading.Thread(target=monitor, daemon=True); monitor_thread.start()
    started = time.time()
    try:
        # Start each VMM from a barrier so a stage has one common admission
        # instant, rather than a host-side sequential start ramp.
        launch_gate = threading.Barrier(len(vms))
        launch_errors: list[BaseException] = []
        def launch(vm: VM) -> None:
            try:
                launch_gate.wait()
                vm.start(rootfs, kernel, initramfs, network.config(vm.session), args.inference_endpoint,
                         args.inference_timeout_seconds, args.max_turns)
            except BaseException as exc:
                launch_errors.append(exc)
        launchers = [threading.Thread(target=launch, args=(vm,)) for vm in vms]
        for thread in launchers: thread.start()
        for thread in launchers: thread.join()
        if launch_errors: raise RuntimeError("failed to launch simultaneous stage") from launch_errors[0]
        while not all(vm.done() for vm in vms):
            if oom_guard.is_set():
                for vm in vms:
                    if not vm.done(): vm.stop()
                break
            time.sleep(0.2)
        if not oom_guard.is_set():
            invalid = [vm.session for vm in vms if not vm.overlay_ready()]
            if invalid:
                raise RuntimeError(f"guest OverlayFS was not mounted for sessions {invalid}")
            errors = {vm.session: vm.replay_error() for vm in vms if vm.replay_error()}
            incomplete = [vm.session for vm in vms if not vm.replay_complete()]
            if errors or incomplete:
                raise RuntimeError(f"remote inference failed: errors={errors}, incomplete_sessions={incomplete}")
    finally:
        stop.set(); monitor_thread.join(timeout=2)
        for vm in vms:
            samples[vm.session].append(vm.sample())
            vm.stop()
        network.close()
    turn_rows = [row for vm in vms for row in summarize_turns(vm, samples[vm.session])]
    (stage_dir / "turn-resource.jsonl").write_text("".join(json.dumps(row) + "\n" for row in turn_rows))
    result = {"concurrency": len(vms), "elapsed_seconds": time.time() - started, "sessions_completed": sum(vm.done() for vm in vms),
              "turns_measured": len(turn_rows), "memory_guard_triggered": oom_guard.is_set(),
              "peak_host_used_bytes": peak_host_used[0],
              "peak_aggregate_cgroup_memory_bytes": max((sum(r["cgroup_memory_bytes"] for r in group) for group in zip(*samples.values())), default=0)}
    (stage_dir / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    # Guest overlays disappear with the VM; reclaim only the regular staging tree.
    shutil.rmtree(scratch_dir, ignore_errors=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path,
                        help="experiment output directory (required for --run/plan; not needed by --serve)")
    parser.add_argument("--serve", action="store_true",
                        help="serve YIG workflows as the remote inference origin")
    parser.add_argument("--trace-root", type=Path, default=DEFAULT_TRACE_ROOT,
                        help="YIG trace root used by --serve")
    parser.add_argument("--serve-host", default="0.0.0.0")
    parser.add_argument("--serve-port", type=int, default=8080)
    parser.add_argument("--inference-endpoint", default=os.environ.get(
        "MICROVM_INFERENCE_ENDPOINT", DEFAULT_INFERENCE_ENDPOINT),
        help="remote trace-backed inference endpoint (default: %(default)s)")
    parser.add_argument("--instance-catalog-url",
                        help="GET endpoint returning the remote instance-id list; defaults to /v1/instances")
    parser.add_argument("--instance-ids-file", type=Path,
                        help="optional newline-delimited instance IDs; no local traces are read")
    parser.add_argument("--instance-id", dest="instance_ids", action="append", default=[],
                        help="explicit remote instance ID (repeatable; no local traces are read)")
    parser.add_argument("--stages", type=int, nargs="+", default=DEFAULT_STAGES)
    parser.add_argument("--seed", type=int, default=20261001)
    parser.add_argument("--sample-interval-seconds", type=float, default=0.25)
    parser.add_argument("--ram-root", type=Path, default=Path("/dev/shm"))
    parser.add_argument("--scratch-root", type=Path, default=Path("/tmp"),
                        help="regular filesystem used while exporting OCI images")
    parser.add_argument("--shared-image-root", type=Path,
                        help="regular filesystem cache for shared read-only Docker images")
    parser.add_argument("--memory-threshold-percent", type=float, default=90.0)
    parser.add_argument("--guest-memory-ceiling-mib", type=int)
    parser.add_argument("--inference-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--max-turns", type=int, default=1000)
    parser.add_argument("--network-subnet", default="10.250.0.0/24",
                        help="private per-stage VM subnet; it is NATed only to the inference host")
    parser.add_argument("--network-interface",
                        help="host uplink for inference NAT; defaults to the default IPv4 route")
    parser.add_argument("--prepare-guest", action="store_true")
    parser.add_argument("--allow-swap", action="store_true",
                        help="permit host and VM swap; default runs require swap to be disabled")
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    if args.serve:
        if args.run:
            raise SystemExit("--serve and --run are mutually exclusive")
        if not 1 <= args.serve_port <= 65535:
            raise SystemExit("serve port must be in [1, 65535]")
        serve_traces(args.trace_root, args.serve_host, args.serve_port)
        return
    if args.output is None:
        raise SystemExit("--output is required unless --serve is used")
    if tuple(args.stages) != DEFAULT_STAGES: raise SystemExit(f"stages must be exactly {DEFAULT_STAGES}")
    if args.sample_interval_seconds <= 0: raise SystemExit("sample interval must be positive")
    if not 1.0 <= args.memory_threshold_percent < 100.0: raise SystemExit("memory threshold must be in [1, 100)")
    if args.inference_timeout_seconds <= 0: raise SystemExit("inference timeout must be positive")
    if args.max_turns <= 0: raise SystemExit("max-turns must be positive")
    validate_inference_endpoint(args.inference_endpoint)
    rootfs_tree, rootfs = ROOT / "microvm-assets/rootfs-tree", ROOT / "microvm-assets/replay-rootfs.img"
    kernel = Path(os.environ.get("MICROVM_KERNEL", ROOT / "microvm-assets/vmlinuz"))
    initramfs = ensure_overlay_initramfs(kernel)
    # Provisioning the guest image is deliberately independent of the remote
    # corpus. A run/plan needs the catalog only to choose OCI instance images.
    if args.prepare_guest and not args.run:
        instance_ids = []
    else:
        instance_ids = load_instance_ids(args.inference_endpoint, args.instance_catalog_url,
                                         args.instance_ids_file, args.instance_ids)
    if args.run and not instance_ids:
        raise SystemExit("the remote catalog must contain at least one trace")
    unique = [{"instance_id": instance_id, "repetition": 0} for instance_id in instance_ids]
    rng = random.Random(args.seed); rng.shuffle(unique)
    args.output.mkdir(parents=True, exist_ok=False)
    plan = {"stages": list(args.stages), "remote_instances": len(unique), "seed": args.seed,
            "inference_endpoint": args.inference_endpoint,
            "instance_catalog_url": args.instance_catalog_url or catalog_url(args.inference_endpoint),
            "selection": "distinct traces until the remote pool is exhausted; seeded trace repeats above pool size",
            "guest_execution": "remote turn-by-turn inference; returned bash tools execute locally",
            "guest_storage": "shared read-only per-image block image plus per-VM guest tmpfs overlay; no trace data packaged",
            "guest_network": {"subnet": args.network_subnet, "nat_destination": urllib.parse.urlsplit(args.inference_endpoint).hostname},
            "guest_initramfs": str(initramfs),
            "memory_threshold_percent": args.memory_threshold_percent,
            "host_numa_nodes": [node.name for node in numa_nodes()],
            "host_cpus": cpuset(),
            "host_memory_bytes": host_memory_bytes()}
    (args.output / "experiment-plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    # Rebuild on every execution run so the guest behavior and host-side
    # result parser always match.  `--prepare-guest` supports a separate
    # provisioning-only invocation.
    args.shared_image_root = args.shared_image_root or args.output / "shared-images"
    if args.prepare_guest or args.run: prepare_guest(rootfs_tree, rootfs)
    if not args.run: print(args.output); return
    if not Path("/dev/kvm").exists() or not rootfs.is_file() or not kernel.is_file() or not initramfs.is_file(): raise SystemExit("--run requires /dev/kvm, a kernel, an OverlayFS initramfs, and a prepared replay rootfs")
    if not all(shutil.which(x) for x in ("cloud-hypervisor", "docker", "numactl", "ip", "iptables")): raise SystemExit("missing cloud-hypervisor, docker, numactl, ip, or iptables")
    if swap_bytes() and not args.allow_swap:
        raise SystemExit("swap is enabled; disable host swap before an in-memory run")
    memory_threshold_bytes = int(host_memory_bytes() * args.memory_threshold_percent / 100.0)
    results = []
    for count in args.stages:
        if host_used_bytes() >= memory_threshold_bytes:
            results.append({"concurrency": count, "skipped": True, "stop_reason": "host memory guard reached before admission"})
            break
        try:
            result = run_stage(args.output / f"sessions-{count:03d}", sessions_for_stage(unique, count, rng), args, rootfs, kernel, initramfs, memory_threshold_bytes)
            results.append(result)
            if result.get("memory_guard_triggered"):
                results.append({"skipped_remaining": True, "stop_reason": "host memory guard reached during stage"})
                break
        except MemoryGuard as exc:
            shutil.rmtree(args.scratch_root / f"swebench-scale-scratch-{os.getpid()}-{count}", ignore_errors=True)
            results.append({"concurrency": count, "skipped": True, "stop_reason": str(exc)})
            break
    (args.output / "experiment-summary.json").write_text(json.dumps(results, indent=2) + "\n")
    print(args.output)


if __name__ == "__main__": main()
