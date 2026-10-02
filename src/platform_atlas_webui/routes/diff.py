"""Diff route — compare two sessions and render the diff HTML."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from starlette.background import BackgroundTask

from platform_atlas.core._version import __version__ as ATLAS_VERSION
from platform_atlas.core.paths import PROJECT_TEMPLATES

from platform_atlas_webui.dependencies import get_templates, template_context
from platform_atlas_webui.security.paths import safe_under
from platform_atlas_webui.services import baseline as baseline_svc
from platform_atlas_webui.services import config as _cfg_svc
from platform_atlas_webui.services import sessions as session_svc

_TMPDIR = Path(tempfile.gettempdir())

router = APIRouter(prefix="/diff", tags=["diff"])
_templates = get_templates()


@router.get("", response_class=HTMLResponse)
async def diff_landing(request: Request) -> HTMLResponse:
    # ``list_sessions`` opens N session.json files — do it off the event loop.
    all_sessions = await run_in_threadpool(session_svc.list_sessions)
    sessions = [s for s in all_sessions if s["validation_completed"]]

    # The active environment's pinned baseline (if any) drives the "compare
    # against baseline" hero — featuring one baseline at a time reads far
    # cleaner than a cross-environment picker, and it matches the env-scoped
    # context the rest of the WebUI already shows in the topbar.
    active_env = _cfg_svc.read_config().get("active_environment") or ""
    active_baseline = baseline_svc.baseline_summary(active_env) if active_env else None
    active_env_sessions = [s for s in sessions if s["environment"] == active_env]

    return _templates.TemplateResponse(
        request,
        "diff/landing.html",
        template_context(
            request,
            atlas_version=ATLAS_VERSION,
            sessions=sessions,
            active_env=active_env,
            active_baseline=active_baseline,
            active_env_sessions=active_env_sessions,
        ),
    )


def _build_diff_html(baseline: str, latest: str) -> Path:
    """Render the diff HTML to a temp file and return its path. Sync — runs in threadpool."""
    from platform_atlas.core.session_manager import get_session_manager
    from platform_atlas.validation.results import load_validation_results
    from platform_atlas.reporting.diff_engine import diff_reports, render_diff_report

    mgr = get_session_manager()
    b_session = mgr.get(baseline)
    l_session = mgr.get(latest)

    if not b_session.validation_file.exists() or not l_session.validation_file.exists():
        raise HTTPException(
            status_code=400,
            detail="Both sessions must have a completed validation step before diffing.",
        )

    b_results = load_validation_results(b_session.validation_file)
    l_results = load_validation_results(l_session.validation_file)

    diff_result = diff_reports(b_results, l_results)
    diff_result.metadata["baseline_name"] = baseline
    diff_result.metadata["current_name"] = latest

    with tempfile.NamedTemporaryFile(
        suffix=".html", prefix="atlas-diff-", delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)
    render_diff_report(
        diff_result,
        template_path=PROJECT_TEMPLATES / "diff.html",
        output_path=tmp_path,
        title="Configuration Diff",
        subtitle=f"{baseline} → {latest}",
    )
    return tmp_path


def _build_baseline_diff_html(latest: str) -> Path:
    """Render `latest` against its environment's pinned baseline. Sync — runs in threadpool."""
    from platform_atlas.core import baseline_store
    from platform_atlas.core.session_manager import get_session_manager
    from platform_atlas.validation.results import load_validation_results
    from platform_atlas.reporting.diff_engine import diff_reports, render_diff_report

    mgr = get_session_manager()
    l_session = mgr.get(latest)
    env_name = l_session.metadata.environment or ""

    baseline_data = baseline_store.load_raw(env_name)
    if baseline_data is None:
        raise HTTPException(
            status_code=400,
            detail=f"No baseline set for environment '{env_name or '(none)'}'.",
        )
    if not l_session.validation_file.exists():
        raise HTTPException(
            status_code=400,
            detail="Session must have a completed validation step before diffing.",
        )

    b_results = baseline_store.load_results(env_name)
    l_results = load_validation_results(l_session.validation_file)

    baseline_name = baseline_data.get("source_session", "baseline")
    diff_result = diff_reports(b_results, l_results)
    diff_result.metadata["baseline_name"] = baseline_name
    diff_result.metadata["current_name"] = latest

    with tempfile.NamedTemporaryFile(
        suffix=".html", prefix="atlas-diff-", delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)
    render_diff_report(
        diff_result,
        template_path=PROJECT_TEMPLATES / "diff.html",
        output_path=tmp_path,
        title="Configuration Diff",
        subtitle=f"{baseline_name} (baseline) → {latest}",
    )
    return tmp_path


@router.get("/render")
async def render_diff(baseline: str = "", latest: str = "", use_baseline: bool = False):
    """Render diff and stream the HTML straight to the browser."""
    if not latest:
        raise HTTPException(status_code=400, detail="Pick a session to compare.")

    try:
        if use_baseline:
            # JSON load + HTML render is sync — push it off the event loop.
            tmp_path = await run_in_threadpool(_build_baseline_diff_html, latest)
        else:
            if not baseline:
                raise HTTPException(status_code=400, detail="Pick a baseline session.")
            if baseline == latest:
                raise HTTPException(status_code=400, detail="Pick two different sessions.")
            tmp_path = await run_in_threadpool(_build_diff_html, baseline, latest)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    safe_tmp = safe_under(tmp_path, _TMPDIR)
    # Clean up the temp file after the response streams — previously we leaked
    # one signed compliance artifact per diff into /tmp until the OS reaped it.
    return FileResponse(
        str(safe_tmp),
        media_type="text/html",
        background=BackgroundTask(_safe_unlink, safe_tmp),
    )


def _safe_unlink(p: Path) -> None:
    try:
        os.unlink(p)
    except OSError:
        pass
