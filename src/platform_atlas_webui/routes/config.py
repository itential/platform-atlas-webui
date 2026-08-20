"""Config routes — view and edit ~/.atlas/config.json fields."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from platform_atlas.core._version import __version__ as ATLAS_VERSION
from platform_atlas.core.context import ctx
from platform_atlas.core.credentials import CredentialKey, applicable_keys
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
async def view_config(request: Request, saved: int = Query(0)) -> HTMLResponse:
    cfg = config_svc.read_config()
    return _templates.TemplateResponse(
        request,
        "config/view.html",
        template_context(
            request,
            atlas_version=ATLAS_VERSION,
            cfg=cfg,
            active_env=_active_env_summary(),
            # Post-save redirect lands on /config?saved=1 — base.html renders
            # this as a toast so the user gets explicit "it saved" feedback.
            flash={"kind": "success", "message": "Settings saved."} if saved else None,
        ),
    )


def _row_to_dict(row: "DoctorRow | tuple[str, str, str, str]") -> dict[str, str]:
    """DoctorRow or legacy tuple → dict shape expected by the Jinja partial."""
    if isinstance(row, DoctorRow):
        return {"label": row.label, "status": row.status, "detail": row.detail, "suggestion": row.suggest}
    label, status, detail, suggestion = row
    return {"label": label, "status": status, "detail": detail, "suggestion": suggestion}


def _webui_doctor_row(row: "DoctorRow | tuple[str, str, str, str]") -> dict[str, str]:
    """``_row_to_dict`` plus WebUI-only presentation tweaks (the shared CLI
    ``collect_doctor_rows`` is left unchanged):

    1. A deliberately-chosen **encrypted local file** backend is a valid choice,
       not a problem — don't surface the user's own selection as a yellow
       warning. An *unreadable* file still fails, and a genuinely insecure or
       broken OS keyring still warns/fails; only the "your selected backend"
       file row is softened to ``ok``.
    2. WebUI users shouldn't be told to run a CLI command — when a suggestion
       points at ``config credentials``, render it as a link to the in-app
       Credentials page instead (handled in ``_doctor_row.html``).
    """
    d = _row_to_dict(row)
    if (d["label"] == "Credential backend" and d["status"] == "warn"
            and "Encrypted local file" in d["detail"]):
        d["status"] = "ok"
        d["suggestion"] = ""
    if d.get("suggestion") and "config credentials" in d["suggestion"]:
        d["suggestion"] = "Set it on the Credentials page."
        d["cred_link"] = "/config/credentials"
    return d


def _active_env_summary() -> "dict | None":
    """Resolved tier + credential store for the ACTIVE environment — read-only
    context for the Settings page's active-environment panel.

    Each environment owns its own tier, credentials, and credential store, so
    this makes explicit that the values in effect belong to the active
    environment, not the workspace. Returns ``None`` when no env is active.
    """
    import json
    import os
    from platform_atlas.core.paths import ATLAS_CONFIG_FILE, ATLAS_ENVIRONMENTS_DIR

    try:
        raw = json.loads(ATLAS_CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    env_name = raw.get("active_environment") or ""
    if not env_name:
        return None
    tier = raw.get("tier") or "standard"
    backend = raw.get("credential_backend") or "keyring"
    try:
        env_file = ATLAS_ENVIRONMENTS_DIR / f"{env_name}.json"
        if env_file.is_file():
            env_data = json.loads(env_file.read_text(encoding="utf-8"))
            if env_data.get("tier"):
                tier = env_data["tier"]
            if env_data.get("credential_backend"):
                backend = env_data["credential_backend"]
    except Exception:
        pass
    env_tier = os.environ.get("ATLAS_TIER")
    if env_tier and env_tier.strip().lower() in ("standard", "extended", "saas"):
        tier = env_tier.strip().lower()
    labels = {"keyring": "OS keyring", "file": "Encrypted local file", "vault": "HashiCorp Vault"}
    return {
        "name": env_name,
        "tier": tier,
        "backend": backend,
        "backend_label": labels.get(backend, backend),
    }


@router.get("/doctor", response_class=HTMLResponse)
async def view_doctor(request: Request) -> HTMLResponse:
    """Render the config-doctor health check page.

    Reuses ``collect_doctor_rows()`` from the CLI handler so the WebUI
    and ``platform-atlas config doctor`` always report the same set of
    checks. The slow URL reachability probes are skipped here and
    htmx-streamed in via ``/config/doctor/probe/{kind}`` so the page
    paints in ~10 ms instead of blocking on TCP timeouts.
    """
    raw_rows, env_name, tier = await asyncio.to_thread(
        collect_doctor_rows, skip_url_probes=True,
    )

    # Apply the WebUI presentation tweaks BEFORE tallying, so the summary tiles
    # and the overall verdict reflect what's actually shown in the table. A
    # deliberately-chosen encrypted-file backend is softened warn→ok by
    # _webui_doctor_row; counting the raw rows would claim a "warning" that no
    # row in the table actually displays.
    rows = [_webui_doctor_row(r) for r in raw_rows]

    counts = {"ok": 0, "warn": 0, "fail": 0}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1

    # Decide which URL probes are even applicable for the current config.
    # An unset Gateway4 URI is intentional (it's optional), so we don't add
    # a placeholder for it — matches CLI behaviour (no row when unset).
    #
    # The Platform URL probe only applies to platform-anchored tiers
    # (standard/extended). SaaS audits a single gateway with no Platform, so it
    # is skipped entirely — same tier gating as collect_doctor_rows() and the
    # CLI doctor, keeping all three surfaces in lockstep.
    platform_used = CredentialKey.PLATFORM_SECRET in applicable_keys(tier)
    pending_probes: list[dict[str, str]] = []
    try:
        cfg = ctx().config
        if platform_used:
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
            rows=rows,
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
            template_context(request, row=_webui_doctor_row(row)),
        )

    # Defense-in-depth: the Platform probe placeholder isn't rendered for tiers
    # that don't use Platform (SaaS), but guard the endpoint too so a direct hit
    # can't resurrect the false "no platform_uri" row.
    if kind == "platform" and CredentialKey.PLATFORM_SECRET not in applicable_keys(cfg.tier):
        raise HTTPException(status_code=404, detail="Platform is not used in this tier")

    probe_fn = probe_platform_url if kind == "platform" else probe_gateway4_url
    result = await asyncio.to_thread(probe_fn, cfg)
    if result is None:
        # Gateway4 not configured — render a "skipped" row so the placeholder
        # resolves to something meaningful rather than vanishing.
        result = ("Gateway4 URL", "ok", "not configured (optional)", "")

    return _templates.TemplateResponse(
        request,
        "config/_doctor_row.html",
        template_context(request, row=_webui_doctor_row(result), oob_decrement=True),
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
    network_policy: str = Form(""),
):
    # Connection-shaped fields (platform_uri, platform_client_id, gateway4_*)
    # are environment-scoped and intentionally not exposed on this page —
    # users edit them on /environments/<name>. Anything received here is
    # ignored to prevent the form from clobbering env-overlay values.
    updates = {
        "organization_name": organization_name,
        "credential_backend": credential_backend,
        "verify_ssl": verify_ssl,
        "dark_mode": dark_mode,
        "theme": theme,
        "extended_validation_checks": extended_validation_checks,
        "debug": debug,
        "manual_input_mode": manual_input_mode,
        "webui_palette_enabled": webui_palette_enabled,
    }
    # Only a known tier value is written — a missing/garbled field must not
    # rewrite the global default (the select includes SaaS now, but a stale
    # or crafted form could still post anything).
    posted_tier = (tier or "").strip().lower()
    if posted_tier in ("standard", "extended", "saas"):
        updates["tier"] = posted_tier
    # Only a known network_policy value is written — reject blanks and typos.
    posted_policy = (network_policy or "").strip().lower()
    if posted_policy in ("allow", "disallow"):
        updates["network_policy"] = posted_policy
    config_svc.update_config(updates)
    # Env-overlay tier wins over root in load_config(), so writing tier here
    # without mirroring would let an active overlay silently undo the change.
    # SaaS is the exception: as a default it applies to FUTURE environments
    # only — the active env keeps its own tier (and the helper refuses to
    # rewrite a SaaS env anyway).
    if posted_tier in ("standard", "extended"):
        config_svc.mirror_tier_to_active_overlay(posted_tier)
    # Reload the in-memory context (same as /tier/set) so edited fields —
    # debug logging in particular — take effect for the next capture or
    # validation job without a server restart. Best-effort: the disk write
    # already succeeded.
    try:
        from platform_atlas.core.context import init_context
        init_context()
    except Exception:
        pass
    return RedirectResponse(url="/config?saved=1", status_code=303)


@router.get("/avc-modules", response_class=HTMLResponse)
async def view_avc_modules(request: Request, saved: int = Query(0)) -> HTMLResponse:
    """Additional Validation Modules — per-check enable/disable.

    Reads check metadata straight from the CLI's ``ExtendedValidationRegistry``
    (imported as a library — no duplicated check list to keep in sync) and the
    disabled set from the same ``config.json`` key the CLI's `config edit` >
    Advanced > Additional Validation Modules menu writes.
    """
    from platform_atlas.core.config import FACTORY_DISABLED_EXTENDED_CHECKS
    from platform_atlas.validation.extended_validation import get_registry

    disabled = set(config_svc.get_disabled_extended_checks())
    categories: dict[str, list[dict[str, Any]]] = {}
    for check_id, name, category in get_registry().list_checks():
        label = category.name.replace("_", " ").title()
        categories.setdefault(label, []).append({
            "check_id": check_id,
            "name": name,
            "enabled": check_id not in disabled,
            # Privacy-sensitive modules (currently just RBAC) stay opt-in
            # even at "factory default" — annotated so the template can
            # explain why this one starts unchecked unlike the rest.
            "factory_disabled": check_id in FACTORY_DISABLED_EXTENDED_CHECKS,
        })

    return _templates.TemplateResponse(
        request,
        "config/avc_modules.html",
        template_context(
            request,
            atlas_version=ATLAS_VERSION,
            categories=categories,
            disabled_count=len(disabled),
            total_count=sum(len(v) for v in categories.values()),
            is_default=(disabled == set(FACTORY_DISABLED_EXTENDED_CHECKS)),
            flash={"kind": "success", "message": "Additional Validation Modules saved."} if saved else None,
        ),
    )


@router.post("/avc-modules")
async def save_avc_modules(request: Request) -> RedirectResponse:
    from platform_atlas.validation.extended_validation import get_registry

    # Raw getlist() — a FastAPI List[str] Form param is unreliable for a
    # variable-length checkbox group (see routes/sessions.py's pipeline
    # selector for the same issue and fix).
    form = await request.form()
    selected = set(form.getlist("check_ids"))

    all_ids = [check_id for check_id, _, _ in get_registry().list_checks()]
    disabled = [cid for cid in all_ids if cid not in selected]
    config_svc.set_disabled_extended_checks(disabled)

    # Reload in-process context (same as the main /config save) so the next
    # validation run in this process picks up the change immediately.
    try:
        from platform_atlas.core.context import init_context
        init_context()
    except Exception:
        pass
    return RedirectResponse(url="/config/avc-modules?saved=1", status_code=303)


@router.post("/avc-modules/reset")
async def reset_avc_modules(request: Request) -> RedirectResponse:
    """Explicitly reset every AVC module to its factory default.

    Every module defaults to enabled EXCEPT RBAC authorization, which stays
    opt-in even at "default" (privacy-sensitive) — this must not blanket
    enable everything, or resetting would silently turn RBAC collection on.
    Separate action from the checkbox save above — mirrors the CLI's
    dedicated "Reset all AVC modules to default" menu item.
    """
    from platform_atlas.core.config import FACTORY_DISABLED_EXTENDED_CHECKS
    config_svc.set_disabled_extended_checks(sorted(FACTORY_DISABLED_EXTENDED_CHECKS))
    try:
        from platform_atlas.core.context import init_context
        init_context()
    except Exception:
        pass
    return RedirectResponse(url="/config/avc-modules?saved=1", status_code=303)
