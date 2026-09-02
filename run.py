#!/usr/bin/env python3
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODEL = "gpt-5-nano"


def load_env() -> None:
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())

    key = os.getenv("OPENAI_API_KEY", "")
    if not key:
        raise SystemExit("Set OPENAI_API_KEY to an OpenAI Platform API key")

    base_url = os.getenv("PROFILE_OPENAI_BASE_URL", "https://api.openai.com/v1")
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
        llm = ChatOpenAI(model=MODEL, base_url=os.environ["OPENAI_BASE_URL"])
        await Agent(task=task, llm=llm).run()

    asyncio.run(main())


def run_research(query: str) -> None:
    sys.path.insert(0, str(ROOT / "gpt-researcher"))
    from gpt_researcher import GPTResearcher

    async def main() -> None:
        researcher = GPTResearcher(query=query)
        await researcher.conduct_research()
        print(await researcher.write_report())

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
    os.chdir(ROOT / directory)
    os.execvpe(executable, [executable, *args], os.environ)


def ensure_venv_python(directory: str) -> None:
    python = ROOT / directory / ".venv/bin/python"
    if Path(sys.executable).resolve() != python.resolve():
        os.execve(python, [str(python), str(Path(__file__).resolve()), *sys.argv[1:]], os.environ)


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit("Usage: ./run.py {browser-use|gpt-researcher|autogpt|metagpt|swe-agent|openhands} TASK")
    workflow, task = sys.argv[1], " ".join(sys.argv[2:])
    load_env()

    if workflow == "browser-use":
        ensure_venv_python("browser-use")
        run_browser(task)
    elif workflow == "gpt-researcher":
        ensure_venv_python("gpt-researcher")
        run_research(task)
    elif workflow == "autogpt":
        os.environ.update(FAST_LLM=MODEL, SMART_LLM=MODEL)
        print(f"Enter this task when prompted: {task}", flush=True)
        exec_in("autogpt/classic", str(ROOT / "autogpt/classic/.venv/bin/python"), [str(ROOT / "autogpt/classic/.venv/bin/autogpt"), "run", "--skip-news", "--continuous", "--continuous-limit", "5"])
    elif workflow == "metagpt":
        configure_metagpt()
        exec_in("metagpt", str(ROOT / "metagpt/.venv/bin/python"), [str(ROOT / "metagpt/.venv/bin/metagpt"), task])
    elif workflow == "swe-agent":
        swe_target = "swe-default-target" if (ROOT / "swe-default-target/.git").exists() else "swe-smoke-target"
        exec_in(
            "swe-agent",
            str(ROOT / "swe-agent/.venv/bin/python"),
            [str(ROOT / "swe-agent/.venv/bin/sweagent"), "run", "--config", "config/default.yaml", "--agent.model.name", f"openai/{MODEL}", "--agent.model.api_base", os.environ["OPENAI_BASE_URL"], "--env.deployment.type", "local", "--env.repo.type", "preexisting", "--env.repo.repo_name", f"users/alanuiuc/agentic-workflow-profiling/{swe_target}", "--problem_statement.text", task],
        )
    elif workflow == "openhands":
        os.environ.update(LLM_API_KEY=os.environ["OPENAI_API_KEY"], LLM_BASE_URL=os.environ["OPENAI_BASE_URL"], LLM_MODEL=f"openai/{MODEL}")
        bundled_node = ROOT / ".tools/node-v22.22.0-linux-x64/bin"
        node_prefix = f"{bundled_node}:" if bundled_node.exists() else ""
        os.environ["PATH"] = f"{node_prefix}{Path.home() / '.local/bin'}:{os.environ['PATH']}"
        exec_in("openhands", str(ROOT / ".tools/openhands/node_modules/.bin/agent-canvas"), [])
    else:
        raise SystemExit(f"Unknown workflow: {workflow}")


if __name__ == "__main__":
    main()
