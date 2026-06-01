"""Job routes — list, view, and SSE-stream live progress."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from platform_atlas.core._version import __version__ as ATLAS_VERSION

from platform_atlas_webui.dependencies import get_templates, template_context
from platform_atlas_webui.services.jobs import get_registry

router = APIRouter(prefix="/jobs", tags=["jobs"])
_templates = get_templates()


@router.get("", response_class=HTMLResponse)
async def list_jobs(request: Request) -> HTMLResponse:
    reg = get_registry()
    items = []
    for j in reg.list():
        items.append({
            "id": j.id,
            "name": j.name,
            "status": j.status.value,
            "created_at": _fmt(j.created_at),
            "started_at": _fmt(j.started_at) if j.started_at else "",
            "finished_at": _fmt(j.finished_at) if j.finished_at else "",
            "error": j.error or "",
        })
    return _templates.TemplateResponse(
        request,
        "jobs/list.html",
        template_context(request, atlas_version=ATLAS_VERSION, jobs=items),
    )


@router.get("/{job_id}", response_class=HTMLResponse)
async def view_job(request: Request, job_id: str) -> HTMLResponse:
    reg = get_registry()
    record = reg.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return _templates.TemplateResponse(
        request,
        "jobs/detail.html",
        template_context(
            request,
            atlas_version=ATLAS_VERSION,
            job={
                "id": record.id,
                "name": record.name,
                "status": record.status.value,
                "created_at": _fmt(record.created_at),
                "started_at": _fmt(record.started_at) if record.started_at else "",
                "finished_at": _fmt(record.finished_at) if record.finished_at else "",
                "error": record.error or "",
                "metadata": record.metadata or {},
                "started_at_epoch": record.started_at or 0,
            },
        ),
    )


@router.get("/{job_id}/stream")
async def stream_job(job_id: str):
    reg = get_registry()
    record = reg.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    async def _event_source():
        async for event in reg.stream(job_id):
            yield event.to_sse()

    return StreamingResponse(
        _event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # disable nginx buffering when behind a proxy
            "Connection": "keep-alive",
        },
    )


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str):
    reg = get_registry()
    record = reg.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if record.is_terminal():
        return {"ok": False, "reason": "already finished"}
    ok = reg.cancel(job_id)
    return {"ok": ok}


def _fmt(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
