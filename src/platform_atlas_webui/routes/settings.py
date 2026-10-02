"""Settings routes — appearance prefs (theme palette + light/dark mode).

Maps onto Aurora & Horizon: ``data-theme`` (palette) and ``data-mode``
(light/dark) attributes on the document root, persisted to
``~/.atlas/config.json`` under ``webui_theme`` and ``webui_mode``.

Also handles small ambient prefs like the "Upgrade to Extended" sidebar
panel dismiss state.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, HTTPException, Response

from platform_atlas_webui.services import config as config_svc

router = APIRouter(prefix="/api/settings", tags=["settings"])

# 3.0 curated theme set — Modern (default), Itential, Obsidian, Carbon.
_VALID_THEMES = {"modern", "itential", "obsidian", "carbon"}
# "auto" follows the OS prefers-color-scheme — the client resolves it to
# light/dark at runtime; we only persist the intent.
_VALID_MODES = {"light", "dark", "auto"}

# Display-scale (WebUI zoom) bounds — mirror services/config.py. The Settings
# slider snaps within this range; we clamp here as a server-side guard.
_UI_SCALE_MIN = 0.9
_UI_SCALE_MAX = 1.3


@router.api_route("/appearance", methods=["PATCH", "POST"])
async def patch_appearance(
    theme: str | None = Form(None),
    mode: str | None = Form(None),
    ui_scale: str | None = Form(None),
) -> Response:
    """Update appearance prefs. Accepts a partial body — either field is optional.

    Returns 204 on success, 400 on validation failure. Client is expected
    to flip ``data-theme`` / ``data-mode`` on ``<html>`` optimistically
    and use this endpoint only for durable storage.
    """
    updates: dict[str, Any] = {}

    if theme is not None:
        if theme not in _VALID_THEMES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid theme '{theme}'. Allowed: {sorted(_VALID_THEMES)}",
            )
        updates["webui_theme"] = theme

    if mode is not None:
        if mode not in _VALID_MODES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid mode '{mode}'. Allowed: {sorted(_VALID_MODES)}",
            )
        updates["webui_mode"] = mode

    if ui_scale is not None:
        try:
            scale_val = float(ui_scale)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid ui_scale '{ui_scale}' — must be a number.",
            )
        updates["webui_ui_scale"] = round(max(_UI_SCALE_MIN, min(_UI_SCALE_MAX, scale_val)), 3)

    if not updates:
        raise HTTPException(
            status_code=400,
            detail="At least one of 'theme', 'mode', or 'ui_scale' must be provided.",
        )

    config_svc.update_config(updates)
    return Response(status_code=204)


@router.api_route("/upgrade-panel", methods=["PATCH", "POST"])
async def patch_upgrade_panel(dismissed: str = Form(...)) -> Response:
    """Persist the dismissed state of the sidebar 'Upgrade to Extended' panel."""
    truthy = {"1", "true", "yes", "on"}
    config_svc.update_config({
        "webui_upgrade_panel_dismissed": dismissed.strip().lower() in truthy,
    })
    return Response(status_code=204)
