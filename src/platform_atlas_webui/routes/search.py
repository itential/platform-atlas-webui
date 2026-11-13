"""Search index for the ⌘K command palette.

Returns a flat list of navigable items (pages, sessions, environments,
rulesets) the palette filters and ranks on the client. Sized for typical
Atlas installs — tens to a few hundred items — so a single fetch on first
palette open is cheaper than the per-keystroke alternative.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api", tags=["search"])


# Static page entries — kept in sync with the sidebar nav in base.html.
# Page links never time out and don't depend on service availability, so
# they're hard-coded rather than introspected.
_PAGES: list[dict[str, str]] = [
    {"name": "Dashboard",          "href": "/"},
    {"name": "Fleet",              "href": "/fleet"},
    {"name": "Sessions",           "href": "/sessions"},
    {"name": "Preflight",          "href": "/preflight"},
    {"name": "Diff",               "href": "/diff"},
    {"name": "Reports",            "href": "/reports"},
    {"name": "Architecture",       "href": "/architecture"},
    {"name": "Environments",       "href": "/environments"},
    {"name": "Rulesets",           "href": "/rulesets"},
    {"name": "Credentials",        "href": "/config/credentials"},
    {"name": "Config Doctor",      "href": "/config/doctor"},
    {"name": "Tier",               "href": "/tier"},
    {"name": "Continuous Audit",   "href": "/continuous"},
    {"name": "Alerts",             "href": "/alerts"},
    {"name": "Notifications",      "href": "/notifications"},
    {"name": "Jobs",               "href": "/jobs"},
    {"name": "Health",             "href": "/system"},
    {"name": "Support Bundle",     "href": "/support-bundle"},
    {"name": "Settings",           "href": "/config"},
]


# Pages the sidebar hides and the routes 403 under the SaaS tier
# (see ``forbid_saas_feature``), so the palette must not offer them either.
_SAAS_HIDDEN_PAGES = {"/continuous", "/alerts", "/notifications", "/support-bundle"}


def _build_index() -> list[dict[str, Any]]:
    """Synchronous index build — pushed to threadpool by the route handler."""
    items: list[dict[str, Any]] = []

    try:
        from platform_atlas_webui.services.config import resolve_active_tier
        is_saas = resolve_active_tier() == "saas"
    except Exception:  # noqa: BLE001 — an unreadable tier just means no filtering
        is_saas = False

    for p in _PAGES:
        if is_saas and p["href"] in _SAAS_HIDDEN_PAGES:
            continue
        items.append({
            "kind": "page",
            "label": p["name"],
            "sublabel": "page",
            "href": p["href"],
        })

    # Sessions — name first, env/ruleset as the contextual sublabel.
    try:
        from platform_atlas_webui.services import sessions as _sess
        for s in _sess.list_sessions():
            env = s.get("environment") or ""
            rs = s.get("ruleset_id") or ""
            sub_parts = [p for p in (env, rs) if p]
            items.append({
                "kind": "session",
                "label": s["name"],
                "sublabel": " · ".join(sub_parts) if sub_parts else "session",
                "href": f"/sessions/{s['name']}",
            })
    except Exception:  # noqa: BLE001
        # Don't fail the whole palette because one collector is sad —
        # the palette degrades gracefully to whatever did succeed.
        pass

    try:
        from platform_atlas_webui.services import environments as _env
        for e in _env.list_environments():
            name = e.get("name") or ""
            if not name:
                continue
            items.append({
                "kind": "environment",
                "label": name,
                "sublabel": "environment" + (" · active" if e.get("is_active") else ""),
                "href": f"/environments/{name}",
            })
    except Exception:  # noqa: BLE001
        pass

    try:
        from platform_atlas_webui.services import rulesets as _rs
        for r in _rs.list_rulesets():
            label = r.get("name") or r.get("id") or ""
            if not label:
                continue
            sub = "ruleset"
            if r.get("id") and r.get("id") != label:
                sub = f"ruleset · {r['id']}"
            items.append({
                "kind": "ruleset",
                "label": label,
                "sublabel": sub,
                "href": "/rulesets",
            })
    except Exception:  # noqa: BLE001
        pass

    return items


@router.get("/search-index")
async def search_index() -> JSONResponse:
    """Return the flat searchable index. Cached per-page-load by the client."""
    items = await run_in_threadpool(_build_index)
    return JSONResponse(
        {"items": items},
        headers={"Cache-Control": "no-store"},
    )
