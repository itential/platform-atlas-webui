"""Reports route — list completed compliance reports and stream the HTML files."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from platform_atlas_webui.dependencies import get_templates, template_context
from platform_atlas_webui.security.paths import safe_under
from platform_atlas_webui.services import sessions as session_svc

_SESSIONS_ROOT = Path("~/.atlas/sessions").expanduser()

router = APIRouter(prefix="/reports", tags=["reports"])
_templates = get_templates()


@router.get("", response_class=HTMLResponse)
async def list_reports(request: Request) -> HTMLResponse:
    """Index page — completed reports across all sessions, or an empty state."""
    # ``list_sessions`` is sync I/O — N file reads — so push it off the event
    # loop. ``report_file`` now arrives in the list payload, removing the old
    # per-row ``get_session`` follow-up read (~2N reads → ~N).
    sessions = await run_in_threadpool(session_svc.list_sessions)
    reports = []
    for s in sessions:
        if not s.get("report_completed"):
            continue
        # Confirm the file is actually on disk before linking — covers the
        # "report flag set but file deleted manually" case.
        if not s.get("report_file"):
            continue
        reports.append({
            "name": s["name"],
            "environment": s.get("environment") or "",
            "tier": s.get("tier") or "extended",
            "ruleset_id": s.get("ruleset_id") or "",
            "ruleset_profile": s.get("ruleset_profile") or "",
            "pass_count": s.get("pass_count") or 0,
            "fail_count": s.get("fail_count") or 0,
            "skip_count": s.get("skip_count") or 0,
            "total_rules": s.get("total_rules") or 0,
            "updated_at": s.get("updated_at") or "",
            "is_active": s.get("is_active", False),
        })

    return _templates.TemplateResponse(
        request,
        "reports/list.html",
        template_context(request, reports=reports),
    )


# Anchor-targeted rewrite: only mutate ``href="..."`` attributes pointing at
# the relative cross-link filenames. A blanket ``str.replace`` would corrupt
# any literal occurrence in <pre>/<code> blocks (audited log content can
# legitimately contain "03_report.html" as text).
#
# /reports/{name} is now the new tabbed WebUI view; the standalone HTML moved
# to /reports/{name}/html so cross-links from inside the HTML still land on
# the HTML twin (and not bounce a user into the SPA-ish view they were trying
# to leave).
_HREF_REWRITES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r'href="04_operational\.html"'), 'href="/reports/{name}/operational"'),
    (re.compile(r'href="05_arch\.html"'),        'href="/reports/{name}/arch"'),
    (re.compile(r'href="03_report\.html"'),      'href="/reports/{name}/html"'),
)


def _get_report_html(path: Path, name: str) -> str:
    """Read report HTML and rewrite relative cross-links to WebUI absolute URLs.

    Only ``href="..."`` attributes are touched — string literals matching
    these filenames inside <pre>/<code> blocks are left intact.
    """
    html = path.read_text(encoding="utf-8")
    for pattern, replacement in _HREF_REWRITES:
        html = pattern.sub(replacement.format(name=name), html)
    return html


def _sandbox_headers() -> dict[str, str]:
    """Headers applied to every report response.

    Reports embed captured operator data (hostnames, error strings, MongoDB
    output). To prevent any of that from running in the WebUI's authenticated
    origin, we serve report HTML with a strict, sandboxed CSP and disable
    embedding/sniffing/referrer leaks.
    """
    return {
        "Content-Security-Policy": (
            "default-src 'none'; "
            "img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; "
            "font-src 'self' data:; "
            "frame-ancestors 'self'; "
            "base-uri 'none'; "
            "form-action 'none'"
        ),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "SAMEORIGIN",
    }


@router.get("/{name}/html")
async def view_session_report_html(name: str):
    """Stream the standalone compliance HTML report (03_report.html).

    The standalone HTML lives here so the new ``GET /reports/{name}`` can
    serve the in-WebUI tabbed view. CLI users and exports still consume
    the same self-contained HTML — only the URL changed.
    """
    session = await run_in_threadpool(session_svc.get_session, name)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session '{name}' not found")
    report_path = session.get("report_file")
    if not report_path:
        raise HTTPException(
            status_code=404,
            detail=f"Session '{name}' has no generated report yet — run validate + report first.",
        )
    p = safe_under(Path(report_path).expanduser(), _SESSIONS_ROOT)
    html = await run_in_threadpool(_get_report_html, p, name)
    return Response(content=html, media_type="text/html", headers=_sandbox_headers())


@router.get("/{name}", response_class=HTMLResponse)
async def view_session_report(name: str, request: Request) -> HTMLResponse:
    """Default 'View Report' surface — the WebUI's tabbed Compliance /
    Operational / Architecture experience.

    Renders the Jinja shell extending base.html so the report inherits
    sidebar nav, theme toggle, and ORG/ENV/Tier pills like every other
    WebUI page. The shell ships inert; ``static/js/reports/viewmodel.js``
    fetches ``/reports/{name}/viewmodel`` on load and hydrates each tab
    with anime.js v4 entrance and chart animations.

    The standalone HTML twin remains accessible at ``/reports/{name}/html``.
    """
    session = await run_in_threadpool(session_svc.get_session, name)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session '{name}' not found")
    if not session.get("report_completed"):
        raise HTTPException(
            status_code=404,
            detail=f"Session '{name}' has no generated report yet — run validate + report first.",
        )
    return _templates.TemplateResponse(
        request,
        "reports/view.html",
        template_context(
            request,
            session=session,
            session_name=name,
        ),
    )


@router.get("/{name}/operational")
async def view_session_operational(name: str):
    """Stream the operational HTML report for a given session."""
    session = await run_in_threadpool(session_svc.get_session, name)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session '{name}' not found")
    session_dir = session.get("directory")
    if not session_dir:
        raise HTTPException(status_code=404, detail=f"Session '{name}' directory not found")
    candidate = Path(session_dir) / "04_operational.html"
    if not candidate.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Operational report not generated yet for session '{name}'.",
        )
    p = safe_under(candidate, _SESSIONS_ROOT)
    html = await run_in_threadpool(_get_report_html, p, name)
    return Response(content=html, media_type="text/html", headers=_sandbox_headers())


@router.get("/{name}/arch")
async def view_session_arch(name: str):
    """Stream the architecture HTML report for a given session."""
    session = await run_in_threadpool(session_svc.get_session, name)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session '{name}' not found")
    session_dir = session.get("directory")
    if not session_dir:
        raise HTTPException(status_code=404, detail=f"Session '{name}' directory not found")
    candidate = Path(session_dir) / "05_arch.html"
    if not candidate.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Architecture report not generated yet for session '{name}'.",
        )
    p = safe_under(candidate, _SESSIONS_ROOT)
    html = await run_in_threadpool(_get_report_html, p, name)
    return Response(content=html, media_type="text/html", headers=_sandbox_headers())


@router.get("/{name}/viewmodel")
async def view_session_viewmodel(name: str, refresh: bool = False) -> JSONResponse:
    """Return the WebUI viewmodel JSON for a session.

    The viewmodel is the typed contract powering the WebUI's unified
    Compliance / Operational / Architecture experience. Cached as
    ``06_webui_viewmodel.json`` at report-generation time; rebuilds on
    the fly for sessions that predate the cache or whose
    ``schema_version`` is stale, **and persists the rebuild to disk**
    so subsequent requests don't re-run extended validation (which
    would otherwise hit GitLab on every report view).

    Pass ``?refresh=1`` to force a rebuild — the only path that
    re-runs extended validation by design. Useful when the user wants
    fresh check results without regenerating reports through the CLI.

    Unlike the HTML report endpoints, this returns ``application/json``
    with no sandboxed CSP — the data is consumed by the WebUI's own
    frontend running in the WebUI's authenticated origin.
    """
    from platform_atlas.core.session_manager import get_session_manager
    from platform_atlas.reporting.webui_viewmodel import load_or_build_viewmodel

    mgr = get_session_manager()
    try:
        session = await run_in_threadpool(mgr.get, name)
    except Exception as exc:  # noqa: BLE001 — translate any lookup error to 404
        raise HTTPException(status_code=404, detail=f"Session '{name}' not found") from exc

    if not session.metadata.report_completed:
        raise HTTPException(
            status_code=404,
            detail=f"Reports not generated yet for session '{name}' — run validate + report first.",
        )

    try:
        viewmodel = await run_in_threadpool(
            load_or_build_viewmodel, session, force_rebuild=refresh,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # The viewmodel is persisted to disk at report-generation time and only
    # changes on explicit ?refresh=1. A 2-minute browser cache eliminates
    # redundant 500 KB+ downloads on HTMX back/forward and tab switches.
    # ?refresh=1 gets no-store so the browser always re-fetches that URL
    # (the base URL's cache will be updated on the next normal load).
    cache_header = "no-store" if refresh else "private, max-age=120"
    return JSONResponse(viewmodel, headers={"Cache-Control": cache_header})
