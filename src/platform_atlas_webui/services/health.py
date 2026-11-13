"""System health — a read-only snapshot of Atlas itself, not of the audited deployment.

``/config/doctor`` answers "is my *configuration* sound?" (file permissions,
reachability probes, SSH key paths). This module answers "is the *application*
healthy right now?": credential backend, TLS certificate, local storage,
version alignment, and the scheduler. It feeds the System page and the global
degraded-mode banner.

Every function here is defensive: a health check must never raise, because it
runs on the pages people open precisely when something is wrong.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Identifies this server process. The browser compares it across polls, so a
# restart (which drops in-memory jobs) can be reported instead of looking like
# a silent hang.
BOOT_ID = uuid.uuid4().hex[:12]

# Storage scan bounds: this runs on a web request, so it must stay cheap even
# when ~/.atlas holds thousands of files.
_SCAN_MAX_ENTRIES = 50_000
_SCAN_MAX_SECONDS = 1.5
_STORAGE_WARN_BYTES = 2 * 1024**3
_STORAGE_TTL_S = 60.0
_TLS_WARN_DAYS = 30

_storage_cache: tuple[float, "HealthCheck"] | None = None


@dataclass
class HealthCheck:
    id: str
    label: str
    state: str          # ok | warn | bad | off
    value: str          # short headline, e.g. "Vault · connected"
    detail: str = ""    # one line of context
    hint: str = ""      # what to do next, when state != ok
    code: str = ""      # machine-readable reason (credential errors)

    def to_dict(self) -> dict:
        return asdict(self)


def _atlas_home() -> Path:
    from platform_atlas.core.paths import ATLAS_HOME
    return Path(ATLAS_HOME)


# ── Credential store ───────────────────────────────────────────────────────

def credential_status_dict(*, force: bool = False) -> dict:
    """Credential backend status as a plain dict. Never raises.

    Falls back to "assume ok" if the installed CLI predates ``credential_status``
    so a version skew degrades the feature instead of breaking the page.
    """
    try:
        from platform_atlas.core.credentials import credential_status
    except ImportError:
        return {"ok": True, "backend": "unknown", "code": "ok", "message": "", "hint": "",
                "checked_at": time.time(), "cached": False, "supported": False}
    try:
        st = credential_status(force=force)
    except Exception as exc:  # noqa: BLE001 — a health probe must not raise
        logger.debug("credential_status failed: %s", exc)
        return {"ok": False, "backend": "unknown", "code": "credential_error", "message": str(exc),
                "hint": "", "checked_at": time.time(), "cached": False, "supported": True}
    return {
        "ok": st.ok, "backend": st.backend, "code": st.code, "message": st.message, "hint": st.hint,
        "checked_at": st.checked_at, "cached": st.cached, "supported": True,
    }


def _age(ts: float) -> str:
    secs = max(0, int(time.time() - ts))
    if secs < 5:
        return "just now"
    if secs < 90:
        return f"{secs}s ago"
    return f"{secs // 60} min ago"


def credential_check(*, force: bool = False) -> HealthCheck:
    s = credential_status_dict(force=force)
    backend = (s["backend"] or "unknown").capitalize()
    if s["ok"]:
        return HealthCheck(
            "credentials", "Credential store", "ok", f"{backend} · available",
            detail=f"Checked {_age(s['checked_at'])}",
        )
    hint = s["hint"]
    if hint and hint in s["message"]:
        hint = ""          # the message already says it; don't print the same sentence twice
    elif not hint:
        hint = "Check that the backend is running, then retry."
    return HealthCheck(
        "credentials", "Credential store", "bad", f"{backend} · unavailable",
        detail=s["message"], hint=hint, code=s["code"],
    )


# ── TLS certificate ────────────────────────────────────────────────────────

def tls_check() -> HealthCheck:
    try:
        from platform_atlas_webui.security import tls
        if not tls.CERT_FILE.exists():
            return HealthCheck("tls", "TLS certificate", "off", "No certificate",
                               detail="The server is not using the generated certificate.")
        expiry = tls._cert_expiry()  # noqa: SLF001 — the module's own reader; avoids re-parsing PEM here
        if expiry is None:
            return HealthCheck("tls", "TLS certificate", "warn", "Unreadable",
                               detail=str(tls.CERT_FILE), hint="Restart the WebUI to regenerate it.")
        from datetime import datetime, timezone
        days = (expiry - datetime.now(timezone.utc)).days
        if days < 0:
            return HealthCheck("tls", "TLS certificate", "bad", "Expired",
                               detail=f"Expired {-days} days ago",
                               hint="Restart the WebUI to rotate the certificate.")
        state = "warn" if days < _TLS_WARN_DAYS else "ok"
        return HealthCheck("tls", "TLS certificate", state, f"{days} days left",
                           detail="Self-signed, rotated automatically on startup",
                           hint="Restart the WebUI to rotate it." if state == "warn" else "")
    except Exception as exc:  # noqa: BLE001
        logger.debug("tls_check failed: %s", exc)
        return HealthCheck("tls", "TLS certificate", "off", "Unknown", detail=str(exc))


# ── Local storage ──────────────────────────────────────────────────────────

def _human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _scan_size(root: Path) -> tuple[int, bool]:
    """Total bytes under ``root`` and whether the scan was cut short."""
    total, seen, stack = 0, 0, [str(root)]
    deadline = time.monotonic() + _SCAN_MAX_SECONDS
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for entry in it:
                    seen += 1
                    if seen > _SCAN_MAX_ENTRIES or time.monotonic() > deadline:
                        return total, True
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total, False


def storage_check(*, force: bool = False) -> HealthCheck:
    global _storage_cache
    if not force and _storage_cache and time.monotonic() - _storage_cache[0] < _STORAGE_TTL_S:
        return _storage_cache[1]
    try:
        home = _atlas_home()
        size, partial = _scan_size(home)
        sessions_dir = home / "sessions"
        n_sessions = sum(1 for p in sessions_dir.iterdir() if p.is_dir()) if sessions_dir.is_dir() else 0
        state = "warn" if size >= _STORAGE_WARN_BYTES else "ok"
        check = HealthCheck(
            "storage", "Local storage", state,
            f"{'≥ ' if partial else ''}{_human_bytes(size)}",
            detail=f"{n_sessions} session{'' if n_sessions == 1 else 's'} in ~/.atlas",
            hint="Delete sessions you no longer need from the Sessions page." if state == "warn" else "",
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("storage_check failed: %s", exc)
        check = HealthCheck("storage", "Local storage", "off", "Unknown", detail=str(exc))
    _storage_cache = (time.monotonic(), check)
    return check


# ── Versions ───────────────────────────────────────────────────────────────

def _minor(v: str) -> tuple[str, str]:
    parts = (v or "").split(".")
    return (parts[0] if parts else "", parts[1] if len(parts) > 1 else "")


def version_check() -> HealthCheck:
    try:
        from platform_atlas.core._version import __version__ as cli_v
        from platform_atlas_webui import __version__ as web_v
        aligned = _minor(cli_v) == _minor(web_v)
        return HealthCheck(
            "versions", "Versions", "ok" if aligned else "warn",
            f"CLI {cli_v} · WebUI {web_v}",
            detail="In sync" if aligned else "Major or minor versions differ",
            hint="" if aligned else "Install matching platform-atlas and platform-atlas-webui releases.",
        )
    except Exception as exc:  # noqa: BLE001
        return HealthCheck("versions", "Versions", "off", "Unknown", detail=str(exc))


# ── Scheduler ──────────────────────────────────────────────────────────────

def scheduler_check(active_env: str | None) -> HealthCheck:
    try:
        from platform_atlas_webui.services.continuous import get_scheduler, topbar_summary
        running = get_scheduler().is_running
        summary = topbar_summary(active_env)
        if not summary.get("enabled"):
            return HealthCheck("scheduler", "Scheduled audits", "off", "Not enabled",
                               detail=f"Off for {active_env}" if active_env else "No active environment")
        state_map = {"ON": "ok", "PENDING": "ok", "STALE": "warn", "FAILING": "bad"}
        state = state_map.get(summary.get("state", ""), "warn")
        if not running:
            state = "warn"
        headline = f"Next run {summary['next_in']}" if summary.get("next_in") else summary.get("state", "")
        return HealthCheck(
            "scheduler", "Scheduled audits", state, headline,
            detail=f"Last run {summary['last_age']}" if summary.get("last_age") else "Has not run yet",
            hint="Open Continuous Audit to see why runs are late or failing." if state != "ok" else "",
        )
    except Exception as exc:  # noqa: BLE001
        return HealthCheck("scheduler", "Scheduled audits", "off", "Unknown", detail=str(exc))


# ── Roll-up ────────────────────────────────────────────────────────────────

def credential_timeout_check(seconds: float) -> HealthCheck:
    """Stand-in tile for a credential probe that did not answer in time."""
    return HealthCheck(
        "credentials", "Credential store", "bad", "No answer",
        detail=f"The credential store did not answer within {int(seconds)} seconds.",
        hint="Check that the backend is reachable, then re-check.", code="timeout",
    )


def collect(active_env: str | None, *, force: bool = False, credential: HealthCheck | None = None) -> dict:
    """All checks plus an overall state: ``ok`` | ``degraded`` | ``down``.

    ``credential`` lets the caller supply an already-bounded credential check, so
    a hung backend can't take the other tiles down with it.
    """
    checks = [
        credential or credential_check(force=force),
        tls_check(),
        storage_check(force=force),
        version_check(),
        scheduler_check(active_env),
    ]
    states = {c.state for c in checks}
    overall = "down" if "bad" in states else "degraded" if "warn" in states else "ok"
    return {"overall": overall, "checks": [c.to_dict() for c in checks], "boot_id": BOOT_ID}
