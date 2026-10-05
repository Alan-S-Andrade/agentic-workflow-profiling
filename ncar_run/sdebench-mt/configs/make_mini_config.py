"""Write configs/mini.yaml = mini-swe-agent's SWE-bench benchmark config + local overrides."""
import sys
from pathlib import Path
import yaml
import minisweagent

base = yaml.safe_load((Path(minisweagent.__file__).parent / "config/benchmarks/swebench.yaml").read_text())
port = sys.argv[1] if len(sys.argv) > 1 else "8000"
base["agent"].update(step_limit=75, cost_limit=0, wall_time_limit_seconds=3600)
for k in ("environment_class", "interpreter"):
    base["environment"].pop(k, None)
base["environment"].update(cwd="/testbed", timeout=180)
base["model"].update(
    model_name="hosted_vllm/qwen3.8-27b",
    cost_tracking="ignore_errors",
    model_kwargs={
        "api_base": f"http://127.0.0.1:{port}/v1",
        "api_key": "EMPTY",
        "temperature": 0.6,
        "top_p": 0.95,
        "max_tokens": 16384,
        "drop_params": True,
        "timeout": 1800,
        "num_retries": 2,
    },
)
base["model"].pop("model_class", None)
out = Path(__file__).with_name("mini.yaml")
out.write_text(yaml.safe_dump(base, sort_keys=False, width=1000))
print(out)
