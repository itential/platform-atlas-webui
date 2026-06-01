"""Tier route — view the active tier and switch from the browser.

POST /tier/set rewrites ``~/.atlas/config.json`` via the same atomic
write the CLI's ``tier set`` handler uses, so changes are immediately
visible to a fresh CLI invocation. The current process keeps its
already-resolved tier until restart — flipping tier mid-process would
invalidate cached collectors.
"""

from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from platform_atlas.core._version import __version__ as ATLAS_VERSION
from platform_atlas.core.paths import ATLAS_CONFIG_FILE
from platform_atlas.core.utils import atomic_write_json

from platform_atlas_webui.dependencies import get_templates, template_context

router = APIRouter(prefix="/tier", tags=["tier"])
_templates = get_templates()


@router.get("", response_class=HTMLResponse)
async def tier_overview(request: Request) -> HTMLResponse:
    # Read tier directly from disk so the page always reflects the current
    # config.json value rather than the frozen in-memory context loaded at startup.
    active_tier = "extended"
    org = ""
    try:
        data = json.loads(ATLAS_CONFIG_FILE.read_text(encoding="utf-8"))
        active_tier = data.get("tier", "extended")
        org = data.get("organization_name", "")
    except Exception:
        pass

    response = _templates.TemplateResponse(
        request,
        "tier/overview.html",
        template_context(
            request,
            atlas_version=ATLAS_VERSION,
            active_tier=active_tier,
            organization_name=org,
        ),
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/set")
async def tier_set(new_tier: Literal["standard", "extended"] = Form(...)):
    """Persist the new tier into config.json and redirect back to /tier."""
    if not ATLAS_CONFIG_FILE.is_file():
        raise HTTPException(
            status_code=400,
            detail=f"Atlas config not found at {ATLAS_CONFIG_FILE}",
        )
    try:
        data = json.loads(ATLAS_CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"Could not read config: {exc}") from exc

    data["tier"] = new_tier
    try:
        atomic_write_json(ATLAS_CONFIG_FILE, data)
    except Exception as exc:  # noqa: BLE001 — surface IO failures to the user
        raise HTTPException(status_code=500, detail=f"Could not write config: {exc}") from exc

    # The env overlay's tier wins over root config in load_config(), so a stale
    # ``tier`` field in the active environment file would silently undo the
    # switch. Mirror the new tier into the overlay when one is active.
    env_name = data.get("active_environment") or ""
    if env_name:
        try:
            from platform_atlas.core.paths import ATLAS_ENVIRONMENTS_DIR
            env_file = ATLAS_ENVIRONMENTS_DIR / f"{env_name}.json"
            if env_file.is_file():
                env_data = json.loads(env_file.read_text(encoding="utf-8"))
                env_data["tier"] = new_tier
                atomic_write_json(env_file, env_data)
        except Exception:
            pass  # Best-effort — root config write already succeeded

    # Reload the in-memory context so subsequent operations (capture, validate)
    # immediately use the new tier without requiring a server restart.
    try:
        from platform_atlas.core.context import init_context
        init_context()
    except Exception:
        pass  # Best-effort — the disk write already succeeded

    # Switching to Extended requires additional credentials (SSH, Mongo, Redis).
    # Send the user to the credential check page so they can verify before proceeding.
    # Switching to Standard needs nothing extra.
    if new_tier == "extended":
        return RedirectResponse(url="/tier/extended/setup", status_code=303)

    return RedirectResponse(url="/tier", status_code=303)


def _check_extended_credentials() -> dict:
    """
    Check which Extended-tier credentials are present in the active backend.

    Returns a dict describing the backend and per-key status. Never raises —
    errors are captured in the ``error`` field so the template can show them.
    """
    from platform_atlas.core.credentials import (
        CredentialKey,
        CredentialStore,
        CredentialBackendType,
        EXTENDED_ONLY_KEYS,
        scoped_service_name,
    )

    # Read backend + env from disk so we always reflect current config.
    # The environment overlay's credential_backend takes precedence over the
    # global config — the setup wizard writes it to the overlay, not the root.
    backend_type = "keyring"
    env_name = ""
    vault_url = ""
    vault_path = ""
    try:
        raw = json.loads(ATLAS_CONFIG_FILE.read_text(encoding="utf-8"))
        backend_type = raw.get("credential_backend") or "keyring"
        env_name = raw.get("active_environment") or ""
        # Merge environment overlay — it wins on credential_backend
        if env_name:
            from platform_atlas.core.paths import ATLAS_ENVIRONMENTS_DIR
            env_file = ATLAS_ENVIRONMENTS_DIR / f"{env_name}.json"
            if env_file.is_file():
                env_data = json.loads(env_file.read_text(encoding="utf-8"))
                if env_data.get("credential_backend"):
                    backend_type = env_data["credential_backend"]
    except Exception:
        pass

    result: dict = {
        "backend": backend_type,
        "env_name": env_name,
        "vault_url": "",
        "vault_path": "",
        "connected": False,
        "error": None,
        "credentials": [],
    }

    # Build per-key status list. All CredentialKey members are shown; Extended-only
    # ones are highlighted as required for this tier switch.
    key_meta = [
        {
            "key":         CredentialKey.PLATFORM_SECRET,
            "required":    True,
            "tier_note":   "both tiers",
            "description": "OAuth client secret for authenticating with IAP.",
            "placeholder": "client-secret-value",
        },
        {
            "key":         CredentialKey.MONGO_URI,
            "required":    True,
            "tier_note":   "Extended only",
            "description": "Full MongoDB connection URI including auth credentials.",
            "placeholder": "mongodb://user:pass@host:27017/?replicaSet=rs0",
        },
        {
            "key":         CredentialKey.REDIS_URI,
            "required":    True,
            "tier_note":   "Extended only",
            "description": "Redis connection URI used by IAP for sessions and pub/sub.",
            "placeholder": "redis://itential:pass@host:6379/0",
        },
        {
            "key":         CredentialKey.SSH_PASSPHRASE,
            "required":    False,
            "tier_note":   "Extended only — optional if key has no passphrase",
            "description": "Passphrase for the SSH private key used in Extended captures.",
            "placeholder": "leave blank if your key has no passphrase",
        },
        {
            "key":         CredentialKey.GATEWAY4_PASSWORD,
            "required":    False,
            "tier_note":   "optional",
            "description": "API password for Gateway 4 (ipsdk authentication).",
            "placeholder": "gateway4-password",
        },
    ]

    try:
        bt = CredentialBackendType(backend_type)
        service = scoped_service_name(env_name) if env_name else "platform-atlas"
        store = CredentialStore(service=service, backend_type=bt, env_name=env_name or None)

        if bt == CredentialBackendType.VAULT:
            from platform_atlas.core.credentials import VaultBackend
            if isinstance(store._backend, VaultBackend):  # noqa: SLF001
                cfg = store._backend.config  # noqa: SLF001
                result["vault_url"] = cfg.display_url
                result["vault_path"] = cfg.full_path

        result["connected"] = True

        for m in key_meta:
            ck: CredentialKey = m["key"]
            try:
                present = store._backend.exists(ck.value)  # noqa: SLF001
            except Exception:
                present = False
            result["credentials"].append({
                "key": ck.value,
                "display": ck.display_name,
                "present": present,
                "required": m["required"],
                "extended_only": ck in EXTENDED_ONLY_KEYS,
                "tier_note": m["tier_note"],
                "description": m["description"],
                "placeholder": m["placeholder"],
            })

    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
        # Still populate credential rows as unknown so the template can render
        for m in key_meta:
            ck: CredentialKey = m["key"]
            result["credentials"].append({
                "key": ck.value,
                "display": ck.display_name,
                "present": None,
                "required": m["required"],
                "extended_only": ck in EXTENDED_ONLY_KEYS,
                "tier_note": m["tier_note"],
                "description": m["description"],
                "placeholder": m["placeholder"],
            })

    return result


def _read_ssh_key_path() -> str:
    """Return the ssh_key value from the active environment overlay, or ''."""
    try:
        raw = json.loads(ATLAS_CONFIG_FILE.read_text(encoding="utf-8"))
        env_name = raw.get("active_environment") or ""
        if not env_name:
            return ""
        from platform_atlas.core.paths import ATLAS_ENVIRONMENTS_DIR
        env_file = ATLAS_ENVIRONMENTS_DIR / f"{env_name}.json"
        if env_file.is_file():
            env_data = json.loads(env_file.read_text(encoding="utf-8"))
            return env_data.get("ssh_key") or ""
    except Exception:
        pass
    return ""


@router.get("/extended/setup", response_class=HTMLResponse)
async def extended_setup(request: Request) -> HTMLResponse:
    """Credential verification page shown after switching to Extended tier."""
    cred_status = _check_extended_credentials()
    all_required_present = all(
        c["present"] for c in cred_status["credentials"] if c["required"]
    )
    return _templates.TemplateResponse(
        request,
        "tier/extended_setup.html",
        template_context(
            request,
            cred_status=cred_status,
            all_required_present=all_required_present,
            ssh_key_path=_read_ssh_key_path(),
        ),
    )


@router.post("/extended/setup/credential")
async def save_extended_credential(
    key: str = Form(...),
    value: str = Form(...),
):
    """Write one credential to the active backend during the Extended switch.

    The page only renders the form for the keyring backend (Vault is read-only),
    so we mirror ``routes/credentials.py:set_credential`` and write through the
    backend directly, bypassing the tier guard — the user has just flipped to
    Extended and the in-memory tier may not yet reflect the new value.
    """
    from platform_atlas.core.credentials import (
        CredentialKey,
        CredentialStore,
        CredentialBackendType,
        scoped_service_name,
    )

    valid_keys = {ck.value: ck for ck in CredentialKey}
    if key not in valid_keys or not value.strip():
        return RedirectResponse(url="/tier/extended/setup", status_code=303)

    backend_type = "keyring"
    env_name = ""
    try:
        raw = json.loads(ATLAS_CONFIG_FILE.read_text(encoding="utf-8"))
        backend_type = raw.get("credential_backend") or "keyring"
        env_name = raw.get("active_environment") or ""
        if env_name:
            from platform_atlas.core.paths import ATLAS_ENVIRONMENTS_DIR
            env_file = ATLAS_ENVIRONMENTS_DIR / f"{env_name}.json"
            if env_file.is_file():
                env_data = json.loads(env_file.read_text(encoding="utf-8"))
                if env_data.get("credential_backend"):
                    backend_type = env_data["credential_backend"]
    except Exception:
        pass

    try:
        bt = CredentialBackendType(backend_type)
        service = scoped_service_name(env_name) if env_name else "platform-atlas"
        store = CredentialStore(service=service, backend_type=bt, env_name=env_name or None)
        if not store.is_read_only:
            store._backend.set(valid_keys[key].value, value.strip())  # noqa: SLF001
    except Exception:
        pass

    return RedirectResponse(url="/tier/extended/setup", status_code=303)


@router.post("/extended/setup/ssh-key")
async def save_ssh_key(ssh_key: str = Form("")):
    """Persist the SSH key path into the active environment overlay."""
    try:
        raw = json.loads(ATLAS_CONFIG_FILE.read_text(encoding="utf-8"))
        env_name = raw.get("active_environment") or ""
        if env_name:
            from platform_atlas.core.paths import ATLAS_ENVIRONMENTS_DIR
            from platform_atlas.core.utils import atomic_write_json
            env_file = ATLAS_ENVIRONMENTS_DIR / f"{env_name}.json"
            env_data = json.loads(env_file.read_text(encoding="utf-8")) if env_file.is_file() else {}
            env_data["ssh_key"] = ssh_key.strip()
            atomic_write_json(env_file, env_data)
    except Exception:
        pass
    return RedirectResponse(url="/tier/extended/setup", status_code=303)


@router.post("/revert-to-standard")
async def revert_to_standard():
    """Cancel the Extended upgrade — revert config.json to Standard tier."""
    if not ATLAS_CONFIG_FILE.is_file():
        return RedirectResponse(url="/tier", status_code=303)
    try:
        data = json.loads(ATLAS_CONFIG_FILE.read_text(encoding="utf-8"))
        data["tier"] = "standard"
        atomic_write_json(ATLAS_CONFIG_FILE, data)
        env_name = data.get("active_environment") or ""
        if env_name:
            try:
                from platform_atlas.core.paths import ATLAS_ENVIRONMENTS_DIR
                env_file = ATLAS_ENVIRONMENTS_DIR / f"{env_name}.json"
                if env_file.is_file():
                    env_data = json.loads(env_file.read_text(encoding="utf-8"))
                    env_data["tier"] = "standard"
                    atomic_write_json(env_file, env_data)
            except Exception:
                pass
        from platform_atlas.core.context import init_context
        init_context()
    except Exception:
        pass
    return RedirectResponse(url="/tier", status_code=303)
