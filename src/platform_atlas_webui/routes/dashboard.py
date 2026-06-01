"""Dashboard route — landing page with at-a-glance status."""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse

from platform_atlas.core._version import __version__ as ATLAS_VERSION
from platform_atlas.core.paths import ATLAS_RULESET_UPDATE_STATE

from platform_atlas_webui.dependencies import get_templates, template_context
from platform_atlas_webui.services import (
    environments as env_svc,
    sessions as session_svc,
    rulesets as ruleset_svc,
)
from platform_atlas_webui.services.jobs import get_registry

router = APIRouter()
_templates = get_templates()


def _ruleset_update_notice() -> list[dict]:
    """Return pending ruleset updates from the state file, or [] if none."""
    try:
        if not ATLAS_RULESET_UPDATE_STATE.is_file():
            return []
        with open(ATLAS_RULESET_UPDATE_STATE, encoding="utf-8") as f:
            return json.load(f).get("updates", [])
    except Exception:
        return []


def _gather_dashboard_data() -> dict:
    """One-shot: read the on-disk state needed by the dashboard. Sync — threadpool only."""
    # Reuse the session list for the calendar aggregation so we don't open
    # every session.json twice per request.
    sessions = session_svc.list_sessions()
    environments = env_svc.list_environments()
    ruleset_summary = ruleset_svc.get_active_ruleset_summary()
    calendar = session_svc.calendar_buckets_from(sessions)
    return {
        "sessions": sessions,
        "environments": environments,
        "ruleset_summary": ruleset_summary,
        "calendar": calendar,
    }


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    data = await run_in_threadpool(_gather_dashboard_data)
    ruleset_updates = _ruleset_update_notice()
    sessions = data["sessions"]
    environments = data["environments"]
    ruleset_summary = data["ruleset_summary"]
    calendar = data["calendar"]

    recent_jobs = []
    for j in get_registry().list()[:5]:
        recent_jobs.append({
            "id": j.id,
            "name": j.name,
            "status": j.status.value,
        })

    try:
        from platform_atlas.core.context import ctx
        atlas = ctx()
        org = atlas.config.organization_name
    except Exception:
        org = ""

    active_session = next((s for s in sessions if s["is_active"]), None)
    active_env = next((e for e in environments if e["is_active"]), None)

    kpis = {
        "session_count": len(sessions),
        "env_count": len(environments),
        "rule_count": ruleset_summary["rule_count"],
        "running_jobs": sum(1 for j in get_registry().list() if j.status.value == "running"),
    }

    return _templates.TemplateResponse(
        request,
        "dashboard.html",
        template_context(
            request,
            atlas_version=ATLAS_VERSION,
            organization_name=org,
            kpis=kpis,
            active_session=active_session,
            active_env=active_env,
            ruleset=ruleset_summary,
            recent_jobs=recent_jobs,
            calendar=calendar,
            ruleset_updates=ruleset_updates,
        ),
    )
