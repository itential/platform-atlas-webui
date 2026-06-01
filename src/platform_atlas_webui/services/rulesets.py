"""Ruleset service — list, switch, and inspect rulesets and profiles."""

from __future__ import annotations

from typing import Any

from platform_atlas.core.context import ctx
from platform_atlas.core.ruleset_manager import get_ruleset_manager


def _format_validation(v: Any) -> str:
    """Render a rule's validation block as a single human line for the table."""
    if not isinstance(v, dict):
        return ""
    op = v.get("operator", "?")
    exp = v.get("expected")

    def _ref_str(ref_obj: dict) -> str:
        ref = ref_obj.get("ref", "?")
        mult = ref_obj.get("multiply")
        if mult and mult != 1:
            return f"{ref} × {mult}"
        return ref

    if isinstance(exp, dict) and "ref" in exp:
        return f"{op} {_ref_str(exp)}"
    if op == "in" and isinstance(exp, list):
        joined = ", ".join(map(str, exp[:4]))
        suffix = "…" if len(exp) > 4 else ""
        return f"in [{joined}{suffix}]"
    if op == "in_range" and isinstance(exp, list) and len(exp) == 2:
        return f"between {exp[0]} and {exp[1]}"
    if isinstance(exp, list):
        joined = ", ".join(map(str, exp[:3]))
        suffix = "…" if len(exp) > 3 else ""
        return f"{op} [{joined}{suffix}]"
    if isinstance(exp, (str, int, float, bool)):
        return f"{op} {exp}"
    return op


def _get_active_ruleset_meta(rs_id: str) -> dict[str, Any]:
    """Read the ruleset's top-level metadata block from disk."""
    if not rs_id:
        return {}
    try:
        import json
        mgr = get_ruleset_manager()
        path = None
        for meta in mgr.discover_rulesets():
            if meta.id == rs_id:
                path = getattr(meta, "file_path", None)
                break
        if path is None or not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("ruleset", {}) or {}
    except Exception:
        return {}


def _is_legacy_ruleset(target_product: str, ruleset_id: str) -> bool:
    """True when the ruleset targets the legacy 2023.x product line."""
    tp = (target_product or "").lower()
    rid = (ruleset_id or "").lower()
    return "2023" in tp or "2023" in rid


def _is_legacy_profile(profile_id: str) -> bool:
    """True when the profile is scoped to the legacy 2023.x ruleset."""
    return (profile_id or "").lower().startswith("2023")


def list_rulesets() -> list[dict[str, Any]]:
    mgr = get_ruleset_manager()
    active = mgr.get_active_ruleset_id()
    out: list[dict[str, Any]] = []
    for meta in mgr.discover_rulesets():
        tp = getattr(meta, "target_product", "")
        out.append({
            "id":             meta.id,
            "name":           getattr(meta, "name", meta.id),
            "version":        getattr(meta, "version", ""),
            "description":    getattr(meta, "description", ""),
            "target_product": tp,
            "rule_count":     getattr(meta, "rule_count", 0),
            "is_active":      meta.id == active,
            "is_legacy":      _is_legacy_ruleset(tp, meta.id),
        })
    return out


def list_profiles() -> list[dict[str, Any]]:
    mgr = get_ruleset_manager()
    active_profile = mgr.get_active_profile_id()
    out: list[dict[str, Any]] = []
    for p in mgr.discover_profiles():
        out.append({
            "id":          p.id,
            "name":        getattr(p, "name", p.id),
            "description": getattr(p, "description", ""),
            "is_active":   p.id == active_profile,
            "is_legacy":   _is_legacy_profile(p.id),
        })
    return out


def get_active_ruleset_summary() -> dict[str, Any]:
    mgr = get_ruleset_manager()
    rs_id = mgr.get_active_ruleset_id() or ""
    profile = mgr.get_active_profile_id() or ""

    try:
        ruleset = ctx().ruleset
    except Exception:
        ruleset = None

    # Build suppressed rule map {rule_number: reason} from the active config.
    try:
        suppressed_map: dict[str, str] = {
            r["rule_number"]: r.get("reason", "")
            for r in (ctx().config.skip_rules or [])
            if isinstance(r, dict)
        }
    except Exception:
        suppressed_map = {}

    rules: list[dict[str, Any]] = []
    if ruleset is not None:
        for r in ruleset.rules:
            d = r if isinstance(r, dict) else {}
            validation = d.get("validation") if isinstance(r, dict) else getattr(r, "validation", None)
            messages = d.get("messages") if isinstance(r, dict) else getattr(r, "messages", None) or {}
            rule_number = d.get("rule_number", "")
            rules.append({
                "id":             d.get("id") or rule_number,
                "rule_number":    rule_number,
                "name":           d.get("name", ""),
                "description":    d.get("description", ""),
                "category":       d.get("category", ""),
                "severity":      (d.get("severity") or "").lower(),
                "tier":           d.get("tier", "standard"),
                "enabled":        d.get("enabled", True),
                # Marker set by RulesetManager._apply_profile when the active
                # profile is the reason a rule is disabled (vs disabled in
                # the base ruleset itself). Drives the muted "Disabled in
                # profile" pill in the active-ruleset table.
                "disabled_by_profile": bool(d.get("disabled_by_profile", False)),
                # True when the rule is in the active environment's skip_rules.
                # These rules still run through validation but produce SKIP
                # results labelled "Suppressed by user" in reports.
                "suppressed_by_user": rule_number in suppressed_map,
                "suppression_reason": suppressed_map.get(rule_number, ""),
                "path":           d.get("path", ""),
                "alt_path":       d.get("alt_path", ""),
                "validation":     validation or {},
                "validation_summary": _format_validation(validation),
                "validation_type":   (validation or {}).get("type", "") if isinstance(validation, dict) else "",
                "operator":       (validation or {}).get("operator", "") if isinstance(validation, dict) else "",
                "messages":       messages or {},
                "updated_at":     d.get("updated_at", ""),
            })

    # Counts by severity / category / status — handy for the stats strip.
    sev_counts: dict[str, int] = {}
    cat_counts: dict[str, int] = {}
    enabled_count = 0
    suppressed_count = 0
    for r in rules:
        sev_counts[r["severity"] or "unspecified"] = sev_counts.get(r["severity"] or "unspecified", 0) + 1
        cat_counts[r["category"] or "uncategorized"] = cat_counts.get(r["category"] or "uncategorized", 0) + 1
        if r["enabled"]:
            enabled_count += 1
        if r["suppressed_by_user"]:
            suppressed_count += 1

    meta = _get_active_ruleset_meta(rs_id)

    return {
        "ruleset_id":     rs_id,
        "profile":        profile,
        "rule_count":     len(rules),
        "enabled_count":  enabled_count,
        "rules":          rules,
        "severities":     sev_counts,
        "categories":     cat_counts,
        # Top-level ruleset metadata — empty dict when not discoverable
        "name":           meta.get("name", rs_id),
        "version":        meta.get("version", ""),
        "description":    meta.get("description", ""),
        "target_product": meta.get("target_product", ""),
        "author":         meta.get("author", ""),
        "updated_at":     meta.get("updated_at", ""),
    }


def set_active(ruleset_id: str, profile_id: str | None = None) -> None:
    mgr = get_ruleset_manager()
    mgr.set_active_ruleset(ruleset_id, profile_id=profile_id)
