"""Calibrate the GPU cost of prefill on the running server (same flags as the run).

Measures time-to-first-token (max_tokens=1) on an otherwise idle server for
  - cold prompts of 2K..96K tokens (n new tokens, no cached context), and
  - incremental prompts: a cached base of c tokens plus n new tokens,
then fits   t(n, c) = t0 + a*n + b*n*(c + n/2)
(a: per-token cost of the linear-attention/MLP part, b: attention cost per token per context
token, t0: per-request overhead). recompute.py uses a*n + b*n*(c + n/2) as the GPU time of
re-prefilling n tokens that start at context position c.
Run after the workload (the prompts would otherwise pollute the prefix/CPU caches).
Usage: prefill_cost.py --url http://127.0.0.1:8000 --out <dir>/prefill_cost.json
"""
import argparse, json, random, string, time, urllib.request
import numpy as np

ap = argparse.ArgumentParser()
ap.add_argument("--url", default="http://127.0.0.1:8000")
ap.add_argument("--model", default="qwen3.8-27b")
ap.add_argument("--out", required=True)
ap.add_argument("--reps", type=int, default=2)
a = ap.parse_args()

rng = random.Random(4321)
TOK_PER_WORD = 3.1  # random lowercase words; exact counts come from the usage field


def words(n_tok, tag):
    return tag + " " + " ".join("".join(rng.choices(string.ascii_lowercase, k=rng.randint(3, 8)))
                                for _ in range(int(n_tok / TOK_PER_WORD)))


def call(prompt):
    body = json.dumps({"model": a.model, "prompt": prompt, "max_tokens": 1, "temperature": 0}).encode()
    req = urllib.request.Request(a.url + "/v1/completions", body, {"Content-Type": "application/json"})
    t = time.time()
    u = json.load(urllib.request.urlopen(req, timeout=1800))["usage"]
    dt = time.time() - t
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    return {"prompt": u["prompt_tokens"], "cached": cached, "n": u["prompt_tokens"] - cached, "c": cached, "t": dt}


pts = []
tiny = [call(words(16, f"TINY{i}")) for i in range(5)]
t0 = float(np.median([p["t"] for p in tiny]))
for rep in range(a.reps):
    for L in (2048, 8192, 16384, 32768, 65536, 98304):
        pts.append(call(words(L, f"COLD{rep}-{L}")) | {"kind": "cold"})
    for c in (16384, 49152, 98304):
        base = words(c, f"BASE{rep}-{c}")
        call(base)  # make the base resident (block-aligned part becomes cached)
        for d in (1568, 6272):
            pts.append(call(base + " " + words(d, f"DELTA{rep}-{c}-{d}")) | {"kind": "incremental"})

n = np.array([p["n"] for p in pts], float)
c = np.array([p["c"] for p in pts], float)
y = np.array([p["t"] for p in pts]) - t0
X = np.stack([n, n * (c + n / 2)], axis=1)
coef, *_ = np.linalg.lstsq(X, y, rcond=None)
coef = np.clip(coef, 0, None)
pred = X @ coef
out = {
    "model": "t(n,c) = t0 + a*n + b*n*(c + n/2)",
    "t0_s": t0, "a_s_per_tok": coef[0], "b_s_per_tok2": coef[1],
    "fit_mape": float(np.mean(np.abs(pred - y) / np.maximum(y, 1e-3))),
    "rate_tok_s": {f"n{nn}_c{cc}": nn / (coef[0] * nn + coef[1] * nn * (cc + nn / 2))
                   for nn, cc in [(1568, 0), (1568, 65536), (8192, 0), (32768, 0), (98304, 0)]},
    "points": pts, "tiny": tiny, "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
}
json.dump(out, open(a.out, "w"), indent=1)
print(f"PREFILL_COST t0={t0*1e3:.0f} ms  a={coef[0]*1e6:.1f} us/tok  b={coef[1]*1e12:.2f} ps/tok^2  "
      f"fit MAPE={out['fit_mape']:.1%}  rates(tok/s): " +
      ", ".join(f"{k}={v:,.0f}" for k, v in out["rate_tok_s"].items()))
