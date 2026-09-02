#!/usr/bin/env python3
"""Small, dependency-free execution-node tracer.

The trace is intentionally JSONL so it can be joined with ``llm.jsonl`` by
timestamp.  CPU time is process CPU (including waited-for local children),
while duration is elapsed monotonic time.  This makes a node's CPU cost and
its position on the wall-clock critical path independently visible.
"""

from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from functools import wraps
import inspect
import json
import os
from pathlib import Path
import resource
import sys
import threading
import time
import tracemalloc
import uuid


_stack: ContextVar[tuple[str, ...]] = ContextVar("execution_node_stack", default=())
_lock = threading.Lock()
_active: dict[str, dict] = {}
_sampler_started = False

if os.getenv("PROFILE_NODE_TRACE") and not tracemalloc.is_tracing():
    tracemalloc.start()


def _cpu_ns() -> int:
    own = time.process_time_ns()
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return own + int((children.ru_utime + children.ru_stime) * 1_000_000_000)


def _rss_bytes() -> int | None:
    """Return current RSS for this process on Linux."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (FileNotFoundError, PermissionError, ValueError):
        return None
    return None


def _resource_usage() -> dict[str, int]:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "minor_page_faults": usage.ru_minflt,
        "major_page_faults": usage.ru_majflt,
        "voluntary_context_switches": usage.ru_nvcsw,
        "involuntary_context_switches": usage.ru_nivcsw,
    }


def _resource_peak_bytes(which: int) -> int | None:
    """Return the OS high-water RSS (Linux reports KiB)."""
    try:
        value = resource.getrusage(which).ru_maxrss
        # Linux and most BSDs report KiB; macOS reports bytes.
        return int(value) if sys.platform == "darwin" else int(value * 1024)
    except (ValueError, OSError):
        return None


def _python_memory() -> tuple[int, int]:
    current, peak = tracemalloc.get_traced_memory()
    return current, peak


def _sample_rss() -> None:
    global _sampler_started
    while True:
        rss = _rss_bytes()
        with _lock:
            if not _active:
                _sampler_started = False
                return
            if rss is not None:
                for record in _active.values():
                    record["peak_rss_bytes"] = max(record["peak_rss_bytes"] or 0, rss)
        time.sleep(0.01)


def _start_sampling(span_id: str, rss: int | None) -> None:
    global _sampler_started
    with _lock:
        _active[span_id] = {"peak_rss_bytes": rss}
        if not _sampler_started:
            _sampler_started = True
            threading.Thread(target=_sample_rss, name="rss-sampler", daemon=True).start()


def _stop_sampling(span_id: str, rss: int | None) -> int | None:
    with _lock:
        record = _active.pop(span_id, {"peak_rss_bytes": None})
    peak = record["peak_rss_bytes"]
    if rss is not None:
        peak = max(peak or 0, rss)
    return peak


def _write(event: dict) -> None:
    target = os.getenv("PROFILE_NODE_TRACE")
    if not target:
        return
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock, path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, separators=(",", ":")) + "\n")


@contextmanager
def execution_node(node_type: str, name: str, **metadata):
    """Record one local execution node, including nested/parallel nodes."""
    if os.getenv("PROFILE_NODE_TRACE") and not tracemalloc.is_tracing():
        tracemalloc.start()
    span_id = uuid.uuid4().hex
    parent = _stack.get()
    token = _stack.set(parent + (span_id,))
    started_wall = time.time()
    started_ns = time.perf_counter_ns()
    started_cpu_ns = _cpu_ns()
    started_rss = _rss_bytes()
    started_python, started_python_peak = _python_memory()
    started_resources = _resource_usage()
    _start_sampling(span_id, started_rss)
    base = {
        "kind": "execution_node",
        "span_id": span_id,
        "parent_span_id": parent[-1] if parent else None,
        "node_type": node_type,
        "name": name,
        "pid": os.getpid(),
        "thread_id": threading.get_ident(),
        "started_at": started_wall,
        **metadata,
    }
    _write({**base, "event": "start"})
    try:
        yield span_id
    except BaseException as error:
        ended_ns = time.perf_counter_ns()
        ended_rss = _rss_bytes()
        ended_python, ended_python_peak = _python_memory()
        ended_resources = _resource_usage()
        process_peak_rss = _resource_peak_bytes(resource.RUSAGE_SELF)
        children_peak_rss = _resource_peak_bytes(resource.RUSAGE_CHILDREN)
        _write({
            **base,
            "event": "end",
            "ended_at": time.time(),
            "duration_ms": round((ended_ns - started_ns) / 1_000_000, 3),
            "cpu_ms": round((_cpu_ns() - started_cpu_ns) / 1_000_000, 3),
            "rss_start_bytes": started_rss,
            "rss_end_bytes": ended_rss,
            "rss_delta_bytes": ended_rss - started_rss if ended_rss is not None and started_rss is not None else None,
            "peak_rss_bytes": _stop_sampling(span_id, ended_rss),
            "process_peak_rss_bytes": process_peak_rss,
            "children_peak_rss_bytes": children_peak_rss,
            "python_current_bytes": ended_python,
            "python_net_allocated_bytes": ended_python - started_python,
            "python_peak_bytes": max(started_python_peak, ended_python_peak),
            "minor_page_faults": ended_resources["minor_page_faults"] - started_resources["minor_page_faults"],
            "major_page_faults": ended_resources["major_page_faults"] - started_resources["major_page_faults"],
            "voluntary_context_switches": ended_resources["voluntary_context_switches"] - started_resources["voluntary_context_switches"],
            "involuntary_context_switches": ended_resources["involuntary_context_switches"] - started_resources["involuntary_context_switches"],
            "status": "error",
            "error_type": type(error).__name__,
        })
        raise
    else:
        ended_ns = time.perf_counter_ns()
        ended_rss = _rss_bytes()
        ended_python, ended_python_peak = _python_memory()
        ended_resources = _resource_usage()
        process_peak_rss = _resource_peak_bytes(resource.RUSAGE_SELF)
        children_peak_rss = _resource_peak_bytes(resource.RUSAGE_CHILDREN)
        _write({
            **base,
            "event": "end",
            "ended_at": time.time(),
            "duration_ms": round((ended_ns - started_ns) / 1_000_000, 3),
            "cpu_ms": round((_cpu_ns() - started_cpu_ns) / 1_000_000, 3),
            "rss_start_bytes": started_rss,
            "rss_end_bytes": ended_rss,
            "rss_delta_bytes": ended_rss - started_rss if ended_rss is not None and started_rss is not None else None,
            "peak_rss_bytes": _stop_sampling(span_id, ended_rss),
            "process_peak_rss_bytes": process_peak_rss,
            "children_peak_rss_bytes": children_peak_rss,
            "python_current_bytes": ended_python,
            "python_net_allocated_bytes": ended_python - started_python,
            "python_peak_bytes": max(started_python_peak, ended_python_peak),
            "minor_page_faults": ended_resources["minor_page_faults"] - started_resources["minor_page_faults"],
            "major_page_faults": ended_resources["major_page_faults"] - started_resources["major_page_faults"],
            "voluntary_context_switches": ended_resources["voluntary_context_switches"] - started_resources["voluntary_context_switches"],
            "involuntary_context_switches": ended_resources["involuntary_context_switches"] - started_resources["involuntary_context_switches"],
            "status": "ok",
        })
    finally:
        _stack.reset(token)


@asynccontextmanager
async def async_execution_node(node_type: str, name: str, **metadata):
    with execution_node(node_type, name, **metadata) as span_id:
        yield span_id


def instrument_callable(node_type: str, name: str, callable_, **metadata):
    """Wrap a local tool/function while retaining its original call shape."""
    if inspect.iscoroutinefunction(callable_):
        @wraps(callable_)
        async def async_wrapper(*args, **kwargs):
            async with async_execution_node(node_type, name, **metadata):
                return await callable_(*args, **kwargs)
        return async_wrapper

    @wraps(callable_)
    def wrapper(*args, **kwargs):
        with execution_node(node_type, name, **metadata):
            return callable_(*args, **kwargs)
    return wrapper
