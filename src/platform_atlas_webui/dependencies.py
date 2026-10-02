"""
FastAPI dependencies — shared accessors used across routes.

The WebUI mirrors the CLI's stance: there's exactly one Atlas context per
process, initialized once on startup. Route handlers reach for it through
these dependencies rather than importing core globals directly, so tests
can override them via FastAPI's dependency_overrides.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates

from platform_atlas.core._version import __version__ as _ATLAS_VERSION
from platform_atlas_webui import __version__ as _WEBUI_VERSION
from platform_atlas.core.context import ctx as _atlas_ctx
from platform_atlas.core.session_manager import get_session_manager
from platform_atlas_webui.security.csrf import generate_csrf_token
from platform_atlas_webui.security.tokens import COOKIE_NAME

# Cache-buster appended to every static asset URL. The version alone doesn't
# bump between in-cycle JS/CSS edits, so we add the daemon start time —
# every restart invalidates browser caches without needing a release bump.
_PROCESS_START = int(time.time())
_ASSET_VERSION = f"{_ATLAS_VERSION}.{_PROCESS_START}"


# ── Templating ────────────────────────────────────────────────────────────────

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def get_templates() -> Jinja2Templates:
    """Return a Jinja2Templates instance bound to the WebUI's template dir."""
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    # Brand colors are baked in here so individual templates can avoid
    # repeating the hex codes — and so a future theme system can swap them
    # in one place.
    templates.env.globals["BRAND"] = {
        "navy": "#101625",
        "blue": "#1B93D2",
        "orange": "#FF6633",
        "green": "#99CA3C",
        "pink": "#C5258F",
    }
    return templates


# ── Atlas context accessors ───────────────────────────────────────────────────

def get_atlas_context() -> Any:
    """Return the active AtlasContext singleton, raising 503 if uninitialized."""
    try:
        return _atlas_ctx()
    except Exception as exc:  # noqa: BLE001
        # Atlas was never bootstrapped (no config.json, no active env, etc.).
        # ``_atlas_ctx`` raises a ``ContextNotInitializedError`` which extends
        # ``AtlasError``/``Exception``, *not* ``RuntimeError`` — catch broadly
        # so this can't leak as a 500.
        raise HTTPException(status_code=503, detail=f"Atlas not initialized: {exc}") from exc


def get_active_tier() -> str:
    """Return the active tier ('standard', 'extended', or 'saas')."""
    return get_atlas_context().tier


def forbid_saas_feature(feature: str = "This feature"):
    """Build a FastAPI dependency that 403s when the active tier is SaaS.

    Used to gate platform-anchored features (Support Bundle, Continuous Audit)
    that have no role in a single-gateway SaaS audit. Standard and Extended
    pass through untouched. Tier is resolved disk-fresh — the same way
    ``template_context`` computes ``is_saas`` — so a hidden nav link and a
    blocked route can never disagree.
    """
    def _dep() -> None:
        from platform_atlas_webui.services.config import resolve_active_tier
        if resolve_active_tier() == "saas":
            raise HTTPException(
                status_code=403,
                detail=f"{feature} is not available in SaaS mode.",
            )
    return _dep


def get_session_mgr():
    """Return the session manager singleton."""
    return get_session_manager()


# ── Request helpers ───────────────────────────────────────────────────────────

def template_context(request: Request, **extra) -> dict[str, Any]:
    """Build a base template context with the request, tier, and appearance prefs.

    Appearance prefs and tier are read fresh from ``~/.atlas/config.json``
    on every render — the in-process AtlasContext is initialized once and
    would serve stale values right after PATCH /api/settings/appearance or
    POST /tier/set until restart. Disk read here is cheap and keeps the
    header badge and theming reactive.
    """
    organization_name = ""
    try:
        atlas = _atlas_ctx()
        tier = atlas.tier
        is_standard = atlas.is_standard
        active_env = atlas.active_environment
        try:
            organization_name = getattr(atlas.config, "organization_name", "") or ""
        except Exception:
            pass
    except Exception:  # noqa: BLE001
        # _atlas_ctx() raises ContextNotInitializedError (an AtlasError, NOT a
        # RuntimeError). Treat any failure as "no context yet" so pages can
        # render before the user activates an env.
        tier = "extended"
        is_standard = False
        active_env = None

    # Prefs come from disk so PATCH /api/settings/appearance is reflected
    # on the very next render without a context refresh.
    theme, mode = "modern", "dark"
    scale = 1.0
    upgrade_panel_dismissed = False
    # Cmd+K palette defaults ON — fresh installs and any config that
    # hasn't toggled it yet get the feature. Only an explicit False in
    # config.json disables it.
    palette_enabled = True
    try:
        from platform_atlas_webui.services import config as _cfg_svc
        cfg = _cfg_svc.read_config()
        theme, mode = _cfg_svc.resolve_appearance(cfg)
        scale = _cfg_svc.resolve_scale(cfg)
        upgrade_panel_dismissed = bool(cfg.get("webui_upgrade_panel_dismissed"))
        palette_enabled = bool(cfg.get("webui_palette_enabled", True))
        if not organization_name and cfg.get("organization_name"):
            organization_name = cfg["organization_name"]

        # Resolution mirrors load_config(): ATLAS_TIER > env overlay > root config.
        disk_tier = cfg.get("tier")
        env_name_disk = cfg.get("active_environment") or active_env
        if env_name_disk:
            try:
                from platform_atlas.core.paths import ATLAS_ENVIRONMENTS_DIR
                env_file = ATLAS_ENVIRONMENTS_DIR / f"{env_name_disk}.json"
                if env_file.is_file():
                    env_data = json.loads(env_file.read_text(encoding="utf-8"))
                    if env_data.get("tier"):
                        disk_tier = env_data["tier"]
                    active_env = env_name_disk
                else:
                    # The active env was deleted under us. Pretend nothing is
                    # active so the topbar pill flips to the amber "no active
                    # environment" state and the user gets nudged to /environments.
                    # The atlas singleton may still cache the stale name; the
                    # next ``ensure_valid_environment`` (CLI) or process restart
                    # (WebUI) will clear it on disk.
                    active_env = None
            except Exception:
                pass
        env_var_tier = os.environ.get("ATLAS_TIER")
        if env_var_tier and env_var_tier.strip().lower() in ("standard", "extended", "saas"):
            disk_tier = env_var_tier.strip().lower()
        if disk_tier in ("standard", "extended", "saas"):
            tier = disk_tier
            is_standard = (tier == "standard")
    except Exception:
        pass

    cookie_value = request.cookies.get(COOKIE_NAME)
    # CSP nonce — every inline <script> tag in the templates must include
    # ``nonce="{{ csp_nonce }}"`` or it will be blocked by the response CSP.
    csp_nonce = getattr(request.state, "csp_nonce", "") or ""

    # Continuous-audit topbar summary — small dict consumed by base.html for
    # the always-on pill + bell. Cheap (two small JSON reads) and tolerant of
    # missing files via the service.
    continuous_summary: dict = {"enabled": False, "state": "OFF", "last_age": "", "unacked": 0}
    try:
        from platform_atlas_webui.services.continuous import topbar_summary
        continuous_summary = topbar_summary(active_env)
    except Exception:
        pass

    # Lightweight env list for the topbar inline switcher dropdown. Each entry
    # carries just `name` and `is_active` — enough to render the dropdown
    # without forcing every route to load the full environment view.
    env_dropdown: list[dict] = []
    try:
        from platform_atlas_webui.services import environments as _env_svc
        for e in _env_svc.list_environments():
            env_dropdown.append({"name": e.get("name"), "is_active": bool(e.get("is_active"))})
    except Exception:
        pass

    base = {
        "request": request,
        "tier": tier,
        "is_standard": is_standard,
        "is_saas": tier == "saas",
        "active_environment": active_env,
        "organization_name": organization_name,
        # ``mode`` is the new light/dark axis; ``theme`` is the palette identity
        # (aurora|horizon). Templates read both via ``prefs``.
        "prefs": {"theme": theme, "mode": mode, "palette_enabled": palette_enabled, "scale": scale},
        "upgrade_panel_dismissed": upgrade_panel_dismissed,
        "atlas_version": _ATLAS_VERSION,
        "webui_version": _WEBUI_VERSION,
        "asset_version": _ASSET_VERSION,
        "csrf_token": generate_csrf_token(cookie_value),
        "csp_nonce": csp_nonce,
        "continuous": continuous_summary,
        "env_dropdown": env_dropdown,
    }
    base.update(extra)
    return base


# ── Toast helper ──────────────────────────────────────────────────────────
# Attach an `atlas:toast` HX-Trigger header so the WebUI's toaster component
# (registered in base.html) renders a notification when the response swaps
# in via HTMX. Use for in-page actions where a full-redirect flash banner
# would feel heavy:
#
#     resp = RedirectResponse("/sessions", 303)
#     return attach_toast(resp, kind="success", msg="Session pinned")
#
# For full-redirect flows the existing `flash=` template context still works
# unchanged — base.html now renders flash as a toast on page load.

def attach_toast(response: Any, *, kind: str = "info", msg: str = "") -> Any:
    """Attach an ``atlas:toast`` HX-Trigger to ``response`` and return it.

    ``response`` is any Starlette/FastAPI response (Response, RedirectResponse,
    HTMLResponse). When the WebUI's HTMX boost picks up the response, htmx
    parses the HX-Trigger header and dispatches the named CustomEvent on
    body — the toaster's ``.window`` listener catches it. Merges with any
    pre-existing HX-Trigger value so callers can chain triggers.
    """
    if not msg:
        return response
    trigger = {"atlas:toast": {"kind": kind, "msg": msg}}
    existing = response.headers.get("HX-Trigger") if hasattr(response, "headers") else None
    if existing:
        try:
            existing_obj = json.loads(existing)
            if isinstance(existing_obj, dict):
                existing_obj.update(trigger)
                response.headers["HX-Trigger"] = json.dumps(existing_obj)
                return response
        except json.JSONDecodeError:
            pass  # fall through and overwrite — better than silently dropping the toast
    response.headers["HX-Trigger"] = json.dumps(trigger)
    return response
