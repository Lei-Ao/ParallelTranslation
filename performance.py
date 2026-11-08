"""Local numeric timings only: never accept prompts, keys or document text."""
import contextvars
import functools
import inspect
import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path

TRACE = contextvars.ContextVar("performance_trace", default=None)
_LOCK = threading.Lock()
LOG = Path(__file__).resolve().parent / "tmp" / "performance.jsonl"


def count(name, value=1):
    trace = TRACE.get()
    if trace is not None:
        with _LOCK:
            trace[name] = trace.get(name, 0) + value


@contextmanager
def span(name):
    started = time.perf_counter()
    try:
        yield
    finally:
        count(name + "_ms", (time.perf_counter() - started) * 1000)


def measured(name):
    def decorate(function):
        if inspect.iscoroutinefunction(function):
            @functools.wraps(function)
            async def async_wrapper(*args, **kwargs):
                with span(name):
                    return await function(*args, **kwargs)
            return async_wrapper
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            with span(name):
                return function(*args, **kwargs)
        return wrapper
    return decorate


def write(trace):
    # Only internally named numeric fields reach disk. Bounded local history.
    numeric = {key: round(value, 3) for key, value in trace.items()
               if isinstance(value, (int, float))}
    try:
        with _LOCK:
            LOG.parent.mkdir(parents=True, exist_ok=True)
            if LOG.exists() and LOG.stat().st_size > 5 * 1024 * 1024:
                LOG.replace(LOG.with_suffix(".previous.jsonl"))
            with LOG.open("a", encoding="utf-8") as output:
                output.write(json.dumps(numeric) + "\n")
    except OSError:
        pass  # Diagnostics must not prevent saving translations.
