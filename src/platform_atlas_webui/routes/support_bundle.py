"""Support Bundle route — form intake, job kick-off, and ZIP download."""

from __future__ import annotations

import json as _json
import logging as _logging
import os as _os
import tempfile as _tempfile
from pathlib import Path as _Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.background import BackgroundTask as _BackgroundTask

from platform_atlas.core._version import __version__ as ATLAS_VERSION

from platform_atlas_webui.dependencies import forbid_saas_feature, get_templates, template_context
from platform_atlas_webui.security.paths import safe_under
from platform_atlas_webui.services.jobs import get_registry
from platform_atlas_webui.services import runners

# SaaS audits are single-gateway — the diagnostic support bundle is a
# platform-anchored feature with no role there. Refuse every route under this
# router for SaaS; Standard/Extended are unaffected.
router = APIRouter(
    prefix="/support-bundle",
    tags=["support-bundle"],
    dependencies=[Depends(forbid_saas_feature("Support Bundle"))],
)
_templates = get_templates()
_TMPDIR = _Path(_tempfile.gettempdir())
_log = _logging.getLogger(__name__)


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
    connections: list[dict] = []
    mongo_configured = False
    mongo_available = False
    try:
        from platform_atlas.core.context import ctx
        from platform_atlas.core.handlers.support_bundle import _describe_connections
        atlas = ctx()
        active_env = getattr(atlas, "active_environment", "") or ""
        tier = (getattr(atlas.config, "tier", "") or "standard").lower()
        connections = [
            {"label": label, "value": value}
            for label, value in _describe_connections(atlas.config, tier)
        ]
        mongo_configured = bool(getattr(atlas.config, "mongo_uri", None))
        from platform_atlas.capture.collectors.mongo_export import mongodump_available
        mongo_available = mongodump_available()
    except Exception:  # noqa: BLE001
        pass

    asset_types: list[dict] = []
    try:
        from platform_atlas.artifacts import ASSET_TYPES
        asset_types = [{"key": k, "label": a.label} for k, a in ASSET_TYPES.items()]
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
            connections=connections,
            asset_types=asset_types,
            mongo_configured=mongo_configured,
            mongo_available=mongo_available,
        ),
    )


@router.get("/mongo-collections")
async def list_mongo_collections() -> JSONResponse:
    """JSON: the MongoDB collections available to export (Extended only).

    Returns ``{available, configured, collections}``. ``available`` reflects
    whether ``mongodump`` is on the server PATH; ``configured`` whether this
    environment has a MongoDB connection. Each entry in ``collections`` is
    ``{name, size_bytes, doc_count}`` — ``size_bytes`` is the uncompressed BSON
    data-size estimate, or ``null`` when stats can't be read. An empty
    ``collections`` list means Mongo is unreachable or has none — the picker
    shows that as "no collections".
    """
    from platform_atlas.core.context import ctx
    try:
        from platform_atlas.capture.collectors.mongo_export import (
            list_exportable_collections_with_sizes, mongodump_available,
        )
    except ImportError:  # pymongo (or the export module's deps) not installed
        return JSONResponse({"available": False, "configured": False, "collections": []})
    try:
        configured = bool(getattr(ctx().config, "mongo_uri", None))
    except Exception:  # noqa: BLE001
        configured = False
    available = mongodump_available()
    collections: list[dict] = []
    if configured and available and not ctx().is_standard:
        try:
            collections = await run_in_threadpool(list_exportable_collections_with_sizes)
        except Exception as exc:  # noqa: BLE001
            _log.exception("mongo collection list failed")
            raise HTTPException(status_code=502, detail=f"Could not list collections: {type(exc).__name__}: {exc}")
    return JSONResponse({"available": available, "configured": configured, "collections": collections})


@router.get("/assets/{asset_type}")
async def list_platform_assets(
    asset_type: str, search: str = "", skip: int = 0, limit: int = 25
) -> JSONResponse:
    """JSON: one page of selectable Platform artifacts for the asset picker."""
    from platform_atlas.artifacts import ASSET_TYPES, list_assets, page_to_dict
    if asset_type not in ASSET_TYPES:
        raise HTTPException(status_code=404, detail="Unknown asset type")
    limit = max(1, min(limit, 200))
    skip = max(0, skip)
    try:
        page = await run_in_threadpool(
            list_assets, asset_type, search=search, skip=skip, limit=limit
        )
    except Exception as exc:  # noqa: BLE001
        _log.exception("artifact list failed for %s", asset_type)
        raise HTTPException(status_code=502, detail=f"Could not list {asset_type}: {type(exc).__name__}: {exc}")
    return JSONResponse(page_to_dict(page))


@router.post("/run")
async def run_support_bundle(
    request: Request,
    ticket: str = Form("", max_length=9),
    description: str = Form("", max_length=512),
    log_days: int = Form(7),
    assets: str = Form(""),
    mongo_collections: str = Form("", max_length=4096),
    mongo_db: str = Form("", max_length=128),
):
    """Validate form input, kick off a support bundle job, redirect to progress page."""
    import re
    ticket = ticket.strip().upper()
    description = description.strip()
    if not re.fullmatch(r"ISD-\d{4,5}", ticket):
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="Ticket number must be ISD- followed by 4 or 5 digits.")
    log_days = max(1, min(log_days, 30))

    # Parse the optional Platform-artifact selection (JSON emitted by the picker).
    selection: dict = {}
    if assets:
        try:
            from platform_atlas.artifacts import ASSET_TYPES
            parsed = _json.loads(assets)
            if isinstance(parsed, dict):
                for key, entries in parsed.items():
                    if key not in ASSET_TYPES or not isinstance(entries, list):
                        continue
                    clean = [
                        {"id": str(e["id"]), "name": str(e.get("name") or e["id"])}
                        for e in entries
                        if isinstance(e, dict) and e.get("id")
                    ]
                    if clean:
                        selection[key] = clean
        except Exception:  # noqa: BLE001
            selection = {}
    asset_count = sum(len(v) for v in selection.values())

    # Parse the optional MongoDB collection selection (JSON array from the picker,
    # or a comma-separated fallback). Names are sanitised to a safe character set.
    mongo_list: list[str] = []
    if mongo_collections.strip():
        raw_names: list = []
        try:
            parsed_m = _json.loads(mongo_collections)
            if isinstance(parsed_m, list):
                raw_names = parsed_m
        except Exception:  # noqa: BLE001
            raw_names = mongo_collections.split(",")
        seen: set[str] = set()
        for name in raw_names:
            n = str(name).strip()
            if n and re.fullmatch(r"[A-Za-z0-9_.$-]{1,120}", n) and n not in seen:
                seen.add(n)
                mongo_list.append(n)
    mongo_db = mongo_db.strip()
    if mongo_db and not re.fullmatch(r"[A-Za-z0-9_.$-]{1,128}", mongo_db):
        mongo_db = ""

    # Capture tier before submission so the progress view can display it
    # even while the job is still queued/running.
    active_tier = "standard"
    try:
        from platform_atlas.core.context import ctx
        active_tier = (getattr(ctx().config, "tier", "") or "standard").lower()
    except Exception:  # noqa: BLE001
        pass

    reg = get_registry()
    # Backstop so a hung Platform/SSH call can't wedge the job (and the worker
    # pool) forever; generous enough not to interrupt a legitimate large bundle.
    bundle_timeout_s = 1800
    record = await reg.submit(
        "support bundle",
        runners.run_support_bundle_job,
        timeout=bundle_timeout_s,
        ticket=ticket,
        description=description,
        log_days=log_days,
        selection=selection,
        mongo_collections=mongo_list,
        mongo_db=mongo_db,
        metadata={},
    )
    # Set return_url and display metadata after submit — record.id is stable.
    record.metadata["return_url"] = f"/support-bundle?job={record.id}"
    record.metadata["ticket"] = ticket
    record.metadata["description"] = description
    record.metadata["log_days"] = log_days
    record.metadata["tier"] = active_tier
    record.metadata["asset_count"] = asset_count
    record.metadata["mongo_count"] = len(mongo_list)

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
