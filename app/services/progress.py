"""Real progress events for long analyses.

The analysis runs in a background thread; every pipeline stage / agent step it actually completes is
appended to the job's event list through `emit()`, and the browser polls the list. The UI therefore shows
only things that really happened, in the order they happened.

Jobs live in this process's memory (single-process limitation, documented in the README) and are only
readable by the user who started them.
"""

from __future__ import annotations

import contextvars
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from app.security.request_scope import owner_scope
from app.utils.exceptions import AInsightError
from app.utils.logger import get_logger

log = get_logger("progress")

_sink: contextvars.ContextVar[Callable[[dict], None] | None] = contextvars.ContextVar("progress_sink", default=None)
class JobNotFoundError(AInsightError):
    status_code = 404
    user_message = "That analysis job was not found (it may have expired)."


_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="analysis-job")
_JOBS: dict[str, "Job"] = {}
_LOCK = threading.Lock()
MAX_JOBS = 200
TTL_SECONDS = 20 * 60


def emit(event: dict) -> None:
    sink = _sink.get()
    if sink is not None:
        try:
            sink(dict(event))
        except Exception:  # progress reporting must never break an analysis
            pass


class Job:
    def __init__(self, owner: str | None):
        self.id = secrets.token_hex(12)
        self.owner = owner
        self.events: list[dict] = []
        self.status = "running"  # running | done | error
        self.result: Any = None
        self.error: dict | None = None
        self.created = time.time()


def _purge() -> None:
    now = time.time()
    for job_id in [j for j, job in _JOBS.items() if now - job.created > TTL_SECONDS]:
        _JOBS.pop(job_id, None)
    while len(_JOBS) > MAX_JOBS:
        _JOBS.pop(min(_JOBS, key=lambda j: _JOBS[j].created), None)


def start(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
    owner = owner_scope()  # fail closed when accounts are on and nobody is signed in
    job = Job(owner)
    with _LOCK:
        _purge()
        _JOBS[job.id] = job
    ctx = contextvars.copy_context()

    def run() -> None:
        _sink.set(job.events.append)
        try:
            job.result = fn(*args, **kwargs)
            job.status = "done"
        except AInsightError as exc:
            job.error = {"message": exc.user_message, "status_code": exc.status_code}
            job.status = "error"
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("Background analysis crashed")
            job.error = {"message": "The analysis failed unexpectedly.", "status_code": 500}
            job.status = "error"

    _POOL.submit(ctx.run, run)
    return job.id


def get(job_id: str, since: int = 0) -> dict:
    owner = owner_scope()
    with _LOCK:
        job = _JOBS.get(job_id)
    if job is None or (owner is not None and job.owner != owner):
        raise JobNotFoundError()
    events = list(job.events)
    out = {"job_id": job.id, "status": job.status, "events": events[since:], "next": len(events)}
    if job.status == "done":
        out["result"] = job.result
    elif job.status == "error":
        out["error"] = job.error
    return out
