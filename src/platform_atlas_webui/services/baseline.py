"""Baseline service — pin, inspect, and clear an environment's validation baseline.

Thin wrapper over ``platform_atlas.core.baseline_store`` (the same module the
CLI's ``env baseline`` commands use) shaped for the WebUI's dict-based
templates and routes.
"""

from __future__ import annotations

from typing import Any

from platform_atlas.core import baseline_store
from platform_atlas.core.session_manager import SessionNotFoundError, get_session_manager


def baseline_summary(env_name: str) -> dict[str, Any] | None:
    """The pinned baseline for ``env_name`` with pass/fail counts, or None if unset."""
    data = baseline_store.load_raw(env_name)
    if data is None:
        return None
    rows = (data.get("validation") or {}).get("results", [])
    pass_count = sum(1 for r in rows if (r.get("status") or "").upper() == "PASS")
    fail_count = sum(1 for r in rows if (r.get("status") or "").upper() == "FAIL")
    return {
        **data,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "total_count": len(rows),
    }


def list_baseline_candidates(env_name: str) -> list[dict[str, Any]]:
    """Sessions in ``env_name`` with validation results — eligible baseline sources."""
    mgr = get_session_manager()
    out = []
    for s in mgr.list():
        if (s.metadata.environment or "") != env_name:
            continue
        if not s.validation_file.exists():
            continue
        out.append({
            "name": s.name,
            "created_at": s.metadata.created_at.isoformat() if s.metadata.created_at else None,
            "pass_count": s.metadata.pass_count,
            "fail_count": s.metadata.fail_count,
        })
    out.sort(key=lambda d: d["created_at"] or "", reverse=True)
    return out


def set_baseline(env_name: str, session_name: str) -> dict[str, Any]:
    """Pin ``session_name`` as the baseline for ``env_name``.

    Raises ValueError if the session doesn't exist, belongs to a different
    environment, or has no validation results — the same guards the CLI's
    ``env baseline set`` enforces.
    """
    mgr = get_session_manager()
    try:
        session = mgr.get(session_name)
    except SessionNotFoundError as exc:
        raise ValueError(f"Session '{session_name}' not found") from exc
    return baseline_store.set_baseline(env_name, session)


def clear_baseline(env_name: str) -> bool:
    """Remove the pinned baseline for ``env_name``. Returns True if anything was removed."""
    return baseline_store.clear(env_name)
