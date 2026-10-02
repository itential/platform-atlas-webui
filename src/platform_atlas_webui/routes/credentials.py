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
    """Return (tier, backend_type, env_name) from disk, merging env overlay.

    Both tier and backend follow the precedence the rest of Atlas uses — the
    active environment's overlay wins over root config, and ``ATLAS_TIER``
    overrides tier. Resolving the overlay tier matters: without it the page
    showed the root config's tier (e.g. "extended") even when a Standard
    environment was active.
    """
    import os
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
                if env_data.get("tier"):
                    tier = env_data["tier"]
        env_tier = os.environ.get("ATLAS_TIER")
        if env_tier and env_tier.strip().lower() in ("standard", "extended", "saas"):
            tier = env_tier.strip().lower()
    except Exception:
        pass
    return tier, backend_type, env_name


def _secret_store_info() -> dict:
    """Honest view of the chosen local secret-store substrate.

    Reflects the store the environment explicitly selected — the OS keyring, or
    the encrypted local file (``~/.atlas/credentials.enc``). The page mirrors how
    the CLI reports the file store: honestly, never dressed up as an "OS keyring."

    Returns ``{"is_file": bool, "label": str, "health": str | None}`` where
    ``health`` is ``"ok" | "empty" | "unreadable"`` for the file store, else
    ``None``.
    """
    info: dict = {"is_file": False, "label": "OS Keyring", "health": None, "locked": False}
    try:
        from platform_atlas.core.credentials import (
            KeyringSecretStore,
            active_secret_store,
            keyring_is_locked,
            FileSecretStore,
        )
        store = active_secret_store()
        info["label"] = store.display_name
        info["is_file"] = bool(getattr(store, "is_file", False))
        if isinstance(store, FileSecretStore):
            info["health"] = store.health().value
        elif isinstance(store, KeyringSecretStore):
            # A password-protected file keyring (e.g. the documented
            # keyrings.alt.file.EncryptedKeyring headless workaround) needs
            # its password before ANYTHING can be read from it — including
            # Vault's own connection settings, when Vault's local substrate
            # is the OS keyring. The WebUI has no TTY, so this can only be
            # unlocked through the explicit /unlock endpoint below; never
            # silently treated as "missing."
            info["locked"] = keyring_is_locked()
    except Exception:  # noqa: BLE001 — a display helper must never break the page
        pass
    return info


def _load_credential_status() -> dict:
    """
    Build the full credential status dict for the template.

    Returns a dict with:
      backend, env_name, tier, vault_url, vault_path, vault_config,
      connected, error, credentials (list of per-key dicts),
      store_is_file, store_label, store_health (the resolved substrate)
    """
    from platform_atlas.core.credentials import (
        CredentialKey,
        CredentialStore,
        CredentialBackendType,
        applicable_keys,
        required_keys,
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

    # Reflect the ACTUAL secret-store substrate (OS keyring vs. the encrypted
    # local-file fallback) so the page never claims "OS Keyring" when the
    # headless file-store fallback is what's really in use.
    _ss = _secret_store_info()
    result["store_is_file"] = _ss["is_file"]
    result["store_label"] = _ss["label"]
    result["store_health"] = _ss["health"]
    result["keyring_locked"] = _ss["locked"]

    # Ordered list of all credentials with their metadata
    _required_now = required_keys(tier)
    key_meta = [
        {
            "key": CredentialKey.PLATFORM_SECRET,
            "required": CredentialKey.PLATFORM_SECRET in _required_now,
            "tier_note": "all tiers — required (Platform OAuth)",
            "extended_only": False,
            "description": "OAuth client secret for authenticating with IAP.",
        },
        {
            "key": CredentialKey.GATEWAY4_PASSWORD,
            "required": False,
            "tier_note": "all tiers — optional",
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
            "tier_note": "Extended & SaaS — optional if key has no passphrase",
            "extended_only": True,
            "description": "Passphrase for the SSH private key used in SSH captures.",
        },
    ]

    if result["keyring_locked"]:
        # The OS keyring needs its password before ANYTHING can be read from
        # it — including Vault's own connection settings, when Vault's local
        # substrate is the keyring. Constructing CredentialStore below would
        # otherwise surface a confusing "Vault URL not found" (Layer B reads
        # through the same locked store). Report the honest state instead;
        # the unlock endpoint is what actually resolves this.
        for m in key_meta:
            unavailable = m["key"] not in applicable_keys(tier)
            result["credentials"].append({
                "key": m["key"].value,
                "display": m["key"].display_name,
                "present": None,
                "required": m["required"],
                "extended_only": m["extended_only"],
                "unavailable": unavailable,
                "tier_note": m["tier_note"],
                "description": m["description"],
            })
        return result

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

            # Keys outside the active tier's applicable set are inaccessible
            # (Mongo/Redis/SSH in Standard; Platform/Mongo/Redis in SaaS).
            unavailable = m["key"] not in applicable_keys(tier)

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
            unavailable = m["key"] not in applicable_keys(tier)
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


@router.post("/unlock")
async def unlock_keyring(password: str = Form(...)):
    """Unlock a password-protected file keyring (e.g. keyrings.alt.file.

    EncryptedKeyring) with a password submitted from the browser.

    The WebUI has no TTY, so it can never call ``getpass()`` — this is the
    only way it can supply the keyring's password. On success the backend
    stays unlocked in-process (same in-memory caching the library already
    does after a correct interactive password) until this server process
    restarts; every request after this one reuses it with no further prompt.
    """
    from urllib.parse import quote
    from platform_atlas.core.credentials import unlock_keyring as _unlock

    if not password:
        return RedirectResponse(
            url="/config/credentials?error=Enter+the+keyring+password",
            status_code=303,
        )

    if _unlock(password):
        return RedirectResponse(url="/config/credentials?saved=Keyring+unlocked", status_code=303)

    return RedirectResponse(
        url=f"/config/credentials?error={quote('Incorrect keyring password.')}",
        status_code=303,
    )


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


def _format_check(key: str, value: str) -> tuple[bool, str]:
    """Validate a credential's *shape* only — never opens a network connection.

    Returns ``(ok, message_html)``. The message is a tiny HTML fragment safe to
    swap into the row's ``.ce-check-result`` div. We deliberately never echo the
    secret itself: for URIs we surface host + query facts; for opaque secrets we
    surface only a character count.
    """
    from urllib.parse import urlsplit, parse_qs

    from markupsafe import escape as _esc

    v = (value or "").strip()
    if not v:
        return False, '<span class="muted">Nothing stored for this key.</span>'

    if key in ("mongo_uri", "redis_uri"):
        try:
            parts = urlsplit(v)
        except Exception:  # noqa: BLE001
            return False, '<span class="bad">✗ Not a parseable URI.</span>'
        expected_schemes = (
            ("mongodb", "mongodb+srv") if key == "mongo_uri" else ("redis", "rediss")
        )
        if parts.scheme not in expected_schemes:
            want = " or ".join(expected_schemes)
            return False, (
                f'<span class="bad">✗ Scheme <code>{_esc(parts.scheme) if parts.scheme else "—"}</code> '
                f'is not {want}.</span>'
            )
        if not parts.hostname:
            return False, '<span class="bad">✗ No host in the URI.</span>'
        bits = ['<span class="ok">✓ valid URI</span>',
                f'host <code>{_esc(parts.hostname)}</code>']
        if key == "mongo_uri":
            qs = parse_qs(parts.query)
            has_creds = bool(parts.username)
            if has_creds and "authSource" not in qs:
                return False, (
                    '<span class="bad">✗ credentials present but no '
                    '<code>authSource</code></span> — MongoDB may reject the '
                    'login. Append <code>?authSource=admin</code>.'
                )
            if "authSource" in qs:
                bits.append(f'authSource <code>{_esc(qs["authSource"][0])}</code>')
        return True, " · ".join(bits)

    # Opaque secrets — presence + a character count, never the value.
    return True, f'<span class="ok">✓ stored</span> <span class="muted">({len(v)} chars)</span>'


@router.post("/check-format", response_class=HTMLResponse)
async def check_credential_format(key: str = Form(...)):
    """Format/presence check of a *stored* credential. No network connection.

    Reads the value straight from the active backend and validates its shape,
    returning a small HTML fragment for an htmx swap. Purely local — this never
    connects to MongoDB, Redis, Vault targets, or the Platform.
    """
    from platform_atlas.core.credentials import CredentialKey

    valid_keys = {ck.value: ck for ck in CredentialKey}
    if key not in valid_keys:
        return HTMLResponse('<span class="bad">✗ Unknown credential key.</span>')
    try:
        store = _make_fresh_store()
        value = store._backend.get(key)  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001
        logger.warning("Credential format-check failed for %s: %s", key, exc)
        return HTMLResponse('<span class="muted">Could not read the stored value.</span>')

    _ok, msg = _format_check(key, value or "")
    return HTMLResponse(msg)


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
