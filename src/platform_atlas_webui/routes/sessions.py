"""Session routes — CRUD plus capture/validate/report job kickoffs."""

from __future__ import annotations

import os as _os
import re as _re
import tempfile as _tempfile
from pathlib import Path as _Path
from urllib.parse import quote as _qs

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.background import BackgroundTask as _BackgroundTask

import json

from platform_atlas.core._version import __version__ as ATLAS_VERSION
from platform_atlas.core.exceptions import SessionAlreadyExistsError

from platform_atlas_webui.dependencies import get_templates, template_context
from platform_atlas_webui.security.paths import safe_under
from platform_atlas_webui.services import sessions as session_svc
from platform_atlas_webui.services import rulesets as ruleset_svc
from platform_atlas_webui.services.environments import active_env_allows_legacy, get_environment
from platform_atlas_webui.services.jobs import get_registry
from platform_atlas_webui.services import runners

_TMPDIR = _Path(_tempfile.gettempdir())


def _safe_unlink_tmp(p: _Path) -> None:
    try:
        _os.unlink(p)
    except OSError:
        pass

router = APIRouter(prefix="/sessions", tags=["sessions"])
_templates = get_templates()


def _filter_legacy(items: list, allow: bool) -> list:
    if allow:
        return items
    return [item for item in items if not item.get("is_legacy")]


_SESSIONS_PER_PAGE_OPTIONS = (20, 50, 100)


@router.get("", response_class=HTMLResponse)
async def list_sessions(
    request: Request,
    page: int = 1,
    per_page: int = 20,
    lock_view: str = "",
    lock_run: str = "",
) -> HTMLResponse:
    # ``list_sessions`` opens N session.json files — push the disk I/O
    # off the event loop so concurrent requests don't serialize behind it.
    items = await run_in_threadpool(session_svc.list_sessions)
    # Clamp inputs so a hand-crafted URL can't surface a 500 — drop unknown
    # per_page values back to the default and pin page to the valid range.
    if per_page not in _SESSIONS_PER_PAGE_OPTIONS:
        per_page = 20
    total = len(items)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    paged = items[start:start + per_page]
    # Pull the active session out of the full list so the "active session"
    # banner shows even when that session lives on a different page.
    active = next((s for s in items if s.get("is_active")), None)
    # Build a tier-lock flash banner if the user was bounced here from a
    # blocked /sessions/<name> view or run attempt. Cap the inputs so a
    # hand-crafted URL can't render an arbitrary string as part of the page.
    locked_session = None
    lock_kind = ""
    locked_name = (lock_view or lock_run or "").strip()[:64]
    if locked_name:
        locked_session = next((s for s in items if s.get("name") == locked_name), None)
        if locked_session:
            lock_kind = "view" if lock_view else "run"
    return _templates.TemplateResponse(
        request,
        "sessions/list.html",
        template_context(
            request,
            atlas_version=ATLAS_VERSION,
            sessions=paged,
            sessions_total=total,
            sessions_page=page,
            sessions_per_page=per_page,
            sessions_total_pages=total_pages,
            sessions_per_page_options=_SESSIONS_PER_PAGE_OPTIONS,
            active_session_banner=active,
            tier_lock_flash={
                "session": locked_session,
                "kind": lock_kind,
            } if locked_session else None,
        ),
    )


def _active_tier() -> str:
    """Resolve the active tier the way load_config() does (overlay > root)."""
    from platform_atlas_webui.services import config as _cfg_svc
    return _cfg_svc.resolve_active_tier()


def _active_environment() -> str | None:
    """Return the active environment name, or None if none is set."""
    from platform_atlas_webui.services import config as _cfg_svc
    cfg = _cfg_svc.read_config()
    return cfg.get("active_environment") or None


@router.get("/new", response_class=HTMLResponse)
async def new_session_form(request: Request) -> HTMLResponse:
    allow_legacy = active_env_allows_legacy()
    try:
        rulesets = _filter_legacy(await run_in_threadpool(ruleset_svc.list_rulesets), allow_legacy)
        profiles = _filter_legacy(await run_in_threadpool(ruleset_svc.list_profiles), allow_legacy)
    except Exception:  # noqa: BLE001
        rulesets = []
        profiles = []
    return _templates.TemplateResponse(
        request,
        "sessions/form.html",
        template_context(
            request,
            atlas_version=ATLAS_VERSION,
            active_tier=_active_tier(),
            active_environment=_active_environment(),
            rulesets=rulesets,
            profiles=profiles,
        ),
    )


@router.post("", response_class=HTMLResponse)
async def create_session(
    request: Request,
    # Server-side caps. Name pattern is also enforced by the form's
    # `pattern` attr + the validate-name endpoint, but we cap here too
    # so a hand-crafted POST can't slip past the client.
    name: str = Form(..., max_length=64),
    description: str = Form("", max_length=4096),
    ruleset_id: str = Form("", max_length=128),
    ruleset_profile: str = Form("", max_length=128),
):
    name = name.strip()
    try:
        created = session_svc.create_session(
            name=name,
            description=description,
            tier=_active_tier(),
            environment=_active_environment() or "",
            ruleset_id=ruleset_id.strip(),
            ruleset_profile=ruleset_profile.strip(),
        )
    except SessionAlreadyExistsError:
        # Re-render the form with an inline error and the submitted values pre-filled
        # so the user can pick a different name without losing their other choices.
        allow_legacy = active_env_allows_legacy()
        try:
            rulesets = _filter_legacy(await run_in_threadpool(ruleset_svc.list_rulesets), allow_legacy)
            profiles = _filter_legacy(await run_in_threadpool(ruleset_svc.list_profiles), allow_legacy)
        except Exception:  # noqa: BLE001
            rulesets = []
            profiles = []
        return _templates.TemplateResponse(
            request,
            "sessions/form.html",
            template_context(
                request,
                atlas_version=ATLAS_VERSION,
                active_tier=_active_tier(),
                active_environment=_active_environment(),
                rulesets=rulesets,
                profiles=profiles,
                flash={"kind": "error", "message": f"A session named \"{name}\" already exists. Choose a different name."},
                prefill={"name": name, "description": description, "ruleset_id": ruleset_id, "ruleset_profile": ruleset_profile},
            ),
            status_code=422,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}") from exc
    return RedirectResponse(url=f"/sessions/{created}", status_code=303)


def _available_pipelines() -> list[dict]:
    """Discover pipeline metadata for the aggregation selection UI."""
    try:
        from platform_atlas.capture.utils import discover_pipelines as _dp
        from platform_atlas.core.paths import ATLAS_PIPELINES_DIR as _pdir
        return [
            {"name": p.name, "collection": p.collection, "desc": p.desc}
            for p in _dp(_pdir)
        ]
    except Exception:
        return []


# ── Live name-collision check for the create form ─────────────────────
# Registered ABOVE `/{name}` so FastAPI matches it before the dynamic
# session-detail route. The form input fires this with hx-trigger
# "keyup changed delay:300ms"; the response is a small status pill
# swapped into a sibling div.
_SESSION_NAME_PATTERN = _re.compile(r"^[a-zA-Z0-9_-]{3,64}$")


def _name_status(tone: str, dot: str, text: str) -> str:
    return (
        f'<span class="inline-flex items-center gap-1.5 text-{tone} text-[12px] font-medium">'
        f'<span class="inline-block w-1.5 h-1.5 rounded-full bg-{dot}"></span>'
        f'{text}</span>'
    )


@router.get("/validate-name", response_class=HTMLResponse)
async def validate_session_name(value: str = "") -> HTMLResponse:
    value = value.strip()
    if not value:
        return HTMLResponse('<span class="text-text-3 text-[12px]">Type a name above.</span>')
    if not _SESSION_NAME_PATTERN.match(value):
        return HTMLResponse(_name_status("warn", "warn", "3–64 chars · letters, digits, hyphen, underscore"))
    existing = await run_in_threadpool(session_svc.list_sessions)
    if any(s["name"].lower() == value.lower() for s in existing):
        return HTMLResponse(_name_status("bad", "bad", f'"{value}" is already in use'))
    return HTMLResponse(_name_status("ok", "ok", f'"{value}" is available'))


@router.get("/{name}", response_class=HTMLResponse)
async def view_session(
    request: Request, name: str, lock_run: str = "",
) -> HTMLResponse:
    session = await run_in_threadpool(session_svc.get_session, name)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session '{name}' not found")
    # Tier guard — incomplete sessions on the wrong tier are bounced to the
    # list page where the user can either switch tier or pick a different
    # session. Complete sessions are allowed through (view_blocked is False)
    # so finished audits stay readable across tier switches; the run buttons
    # on the detail page handle their own block via run_blocked.
    if session.get("tier_lock", {}).get("view_blocked"):
        return RedirectResponse(
            url=f"/sessions?lock_view={_qs(name)}", status_code=303,
        )
    pipelines = await run_in_threadpool(_available_pipelines)

    cm_reminder_cmd = None
    # Whether the session's bound environment is missing any required
    # credentials. Reuses the same helper the environments detail page uses so
    # the two stay in sync. Empty list (the default) means "all set" or "no
    # bound env" — either way the capture step renders without the warning.
    missing_creds: list[dict] = []
    env_name = session.get("environment", "")
    if env_name:
        env = await run_in_threadpool(get_environment, env_name)
        if env:
            from platform_atlas_webui.routes.environments import _missing_credentials
            missing_creds = await run_in_threadpool(_missing_credentials, env)
            nodes = (env.get("data") or {}).get("deployment", {}).get("nodes") or []
            for node in nodes:
                if node.get("transport") == "control_master":
                    socket = node.get("ssh_control_socket", "")
                    target = node.get("ssh_control_target", "")
                    port = node.get("ssh_port") or 22
                    if socket and target:
                        port_flag = f"-p {port} " if port != 22 else ""
                        cm_reminder_cmd = (
                            f"ssh -M -S {socket} {port_flag}"
                            f"-o ControlPersist=10m "
                            f"-o StrictHostKeyChecking=no "
                            f"-o UserKnownHostsFile=/dev/null "
                            f"-fN {target}"
                        )
                    break

    return _templates.TemplateResponse(
        request,
        "sessions/detail.html",
        template_context(
            request,
            atlas_version=ATLAS_VERSION,
            session=session,
            available_pipelines=pipelines,
            run_lock_flash=bool(lock_run),
            cm_reminder_cmd=cm_reminder_cmd,
            missing_creds=missing_creds,
        ),
    )


@router.post("/{name}/activate")
async def activate_session(name: str):
    try:
        session_svc.activate(name)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/sessions", status_code=303)


@router.post("/{name}/pin")
async def toggle_pin(name: str):
    """Toggle the pinned state of ``name`` in config.json.

    Persists to ``~/.atlas/config.json::pinned_sessions``. The pin is per
    Atlas-home, so it follows the user across browsers. After the toggle
    we redirect back to the list so the new order is visible without a
    client-side re-render — the list view already partitions pinned-first.
    """
    from platform_atlas_webui.dependencies import attach_toast
    from platform_atlas_webui.services import config as _cfg_svc
    try:
        is_pinned = _cfg_svc.toggle_pinned_session(name)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response = RedirectResponse(url="/sessions", status_code=303)
    return attach_toast(
        response,
        kind="success",
        msg=f"Pinned “{name}” to top." if is_pinned else f"Unpinned “{name}”.",
    )


@router.post("/{name}/delete")
async def delete_session(name: str):
    try:
        session_svc.delete(name, force=True)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/sessions", status_code=303)


# ── Run actions — each kicks off a job and redirects to the live stream ──

def _build_session_json_export(name: str) -> _Path:
    """Render the JSON report to a temp file. Sync — runs in threadpool."""
    import tempfile
    import pandas as pd
    from pathlib import Path
    from platform_atlas.reporting.reporting_engine import export_json_report
    from platform_atlas.core.session_manager import get_session_manager

    mgr = get_session_manager()
    session = mgr.get(name)

    if not session.validation_file.exists():
        raise HTTPException(
            status_code=400,
            detail=f"Session '{name}' has no validation results yet — run validate first.",
        )

    df = pd.read_parquet(session.validation_file)

    # Rehydrate metadata (same as report runner)
    if session.capture_file.exists():
        try:
            with session.capture_file.open(encoding="utf-8") as f:
                cap = json.load(f)
            atlas_meta = (cap.get("_atlas") or {}).get("metadata") or {}
            for key, val in atlas_meta.items():
                df.attrs.setdefault(key, val)
        except Exception:
            pass

    with tempfile.NamedTemporaryFile(suffix=".json", prefix=f"atlas-{name}-", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    export_json_report(df, tmp_path)
    return tmp_path


@router.get("/{name}/summary.json")
async def session_summary(name: str):
    """Lightweight JSON summary used by the post-pipeline mini-report.

    Returns counts, compliance %, and a top-N slice of failing rules ordered
    by severity. Reads the validation parquet, so this only returns useful
    data once validation has completed.
    """
    summary = await run_in_threadpool(session_svc.get_session_summary, name)
    if summary is None:
        raise HTTPException(status_code=404, detail=f"Session '{name}' not found")
    return JSONResponse(summary)


@router.get("/{name}/export/json")
async def export_session_json(name: str):
    """Generate and return a JSON compliance report for the session."""
    try:
        # Pandas/pyarrow + JSON export is sync — push it off the event loop.
        tmp_path = await run_in_threadpool(_build_session_json_export, name)
    except HTTPException:
        raise
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Session '{name}' not found") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="Could not export session JSON") from exc

    safe_tmp = safe_under(tmp_path, _TMPDIR)
    # Clean up after the response streams — previously this leaked one signed
    # JSON artifact per request into /tmp until the OS reaped it.
    return FileResponse(
        str(safe_tmp),
        media_type="application/json",
        filename=f"{name}_report.json",
        background=_BackgroundTask(_safe_unlink_tmp, safe_tmp),
    )


@router.post("/{name}/retry-log-capture", response_class=HTMLResponse)
async def retry_log_capture(
    request: Request,
    name: str,
    module: str = Form(..., max_length=32),
    custom_path: str = Form(..., max_length=1024),
) -> HTMLResponse:
    """Re-run a single failed log module against a user-supplied path.

    Surfaces under each failed-log card on the session detail page. The
    response is an HTMX-style fragment that replaces the card with its
    success or error state; full-page redirects would lose the inline
    feedback that makes the retry feel immediate.

    On success the path is auto-persisted to the active environment so
    future captures use it without re-prompting.
    """
    session = session_svc.get_session(name)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session '{name}' not found")
    if session.get("tier_lock", {}).get("run_blocked"):
        return _retry_fragment(
            module=module,
            ok=False,
            message="Retry blocked — active tier doesn't match the session's tier.",
            session_name=name,
            request=request,
        )

    custom_path = custom_path.strip()

    try:
        result = await run_in_threadpool(
            session_svc.retry_log_capture,
            name,
            module=module,
            custom_path=custom_path,
        )
    except ValueError as exc:
        return _retry_fragment(
            module=module, ok=False, message=str(exc),
            session_name=name, request=request,
        )
    except Exception as exc:  # noqa: BLE001
        return _retry_fragment(
            module=module, ok=False,
            message=f"{type(exc).__name__}: {exc}",
            session_name=name, request=request,
        )

    return _retry_fragment(
        module=module,
        ok=bool(result.get("ok")),
        message=str(result.get("message") or ""),
        session_name=name,
        request=request,
        persisted=bool(result.get("persisted")),
        persist_error=str(result.get("persist_error") or ""),
        custom_path=custom_path,
    )


def _retry_fragment(
    *,
    module: str,
    ok: bool,
    message: str,
    session_name: str,
    request: Request,
    persisted: bool = False,
    persist_error: str = "",
    custom_path: str = "",
) -> HTMLResponse:
    """Render the small HTML fragment that replaces the retry card.

    On success, hides the form and shows the recovered path. On failure,
    redisplays the form with an inline error so the user can adjust the
    path without re-navigating.
    """
    return _templates.TemplateResponse(
        request,
        "sessions/_log_retry_result.html",
        template_context(
            request,
            module=module,
            ok=ok,
            message=message,
            session_name=session_name,
            persisted=persisted,
            persist_error=persist_error,
            custom_path=custom_path,
        ),
    )


@router.post("/{name}/run/{stage}")
async def run_session_stage(
    request: Request,
    name: str,
    stage: str,
    run_aggregations: str = Form(""),
):
    session = session_svc.get_session(name)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session '{name}' not found")
    # Tier guard — refuse to kick off a job on a tier-mismatched session.
    # Without this, capture/validate/report would either fail on the wrong
    # collectors or silently produce data that can't be replayed under the
    # session's bound tier. The user has to flip the active tier first.
    lock = session.get("tier_lock", {})
    if lock.get("run_blocked"):
        # Complete sessions are still viewable, so bounce back to detail and
        # let the banner there explain why nothing happened. Incomplete
        # sessions are also view-blocked, so send them to the list instead.
        if lock.get("view_blocked"):
            return RedirectResponse(
                url=f"/sessions?lock_run={_qs(name)}", status_code=303,
            )
        return RedirectResponse(
            url=f"/sessions/{_qs(name)}?lock_run=1", status_code=303,
        )
    # Capture/validate/report run against the active session AND its bound
    # env+ruleset, so we must restore the full context — not just the pointer.
    session_svc.activate(name)

    # Checkbox semantics differ by stage:
    # - "capture": the checkbox is in the same form, so "" means unchecked = opt out.
    # - "all": the hero form mirrors the checkbox via JS. JS sends "1" (checked) or
    #   "0" (explicitly unchecked). An empty string means JS didn't run — fall back
    #   to True so extended-tier sessions don't silently skip aggregations.
    if stage == "all":
        aggregations_on = run_aggregations != "0"
    else:
        aggregations_on = bool(run_aggregations)

    # Use getlist() directly on the raw form data — FastAPI's List[str] Form
    # annotation is unreliable for single submitted values (may call .get()
    # instead of .getlist() internally, returning a bare string that then
    # passes through to set() as individual characters).
    form = await request.form()
    raw_names: list[str] = list(form.getlist("pipeline_names"))
    # Empty list means no pipelines were checked or the selection panel wasn't
    # rendered (no pipelines on disk, Standard tier) — treat as "run all".
    selected_pipelines: list[str] | None = raw_names if raw_names else None

    reg = get_registry()
    if stage == "capture":
        # ``resume_checkpoint`` is "0" when the user explicitly chose "Start over"
        # from the checkpoint banner; anything else (including the default "1" from
        # the normal Run capture button) resumes from the checkpoint if one exists.
        resume_capture = form.get("resume_checkpoint", "1") != "0"
        record = await reg.submit(
            f"capture: {name}",
            runners.run_capture_job,
            session_name=name,
            resume=resume_capture,
            run_aggregations=aggregations_on,
            pipeline_names=selected_pipelines,
            metadata={"return_url": f"/sessions/{name}"},
        )
    elif stage == "validate":
        record = await reg.submit(
            f"validate: {name}",
            runners.run_validate_job,
            session_name=name,
            metadata={"return_url": f"/sessions/{name}"},
        )
    elif stage == "report":
        record = await reg.submit(
            f"report: {name}",
            runners.run_report_job,
            session_name=name,
            metadata={"return_url": f"/sessions/{name}"},
        )
    elif stage == "all":
        record = await reg.submit(
            f"full pipeline: {name}",
            runners.run_full_pipeline_job,
            session_name=name,
            run_aggregations=aggregations_on,
            pipeline_names=selected_pipelines,
            metadata={"return_url": f"/sessions/{name}", "session_name": name},
        )
    else:
        raise HTTPException(status_code=400, detail=f"Unknown stage: {stage}")

    return RedirectResponse(url=f"/jobs/{record.id}", status_code=303)
