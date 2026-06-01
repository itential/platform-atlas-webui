"""Session service — CRUD plus capture/validate/report job kickoffs."""

from __future__ import annotations

import time as _time
from datetime import datetime, timedelta, timezone
from typing import Any

from platform_atlas.core.session_manager import get_session_manager

# In-process cache for get_session_summary() — avoids re-reading the Parquet
# file on every mini-report fetch. Keyed by session name; TTL prevents stale
# data from lingering if the user re-runs validation. Only populated when
# validation_completed so in-progress sessions always read fresh.
_summary_cache: dict[str, tuple[dict[str, Any], float]] = {}
_SUMMARY_CACHE_TTL = 60.0  # seconds


def is_session_complete(session_dict: dict[str, Any]) -> bool:
    """A session is 'complete' once capture, validate, and report all succeeded.

    Tier-mismatched sessions can still be opened in the WebUI when complete —
    you just can't re-run any stage until the active tier matches again.
    """
    return bool(
        session_dict.get("capture_completed")
        and session_dict.get("validation_completed")
        and session_dict.get("report_completed")
    )


def tier_lock_state(session_dict: dict[str, Any], active_tier: str) -> dict[str, Any]:
    """Compute whether a session is locked due to active-tier mismatch.

    Locks have two flavors:
    - ``run_blocked`` — always set when tiers differ. Capturing/validating/
      reporting against the wrong tier produces nonsense data, so we refuse
      regardless of the session's completion state.
    - ``view_blocked`` — set only when tiers differ AND the session has not
      yet completed all three stages. A finished audit's results should
      remain inspectable even after the user switches tiers; an in-progress
      session under the wrong tier is just a trap.
    """
    session_tier = (session_dict.get("tier") or "extended").lower()
    active = (active_tier or "extended").lower()
    locked = session_tier != active
    complete = is_session_complete(session_dict)
    if not locked:
        return {
            "locked": False, "is_complete": complete,
            "run_blocked": False, "view_blocked": False,
            "session_tier": session_tier, "active_tier": active,
            "reason": "", "fix": "",
        }
    return {
        "locked": True,
        "is_complete": complete,
        "run_blocked": True,
        "view_blocked": not complete,
        "session_tier": session_tier,
        "active_tier": active,
        "reason": (
            f"This session is bound to the {session_tier.capitalize()} tier, "
            f"but the active tier is {active.capitalize()}."
        ),
        "fix": (
            f"Switch the active tier back to {session_tier.capitalize()} "
            f"to {'open and run this session' if not complete else 're-run any stage'}."
        ),
    }


def _annotate_tier_lock(d: dict[str, Any], active_tier: str) -> dict[str, Any]:
    d["tier_lock"] = tier_lock_state(d, active_tier)
    return d


def list_sessions() -> list[dict[str, Any]]:
    """Return all sessions as a list of plain dicts ordered by recency.

    Pinned sessions (from ``config.json::pinned_sessions``) float to the
    top, ordered by recency within the pinned group. Unpinned sessions
    follow in their natural recency order.
    """
    from platform_atlas_webui.services.config import resolve_active_tier, get_pinned_session_names
    active_tier = resolve_active_tier()
    pinned_set = set(get_pinned_session_names())

    mgr = get_session_manager()
    sessions = sorted(
        mgr.list(),
        key=lambda s: s.metadata.updated_at,
        reverse=True,
    )
    active_name = mgr.get_active_session_name()

    out: list[dict[str, Any]] = []
    for s in sessions:
        m = s.metadata
        # Resolve report_file inline so callers iterating a session list don't
        # have to re-open every session.json with a follow-up get_session().
        report_file = str(s.report_file) if s.report_file.exists() else None
        d = {
            "name": m.name,
            "is_active": m.name == active_name,
            "is_pinned": m.name in pinned_set,
            "status": str(m.status),
            "environment": m.environment or "",
            "ruleset_id": m.ruleset_id or "",
            "ruleset_profile": m.ruleset_profile or "",
            "tier": getattr(m, "tier", None) or "extended",
            "organization_name": m.organization_name or "",
            "capture_completed": m.capture_completed,
            "validation_completed": m.validation_completed,
            "report_completed": m.report_completed,
            "pass_count": m.pass_count,
            "fail_count": m.fail_count,
            "skip_count": m.skip_count,
            "total_rules": m.total_rules,
            "updated_at": m.updated_at.isoformat() if m.updated_at else None,
            "created_at": m.created_at.isoformat() if m.created_at else None,
            "report_file": report_file,
            "directory": str(s.directory),
        }
        out.append(_annotate_tier_lock(d, active_tier))
    # Stable partition: pinned first (preserving recency order from above),
    # unpinned second. Avoids the O(n log n) re-sort a Python sorted() key
    # would incur and keeps the within-group recency ordering intact.
    pinned_out = [d for d in out if d["is_pinned"]]
    unpinned_out = [d for d in out if not d["is_pinned"]]
    return pinned_out + unpinned_out


_RETRYABLE_LOG_MODULES = ("platform_logs", "webserver_logs", "mongo_logs")


def _failed_log_modules_from_capture(session) -> list[dict[str, str]]:
    """Read ``_atlas.metadata.failed_modules`` from the session's capture JSON
    and filter to the three retry-eligible log modules.

    Returns a list of {"name", "error_message", "label", "kind_label",
    "instruction", "env_field"} dicts ready for the template to render.
    Empty list when capture hasn't run or no log modules failed.
    """
    if not session.capture_file.exists():
        return []
    try:
        import json
        with session.capture_file.open(encoding="utf-8") as f:
            cap = json.load(f)
    except Exception:  # noqa: BLE001
        return []

    metadata = (cap.get("_atlas") or {}).get("metadata") or {}
    failed = metadata.get("failed_modules") or []
    if not failed:
        return []

    try:
        from platform_atlas.capture.capture_engine import LOG_MODULE_RETRY_SPECS
    except Exception:  # noqa: BLE001
        return []

    out: list[dict[str, str]] = []
    for entry in failed:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name", "")
        if name not in _RETRYABLE_LOG_MODULES:
            continue
        spec = LOG_MODULE_RETRY_SPECS.get(name)
        if not spec:
            continue
        out.append({
            "name": name,
            "error_message": entry.get("error_message", ""),
            "label": spec["label"],
            "kind_label": spec["kind_label"],
            "instruction": spec["instruction"].strip(),
            "env_field": spec["env_field"],
        })
    return out


def get_session(name: str) -> dict[str, Any] | None:
    from platform_atlas_webui.services.config import resolve_active_tier
    active_tier = resolve_active_tier()

    mgr = get_session_manager()
    try:
        s = mgr.get(name)
    except Exception:
        return None
    m = s.metadata
    d = {
        "name": m.name,
        "status": str(m.status),
        "environment": m.environment or "",
        "ruleset_id": m.ruleset_id or "",
        "ruleset_profile": m.ruleset_profile or "",
        "tier": getattr(m, "tier", None) or "extended",
        "organization_name": m.organization_name or "",
        "description": m.description or "",
        "capture_completed": m.capture_completed,
        "validation_completed": m.validation_completed,
        "report_completed": m.report_completed,
        "pass_count": m.pass_count,
        "fail_count": m.fail_count,
        "skip_count": m.skip_count,
        "total_rules": m.total_rules,
        "report_file": str(s.report_file) if s.report_file.exists() else None,
        "directory": str(s.directory),
        "failed_log_modules": _failed_log_modules_from_capture(s),
    }
    try:
        from platform_atlas.capture.checkpoint import CaptureCheckpoint
        ckpt = CaptureCheckpoint(s.directory)
        d["has_checkpoint"] = ckpt.exists
        d["checkpoint_module_count"] = len(ckpt.completed_modules()) if ckpt.exists else 0
    except Exception:  # noqa: BLE001
        d["has_checkpoint"] = False
        d["checkpoint_module_count"] = 0
    return _annotate_tier_lock(d, active_tier)


def retry_log_capture(
    session_name: str,
    *,
    module: str,
    custom_path: str,
) -> dict[str, Any]:
    """Re-run a single failed log module against ``custom_path`` and patch
    the session's capture JSON in place.

    Mirrors the CLI's post-capture retry prompt: rebuild transport against
    the target that originally hosted the module, run the collector, and
    splice the result into both the flat module key and its nested
    destination so the capture JSON looks identical to a clean run.

    On success the path is auto-persisted to the active environment's
    matching override field so subsequent captures don't re-prompt for
    the same location. Raises ``ValueError`` for bad inputs (unknown
    module, empty path, non-absolute path).
    """
    if module not in _RETRYABLE_LOG_MODULES:
        raise ValueError(f"Module '{module}' is not retry-eligible")
    custom_path = (custom_path or "").strip()
    if not custom_path:
        raise ValueError("custom_path is required")
    if not custom_path.startswith("/"):
        raise ValueError("custom_path must be an absolute path")

    import json
    from pathlib import Path

    from platform_atlas.core.context import init_context, ctx
    from platform_atlas.core.session_manager import get_session_manager
    from platform_atlas.capture.capture_engine import (
        CAPTURE_STRUCTURE,
        LOG_MODULE_RETRY_SPECS,
        find_target_for_log_module,
        reshape_capture,
        retry_log_module_with_custom_path,
    )

    mgr = get_session_manager()
    # Activate so the session's bound env/tier/ruleset are live during
    # transport build + collector invocation.
    mgr.activate_session_context(session_name)
    init_context()

    session = mgr.get(session_name)
    if not session.capture_file.exists():
        return {"ok": False, "message": "No capture file — run capture first."}

    config = ctx().config
    target_dict = find_target_for_log_module(module, config)
    if target_dict is None:
        return {
            "ok": False,
            "message": (
                f"Could not locate the target that originally hosted "
                f"'{module}'. The session's environment may have changed."
            ),
        }

    try:
        retry_result = retry_log_module_with_custom_path(
            module_name=module,
            target_dict=target_dict,
            custom_path=custom_path,
            log_since=None,
            log_until=None,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "message": f"Retry failed: {type(exc).__name__}: {exc}",
        }

    # Patch the capture JSON: write to the nested destination key, drop the
    # entry from failed_modules, and re-mark the module ran successfully so
    # downstream consumers see a clean state.
    with session.capture_file.open(encoding="utf-8") as f:
        cap = json.load(f)

    nested_path = CAPTURE_STRUCTURE.get(module, "")
    if nested_path:
        # Re-reshape via the canonical helper so platform_logs lands at the
        # right nested location without us hand-rolling dot-notation writes.
        reshaped = reshape_capture({module: retry_result})
        for top_key, top_val in reshaped.items():
            if not isinstance(top_val, dict):
                cap[top_key] = top_val
                continue
            cap.setdefault(top_key, {})
            if isinstance(cap[top_key], dict):
                cap[top_key].update(top_val)

    metadata = cap.setdefault("_atlas", {}).setdefault("metadata", {})
    failed = metadata.get("failed_modules") or []
    metadata["failed_modules"] = [
        e for e in failed if not (isinstance(e, dict) and e.get("name") == module)
    ]
    modules_ran = metadata.get("modules_ran") or []
    # ``modules_ran`` is either a list of names or the sentinel ["all"];
    # only append when it's a name list and the module isn't already there.
    if isinstance(modules_ran, list) and modules_ran != ["all"] and module not in modules_ran:
        modules_ran.append(module)
        metadata["modules_ran"] = modules_ran

    session.capture_file.write_text(
        json.dumps(cap, indent=2, default=str, ensure_ascii=False),
        encoding="utf-8",
    )

    # Auto-persist the working path to the active env so the next capture
    # picks it up without re-prompting. Failures here are surfaced but
    # don't roll back the patched capture JSON — the user got their data.
    persisted = False
    persist_error = ""
    try:
        from platform_atlas.core.environment import get_environment_manager
        spec = LOG_MODULE_RETRY_SPECS[module]
        env_field = spec["env_field"]
        env_mgr = get_environment_manager()
        active_env = env_mgr.get_active_name()
        if active_env:
            env = env_mgr.load(active_env)
            setattr(env, env_field, custom_path)
            env_mgr.save(env)
            persisted = True
        else:
            persist_error = "No active environment — path not persisted."
    except Exception as exc:  # noqa: BLE001
        persist_error = f"Could not save to env: {type(exc).__name__}: {exc}"

    return {
        "ok": True,
        "message": f"Recovered {module} from {custom_path}",
        "module": module,
        "path": custom_path,
        "persisted": persisted,
        "persist_error": persist_error,
    }


def get_session_summary(name: str, *, max_failures: int = 12) -> dict[str, Any] | None:
    """Lightweight summary for the post-pipeline mini-report.

    Combines session metadata with a top-N slice of failing rules read from
    the validation parquet. Severity ordering matches the report — critical
    first, then warning, then info — so the UI renders by-importance without
    sorting client-side.

    Result is cached for _SUMMARY_CACHE_TTL seconds once validation is complete
    so repeated fetches (e.g. SSE close + frontend poll) skip the Parquet read.
    """
    mgr = get_session_manager()
    try:
        s = mgr.get(name)
    except Exception:
        return None
    m = s.metadata

    cached = _summary_cache.get(name)
    if cached is not None and m.validation_completed:
        result, ts = cached
        if _time.monotonic() - ts < _SUMMARY_CACHE_TTL:
            return result

    evaluated = (m.pass_count or 0) + (m.fail_count or 0)
    compliance_pct = round((m.pass_count / evaluated) * 100) if evaluated else 0

    failures: list[dict[str, Any]] = []
    if m.validation_completed and s.validation_file.exists():
        try:
            import pandas as pd
            # Read only the columns needed for the summary — avoids deserializing
            # the full DataFrame (which can be 100+ MB for large audits).
            df = pd.read_parquet(
                s.validation_file,
                columns=["status", "severity", "rule_number", "name", "category",
                         "path", "expected", "actual", "recommendations"],
            )
            fail_df = df[df["status"].astype(str).str.upper() == "FAIL"]
            sev_order = {"critical": 0, "warning": 1, "info": 2}
            fail_df = fail_df.assign(
                _ord=fail_df["severity"].astype(str).str.lower().map(sev_order).fillna(3)
            ).sort_values("_ord").head(max_failures)
            for _, row in fail_df.iterrows():
                failures.append({
                    "rule_number": str(row.get("rule_number", "")),
                    "name": str(row.get("name", "")),
                    "severity": str(row.get("severity", "info")).lower(),
                    "category": str(row.get("category", "")),
                    "path": str(row.get("path", "")),
                    "expected": str(row.get("expected", "")),
                    "actual": str(row.get("actual", "")),
                    "recommendations": str(row.get("recommendations", "")),
                })
        except Exception:  # noqa: BLE001
            # Parquet read can fail if the file was just written and not yet
            # flushed, or if pyarrow isn't available — fall through with an
            # empty failures list. The summary is still useful.
            pass

    result = {
        "name": m.name,
        "environment": m.environment or "",
        "tier": getattr(m, "tier", None) or "extended",
        "ruleset_id": m.ruleset_id or "",
        "ruleset_profile": m.ruleset_profile or "",
        "capture_completed": m.capture_completed,
        "validation_completed": m.validation_completed,
        "report_completed": m.report_completed,
        "pass_count": m.pass_count or 0,
        "fail_count": m.fail_count or 0,
        "skip_count": m.skip_count or 0,
        "total_rules": m.total_rules or 0,
        "compliance_pct": compliance_pct,
        "evaluated": evaluated,
        "failures": failures,
        "report_file_url": f"/reports/{m.name}" if s.report_file.exists() else None,
        "session_url": f"/sessions/{m.name}",
    }

    if m.validation_completed:
        _summary_cache[name] = (result, _time.monotonic())

    return result


def create_session(
    name: str,
    *,
    description: str = "",
    tier: str = "",
    environment: str = "",
    ruleset_id: str = "",
    ruleset_profile: str = "",
) -> str:
    """Create a session under the active env+ruleset+tier and return its name."""
    mgr = get_session_manager()
    s = mgr.create(
        name=name,
        description=description,
        tier=tier or "",
        environment=environment,
        ruleset_id=ruleset_id,
        ruleset_profile=ruleset_profile,
    )
    return s.metadata.name


def activate(name: str) -> None:
    """Activate a session and atomically restore its env + ruleset + profile.

    Mirrors what the CLI's ``session activate`` does. Calling ``set_active``
    alone (the previous WebUI behavior) only moved the active-session pointer
    and left the previous session's env/ruleset/profile in place — silently
    breaking the atomicity guarantee that sessions exist for.
    """
    mgr = get_session_manager()
    mgr.activate_session_context(name)


def delete(name: str, *, force: bool = False) -> None:
    mgr = get_session_manager()
    mgr.delete(name, force=force)


# ── Compliance calendar aggregation ───────────────────────────────────────────

def calendar_buckets_from(
    sessions: list[dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, Any]:
    """53×7 daily calendar grid for the trailing year, in the user's local TZ.

    Cells are emitted in column-major order to pair with CSS
    ``grid-auto-flow: column`` — 53 columns (weeks), 7 rows (Sunday top to
    Saturday bottom). The rightmost column ends with today; future cells
    in the current week carry ``future: True`` so the template can render
    them blank without affecting stats.

    Intensity tiers reflect daily session count:
    1 → l1, 2 → l2, 3-4 → l3, 5+ → l4. A day where any session ended in
    SessionStatus.FAILED — i.e. the audit itself broke (capture errored,
    credential backend unreachable, etc.) — is flagged ``fail`` and
    overrides the tier. Compliance findings (failed rules in an otherwise
    successful run) do NOT count as failures here; an audit can complete
    cleanly and still surface non-passing rules.
    """
    base = now or datetime.now(timezone.utc)
    today = base.astimezone().date()

    # Anchor to the Sunday at start of "this week" (US convention, GitHub-style).
    days_since_sunday = (today.weekday() + 1) % 7  # weekday: Mon=0..Sun=6 → Sun=0..Sat=6
    sunday_this_week = today - timedelta(days=days_since_sunday)
    start_date = sunday_this_week - timedelta(weeks=52)

    by_day: dict[Any, dict[str, int]] = {}
    last_session_date = None
    for s in sessions:
        ts = s.get("created_at")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        d = dt.astimezone().date()
        if last_session_date is None or d > last_session_date:
            last_session_date = d
        agg = by_day.setdefault(d, {"count": 0, "fails": 0})
        agg["count"] += 1
        if str(s.get("status", "")).lower() == "failed":
            agg["fails"] += 1

    cells: list[dict[str, Any]] = []
    for week in range(53):
        col_start = start_date + timedelta(weeks=week)
        for dow in range(7):
            cell_date = col_start + timedelta(days=dow)
            if cell_date > today:
                cells.append({"date": cell_date.isoformat(), "future": True, "count": 0, "level": "", "fail": False})
                continue
            agg = by_day.get(cell_date)
            count = agg["count"] if agg else 0
            fail = bool(agg and agg["fails"] > 0)
            if count >= 5:
                level = "l4"
            elif count >= 3:
                level = "l3"
            elif count == 2:
                level = "l2"
            elif count == 1:
                level = "l1"
            else:
                level = ""
            cells.append({
                "date": cell_date.isoformat(),
                "future": False,
                "count": count,
                "level": level,
                "fail": fail,
            })

    sessions_total = sum(c["count"] for c in cells)
    fail_days = sum(1 for c in cells if c["fail"])

    longest_gap = 0
    active_dates = sorted(d for d, agg in by_day.items() if agg["count"] > 0 and start_date <= d <= today)
    for i in range(1, len(active_dates)):
        gap = (active_dates[i] - active_dates[i - 1]).days - 1
        if gap > longest_gap:
            longest_gap = gap

    days_since_last = (today - last_session_date).days if last_session_date else None

    # 12 month abbreviations ending with the current month.
    months: list[str] = []
    cursor = today.replace(day=1)
    seen = []
    for _ in range(12):
        seen.append(cursor.strftime("%b"))
        # step back one month
        if cursor.month == 1:
            cursor = cursor.replace(year=cursor.year - 1, month=12)
        else:
            cursor = cursor.replace(month=cursor.month - 1)
    months = list(reversed(seen))

    return {
        "cells": cells,
        "total": sessions_total,
        "fail_days": fail_days,
        "longest_gap": longest_gap,
        "days_since_last": days_since_last,
        "months": months,
    }
