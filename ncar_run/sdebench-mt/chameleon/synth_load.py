"""Synthetic multi-turn agent load to validate the recompute measurement (not a benchmark result).

N tenants, each running conversations of growing context like an agent loop: a shared system
prefix + a per-conversation task, then per turn one completion (max_tokens output, EOS ignored),
append the output and a new random "observation", sleep a "tool" time, repeat until the context
limit, then start a new conversation. The total live context exceeds the GPU KV cache, which forces
evictions. Writes the driver's layout: <out>/tenants/tNNN/<conv>.events.jsonl, samples.jsonl,
gpu.csv (via sdebench_mt.metrics.Sampler) and config.json.
Usage: synth_load.py --out runs/<run>/N32 [--tenants 32 --duration-s 900 --ctx-limit 60000]
"""
import argparse, json, random, string, sys, threading, time, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sdebench_mt.metrics import Sampler

ap = argparse.ArgumentParser()
ap.add_argument("--url", default="http://127.0.0.1:8000")
ap.add_argument("--model", default="qwen3.8-27b")
ap.add_argument("--out", required=True)
ap.add_argument("--tenants", type=int, default=32)
ap.add_argument("--duration-s", type=float, default=900)
ap.add_argument("--ctx-limit", type=int, default=60000, help="tokens; then a new conversation starts")
ap.add_argument("--system-tokens", type=int, default=3000)
ap.add_argument("--task-tokens", type=int, default=2000)
ap.add_argument("--obs-tokens", default="300,3000", help="min,max new observation tokens per turn")
ap.add_argument("--max-tokens", type=int, default=300)
ap.add_argument("--tool-s", default="1,6", help="min,max sleep between turns (tool execution)")
ap.add_argument("--stagger-s", type=float, default=2)
a = ap.parse_args()
out = Path(a.out)
out.mkdir(parents=True, exist_ok=True)
(out / "config.json").write_text(json.dumps(vars(a) | {"synthetic": True}, indent=1))
obs_lo, obs_hi = map(int, a.obs_tokens.split(","))
tool_lo, tool_hi = map(float, a.tool_s.split(","))


def text(rng, n_tok, tag=""):
    return tag + " " + " ".join("".join(rng.choices(string.ascii_lowercase, k=rng.randint(3, 8)))
                                for _ in range(int(n_tok / 3.1)))


SYSTEM = text(random.Random(7), a.system_tokens, "SYSTEM")


def complete(prompt, seed):
    body = json.dumps({"model": a.model, "prompt": prompt, "max_tokens": a.max_tokens, "temperature": 0.6,
                       "ignore_eos": True, "seed": seed}).encode()
    req = urllib.request.Request(a.url + "/v1/completions", body, {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=1800))


def tenant(t, t_end):
    rng = random.Random(1000 + t)
    d = out / "tenants" / f"t{t:03d}"
    d.mkdir(parents=True, exist_ok=True)
    k = 0
    while time.time() < t_end:
        conv = f"synth-t{t:03d}-c{k}"
        k += 1
        prompt = SYSTEM + text(rng, a.task_tokens, f"TASK {conv}")
        with open(d / f"{conv}.events.jsonl", "a", buffering=1) as log:
            turn = 0
            while time.time() < t_end:
                t0 = time.time()
                rec = {"tenant": t, "instance_id": conv, "kind": "llm", "started_at": t0, "n_messages": 2 * turn + 1}
                try:
                    r = complete(prompt, rng.randint(0, 2**31))
                    u = r["usage"]
                    rec |= {"ok": True, "prompt_tokens": u["prompt_tokens"], "completion_tokens": u["completion_tokens"],
                            "cached_tokens": (u.get("prompt_tokens_details") or {}).get("cached_tokens")}
                    out_text = r["choices"][0]["text"]
                except Exception as e:
                    rec |= {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
                    out_text = ""
                rec["duration_ms"] = (time.time() - t0) * 1000
                log.write(json.dumps(rec) + "\n")
                if not rec["ok"] or rec["prompt_tokens"] + a.max_tokens + obs_hi > a.ctx_limit:
                    break
                time.sleep(rng.uniform(tool_lo, tool_hi))
                prompt += out_text + text(rng, rng.randint(obs_lo, obs_hi), f"\nOBS{turn}")
                turn += 1


sampler = Sampler(out, a.url)
sampler.start()
t_end = time.time() + a.duration_s
ths = []
for t in range(a.tenants):
    th = threading.Thread(target=tenant, args=(t, t_end), daemon=True)
    th.start()
    ths.append(th)
    time.sleep(a.stagger_s)
for th in ths:
    th.join()
sampler.stop()
n = sum(1 for f in (out / "tenants").glob("*/*.events.jsonl") for _ in open(f))
(out / "summary.json").write_text(json.dumps({"tenants": a.tenants, "llm_calls": n, "synthetic": True}))
print(f"SYNTH done: {n} calls in {a.duration_s:.0f}s with {a.tenants} tenants")
