"""Config routes — view and edit ~/.atlas/config.json fields."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from platform_atlas.core._version import __version__ as ATLAS_VERSION
from platform_atlas.core.context import ctx
from platform_atlas.core.handlers.config import (
    DoctorRow,
    collect_doctor_rows,
    probe_gateway4_url,
    probe_platform_url,
)

from platform_atlas_webui.dependencies import get_templates, template_context
from platform_atlas_webui.services import config as config_svc

router = APIRouter(prefix="/config", tags=["config"])
_templates = get_templates()


@router.get("", response_class=HTMLResponse)
async def view_config(request: Request) -> HTMLResponse:
    cfg = config_svc.read_config()
    return _templates.TemplateResponse(
        request,
        "config/view.html",
        template_context(
            request,
            atlas_version=ATLAS_VERSION,
            cfg=cfg,
        ),
    )


def _row_to_dict(row: "DoctorRow | tuple[str, str, str, str]") -> dict[str, str]:
    """DoctorRow or legacy tuple → dict shape expected by the Jinja partial."""
    if isinstance(row, DoctorRow):
        return {"label": row.label, "status": row.status, "detail": row.detail, "suggestion": row.suggest}
    label, status, detail, suggestion = row
    return {"label": label, "status": status, "detail": detail, "suggestion": suggestion}


@router.get("/doctor", response_class=HTMLResponse)
async def view_doctor(request: Request) -> HTMLResponse:
    """Render the config-doctor health check page.

    Reuses ``collect_doctor_rows()`` from the CLI handler so the WebUI
    and ``platform-atlas config doctor`` always report the same set of
    checks. The slow URL reachability probes are skipped here and
    htmx-streamed in via ``/config/doctor/probe/{kind}`` so the page
    paints in ~10 ms instead of blocking on TCP timeouts.
    """
    rows, env_name, tier = await asyncio.to_thread(
        collect_doctor_rows, skip_url_probes=True,
    )

    counts = {"ok": 0, "warn": 0, "fail": 0}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1

    # Decide which URL probes are even applicable for the current config.
    # An unset Gateway4 URI is intentional (it's optional), so we don't add
    # a placeholder for it — matches CLI behaviour (no row when unset).
    pending_probes: list[dict[str, str]] = []
    try:
        cfg = ctx().config
        pending_probes.append({"kind": "platform", "label": "Platform URL"})
        if cfg.gateway4_uri:
            pending_probes.append({"kind": "gateway4", "label": "Gateway4 URL"})
    except Exception:
        # Context not initialized (no config) — leave probes empty.
        pass

    if counts["fail"]:
        overall = "fail"
    elif counts["warn"]:
        overall = "warn"
    else:
        overall = "ok"

    return _templates.TemplateResponse(
        request,
        "config/doctor.html",
        template_context(
            request,
            atlas_version=ATLAS_VERSION,
            rows=[_row_to_dict(r) for r in rows],
            counts=counts,
            overall=overall,
            doctor_env_name=env_name,
            doctor_tier=tier,
            pending_probes=pending_probes,
        ),
    )


@router.get("/doctor/probe/{kind}", response_class=HTMLResponse)
async def view_doctor_probe(request: Request, kind: str) -> HTMLResponse:
    """Run a single slow URL reachability probe and return one row partial.

    Called by htmx via ``hx-trigger="load"`` on the placeholder row that
    ``/config/doctor`` rendered. The response also OOB-swaps the pending
    counter in the page header so the user sees probes drain.
    """
    if kind not in ("platform", "gateway4"):
        raise HTTPException(status_code=404, detail="unknown probe kind")

    try:
        cfg = ctx().config
    except Exception:
        # Context not initialized — return a warn row so the placeholder swaps
        # to something the user can act on instead of staying spinning forever.
        row = (
            f"{'Platform' if kind == 'platform' else 'Gateway4'} URL",
            "warn",
            "Atlas context not initialized — reload /config/doctor.",
            "",
        )
        return _templates.TemplateResponse(
            request,
            "config/_doctor_row.html",
            template_context(request, row=_row_to_dict(row)),
        )

    probe_fn = probe_platform_url if kind == "platform" else probe_gateway4_url
    result = await asyncio.to_thread(probe_fn, cfg)
    if result is None:
        # Gateway4 not configured — render a "skipped" row so the placeholder
        # resolves to something meaningful rather than vanishing.
        result = ("Gateway4 URL", "ok", "not configured (optional)", "")

    return _templates.TemplateResponse(
        request,
        "config/_doctor_row.html",
        template_context(request, row=_row_to_dict(result), oob_decrement=True),
    )


@router.post("")
async def save_config(
    request: Request,
    organization_name: str = Form(""),
    credential_backend: str = Form("keyring"),
    verify_ssl: str = Form(""),
    dark_mode: str = Form(""),
    theme: str = Form(""),
    extended_validation_checks: str = Form(""),
    debug: str = Form(""),
    tier: str = Form(""),
    manual_input_mode: str = Form(""),
    webui_palette_enabled: str = Form(""),
):
    # Connection-shaped fields (platform_uri, platform_client_id, gateway4_*)
    # are environment-scoped and intentionally not exposed on this page —
    # users edit them on /environments/<name>. Anything received here is
    # ignored to prevent the form from clobbering env-overlay values.
    config_svc.update_config({
        "organization_name": organization_name,
        "credential_backend": credential_backend,
        "verify_ssl": verify_ssl,
        "dark_mode": dark_mode,
        "theme": theme,
        "extended_validation_checks": extended_validation_checks,
        "debug": debug,
        "tier": tier,
        "manual_input_mode": manual_input_mode,
        "webui_palette_enabled": webui_palette_enabled,
    })
    # Env-overlay tier wins over root in load_config(), so writing tier here
    # without mirroring would let an active overlay silently undo the change.
    if tier in ("standard", "extended"):
        config_svc.mirror_tier_to_active_overlay(tier)
    return RedirectResponse(url="/config", status_code=303)
