"""Direct check that KV blocks migrate GPU -> CPU -> GPU (vLLM native CPU offloading).

1. Send a unique long prompt A (cold: no cached tokens).
2. Send A again (expect a GPU prefix-cache hit).
3. Flood the server with distinct prompts totalling more than the GPU KV capacity (~810K tokens)
   but less than the CPU cache (64 GiB ~ 1.98M tokens at ~34.7 KB/token incl. mamba state), which
   evicts A's blocks from HBM while they survive in the CPU cache (LRU).
4. Send A again. Offload arm: cached tokens come back via the connector (external prefix-cache
   hits rise, TTFT stays low). Baseline arm: A is recomputed.
Usage: offload_check.py [--url http://127.0.0.1:8000] [--flood-tokens 1300000]
"""
import argparse, json, random, re, string, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

ap = argparse.ArgumentParser()
ap.add_argument("--url", default="http://127.0.0.1:8000")
ap.add_argument("--model", default="qwen3.8-27b")
ap.add_argument("--a-words", type=int, default=16000, help="words in prompt A (~3.1 tokens/word)")
ap.add_argument("--flood-tokens", type=int, default=1_300_000,
                help="> GPU KV capacity (~810K) and < CPU offload capacity (~1.98M at 64 GiB)")
ap.add_argument("--flood-words", type=int, default=20000)
ap.add_argument("--flood-par", type=int, default=8)
a = ap.parse_args()

rng = random.Random(1234)
def text(n, tag):
    return tag + " " + " ".join("".join(rng.choices(string.ascii_lowercase, k=rng.randint(3, 8))) for _ in range(n))

def complete(prompt):
    body = json.dumps({"model": a.model, "prompt": prompt, "max_tokens": 1, "temperature": 0}).encode()
    req = urllib.request.Request(a.url + "/v1/completions", body, {"Content-Type": "application/json"})
    t = time.time()
    r = json.load(urllib.request.urlopen(req, timeout=1800))
    u = r["usage"]
    return {"prompt_tokens": u["prompt_tokens"], "cached_tokens": (u.get("prompt_tokens_details") or {}).get("cached_tokens"),
            "latency_s": round(time.time() - t, 3)}

def metrics():
    txt = urllib.request.urlopen(a.url + "/metrics", timeout=10).read().decode()
    keep = {}
    for line in txt.splitlines():
        m = re.match(r"^(vllm:(?:external_prefix_cache_(?:hits|queries)_total|prefix_cache_(?:hits|queries)_total|"
                     r"kv_offload_[a-z_]+|num_preemptions_total))(?:\{[^}]*\})? ([0-9.e+-]+)$", line)
        if m:
            keep[m.group(1)] = keep.get(m.group(1), 0) + float(m.group(2))
    return keep

A = text(a.a_words, "PROMPT-A")
out = {"metrics_start": metrics()}
out["A_cold"] = complete(A)
out["A_gpu_hit"] = complete(A)
tok_per_word = out["A_cold"]["prompt_tokens"] / a.a_words      # random words tokenize at ~3.1
n_flood = -(-a.flood_tokens // int(a.flood_words * tok_per_word))
floods = [text(a.flood_words, f"FLOOD-{i}") for i in range(n_flood)]
t = time.time()
with ThreadPoolExecutor(a.flood_par) as ex:
    fl = list(ex.map(complete, floods))
out["flood"] = {"requests": len(fl), "prompt_tokens": sum(x["prompt_tokens"] for x in fl), "wall_s": round(time.time() - t, 1)}
m_before = metrics()
out["A_after_eviction"] = complete(A)
m_after = metrics()
out["metrics_end"] = m_after
out["delta_during_A_after_eviction"] = {k: m_after.get(k, 0) - m_before.get(k, 0) for k in m_after
                                        if m_after.get(k, 0) != m_before.get(k, 0)}
print(json.dumps(out, indent=1))
A3, A1 = out["A_after_eviction"], out["A_cold"]
ext = out["delta_during_A_after_eviction"].get("vllm:external_prefix_cache_hits_total", 0)
print(f"\nOFFLOAD_CHECK prompt={A1['prompt_tokens']} tok | cold {A1['latency_s']}s cached={A1['cached_tokens']} | "
      f"gpu-hit {out['A_gpu_hit']['latency_s']}s cached={out['A_gpu_hit']['cached_tokens']} | "
      f"after flood of {out['flood']['prompt_tokens']} tok: {A3['latency_s']}s cached={A3['cached_tokens']} "
      f"external_hits+={ext:.0f} -> {'MIGRATED BACK FROM CPU' if ext > 0 else 'RECOMPUTED (no CPU hit)'}")
m0, m1 = out["metrics_start"], out["metrics_end"]
gb = lambda k: (m1.get(k, 0) - m0.get(k, 0)) / 1e9
print(f"OFFLOAD_BYTES stored {gb('vllm:kv_offload_store_bytes_total'):.1f} GB, "
      f"loaded {gb('vllm:kv_offload_load_bytes_total'):.2f} GB during the check")
