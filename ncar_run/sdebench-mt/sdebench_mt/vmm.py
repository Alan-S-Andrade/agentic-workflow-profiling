"""Launch and talk to one Cloud Hypervisor microVM per tenant (unprivileged, vsock only)."""
import json, os, shutil, socket, subprocess, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CH_BIN = Path(os.environ.get("CLOUD_HYPERVISOR_BIN", ROOT / "vm/bin/cloud-hypervisor"))
KERNEL = Path(os.environ.get("MICROVM_KERNEL", ROOT / "vm/kernel/Image"))
VSOCK_PORT = 52


class GuestError(RuntimeError):
    pass


class MicroVM:
    """One tenant VM: shared read-only rootfs (vda) + private blank upper disk (vdb)."""

    def __init__(self, name, rootfs, workdir, vcpus=2, mem_mb=4096, upper_gb=8, cpuset=None, mem_node=None):
        self.name, self.rootfs = name, Path(rootfs)
        self.dir = Path(workdir) / name
        self.vcpus, self.mem_mb, self.upper_gb = vcpus, mem_mb, upper_gb
        self.cpuset, self.mem_node = cpuset, mem_node
        self.proc = None
        self._conn = None

    @property
    def vsock_path(self):
        return self.dir / "vsock.sock"

    @property
    def pid(self):
        return self.proc.pid if self.proc else None

    def start(self, boot_timeout=120):
        if self.dir.exists():
            shutil.rmtree(self.dir)
        self.dir.mkdir(parents=True)
        upper = self.dir / "upper.ext4"
        with open(upper, "wb") as f:
            f.truncate(self.upper_gb << 30)  # sparse
        subprocess.run(["mkfs.ext4", "-q", "-F", "-b", "4096", "-E", "lazy_itable_init=1,lazy_journal_init=1", str(upper)], check=True)
        cmd = [str(CH_BIN), "--kernel", str(KERNEL),
               "--cmdline", "console=ttyAMA0 root=/dev/vda ro rootfstype=ext4 init=/sbin/replay-init quiet",
               "--disk", f"path={self.rootfs},readonly=on,image_type=raw", f"path={upper},image_type=raw",
               "--cpus", f"boot={self.vcpus}", "--memory", f"size={self.mem_mb}M",
               "--vsock", f"cid=3,socket={self.vsock_path}",
               "--api-socket", f"path={self.dir / 'api.sock'}",
               "--serial", f"file={self.dir / 'serial.log'}", "--console", "off"]
        if self.mem_node is not None and shutil.which("numactl"):
            cmd = ["numactl", f"--membind={self.mem_node}"] + cmd
        if self.cpuset:
            cmd = ["taskset", "-c", self.cpuset] + cmd
        self.boot_started = time.time()
        self.proc = subprocess.Popen(cmd, stdout=open(self.dir / "vmm.log", "wb"), stderr=subprocess.STDOUT)
        deadline = time.time() + boot_timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise GuestError(f"{self.name}: VMM exited rc={self.proc.returncode}: {self.tail()}")
            try:
                self.request({"op": "ping"}, timeout=5)
                self.boot_s = time.time() - self.boot_started
                return self
            except (OSError, GuestError):
                self._close()
                time.sleep(0.5)
        raise GuestError(f"{self.name}: guest agent not reachable after {boot_timeout}s: {self.tail()}")

    def tail(self, n=2000):
        out = ""
        for f in ("vmm.log", "serial.log"):
            p = self.dir / f
            if p.exists():
                out += f"\n--- {f}\n" + p.read_text(errors="replace")[-n:]
        return out

    def _connect(self, timeout):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(str(self.vsock_path))
        s.sendall(f"CONNECT {VSOCK_PORT}\n".encode())
        f = s.makefile("rwb")
        ack = f.readline()
        if not ack.startswith(b"OK"):
            s.close()
            raise GuestError(f"vsock handshake failed: {ack!r}")
        self._conn = (s, f)

    def _close(self):
        if self._conn:
            try:
                self._conn[0].close()
            except OSError:
                pass
        self._conn = None

    def request(self, req, timeout=None):
        """Send one JSON request on the persistent connection; return the JSON reply."""
        io_timeout = (timeout or req.get("timeout") or 600) + 30
        if self._conn is None:
            self._connect(io_timeout)
        s, f = self._conn
        s.settimeout(io_timeout)
        try:
            f.write(json.dumps(req).encode() + b"\n")
            f.flush()
            line = f.readline()
        except OSError:
            self._close()
            raise
        if not line:
            self._close()
            raise GuestError("guest closed connection")
        resp = json.loads(line)
        if "error" in resp:
            raise GuestError(resp["error"])
        return resp

    def exec(self, command, cwd="/testbed", timeout=600, env=None):
        return self.request({"op": "exec", "cwd": cwd, "command": command, "timeout": timeout, "env": env or {}})

    def write_file(self, path, content):
        return self.request({"op": "write_file", "path": path, "content": content})

    def stop(self, keep_dir=False):
        self._close()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        if not keep_dir and self.dir.exists():
            shutil.rmtree(self.dir, ignore_errors=True)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
