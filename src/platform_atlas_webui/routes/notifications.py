"""
Notification channels routes — list, add, remove, and test Slack and
generic-webhook channels per environment for continuous-audit drift.

Channels persist in the per-environment overlay JSON under
``notification_channels``; see ``platform_atlas.continuous.notifications``.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from platform_atlas.core._version import __version__ as ATLAS_VERSION
from platform_atlas.continuous import notifications

from platform_atlas_webui.dependencies import get_atlas_context, get_templates, template_context
from platform_atlas_webui.services import environments as env_svc

router = APIRouter(prefix="/notifications", tags=["notifications"])
_templates = get_templates()


def _active_env_or_400() -> str:
    try:
        atlas = get_atlas_context()
        env = atlas.active_environment or ""
    except HTTPException:
        env = ""
    if not env:
        raise HTTPException(status_code=400, detail="No active environment.")
    return env


def _parse_headers(raw: str) -> dict[str, str]:
    """Parse a multi-line ``Key: Value`` header textarea into a dict."""
    out: dict[str, str] = {}
    if not raw:
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key:
            out[key] = value
    return out


@router.get("", response_class=HTMLResponse)
async def landing(request: Request, sent: str = "", failed: str = "") -> HTMLResponse:
    try:
        atlas = get_atlas_context()
        active_env = atlas.active_environment or ""
    except HTTPException:
        active_env = ""

    channels: list[notifications.NotificationChannel] = []
    if active_env:
        channels = await run_in_threadpool(notifications.list_channels, active_env)
    environments = await run_in_threadpool(env_svc.list_environments)

    flash = None
    if sent:
        flash = {"kind": "success", "message": f"Test payload delivered to channel {sent}."}
    elif failed:
        flash = {"kind": "error", "message": f"Test send failed for channel {failed}. See server log for details."}

    return _templates.TemplateResponse(
        request,
        "notifications/landing.html",
        template_context(
            request,
            atlas_version=ATLAS_VERSION,
            env=active_env,
            channels=channels,
            environments=environments,
            flash=flash,
            CHANNEL_TYPE_SLACK=notifications.CHANNEL_TYPE_SLACK,
            CHANNEL_TYPE_WEBHOOK=notifications.CHANNEL_TYPE_WEBHOOK,
        ),
    )


@router.post("/add")
async def add_channel(
    channel_type: str = Form(...),
    channel_name: str = Form(""),
    url: str = Form(...),
    headers_raw: str = Form(""),
    secret: str = Form(""),
):
    env = _active_env_or_400()
    channel_type = (channel_type or "").lower().strip()
    if channel_type not in notifications.SUPPORTED_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported channel type: {channel_type!r}")
    url = (url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Channel URL is required.")
    # URL scheme + SSRF blocklist enforcement happens inside notifications.add_channel
    # so the CLI and WebUI share one validator. Errors surface as ValueError below.

    headers: dict[str, str] = {}
    secret_val = ""
    if channel_type == notifications.CHANNEL_TYPE_WEBHOOK:
        headers = _parse_headers(headers_raw)
        secret_val = (secret or "").strip()

    channel_id = notifications.make_channel_id(channel_type[:4])
    name = (channel_name or "").strip() or channel_id

    channel = notifications.NotificationChannel(
        id=channel_id,
        type=channel_type,
        name=name,
        url=url,
        headers=headers,
        secret=secret_val,
        enabled=True,
    )
    try:
        await run_in_threadpool(notifications.add_channel, env, channel)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/notifications", status_code=303)


@router.post("/{channel_id}/remove")
async def remove_channel(channel_id: str):
    env = _active_env_or_400()
    removed = await run_in_threadpool(notifications.remove_channel, env, channel_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Channel not found: {channel_id}")
    return RedirectResponse(url="/notifications", status_code=303)


@router.post("/{channel_id}/test")
async def test_channel(channel_id: str):
    env = _active_env_or_400()
    record = await run_in_threadpool(notifications.test_channel, env, channel_id)
    if record.get("ok"):
        return RedirectResponse(url=f"/notifications?sent={channel_id}", status_code=303)
    return RedirectResponse(url=f"/notifications?failed={channel_id}", status_code=303)
