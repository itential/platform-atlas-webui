"""Architecture form route — per-environment collection and persistence.

Architecture answers live at ``~/.atlas/architecture/<env>.json`` (one file
per environment). The route resolves the target env from ``?env=<name>``,
falling back to the active environment, falling back to the ``_default``
bucket. ``/architecture/copy`` clones a source env's answers onto a
destination — the "prod and dev are mostly the same" workflow.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from platform_atlas.core import architecture_store

from platform_atlas_webui.security.scriptjson import script_json
from platform_atlas.core.config import environment_effective_tier
from platform_atlas.reporting.arch_warnings import compute_arch_warnings

from platform_atlas_webui.dependencies import get_atlas_context, get_templates, template_context
from platform_atlas_webui.services import environments as env_svc
from platform_atlas_webui.services import runners
from platform_atlas_webui.services.jobs import get_registry

router = APIRouter(prefix="/architecture", tags=["architecture"])
_templates = get_templates()
logger = logging.getLogger(__name__)


# Architecture state is small structured form output — cap at 256 KB so a
# malicious or runaway client can't fill the disk via this endpoint.
_MAX_ARCH_BODY_BYTES = 256 * 1024
_ALLOWED_STATUSES = frozenset({"in_progress", "complete", "skipped"})


def _resolve_target_env(requested: str | None) -> str:
    """Pick the env we're acting on for this request.

    Resolution order: explicit ``?env=`` value, active environment, then
    ``_default``. The store's name validation runs at write time, so any
    unsafe value here errors there before touching disk.
    """
    if requested:
        return requested.strip()
    try:
        atlas = get_atlas_context()
        if atlas.active_environment:
            return atlas.active_environment
    except HTTPException:
        pass
    return architecture_store.DEFAULT_ENV_KEY


def _resolve_arch_tier(target_env: str) -> tuple[bool, str]:
    """Resolve ``(is_saas, saas_gateway_kind)`` for the env the form is editing.

    Keyed off the *target* env (not the active tier) so the env switcher renders
    the correct layout for whichever env is selected. Fails open to
    ``(False, "")`` — the full form — on any error, which is harmless for
    Standard/Extended and the only safe default when the env can't be read.
    """
    try:
        env_rec = env_svc.get_environment(target_env)
    except Exception:
        return (False, "")
    if not env_rec or env_rec.get("tier") != "saas":
        return (False, "")
    kind = str((env_rec.get("data") or {}).get("saas_gateway_kind") or "").strip().lower()
    return (True, kind)


def _can_autofill(target_env: str) -> bool:
    """Whether the auto-detect button should be offered for ``target_env``.

    Extended tier only, and only when it's the *active* environment — the
    autofill probe runs against the active context's resolved SSH targets
    (credentials included), and building that resolved target list for an
    arbitrary non-active named environment is a separate, higher-risk change
    this doesn't take on. Fails closed (hides the button) on any error.
    """
    try:
        if environment_effective_tier(target_env) != "extended":
            return False
        atlas = get_atlas_context()
        return bool(target_env) and target_env == (atlas.active_environment or "")
    except Exception:
        return False


@router.get("", response_class=HTMLResponse)
async def architecture_form(request: Request, env: str | None = None) -> HTMLResponse:
    """Render the architecture overview form for ``env`` (active env if blank)."""
    # One-shot legacy migration: silently lifts a pre-1.7.x global
    # architecture.json into the active env's bucket.
    await run_in_threadpool(architecture_store.migrate_legacy)

    target_env = _resolve_target_env(env)

    # If the user has an existing env whose name predates our stricter
    # validator (e.g. ``Local Staging TEST-ONLY!``), the architecture store
    # would raise on load. Show a graceful empty state instead of 500ing.
    if not architecture_store.is_safe_env_name(target_env):
        available_envs = await run_in_threadpool(env_svc.list_environments)
        return _templates.TemplateResponse(
            request,
            "architecture/index.html",
            template_context(
                request,
                target_env=target_env,
                arch_data_json="{}",
                arch_topology_seed_json="{}",
                arch_suggested_skips_json="[]",
                can_autofill=False,
                arch_autofill_at="",
                available_envs=available_envs,
                envs_with_data=[],
                copy_sources=[],
                show_copy_ui=False,
                copied_from="",
                copied_count="",
                copy_error=(
                    f"The environment name {target_env!r} contains characters that "
                    "aren't allowed (only letters, digits, spaces, dots, hyphens, "
                    "and underscores). Rename or delete this environment to use the "
                    "Architecture Overview here."
                ),
                arch_is_saas=False,
                saas_gateway_kind="",
            ),
        )

    arch_data = await run_in_threadpool(architecture_store.load, target_env)
    arch_warnings = await run_in_threadpool(
        compute_arch_warnings, arch_data.get("completed", {})
    )
    available_envs = await run_in_threadpool(env_svc.list_environments)
    envs_with_data = await run_in_threadpool(architecture_store.list_envs_with_data)

    # ``envs_with_data`` reflects what's on disk under ~/.atlas/architecture/,
    # which can include stale records from envs the user has since deleted.
    # Intersect with the set of valid sources: currently-defined envs plus the
    # legacy ``_default`` bucket (data filled in before any env was named).
    available_env_names = {e["name"] for e in available_envs}
    valid_source_names = available_env_names | {architecture_store.DEFAULT_ENV_KEY}
    copy_sources = [
        name for name in envs_with_data
        if name != target_env and name in valid_source_names
    ]
    # Copy UI only makes sense when:
    #   1) the destination (target_env) is a currently-defined env — the POST
    #      handler refuses to write to anything else, so showing the form
    #      would just produce a confusing redirect-with-error.
    #   2) at least one valid source other than target_env exists. That's
    #      either another named env, or the legacy ``_default`` bucket
    #      (handles the "I used Atlas before naming an env, now want to
    #      migrate that data to production" workflow).
    has_default_data = architecture_store.DEFAULT_ENV_KEY in envs_with_data
    target_is_real = target_env in available_env_names
    show_copy_ui = target_is_real and (
        len(available_env_names) > 1 or has_default_data
    )

    # Optional flash params from a successful POST /copy redirect.
    copied_from = (request.query_params.get("copied_from") or "").strip()
    copied_count = (request.query_params.get("copied_count") or "").strip()
    copy_error = (request.query_params.get("copy_error") or "").strip()

    # Resolve the *target* env's tier + gateway kind so the form can render a
    # gateway-scoped (SaaS) layout for the env being edited. This is keyed off
    # target_env (the env switcher can point at a non-active env), NOT the
    # active tier the nav's ``is_saas`` uses. Fails open to the full form.
    arch_is_saas, saas_gateway_kind = _resolve_arch_tier(target_env)

    # Free, zero-cost topology/config-derived suggestions (no SSH) — the same
    # source the CLI's browser form seeds from. Layered client-side under the
    # user's own completed answers and any prior SSH auto-detect run (already
    # present in arch_data.autofill). "Suggested skip" sections are ones the
    # topology confidently rules out (e.g. Gateway4-only ⇒ no Gateway5),
    # minus anything already answered (never second-guessed) or already
    # explicitly skipped by the user (that's applied — and rendered — via
    # SAVED.skipped already; re-listing it here would badge the user's own
    # decision as if Atlas had made it).
    from platform_atlas.capture.collectors.manual import TopologyHints
    topology_hints = await run_in_threadpool(TopologyHints.from_config, target_env)
    completed_sections = set(arch_data.get("completed") or {})
    already_skipped = set(arch_data.get("skipped") or [])
    suggested_skips = sorted(
        set(topology_hints.suggested_section_skips()) - completed_sections - already_skipped
    )

    return _templates.TemplateResponse(
        request,
        "architecture/index.html",
        template_context(
            request,
            target_env=target_env,
            arch_data_json=script_json(arch_data),
            arch_topology_seed_json=script_json(topology_hints.as_seed_dict()),
            arch_suggested_skips_json=script_json(suggested_skips),
            can_autofill=_can_autofill(target_env),
            arch_autofill_at=arch_data.get("autofill_at") or "",
            available_envs=available_envs,
            envs_with_data=envs_with_data,
            copy_sources=copy_sources,
            show_copy_ui=show_copy_ui,
            copied_from=copied_from,
            copied_count=copied_count,
            copy_error=copy_error,
            arch_warnings=arch_warnings,
            arch_is_saas=arch_is_saas,
            saas_gateway_kind=saas_gateway_kind,
        ),
    )


def _sanitize_arch_payload(payload: object) -> dict:
    """Validate shape and coerce to a strict, render-safe structure."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")

    completed = payload.get("completed", {})
    skipped = payload.get("skipped", [])
    status = payload.get("status", "in_progress")

    if not isinstance(completed, dict):
        raise HTTPException(status_code=400, detail="'completed' must be an object")
    if not isinstance(skipped, list):
        raise HTTPException(status_code=400, detail="'skipped' must be an array")
    if not isinstance(status, str) or status not in _ALLOWED_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"'status' must be one of: {', '.join(sorted(_ALLOWED_STATUSES))}",
        )

    # Section names: only short alnum/dash/underscore identifiers from the form.
    safe_completed: dict[str, dict] = {}
    for section_name, section_value in completed.items():
        if not isinstance(section_name, str) or len(section_name) > 64:
            raise HTTPException(status_code=400, detail="Invalid section name")
        if not all(c.isalnum() or c in "_-" for c in section_name):
            raise HTTPException(status_code=400, detail="Invalid section name")
        if not isinstance(section_value, dict):
            raise HTTPException(status_code=400, detail="Section value must be an object")
        safe_completed[section_name] = section_value

    safe_skipped: list[str] = []
    for entry in skipped:
        if not isinstance(entry, str) or len(entry) > 64:
            raise HTTPException(status_code=400, detail="Invalid skipped entry")
        safe_skipped.append(entry)

    result = {"completed": safe_completed, "skipped": safe_skipped, "status": status}

    # Optional persisted progress counts (filled / total fields), used to render
    # the "X / Y" bar on other surfaces. Coerced to sane non-negative ints; a
    # malformed value is simply dropped rather than rejected.
    progress = payload.get("progress")
    if isinstance(progress, dict):
        try:
            filled = max(0, min(int(progress.get("filled", 0)), 100000))
            total = max(0, min(int(progress.get("total", 0)), 100000))
            result["progress"] = {"filled": filled, "total": total}
        except (TypeError, ValueError):
            pass

    return result


@router.post("/autofill")
async def kick_architecture_autofill(env: str | None = None) -> RedirectResponse:
    """Kick off the best-effort SSH auto-detect pass as a background job.

    Redirects to the job's own page, which streams progress via the same
    generic ``check`` event rendering every other job page uses. The job's
    "Continue" button (via ``metadata.return_url``) brings the user back to
    this environment's architecture page, where the (now-populated)
    ``autofill`` bucket flows through on the next render.
    """
    target_env = _resolve_target_env(env)
    if not _can_autofill(target_env):
        atlas = get_atlas_context()
        active_env = atlas.active_environment or ""
        if target_env != active_env:
            detail = (
                f"Auto-detect runs against the active environment's SSH targets. "
                f"Switch to '{target_env}' under Environments first, then try again."
            )
        else:
            detail = "Architecture auto-detect is available in the Extended tier only."
        raise HTTPException(status_code=400, detail=detail)

    from urllib.parse import quote

    reg = get_registry()
    record = await reg.submit(
        "architecture autofill",
        runners.run_architecture_autofill_job,
        environment=target_env,
        metadata={"return_url": f"/architecture?env={quote(target_env, safe='')}"},
    )
    return RedirectResponse(url=f"/jobs/{record.id}", status_code=303)


@router.get("/warnings")
async def get_arch_warnings(request: Request, env: str | None = None) -> JSONResponse:
    """Return architecture warnings for the active (or requested) environment."""
    target_env = _resolve_target_env(env)
    arch_data = await run_in_threadpool(architecture_store.load, target_env)
    if not arch_data.get("completed"):
        return JSONResponse({"warnings": [], "count": 0})
    warnings = await run_in_threadpool(
        compute_arch_warnings, arch_data.get("completed", {})
    )
    return JSONResponse({
        "warnings": [
            {
                "category": w.category,
                "severity": w.severity,
                "component": w.component,
                "message": w.message,
                "detail": w.detail,
            }
            for w in warnings
        ],
        "count": len(warnings),
    })


@router.post("/save")
async def save_architecture(request: Request, env: str | None = None) -> JSONResponse:
    """Persist architecture data to ~/.atlas/architecture/<env>.json."""
    body = await request.body()
    if len(body) > _MAX_ARCH_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Architecture payload too large")

    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    data = _sanitize_arch_payload(payload)
    target_env = _resolve_target_env(env)

    try:
        await run_in_threadpool(architecture_store.save, target_env, data)
    except (OSError, ValueError) as exc:
        logger.error("Failed to write architecture file for env=%s: %s", target_env, exc)
        raise HTTPException(status_code=500, detail="Could not save architecture data") from exc

    return JSONResponse({"ok": True, "status": data["status"], "environment": target_env})


@router.post("/copy")
async def copy_architecture(
    src_env: str = Form(...),
    dst_env: str = Form(...),
    overwrite: str = Form(""),
):
    """Copy answers from ``src_env`` onto ``dst_env``.

    Refuses to overwrite an existing destination unless ``overwrite=1`` is
    posted, so the user can't accidentally clobber answers they already
    spent time filling in. Both envs must reference a currently-defined
    environment (or the ``_default`` legacy bucket on the source side) — the
    UI dropdown enforces this, but a hand-crafted POST otherwise would let a
    user create orphan ``architecture/<unknown>.json`` files or copy from
    stale data of a deleted env.
    """
    from urllib.parse import quote

    src_env = (src_env or "").strip()
    dst_env = (dst_env or "").strip()
    if not src_env or not dst_env:
        raise HTTPException(status_code=400, detail="Both src_env and dst_env are required")
    if src_env == dst_env:
        raise HTTPException(status_code=400, detail="Source and destination must differ")

    # Validate both endpoints against the currently-defined env set. ``_default``
    # is allowed as a *source* only — it's a fallback bucket from before any
    # env was named, and migrating that data to a real env is a real workflow.
    available = await run_in_threadpool(env_svc.list_environments)
    available_names = {e["name"] for e in available}
    if dst_env not in available_names:
        # Friendly redirect rather than a raw 400 — the user came from /copy
        # via a form, so re-render their architecture page with an inline
        # error, but only if dst is a valid env name they can recover to.
        raise HTTPException(
            status_code=400,
            detail=(
                f"Destination environment '{dst_env}' is not defined. "
                "Create it under Environments first."
            ),
        )
    if src_env != architecture_store.DEFAULT_ENV_KEY and src_env not in available_names:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Source environment '{src_env}' is not defined. "
                "Choose a currently-existing environment to copy from."
            ),
        )

    # Confirm the source actually has answers before we touch anything.
    src_has_data = await run_in_threadpool(architecture_store.has_data, src_env)
    if not src_has_data:
        return RedirectResponse(
            url=(
                f"/architecture?env={quote(dst_env, safe='')}"
                f"&copy_error={quote(f'Source environment {src_env!r} has no answers to copy.', safe='')}"
            ),
            status_code=303,
        )

    if (overwrite or "").strip() != "1":
        existing = await run_in_threadpool(architecture_store.has_data, dst_env)
        if existing:
            # Send the user back to the form so we can render a real
            # confirmation prompt instead of a raw 409 error page. The UI
            # JS will trigger a confirm() and resubmit with overwrite=1.
            return RedirectResponse(
                url=(
                    f"/architecture?env={quote(dst_env, safe='')}"
                    f"&copy_error={quote(f'Destination {dst_env!r} already has answers — confirm overwrite to replace them.', safe='')}"
                ),
                status_code=303,
            )

    try:
        result = await run_in_threadpool(architecture_store.copy, src_env, dst_env)
    except ValueError as exc:
        return RedirectResponse(
            url=(
                f"/architecture?env={quote(dst_env, safe='')}"
                f"&copy_error={quote(str(exc), safe='')}"
            ),
            status_code=303,
        )

    copied_count = len(result.get("completed") or {})
    return RedirectResponse(
        url=(
            f"/architecture?env={quote(dst_env, safe='')}"
            f"&copied_from={quote(src_env, safe='')}"
            f"&copied_count={copied_count}"
        ),
        status_code=303,
    )
