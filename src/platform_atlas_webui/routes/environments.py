"""Environment routes — list, view, create, edit, delete, set active."""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from platform_atlas.core._version import __version__ as ATLAS_VERSION

from platform_atlas_webui.dependencies import get_templates, template_context
from platform_atlas_webui.services import config as _cfg_svc
from platform_atlas_webui.services import environments as env_svc
from platform_atlas_webui.services import ssh_keys as ssh_keys_svc

router = APIRouter(prefix="/environments", tags=["environments"])
_templates = get_templates()
logger = logging.getLogger(__name__)


def _missing_credentials(env: dict) -> list[dict]:
    """Return required credentials missing for ``env`` at its declared tier.

    Each entry: ``{"key": str, "display": str}``. Empty list when everything
    needed is already in place (or when we can't introspect the backend —
    we degrade silently rather than scaring the user).
    """
    from platform_atlas.core.credentials import (
        CredentialKey,
        CredentialStore,
        CredentialBackendType,
        EXTENDED_ONLY_KEYS,
        scoped_service_name,
    )

    name = env.get("name") or ""
    tier = (env.get("tier") or "extended").lower()
    data = env.get("data") or {}
    backend = data.get("credential_backend") or "keyring"

    # The same required-set the credentials page uses; keep them in sync.
    required_keys = [CredentialKey.PLATFORM_SECRET]
    if tier != "standard":
        required_keys += [CredentialKey.MONGO_URI, CredentialKey.REDIS_URI]

    try:
        bt = CredentialBackendType(backend)
        service = scoped_service_name(name) if name else "platform-atlas"
        store = CredentialStore(service=service, backend_type=bt, env_name=name or None)
        missing = []
        for ck in required_keys:
            if ck in EXTENDED_ONLY_KEYS and tier == "standard":
                continue
            try:
                present = store._backend.exists(ck.value)  # noqa: SLF001
            except Exception:
                present = False
            if not present:
                missing.append({"key": ck.value, "display": ck.display_name})
        return missing
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not introspect creds for env %r: %s", name, exc)
        return []


@router.get("", response_class=HTMLResponse)
async def list_environments(request: Request, from_tier: str = Query("")) -> HTMLResponse:
    items = env_svc.list_environments()
    return _templates.TemplateResponse(
        request,
        "environments/list.html",
        template_context(
            request,
            atlas_version=ATLAS_VERSION,
            environments=items,
            from_tier=from_tier,
        ),
    )


@router.get("/new", response_class=HTMLResponse)
async def new_environment_form(request: Request) -> HTMLResponse:
    # ``organization_name`` is no longer a per-env field — it lives in the
    # global Configuration and is supplied to the template by ``template_context``.
    return _templates.TemplateResponse(
        request,
        "environments/form.html",
        template_context(
            request,
            atlas_version=ATLAS_VERSION,
            mode="create",
            env=None,
            ssh_keys=ssh_keys_svc.list_private_keys(),
        ),
    )


# ── Live name-collision check for the create form ─────────────────────
# Registered ABOVE `/{name}` so FastAPI matches it before the dynamic
# environment-detail route. The form input fires this with hx-trigger
# "keyup changed delay:300ms"; the response is a small status pill
# swapped into a sibling div. Only used in create mode — the edit form
# renders the name field readonly.
_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,127}$")


def _name_status(tone: str, dot: str, text: str) -> str:
    return (
        f'<span class="inline-flex items-center gap-1.5 text-{tone} text-[12px] font-medium">'
        f'<span class="inline-block w-1.5 h-1.5 rounded-full bg-{dot}"></span>'
        f'{text}</span>'
    )


@router.get("/check-kubectl", response_class=HTMLResponse)
async def check_kubectl_binary(path: str = "") -> HTMLResponse:
    """HTMX endpoint: verify the kubectl binary at the given path (or in PATH)."""
    import shutil
    import os
    from pathlib import Path as _Path

    ok_style = "color:var(--green,#4ade80); font-size:12px; font-weight:500;"
    warn_style = "color:var(--orange,#fb923c); font-size:12px; font-weight:500;"
    err_style = "color:var(--red,#f87171); font-size:12px; font-weight:500;"

    if path.strip():
        p = _Path(path.strip()).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return HTMLResponse(
                f'<span style="{ok_style}">✓ Found: {p}</span>'
            )
        if p.exists():
            return HTMLResponse(
                f'<span style="{err_style}">✗ Not executable: {p}</span>'
            )
        return HTMLResponse(
            f'<span style="{err_style}">✗ Not found: {p}</span>'
        )

    found = shutil.which("kubectl")
    if found:
        return HTMLResponse(
            f'<span style="{ok_style}">✓ Found in PATH: {found}</span>'
        )
    return HTMLResponse(
        f'<span style="{warn_style}">⚠ kubectl not found in PATH — '
        f'enter the full path to the binary below</span>'
    )


@router.get("/validate-name", response_class=HTMLResponse)
async def validate_environment_name(value: str = "") -> HTMLResponse:
    value = value.strip()
    if not value:
        return HTMLResponse('<span class="text-text-3 text-[12px]">Type a name above.</span>')
    if not _ENV_NAME_PATTERN.match(value):
        return HTMLResponse(_name_status(
            "warn", "warn",
            "Letters, digits, spaces, dots, hyphens, underscores · must start with a letter or digit",
        ))
    existing = await run_in_threadpool(env_svc.list_environments)
    if any((e.get("name") or "").lower() == value.lower() for e in existing):
        return HTMLResponse(_name_status("bad", "bad", f'"{value}" already exists'))
    return HTMLResponse(_name_status("ok", "ok", f'"{value}" is available'))


@router.get("/{name}", response_class=HTMLResponse)
async def view_environment(
    request: Request,
    name: str,
    just_created: int = Query(0),
) -> HTMLResponse:
    env = env_svc.get_environment(name)
    if env is None:
        raise HTTPException(status_code=404, detail=f"Environment '{name}' not found")
    missing_creds = _missing_credentials(env)
    topology = env_svc.topology_summary(env.get("data"))
    return _templates.TemplateResponse(
        request,
        "environments/detail.html",
        template_context(
            request,
            atlas_version=ATLAS_VERSION,
            environment=env,
            just_created=bool(just_created),
            missing_creds=missing_creds,
            topology=topology,
        ),
    )


@router.get("/{name}/edit", response_class=HTMLResponse)
async def edit_environment_form(request: Request, name: str, from_tier: str = Query("")) -> HTMLResponse:
    env = env_svc.get_environment(name)
    if env is None:
        raise HTTPException(status_code=404, detail=f"Environment '{name}' not found")
    return _templates.TemplateResponse(
        request,
        "environments/form.html",
        template_context(
            request,
            atlas_version=ATLAS_VERSION,
            mode="edit",
            env=env,
            from_tier=from_tier,
            ssh_keys=ssh_keys_svc.list_private_keys(),
        ),
    )


@router.post("")
async def save_environment(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    platform_uri: str = Form(""),
    platform_client_id: str = Form(""),
    tier: str = Form("extended"),
    credential_backend: str = Form("keyring"),
    gateway4_uri: str = Form(""),
    gateway4_username: str = Form(""),
    legacy_profile: str = Form(""),
    ssh_key: str = Form(""),
    values_yaml_path: str = Form(""),
    iag5_values_yaml_path: str = Form(""),
    kubectl_context: str = Form(""),
    kubectl_namespace: str = Form(""),
    use_kubectl: str = Form(""),
    kubectl_binary_path: str = Form(""),
    # ── Topology fields ── (Extended-only; Standard ignores these and the
    # capture engine synthesizes targets from platform_uri at runtime.)
    deployment_mode: str = Form(""),
    iap_host: str = Form(""),
    mongo_host: str = Form(""),
    redis_host: str = Form(""),
    iag_host: str = Form(""),
    # HA2 multi-node hosts. The "_ha"-suffixed primaries shadow the standalone
    # fields when deployment_mode == "ha2" so the two panes don't fight over
    # the same input names.
    iap_host_ha: str = Form(""),
    iap_host_2: str = Form(""),
    iap_host_3: str = Form(""),
    mongo_host_ha: str = Form(""),
    mongo_host_2: str = Form(""),
    mongo_host_3: str = Form(""),
    redis_host_ha: str = Form(""),
    redis_host_2: str = Form(""),
    redis_host_3: str = Form(""),
    iag_host_ha: str = Form(""),
    ssh_user: str = Form(""),
    ssh_port: str = Form(""),
    # IAP transport — applies to the primary IAP node in standalone and HA2;
    # custom/k8s ignore these. HA2 secondary/tertiary nodes keep whatever
    # transport the CLI wizard previously set on them.
    iap_transport: str = Form(""),
    iap_cm_socket: str = Form(""),
    iap_cm_target: str = Form(""),
    iap_cm_port: str = Form(""),
):
    # Standard active tier locks env tier to Standard for both create and edit.
    # Mirrors the disabled dropdown in templates/environments/form.html — this
    # rejects crafted POSTs that bypass the UI lock so the tier boundary holds
    # for direct form submissions too.
    posted_tier = (tier or "").strip().lower() or "extended"
    if _cfg_svc.resolve_active_tier() == "standard" and posted_tier != "standard":
        raise HTTPException(
            status_code=400,
            detail=(
                "Active tier is Standard — environments cannot be saved with a "
                "different tier. Switch the active tier via /tier first."
            ),
        )

    payload = {
        "name": name.strip(),
        "description": description,
        "platform_uri": platform_uri,
        "platform_client_id": platform_client_id,
        "tier": tier or None,
        "credential_backend": credential_backend,
        "gateway4_uri": gateway4_uri,
        "gateway4_username": gateway4_username,
        "legacy_profile": legacy_profile or None,
        "ssh_key": ssh_key,
        "values_yaml_path": values_yaml_path,
        "iag5_values_yaml_path": iag5_values_yaml_path,
        "kubectl_context": kubectl_context,
        "kubectl_namespace": kubectl_namespace,
        "use_kubectl": use_kubectl,
        "kubectl_binary_path": kubectl_binary_path,
        "deployment_mode": deployment_mode,
        "iap_host": iap_host,
        "mongo_host": mongo_host,
        "redis_host": redis_host,
        "iag_host": iag_host,
        "iap_host_ha": iap_host_ha,
        "iap_host_2": iap_host_2,
        "iap_host_3": iap_host_3,
        "mongo_host_ha": mongo_host_ha,
        "mongo_host_2": mongo_host_2,
        "mongo_host_3": mongo_host_3,
        "redis_host_ha": redis_host_ha,
        "redis_host_2": redis_host_2,
        "redis_host_3": redis_host_3,
        "iag_host_ha": iag_host_ha,
        "ssh_user": ssh_user,
        "ssh_port": ssh_port,
        "iap_transport": iap_transport,
        "iap_cm_socket": iap_cm_socket,
        "iap_cm_target": iap_cm_target,
        "iap_cm_port": iap_cm_port,
    }
    # Detect create vs edit so we can lead first-time users into setting up
    # credentials right after the env is saved. We don't trust a hidden form
    # field for this — just check whether the env existed before save.
    is_create = env_svc.get_environment(payload["name"]) is None

    try:
        env = env_svc.save_environment(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc

    target = f"/environments/{env.name}"
    if is_create:
        target += "?just_created=1"
    return RedirectResponse(url=target, status_code=303)


def _safe_next_url(value: str) -> str:
    """Return ``value`` if it's a same-origin path, else ``''``.

    Blocks open-redirect tricks: protocol-relative ``//evil``, backslash
    smuggling, or fully-qualified URLs. Only accepts paths starting with a
    single ``/`` followed by a non-slash character.
    """
    value = (value or "").strip()
    if len(value) < 2 or not value.startswith("/"):
        return ""
    if value.startswith("//") or value.startswith("/\\"):
        return ""
    return value


@router.post("/{name}/activate")
async def activate_environment(name: str, next: str = Form("")):
    """Activate ``name`` as the current env. Optional ``next`` form field
    redirects somewhere other than the environments list — used by the
    "Set up credentials" CTA on the env detail page.
    """
    try:
        env_svc.set_active(name)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        from platform_atlas.core.context import init_context
        init_context()
    except Exception:
        pass
    target = _safe_next_url(next) or "/environments"
    return RedirectResponse(url=target, status_code=303)


@router.post("/{name}/delete")
async def delete_environment(name: str):
    try:
        env_svc.delete_environment(name)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/environments", status_code=303)
