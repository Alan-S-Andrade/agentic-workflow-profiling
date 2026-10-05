"""Background samplers: vLLM Prometheus metrics, GPU (nvidia-smi), host + per-NUMA-node memory,
vLLM process-tree memory (CPU KV buffer, pinned weights) and per-VMM memory/CPU."""
import json, os, re, subprocess, threading, time, urllib.request
from pathlib import Path

VLLM_KEEP = re.compile(
    r"^vllm:(num_requests_running|num_requests_waiting|kv_cache_usage_perc|gpu_cache_usage_perc|"
    r"prefix_cache_queries_total|prefix_cache_hits_total|prompt_tokens_total|generation_tokens_total|"
    r"num_preemptions_total|request_success_total|"
    r"time_to_first_token_seconds_(sum|count)|inter_token_latency_seconds_(sum|count)|"
    r"time_per_output_token_seconds_(sum|count)|e2e_request_latency_seconds_(sum|count)|"
    r"request_queue_time_seconds_(sum|count)|request_prefill_time_seconds_(sum|count)|"
    r"request_decode_time_seconds_(sum|count)|spec_decode_num_accepted_tokens_total|"
    r"spec_decode_num_draft_tokens_total|external_prefix_cache_(queries|hits)_total|"
    r"[a-z_]*offload[a-z_]*|[a-z_]*kv_transfer[a-z_]*|cpu_[a-z_]*)\b")
CLK = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")


def scrape_vllm(url):
    txt = urllib.request.urlopen(url + "/metrics", timeout=5).read().decode()
    out = {}
    for line in txt.splitlines():
        if line.startswith("#") or not VLLM_KEEP.match(line):
            continue
        name, _, val = line.rpartition(" ")
        key = name.split("{")[0][5:]
        out[key] = out.get(key, 0.0) + float(val)  # sum over label sets (e.g. engine)
    return out


def meminfo(path="/proc/meminfo"):
    d = {}
    for l in open(path):
        parts = l.replace(":", " ").split()
        # node meminfo lines start with "Node <n>"
        if parts[0] == "Node":
            parts = parts[2:]
        d[parts[0]] = int(parts[1])
    return d


def proc_table():
    """One /proc scan: pid -> (comm, ppid, rss_kb, cpu_ticks)."""
    out = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            comm = open(f"/proc/{pid}/comm").read().strip()
            st = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
            out[int(pid)] = (comm, int(st[1]), int(st[21]) * PAGE // 1024, int(st[11]) + int(st[12]))
        except (OSError, IndexError, ValueError):
            pass
    return out


def vmm_procs(table=None):
    """cloud-hypervisor processes: pid -> (rss_kb, cpu_ticks)."""
    table = proc_table() if table is None else table
    return {p: (r, c) for p, (comm, _, r, c) in table.items() if comm == "cloud-hyperviso"}


def tree_memory(table, root):
    """RssAnon/RssShmem/RssFile (kB) of `root` and all its descendants, total and per comm.
    For vLLM: the CPU KV offload buffer is shared memory (RssShmem); pinned host buffers
    (UVA-offloaded weights, LMCache) and heaps are RssAnon."""
    kids = {}
    for p, (_, pp, _, _) in table.items():
        kids.setdefault(pp, []).append(p)
    todo, seen = [root], set()
    while todo:
        p = todo.pop()
        if p in seen:
            continue
        seen.add(p)
        todo.extend(kids.get(p, []))
    tot, per = {"anon": 0, "shmem": 0, "file": 0}, {}
    for p in seen:
        try:
            st = {l.split(":")[0]: int(l.split()[1]) for l in open(f"/proc/{p}/status") if l.startswith("Rss")}
        except (OSError, ValueError, IndexError):
            continue
        v = {"anon": st.get("RssAnon", 0), "shmem": st.get("RssShmem", 0), "file": st.get("RssFile", 0)}
        name = table.get(p, ("?",))[0]
        for k in tot:
            tot[k] += v[k]
            per.setdefault(name, {"anon": 0, "shmem": 0, "file": 0})[k] += v[k]
    return tot | {"n_procs": len(seen), "by_comm": per}


NODE_KEYS = ("MemFree", "MemUsed", "FilePages", "AnonPages", "Shmem")


class Sampler:
    def __init__(self, out_dir, vllm_url, period=1.0):
        self.out, self.url, self.period = Path(out_dir), vllm_url, period
        self._stop = threading.Event()
        self.gpu = None

    def start(self):
        self.gpu = subprocess.Popen(
            ["nvidia-smi", "--query-gpu=timestamp,utilization.gpu,utilization.memory,memory.used,power.draw,clocks.sm",
             "--format=csv,noheader,nounits", "-lms", str(int(self.period * 1000))],
            stdout=open(self.out / "gpu.csv", "w"), stderr=subprocess.DEVNULL)
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def stop(self):
        self._stop.set()
        self.t.join(10)
        if self.gpu:
            self.gpu.terminate()

    def _loop(self):
        node0 = "/sys/devices/system/node/node0/meminfo"
        with open(self.out / "samples.jsonl", "w", buffering=1) as f:
            while not self._stop.is_set():
                t = time.time()
                rec = {"t": t}
                try:
                    rec["vllm"] = scrape_vllm(self.url)
                except Exception as e:
                    rec["vllm_error"] = str(e)[:200]
                m = meminfo()
                rec["mem_total_kb"], rec["mem_avail_kb"] = m["MemTotal"], m["MemAvailable"]
                if os.path.exists(node0):
                    n = meminfo(node0)
                    rec["node0_free_kb"], rec["node0_total_kb"] = n["MemFree"], n["MemTotal"]
                # Per NUMA node (0 = Grace LPDDR5X, 1 = GPU HBM on GH200), kB.
                rec["node"] = {}
                for nd in sorted(Path("/sys/devices/system/node").glob("node[0-9]*")):
                    n = meminfo(nd / "meminfo")
                    rec["node"][nd.name[4:]] = {k: n.get(k, 0) for k in NODE_KEYS}
                table = proc_table()
                # vLLM process tree (apptainer starter pid written by serve_vllm.sh to $VLLM_OUT/vllm.pid).
                pidf = Path(os.environ.get("VLLM_OUT", "")) / "vllm.pid"
                try:
                    rec["vllm_mem_kb"] = tree_memory(table, int(pidf.read_text().split()[0]))
                except (OSError, ValueError, IndexError):
                    pass
                v = vmm_procs(table)
                rec["n_vmm"] = len(v)
                rec["vmm_rss_kb"] = sum(x[0] for x in v.values())
                rec["vmm_cpu_ticks"] = sum(x[1] for x in v.values())
                rec["vmm"] = {str(p): x for p, x in v.items()}
                rec["loadavg"] = float(open("/proc/loadavg").read().split()[0])
                f.write(json.dumps(rec) + "\n")
                self._stop.wait(max(0.0, self.period - (time.time() - t)))
