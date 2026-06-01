"""
Job runner functions for Atlas long-running operations.

Each runner is a synchronous function that the JobRegistry executes in
a worker thread. It receives a JobLogger as the first argument and pushes
progress events as it runs.

The implementation reuses the same Atlas core APIs the CLI calls but
swaps the Rich console for a JobLogger so the WebUI gets streaming
events instead of terminal output.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _restore_session_context(session_name: str, jlogger) -> None:
    """Activate session context and reinit Atlas so the session's bound
    ruleset/profile/environment are live before any job starts."""
    from platform_atlas.core.session_manager import get_session_manager
    from platform_atlas.core.context import init_context

    mgr = get_session_manager()
    try:
        mgr.activate_session_context(session_name)
    except Exception as exc:
        jlogger.warning(f"Could not fully restore session context: {exc}")

    try:
        init_context()
    except Exception as exc:
        jlogger.warning(f"Context reinit after session restore failed: {exc}")


def run_preflight_job(jlogger, *, scope_summary: str = "") -> dict[str, Any]:
    """Run preflight against the active environment's targets.

    Runs each check individually and emits a structured ``check`` event per
    result so the WebUI can render per-system pass/fail badges in real time.
    """
    from platform_atlas.core.context import ctx
    from platform_atlas.core.preflight import (
        _check_credential_backend,
        _check_node_ssh,
        _SSH_COLLECTORS,
        _CONNECTOR_COLLECTORS,
        PreflightReport,
        CheckResult,
    )
    from platform_atlas.capture.modules_registry import build_preflight_checks

    config = ctx().config
    targets = list(config.targets)

    if not targets:
        jlogger.error("No deployment targets configured.")
        return {"all_passed": False, "target_count": 0}

    is_standard = ctx().is_standard
    jlogger.phase(f"Preflight · {len(targets)} target(s) · {'Standard' if is_standard else 'Extended'} tier")
    if scope_summary:
        jlogger.info(scope_summary)

    report = PreflightReport()

    # ── Phase 0: Credential store ─────────────────────────────────
    jlogger.phase("Phase 0 — Credential store")
    cred = _check_credential_backend()
    report.results.append(cred)
    jlogger.check(cred.name, cred.status.value, cred.message, cred.details)
    if not cred.passed:
        jlogger.error("Credential check failed — cannot continue without credentials.")
        return {"all_passed": False, "target_count": len(targets), "passed": 0, "failed": 1}

    ssh_healthy: list[dict] = []

    # ── Phase 1: SSH connectivity ─────────────────────────────────
    if targets and not is_standard:
        ssh_targets = [t for t in targets if t.get("transport", "ssh") == "ssh"]
        if ssh_targets:
            jlogger.phase(f"Phase 1 — SSH connectivity ({len(ssh_targets)} node(s))")
            for target in ssh_targets:
                name = target.get("name", "unknown")
                host = target.get("host", "")
                jlogger.info(f"Probing SSH → {name}" + (f" ({host})" if host else "") + "…")
                result = _check_node_ssh(target)
                report.results.append(result)
                jlogger.check(result.name, result.status.value, result.message, result.details)
                if result.passed:
                    ssh_healthy.append(target)

    # ── Phase 2: Node services via SSH ────────────────────────────
    if ssh_healthy:
        from platform_atlas.core.transport import transport_from_config
        jlogger.phase(f"Phase 2 — Node services via SSH ({len(ssh_healthy)} reachable node(s))")
        for target in ssh_healthy:
            target_name = target.get("name", "unknown")
            target_modules = set(target.get("modules", []))
            relevant = (target_modules & _SSH_COLLECTORS) if target_modules else set(_SSH_COLLECTORS)
            if not relevant:
                continue
            jlogger.info(f"Checking services on {target_name}: {', '.join(sorted(relevant))}…")
            try:
                transport = transport_from_config(target)
            except Exception as e:
                for mk in sorted(relevant):
                    r = CheckResult.fail(f"{mk} → {target_name}", f"Transport error: {e}", group="node_services")
                    report.results.append(r)
                    jlogger.check(r.name, r.status.value, r.message, r.details)
                continue
            try:
                all_checks = build_preflight_checks(transport)
            except Exception as e:
                for mk in sorted(relevant):
                    r = CheckResult.fail(f"{mk} → {target_name}", f"Build error: {e}", group="node_services")
                    report.results.append(r)
                    jlogger.check(r.name, r.status.value, r.message, r.details)
                continue
            for mk in sorted(relevant):
                check_fn = all_checks.get(mk)
                if check_fn is None:
                    continue
                check_label = f"{mk} → {target_name}"
                try:
                    res = check_fn()
                    r = CheckResult(name=check_label, status=res.status, message=res.message, details=res.details, group="node_services")
                except Exception as e:
                    r = CheckResult.fail(check_label, f"{type(e).__name__}: {e}", group="node_services")
                report.results.append(r)
                jlogger.check(r.name, r.status.value, r.message, r.details)
            try:
                transport.close()
            except Exception:
                pass

    # ── Phase 2 (local): Local transport services ─────────────────
    if targets and not is_standard:
        from platform_atlas.core.transport import LocalTransport
        local_targets = [t for t in targets if t.get("transport") == "local"]
        for target in local_targets:
            target_name = target.get("name", "local")
            target_modules = set(target.get("modules", []))
            relevant = (target_modules & _SSH_COLLECTORS) if target_modules else set(_SSH_COLLECTORS)
            if not relevant:
                continue
            local_transport = LocalTransport()
            all_checks = build_preflight_checks(local_transport)
            for mk in sorted(relevant):
                check_fn = all_checks.get(mk)
                if check_fn is None:
                    continue
                check_label = f"{mk} → {target_name}"
                try:
                    res = check_fn()
                    r = CheckResult(name=check_label, status=res.status, message=res.message, details=res.details, group="node_services")
                except Exception as e:
                    r = CheckResult.fail(check_label, f"{type(e).__name__}: {e}", group="node_services")
                report.results.append(r)
                jlogger.check(r.name, r.status.value, r.message, r.details)

    # ── Phase 2b: Kubernetes ──────────────────────────────────────
    if targets and not is_standard:
        k8s_targets = [t for t in targets if t.get("transport") == "kubernetes"]
        if k8s_targets:
            jlogger.phase("Phase 2b — Kubernetes configuration")
            from platform_atlas.core.config import get_config
            try:
                cfg = get_config()
                from platform_atlas.capture.collectors.kubernetes import KubernetesCollector
                k8s = KubernetesCollector(
                    values_yaml_path=cfg.values_yaml_path,
                    kubectl_context=cfg.kubectl_context,
                    kubectl_namespace=cfg.kubectl_namespace,
                    use_kubectl=cfg.use_kubectl,
                )
                res = k8s.preflight()
                r = CheckResult(name=res.name, status=res.status, message=res.message, details=res.details, group="kubernetes")
            except Exception as e:
                r = CheckResult.fail("Kubernetes", f"K8s preflight error: {type(e).__name__}: {e}", group="kubernetes")
            report.results.append(r)
            jlogger.check(r.name, r.status.value, r.message, r.details)

    # ── Phase 3: Connector-based checks ──────────────────────────
    all_active: set[str] = set()
    for t in targets:
        all_active.update(t.get("modules", []))
    active_connectors = _CONNECTOR_COLLECTORS & all_active

    if active_connectors:
        connector_labels = {"mongo": "MongoDB", "redis": "Redis", "platform": "Platform OAuth", "gateway4_api": "Gateway4 API"}
        jlogger.phase("Phase 3 — Service connectors (" + ", ".join(connector_labels.get(c, c) for c in sorted(active_connectors)) + ")")
        all_checks = build_preflight_checks(include=frozenset(active_connectors))
        for mk in sorted(active_connectors):
            check_fn = all_checks.get(mk)
            if check_fn is None:
                continue
            jlogger.info(f"Connecting to {connector_labels.get(mk, mk)}…")
            try:
                res = check_fn()
                r = CheckResult(name=res.name, status=res.status, message=res.message, details=res.details, group="collectors")
            except Exception as e:
                r = CheckResult.fail(mk, f"{type(e).__name__}: {e}", group="collectors")
            report.results.append(r)
            jlogger.check(r.name, r.status.value, r.message, r.details)

    # ── Summary ───────────────────────────────────────────────────
    total = len(report.results)
    n_pass = sum(1 for r in report.results if r.status.value == "pass")
    n_fail = sum(1 for r in report.results if r.status.value == "fail")
    n_skip = sum(1 for r in report.results if r.status.value in ("skip", "warn"))

    if report.all_passed:
        jlogger.success(f"All checks passed — {n_pass} pass · {n_skip} skip/warn · {total} total")
    else:
        jlogger.warning(f"Preflight complete — {n_pass} pass · {n_fail} fail · {n_skip} skip/warn")

    return {
        "all_passed": report.all_passed,
        "target_count": len(targets),
        "passed": n_pass,
        "failed": n_fail,
    }


def run_capture_job(
    jlogger,
    *,
    session_name: str,
    headless: bool = True,
    run_aggregations: bool = True,
    pipeline_names: list[str] | None = None,
    resume: bool = True,
) -> dict[str, Any]:
    """Run capture for a session and write the result to its capture file.

    When ``run_aggregations`` is True (default) and the active tier is Extended
    with MongoDB configured, the MongoDB operational pipelines from
    ``~/.atlas/pipelines/`` are executed after capture and their results saved
    to the session's ``operational_data_file``. Standard tier always skips
    aggregations regardless of the flag — MongoDB isn't reachable there.

    ``pipeline_names``: when provided only those pipelines are executed. Pass
    ``None`` (the default) to run every pipeline in the pipelines directory.
    """
    from platform_atlas.capture.capture_engine import run_capture
    from platform_atlas.core.session_manager import get_session_manager, SessionStage

    _restore_session_context(session_name, jlogger)

    mgr = get_session_manager()
    session = mgr.get(session_name)

    # Capture checkpoint — create for this session so incremental results
    # survive an interrupted run. If resume=False the user chose "Start over"
    # on the banner, so clear any existing checkpoint first.
    from platform_atlas.capture.checkpoint import CaptureCheckpoint
    _checkpoint = CaptureCheckpoint(session.directory)
    if _checkpoint.exists:
        _done = _checkpoint.completed_modules()
        if resume:
            jlogger.info(
                f"Incomplete capture found — resuming from checkpoint "
                f"({len(_done)} module(s) already collected: {', '.join(_done)})"
            )
        else:
            jlogger.info(
                f"Starting over — clearing checkpoint "
                f"({len(_done)} module(s) from interrupted run discarded)"
            )
            _checkpoint.clear()

    jlogger.phase(f"Capture · session '{session_name}'")

    # Resolve and display tier + targets before starting
    try:
        from platform_atlas.core.context import ctx
        config = ctx().config
        tier = config.tier
        tier_label = "Standard (Platform OAuth + Gateway API only)" if tier == "standard" else "Extended (SSH · MongoDB · Redis · Gateways · System)"
        jlogger.info(f"Tier: {tier_label}")
        targets = list(config.targets)
        jlogger.info(f"Targets: {len(targets)} configured")
        for t in targets:
            role = t.get("role", t.get("name", "?"))
            transport = t.get("transport", "ssh")
            host = t.get("host", "")
            mods = t.get("modules", [])
            loc = f" @ {host}" if host else ""
            mod_str = f" — modules: {', '.join(mods)}" if mods else ""
            jlogger.info(f"  · {role} via {transport}{loc}{mod_str}")
    except Exception as _e:
        jlogger.info(f"Could not resolve target config: {_e}")

    jlogger.info("Starting data collection — this may take 1–3 minutes depending on environment size…")
    jlogger.info("Atlas is now connecting to each target and running collection modules in sequence.")
    jlogger.info("Collector progress is buffered while the underlying engine runs; post-collection events follow.")

    # Raw-capture debug export: env-persistent flag only (no CLI in the WebUI
    # context). When enabled, drops 01_raw_capture.json next to the regular
    # capture file so rule authors can trace dot-path values.
    _raw_callback = None
    try:
        from platform_atlas.core.context import ctx as _ctx_for_raw
        if getattr(_ctx_for_raw().config, "debug_export_raw_capture", False):
            _raw_path = session.capture_file.parent / "01_raw_capture.json"

            def _write_raw(structured_data: dict) -> None:
                _raw_path.parent.mkdir(parents=True, exist_ok=True)
                _raw_path.write_text(
                    json.dumps(structured_data, indent=2, default=str, ensure_ascii=False),
                    encoding="utf-8",
                )
                jlogger.info(f"  [debug] Raw capture written → {_raw_path.name}")
            _raw_callback = _write_raw
    except Exception as _e:  # noqa: BLE001
        jlogger.info(f"Raw-capture toggle resolution failed (continuing without): {_e}")

    raw = run_capture(headless=headless, on_raw_capture=_raw_callback, checkpoint=_checkpoint)

    # Summarize what was collected — emit each fact as its own event so the
    # timeline reads like a play-by-play instead of a single dense paragraph.
    jlogger.info("Collection pass complete — analyzing returned data…")
    atlas_meta = (raw.get("_atlas") or {}).get("metadata") or {}
    modules_ran = atlas_meta.get("modules_ran") or []
    if modules_ran:
        jlogger.info(f"Modules run: {len(modules_ran)} total")
        for m in modules_ran:
            jlogger.info(f"  ✓ {m}")
    sections = [k for k in raw if not k.startswith("_") and isinstance(raw[k], dict) and raw[k]]
    if sections:
        jlogger.info(f"Data sections captured: {len(sections)}")
        for s in sections:
            section = raw[s]
            keys = len(section) if isinstance(section, dict) else 0
            jlogger.info(f"  · {s} ({keys} key{'s' if keys != 1 else ''})")
    else:
        jlogger.warning("No data sections captured — check targets and credentials.")

    if "errors" in raw and not any(k for k in raw if k != "errors"):
        target_errors = raw.get("errors", [])
        if target_errors:
            jlogger.error("No modules could be initialized — target connection errors:")
            for target_name, err_msg in target_errors:
                jlogger.error(f"  · {target_name}: {err_msg}")
            jlogger.error("Tip: run Preflight first to diagnose connectivity issues.")
        else:
            jlogger.error(
                "No capture modules were available to run. "
                "Check that your environment is configured, credentials are set, "
                "and run Preflight to diagnose connectivity."
            )
        raise RuntimeError("Capture failed: no modules ran — see errors above")

    jlogger.info("Reshaping flat capture into nested hierarchy…")
    jlogger.info("Pruning to ruleset scope (sections referenced by an active rule)…")

    capture_path: Path = session.capture_file
    capture_path.parent.mkdir(parents=True, exist_ok=True)
    jlogger.info(f"Serializing capture JSON → {capture_path.name}")
    raw_json = json.dumps(raw, indent=2, default=str, ensure_ascii=False)
    capture_path.write_text(raw_json, encoding="utf-8")
    size_kb = len(raw_json.encode()) // 1024
    session.mark_stage_complete(SessionStage.CAPTURE)
    _checkpoint.clear()
    jlogger.success(f"Capture saved — {capture_path.name} ({size_kb} KB)")

    # Optional follow-up: MongoDB aggregation pipelines. The CLI runs these
    # interactively after capture; here we drive them off the WebUI checkbox.
    # Standard tier never reaches Mongo, so suppress the call there.
    try:
        from platform_atlas.core.context import ctx as _ctx
        _tier = (_ctx().config.tier or "extended").strip().lower()
    except Exception:
        _tier = "extended"
    if run_aggregations and _tier != "standard":
        _run_mongo_aggregations(jlogger, session, pipeline_names=pipeline_names)
    elif _tier == "standard":
        jlogger.info("MongoDB aggregations skipped — Standard tier doesn't reach MongoDB.")
    else:
        jlogger.info("MongoDB aggregations skipped — opted out for this run.")

    return {"ok": True, "capture_file": str(capture_path)}


def _run_mongo_aggregations(
    jlogger,
    session,
    pipeline_names: list[str] | None = None,
) -> None:
    """Run MongoDB aggregation pipelines and save to ``04_operational.json``.

    WebUI-flavored counterpart to ``_collect_operational_pipelines`` in the CLI
    session handler — same behavior, but reports through ``jlogger`` instead of
    Rich so per-pipeline progress shows up live in the job stream rather than
    a single bookend phase + summary.

    ``pipeline_names``: when provided only those pipelines are executed. Pass
    ``None`` (the default) to run every pipeline in the pipelines directory.
    """
    from platform_atlas.capture.collectors.mongo import MongoCollector
    from platform_atlas.reporting.operational_engine import run_operational_pipelines

    scope = f"{len(pipeline_names)} selected" if pipeline_names else "all"
    jlogger.phase(f"MongoDB aggregation pipelines ({scope})")
    try:
        collector = MongoCollector.from_config()
    except Exception as exc:  # noqa: BLE001
        jlogger.warning(f"Could not initialize MongoDB collector: {exc}")
        return
    if collector is None:
        jlogger.info("No MongoDB URI configured — skipping operational pipelines.")
        return

    def _on_start(idx: int, total: int, pipeline) -> None:
        jlogger.info(f"  [{idx}/{total}] Running {pipeline.name} → {pipeline.collection}…")

    def _on_complete(idx: int, total: int, result) -> None:
        if result.succeeded:
            jlogger.info(
                f"  [{idx}/{total}] ✓ {result.name} — "
                f"{result.row_count} rows · {result.duration_ms:.0f} ms"
            )
        else:
            jlogger.warning(
                f"  [{idx}/{total}] ✗ {result.name} — "
                f"{result.error or 'failed'} ({result.duration_ms:.0f} ms)"
            )

    try:
        with collector:
            report = run_operational_pipelines(
                collector,
                pipeline_names=pipeline_names,
                on_pipeline_start=_on_start,
                on_pipeline_complete=_on_complete,
                use_console=False,
            )
    except Exception as exc:  # noqa: BLE001
        jlogger.warning(f"Operational pipeline collection failed: {exc}")
        return

    if report.pipeline_count == 0:
        jlogger.info("No pipeline files found in ~/.atlas/pipelines/ — nothing to run.")
        return

    try:
        report.to_json(session.operational_data_file)
    except Exception as exc:  # noqa: BLE001
        jlogger.warning(f"Could not persist operational data: {exc}")
        return

    jlogger.success(
        f"Operational data collected — {report.success_count}/{report.pipeline_count} "
        f"pipelines succeeded ({report.total_rows} rows) → {session.operational_data_file.name}"
    )


def run_validate_job(jlogger, *, session_name: str) -> dict[str, Any]:
    """Validate a session's capture against the active ruleset."""
    from platform_atlas.core.session_manager import get_session_manager, SessionStage
    from platform_atlas.validation.validation_engine import validate_from_files

    _restore_session_context(session_name, jlogger)

    mgr = get_session_manager()
    session = mgr.get(session_name)

    if not session.capture_file.exists():
        jlogger.error("No capture file for this session — run capture first.")
        return {"ok": False, "error": "no capture file"}

    jlogger.phase(f"Validation · session '{session_name}'")

    # Log what we're validating against
    try:
        from platform_atlas.core.context import ctx
        ruleset = ctx().ruleset
        if ruleset:
            ruleset_id = ruleset.get("id", "?")
            ruleset_ver = ruleset.get("version", "?")
            ruleset_profile = ruleset.get("profile", "")
            profile_str = f" [{ruleset_profile}]" if ruleset_profile else ""
            jlogger.info(f"Ruleset: {ruleset_id} v{ruleset_ver}{profile_str}")
    except Exception:
        pass

    jlogger.info("Loading capture data and applying ruleset…")
    jlogger.info("Compiling rule paths and operator chain…")
    jlogger.info("Evaluating each rule against the captured data structure…")

    df = validate_from_files(session.capture_file, headless=True)
    jlogger.info(f"Engine returned a DataFrame with {len(df)} row(s)")

    total = len(df)
    p = int((df["status"].str.upper() == "PASS").sum()) if "status" in df else 0
    f = int((df["status"].str.upper() == "FAIL").sum()) if "status" in df else 0
    s = int((df["status"].str.upper().isin(["SKIP", "N/A", "-"])).sum()) if "status" in df else 0

    jlogger.info(f"Evaluated {total} rules across all categories")

    # Per-category breakdown
    try:
        if "category" in df.columns:
            for cat, grp in df.groupby("category"):
                cp = int((grp["status"].str.upper() == "PASS").sum())
                cf = int((grp["status"].str.upper() == "FAIL").sum())
                cs = int((grp["status"].str.upper().isin(["SKIP", "N/A", "-"])).sum())
                icon = "✓" if cf == 0 else "✗"
                jlogger.info(f"  {icon} {cat}: {cp} pass · {cf} fail · {cs} skip")
    except Exception:
        pass

    if f == 0:
        jlogger.success(f"All {p} rules passed — no failures detected")
    else:
        jlogger.warning(f"Results: {p} pass · {f} fail · {s} skip ({total} total)")
        # List the failed rule IDs (up to 10)
        try:
            failed_df = df[df["status"].str.upper() == "FAIL"]
            failed_ids = list(failed_df["rule_id"].head(10)) if "rule_id" in failed_df else []
            if failed_ids:
                more = f" …and {f - 10} more" if f > 10 else ""
                jlogger.info(f"Failed rules: {', '.join(str(r) for r in failed_ids)}{more}")
        except Exception:
            pass

    out_path: Path = session.validation_file
    out_path.parent.mkdir(parents=True, exist_ok=True)
    jlogger.info(f"Persisting validation results → {out_path.name}")
    df.to_parquet(out_path, index=False)

    pass_count = int((df["status"].str.upper() == "PASS").sum()) if "status" in df else 0
    fail_count = int((df["status"].str.upper() == "FAIL").sum()) if "status" in df else 0
    skip_count = int((df["status"].str.upper().isin(["SKIP", "N/A", "-"])).sum()) if "status" in df else 0

    session.metadata.pass_count = pass_count
    session.metadata.fail_count = fail_count
    session.metadata.skip_count = skip_count
    session.metadata.total_rules = len(df)
    session.save_metadata()
    session.mark_stage_complete(SessionStage.VALIDATE)

    jlogger.success(
        f"Validation complete: {pass_count} pass · {fail_count} fail · {skip_count} skip "
        f"({len(df)} rules)"
    )
    return {"ok": True, "pass": pass_count, "fail": fail_count, "skip": skip_count}


def run_report_job(jlogger, *, session_name: str) -> dict[str, Any]:
    """Generate the three-report bundle (compliance, operational, arch)."""
    _restore_session_context(session_name, jlogger)

    from platform_atlas.core.session_manager import get_session_manager, SessionStage
    from platform_atlas.core.context import ctx
    from platform_atlas.core.paths import (
        REPORT_TEMPLATE,
        OPERATIONAL_TEMPLATE,
        ARCH_TEMPLATE,
    )
    from platform_atlas.reporting.report_renderer import (
        render_html_report,
        generate_log_sections_html,
    )
    from platform_atlas.reporting.operational_renderer import render_operational_report
    from platform_atlas.reporting.operational_engine import OperationalReport
    from platform_atlas.reporting.arch_renderer import render_arch_report
    import pandas as pd

    mgr = get_session_manager()
    session = mgr.get(session_name)

    if not session.validation_file.exists():
        jlogger.error("No validation file for this session — run validate first.")
        return {"ok": False, "error": "no validation file"}

    jlogger.phase(f"Report · session '{session_name}'")
    jlogger.info("Loading validation results from parquet…")
    df = pd.read_parquet(session.validation_file)
    jlogger.info(f"Loaded {len(df)} rule results")

    # Rehydrate metadata that doesn't survive parquet round-trip.
    if session.capture_file.exists():
        try:
            with session.capture_file.open(encoding="utf-8") as f:
                cap = json.load(f)
            atlas_meta = (cap.get("_atlas") or {}).get("metadata") or {}
            for key, val in atlas_meta.items():
                df.attrs.setdefault(key, val)
        except Exception as exc:  # noqa: BLE001
            jlogger.warning(f"Could not rehydrate metadata: {exc}")

    organization_name = df.attrs.get("organization_name", "Unknown Organization")
    ruleset_id = df.attrs.get("ruleset_id", "unknown")
    ruleset_ver = df.attrs.get("ruleset_version", "unknown")
    ruleset_profile = df.attrs.get("ruleset_profile", "")

    session_tier = getattr(session.metadata, "tier", None) or df.attrs.get("tier") or "extended"

    p_count = int((df["status"].str.upper() == "PASS").sum()) if "status" in df else 0
    f_count = int((df["status"].str.upper() == "FAIL").sum()) if "status" in df else 0
    jlogger.info(f"Rendering compliance report — {p_count} pass · {f_count} fail · org: {organization_name}")
    jlogger.info("Compiling rule rows, status pills, and remediation modals…")
    render_html_report(
        df,
        REPORT_TEMPLATE,
        output_path=session.report_file,
        title="Platform Health Report",
        subtitle=session.name,
        organization_name=organization_name,
        ruleset_version=f"{ruleset_ver} ({ruleset_profile})" if ruleset_profile else ruleset_ver,
        target_system=ruleset_id,
        modules_ran=session.metadata.modules_ran,
        tier=session_tier,
    )

    # Try to load extended results + log sections; fall back to empty structures.
    extended_results: list = df.attrs.get("extended_results") or []
    log_html = generate_log_sections_html(extended_results)

    try:
        report_kb = session.report_file.stat().st_size // 1024
        jlogger.success(f"Compliance report (03_report.html) ready — {report_kb} KB")
    except OSError:
        jlogger.success("Compliance report (03_report.html) ready")

    if session_tier == "standard":
        jlogger.info("Operational report skipped — log analysis and MongoDB pipeline data require Extended tier.")
    else:
        jlogger.info("Rendering operational report — includes log sections and MongoDB pipeline metrics…")
        has_mongo = session.operational_data_file.exists()
        mongo_report = (
            OperationalReport.from_json(session.operational_data_file)
            if has_mongo else OperationalReport(results=[])
        )
        render_operational_report(
            mongo_report,
            template_path=OPERATIONAL_TEMPLATE,
            output_path=session.operational_file,
            title="Operational Metrics Report",
            subtitle=session.name,
            organization_name=organization_name,
            log_sections_html=log_html,
            has_mongo_data=has_mongo,
            tier=session_tier,
        )

    if session_tier != "standard":
        try:
            op_kb = session.operational_file.stat().st_size // 1024
            jlogger.success(f"Operational report (04_operational.html) ready — {op_kb} KB")
        except OSError:
            jlogger.success("Operational report (04_operational.html) ready")

    # Load architecture data from the per-environment store (primary) or the
    # capture file (fallback for envs that haven't had the form filled in yet).
    try:
        from platform_atlas.core import architecture_store
        arch_env = session.metadata.environment or ""
        arch_record = architecture_store.load(arch_env)
        architecture_data = arch_record.get("completed") or {}
    except Exception:
        architecture_data = {}

    if not architecture_data and session.capture_file.exists():
        try:
            cap_raw = json.loads(session.capture_file.read_text(encoding="utf-8"))
            architecture_data = cap_raw.get("checks", {}).get("architecture_validation") or {}
        except Exception:
            pass

    if architecture_data:
        sections_found = [k for k in architecture_data if architecture_data[k]]
        jlogger.info(f"Architecture data found — {len(sections_found)} section(s): {', '.join(sections_found)}")
    else:
        jlogger.info("No architecture data found for this environment — architecture report will show placeholder content")
        jlogger.info("Fill in the Architecture form under the Audit menu to add this data.")

    jlogger.info("Rendering architecture report (05_arch.html)…")
    render_arch_report(
        extended_results,
        architecture_data,
        template_path=ARCH_TEMPLATE,
        output_path=session.arch_file,
        title="Architecture & Maintenance",
        subtitle=session.name,
        organization_name=organization_name,
        tier=session_tier,
    )

    session.mark_stage_complete(SessionStage.REPORT)
    try:
        arch_kb = session.arch_file.stat().st_size // 1024
        jlogger.success(f"Architecture report (05_arch.html) ready — {arch_kb} KB")
    except OSError:
        jlogger.success("Architecture report (05_arch.html) ready")
    jlogger.success(f"All reports generated — session: {session.name}")
    jlogger.info(f"View reports from the Reports page or open {session.directory}")
    return {"ok": True, "report_file": str(session.report_file)}


def run_full_pipeline_job(
    jlogger,
    *,
    session_name: str,
    run_aggregations: bool = True,
    pipeline_names: list[str] | None = None,
) -> dict[str, Any]:
    """Capture → Validate → Report in a single job.

    Wraps each sub-runner with a "Stage N of 3" phase header so the timeline
    reads as a coherent narrative instead of three loosely-glued runs, and
    closes with a recap that surfaces the validation totals + per-stage timing.
    Aggregations honor the checkbox passed in from the form.

    ``pipeline_names``: when provided only those pipelines are executed during
    the Capture stage. Pass ``None`` to run all discovered pipelines.
    """
    from time import perf_counter

    t_start = perf_counter()
    if run_aggregations and pipeline_names:
        n = len(pipeline_names)
        aggs_note = f"with {n} selected MongoDB pipeline{'s' if n != 1 else ''}"
    elif run_aggregations:
        aggs_note = "with MongoDB aggregations"
    else:
        aggs_note = "without aggregations"
    jlogger.phase(f"Full pipeline · session '{session_name}' ({aggs_note})")
    jlogger.info("Three stages: Capture → Validate → Report. Each stage's events will follow.")

    # ── Stage 1: Capture ──────────────────────────────────────────
    t_capture = perf_counter()
    jlogger.phase("Stage 1 of 3 — Capture")
    cap = run_capture_job(
        jlogger,
        session_name=session_name,
        headless=True,
        run_aggregations=run_aggregations,
        pipeline_names=pipeline_names,
    )
    cap_dur = perf_counter() - t_capture
    if not cap.get("ok"):
        jlogger.error(
            f"Pipeline halted at Capture stage after {cap_dur:.1f}s — "
            "see errors above. Validate and Report were skipped."
        )
        return cap
    jlogger.info(f"Stage 1 complete in {cap_dur:.1f}s — proceeding to validation")

    # ── Stage 2: Validate ─────────────────────────────────────────
    t_validate = perf_counter()
    jlogger.phase("Stage 2 of 3 — Validate")
    val = run_validate_job(jlogger, session_name=session_name)
    val_dur = perf_counter() - t_validate
    if not val.get("ok"):
        jlogger.error(
            f"Pipeline halted at Validate stage after {val_dur:.1f}s — "
            "see errors above. Report was skipped."
        )
        return val
    jlogger.info(f"Stage 2 complete in {val_dur:.1f}s — proceeding to report generation")

    # ── Stage 3: Report ───────────────────────────────────────────
    t_report = perf_counter()
    jlogger.phase("Stage 3 of 3 — Report")
    rep = run_report_job(jlogger, session_name=session_name)
    rep_dur = perf_counter() - t_report
    if not rep.get("ok"):
        jlogger.error(f"Report stage failed after {rep_dur:.1f}s — see errors above.")
        return {"capture": cap, "validate": val, "report": rep}

    # ── Recap ─────────────────────────────────────────────────────
    total_dur = perf_counter() - t_start
    p, f, s = val.get("pass", 0), val.get("fail", 0), val.get("skip", 0)
    jlogger.phase("Pipeline complete")
    if f == 0:
        jlogger.success(
            f"All three stages succeeded · {p} rules passed · {s} skipped "
            f"({p + f + s} total)"
        )
    else:
        jlogger.warning(
            f"Pipeline finished with rule failures · {p} pass · {f} fail · {s} skip"
        )
    jlogger.info(
        f"Timing — capture {cap_dur:.1f}s · validate {val_dur:.1f}s · "
        f"report {rep_dur:.1f}s · total {total_dur:.1f}s"
    )
    jlogger.info(f"Open the report from the session page or the Reports tab.")

    return {"capture": cap, "validate": val, "report": rep}


def run_support_bundle_job(
    jlogger,
    *,
    ticket: str = "",
    description: str = "",
    log_days: int = 7,
) -> dict[str, Any]:
    """Collect Platform health + logs and pack into a support bundle ZIP.

    Mirrors the CLI's handle_support_bundle but streams progress via jlogger
    instead of Rich so the WebUI gets live events. The ZIP is written to a
    temp file; the download route reads ``result["bundle_path"]`` to serve it
    as a single-use FileResponse.
    """
    import os
    import tempfile
    import warnings
    from datetime import datetime, timezone
    from pathlib import Path

    from platform_atlas.core.context import ctx
    from platform_atlas.core.handlers.support_bundle import (
        _build_zip,
        _collect_logs_and_system,
        _collect_platform_health,
        _redact_config,
    )

    config = ctx().config
    is_extended = not ctx().is_standard
    env_name = getattr(config, "active_environment", None) or "—"
    mode_label = "Extended" if is_extended else "Standard"

    jlogger.phase(f"Support Bundle · {mode_label} tier · {env_name} environment")
    if ticket:
        jlogger.info(f"Ticket: {ticket}")
    if description:
        jlogger.info(f"Description: {description}")
    jlogger.info(f"Log window: last {log_days} days")

    errors: list[str] = []
    collected: list[str] = []

    # ── Platform health endpoints ─────────────────────────────────
    jlogger.info("Collecting Platform health endpoints…")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            platform_health = _collect_platform_health(log_days)
        ok_count = sum(
            1 for v in platform_health.values()
            if not (isinstance(v, dict) and v.get("status") == "failed")
        )
        total_count = len(platform_health)
        collected.append(f"Platform health ({ok_count}/{total_count} endpoints)")
        for name, v in platform_health.items():
            if isinstance(v, dict) and v.get("status") == "failed":
                errors.append(f"Platform/{name}: {v.get('error', 'failed')}")
        jlogger.success(f"{ok_count}/{total_count} health endpoints collected")
    except Exception as exc:  # noqa: BLE001
        platform_health = {}
        errors.append(f"Platform health collection failed: {exc}")
        jlogger.warning(f"Platform health collection failed — {exc}")

    # ── SSH logs + system info (Extended only) ────────────────────
    logs: dict = {}
    system: dict = {}
    raw_logs: dict = {}
    if is_extended:
        jlogger.info("Collecting SSH logs and system info (Extended tier)…")
        try:
            logs, system, raw_logs = _collect_logs_and_system(log_days, progress_cb=jlogger.info)
            if logs:
                ok_logs = sum(
                    1 for v in logs.values()
                    if not (isinstance(v, dict) and v.get("status") == "failed")
                )
                collected.append(f"Logs ({ok_logs}/{len(logs)} sources)")
                jlogger.success(f"{ok_logs}/{len(logs)} log sources collected")
            if system and "_error" not in system and "_tier_error" not in system:
                collected.append("System info")
                jlogger.success("System info collected")
            if raw_logs:
                collected.append(f"Raw logs ({len(raw_logs)} file(s))")
                jlogger.success(f"{len(raw_logs)} raw log file(s) transferred to raw_logs/")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Log/system collection failed: {exc}")
            jlogger.warning(f"Log/system collection failed — {exc}")
    else:
        jlogger.info("SSH logs and system info skipped — Standard tier collects Platform health + Atlas config only.")

    # ── Atlas config (redacted) ───────────────────────────────────
    jlogger.info("Bundling Atlas config (redacted)…")
    try:
        config_redacted = _redact_config(config)
        collected.append("Atlas config (redacted)")
        jlogger.success("Config snapshot included (credentials redacted)")
    except Exception as exc:  # noqa: BLE001
        config_redacted = {"error": str(exc)}
        errors.append(f"Config redaction failed: {exc}")
        jlogger.warning(f"Config redaction failed — {exc}")

    # ── Assemble ZIP ──────────────────────────────────────────────
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    ticket_slug = ticket.replace("/", "-").replace(" ", "-") if ticket else ""
    bundle_name = (
        f"atlas-support-bundle-{ticket_slug}-{timestamp}.zip"
        if ticket_slug else
        f"atlas-support-bundle-{timestamp}.zip"
    )
    try:
        from platform_atlas.core._version import __version__ as _atlas_ver
    except Exception:
        _atlas_ver = None
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ticket": ticket or None,
        "description": description or None,
        "environment": env_name,
        "log_window_days": log_days,
        "mode": "extended" if is_extended else "standard",
        "atlas_version": _atlas_ver,
        "collected": collected,
        "errors": errors,
    }

    jlogger.info("Assembling ZIP…")
    try:
        bundle_bytes = _build_zip(platform_health, logs, system, raw_logs, config_redacted, manifest, folder=bundle_name.removesuffix(".zip"))
    except Exception as exc:  # noqa: BLE001
        jlogger.error(f"Failed to build ZIP: {exc}")
        return {"ok": False, "error": str(exc)}

    # Write to a temp file — the download route reads result["bundle_path"]
    # and serves it as a single-use FileResponse with a cleanup BackgroundTask.
    try:
        fd, tmp = tempfile.mkstemp(suffix=".zip", prefix="atlas-bundle-")
        os.close(fd)
        bundle_path = Path(tmp)
        bundle_path.write_bytes(bundle_bytes)
    except Exception as exc:  # noqa: BLE001
        jlogger.error(f"Failed to write bundle to temp file: {exc}")
        return {"ok": False, "error": str(exc)}

    size_kb = round(len(bundle_bytes) / 1024, 1)
    jlogger.success(f"Bundle ready — {bundle_name} ({size_kb} KB)")
    if errors:
        jlogger.warning(f"{len(errors)} collection error(s) logged in manifest.json — bundle still complete")

    return {
        "ok": True,
        "bundle_path": str(bundle_path),
        "bundle_name": bundle_name,
        "size_kb": size_kb,
        "collected": collected,
        "errors": errors,
    }
