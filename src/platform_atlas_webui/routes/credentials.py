"""Credentials route — view and manage Atlas credential store from the browser.

GET  /config/credentials            — status page (tier + backend aware)
POST /config/credentials/set        — store one credential (Keyring only)
POST /config/credentials/delete     — remove one credential (Keyring only)
POST /config/credentials/vault-config — save Vault connection settings

The page reads config.json from disk on every request so it always reflects
the current backend and tier rather than the frozen in-memory AtlasContext.
Writes go directly to the OS keyring via CredentialStore; no credentials are
ever stored in config.json.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from platform_atlas.core._version import __version__ as ATLAS_VERSION
from platform_atlas.core.paths import ATLAS_CONFIG_FILE

from platform_atlas_webui.dependencies import get_templates, template_context

router = APIRouter(prefix="/config/credentials", tags=["credentials"])
_templates = get_templates()
logger = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _read_runtime_config() -> tuple[str, str, str]:
    """Return (tier, backend_type, env_name) from disk, merging env overlay."""
    tier = "standard"
    backend_type = "keyring"
    env_name = ""
    try:
        raw = json.loads(ATLAS_CONFIG_FILE.read_text(encoding="utf-8"))
        tier = raw.get("tier") or "standard"
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
    return tier, backend_type, env_name


def _load_credential_status() -> dict:
    """
    Build the full credential status dict for the template.

    Returns a dict with:
      backend, env_name, tier, vault_url, vault_path, vault_config,
      connected, error, credentials (list of per-key dicts)
    """
    from platform_atlas.core.credentials import (
        CredentialKey,
        CredentialStore,
        CredentialBackendType,
        EXTENDED_ONLY_KEYS,
        scoped_service_name,
    )

    tier, backend_type, env_name = _read_runtime_config()

    result: dict = {
        "backend": backend_type,
        "env_name": env_name,
        "tier": tier,
        "vault_url": "",
        "vault_path": "",
        "vault_config": None,   # VaultConfig dataclass for the reconfigure form
        "token_ttl": 0,
        "token_renewable": False,
        "connected": False,
        "error": None,
        "credentials": [],
    }

    # Ordered list of all credentials with their metadata
    key_meta = [
        {
            "key": CredentialKey.PLATFORM_SECRET,
            "required": True,
            "tier_note": "both tiers",
            "extended_only": False,
            "description": "OAuth client secret for authenticating with IAP.",
        },
        {
            "key": CredentialKey.GATEWAY4_PASSWORD,
            "required": False,
            "tier_note": "both tiers — optional",
            "extended_only": False,
            "description": "API password for Gateway 4 (ipsdk authentication).",
        },
        {
            "key": CredentialKey.MONGO_URI,
            "required": True,
            "tier_note": "Extended only",
            "extended_only": True,
            "description": "Full MongoDB connection URI including auth credentials.",
        },
        {
            "key": CredentialKey.REDIS_URI,
            "required": True,
            "tier_note": "Extended only",
            "extended_only": True,
            "description": "Redis connection URI (redis://user:pass@host:port/db).",
        },
        {
            "key": CredentialKey.SSH_PASSPHRASE,
            "required": False,
            "tier_note": "Extended only — optional if key has no passphrase",
            "extended_only": True,
            "description": "Passphrase for the SSH private key used in Extended captures.",
        },
    ]

    try:
        bt = CredentialBackendType(backend_type)
        service = scoped_service_name(env_name) if env_name else "platform-atlas"
        store = CredentialStore(service=service, backend_type=bt, env_name=env_name or None)

        if bt == CredentialBackendType.VAULT:
            from platform_atlas.core.credentials import VaultBackend
            if isinstance(store._backend, VaultBackend):  # noqa: SLF001
                vb: VaultBackend = store._backend  # noqa: SLF001
                cfg = vb.config
                result["vault_url"] = cfg.display_url
                result["vault_path"] = cfg.full_path
                result["vault_config"] = cfg
                result["token_ttl"] = vb.token_ttl
                result["token_renewable"] = vb.token_renewable

        result["connected"] = True

        for m in key_meta:
            ck: CredentialKey = m["key"]
            # Bypass tier guard — we want actual keyring/vault state regardless
            # of the active tier so the user can see what's stored.
            try:
                present = store._backend.exists(ck.value)  # noqa: SLF001
            except Exception:
                present = False

            # In Standard mode, Extended-only credentials are inaccessible
            unavailable = (m["extended_only"] and tier == "standard")

            result["credentials"].append({
                "key": ck.value,
                "display": ck.display_name,
                "present": None if unavailable else present,
                "required": m["required"],
                "extended_only": m["extended_only"],
                "unavailable": unavailable,
                "tier_note": m["tier_note"],
                "description": m["description"],
            })

    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
        for m in key_meta:
            ck: CredentialKey = m["key"]
            unavailable = (m["extended_only"] and tier == "standard")
            result["credentials"].append({
                "key": ck.value,
                "display": ck.display_name,
                "present": None,
                "required": m["required"],
                "extended_only": m["extended_only"],
                "unavailable": unavailable,
                "tier_note": m["tier_note"],
                "description": m["description"],
            })

    return result


def _make_fresh_store():
    """Instantiate a CredentialStore from current disk config (bypasses singleton)."""
    from platform_atlas.core.credentials import (
        CredentialStore,
        CredentialBackendType,
        scoped_service_name,
    )
    _, backend_type, env_name = _read_runtime_config()
    bt = CredentialBackendType(backend_type)
    service = scoped_service_name(env_name) if env_name else "platform-atlas"
    return CredentialStore(service=service, backend_type=bt, env_name=env_name or None)


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def view_credentials(
    request: Request,
    saved: str = "",
    error: str = "",
    vault_saved: str = "",
    vault_ttl: int = 0,
) -> HTMLResponse:
    cred_status = _load_credential_status()

    flash = None
    if saved:
        flash = {"message": f"Credential saved: {saved}.", "kind": "ok"}
    elif vault_saved:
        ttl_note = ""
        if vault_ttl > 0:
            ttl_note = f" Token TTL: {vault_ttl // 60}m {vault_ttl % 60}s."
        flash = {"message": f"Vault connection settings saved and verified.{ttl_note}", "kind": "ok"}
    elif error:
        flash = {"message": error, "kind": "error"}

    present_count = sum(
        1 for c in cred_status["credentials"]
        if c["present"] is True
    )
    total_active = sum(
        1 for c in cred_status["credentials"]
        if not c["unavailable"]
    )

    response = _templates.TemplateResponse(
        request,
        "config/credentials.html",
        template_context(
            request,
            atlas_version=ATLAS_VERSION,
            cred_status=cred_status,
            flash=flash,
            present_count=present_count,
            total_active=total_active,
        ),
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/set")
async def set_credential(
    key: str = Form(...),
    value: str = Form(...),
):
    """Store a credential in the OS keyring. Vault backend rejects writes."""
    from platform_atlas.core.credentials import CredentialKey

    # Validate key
    valid_keys = {ck.value: ck for ck in CredentialKey}
    if key not in valid_keys:
        return RedirectResponse(
            url=f"/config/credentials?error=Unknown+credential+key+%27{key}%27",
            status_code=303,
        )
    if not value.strip():
        return RedirectResponse(
            url=f"/config/credentials?error=Value+cannot+be+empty",
            status_code=303,
        )

    ck = valid_keys[key]
    try:
        store = _make_fresh_store()
        if store.is_read_only:
            return RedirectResponse(
                url="/config/credentials?error=Vault+backend+is+read-only+%E2%80%94+manage+secrets+directly+in+Vault",
                status_code=303,
            )
        # Write directly through the backend to bypass tier guard (user is
        # intentionally setting up creds before potentially switching tier).
        store._backend.set(ck.value, value.strip())  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001
        logger.warning("Credential set failed for %s: %s", key, exc)
        from urllib.parse import quote
        return RedirectResponse(
            url=f"/config/credentials?error={quote(str(exc))}",
            status_code=303,
        )

    from urllib.parse import quote
    return RedirectResponse(
        url=f"/config/credentials?saved={quote(ck.display_name)}",
        status_code=303,
    )


@router.post("/delete")
async def delete_credential(key: str = Form(...)):
    """Remove a credential from the OS keyring."""
    from platform_atlas.core.credentials import CredentialKey

    valid_keys = {ck.value: ck for ck in CredentialKey}
    if key not in valid_keys:
        return RedirectResponse(
            url=f"/config/credentials?error=Unknown+credential+key+%27{key}%27",
            status_code=303,
        )

    ck = valid_keys[key]
    try:
        store = _make_fresh_store()
        if store.is_read_only:
            return RedirectResponse(
                url="/config/credentials?error=Vault+backend+is+read-only",
                status_code=303,
            )
        store._backend.delete(ck.value)  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001
        logger.warning("Credential delete failed for %s: %s", key, exc)
        from urllib.parse import quote
        return RedirectResponse(
            url=f"/config/credentials?error={quote(str(exc))}",
            status_code=303,
        )

    from urllib.parse import quote
    return RedirectResponse(
        url=f"/config/credentials?saved={quote(ck.display_name + ' removed')}",
        status_code=303,
    )


@router.post("/vault-config")
async def save_vault_config(
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
    """Save Vault connection settings to the OS keyring and verify the connection."""
    from urllib.parse import quote

    if not vault_url.strip():
        return RedirectResponse(
            url="/config/credentials?error=Vault+URL+is+required",
            status_code=303,
        )

    from platform_atlas.core.credentials import (
        VaultAuthMethod,
        VaultBackend,
        VaultConfig,
        scoped_service_name,
    )

    try:
        auth_method = VaultAuthMethod(vault_auth_method)
    except ValueError:
        auth_method = VaultAuthMethod.TOKEN

    verify_ssl = vault_verify_ssl.strip().lower() in ("1", "true", "yes", "on")

    vault_config = VaultConfig(
        url=vault_url.strip(),
        auth_method=auth_method,
        token=vault_token.strip() or None,
        role_id=vault_role_id.strip() or None,
        secret_id=vault_secret_id.strip() or None,
        wrapping_token=vault_wrapping_token.strip() or None,
        token_file_path=vault_token_file_path.strip() or None,
        mount_point=vault_mount_point.strip() or "secret",
        secret_path=vault_secret_path.strip() or "platform-atlas",
        namespace=vault_namespace.strip() or None,
        verify_ssl=verify_ssl,
    )

    _, _, env_name = _read_runtime_config()
    service = scoped_service_name(env_name) if env_name else "platform-atlas"

    try:
        # Test connection before saving — capture backend to read TTL
        test_backend = VaultBackend(vault_config=vault_config, service=service)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Vault connection test failed: %s", exc)
        return RedirectResponse(
            url=f"/config/credentials?error={quote('Vault connection failed: ' + str(exc))}",
            status_code=303,
        )

    try:
        VaultBackend.save_config_to_keyring(vault_config, service=service)
        # Reset the module-level singleton so next credential access picks up new config
        from platform_atlas.core.credentials import reset_credential_store
        reset_credential_store()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Vault config save failed: %s", exc)
        return RedirectResponse(
            url=f"/config/credentials?error={quote(str(exc))}",
            status_code=303,
        )

    ttl_param = f"&vault_ttl={test_backend.token_ttl}" if test_backend.token_ttl > 0 else ""
    return RedirectResponse(url=f"/config/credentials?vault_saved=1{ttl_param}", status_code=303)
