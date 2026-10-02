"""
Background job runner with SSE event streaming.

Why this exists:
    The CLI's long-running ops (preflight, capture, validate, report) are
    blocking and print to a Rich console. The WebUI needs them to run in
    the background and stream progress to the browser as it happens.

How it works:
    1. ``submit(name, fn, **kwargs)`` queues a job. ``fn`` runs in a
       worker thread (via asyncio.to_thread) and receives a ``JobLogger``
       it can call ``log()`` on to push events to subscribers.
    2. Each job has an ``asyncio.Queue`` of events. ``stream(job_id)``
       yields events as Server-Sent Events while the job runs and on
       completion sends a terminal ``status`` event.
    3. Multiple browser tabs can subscribe to the same job — each gets
       its own queue, and the broadcast fan-out happens inside the
       JobLogger.

Events have shape ``{"kind": str, "message": str, "timestamp": float,
"data": dict | None}``. The kind is rendered as a CSS class on the
client (``info|success|warning|error|phase|debug|check``).

Two streams in one:
    Curated ``jlogger.info/phase/etc.`` events form the human-friendly
    narrative. A ``logging.Handler`` attached for the duration of each job
    converts ``platform_atlas.*`` ``LogRecord``s into ``kind='debug'``
    events on the same stream. The browser can hide or show debug events
    via the per-job toggle, so users get a curated view by default and
    raw progress logs on demand without restarting the job.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Callable

from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class JobBusyError(StarletteHTTPException):
    """Another job is active; only one job may touch the shared CLI context at a time.

    Subclasses the Starlette HTTPException (409) so the app's existing error
    handler renders the message for browsers and JSON for htmx/API clients.
    """

    def __init__(self, message: str) -> None:
        super().__init__(status_code=409, detail=message)


class JobCancelled(Exception):
    """Raised inside a runner when its cooperative cancel flag is set."""


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass
class JobEvent:
    kind: str  # info | success | warning | error | phase | status
    message: str
    timestamp: float = field(default_factory=time.time)
    data: dict[str, Any] | None = None

    def to_sse(self) -> str:
        payload = {
            "kind": self.kind,
            "message": self.message,
            "timestamp": self.timestamp,
            "data": self.data or {},
        }
        return f"event: {self.kind}\ndata: {json.dumps(payload)}\n\n"


@dataclass
class JobRecord:
    id: str
    name: str
    status: JobStatus = JobStatus.QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    history: list[JobEvent] = field(default_factory=list)
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    result: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # Cooperative cancellation: workers consult ``cancel_event`` at safe
    # checkpoints and raise JobCancelled. Replaces the old PyThreadState_SetAsyncExc
    # approach which can't interrupt blocking C calls (paramiko/pymongo/etc.)
    # and could leave half-open network connections.
    cancel_event: threading.Event = field(default_factory=threading.Event)
    # True while the worker thread is actually executing ``fn``. A job that
    # timed out is marked FAILED immediately but its thread may still be
    # winding down; this flag keeps the single-flight guard honest about that.
    worker_running: bool = False

    def is_active(self) -> bool:
        """Job (or its worker thread) may still be touching shared state."""
        return (not self.is_terminal()) or self.worker_running

    def is_terminal(self) -> bool:
        return self.status in (JobStatus.SUCCEEDED, JobStatus.FAILED)


class JobLogger:
    """Handed to a job function — calls fan out to all subscribers."""

    def __init__(self, record: JobRecord, loop: asyncio.AbstractEventLoop) -> None:
        self._record = record
        self._loop = loop

    def _emit(self, event: JobEvent) -> None:
        self._record.history.append(event)
        # Cap history so a runaway runner can't grow this list unboundedly —
        # late SSE subscribers still get the most recent ~5000 events.
        if len(self._record.history) > 5000:
            del self._record.history[:-5000]
        # Broadcast from worker thread back to the loop. Re-fetch the loop
        # in case the registry's cached loop has rotated under us.
        loop = self._loop
        if loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._fan_out, event)
        except RuntimeError:
            # Loop was closed between is_closed() and call_soon_threadsafe.
            pass

    def _fan_out(self, event: JobEvent) -> None:
        for q in list(self._record.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("Job %s: subscriber queue full, dropping event", self._record.id)

    def is_cancelled(self) -> bool:
        """Workers should consult this at checkpoints and raise JobCancelled."""
        return self._record.cancel_event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._record.cancel_event.is_set():
            raise JobCancelled()

    def log(self, message: str, *, kind: str = "info", data: dict[str, Any] | None = None) -> None:
        self._emit(JobEvent(kind=kind, message=message, data=data))

    def info(self, message: str, **kw) -> None:
        self.log(message, kind="info", **kw)

    def success(self, message: str, **kw) -> None:
        self.log(message, kind="success", **kw)

    def warning(self, message: str, **kw) -> None:
        self.log(message, kind="warning", **kw)

    def error(self, message: str, **kw) -> None:
        self.log(message, kind="error", **kw)

    def phase(self, message: str, **kw) -> None:
        self.log(message, kind="phase", **kw)

    def debug(self, message: str, **kw) -> None:
        """Emit a raw/debug-level event. Hidden by default in the WebUI;
        shown when the user toggles the stream into raw mode."""
        self.log(message, kind="debug", **kw)

    def check(self, name: str, status: str, message: str, details: str = "") -> None:
        """Emit a structured check result (preflight per-system status)."""
        self._emit(JobEvent(
            kind="check",
            message=message,
            data={"name": name, "status": status, "message": message, "details": details},
        ))


class JobLogHandler(logging.Handler):
    """Forwards Python ``LogRecord`` instances into a job event stream as
    ``kind='debug'`` events.

    Attached to the ``platform_atlas`` logger hierarchy for the lifetime of a
    single job. Records are formatted with logger name + level so the raw
    stream surfaces *what* part of Atlas is talking, not just the message.
    The handler ignores its own subtree (``platform_atlas_webui.*``) so HTTP
    request/response chatter doesn't pollute job streams that aren't about it.
    """

    def __init__(self, jlogger: "JobLogger") -> None:
        super().__init__(level=logging.DEBUG)
        self._jlogger = jlogger
        self.setFormatter(
            logging.Formatter(fmt="[%(levelname)s] %(name)s — %(message)s")
        )

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        # WebUI internals (HTTP access, auth, request lifecycle) shouldn't
        # bleed into a job stream that's nominally about capture/validate.
        if record.name.startswith("platform_atlas_webui"):
            return
        try:
            msg = self.format(record)
        except Exception:  # noqa: BLE001 — formatting failure must not propagate
            try:
                msg = record.getMessage()
            except Exception:  # noqa: BLE001
                return
        self._jlogger.debug(msg)


class JobRegistry:
    """Process-wide registry of running and recently-finished jobs."""

    def __init__(self, *, retention: int = 50) -> None:
        self._jobs: dict[str, JobRecord] = {}
        # Use a thread-safe lock — ``asyncio.Lock`` would bind to whichever
        # event loop touched it first, which breaks across reload/test runs.
        # The critical sections here are tiny dict mutations so a sync lock
        # is appropriate.
        self._lock = threading.Lock()
        self._retention = retention

    def list(self) -> list[JobRecord]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def get(self, job_id: str) -> JobRecord | None:
        return self._jobs.get(job_id)

    async def submit(
        self,
        name: str,
        fn: Callable[..., Any],
        *,
        timeout: float | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> JobRecord:
        """Queue a callable to run as a background job.

        ``fn`` is invoked as ``fn(job_logger, **kwargs)``. Its return
        value is stored in ``record.result``. Any exception is caught
        and surfaces as ``status=FAILED``. When ``timeout`` (seconds) is
        given, the job is failed if it runs longer — a backstop so a hung
        network call can't wedge the job (and the worker pool) indefinitely.
        """
        job_id = uuid.uuid4().hex[:12]
        record = JobRecord(id=job_id, name=name, metadata=metadata or {})

        with self._lock:
            # Single-flight (BUG-01): every job reads the process-wide CLI
            # context (ctx()), and capture/validate/report re-initialise it
            # for their session, so overlapping jobs could swap ruleset /
            # environment / tier under one another mid-run. Serialise all jobs.
            for other in self._jobs.values():
                if other.is_active():
                    if other.is_terminal():
                        why = "is still shutting down after a timeout/failure"
                    else:
                        why = "is still running"
                    raise JobBusyError(
                        f"Another job ('{other.name}', id {other.id}) {why}. "
                        f"Wait for it to finish (see /jobs/{other.id}) and try again."
                    )
            self._jobs[job_id] = record
            self._evict_old_locked()

        loop = asyncio.get_running_loop()

        async def _runner() -> None:
            record.status = JobStatus.RUNNING
            record.started_at = time.time()
            jlogger = JobLogger(record, loop)

            # Attach a logging handler that forwards platform_atlas log
            # records into the stream as kind='debug' events. We lower the
            # logger's level to DEBUG for the duration of the job so chatty
            # internals (collector progress, retries, mongo timing, etc.) are
            # actually emitted. The previous level is restored in the finally
            # block so post-job logging behaves exactly as it did before.
            atlas_logger = logging.getLogger("platform_atlas")
            log_handler = JobLogHandler(jlogger)
            atlas_logger.addHandler(log_handler)
            prev_level = atlas_logger.level
            atlas_logger.setLevel(logging.DEBUG)

            jlogger.info(f"Job '{name}' started", data={"job_id": job_id})
            try:
                def _worker() -> Any:
                    try:
                        return fn(jlogger, **kwargs)
                    finally:
                        record.worker_running = False

                # Set before the thread starts so the single-flight guard
                # never sees a gap between "queued" and "running".
                record.worker_running = True
                if timeout is not None:
                    result = await asyncio.wait_for(
                        asyncio.to_thread(_worker), timeout
                    )
                else:
                    result = await asyncio.to_thread(_worker)
                record.result = result
                record.status = JobStatus.SUCCEEDED
                jlogger.success("Job completed successfully")
            except asyncio.TimeoutError:
                # The worker thread cannot be killed; ask it to stop at its next
                # cooperative checkpoint and report honestly that it may still
                # be winding down (new jobs stay blocked until it exits).
                record.cancel_event.set()
                record.status = JobStatus.FAILED
                record.error = (
                    f"Job timed out after {int(timeout)}s. A stop was requested, but the "
                    "worker may still be finishing in the background; new jobs are blocked "
                    "until it exits."
                )
                jlogger.error(record.error)
            except JobCancelled:
                record.status = JobStatus.FAILED
                record.error = "Cancelled by user"
                jlogger.error("Job was cancelled")
            except Exception as exc:  # noqa: BLE001 — terminal capture
                record.error = f"{type(exc).__name__}: {exc}"
                record.status = JobStatus.FAILED
                jlogger.error(record.error)
                # Full traceback goes to the server log only — never to record
                # metadata, which is rendered into the browser-facing job view.
                logger.exception("Job %s failed", job_id)
            finally:
                # Detach handler before emitting the terminal event so any
                # logging that fires during shutdown doesn't loop back through
                # us (and so concurrent jobs don't share each other's debug).
                try:
                    atlas_logger.removeHandler(log_handler)
                    atlas_logger.setLevel(prev_level)
                except Exception:  # noqa: BLE001
                    pass
                record.finished_at = time.time()
                # Final status event so streaming clients can close.
                terminal = JobEvent(
                    kind="status",
                    message=record.status.value,
                    data={
                        "status": record.status.value,
                        "error": record.error,
                    },
                )
                record.history.append(terminal)
                # The terminal event must reach every subscriber — if the queue
                # is full we drop the oldest event to make room rather than
                # silently swallowing the close signal (which would leave SSE
                # clients hanging until idle timeout).
                for q in list(record.subscribers):
                    while True:
                        try:
                            q.put_nowait(terminal)
                            break
                        except asyncio.QueueFull:
                            try:
                                q.get_nowait()
                            except asyncio.QueueEmpty:
                                break

        asyncio.create_task(_runner(), name=f"atlas-job-{job_id}")
        return record

    def cancel(self, job_id: str) -> bool:
        """Signal cooperative cancellation. Workers must consult their flag.

        Returns True if a cancellation signal was sent. The job's status is
        only flipped to FAILED when the worker actually unwinds — this avoids
        the previous double-terminal-event bug where status was set both here
        and in the runner's finally block.
        """
        record = self._jobs.get(job_id)
        if record is None or record.is_terminal():
            return False
        record.cancel_event.set()
        return True

    def _evict_old_locked(self) -> None:
        if len(self._jobs) <= self._retention:
            return
        ordered = sorted(self._jobs.values(), key=lambda j: j.created_at)
        # Evict oldest terminal jobs first; never evict running ones.
        for j in ordered:
            if len(self._jobs) <= self._retention:
                break
            if j.is_terminal():
                self._jobs.pop(j.id, None)

    async def stream(self, job_id: str) -> AsyncIterator[JobEvent]:
        record = self._jobs.get(job_id)
        if record is None:
            return
        # Replay history first so a late subscriber sees the full log.
        for event in list(record.history):
            yield event
        if record.is_terminal():
            return
        q: asyncio.Queue[JobEvent] = asyncio.Queue(maxsize=512)
        record.subscribers.append(q)
        try:
            while True:
                event = await q.get()
                yield event
                if event.kind == "status":
                    break
        finally:
            try:
                record.subscribers.remove(q)
            except ValueError:
                pass


_registry: JobRegistry | None = None


def get_registry() -> JobRegistry:
    """Return the process-wide JobRegistry, creating it on first access."""
    global _registry
    if _registry is None:
        _registry = JobRegistry()
    return _registry
