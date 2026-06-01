"""Config service — read and update fields in ~/.atlas/config.json."""

from __future__ import annotations

import json
import threading
import time as _time
from typing import Any

from platform_atlas.core.paths import ATLAS_CONFIG_FILE
from platform_atlas.core.utils import atomic_write_json

# Serialize read-modify-write of config.json across concurrent requests.
# atomic_write_json is FS-atomic, but two parallel updates would each read
# the pre-write state and the second writer would clobber the first.
# threading.Lock works for both sync def (threadpool) and async def callers.
_CONFIG_LOCK = threading.Lock()

# Short-lived in-process cache for resolve_active_tier() — covers burst reads
# within a single page-load cycle (list_sessions + get_session both call it).
_tier_cache_value: str | None = None
_tier_cache_ts: float = 0.0
_TIER_CACHE_TTL = 1.0  # seconds


def _invalidate_tier_cache() -> None:
    global _tier_cache_ts
    _tier_cache_ts = 0.0


# Fields the WebUI will let the user edit. The CLI's `config set` allows
# many more, but exposing every one to a form would be visual clutter.
EDITABLE_FIELDS: tuple[str, ...] = (
    "organization_name",
    "platform_uri",
    "platform_client_id",
    "credential_backend",
    "verify_ssl",
    "dark_mode",
    "theme",
    "extended_validation_checks",
    "debug",
    "webui_theme",
    "webui_mode",
    "webui_accent",  # legacy — accepted on write so existing forms don't 4xx
    "webui_upgrade_panel_dismissed",
    "webui_palette_enabled",
    "tier",
    "active_environment",
    "active_ruleset",
    "active_profile",
    "gateway4_uri",
    "gateway4_username",
    "manual_input_mode",
)


# Theme identities (palette). Light/dark is a separate axis (the mode).
_VALID_WEBUI_THEMES = {"aurora", "horizon", "obsidian", "meadow", "carbon", "itential", "dracula"}
# "auto" follows OS prefers-color-scheme; resolved to light/dark on the client.
_VALID_WEBUI_MODES = {"light", "dark", "auto"}

# Mapping from the legacy 1.7.0 accent picker → new theme identity. Cyan/violet/mono
# read as cool/technical → Aurora; amber/lime read as warm → Horizon.
_LEGACY_ACCENT_TO_THEME = {
    "cyan": "aurora",
    "violet": "aurora",
    "mono": "aurora",
    "amber": "horizon",
    "lime": "horizon",
}


def resolve_appearance(cfg: dict[str, Any]) -> tuple[str, str]:
    """Return ``(theme, mode)`` for the WebUI, applying legacy migrations.

    Old values that may live in ``config.json`` from 1.7.0:
      * ``webui_theme = "light" | "dark"``  → that string is actually the *mode*
      * ``webui_accent = "cyan|amber|violet|lime|mono"`` → maps to a new theme

    New values:
      * ``webui_theme = "aurora" | "horizon"``
      * ``webui_mode  = "light"  | "dark"``
    """
    raw_theme = (cfg.get("webui_theme") or "").strip().lower()
    raw_mode = (cfg.get("webui_mode") or "").strip().lower()
    raw_accent = (cfg.get("webui_accent") or "").strip().lower()

    # Mode resolution.
    if raw_mode in _VALID_WEBUI_MODES:
        mode = raw_mode
    elif raw_theme in _VALID_WEBUI_MODES:
        # Legacy: webui_theme used to hold the mode.
        mode = raw_theme
    else:
        mode = "dark"

    # Theme resolution.
    if raw_theme in _VALID_WEBUI_THEMES:
        theme = raw_theme
    elif raw_accent in _LEGACY_ACCENT_TO_THEME:
        theme = _LEGACY_ACCENT_TO_THEME[raw_accent]
    else:
        theme = "itential"

    return theme, mode


def read_config() -> dict[str, Any]:
    if not ATLAS_CONFIG_FILE.is_file():
        return {}
    try:
        return json.loads(ATLAS_CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def resolve_active_tier(default: str = "extended") -> str:
    """Resolve the persisted tier the way ``core/config.load_config()`` does.

    Order: ATLAS_TIER env var > active environment overlay > root config > default.
    Returns the literal tier string ("standard"/"extended"); does not validate.
    Read-only — never writes.

    Result is cached for _TIER_CACHE_TTL seconds to avoid re-reading config.json
    on every route handler that calls list_sessions() and get_session() in the same
    request. _invalidate_tier_cache() is called by update_config() on every write.
    """
    global _tier_cache_value, _tier_cache_ts
    now = _time.monotonic()
    if _tier_cache_value is not None and now - _tier_cache_ts < _TIER_CACHE_TTL:
        return _tier_cache_value

    import os as _os
    env_tier = _os.environ.get("ATLAS_TIER", "").strip().lower()
    if env_tier in ("standard", "extended"):
        _tier_cache_value = env_tier
        _tier_cache_ts = now
        return env_tier
    cfg = read_config()
    tier = cfg.get("tier") or default
    env_name = cfg.get("active_environment") or ""
    if env_name:
        try:
            from platform_atlas.core.paths import ATLAS_ENVIRONMENTS_DIR
            env_file = ATLAS_ENVIRONMENTS_DIR / f"{env_name}.json"
            if env_file.is_file():
                env_data = json.loads(env_file.read_text(encoding="utf-8"))
                if env_data.get("tier"):
                    tier = env_data["tier"]
        except Exception:
            pass
    _tier_cache_value = tier
    _tier_cache_ts = now
    return tier


def mirror_tier_to_active_overlay(new_tier: str) -> None:
    """If an active environment is set, write ``new_tier`` into its overlay file.

    Why: the env overlay's ``tier`` wins over root config in load_config(), so
    any place we update root-config tier without also updating the overlay
    silently undoes the change. Best-effort — the caller has already written
    the root config and the overlay write must not raise to the request handler.
    """
    cfg = read_config()
    env_name = cfg.get("active_environment") or ""
    if not env_name:
        return
    try:
        from platform_atlas.core.paths import ATLAS_ENVIRONMENTS_DIR
        env_file = ATLAS_ENVIRONMENTS_DIR / f"{env_name}.json"
        if not env_file.is_file():
            return
        env_data = json.loads(env_file.read_text(encoding="utf-8"))
        env_data["tier"] = new_tier
        atomic_write_json(env_file, env_data)
        _invalidate_tier_cache()
    except Exception:
        pass


def update_config(updates: dict[str, Any]) -> dict[str, Any]:
    """Apply `updates` to ~/.atlas/config.json atomically.

    Only EDITABLE_FIELDS are accepted — anything else is silently dropped
    so a malformed form post can't corrupt unrelated settings.
    """
    with _CONFIG_LOCK:
        data = read_config()
        for key, raw in updates.items():
            if key not in EDITABLE_FIELDS:
                continue
            data[key] = _coerce(key, raw)
        atomic_write_json(ATLAS_CONFIG_FILE, data)
        _invalidate_tier_cache()
        return data


def _coerce(key: str, raw: Any) -> Any:
    """Coerce form values (strings) to the right type for known boolean fields."""
    bool_keys = {
        "verify_ssl", "dark_mode",
        "extended_validation_checks", "debug",
        "webui_palette_enabled",
    }
    if key in bool_keys:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return raw.strip().lower() in ("1", "true", "yes", "on")
        return bool(raw)
    if isinstance(raw, str) and raw.strip() == "":
        # Empty form fields → drop string "None" markers and let downstream
        # treat them as unset rather than literal empty strings.
        return ""
    return raw


# ── Pinned sessions ──────────────────────────────────────────────────────
# Stored in ``~/.atlas/config.json`` under the key ``pinned_sessions`` as a
# list of session names. Per-install (not per-browser) so the pin sticks
# across browsers and machines that share the same Atlas home directory.
# Bypasses the EDITABLE_FIELDS / _coerce flow because the value is a list,
# not a stringly-typed form field.

def get_pinned_session_names() -> list[str]:
    """Return the current pin list (always a list of strings, possibly empty)."""
    cfg = read_config()
    raw = cfg.get("pinned_sessions") or []
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if isinstance(item, (str, int))]


def toggle_pinned_session(name: str) -> bool:
    """Flip the pin state of ``name``. Returns the new pinned state."""
    if not name:
        return False
    with _CONFIG_LOCK:
        cfg = read_config()
        pinned = cfg.get("pinned_sessions") or []
        if not isinstance(pinned, list):
            pinned = []
        # Normalize to strings, dedupe, preserve insertion order.
        pinned = [str(p) for p in pinned if isinstance(p, (str, int))]
        if name in pinned:
            pinned.remove(name)
            new_state = False
        else:
            pinned.append(name)
            new_state = True
        cfg["pinned_sessions"] = pinned
        atomic_write_json(ATLAS_CONFIG_FILE, cfg)
        return new_state
