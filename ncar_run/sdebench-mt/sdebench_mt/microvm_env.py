"""mini-swe-agent Environment that executes each action inside the tenant's microVM."""
import time
from typing import Any

from pydantic import BaseModel

from minisweagent.environments.local import LocalEnvironment


class MicroVMEnvironmentConfig(BaseModel):
    cwd: str = "/testbed"
    env: dict[str, str] = {}
    timeout: int = 60


class MicroVMEnvironment(LocalEnvironment):
    def __init__(self, vm, *, on_exec=None, **kwargs):
        super().__init__(config_class=MicroVMEnvironmentConfig, **kwargs)
        self.vm = vm
        self.on_exec = on_exec  # callback(record) for per-tool timing

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        command = action.get("command", "")
        t0 = time.time()
        try:
            r = self.vm.exec(command, cwd=cwd or self.config.cwd, timeout=timeout or self.config.timeout,
                             env=self.config.env)
            timed_out = r["exit_status"] == -9 and "[replay-agent] timeout" in r["stderr"]
            output = {"output": r["stdout"] + r["stderr"], "returncode": r["exit_status"],
                      "exception_info": f"Command timed out after {timeout or self.config.timeout}s" if timed_out else ""}
        except Exception as e:
            output = {"output": "", "returncode": -1,
                      "exception_info": f"An error occurred while executing the command: {e}",
                      "extra": {"exception_type": type(e).__name__, "exception": str(e)}}
        if self.on_exec:
            self.on_exec({"kind": "tool", "started_at": t0, "duration_ms": (time.time() - t0) * 1000,
                          "returncode": output["returncode"], "out_bytes": len(output["output"]),
                          "command": command[:500]})
        self._check_finished(output)
        return output

    def get_template_vars(self, **kwargs) -> dict[str, Any]:
        return self.config.model_dump() | kwargs
