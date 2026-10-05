"""SWE-bench evaluation inside a tenant microVM, mirroring swebench.harness.run_evaluation.

The agent's working tree is reset to base_commit, the submitted patch is applied with
the harness's fallback sequence, eval.sh runs in the guest, and the log is graded with
swebench's own parser/report code (host side).
"""
import json, tempfile
from swebench.harness.constants import APPLY_PATCH_FAIL, APPLY_PATCH_PASS, CONTAINER_PATCH_FILE
from swebench.harness.grading import get_eval_report
from swebench.harness.run_evaluation import GIT_APPLY_CMDS
from swebench.harness.utils import make_test_spec

EVAL_TIMEOUT = 1800


def load_instances(path):
    return {x["instance_id"]: x for x in map(json.loads, open(path))}


def collect_patch(vm):
    """The agent's submission = working-tree diff against base (incl. new files)."""
    r = vm.exec("git add -A && git diff --cached --no-color HEAD", cwd="/testbed", timeout=120)
    return r["stdout"] if r["exit_status"] == 0 else ""


def evaluate(vm, instance, patch, timeout=EVAL_TIMEOUT):
    spec = make_test_spec(instance)
    out = {"instance_id": instance["instance_id"], "patch_bytes": len(patch.encode())}
    if not patch.strip():
        out.update(resolved=False, reason="empty_patch")
        return out
    base = instance["base_commit"]
    vm.exec(f"git reset -q --hard {base} && git clean -fdq", cwd="/testbed", timeout=300)
    vm.write_file(CONTAINER_PATCH_FILE, patch)
    applied = None
    for cmd in GIT_APPLY_CMDS:
        r = vm.exec(f"{cmd} {CONTAINER_PATCH_FILE}", cwd="/testbed", timeout=120)
        if r["exit_status"] == 0:
            applied = cmd
            break
    if not applied:
        out.update(resolved=False, reason="patch_apply_failed")
        log = APPLY_PATCH_FAIL
    else:
        vm.write_file("/eval.sh", spec.eval_script)
        r = vm.exec("/bin/bash /eval.sh 2>&1", cwd="/testbed", timeout=timeout)
        log = f"{APPLY_PATCH_PASS}:\n{applied}\n" + r["stdout"] + r["stderr"]
        out["eval_exit"] = r["exit_status"]
        out["eval_ms"] = r.get("duration_ms")
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
        f.write(log)
        log_path = f.name
    if applied:
        rep = get_eval_report(spec, {"instance_id": spec.instance_id, "model_patch": patch,
                                     "model_name_or_path": "qwen3.8-27b"}, log_path, include_tests_status=True)
        rep = rep[spec.instance_id]
        out.update(resolved=bool(rep["resolved"]), reason="graded", report=rep)
    out["log_path"] = log_path
    out["log_tail"] = log[-4000:]
    return out
