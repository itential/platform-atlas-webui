"""
Continuous audit routes — landing page (status + run history + force-run),
single-run detail page, and toggle/settings POST.
"""

from __future__ import annotations

import asyncio
import json
import re

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response

# from platform_atlas.capture.log_parser import DEFAULT_KEYWORDS  # used by log-watch (deferred)
from platform_atlas.core._version import __version__ as ATLAS_VERSION
from platform_atlas.continuous import os_scheduler, storage
from platform_atlas.continuous.engine import run_once
from platform_atlas.continuous.models import (
    ContinuousSettings,
    # TODO: Log file watching — deferred to a later version of Atlas.
    # LogWatchEntry,
    # LOG_WATCH_ANY,
    # LOG_WATCH_COUNT,
    # LOG_WATCH_WINDOW,
    # LOG_WATCH_SOURCES,
    VALID_ALERT_POLICIES,
    # VALID_LOG_WATCH_THRESHOLDS,
)
from platform_atlas.continuous.policy import describe_policy
from platform_atlas.continuous.runtime import can_enable, read_settings, write_settings

from platform_atlas_webui.dependencies import get_atlas_context, get_templates, template_context
from platform_atlas_webui.services import continuous as cont_svc
from platform_atlas_webui.services import continuous as _cont_svc  # alias for topbar_summary
from platform_atlas_webui.services import environments as env_svc
from platform_atlas_webui.services import rulesets as ruleset_svc

router = APIRouter(prefix="/continuous", tags=["continuous"])
_templates = get_templates()

# TODO: Log file watching — deferred to a later version of Atlas.
# Uncomment the block below (and matching sections in landing.html, engine.py,
# handlers/continuous.py, cli.py) to re-enable log-watch routes.
#
# _LW_KEYWORD_GROUPS: list[tuple[str, list[str]]] = [
#     ("Connection / Infrastructure", ["ECONNREFUSED", "ECONNRESET", "ENOTFOUND", "ETIMEDOUT", "EPIPE", "unreachable", "offline", "unavailable"]),
#     ("Severity",                    ["fail", "fatal", "exception", "traceback", "panic", "critical", "segfault", "sigabrt", "crashed"]),
#     ("Auth / Permissions",          ["denied", "forbidden", "unauthorized"]),
#     ("General",                     ["timeout", "invalid", "unexpected", "unknown", "could not", "missing"]),
#     ("Services",                    ["Channel closed", "ERROR RETURN", "maxmemory", "NOREPLICAS", "QueueDeclare", "ConnBlockedError"]),
#     ("MongoDB",                     ["MongoError", "MongoServerError", "topology was destroyed"]),
# ]
# _LW_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")
# _MIN_THRESHOLD_COUNT = 2
# _MIN_WINDOW_MINUTES  = 5


# Mirrors the format produced by ``storage.make_run_id``:
#     YYYY-MM-DDTHH-MM-SSZ-<6digit-micro>-<4hex-nonce>-<env_suffix>
# The env_suffix has slashes/spaces replaced with "_". This is paranoia about
# user-supplied path params — even though Starlette path params don't contain
# raw slashes, an attacker-controlled run_id used in run_path() could escape
# the runs/ directory. Validating against the make_run_id pattern blocks that
# AND any header-injection attempt via Content-Disposition.
_RUN_ID_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z-\d{6}-[0-9a-f]{4}-[A-Za-z0-9_.\-]{1,128}$"
)


def _validate_run_id(run_id: str) -> str:
    """Return ``run_id`` if it matches the canonical format; else raise 400.

    Defense-in-depth — the storage layer also asserts paths stay under the
    runs/ root, but rejecting at the route boundary gives a clearer error
    message and short-circuits the lookup.
    """
    if not _RUN_ID_RE.match(run_id or ""):
        raise HTTPException(status_code=400, detail="Invalid run_id format")
    return run_id


def _active_env() -> str | None:
    try:
        atlas = get_atlas_context()
        return atlas.active_environment
    except HTTPException:
        return None


# Set of envs with an in-flight manual run-now invocation. Process-local —
# good enough since the engine itself takes a per-env file lock that catches
# the cross-process case (see engine.run_once). This guard's job is just to
# make the WebUI feel sensible when a user spams the button.
_in_flight_runs: set[str] = set()
_in_flight_lock = asyncio.Lock()


@router.get("", response_class=HTMLResponse)
async def landing(request: Request) -> HTMLResponse:
    env = _active_env()
    settings = read_settings(env or "")
    status = storage.read_status(env or "")
    run_ids = await run_in_threadpool(storage.list_runs, env or "", limit=50)
    recent_runs: list[dict] = []
    for rid in run_ids:
        run = await run_in_threadpool(storage.read_run, env or "", rid)
        if not run:
            continue
        recent_runs.append({
            "run_id": run.get("run_id", rid),
            "started_at": run.get("started_at", ""),
            "finished_at": run.get("finished_at", ""),
            "duration_ms": run.get("duration_ms", 0),
            "summary": run.get("summary", {}),
            "capture_error": run.get("capture_error"),
        })

    enable_allowed, enable_blocked_reason = can_enable(env or "")
    timer = await run_in_threadpool(os_scheduler.status, env or "")
    rulesets = await run_in_threadpool(ruleset_svc.list_rulesets)
    profiles = await run_in_threadpool(ruleset_svc.list_profiles)
    environments = await run_in_threadpool(env_svc.list_environments)

    return _templates.TemplateResponse(
        request,
        "continuous/landing.html",
        template_context(
            request,
            atlas_version=ATLAS_VERSION,
            env=env,
            settings=settings,
            status=status,
            recent_runs=recent_runs,
            scheduler_running=cont_svc.get_scheduler().is_running,
            enable_allowed=enable_allowed,
            enable_blocked_reason=enable_blocked_reason,
            timer=timer,
            timer_unit=os_scheduler.unit_basename(env) if env else "",
            rulesets=rulesets,
            profiles=profiles,
            environments=environments,
            # TODO: Log file watching — deferred to a later version of Atlas.
            # Restore these when uncommenting the log-watch template section.
            # lw_keyword_groups=_LW_KEYWORD_GROUPS,
            # lw_sources=LOG_WATCH_SOURCES,
            # lw_threshold_modes=VALID_LOG_WATCH_THRESHOLDS,
            # lw_min_count=_MIN_THRESHOLD_COUNT,
            # lw_min_window=_MIN_WINDOW_MINUTES,
        ),
    )


@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def view_run(request: Request, run_id: str) -> HTMLResponse:
    run_id = _validate_run_id(run_id)
    env = _active_env()
    run = await run_in_threadpool(storage.read_run, env or "", run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    return _templates.TemplateResponse(
        request,
        "continuous/run_detail.html",
        template_context(
            request,
            atlas_version=ATLAS_VERSION,
            env=env,
            run=run,
            run_pretty=json.dumps(run, indent=2, ensure_ascii=False),
        ),
    )


@router.get("/runs/{run_id}/raw")
async def download_run(run_id: str):
    run_id = _validate_run_id(run_id)
    env = _active_env()
    run = await run_in_threadpool(storage.read_run, env or "", run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    body = json.dumps(run, indent=2, ensure_ascii=False)
    # run_id has been regex-validated against _RUN_ID_RE, so it cannot contain
    # CR/LF or quote chars that would split the Content-Disposition header.
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="continuous-{run_id}.json"'},
    )


@router.post("/switch-env")
async def switch_env(env_name: str = Form(...)):
    """Activate a different environment and return to the continuous audit page."""
    try:
        await run_in_threadpool(env_svc.set_active, env_name)
        from platform_atlas.core.context import init_context
        await run_in_threadpool(init_context)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/continuous", status_code=303)


_MIN_INTERVAL_SECONDS = 3600  # WebUI floor — sub-hour cadences would hammer the platform


@router.post("/enable")
async def enable(
    interval: int = Form(0),
    retain: int = Form(0),
    ruleset_id: str = Form(""),
    profile_id: str = Form(""),
):
    env = _active_env()
    if not env:
        raise HTTPException(status_code=400, detail="No active environment.")
    existing = read_settings(env)
    # If already enabled, treat this as a settings update — no precheck needed.
    if not existing.enabled:
        allowed, reason = can_enable(env)
        if not allowed:
            raise HTTPException(status_code=400, detail=reason)
    # 0 is the "keep existing" sentinel; any other value must meet the WebUI floor.
    if interval and interval < _MIN_INTERVAL_SECONDS:
        raise HTTPException(
            status_code=400,
            detail=f"Interval must be at least {_MIN_INTERVAL_SECONDS}s (1 hour).",
        )
    settings = ContinuousSettings(
        enabled=True,
        interval_seconds=interval if interval > 0 else existing.interval_seconds,
        retain_runs=retain if retain > 0 else existing.retain_runs,
        ruleset_id=ruleset_id if ruleset_id else existing.ruleset_id,
        profile_id=profile_id if profile_id else existing.profile_id,
        alert_policy=existing.alert_policy,
        watchlist=existing.watchlist,
    )
    write_settings(env, settings)
    # Install (or refresh) the systemd user timer. Idempotent — safe to call
    # on every settings update so interval changes propagate.
    await run_in_threadpool(os_scheduler.install, env, settings.interval_seconds)
    return RedirectResponse(url="/continuous", status_code=303)


@router.post("/disable")
async def disable():
    env = _active_env()
    if not env:
        raise HTTPException(status_code=400, detail="No active environment.")
    existing = read_settings(env)
    write_settings(env, ContinuousSettings(
        enabled=False,
        interval_seconds=existing.interval_seconds,
        retain_runs=existing.retain_runs,
        ruleset_id=existing.ruleset_id,
        profile_id=existing.profile_id,
        alert_policy=existing.alert_policy,
        watchlist=existing.watchlist,
    ))
    await run_in_threadpool(os_scheduler.uninstall, env)
    return RedirectResponse(url="/continuous", status_code=303)


# ── alert policy + watchlist (parity with CLI continuous-audit policy / watch) ──

def _replace_settings_on_disk(env: str, **changes) -> ContinuousSettings:
    """Persist a partial settings update — mirrors the CLI helper of the same name."""
    current = read_settings(env)
    fields = {
        "enabled":           current.enabled,
        "interval_seconds":  current.interval_seconds,
        "retain_runs":       current.retain_runs,
        "ruleset_id":        current.ruleset_id,
        "profile_id":        current.profile_id,
        "alert_policy":      current.alert_policy,
        "watchlist":         current.watchlist,
        "log_watch_enabled": current.log_watch_enabled,
        "log_watches":       current.log_watches,
    }
    fields.update(changes)
    new = ContinuousSettings(**fields)
    write_settings(env, new)
    return new


def _split_watch_input(raw: str) -> list[str]:
    """Parse a free-form rule list ('PLAT-001 PLAT-002, plat-003') from a form."""
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for token in raw.replace(",", " ").split():
        norm = token.strip().upper()
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


@router.post("/policy")
async def set_policy(policy: str = Form("")):
    """Update the alert policy. Empty body → no-op (form might submit blank)."""
    env = _active_env()
    if not env:
        raise HTTPException(status_code=400, detail="No active environment.")
    candidate = (policy or "").strip().lower()
    if candidate not in VALID_ALERT_POLICIES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid policy: {policy!r}. Choose one of: {', '.join(VALID_ALERT_POLICIES)}",
        )
    await run_in_threadpool(_replace_settings_on_disk, env, alert_policy=candidate)
    return RedirectResponse(url="/continuous", status_code=303)


@router.post("/watch/add")
async def watch_add(rules: str = Form("")):
    """Add rule numbers to the watchlist. Accepts space- or comma-separated input."""
    env = _active_env()
    if not env:
        raise HTTPException(status_code=400, detail="No active environment.")
    new_rules = _split_watch_input(rules)
    if not new_rules:
        raise HTTPException(status_code=400, detail="No rule numbers supplied.")
    current = await run_in_threadpool(read_settings, env)
    existing = set(current.watchlist)
    additions = [r for r in new_rules if r not in existing]
    if not additions:
        # Idempotent — return to the page without changing settings.
        return RedirectResponse(url="/continuous", status_code=303)
    merged = tuple(list(current.watchlist) + additions)
    await run_in_threadpool(_replace_settings_on_disk, env, watchlist=merged)
    return RedirectResponse(url="/continuous", status_code=303)


@router.post("/watch/remove")
async def watch_remove(rule: str = Form("")):
    """Remove a single rule number from the watchlist."""
    env = _active_env()
    if not env:
        raise HTTPException(status_code=400, detail="No active environment.")
    norm = (rule or "").strip().upper()
    if not norm:
        raise HTTPException(status_code=400, detail="rule is required")
    current = await run_in_threadpool(read_settings, env)
    new_list = tuple(r for r in current.watchlist if r != norm)
    if len(new_list) == len(current.watchlist):
        return RedirectResponse(url="/continuous", status_code=303)
    await run_in_threadpool(_replace_settings_on_disk, env, watchlist=new_list)
    return RedirectResponse(url="/continuous", status_code=303)


@router.post("/watch/clear")
async def watch_clear():
    """Empty the watchlist (alert on all rules again)."""
    env = _active_env()
    if not env:
        raise HTTPException(status_code=400, detail="No active environment.")
    await run_in_threadpool(_replace_settings_on_disk, env, watchlist=())
    return RedirectResponse(url="/continuous", status_code=303)


@router.post("/run-now")
async def run_now():
    env = _active_env()
    if not env:
        raise HTTPException(status_code=400, detail="No active environment.")
    # Process-local guard: reject duplicate clicks while a previous run is
    # still executing. The engine takes a cross-process file lock too, but
    # that one would block the threadpool worker; this short-circuit returns
    # 429 immediately so the UI can render a "run already in progress" hint.
    async with _in_flight_lock:
        if env in _in_flight_runs:
            raise HTTPException(status_code=429, detail="A run is already in progress for this environment.")
        _in_flight_runs.add(env)
    try:
        await run_in_threadpool(run_once, environment=env)
    finally:
        async with _in_flight_lock:
            _in_flight_runs.discard(env)
    return RedirectResponse(url="/continuous", status_code=303)


# TODO: Log file watching — deferred to a later version of Atlas.
# The routes below are preserved but not active. To re-enable:
#   1. Uncomment this entire section
#   2. Uncomment the _LW_* constants and log-watch model imports above
#   3. Restore the lw_* keys in the landing() template_context call
#   4. Uncomment the log-watch section in landing.html

# def _validate_lw_id(watch_id: str) -> str:
#     if not _LW_ID_RE.match(watch_id or ""):
#         raise HTTPException(status_code=400, detail="Invalid watch ID format.")
#     return watch_id
#
#
# @router.post("/log-watch/enable")
# async def lw_enable():
#     env = _active_env()
#     if not env:
#         raise HTTPException(status_code=400, detail="No active environment.")
#     await run_in_threadpool(_replace_settings_on_disk, env, log_watch_enabled=True)
#     return RedirectResponse(url="/continuous", status_code=303)
#
#
# @router.post("/log-watch/disable")
# async def lw_disable():
#     env = _active_env()
#     if not env:
#         raise HTTPException(status_code=400, detail="No active environment.")
#     await run_in_threadpool(_replace_settings_on_disk, env, log_watch_enabled=False)
#     return RedirectResponse(url="/continuous", status_code=303)
#
#
# @router.post("/log-watch/add")
# async def lw_add(
#     name: str = Form(""),
#     pattern: str = Form(""),
#     log_source: str = Form("any"),
#     severity: str = Form("warning"),
#     threshold_mode: str = Form(LOG_WATCH_ANY),
#     threshold_count: int = Form(1),
#     threshold_window_minutes: int = Form(60),
# ):
#     import hashlib as _hl
#     import time as _t
#     env = _active_env()
#     if not env:
#         raise HTTPException(status_code=400, detail="No active environment.")
#     name = name.strip()
#     pattern = pattern.strip()
#     if not name:
#         raise HTTPException(status_code=400, detail="Watch name is required.")
#     if not pattern or pattern not in DEFAULT_KEYWORDS:
#         raise HTTPException(status_code=400, detail="Pattern must be one of the approved log keywords.")
#     if log_source not in LOG_WATCH_SOURCES:
#         raise HTTPException(status_code=400, detail=f"Invalid log source: {log_source!r}.")
#     if severity not in ("critical", "warning", "info"):
#         raise HTTPException(status_code=400, detail=f"Invalid severity: {severity!r}.")
#     if threshold_mode not in VALID_LOG_WATCH_THRESHOLDS:
#         raise HTTPException(status_code=400, detail=f"Invalid threshold mode: {threshold_mode!r}.")
#     if threshold_mode in (LOG_WATCH_COUNT, LOG_WATCH_WINDOW) and threshold_count < _MIN_THRESHOLD_COUNT:
#         raise HTTPException(status_code=400,
#             detail=f"Threshold count must be at least {_MIN_THRESHOLD_COUNT} for {threshold_mode!r} mode.")
#     if threshold_mode == LOG_WATCH_WINDOW and threshold_window_minutes < _MIN_WINDOW_MINUTES:
#         raise HTTPException(status_code=400, detail=f"Window must be at least {_MIN_WINDOW_MINUTES} minutes.")
#     current = await run_in_threadpool(read_settings, env)
#     for w in current.log_watches:
#         if w.name.lower() == name.lower():
#             raise HTTPException(status_code=400, detail=f"A watch named '{name}' already exists.")
#     watch_id = _hl.md5(f"{pattern}:{_t.time()}".encode()).hexdigest()[:8]
#     new_entry = LogWatchEntry(
#         id=watch_id, name=name, pattern=pattern, log_source=log_source,
#         severity=severity, threshold_mode=threshold_mode, threshold_count=threshold_count,
#         threshold_window_minutes=threshold_window_minutes, enabled=True,
#     )
#     new_watches = tuple(list(current.log_watches) + [new_entry])
#     await run_in_threadpool(_replace_settings_on_disk, env, log_watches=new_watches)
#     return RedirectResponse(url="/continuous", status_code=303)
#
#
# @router.post("/log-watch/{watch_id}/toggle")
# async def lw_toggle(watch_id: str):
#     _validate_lw_id(watch_id)
#     env = _active_env()
#     if not env:
#         raise HTTPException(status_code=400, detail="No active environment.")
#     current = await run_in_threadpool(read_settings, env)
#     new_watches = tuple(
#         LogWatchEntry.from_dict({**w.to_dict(), "enabled": not w.enabled})
#         if w.id == watch_id else w
#         for w in current.log_watches
#     )
#     await run_in_threadpool(_replace_settings_on_disk, env, log_watches=new_watches)
#     return RedirectResponse(url="/continuous", status_code=303)
#
#
# @router.post("/log-watch/{watch_id}/remove")
# async def lw_remove(watch_id: str):
#     _validate_lw_id(watch_id)
#     env = _active_env()
#     if not env:
#         raise HTTPException(status_code=400, detail="No active environment.")
#     current = await run_in_threadpool(read_settings, env)
#     new_watches = tuple(w for w in current.log_watches if w.id != watch_id)
#     await run_in_threadpool(_replace_settings_on_disk, env, log_watches=new_watches)
#     return RedirectResponse(url="/continuous", status_code=303)
