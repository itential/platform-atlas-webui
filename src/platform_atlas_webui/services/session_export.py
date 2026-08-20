"""
Session export service — the WebUI twin of ``platform-atlas session export``.

Packages a finished session into a delivery archive (``report.html`` +
machine-readable ``report.json`` + metadata + README) that the user attaches
to an Itential ER ticket. The heavy lifting is reused
verbatim from the core library so the WebUI archive is byte-for-byte identical
to the one the CLI produces — this module only swaps the CLI's interactive
prompt for plain arguments and its current-working-directory output for a
stable, discoverable location.

Two deliberate WebUI choices, both so the modal can be friendly about the
result:
  * the archive lands in ``~/.atlas/exports/`` (persistent — an export is a
    deliverable the user needs to find again, unlike the support bundle's
    single-use temp file); and
  * the function returns a summary dict the route serializes straight to the
    modal (archive name, on-disk path, size, and a contents list).
"""

from __future__ import annotations

import logging
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from platform_atlas.core.paths import ATLAS_HOME

logger = logging.getLogger(__name__)

# Stable, per-user home for generated delivery archives. Created on demand.
EXPORTS_DIR = ATLAS_HOME / "exports"

# Archive formats the core packager understands (mirrors the CLI's --format).
VALID_FORMATS = ("zip", "tar.gz")


def build_session_export(
    name: str,
    *,
    archive_format: str = "zip",
    include_debug: bool = False,
) -> dict[str, Any]:
    """Package session ``name`` into ``~/.atlas/exports/`` and describe the result.

    Mirrors ``handle_session_export``: generate ``report.json`` into a throwaway
    temp dir, then hand it to ``SessionManager.export`` with an organization-aware
    archive name (``ATLAS-<org>-<session>-<date>``). ``include_debug`` is the
    single switch that also bundles troubleshooting files (session.log,
    01_capture.json, debug.log); off by default keeps sensitive data out.

    Returns ``{ok, archive_name, archive_path, size_bytes, contents,
    organization, environment, include_debug}``. Raises ``ValueError`` on an
    unsupported format and ``SessionError`` (from the core manager) on an
    unknown session.
    """
    if archive_format not in VALID_FORMATS:
        raise ValueError(f"Unsupported archive format: {archive_format!r}")

    # Reused straight from the CLI handler so the archive is identical to the
    # one `platform-atlas session export` writes — same report.json assembly,
    # same packager, same README.
    from platform_atlas.core.session_manager import get_session_manager
    from platform_atlas.core.handlers.session import _generate_report_json, _slugify

    mgr = get_session_manager()
    session = mgr.get(name)  # raises SessionError if the session is unknown

    org_slug = _slugify(session.metadata.organization_name) or "Unknown-Org"
    base_name = f"ATLAS-{org_slug}-{name}-{datetime.now():%Y%m%d}"

    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = EXPORTS_DIR / f"{base_name}.{archive_format}"

    with tempfile.TemporaryDirectory() as gen_dir:
        report_json_path = _generate_report_json(session, Path(gen_dir))
        mgr.export(
            name,
            output_path,
            archive_format=archive_format,
            include_debug=include_debug,
            report_json_path=report_json_path,
            arc_dir_name=base_name,
        )
        report_json_included = report_json_path is not None

    # Contents summary — mirrors the CLI's _print_export_summary so the modal
    # can list exactly what landed in the archive.
    contents: list[str] = []
    if session.report_file.exists():
        contents.append("Report")
    if report_json_included:
        contents.append("report.json")
    contents.append("Session metadata")
    if include_debug:
        contents.append("Debug (logs + raw capture)")

    size_bytes = output_path.stat().st_size
    logger.info("Session '%s' exported to %s (%d bytes)", name, output_path, size_bytes)

    return {
        "ok": True,
        "archive_name": output_path.name,
        "archive_path": str(output_path),
        "size_bytes": size_bytes,
        "contents": contents,
        "organization": session.metadata.organization_name or "",
        "environment": session.metadata.environment or "",
        "include_debug": include_debug,
    }
