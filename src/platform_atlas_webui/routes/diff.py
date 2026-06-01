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
from platform_atlas_webui.services import sessions as session_svc

_TMPDIR = Path(tempfile.gettempdir())

router = APIRouter(prefix="/diff", tags=["diff"])
_templates = get_templates()


@router.get("", response_class=HTMLResponse)
async def diff_landing(request: Request) -> HTMLResponse:
    # ``list_sessions`` opens N session.json files — do it off the event loop.
    all_sessions = await run_in_threadpool(session_svc.list_sessions)
    sessions = [s for s in all_sessions if s["validation_completed"]]
    return _templates.TemplateResponse(
        request,
        "diff/landing.html",
        template_context(
            request,
            atlas_version=ATLAS_VERSION,
            sessions=sessions,
        ),
    )


def _build_diff_html(baseline: str, latest: str) -> Path:
    """Render the diff HTML to a temp file and return its path. Sync — runs in threadpool."""
    import pandas as pd
    from platform_atlas.core.session_manager import get_session_manager, rehydrate_validation_attrs
    from platform_atlas.reporting.diff_engine import diff_reports, render_diff_report

    mgr = get_session_manager()
    b_session = mgr.get(baseline)
    l_session = mgr.get(latest)

    if not b_session.validation_file.exists() or not l_session.validation_file.exists():
        raise HTTPException(
            status_code=400,
            detail="Both sessions must have a completed validation step before diffing.",
        )

    b_df = pd.read_parquet(b_session.validation_file)
    l_df = pd.read_parquet(l_session.validation_file)

    # Parquet round-trips lose df.attrs (CLAUDE.md lesson #3). Rehydrate
    # from the capture JSON before calling diff_reports — without this,
    # hostname/ruleset_id/modules_ran/cross-tier banner all silently
    # default to empty/wrong values in the rendered diff.
    rehydrate_validation_attrs(b_df, b_session)
    rehydrate_validation_attrs(l_df, l_session)

    diff_df = diff_reports(b_df, l_df)
    diff_df.attrs["baseline_name"] = baseline
    diff_df.attrs["current_name"] = latest

    with tempfile.NamedTemporaryFile(
        suffix=".html", prefix="atlas-diff-", delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)
    render_diff_report(
        diff_df,
        template_path=PROJECT_TEMPLATES / "diff.html",
        output_path=tmp_path,
        title="Configuration Diff",
        subtitle=f"{baseline} → {latest}",
    )
    return tmp_path


@router.get("/render")
async def render_diff(baseline: str, latest: str):
    """Render diff and stream the HTML straight to the browser."""
    if baseline == latest:
        raise HTTPException(status_code=400, detail="Pick two different sessions.")

    try:
        # Pandas + pyarrow + HTML render is sync — push it off the event loop.
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
