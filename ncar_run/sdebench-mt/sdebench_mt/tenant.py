"""One tenant: pull SWE-bench instances from a shared queue; for each, boot a private
microVM, run mini-swe-agent against the shared vLLM server, evaluate, tear down."""
import json, os, threading, time, traceback
from pathlib import Path

import yaml

from sdebench_mt.vmm import MicroVM, ROOT
from sdebench_mt.swebench_eval import collect_patch, evaluate


class EventLog:
    def __init__(self, path, **tags):
        self.f = open(path, "a", buffering=1)
        self.tags = tags
        self.lock = threading.Lock()

    def __call__(self, rec):
        with self.lock:
            self.f.write(json.dumps({**self.tags, **rec}) + "\n")


def make_model(cfg, log):
    from minisweagent.models.litellm_model import LitellmModel

    class TimedModel(LitellmModel):
        """Records wall time and token usage of every chat completion."""

        def _query(self, messages, **kwargs):
            t0 = time.time()
            rec = {"kind": "llm", "started_at": t0, "n_messages": len(messages)}
            try:
                resp = super()._query(messages, **kwargs)
                u = getattr(resp, "usage", None)
                if u is not None:
                    rec["prompt_tokens"] = getattr(u, "prompt_tokens", None)
                    rec["completion_tokens"] = getattr(u, "completion_tokens", None)
                    det = getattr(u, "prompt_tokens_details", None)
                    rec["cached_tokens"] = getattr(det, "cached_tokens", None) if det else None
                    cdet = getattr(u, "completion_tokens_details", None)
                    rec["reasoning_tokens"] = getattr(cdet, "reasoning_tokens", None) if cdet else None
                rec["ok"] = True
                return resp
            except Exception as e:
                rec.update(ok=False, error=f"{type(e).__name__}: {e}"[:500])
                raise
            finally:
                rec["duration_ms"] = (time.time() - t0) * 1000
                log(rec)

    return TimedModel(**cfg)


def mem_available_frac(node=0):
    """Reclaimable fraction of NUMA node `node` (Grace LPDDR; node1 is the GPU's HBM).
    Node 0 holds tenant VM memory and, in the offload arm, vLLM's CPU KV cache."""
    path = f"/sys/devices/system/node/node{node}/meminfo"
    info = {}
    for l in open(path):
        parts = l.replace(":", " ").split()
        info[parts[2]] = int(parts[3])
    return (info["MemFree"] + info.get("Inactive(file)", 0)) / info["MemTotal"]


def run_tenant(tenant_id, queue, args, results_q):
    """Process entry point. `args` is a plain dict (picklable)."""
    from minisweagent.agents.default import DefaultAgent
    from sdebench_mt.microvm_env import MicroVMEnvironment

    os.environ.setdefault("MSWEA_COST_TRACKING", "ignore_errors")
    out = Path(args["out_dir"]) / "tenants" / f"t{tenant_id:03d}"
    out.mkdir(parents=True, exist_ok=True)
    cfg = yaml.safe_load(open(args["agent_config"]))
    insts = {x["instance_id"]: x for x in map(json.loads, open(args["instances"]))}
    while True:
        if args.get("dispatch_deadline") and time.time() > args["dispatch_deadline"]:
            return  # leave the rest of the queue; in-flight instances already finished
        iid = queue.get()
        if iid is None:
            return
        while mem_available_frac() < args["min_mem_frac"]:  # admission control
            time.sleep(2)
        log = EventLog(out / f"{iid}.events.jsonl", tenant=tenant_id, instance_id=iid)
        res = {"tenant": tenant_id, "instance_id": iid, "t_start": time.time()}
        vm = MicroVM(f"t{tenant_id:03d}-{iid}", f"{args['images']}/{iid}.ext4", args["vm_dir"],
                     vcpus=args["vm_vcpus"], mem_mb=args["vm_mem_mb"], cpuset=args.get("vm_cpuset"),
                     mem_node=args.get("vm_mem_node"))
        try:
            vm.start()
            res["boot_s"] = vm.boot_s
            res["vmm_pid"] = vm.pid
            log({"kind": "vm_boot", "started_at": vm.boot_started, "duration_ms": vm.boot_s * 1000, "vmm_pid": vm.pid})
            model = make_model(cfg["model"], log)
            env = MicroVMEnvironment(vm, on_exec=log, **cfg["environment"])
            agent = DefaultAgent(model, env, **(cfg["agent"] | {"output_path": out / f"{iid}.traj.json"}))
            t0 = time.time()
            info = agent.run(insts[iid]["problem_statement"])
            res["agent_s"] = time.time() - t0
            res["exit_status"] = info.get("exit_status")
            res["n_calls"] = agent.n_calls
            patch = info.get("submission") or ""
            if not patch.strip():  # fall back to the working-tree diff (e.g. step limit hit)
                patch = collect_patch(vm)
                res["patch_source"] = "worktree"
            t0 = time.time()
            ev = evaluate(vm, insts[iid], patch)
            res["eval_s"] = time.time() - t0
            res.update(resolved=ev["resolved"], eval_reason=ev["reason"], patch_bytes=ev["patch_bytes"])
            (out / f"{iid}.patch").write_text(patch)
        except Exception as e:
            res.update(resolved=False, error=f"{type(e).__name__}: {e}"[:2000], tb=traceback.format_exc()[-3000:])
            try:
                res["vm_tail"] = vm.tail(1500)
            except Exception:
                pass
        finally:
            vm.stop()
            res["t_end"] = time.time()
            res["wall_s"] = res["t_end"] - res["t_start"]
            (out / f"{iid}.result.json").write_text(json.dumps(res, indent=1))
            results_q.put(res)
