"""
Alerts routes — drift event timeline + ack actions.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from platform_atlas.core._version import __version__ as ATLAS_VERSION
from platform_atlas.continuous import alerts as alerts_mod, storage
from platform_atlas.continuous.models import AlertStatus

from platform_atlas_webui.dependencies import get_atlas_context, get_templates, template_context

router = APIRouter(prefix="/alerts", tags=["alerts"])
_templates = get_templates()


def _active_env() -> str | None:
    try:
        atlas = get_atlas_context()
        return atlas.active_environment
    except HTTPException:
        return None


@router.get("", response_class=HTMLResponse)
async def landing(
    request: Request,
    severity: str = "",
    unacked: int = 0,
) -> HTMLResponse:
    env = _active_env()
    items = await run_in_threadpool(
        alerts_mod.list_alerts, env or "",
        severity=severity or None,
        only_unacked=bool(unacked),
    )
    counts = alerts_mod.counts(env or "")
    events = await run_in_threadpool(storage.read_events, env or "", limit=100)
    return _templates.TemplateResponse(
        request,
        "alerts/landing.html",
        template_context(
            request,
            atlas_version=ATLAS_VERSION,
            env=env,
            alerts=items,
            counts=counts,
            events=events,
            filter_severity=severity,
            filter_unacked=bool(unacked),
            AlertStatus=AlertStatus,
        ),
    )


@router.post("/{alert_id}/ack")
async def ack_one(alert_id: str):
    env = _active_env()
    if not env:
        raise HTTPException(status_code=400, detail="No active environment.")
    alerts_mod.ack_alert(env, alert_id, actor="webui")
    return RedirectResponse(url="/alerts", status_code=303)


@router.post("/ack-all")
async def ack_all():
    env = _active_env()
    if not env:
        raise HTTPException(status_code=400, detail="No active environment.")
    alerts_mod.ack_all(env, actor="webui")
    return RedirectResponse(url="/alerts", status_code=303)
