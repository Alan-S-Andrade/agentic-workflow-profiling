"""Boot each instance's microVM and verify the gold patch evaluates as resolved."""
import argparse, json, os, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sdebench_mt.vmm import MicroVM, ROOT
from sdebench_mt.swebench_eval import evaluate, load_instances

ap = argparse.ArgumentParser()
ap.add_argument("ids", nargs="*")
ap.add_argument("--instances", default=str(ROOT / "configs/instances.jsonl"))
ap.add_argument("--images", default=os.environ.get("IMAGES_DIR", str(ROOT / "vm/images")))
ap.add_argument("--out", default=str(ROOT / "configs/gold_check.jsonl"))
a = ap.parse_args()
insts = load_instances(a.instances)
ids = a.ids or list(insts)
with open(a.out, "a") as f:
    for iid in ids:
        vm = MicroVM(f"gold-{iid}", f"{a.images}/{iid}.ext4", f"/tmp/{os.environ['USER']}-vms", vcpus=4, mem_mb=8192)
        t = time.time()
        try:
            vm.start()
            r = evaluate(vm, insts[iid], insts[iid]["patch"])
            r["boot_s"] = vm.boot_s
        except Exception as e:
            r = {"instance_id": iid, "resolved": False, "reason": f"error: {e}"}
        finally:
            vm.stop()
        r["wall_s"] = time.time() - t
        print(f"GOLD {iid} resolved={r['resolved']} reason={r['reason']} wall={r['wall_s']:.0f}s", flush=True)
        if not r["resolved"]:
            print(r.get("log_tail", "")[-1500:], flush=True)
        r.pop("log_tail", None)
        f.write(json.dumps(r) + "\n")
