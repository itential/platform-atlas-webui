"""Preflight route — kick off connectivity checks as a streamed job."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import escape

from platform_atlas.core._version import __version__ as ATLAS_VERSION

from platform_atlas_webui.dependencies import get_templates, template_context
from platform_atlas_webui.services.jobs import get_registry
from platform_atlas_webui.services import runners

router = APIRouter(prefix="/preflight", tags=["preflight"])
_templates = get_templates()


@router.get("", response_class=HTMLResponse)
async def preflight_landing(request: Request, job: str = "") -> HTMLResponse:
    targets: list[dict] = []
    scope = ""
    active_env = ""
    tier = ""
    try:
        from platform_atlas.core.context import ctx
        atlas = ctx()
        active_env = getattr(atlas, "active_environment", "") or ""
        tier = (getattr(atlas.config, "tier", "") or "").lower()
        scope = getattr(atlas.config, "capture_scope", "") or ""
        for t in atlas.config.targets:
            if not isinstance(t, dict):
                continue
            targets.append({
                "name": t.get("name") or t.get("role") or "node",
                "role": (t.get("role") or "").lower(),
                "host": t.get("host") or "",
                "transport": (t.get("transport") or "ssh").lower(),
                "primary": bool(t.get("primary")),
                "modules": list(t.get("modules") or []),
                "ssh_user": t.get("ssh_user") or "",
                "ssh_port": t.get("ssh_port") or 22,
            })
    except Exception:
        targets = []

    # If a job id is supplied, surface the live preflight panel inline so the
    # user sees marching-ants + check rows on this page; the verbose stream
    # stays one click away on the full job output page.
    job_record = None
    started_at_epoch = 0.0
    if job:
        rec = get_registry().get(job)
        if rec is not None:
            job_record = {
                "id": rec.id,
                "name": rec.name,
                "status": rec.status.value,
                "started_at_epoch": rec.started_at or 0.0,
            }
            started_at_epoch = rec.started_at or 0.0

    return _templates.TemplateResponse(
        request,
        "preflight/landing.html",
        template_context(
            request,
            atlas_version=ATLAS_VERSION,
            targets=targets,
            scope=scope,
            active_env=active_env,
            tier=tier,
            job=job_record,
            job_started_at_epoch=started_at_epoch,
        ),
    )


@router.post("")
async def kick_preflight(request: Request):
    reg = get_registry()
    record = await reg.submit(
        "preflight",
        runners.run_preflight_job,
        scope_summary="",
        metadata={"return_url": "/preflight"},
    )
    # Stay on /preflight so the inline panel renders and the marching-ants
    # animation is visible; the user can click into /jobs/<id> if they want
    # the verbose event stream.
    return RedirectResponse(url=f"/preflight?job={record.id}", status_code=303)


# ──────────────────────────────────────────────────────────────────────────────
# Live single-target ping. Used by the "Test" button on each target card —
# returns a small HTML fragment (status badge) that HTMX swaps directly into
# the card's .pf-status-badge slot. No job registry, no SSE; just a synchronous
# connectivity probe wrapped in a thread so the event loop stays free.
# ──────────────────────────────────────────────────────────────────────────────

def _badge_html(kind: str, label: str, hint: str = "") -> str:
    """Render the chip that gets swapped into .pf-status-badge.

    ``kind`` is one of ``ok | fail | skip | warn``; the chip class controls
    color via existing atlas.css tokens. The optional ``hint`` shows on hover
    so a 1-line failure reason stays available without expanding the card.
    """
    chip_cls = {
        "ok": "chip ok",
        "fail": "chip bad",
        "skip": "chip",
        "warn": "chip warn",
    }.get(kind, "chip")
    title_attr = f' title="{escape(hint)}"' if hint else ""
    return (
        f'<span class="pf-status-badge {chip_cls} text-[10px] px-1.5 py-px"{title_attr}>'
        f'<span class="dot"></span>{escape(label)}'
        f'</span>'
    )


def _resolve_target(name: str) -> dict | None:
    """Find the active-environment target dict whose ``name`` matches."""
    from platform_atlas.core.context import ctx
    try:
        atlas = ctx()
    except Exception:
        return None
    for t in atlas.config.targets:
        if isinstance(t, dict) and t.get("name") == name:
            return dict(t)
    return None


def _run_target_check(target: dict) -> tuple[str, str, str]:
    """Synchronous probe — returns (kind, label, hint).

    Dispatches by transport: SSH-style transports get a paramiko handshake;
    api targets call the matching protocol collector's ``preflight()`` static
    method (Platform OAuth, Gateway4 API). Other transports report skip — the
    Test button is for "is it reachable", not module-by-module validation.
    """
    from platform_atlas.core.preflight import _check_node_ssh, CheckStatus

    transport = (target.get("transport") or "").lower()
    role = (target.get("role") or "").lower()
    modules = [m.lower() for m in (target.get("modules") or [])]

    if transport in ("ssh", "control_master"):
        result = _check_node_ssh(target)
        kind = {"pass": "ok", "fail": "fail", "skip": "skip", "warn": "warn"}.get(
            result.status.value, "skip"
        )
        if result.status == CheckStatus.PASS:
            return ("ok", "Reachable", result.message)
        if result.status == CheckStatus.SKIP:
            return ("skip", "Skipped", result.message)
        return (kind, "Unreachable", f"{result.message} — {result.details}".strip(" —"))

    if transport == "api":
        # Pick the right protocol preflight for this api node. Platform always
        # has an OAuth check; Gateway4 has its own API check. Anything else
        # falls through to skip — Mongo/Redis are connector-only checks that
        # don't bind to a single target dict.
        try:
            if "platform" in modules or role in ("iap", "iap_proxy"):
                from platform_atlas.capture.collectors.platform import PlatformCollector
                result = PlatformCollector.preflight()
            elif "gateway4_api" in modules or role == "iag":
                from platform_atlas.capture.collectors.gateway4 import Gateway4ApiCollector
                result = Gateway4ApiCollector.preflight()
            else:
                return ("skip", "No API check", "")
        except Exception as exc:  # noqa: BLE001
            return ("fail", "Error", f"{type(exc).__name__}: {exc}")

        if result.status == CheckStatus.PASS:
            return ("ok", "Reachable", result.message)
        if result.status == CheckStatus.SKIP:
            return ("skip", "Skipped", result.message)
        if result.status == CheckStatus.WARN:
            return ("warn", "Warning", result.message)
        return ("fail", "Unreachable", f"{result.message} — {result.details}".strip(" —"))

    if transport in ("local", "kubernetes"):
        return ("skip", "No-op", f"{transport} transport — nothing to ping")

    return ("skip", "Unknown", f"transport={transport or 'unset'}")


@router.post("/test/{target_name}", response_class=HTMLResponse)
async def test_target(target_name: str) -> HTMLResponse:
    """Probe a single target and return its updated status badge as HTML.

    Designed to be called from a Tailwind/HTMX form on the preflight page:
    ``hx-post="/preflight/test/<name>"`` ``hx-target=".pf-status-badge"``
    ``hx-swap="outerHTML"``. The middleware enforces CSRF on POST.
    """
    target = _resolve_target(target_name)
    if target is None:
        raise HTTPException(status_code=404, detail=f"Unknown target: {target_name}")

    # Probes block on network IO — push to a thread so we don't pin the loop.
    kind, label, hint = await asyncio.to_thread(_run_target_check, target)
    return HTMLResponse(_badge_html(kind, label, hint))
