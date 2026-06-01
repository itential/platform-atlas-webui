"""Ruleset routes — list, view, switch active, switch profile."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from platform_atlas.core._version import __version__ as ATLAS_VERSION

from platform_atlas_webui.dependencies import get_atlas_context, get_templates, template_context
from platform_atlas_webui.services import rulesets as ruleset_svc
from platform_atlas_webui.services.environments import active_env_allows_legacy

router = APIRouter(prefix="/rulesets", tags=["rulesets"])
_templates = get_templates()


def _filter_legacy(items: list, allow: bool) -> list:
    if allow:
        return items
    return [item for item in items if not item.get("is_legacy")]


@router.get("", response_class=HTMLResponse)
async def view_rulesets(request: Request) -> HTMLResponse:
    summary = ruleset_svc.get_active_ruleset_summary()
    allow_legacy = active_env_allows_legacy()
    rulesets = _filter_legacy(ruleset_svc.list_rulesets(), allow_legacy)
    profiles = _filter_legacy(ruleset_svc.list_profiles(), allow_legacy)
    return _templates.TemplateResponse(
        request,
        "rulesets/active.html",
        template_context(
            request,
            atlas_version=ATLAS_VERSION,
            ruleset=summary,
            rulesets=rulesets,
            profiles=profiles,
        ),
    )


@router.post("/activate")
async def activate_ruleset(
    ruleset_id: str = Form(...),
    profile_id: str = Form(""),
):
    try:
        ruleset_svc.set_active(ruleset_id, profile_id=profile_id or None)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/rulesets", status_code=303)


@router.post("/suppress")
async def suppress_rule(rule_number: str = Form(...), reason: str = Form(...)):
    """Add rule_number to the active environment's skip_rules list."""
    from datetime import datetime, timezone
    from platform_atlas.core.context import init_context
    from platform_atlas.core.environment import get_environment_manager
    atlas = get_atlas_context()
    env_name = atlas.active_environment
    if not env_name:
        raise HTTPException(status_code=400, detail="No active environment — switch to one first.")
    reason_stripped = reason.strip()
    if len(reason_stripped) < 10:
        raise HTTPException(status_code=400, detail="Reason must be at least 10 characters.")
    try:
        mgr = get_environment_manager()
        env = mgr.load(env_name)
        rule = rule_number.strip().upper()
        current = list(env.skip_rules or [])
        existing = {r["rule_number"] for r in current if isinstance(r, dict)}
        if rule not in existing:
            current.append({
                "rule_number": rule,
                "reason": reason_stripped,
                "suppressed_at": datetime.now(timezone.utc).isoformat(),
            })
            env.skip_rules = current
            mgr.save(env)
            init_context()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/rulesets", status_code=303)


@router.post("/unsuppress")
async def unsuppress_rule(rule_number: str = Form(...)):
    """Remove rule_number from the active environment's skip_rules list."""
    from platform_atlas.core.context import init_context
    from platform_atlas.core.environment import get_environment_manager
    atlas = get_atlas_context()
    env_name = atlas.active_environment
    if not env_name:
        raise HTTPException(status_code=400, detail="No active environment.")
    try:
        mgr = get_environment_manager()
        env = mgr.load(env_name)
        rule = rule_number.strip().upper()
        env.skip_rules = [r for r in (env.skip_rules or []) if r.get("rule_number") != rule] or None
        mgr.save(env)
        init_context()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/rulesets", status_code=303)
