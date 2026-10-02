"""Tier route — view the active tier and switch from the browser.

Switching to Extended is a guided walkthrough, not a single write: POST
/tier/set marks the upgrade *pending* (``pending_tier_upgrade`` in
config.json) and sends the user to /tier/extended/setup to supply SSH,
MongoDB, and Redis credentials. The ``tier`` field itself is only written
by /tier/extended/setup/finish, once every required credential is
present — so leaving the walkthrough at any point (closing the tab,
hitting Cancel) leaves the environment in Standard, unchanged. Switching
to Standard needs no extra credentials and is written immediately, same
as before.

Writes go through the same atomic write the CLI's ``tier set`` handler
uses, so changes are immediately visible to a fresh CLI invocation. The
current process keeps its already-resolved tier until restart — flipping
tier mid-process would invalidate cached collectors.
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
from platform_atlas_webui.services import config as config_svc

router = APIRouter(prefix="/tier", tags=["tier"])
_templates = get_templates()


@router.get("", response_class=HTMLResponse)
async def tier_overview(request: Request) -> HTMLResponse:
    # Read tier directly from disk so the page always reflects the current
    # config.json value rather than the frozen in-memory context loaded at startup.
    active_tier = "extended"
    org = ""
    pending_extended = False
    try:
        import os
        data = json.loads(ATLAS_CONFIG_FILE.read_text(encoding="utf-8"))
        active_tier = data.get("tier", "extended")
        org = data.get("organization_name", "")
        # Set by /tier/set while an Extended upgrade is awaiting credential
        # verification — the root ``tier`` field hasn't changed yet.
        pending_extended = bool(data.get("pending_tier_upgrade")) and active_tier != "extended"
        # The active environment's overlay tier wins over the root config tier
        # (same precedence as load_config), so this page reflects the tier the
        # active environment actually runs as — not a stale root default. Without
        # this, a Standard active environment showed "extended" here.
        env_name = data.get("active_environment") or ""
        if env_name:
            from platform_atlas.core.paths import ATLAS_ENVIRONMENTS_DIR
            env_file = ATLAS_ENVIRONMENTS_DIR / f"{env_name}.json"
            if env_file.is_file():
                env_data = json.loads(env_file.read_text(encoding="utf-8"))
                if env_data.get("tier"):
                    active_tier = env_data["tier"]
        env_tier = os.environ.get("ATLAS_TIER")
        if env_tier and env_tier.strip().lower() in ("standard", "extended", "saas"):
            active_tier = env_tier.strip().lower()
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
            pending_extended=pending_extended,
        ),
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/set")
async def tier_set(new_tier: Literal["standard", "extended"] = Form(...)):
    """Start an Extended upgrade (pending) or switch to Standard (immediate)."""
    if not ATLAS_CONFIG_FILE.is_file():
        raise HTTPException(
            status_code=400,
            detail=f"Atlas config not found at {ATLAS_CONFIG_FILE}",
        )
    try:
        data = json.loads(ATLAS_CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"Could not read config: {exc}") from exc

    if new_tier == "extended":
        # Don't flip the tier yet — mark the upgrade pending and send the user
        # to the credential walkthrough. ``tier`` itself is only written by
        # /tier/extended/setup/finish once every required credential is
        # verified present, so abandoning the walkthrough here leaves the
        # environment in Standard, unchanged.
        data["pending_tier_upgrade"] = True
        try:
            atomic_write_json(ATLAS_CONFIG_FILE, data)
        except Exception as exc:  # noqa: BLE001 — surface IO failures to the user
            raise HTTPException(status_code=500, detail=f"Could not write config: {exc}") from exc
        return RedirectResponse(url="/tier/extended/setup", status_code=303)

    # Standard needs no extra credentials — switch immediately and clear any
    # Extended upgrade that was left pending.
    data["tier"] = "standard"
    data.pop("pending_tier_upgrade", None)
    try:
        atomic_write_json(ATLAS_CONFIG_FILE, data)
    except Exception as exc:  # noqa: BLE001 — surface IO failures to the user
        raise HTTPException(status_code=500, detail=f"Could not write config: {exc}") from exc

    # The env overlay's tier wins over root config in load_config(), so a stale
    # ``tier`` field in the active environment file would silently undo the
    # switch. The service helper mirrors the new tier into the overlay, skips
    # SaaS environments (tier fixed at create time — the switch then only
    # changes the global default, exactly what the /tier overview promises),
    # and invalidates the resolve_active_tier() cache.
    config_svc.mirror_tier_to_active_overlay("standard")

    # Reload the in-memory context so subsequent operations (capture, validate)
    # immediately use the new tier without requiring a server restart.
    try:
        from platform_atlas.core.context import init_context
        init_context()
    except Exception:
        pass  # Best-effort — the disk write already succeeded

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
        "store_is_file": False,
        "store_label": "OS Keyring",
    }

    # Reflect the ACTUAL local secret-store substrate (OS keyring vs. the
    # encrypted file) so the setup banner never claims "OS Keyring" for a file
    # store — same honesty rule the credentials page follows.
    try:
        from platform_atlas.core.credentials import active_secret_store
        _store = active_secret_store()
        result["store_label"] = _store.display_name
        result["store_is_file"] = bool(getattr(_store, "is_file", False))
    except Exception:  # noqa: BLE001 — a display helper must never break the page
        pass

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
    """Credential walkthrough for a pending Extended upgrade, or a standing
    recheck page for an environment that's already Extended. Not reachable
    otherwise — there's nothing to verify."""
    try:
        raw = json.loads(ATLAS_CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    pending = bool(raw.get("pending_tier_upgrade"))
    already_extended = raw.get("tier") == "extended" and not pending
    if not pending and not already_extended:
        return RedirectResponse(url="/tier", status_code=303)

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
            pending=pending,
        ),
    )


@router.post("/extended/setup/finish")
async def finish_extended_setup():
    """Confirm the pending Extended upgrade — the only place ``tier`` is
    actually written to ``extended``, and only once every required
    credential is verified present."""
    cred_status = _check_extended_credentials()
    all_required_present = all(
        c["present"] for c in cred_status["credentials"] if c["required"]
    )
    if not all_required_present:
        # Nothing to finish yet — required credentials are still missing.
        return RedirectResponse(url="/tier/extended/setup", status_code=303)

    if not ATLAS_CONFIG_FILE.is_file():
        raise HTTPException(
            status_code=400,
            detail=f"Atlas config not found at {ATLAS_CONFIG_FILE}",
        )
    try:
        data = json.loads(ATLAS_CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"Could not read config: {exc}") from exc

    data["tier"] = "extended"
    data.pop("pending_tier_upgrade", None)
    try:
        atomic_write_json(ATLAS_CONFIG_FILE, data)
    except Exception as exc:  # noqa: BLE001 — surface IO failures to the user
        raise HTTPException(status_code=500, detail=f"Could not write config: {exc}") from exc

    config_svc.mirror_tier_to_active_overlay("extended")
    try:
        from platform_atlas.core.context import init_context
        init_context()
    except Exception:
        pass  # Best-effort — the disk write already succeeded

    return RedirectResponse(url="/tier?upgraded=1", status_code=303)


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
    """Cancel a pending Extended upgrade, or downgrade an active Extended
    environment — either way, config.json ends up at Standard with no
    upgrade left pending. If the upgrade was only ever pending, ``tier``
    was already Standard, so this just clears the pending flag."""
    if not ATLAS_CONFIG_FILE.is_file():
        return RedirectResponse(url="/tier", status_code=303)
    try:
        data = json.loads(ATLAS_CONFIG_FILE.read_text(encoding="utf-8"))
        data["tier"] = "standard"
        data.pop("pending_tier_upgrade", None)
        atomic_write_json(ATLAS_CONFIG_FILE, data)
        # Same SaaS-aware overlay mirror as tier_set above.
        config_svc.mirror_tier_to_active_overlay("standard")
        from platform_atlas.core.context import init_context
        init_context()
    except Exception:
        pass
    return RedirectResponse(url="/tier", status_code=303)
