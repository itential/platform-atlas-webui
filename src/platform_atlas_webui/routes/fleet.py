"""
Fleet route — multi-environment compliance overview rendered from the local
cache. Read-only; never triggers a capture.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, Response

from platform_atlas.core._version import __version__ as ATLAS_VERSION
from platform_atlas.core.fleet import collect_fleet

from platform_atlas_webui.dependencies import get_templates, template_context

router = APIRouter(prefix="/fleet", tags=["fleet"])
_templates = get_templates()


@router.get("", response_class=HTMLResponse)
async def landing(request: Request) -> HTMLResponse:
    entries, summary = await run_in_threadpool(collect_fleet)
    return _templates.TemplateResponse(
        request,
        "fleet/landing.html",
        template_context(
            request,
            atlas_version=ATLAS_VERSION,
            fleet=entries,
            fleet_summary=summary,
        ),
    )


@router.get("/data.json")
async def fleet_json() -> Response:
    """Machine-readable snapshot for external dashboards / curl."""
    entries, summary = await run_in_threadpool(collect_fleet)
    body = json.dumps(
        {
            "summary": summary.to_dict(),
            "environments": [e.to_dict() for e in entries],
        },
        ensure_ascii=False,
        indent=2,
    )
    return Response(content=body, media_type="application/json")
