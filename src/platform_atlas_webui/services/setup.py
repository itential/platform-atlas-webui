"""
First-run bootstrap for the WebUI.

Mirrors the CLI's ``init_setup.start_setup_process`` but expressed as
small synchronous helpers the FastAPI routes can call directly:

    * ``is_initialized()`` — checked on every request
    * ``required_keys_for_tier(tier, has_gateway4, has_iag5)``
        — the canonical list of credential keys that need to live in
        the chosen backend for the given tier and topology
    * ``verify_vault_connection(...)``
        — authenticates against Vault and returns a per-key existence
        report. Used both by the AJAX "Test connection" button and by
        the final form submit as defense in depth
    * ``bootstrap(...)``
        — writes ``~/.atlas`` config + first environment + persists
        either the platform_client_secret (keyring backend) or the
        Vault connection settings (vault backend)

The credential write is best-effort — keyring availability is
environment-dependent. We surface a clear error rather than silently
losing the secret.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from platform_atlas_webui.security.redact import redact

from platform_atlas.core.paths import (
    ATLAS_HOME,
    ATLAS_CONFIG_FILE,
    ATLAS_ENVIRONMENTS_DIR,
    ATLAS_HOME_SESSIONS,
)
from platform_atlas.core.utils import atomic_write_json

logger = logging.getLogger(__name__)


def is_initialized() -> bool:
    """True iff Atlas has a config file we can load."""
    return ATLAS_CONFIG_FILE.is_file()


# ─────────────────────────────────────────────────────────────────────
# Tier-aware required-key map
# ─────────────────────────────────────────────────────────────────────

def required_keys_for_tier(
    tier: str,
    *,
    has_gateway4: bool = False,
    has_iag5: bool = False,
) -> list[dict[str, Any]]:
    """Return the credential keys expected to exist in the active backend.

    Each entry is ``{"key", "label", "required", "tier"}``. The Vault
    "Test connection" check uses this to report which keys are present
    and which still need to be put into Vault by the operator.

    The list mirrors what the CLI wizard checks at
    ``init_setup.py:1517-1556``:

    * Standard tier always needs ``platform_client_secret``. Gateway4
      password is optional and only needed when the env has a
      ``gateway4_uri``.
    * Extended tier adds the Extended-only keys (mongo/redis/SSH).
      Those are deployment-dependent — we list them as optional so
      the operator can fill them in once topology is wired up.
    """
    from platform_atlas.core.credentials import CredentialKey

    items: list[dict[str, Any]] = []
    # Every tier now anchors on the Platform, including SaaS (Platform OAuth is
    # required; SaaS just uses it for a limited adapter/application check set).
    items.append({
        "key": CredentialKey.PLATFORM_SECRET.value,
        "label": CredentialKey.PLATFORM_SECRET.display_name,
        "required": True,
        "tier": "both",
    })
    if has_gateway4:
        items.append({
            "key": CredentialKey.GATEWAY4_PASSWORD.value,
            "label": CredentialKey.GATEWAY4_PASSWORD.display_name,
            "required": False,
            "tier": "both",
        })
    if (tier or "").lower() == "extended":
        if has_iag5:
            items.append({
                "key": "iag5_password",
                "label": "Gateway 5 Password",
                "required": False,
                "tier": "extended",
            })
        items.extend([
            {
                "key": CredentialKey.MONGO_URI.value,
                "label": CredentialKey.MONGO_URI.display_name,
                "required": False,
                "tier": "extended",
            },
            {
                "key": CredentialKey.REDIS_URI.value,
                "label": CredentialKey.REDIS_URI.display_name,
                "required": False,
                "tier": "extended",
            },
            {
                "key": CredentialKey.SSH_PASSPHRASE.value,
                "label": CredentialKey.SSH_PASSPHRASE.display_name,
                "required": False,
                "tier": "extended",
            },
        ])
    return items


# ─────────────────────────────────────────────────────────────────────
# Vault connection check
# ─────────────────────────────────────────────────────────────────────

def _build_vault_config(payload: dict[str, Any]):
    """Translate a flat form dict into a VaultConfig instance."""
    from platform_atlas.core.credentials import VaultAuthMethod, VaultConfig

    auth_str = (payload.get("auth_method") or "token").strip()
    try:
        auth_method = VaultAuthMethod(auth_str)
    except ValueError:
        raise ValueError(
            f"Unsupported Vault auth method '{auth_str}'. "
            "Pick one of: token, approle, approle_wrapped, token_file, token_env."
        )

    url = (payload.get("url") or "").strip()
    if not url:
        raise ValueError("Vault URL is required")

    return VaultConfig(
        url=url,
        auth_method=auth_method,
        token=(payload.get("token") or "").strip() or None,
        role_id=(payload.get("role_id") or "").strip() or None,
        secret_id=(payload.get("secret_id") or "").strip() or None,
        wrapping_token=(payload.get("wrapping_token") or "").strip() or None,
        token_file_path=(payload.get("token_file_path") or "").strip() or None,
        mount_point=(payload.get("mount_point") or "secret").strip() or "secret",
        secret_path=(payload.get("secret_path") or "platform-atlas").strip() or "platform-atlas",
        verify_ssl=bool(payload.get("verify_ssl", False)),
        namespace=(payload.get("namespace") or "").strip() or None,
    )


def verify_vault_connection(
    *,
    vault_payload: dict[str, Any],
    tier: str,
    env_name: str,
    has_gateway4: bool,
    has_iag5: bool,
) -> dict[str, Any]:
    """Connect to Vault and report which required keys exist.

    Returns a JSON-serializable dict:
        {
          "ok": bool,             # auth succeeded
          "url": str,
          "mount": str,
          "path": str,
          "message": str,         # short human-readable status
          "keys": [{"key", "label", "required", "exists"}, ...],
          "missing_required": [<key>, ...],
        }

    Never raises — every failure path is captured into ``ok=false`` so
    the AJAX caller can render it inline.
    """
    from platform_atlas.core.credentials import (
        CredentialError,
        VaultBackend,
        scoped_service_name,
    )

    try:
        cfg = _build_vault_config(vault_payload)
    except ValueError as exc:
        return {
            "ok": False,
            "url": vault_payload.get("url") or "",
            "mount": vault_payload.get("mount_point") or "secret",
            "path": vault_payload.get("secret_path") or "platform-atlas",
            "message": str(exc),
            "keys": [],
            "missing_required": [],
        }

    expected = required_keys_for_tier(
        tier,
        has_gateway4=has_gateway4,
        has_iag5=has_iag5,
    )

    service = scoped_service_name(env_name) if env_name else scoped_service_name(None)

    try:
        backend = VaultBackend(cfg, service=service)
    except CredentialError as exc:
        return {
            "ok": False,
            "url": cfg.url,
            "mount": cfg.mount_point,
            "path": cfg.secret_path,
            "message": f"Vault connection failed: {exc}",
            "keys": [{**k, "exists": False} for k in expected],
            "missing_required": [k["key"] for k in expected if k["required"]],
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected Vault connection error (payload keys: %s)", list(redact(vault_payload)))
        return {
            "ok": False,
            "url": cfg.url,
            "mount": cfg.mount_point,
            "path": cfg.secret_path,
            "message": f"{type(exc).__name__}: {exc}",
            "keys": [{**k, "exists": False} for k in expected],
            "missing_required": [k["key"] for k in expected if k["required"]],
        }

    key_results: list[dict[str, Any]] = []
    missing_required: list[str] = []
    for item in expected:
        try:
            present = backend.exists(item["key"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("Vault exists() probe for %s failed: %s", item["key"], exc)
            present = False
        key_results.append({**item, "exists": present})
        if item["required"] and not present:
            missing_required.append(item["key"])

    if missing_required:
        message = (
            f"Connected to Vault at {cfg.url}, but {len(missing_required)} "
            "required secret(s) are missing — see the list below."
        )
    else:
        message = f"Connected to Vault at {cfg.url}. All required secrets present."

    return {
        "ok": True,
        "url": cfg.url,
        "mount": cfg.mount_point,
        "path": cfg.secret_path,
        "message": message,
        "keys": key_results,
        "missing_required": missing_required,
        "token_ttl": backend.token_ttl,
        "token_renewable": backend.token_renewable,
    }


# ─────────────────────────────────────────────────────────────────────
# Bootstrap
# ─────────────────────────────────────────────────────────────────────

# 3.0 curated theme set — Modern (default), Itential, Obsidian, Carbon.
_VALID_WEBUI_THEMES = {"modern", "itential", "obsidian", "carbon"}
_VALID_WEBUI_MODES = {"light", "dark"}


def bootstrap(
    *,
    organization_name: str,
    tier: str,
    env_name: str,
    platform_uri: str,
    platform_client_id: str,
    platform_client_secret: str = "",
    gateway4_uri: str = "",
    gateway4_username: str = "",
    gateway4_password: str = "",
    saas_gateway_kind: str = "",
    saas_gw4_ssh: bool = False,
    saas_iag_host: str = "",
    saas_ssh_user: str = "",
    saas_ssh_port: str = "",
    saas_ssh_key: str = "",
    saas_gw5_source: str = "",
    saas_gw5_source_path: str = "",
    saas_gw5_conf_path: str = "",
    verify_ssl: bool = False,
    credential_backend: str = "keyring",
    vault_secret_store: str = "keyring",
    vault_payload: dict[str, Any] | None = None,
    webui_theme: str = "",
    webui_mode: str = "",
) -> dict[str, Any]:
    """Create ~/.atlas, write config + environment, persist credentials.

    With ``credential_backend == "keyring"``:
        Stores ``platform_client_secret`` (and optional gateway4
        password) in the OS keyring scoped to the env.

    With ``credential_backend == "vault"``:
        Stores the Vault *connection* settings in the OS keyring under
        ``vault_*`` keys (still env-scoped). The actual platform secret
        is NOT written here — Vault is read-only from Atlas. We
        re-verify the Vault connection server-side before writing
        anything to disk; if it fails, no config is written.

    Returns a dict summarizing what was written so the caller can show
    a confirmation. Raises ``ValueError`` on validation failures and
    surfaces credential-store errors with their original messages.
    """
    backend_choice = (credential_backend or "keyring").strip().lower()
    if backend_choice not in ("keyring", "file", "vault"):
        raise ValueError(
            f"Unsupported credential backend '{credential_backend}'. "
            "Pick one of: keyring, file, vault."
        )
    # Vault's own connection settings live in a local store (keyring or file).
    vault_store_choice = (vault_secret_store or "file").strip().lower()
    if vault_store_choice not in ("keyring", "file"):
        vault_store_choice = "file"
    if tier not in ("standard", "extended", "saas"):
        raise ValueError(f"Invalid tier '{tier}' (must be 'standard', 'extended', or 'saas')")
    is_saas = tier == "saas"
    saas_kind = (saas_gateway_kind or "").strip().lower()
    if not organization_name.strip():
        raise ValueError("Organization name is required")
    if not env_name.strip():
        raise ValueError("Environment name is required")
    if any(c in env_name for c in "/\\\x00"):
        raise ValueError(
            f"Environment name '{env_name}' contains characters that aren't safe "
            "to use as a file name (slashes or null bytes)."
        )
    if is_saas:
        # SaaS is Platform-anchored now: Platform OAuth is required and a gateway
        # is optional (Gateway 4, Gateway 5, both, or Platform-only). The gateway
        # SSH/source specifics are validated by _build_saas_topology below.
        if saas_kind and saas_kind not in ("gateway4", "gateway5", "gw4-gw5"):
            raise ValueError(
                "Invalid SaaS gateway choice — pick Gateway 4, Gateway 5, both, or Platform-only.")
        if saas_kind in ("gateway4", "gw4-gw5") and not gateway4_uri.strip():
            raise ValueError("A SaaS Gateway 4 environment needs the Gateway 4 API URL.")
    # Platform OAuth is the anchor for every tier, SaaS included.
    if not platform_uri.strip():
        raise ValueError("Platform URI is required")
    if not platform_client_id.strip():
        raise ValueError("Platform OAuth client ID is required")

    if backend_choice in ("keyring", "file"):
        if not platform_client_secret:
            raise ValueError(
                "Platform OAuth client secret is required for the "
                f"{'encrypted file' if backend_choice == 'file' else 'OS keyring'} backend")
    else:
        if not vault_payload or not (vault_payload.get("url") or "").strip():
            raise ValueError("Vault URL is required when the Vault backend is selected")

    # ── Pre-flight Vault check ───────────────────────────────────────
    # We re-verify here so a determined client that bypasses the AJAX
    # check still gets caught before we touch the filesystem.
    vault_verify: dict[str, Any] | None = None
    if backend_choice == "vault":
        vault_verify = verify_vault_connection(
            vault_payload=vault_payload or {},
            tier=tier,
            env_name=env_name.strip(),
            has_gateway4=bool(gateway4_uri.strip()) or saas_kind in ("gateway4", "gw4-gw5"),
            has_iag5=False,  # IAG5 URI lives on the env edit page, not the welcome form yet
        )
        if not vault_verify["ok"]:
            raise ValueError(
                f"Cannot complete setup — Vault connection failed: {vault_verify['message']}"
            )

    # ── 1. Filesystem layout ─────────────────────────────────────────
    ATLAS_HOME.mkdir(mode=0o700, exist_ok=True)
    ATLAS_ENVIRONMENTS_DIR.mkdir(mode=0o700, exist_ok=True)
    ATLAS_HOME_SESSIONS.mkdir(mode=0o700, exist_ok=True)
    try:
        os.chmod(ATLAS_HOME, 0o700)
    except OSError:
        pass

    # ── 2. config.json ───────────────────────────────────────────────
    config_payload: dict[str, Any] = {
        "organization_name": organization_name.strip(),
        "verify_ssl": verify_ssl,
        "dark_mode": True,
        "theme": "horizon-dark",
        "extended_validation_checks": True,
        "debug": False,
        # The chosen tier becomes the GLOBAL default for future environments —
        # including SaaS: a SaaS install audits gateways ~all the time, so new
        # envs should default to SaaS too. The env create form still offers
        # Standard/Extended for the exceptions.
        "tier": tier,
        "active_environment": env_name.strip(),
    }
    chosen_theme = (webui_theme or "").strip().lower()
    chosen_mode = (webui_mode or "").strip().lower()
    if chosen_theme in _VALID_WEBUI_THEMES:
        config_payload["webui_theme"] = chosen_theme
    if chosen_mode in _VALID_WEBUI_MODES:
        config_payload["webui_mode"] = chosen_mode
    atomic_write_json(ATLAS_CONFIG_FILE, config_payload)

    # ── 3. Environment overlay ───────────────────────────────────────
    from platform_atlas.core.environment import Environment, get_environment_manager
    env_data: dict[str, Any] = {
        "name": env_name.strip(),
        "description": "Created via WebUI first-run setup",
        "organization_name": organization_name.strip(),
        "platform_uri": platform_uri.strip(),
        "platform_client_id": platform_client_id.strip(),
        "credential_backend": backend_choice,
        "tier": tier,
    }
    if backend_choice == "vault":
        env_data["vault_secret_store"] = vault_store_choice
    if gateway4_uri.strip():
        env_data["gateway4_uri"] = gateway4_uri.strip()
    if gateway4_username.strip():
        env_data["gateway4_username"] = gateway4_username.strip()
    if is_saas:
        # SaaS is Platform-anchored — the Platform fields stay in the overlay.
        # The gateway is optional; saas_gateway_kind is "" for a Platform-only
        # audit (no gateway, no deployment topology).
        env_data["saas_gateway_kind"] = saas_kind
        if saas_kind in ("gateway4", "gateway5", "gw4-gw5"):
            # Gateway SSH only ever targets the gateway server(s). GW4 is always
            # audited over SSH in addition to its REST API; GW5 over SSH or a
            # local Compose/Helm file.
            from platform_atlas_webui.services.environments import _build_saas_topology
            saas_topology = _build_saas_topology({
                "saas_gateway_kind": saas_kind,
                "saas_gw4_ssh": "1" if saas_gw4_ssh else "",
                "iag_host": saas_iag_host,
                "ssh_user": saas_ssh_user,
                "ssh_port": saas_ssh_port,
                "ssh_key": saas_ssh_key,
                "gateway5_source": saas_gw5_source,
                "gateway5_source_path": saas_gw5_source_path,
                "gateway5_conf_path": saas_gw5_conf_path,
                # The first-run wizard collects a single gateway SSH host, so for
                # a GW4+GW5 pair the GW5 server reuses it. Separate hosts can be
                # set later on the environment edit page.
                "saas_gw5_same_host": "1",
            })
            if saas_topology is not None:
                env_data["deployment"] = saas_topology
            if saas_ssh_key.strip():
                env_data["ssh_key"] = saas_ssh_key.strip()

    # Extended-tier topology (deployment / SSH / Mongo / Redis / IAG5)
    # is owned by /environments/{name}/edit. The wizard hands Extended
    # users off there from /setup/done — no inline topology capture
    # here. Same for Extended-only credentials (set via /config/credentials
    # or the env detail page's "Set up credentials" CTA).

    env_mgr = get_environment_manager()
    environment = Environment.from_dict(env_data)
    env_mgr.save(environment)
    env_mgr.set_active(environment.name)

    # ── 4. Credential storage ────────────────────────────────────────
    cred_summary: dict[str, Any] = {"backend": backend_choice}

    if backend_choice in ("keyring", "file"):
        try:
            from platform_atlas.core.credentials import (
                CredentialKey,
                FileSecretStore,
                KeyringSecretStore,
                scoped_service_name,
            )
            # The new env isn't the active config yet, so write straight to the
            # chosen substrate rather than via active_secret_store() (which reads
            # the current config). No auto-anything.
            substrate = FileSecretStore() if backend_choice == "file" else KeyringSecretStore()
            svc = scoped_service_name(environment.name)
            # Platform secret is stored for every tier now — SaaS included.
            substrate.set(svc, CredentialKey.PLATFORM_SECRET.value, platform_client_secret)
            cred_summary["platform_secret"] = "stored"
            if gateway4_password:
                substrate.set(svc, CredentialKey.GATEWAY4_PASSWORD.value, gateway4_password)
                cred_summary["gateway4_password"] = "stored"
            # Extended-tier credentials (Mongo URI, Redis URI, SSH passphrase,
            # IAG5 password) are set via /config/credentials after the user
            # finishes topology in the env editor — keeps the wizard tight.
        except Exception as exc:  # noqa: BLE001 — surface to user
            logger.exception("Credential store write failed during setup (backend=%s)", backend_choice)
            cred_summary["error"] = f"{type(exc).__name__}: {exc}"
    else:
        # Vault: persist the connection settings into the chosen local store
        # (keyring or encrypted file). The platform secret lives in Vault.
        try:
            from platform_atlas.core.credentials import (
                VaultBackend,
                FileSecretStore,
                KeyringSecretStore,
                scoped_service_name,
            )
            cfg = _build_vault_config(vault_payload or {})
            boot_store = FileSecretStore() if vault_store_choice == "file" else KeyringSecretStore()
            VaultBackend.save_config_to_keyring(
                cfg,
                service=scoped_service_name(environment.name),
                store=boot_store,
            )
            cred_summary["vault_config"] = "stored"
            cred_summary["vault_url"] = cfg.url
            cred_summary["vault_secret_store"] = vault_store_choice
            if vault_verify is not None:
                cred_summary["vault_keys"] = vault_verify.get("keys", [])
                cred_summary["vault_missing_required"] = vault_verify.get(
                    "missing_required", []
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Vault config persist failed during setup (url=%s)", redact(vault_payload or {}).get("url", ""))
            cred_summary["error"] = f"{type(exc).__name__}: {exc}"

    # ── 5. Re-initialize Atlas context ───────────────────────────────
    try:
        from platform_atlas.core.init_env import sync_bundled_files
        from platform_atlas.core.context import init_context
        sync_bundled_files()
        init_context()
        cred_summary["context"] = "initialized"
    except Exception as exc:  # noqa: BLE001
        logger.exception("Context init failed after setup")
        cred_summary["context_error"] = f"{type(exc).__name__}: {exc}"

    return {
        "config_file": str(ATLAS_CONFIG_FILE),
        "environment": environment.name,
        "tier": tier,
        "credential_backend": backend_choice,
        "credentials": cred_summary,
    }
