#!/usr/bin/env python3
import asyncio
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODEL = "local-llama"

from node_trace import async_execution_node, execution_node, instrument_callable


def load_env() -> None:
    global MODEL
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())

    base_url = os.getenv("PROFILE_OPENAI_BASE_URL", "http://localhost:8080/v1").rstrip("/")
    MODEL = os.getenv("WORKFLOW_MODEL", "local-llama")
    key = os.getenv("OPENAI_API_KEY", "")
    if not key:
        if base_url.startswith(("http://127.0.0.1", "http://localhost")):
            key = "sk-local-llama"
        else:
            raise SystemExit("Set OPENAI_API_KEY for the configured OpenAI-compatible endpoint")
    os.environ.update(
        OPENAI_API_KEY=key,
        OPENAI_BASE_URL=base_url,
        OPENAI_API_BASE_URL=base_url,
        FAST_LLM=f"openai:{MODEL}",
        SMART_LLM=f"openai:{MODEL}",
        STRATEGIC_LLM=f"openai:{MODEL}",
        RETRIEVER="duckduckgo",
        EMBEDDING="huggingface:sentence-transformers/all-MiniLM-L6-v2",
        REASONING_EFFORT="low",
    )


def run_browser(task: str) -> None:
    sys.path.insert(0, str(ROOT / "browser-use"))
    from browser_use import Agent, ChatOpenAI

    async def main() -> None:
        llm = ChatOpenAI(
            model=MODEL,
            base_url=os.environ["OPENAI_BASE_URL"],
            reasoning_effort="none",
            max_completion_tokens=512,
        )
        async with async_execution_node("agent", "browser-use.agent_loop"):
            await Agent(
                task=task,
                llm=llm,
                use_vision=False,
                use_thinking=False,
                flash_mode=True,
                max_failures=2,
            ).run(max_steps=5)

    asyncio.run(main())


def run_research(query: str) -> None:
    sys.path.insert(0, str(ROOT / "gpt-researcher"))
    from gpt_researcher import GPTResearcher
    from gpt_researcher.scraper.scraper import Scraper
    from gpt_researcher.skills.researcher import ResearchConductor

    # GPT Researcher uses asyncio.gather for subqueries and URL retrieval. Wrap
    # those boundaries in the shared tracer so its graph retains real fan-out
    # rather than collapsing everything into one outer research span.
    if not getattr(ResearchConductor, "_profile_instrumented", False):
        ResearchConductor.plan_research = instrument_callable(
            "control", "gpt-researcher.plan_research", ResearchConductor.plan_research
        )
        ResearchConductor._get_context_by_web_search = instrument_callable(
            "control", "gpt-researcher.web_search", ResearchConductor._get_context_by_web_search
        )
        ResearchConductor._process_sub_query = instrument_callable(
            "agent", "gpt-researcher.subquery", ResearchConductor._process_sub_query
        )
        Scraper.run = instrument_callable("tool", "gpt-researcher.scrape_batch", Scraper.run)
        Scraper.extract_data_from_url = instrument_callable(
            "tool", "gpt-researcher.scrape_url", Scraper.extract_data_from_url
        )
        ResearchConductor._profile_instrumented = True

    async def main() -> None:
        async with async_execution_node("agent", "gpt-researcher.conduct_research") as research_span:
            researcher = GPTResearcher(query=query)
            await researcher.conduct_research()
        async with async_execution_node("control", "gpt-researcher.write_report", depends_on=[research_span]):
            print(await researcher.write_report())

    asyncio.run(main())


def run_speculative_tools(task: str) -> None:
    sys.path.insert(0, str(ROOT / "speculative-tools" / "examples"))
    sys.path.insert(0, str(ROOT / "speculative-tools"))
    from openai import AsyncOpenAI
    from openai_agent_demo import TOOL_SCHEMAS, get_alerts, get_forecast, get_weather
    from speculative_tools.openai_adapter import OpenAISpeculativeAdapter

    async def main() -> None:
        client = AsyncOpenAI(
            api_key=os.environ["OPENAI_API_KEY"],
            base_url=os.environ["OPENAI_BASE_URL"],
        )
        adapter = OpenAISpeculativeAdapter(min_confidence=0.25, max_speculations=3)
        functions = {
            "get_weather": get_weather,
            "get_forecast": get_forecast,
            "get_alerts": get_alerts,
        }
        for schema in TOOL_SCHEMAS:
            name = schema["function"]["name"]
            adapter.register_tool(name, instrument_callable("tool", f"speculative-tools.{name}", functions[name]), schema=schema)

        async with async_execution_node("agent", "speculative-tools.agent_loop"):
            result = await adapter.run_agent_loop(
                client,
                [{"role": "user", "content": task}],
                model=MODEL,
                max_iterations=5,
            )
        final_message = result["messages"][-1]
        content = final_message.get("content") if isinstance(final_message, dict) else None
        if content:
            print(content)
        stats = result["stats"]
        print(
            f"tool_calls={result['tool_calls']} "
            f"speculation_hits={stats.speculation_hits} "
            f"speculation_misses={stats.speculation_misses}"
        )

    asyncio.run(main())


def configure_metagpt() -> None:
    target = Path.home() / ".metagpt" / "config2.yaml"
    target.parent.mkdir(mode=0o700, exist_ok=True)
    target.write_text(
        "llm:\n"
        "  api_type: openai\n"
        f"  model: {MODEL}\n"
        f"  base_url: {os.environ['OPENAI_BASE_URL']}\n"
        f"  api_key: {os.environ['OPENAI_API_KEY']}\n"
    )
    target.chmod(0o600)


def exec_in(directory: str, executable: str, args: list[str]) -> None:
    # Make the shared tracer importable by the independently-launched
    # workflow interpreter.  SWE-agent's local environment hook uses it to
    # emit one resource span for every shell action.
    existing_pythonpath = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = f"{ROOT}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else str(ROOT)
    with execution_node("workflow", directory):
        completed = subprocess.run([executable, *args], cwd=ROOT / directory, env=os.environ)
    raise SystemExit(completed.returncode)


def run_openhands() -> None:
    executable = ROOT / ".tools/openhands/node_modules/.bin/agent-canvas"
    bundled_node = ROOT / ".tools/node-v22.22.0-linux-x64/bin"
    if not executable.exists():
        raise SystemExit("OpenHands Agent Canvas is not installed; rerun ./setup.sh")
    if not (bundled_node / "node").exists():
        raise SystemExit("OpenHands requires Node.js 22; rerun ./setup.sh to install the bundled runtime")
    env = os.environ.copy()
    env["PATH"] = f"{bundled_node}:{Path.home() / '.local/bin'}:{env['PATH']}"
    process = subprocess.Popen([str(executable)], cwd=ROOT / "openhands", env=env)
    try:
        deadline = time.monotonic() + float(os.getenv("OPENHANDS_STARTUP_TIMEOUT", "60"))
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise SystemExit(f"OpenHands Agent Canvas exited during startup ({process.returncode})")
            try:
                with urllib.request.urlopen("http://127.0.0.1:8000", timeout=2):
                    print("OpenHands Agent Canvas is ready at http://localhost:8000", flush=True)
                    if os.getenv("OPENHANDS_START_ONLY") == "1":
                        process.terminate()
                        process.wait(timeout=10)
                        raise SystemExit(0)
                    returncode = process.wait()
                    raise SystemExit(returncode)
            except (urllib.error.URLError, TimeoutError, OSError):
                time.sleep(1)
        process.terminate()
        raise SystemExit("OpenHands Agent Canvas did not become ready within startup timeout")
    except BaseException:
        if process.poll() is None:
            process.terminate()
        raise


def ensure_venv_python(directory: str) -> None:
    python = ROOT / directory / ".venv/bin/python"
    if Path(sys.executable).resolve() != python.resolve():
        os.execve(python, [str(python), str(Path(__file__).resolve()), *sys.argv[1:]], os.environ)


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit("Usage: ./run.py {browser-use|gpt-researcher|speculative-tools|autogpt|metagpt|swe-agent|openhands} TASK")
    workflow, task = sys.argv[1], " ".join(sys.argv[2:])
    load_env()

    if workflow == "browser-use":
        ensure_venv_python("browser-use")
        run_browser(task)
    elif workflow == "gpt-researcher":
        ensure_venv_python("gpt-researcher")
        run_research(task)
    elif workflow == "speculative-tools":
        ensure_venv_python("speculative-tools")
        run_speculative_tools(task)
    elif workflow == "autogpt":
        # AutoGPT validates model names against its provider enum. The
        # OpenAI-compatible llama.cpp endpoint accepts this standard alias.
        autogpt_model = os.getenv("AUTOGPT_MODEL", "gpt-4o-mini")
        os.environ.update(FAST_LLM=autogpt_model, SMART_LLM=autogpt_model)
        print(f"Enter this task when prompted: {task}", flush=True)
        exec_in("autogpt/classic", str(ROOT / "autogpt/classic/.venv/bin/python"), [str(ROOT / "autogpt/classic/.venv/bin/autogpt"), "run", "--skip-news", "--continuous", "--continuous-limit", "5"])
    elif workflow == "metagpt":
        configure_metagpt()
        exec_in("metagpt", str(ROOT / "metagpt/.venv/bin/python"), [str(ROOT / "metagpt/.venv/bin/metagpt"), task])
    elif workflow == "swe-agent":
        # SWE-ReX uploads a generated registry bundle with copytree, which
        # requires a clean target when multiple runs share this smoke host.
        shutil.rmtree("/tmp/swe-agent-tools", ignore_errors=True)
        Path("/tmp/swe-agent-tools").mkdir(parents=True, exist_ok=True)
        configured_target = os.getenv("SWE_TARGET")
        target_path = (
            Path(configured_target).expanduser().resolve()
            if configured_target
            else ROOT / ("swe-default-target" if (ROOT / "swe-default-target/.git").exists() else "swe-smoke-target")
        )
        if not (target_path / ".git").exists():
            raise SystemExit(f"SWE_TARGET must be a Git working tree: {target_path}")
        # LocalDeployment runs reset commands in the real host working tree.
        # Refuse a dirty target rather than silently discarding user edits.
        status = subprocess.run(
            ["git", "-C", str(target_path), "status", "--porcelain"], text=True, stdout=subprocess.PIPE, check=False
        )
        if status.returncode or status.stdout.strip():
            raise SystemExit(f"Refusing to run SWE-agent against a dirty target: {target_path}. Use a fresh clone via prepare-swe-go-redis-target.sh.")
        swe_target = target_path.as_posix().lstrip("/")
        exec_in(
            "swe-agent",
            str(ROOT / "swe-agent/.venv/bin/python"),
            [str(ROOT / "swe-agent/.venv/bin/sweagent"), "run", "--config", "config/default.yaml", "--agent.model.name", f"openai/{MODEL}", "--agent.model.api_base", os.environ["OPENAI_BASE_URL"], "--agent.model.litellm_model_registry", str(ROOT / "swe-agent-local-model-cost.json"), "--agent.model.max_output_tokens", "1024", "--agent.model.per_instance_cost_limit", "0", "--agent.model.total_cost_limit", "0", "--agent.tools.parse_function.type", "thought_action", "--env.deployment.type", "local", "--env.repo.type", "preexisting", "--env.repo.repo_name", swe_target, "--problem_statement.text", task],
        )
    elif workflow == "openhands":
        os.environ.update(LLM_API_KEY=os.environ["OPENAI_API_KEY"], LLM_BASE_URL=os.environ["OPENAI_BASE_URL"], LLM_MODEL=f"openai/{MODEL}")
        bundled_node = ROOT / ".tools/node-v22.22.0-linux-x64/bin"
        node_prefix = f"{bundled_node}:" if bundled_node.exists() else ""
        os.environ["PATH"] = f"{node_prefix}{Path.home() / '.local/bin'}:{os.environ['PATH']}"
        run_openhands()
    else:
        raise SystemExit(f"Unknown workflow: {workflow}")


if __name__ == "__main__":
    main()
