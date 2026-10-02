"""
Atlas MCP tool implementations — read-only queries over already-audited
Atlas data (v1: no action tools, see design/MCP/*.md in the platform-atlas
repo for the phasing rationale).

Every tool is a thin wrapper: call the CLI's own "brains"
(`platform_atlas.core.fleet`, `platform_atlas.mcp_queries`,
`session_manager`, `environment`, `reporting.diff_engine`) and format a
concise response — never a raw capture dump. `core.fleet` owns the
per-environment snapshot (`collect_fleet`) that also powers the CLI's
`fleet status` table and the WebUI's `/fleet` page; `mcp_queries` holds
every cross-session/cross-rule analytical query that exists solely to back
these tools. All of these are pure local-disk reads that are already
confirmed independent of `ctx()`/`init_context()`, so none of them touch
Atlas's process-global context singletons.

Every returned payload has embedded URI credentials masked in every string
leaf on the way out — defense in depth in case a captured `actual`/
`expected` value ever contains something credential-shaped (e.g. a
`mongodb://user:pass@host` connection string).
"""

from __future__ import annotations

from typing import Any

from platform_atlas.core.utils import redact_uri_credentials


def _redact_strings(obj: Any) -> Any:
    """Recursively mask embedded URI credentials in every string leaf.

    Deliberately narrower than `redact_capture_credentials` (core/utils.py):
    that one *also* wholesale-redacts any dict value whose key merely looks
    secret-shaped (matches on bare "pass" bounded by underscores, among
    others) — tuned for raw capture data, where Atlas never uses "pass" as
    a compliance term. MCP tool responses do constantly (`pass_count`,
    `pass_rate_pct`, `baseline_pass_pct`, `fleet_pass_rate_pct`, ...), so
    that key-based pass would falsely nuke real compliance numbers to
    "*****". The actual credential risk here is narrower: an `actual`/
    `expected` value that happens to be a captured connection string —
    exactly what `redact_uri_credentials`'s string-level scan already
    catches, with no key-name heuristic involved.
    """
    if isinstance(obj, dict):
        return {k: _redact_strings(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_strings(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_redact_strings(v) for v in obj)
    return redact_uri_credentials(obj)


def _out(payload: dict[str, Any]) -> dict[str, Any]:
    return _redact_strings(payload)


def list_environments() -> dict[str, Any]:
    """List every configured environment with its tier and latest audit status."""
    from platform_atlas.core.fleet import collect_fleet
    from platform_atlas.core.config import environment_effective_tier

    entries, summary = collect_fleet()
    environments = []
    for e in entries:
        environments.append({
            "name": e.name,
            "tier": environment_effective_tier(e.name),
            "is_active": e.is_active,
            "description": e.description,
            "last_session_name": e.last_session_name,
            "last_session_status": e.last_session_status,
            "last_session_at": e.last_session_at,
            "last_validated_session_name": e.last_validated_session_name,
            "pass_rate_pct": e.pass_rate_pct,
            "state": e.state,
        })
    return _out({"environments": environments, "summary": summary.to_dict()})


def list_sessions(environment: str = "", limit: int = 20) -> dict[str, Any]:
    """List recent sessions, newest first, optionally scoped to one environment."""
    from platform_atlas.core.session_manager import get_session_manager

    sessions = get_session_manager().list(sort_by="updated_at")
    if environment:
        sessions = [s for s in sessions if s.metadata.environment == environment]
    sessions = sessions[:limit]

    rows = [{
        "name": s.metadata.name,
        "environment": s.metadata.environment,
        "status": str(s.metadata.status),
        "tier": s.metadata.tier,
        "updated_at": s.metadata.updated_at.isoformat(),
        "capture_completed": s.metadata.capture_completed,
        "validation_completed": s.metadata.validation_completed,
        "report_completed": s.metadata.report_completed,
        "pass_count": s.metadata.pass_count,
        "fail_count": s.metadata.fail_count,
        "skip_count": s.metadata.skip_count,
        "total_rules": s.metadata.total_rules,
    } for s in sessions]
    return _out({"sessions": rows, "count": len(rows)})


def get_compliance_summary(environment: str = "") -> dict[str, Any]:
    """Compliance summary for one environment's latest validated session.

    Falls back to whichever environment was most recently audited when
    *environment* is omitted.
    """
    from platform_atlas.core.fleet import collect_fleet
    from platform_atlas.mcp_queries import latest_validated_environment
    from platform_atlas.core.config import environment_effective_tier

    target = environment or latest_validated_environment()
    if not target:
        return _out({"error": "No validated sessions found for any environment."})

    entries, _summary = collect_fleet()
    entry = next((e for e in entries if e.name == target), None)
    if entry is None:
        return _out({"error": f"Environment '{target}' not found."})
    if not entry.last_validated_session_name:
        return _out({"environment": target, "error": "No validated session for this environment yet."})

    return _out({
        "environment": target,
        "tier": environment_effective_tier(target),
        "session_name": entry.last_validated_session_name,
        "pass_count": entry.pass_count,
        "fail_count": entry.fail_count,
        "skip_count": entry.skip_count,
        "total_rules": entry.total_rules,
        "pass_rate_pct": entry.pass_rate_pct,
        "state": entry.state,
    })


def fleet_top_fix(top_n: int = 3) -> dict[str, Any]:
    """Rank rule failures by how many environments they recur in fleet-wide."""
    from platform_atlas.mcp_queries import fleet_rule_impact

    impacts = fleet_rule_impact(top_n=top_n)
    return _out({"top_fixes": [i.to_dict() for i in impacts]})


def explain_rule(query: str, environment: str = "") -> dict[str, Any]:
    """Look up a rule by number/name/path and show its status in one environment.

    Falls back to whichever environment was most recently audited when
    *environment* is omitted.
    """
    from platform_atlas.mcp_queries import find_rule

    matches = find_rule(query, environment=environment or None)
    return _out({"matches": [m.to_dict() for m in matches]})


def rule_fleet_distribution(query: str) -> dict[str, Any]:
    """Look up a rule by number/name/path across *every* environment's latest
    validated session — shows whether a failure is a systemic policy gap or
    a one-off, which ``explain_rule`` (one environment) can't."""
    from platform_atlas.mcp_queries import find_rule_across_fleet

    matches = find_rule_across_fleet(query)
    return _out({"matches": [m.to_dict() for m in matches]})


def fleet_severity_breakdown(environment: str = "") -> dict[str, Any]:
    """FAIL-row counts by severity, one environment or fleet-wide — what's
    most urgent right now."""
    from platform_atlas.mcp_queries import fleet_severity_breakdown as _fleet_severity_breakdown

    return _out(_fleet_severity_breakdown(environment=environment).to_dict())


def rule_category_health(environment: str = "") -> dict[str, Any]:
    """Pass/fail/skip counts grouped by rule category (gateway4, mongo_conf,
    redis_conf, ...), one environment or fleet-wide — pinpoints the weakest
    subsystem."""
    from platform_atlas.mcp_queries import rule_category_health as _rule_category_health

    categories = _rule_category_health(environment=environment)
    return _out({"categories": [c.to_dict() for c in categories]})


def stale_environments(days: int = 30) -> dict[str, Any]:
    """Environments whose last session is older than *days*, or that have
    never been audited at all — audit-coverage gaps, not compliance
    failures."""
    from platform_atlas.mcp_queries import stale_environments as _stale_environments

    stale = _stale_environments(days=days)
    return _out({"stale_environments": [s.to_dict() for s in stale]})


def session_history_trend(environment: str, limit: int = 10) -> dict[str, Any]:
    """Pass-rate trajectory across the last *limit* validated sessions in one
    environment, with an improving/degrading/flat classification."""
    from platform_atlas.mcp_queries import session_history_trend as _session_history_trend

    return _out(_session_history_trend(environment, limit=limit).to_dict())


def flaky_rules(environment: str, lookback: int = 5) -> dict[str, Any]:
    """Rules that flip PASS/FAIL more than once across the last *lookback*
    validated sessions in one environment — flapping/unstable checks, not a
    single clean regression."""
    from platform_atlas.mcp_queries import flaky_rules as _flaky_rules

    flaky = _flaky_rules(environment, lookback=lookback)
    return _out({"flaky_rules": [f.to_dict() for f in flaky]})


def compare_environments(environment_a: str, environment_b: str) -> dict[str, Any]:
    """Rules where two environments' latest validated sessions disagree (one
    passes, the other fails) on the same rule number."""
    from platform_atlas.mcp_queries import compare_environments as _compare_environments

    comparison = _compare_environments(environment_a, environment_b)
    if comparison is None:
        return _out({
            "error": f"No validated session found for '{environment_a}' and/or '{environment_b}'.",
        })
    return _out(comparison.to_dict())


def skip_reason_breakdown(environment: str = "") -> dict[str, Any]:
    """SKIP rows grouped by why they were skipped — distinguishes a
    deliberate suppression from data that was simply never collected
    (e.g. the collector couldn't reach the service)."""
    from platform_atlas.mcp_queries import skip_reason_breakdown as _skip_reason_breakdown

    return _out(_skip_reason_breakdown(environment=environment).to_dict())


def fleet_tier_coverage() -> dict[str, Any]:
    """Environment counts by tier (standard/extended/saas), cross-referenced
    with average pass rate per tier — useful for tier-migration planning."""
    from platform_atlas.mcp_queries import fleet_tier_coverage as _fleet_tier_coverage

    coverage = _fleet_tier_coverage()
    return _out({"tiers": [c.to_dict() for c in coverage]})


def fleet_regressions_since_last_audit(limit: int = 10) -> dict[str, Any]:
    """For every environment with at least two validated sessions, the
    regressions since its previous audit — ranked by severity across the
    whole fleet: what just got worse, anywhere, since we last looked."""
    from platform_atlas.mcp_queries import fleet_regressions_since_last_audit as _fleet_regressions

    regressions = _fleet_regressions(limit=limit)
    return _out({"regressions": [r.to_dict() for r in regressions]})


def diff_sessions(environment: str, baseline: str = "", latest: str = "") -> dict[str, Any]:
    """Diff two validated sessions in one environment.

    Defaults to the two most recent validated sessions when *baseline*/
    *latest* are omitted. Only fixed/regressed/new/removed/changed rules are
    returned — unchanged and skipped rows are dropped to keep the response
    concise (still counted in ``summary``).
    """
    import logging

    from platform_atlas.core.session_manager import get_session_manager
    from platform_atlas.validation.results import load_validation_results
    from platform_atlas.reporting.diff_engine import diff_reports, summarize_diff, ChangeType

    logger = logging.getLogger(__name__)

    sessions = [
        s for s in get_session_manager().list(sort_by="updated_at")
        if s.metadata.environment == environment
        and s.metadata.validation_completed and s.metadata.total_rules
    ]
    if not sessions:
        return _out({"error": f"No validated sessions found for environment '{environment}'."})

    def _find(name: str):
        return next((s for s in sessions if s.metadata.name == name), None)

    def _load(session) -> Any | None:
        """Load one session's validation results, or None if the file is
        missing/unreadable. A session's metadata can claim "validated" long
        after its ``02_validation.json`` was pruned, moved, or — for a
        session validated before the pandas/pyarrow-to-JSON migration —
        never existed at all (those wrote ``02_validation.parquet`` instead).
        """
        try:
            return load_validation_results(session.validation_file)
        except Exception as exc:  # noqa: BLE001 — one unreadable session shouldn't sink the diff
            logger.warning("diff_sessions: could not read %s: %s", session.validation_file, exc)
            return None

    if latest:
        latest_session = _find(latest)
        if latest_session is None:
            return _out({"error": f"Session '{latest}' not found (or not validated) in '{environment}'."})
        latest_results = _load(latest_session)
        if latest_results is None:
            return _out({"error": f"Session '{latest_session.metadata.name}' has no readable validation data on disk."})
    else:
        latest_session = latest_results = None
        for candidate in sessions:
            results = _load(candidate)
            if results is not None:
                latest_session, latest_results = candidate, results
                break
        if latest_session is None:
            return _out({"error": f"No environment '{environment}' session has readable validation data on disk."})

    if baseline:
        baseline_session = _find(baseline)
        if baseline_session is None:
            return _out({"error": f"Session '{baseline}' not found (or not validated) in '{environment}'."})
        baseline_results = _load(baseline_session)
        if baseline_results is None:
            return _out({"error": f"Session '{baseline_session.metadata.name}' has no readable validation data on disk."})
    else:
        baseline_session = baseline_results = None
        for candidate in sessions:
            if candidate.metadata.name == latest_session.metadata.name:
                continue
            results = _load(candidate)
            if results is not None:
                baseline_session, baseline_results = candidate, results
                break
        if baseline_session is None:
            return _out({"error": "No baseline session with readable validation data available to diff against."})

    diff_result = diff_reports(baseline_results, latest_results)
    summary = summarize_diff(diff_result)

    notable = {str(ChangeType.FIXED), str(ChangeType.REGRESSED), str(ChangeType.NEW_RULE),
               str(ChangeType.REMOVED), str(ChangeType.CHANGED)}
    changes = [row for row in diff_result.rows if row.get("change_type") in notable]

    return _out({
        "environment": environment,
        "baseline_session": baseline_session.metadata.name,
        "latest_session": latest_session.metadata.name,
        "summary": {
            "total_rules": summary.total_rules,
            "fixed": summary.fixed,
            "regressed": summary.regressed,
            "unchanged": summary.unchanged,
            "new_rules": summary.new_rules,
            "removed": summary.removed,
            "changed": summary.changed,
            "skipped": summary.skipped,
            "baseline_pass_pct": summary.baseline_pass_pct,
            "latest_pass_pct": summary.latest_pass_pct,
            "delta_pct": summary.delta_pct,
            "rating": summary.rating,
        },
        "changes": changes,
    })
