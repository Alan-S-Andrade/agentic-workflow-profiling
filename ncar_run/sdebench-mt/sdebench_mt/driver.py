"""Multi-tenant driver: N concurrent tenants share one vLLM server; each instance runs in
its own microVM. Tenants pull from a shared work queue (closed loop) until `--jobs`
instance runs have been started; starts are staggered to avoid a synchronized burst."""
import argparse, json, multiprocessing as mp, os, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sdebench_mt.vmm import ROOT
from sdebench_mt.tenant import run_tenant
from sdebench_mt.metrics import Sampler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenants", "-n", type=int, required=True)
    ap.add_argument("--jobs", type=int, default=0, help="instance runs to complete (default: 2*N, min 8)")
    ap.add_argument("--ids", default=str(ROOT / "configs/instances_ok.txt"))
    ap.add_argument("--instances", default=str(ROOT / "configs/instances.jsonl"))
    ap.add_argument("--agent-config", default=str(ROOT / "configs/mini.yaml"))
    ap.add_argument("--images", default=os.environ.get("IMAGES_DIR", str(ROOT / "vm/images")))
    ap.add_argument("--out", required=True)
    ap.add_argument("--vm-vcpus", type=int, default=2)
    ap.add_argument("--vm-mem-mb", type=int, default=4096)
    ap.add_argument("--vm-cpuset", default=os.environ.get("VM_CPUSET"))
    ap.add_argument("--vm-mem-node", type=int, default=0)
    ap.add_argument("--stagger-s", type=float, default=5.0)
    ap.add_argument("--min-mem-frac", type=float, default=0.10)
    ap.add_argument("--dispatch-deadline", type=float, default=0,
                    help="epoch seconds after which tenants start no new instance (0 = none)")
    ap.add_argument("--vllm-url", default=os.environ.get("VLLM_URL", "http://127.0.0.1:8000"))
    a = ap.parse_args()

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    ids = [l.strip() for l in open(a.ids) if l.strip()]
    jobs = a.jobs or max(8, 2 * a.tenants)
    order = [ids[i % len(ids)] for i in range(jobs)]  # round-robin over the instance set
    args = dict(out_dir=str(out), agent_config=a.agent_config, instances=a.instances, images=a.images,
                vm_dir=f"/tmp/{os.environ['USER']}-vms", vm_vcpus=a.vm_vcpus, vm_mem_mb=a.vm_mem_mb,
                vm_cpuset=a.vm_cpuset, vm_mem_node=a.vm_mem_node, min_mem_frac=a.min_mem_frac,
                dispatch_deadline=a.dispatch_deadline)
    (out / "config.json").write_text(json.dumps(vars(a) | {"jobs": jobs, "order": order}, indent=1))

    ctx = mp.get_context("spawn")
    q, results = ctx.Queue(), ctx.Queue()
    for iid in order:
        q.put(iid)
    for _ in range(a.tenants):
        q.put(None)
    sampler = Sampler(out, a.vllm_url)
    sampler.start()
    t0 = time.time()
    procs = []
    for t in range(a.tenants):
        p = ctx.Process(target=run_tenant, args=(t, q, args, results), daemon=False)
        p.start()
        procs.append(p)
        if t + 1 < a.tenants:
            time.sleep(a.stagger_s)
    done = []
    with open(out / "results.jsonl", "a") as f:
        while len(done) < jobs and any(p.is_alive() for p in procs) or not results.empty():
            try:
                r = results.get(timeout=5)
            except Exception:
                continue
            done.append(r)
            f.write(json.dumps({k: v for k, v in r.items() if k not in ("tb", "vm_tail")}) + "\n")
            f.flush()
            print(f"[{time.time()-t0:7.0f}s] {len(done)}/{jobs} t{r['tenant']:03d} {r['instance_id']} "
                  f"resolved={r.get('resolved')} wall={r.get('wall_s', 0):.0f}s calls={r.get('n_calls')} "
                  f"{r.get('error', '')[:120]}", flush=True)
    for p in procs:
        p.join()
    sampler.stop()
    n_res = sum(bool(r.get("resolved")) for r in done)
    summary = {"tenants": a.tenants, "jobs": len(done), "resolved": n_res, "makespan_s": time.time() - t0,
               "errors": sum(1 for r in done if r.get("error"))}
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    print("SUMMARY", json.dumps(summary))


if __name__ == "__main__":
    main()
