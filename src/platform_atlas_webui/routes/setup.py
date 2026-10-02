"""First-run setup routes — bootstrap Atlas from the browser."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from platform_atlas.core._version import __version__ as ATLAS_VERSION

from platform_atlas_webui.dependencies import get_templates
from platform_atlas_webui.security.csrf import generate_csrf_token
from platform_atlas_webui.security.redact import redact
from platform_atlas_webui.security.tokens import COOKIE_NAME
from platform_atlas_webui.services import setup as setup_svc

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/setup", tags=["setup"])
_templates = get_templates()


def _render_form(
    request: Request,
    *,
    error: str | None = None,
    form: dict | None = None,
    tier: str = "standard",
    organization_name: str = "",
    active_environment: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    # Read prefs from config if it exists (so a half-bootstrapped state
    # still respects the user's chosen theme/mode), else use defaults.
    # Modern is the 3.0 flagship default and is dark-first (see
    # services/config.resolve_appearance), so a fresh onboarding opens on it.
    prefs = {"theme": "modern", "mode": "dark"}
    try:
        from platform_atlas_webui.services import config as _cfg_svc
        cfg = _cfg_svc.read_config()
        prefs["theme"], prefs["mode"] = _cfg_svc.resolve_appearance(cfg)
    except Exception:
        pass

    return _templates.TemplateResponse(
        request,
        "setup/welcome.html",
        {
            "request": request,
            "atlas_version": ATLAS_VERSION,
            "tier": tier,
            "is_standard": tier == "standard",
            "active_environment": active_environment,
            "organization_name": organization_name,
            "error": error,
            "form": form or {},
            "prefs": prefs,
            "csrf_token": generate_csrf_token(request.cookies.get(COOKIE_NAME)),
            # Inline scripts in welcome.html must include nonce="…" or CSP
            # will block them.
            "csp_nonce": getattr(request.state, "csp_nonce", "") or "",
        },
        status_code=status_code,
    )


@router.get("", response_class=HTMLResponse)
async def setup_form(request: Request) -> HTMLResponse:
    if setup_svc.is_initialized():
        # Already configured — bounce back to the dashboard rather than
        # showing the wizard again. The user can still use /config to edit.
        return RedirectResponse(url="/", status_code=303)
    return _render_form(request)


@router.post("/check-vault", response_class=JSONResponse)
async def check_vault(
    url: str = Form(""),
    auth_method: str = Form("token"),
    token: str = Form(""),
    role_id: str = Form(""),
    secret_id: str = Form(""),
    wrapping_token: str = Form(""),
    token_file_path: str = Form(""),
    mount_point: str = Form("secret"),
    secret_path: str = Form("platform-atlas"),
    namespace: str = Form(""),
    verify_ssl: str = Form(""),
    tier: str = Form("standard"),
    env_name: str = Form(""),
    gateway4_uri: str = Form(""),
    iag5_uri: str = Form(""),
) -> JSONResponse:
    """AJAX probe — try to authenticate against Vault and report key presence.

    Returns the structured result from ``setup_svc.verify_vault_connection``
    so the welcome form can render a per-key list with colored badges.
    """
    payload = {
        "url": url,
        "auth_method": auth_method,
        "token": token,
        "role_id": role_id,
        "secret_id": secret_id,
        "wrapping_token": wrapping_token,
        "token_file_path": token_file_path,
        "mount_point": mount_point,
        "secret_path": secret_path,
        "namespace": namespace,
        "verify_ssl": bool(verify_ssl),
    }
    result = setup_svc.verify_vault_connection(
        vault_payload=payload,
        tier=tier,
        env_name=env_name,
        has_gateway4=bool(gateway4_uri.strip()),
        has_iag5=bool(iag5_uri.strip()),
    )
    return JSONResponse(result)


@router.post("", response_class=HTMLResponse)
async def submit_setup(
    request: Request,
    organization_name: str = Form(""),
    tier: str = Form("standard"),
    env_name: str = Form(""),
    platform_uri: str = Form(""),
    platform_client_id: str = Form(""),
    platform_client_secret: str = Form(""),
    gateway4_uri: str = Form(""),
    gateway4_username: str = Form(""),
    gateway4_password: str = Form(""),
    # SaaS — single-gateway audit fields (saas_-prefixed so they never collide
    # with the optional platform-tier Gateway 4 disclose on the same form)
    saas_gateway_kind: str = Form(""),
    saas_gw4_ssh: str = Form(""),
    saas_gw4_uri: str = Form(""),
    saas_gw4_username: str = Form(""),
    saas_gw4_password: str = Form(""),
    saas_iag_host: str = Form(""),
    saas_ssh_user: str = Form(""),
    saas_ssh_port: str = Form(""),
    saas_ssh_key: str = Form(""),
    saas_gw5_source: str = Form("ssh"),
    saas_gw5_source_path: str = Form(""),
    saas_gw5_conf_path: str = Form(""),
    verify_ssl: str = Form(""),
    credential_backend: str = Form("keyring"),
    vault_secret_store: str = Form("keyring"),
    # Appearance — applied during the wizard via live preview, persisted on submit
    webui_theme: str = Form(""),
    webui_mode: str = Form(""),
    # Vault-only fields — ignored when credential_backend == 'keyring'
    vault_url: str = Form(""),
    vault_auth_method: str = Form("token"),
    vault_token: str = Form(""),
    vault_role_id: str = Form(""),
    vault_secret_id: str = Form(""),
    vault_wrapping_token: str = Form(""),
    vault_token_file_path: str = Form(""),
    vault_mount_point: str = Form("secret"),
    vault_secret_path: str = Form("platform-atlas"),
    vault_namespace: str = Form(""),
    vault_verify_ssl: str = Form(""),
):
    if setup_svc.is_initialized():
        return RedirectResponse(url="/", status_code=303)

    backend_choice = (credential_backend or "keyring").strip().lower()
    if (tier or "").strip().lower() == "saas":
        # The SaaS Connect step posts its own gateway-API fields.
        gateway4_uri = saas_gw4_uri
        gateway4_username = saas_gw4_username
        gateway4_password = saas_gw4_password
    form_payload = {
        "organization_name": organization_name,
        "tier": tier,
        "env_name": env_name,
        "saas_gateway_kind": saas_gateway_kind,
        "saas_gw4_ssh": saas_gw4_ssh,
        "saas_gw4_uri": saas_gw4_uri,
        "saas_gw4_username": saas_gw4_username,
        "saas_iag_host": saas_iag_host,
        "saas_ssh_user": saas_ssh_user,
        "saas_ssh_port": saas_ssh_port,
        "saas_ssh_key": saas_ssh_key,
        "saas_gw5_source": saas_gw5_source,
        "saas_gw5_source_path": saas_gw5_source_path,
        "saas_gw5_conf_path": saas_gw5_conf_path,
        "platform_uri": platform_uri,
        "platform_client_id": platform_client_id,
        "gateway4_uri": gateway4_uri,
        "gateway4_username": gateway4_username,
        "verify_ssl": bool(verify_ssl),
        "credential_backend": backend_choice,
        "vault_secret_store": (vault_secret_store or "keyring").strip().lower(),
        "vault_url": vault_url,
        "vault_auth_method": vault_auth_method,
        "vault_role_id": vault_role_id,
        "vault_token_file_path": vault_token_file_path,
        "vault_mount_point": vault_mount_point or "secret",
        "vault_secret_path": vault_secret_path or "platform-atlas",
        "vault_namespace": vault_namespace,
        "vault_verify_ssl": bool(vault_verify_ssl),
    }

    vault_payload = {
        "url": vault_url,
        "auth_method": vault_auth_method,
        "token": vault_token,
        "role_id": vault_role_id,
        "secret_id": vault_secret_id,
        "wrapping_token": vault_wrapping_token,
        "token_file_path": vault_token_file_path,
        "mount_point": vault_mount_point or "secret",
        "secret_path": vault_secret_path or "platform-atlas",
        "namespace": vault_namespace,
        "verify_ssl": bool(vault_verify_ssl),
    }

    try:
        result = setup_svc.bootstrap(
            organization_name=organization_name,
            tier=tier,
            env_name=env_name,
            platform_uri=platform_uri,
            platform_client_id=platform_client_id,
            platform_client_secret=platform_client_secret,
            gateway4_uri=gateway4_uri,
            gateway4_username=gateway4_username,
            gateway4_password=gateway4_password,
            saas_gateway_kind=saas_gateway_kind,
            saas_gw4_ssh=bool(saas_gw4_ssh),
            saas_iag_host=saas_iag_host,
            saas_ssh_user=saas_ssh_user,
            saas_ssh_port=saas_ssh_port,
            saas_ssh_key=saas_ssh_key,
            saas_gw5_source=saas_gw5_source,
            saas_gw5_source_path=saas_gw5_source_path,
            saas_gw5_conf_path=saas_gw5_conf_path,
            verify_ssl=bool(verify_ssl),
            credential_backend=backend_choice,
            vault_secret_store=(vault_secret_store or "keyring").strip().lower(),
            vault_payload=vault_payload if backend_choice == "vault" else None,
            webui_theme=webui_theme,
            webui_mode=webui_mode,
        )
    except ValueError as exc:
        return _render_form(
            request,
            error=str(exc),
            form=form_payload,
            tier=tier,
            organization_name=organization_name,
            status_code=400,
        )
    except Exception as exc:  # noqa: BLE001 — surface to user
        # Log full detail server-side; show a generic message to the browser
        # so we don't leak class names, file paths, or stack frames.
        logger.exception("Setup bootstrap failed: payload=%s", redact(form_payload))
        del exc  # avoid accidental interpolation
        return _render_form(
            request,
            error=(
                "Setup failed unexpectedly. The full error has been written to "
                "the server log — re-check your inputs and try again, or contact "
                "your administrator."
            ),
            form=form_payload,
            tier=tier,
            organization_name=organization_name,
            status_code=500,
        )

    cred_err = result.get("credentials", {}).get("error")
    if cred_err:
        # The config + env were written but the secret didn't make it into
        # the keyring (or Vault config persist failed) — keep the user
        # in the wizard so they can retry, but don't lose the dir state.
        return _render_form(
            request,
            error=(
                "Setup wrote the config and environment but couldn't store "
                f"credentials in {backend_choice}: {cred_err}. "
                "Continue — you can store them later via Config."
            ),
            form=form_payload,
            tier=tier,
            organization_name=organization_name,
            active_environment=result.get("environment"),
            status_code=200,
        )

    # The caller is already authenticated (auth middleware guards /setup);
    # never mint a session cookie here.
    return RedirectResponse(url="/setup/done", status_code=303)


def _done_tier(cfg: dict) -> str:
    """The tier the finished setup actually created — the active env's
    overlay tier when set, falling back to the global value. (Bootstrap
    writes the same tier to both since 2.0.0, but the overlay stays the
    source of truth in case the config was hand-edited in between.)"""
    env_name = cfg.get("active_environment") or ""
    if env_name:
        try:
            from platform_atlas.core.paths import ATLAS_ENVIRONMENTS_DIR
            import json as _json
            env_file = ATLAS_ENVIRONMENTS_DIR / f"{env_name}.json"
            if env_file.is_file():
                env_tier = (_json.loads(env_file.read_text(encoding="utf-8")).get("tier") or "").lower()
                if env_tier in ("standard", "extended", "saas"):
                    return env_tier
        except Exception:
            pass
    return cfg.get("tier") or "standard"


@router.get("/done", response_class=HTMLResponse)
async def setup_done(request: Request) -> HTMLResponse:
    """Post-bootstrap success view — the peak/end moment of onboarding.

    Shows what was configured and a 3-card 'Sessions in 60 seconds'
    explainer. Reachable only after bootstrap; if the user lands here
    on a fresh box, bounce them back to the wizard.
    """
    if not setup_svc.is_initialized():
        return RedirectResponse(url="/setup", status_code=303)

    from platform_atlas_webui.services import config as _cfg_svc
    cfg = _cfg_svc.read_config()
    theme, mode = _cfg_svc.resolve_appearance(cfg)
    return _templates.TemplateResponse(
        request,
        "setup/done.html",
        {
            "request": request,
            "atlas_version": ATLAS_VERSION,
            "organization_name": cfg.get("organization_name") or "",
            "active_environment": cfg.get("active_environment") or "",
            "tier": _done_tier(cfg),
            "prefs": {"theme": theme, "mode": mode},
            "csrf_token": generate_csrf_token(request.cookies.get(COOKIE_NAME)),
            "csp_nonce": getattr(request.state, "csp_nonce", "") or "",
        },
    )
