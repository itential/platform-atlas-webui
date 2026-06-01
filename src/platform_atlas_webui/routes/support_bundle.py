"""Support Bundle route — form intake, job kick-off, and ZIP download."""

from __future__ import annotations

import os as _os
import tempfile as _tempfile
from pathlib import Path as _Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from starlette.background import BackgroundTask as _BackgroundTask

from platform_atlas.core._version import __version__ as ATLAS_VERSION

from platform_atlas_webui.dependencies import get_templates, template_context
from platform_atlas_webui.security.paths import safe_under
from platform_atlas_webui.services.jobs import get_registry
from platform_atlas_webui.services import runners

router = APIRouter(prefix="/support-bundle", tags=["support-bundle"])
_templates = get_templates()
_TMPDIR = _Path(_tempfile.gettempdir())


def _safe_unlink(p: _Path) -> None:
    try:
        _os.unlink(p)
    except OSError:
        pass


@router.get("", response_class=HTMLResponse)
async def support_bundle_form(request: Request, job: str = "") -> HTMLResponse:
    """Render the support bundle page — form when no job, progress/result when one is given."""
    reg = get_registry()
    job_record = None
    if job:
        rec = reg.get(job)
        if rec is not None:
            job_record = {
                "id": rec.id,
                "name": rec.name,
                "status": rec.status.value,
                "result": rec.result or {},
                "error": rec.error or "",
                "metadata": rec.metadata or {},
            }

    active_env = ""
    tier = "standard"
    try:
        from platform_atlas.core.context import ctx
        atlas = ctx()
        active_env = getattr(atlas, "active_environment", "") or ""
        tier = (getattr(atlas.config, "tier", "") or "standard").lower()
    except Exception:  # noqa: BLE001
        pass

    return _templates.TemplateResponse(
        request,
        "support_bundle/index.html",
        template_context(
            request,
            atlas_version=ATLAS_VERSION,
            job=job_record,
            active_env=active_env,
            tier=tier,
        ),
    )


@router.post("/run")
async def run_support_bundle(
    request: Request,
    ticket: str = Form("", max_length=9),
    description: str = Form("", max_length=512),
    log_days: int = Form(7),
):
    """Validate form input, kick off a support bundle job, redirect to progress page."""
    import re
    ticket = ticket.strip().upper()
    description = description.strip()
    if not re.fullmatch(r"ISD-\d{4,5}", ticket):
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="Ticket number must be ISD- followed by 4 or 5 digits.")
    log_days = max(1, min(log_days, 30))

    # Capture tier before submission so the progress view can display it
    # even while the job is still queued/running.
    active_tier = "standard"
    try:
        from platform_atlas.core.context import ctx
        active_tier = (getattr(ctx().config, "tier", "") or "standard").lower()
    except Exception:  # noqa: BLE001
        pass

    reg = get_registry()
    record = await reg.submit(
        "support bundle",
        runners.run_support_bundle_job,
        ticket=ticket,
        description=description,
        log_days=log_days,
        metadata={},
    )
    # Set return_url and display metadata after submit — record.id is stable.
    record.metadata["return_url"] = f"/support-bundle?job={record.id}"
    record.metadata["ticket"] = ticket
    record.metadata["description"] = description
    record.metadata["log_days"] = log_days
    record.metadata["tier"] = active_tier

    # Redirect to the dedicated support bundle progress/result page, not the
    # generic job stream — the support bundle page has purpose-built UX.
    return RedirectResponse(url=f"/support-bundle?job={record.id}", status_code=303)


@router.get("/download/{job_id}")
async def download_bundle(job_id: str):
    """Serve the completed support bundle ZIP from the temp file the runner wrote.

    Single-use — the temp file is deleted after the response streams. If the
    user navigates back and tries to download again, they'll get a 404 with a
    clear message.
    """
    reg = get_registry()
    rec = reg.get(job_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Bundle job not found")
    if not rec.is_terminal():
        raise HTTPException(status_code=409, detail="Bundle job is still running")
    if rec.status.value != "succeeded":
        raise HTTPException(status_code=400, detail=f"Bundle job failed: {rec.error}")

    result = rec.result or {}
    bundle_path_str = result.get("bundle_path", "")
    bundle_name = result.get("bundle_name", "atlas-support-bundle.zip")

    if not bundle_path_str:
        raise HTTPException(status_code=404, detail="Bundle path not in job result")

    bundle_path = _Path(bundle_path_str)
    try:
        safe_path = safe_under(bundle_path, _TMPDIR)
    except (ValueError, HTTPException):
        raise HTTPException(status_code=403, detail="Bundle path is outside allowed directory")

    if not safe_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Bundle file not found — it may have already been downloaded.",
        )

    return FileResponse(
        str(safe_path),
        media_type="application/zip",
        filename=bundle_name,
        background=_BackgroundTask(_safe_unlink, safe_path),
    )
