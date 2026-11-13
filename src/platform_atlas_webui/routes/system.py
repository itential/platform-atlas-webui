"""System health page and the small JSON endpoints the global chrome polls.

* ``/system``                 — full health page (credential store, TLS, storage, versions, scheduler).
* ``/api/system/status``      — light status for the degraded-mode banner (credential store only, cached).
* ``/api/system/recheck``     — force a fresh credential check (POST, CSRF-protected).
* ``/api/jobs/active``        — the running job (or the one that just finished) for the run dock.

Both ``/api`` reads are cheap and safe to poll; neither ever raises on a degraded system.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from platform_atlas.core._version import __version__ as ATLAS_VERSION

from platform_atlas_webui.dependencies import get_templates, template_context
from platform_atlas_webui.security.redirects import local_url
from platform_atlas_webui.services import health
from platform_atlas_webui.services.jobs import JobRecord, get_registry

router = APIRouter(tags=["system"])
_templates = get_templates()

# A health probe that hangs (an unroutable Vault host, say) must not hang the page.
_CHECK_TIMEOUT_S = 6.0
# How long a just-finished job stays in the dock so the result is noticed.
_FINISHED_LINGER_S = 20.0


def _active_env() -> str | None:
    from platform_atlas_webui.services import config as _cfg
    try:
        return _cfg.read_config().get("active_environment") or None
    except Exception:  # noqa: BLE001
        return None


# A timed-out probe's worker thread can't be cancelled, and the CLI's credential
# check holds a process-wide lock, so every further poll would park one more
# thread behind it. One probe per function stays in flight; later callers wait
# on that same probe instead of starting another.
_inflight: dict[Any, "asyncio.Future"] = {}


def _forget(fn, task: "asyncio.Future") -> None:
    if _inflight.get(fn) is task:
        del _inflight[fn]
    if not task.cancelled():
        task.exception()  # mark retrieved so a probe that outlived its caller doesn't log


async def _bounded(fn, *args, **kwargs):
    """Run a blocking health probe in the pool with a hard upper bound.

    Callers share one in-flight run per ``fn`` (``force`` and other arguments
    only apply to the run that started it).
    """
    task = _inflight.get(fn)
    if task is None:
        task = asyncio.ensure_future(run_in_threadpool(fn, *args, **kwargs))
        _inflight[fn] = task
        task.add_done_callback(lambda t, f=fn: _forget(f, t))
    try:
        return await asyncio.wait_for(asyncio.shield(task), _CHECK_TIMEOUT_S)
    except asyncio.TimeoutError:
        return None


def _credential_timeout_status() -> dict:
    return {
        "ok": False, "backend": "unknown", "code": "timeout", "supported": True, "cached": False,
        "message": f"The credential store did not answer within {int(_CHECK_TIMEOUT_S)} seconds.",
        "hint": "Check that the backend is reachable, then retry.", "checked_at": time.time(),
    }


@router.get("/system", response_class=HTMLResponse)
async def system_page(request: Request) -> HTMLResponse:
    env = _active_env()
    # The credential probe is the only check that can block on the network, so it
    # alone is time-bounded; the rest are local and cheap.
    credential = await _bounded(health.credential_check)
    if credential is None:
        credential = health.credential_timeout_check(_CHECK_TIMEOUT_S)
    snapshot = await run_in_threadpool(health.collect, env, credential=credential)
    return _templates.TemplateResponse(
        request,
        "system/index.html",
        template_context(request, atlas_version=ATLAS_VERSION, snapshot=snapshot, active_env=env or ""),
    )


@router.post("/system/recheck")
async def system_recheck():
    await _bounded(health.credential_status_dict, force=True)
    await _bounded(health.storage_check, force=True)
    return RedirectResponse(url="/system", status_code=303)


@router.get("/api/system/status")
async def system_status() -> JSONResponse:
    status = await _bounded(health.credential_status_dict)
    return JSONResponse({"boot_id": health.BOOT_ID, "credentials": status or _credential_timeout_status()})


@router.post("/api/system/recheck")
async def system_recheck_api() -> JSONResponse:
    status = await _bounded(health.credential_status_dict, force=True)
    return JSONResponse({"boot_id": health.BOOT_ID, "credentials": status or _credential_timeout_status()})


def _job_payload(rec: JobRecord) -> dict:
    """Compact, secret-free summary of a job for the run dock."""
    events = list(rec.history)
    phase = next((e.message for e in reversed(events) if e.kind == "phase"), "")
    message = next((e.message for e in reversed(events) if e.kind in ("info", "success", "warning", "error")), "")
    session = (rec.metadata or {}).get("session_name")
    try:
        url = local_url("/sessions", session, query={"job": rec.id}) if session else local_url("/jobs", rec.id)
    except Exception:  # noqa: BLE001 — a malformed name must not break the dock
        url = local_url("/jobs", rec.id)
    started = rec.started_at or rec.created_at
    return {
        "id": rec.id,
        "name": rec.name,
        "status": rec.status.value,
        "active": rec.is_active(),
        "started_at": started,
        "elapsed_s": int(max(0, (rec.finished_at or time.time()) - started)),
        "phase": phase,
        "message": message[:200],
        "error": (rec.error or "")[:300],
        "url": url,
        "log_url": local_url("/jobs", rec.id),
    }


@router.get("/api/jobs/active")
async def active_job() -> JSONResponse:
    jobs = get_registry().list()
    running = next((j for j in jobs if j.is_active()), None)
    finished = None
    if running is None:
        recent = [j for j in jobs if j.is_terminal() and j.finished_at
                  and time.time() - j.finished_at < _FINISHED_LINGER_S]
        finished = max(recent, key=lambda j: j.finished_at, default=None)
    chosen = running or finished
    return JSONResponse({"boot_id": health.BOOT_ID, "job": _job_payload(chosen) if chosen else None})
