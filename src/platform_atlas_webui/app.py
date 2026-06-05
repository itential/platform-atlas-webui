"""
FastAPI application factory.

The WebUI is a FastAPI app that imports `platform_atlas` as a library —
there's no shell-out to the CLI. On startup we initialize the Atlas
context exactly once so routes can reuse it.

When ``~/.atlas/config.json`` is missing (typical first run), every
request is redirected to ``/setup`` until the user fills out the
bootstrap form. This keeps the dashboard from crashing before
configuration exists and gives a clear path to first audit.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import Response as _StarletteResponse

from platform_atlas.core.context import init_context
from platform_atlas.core._version import __version__ as ATLAS_VERSION

from platform_atlas_webui.config import WebUISettings
from platform_atlas_webui.dependencies import get_templates, _ASSET_VERSION
from platform_atlas_webui.routes import register_routes
from platform_atlas_webui.security.audit import AuditMiddleware
from platform_atlas_webui.security.headers import SecurityHeadersMiddleware
from platform_atlas_webui.security.csrf import validate_csrf_token
from platform_atlas_webui.security.tokens import COOKIE_NAME, validate_session_cookie
from platform_atlas_webui.services.setup import is_initialized


logger = logging.getLogger(__name__)


_STATIC_DIR = Path(__file__).parent / "static"

# Templates instance for the themed error page. Built once at import — the
# error handler must never depend on the (heavier, fail-prone) per-request
# template_context() so a 500 can't trigger a second 500 while rendering.
_ERROR_TEMPLATES = get_templates()

# Friendly fallback copy keyed by status code. ``exc.detail`` (when it's a
# real human message and not the bare status phrase) takes precedence as the
# body message; these supply the heading and a default body line.
_ERROR_COPY: dict[int, tuple[str, str]] = {
    403: ("Access denied", "You don't have permission to view this page."),
    404: ("Page not found", "The page you're looking for doesn't exist or has moved."),
    405: ("Method not allowed", "That action isn't supported on this page."),
    500: ("Something went wrong", "Atlas hit an unexpected error. Try again, or head back to the dashboard."),
}
_ERROR_COPY_DEFAULT: tuple[str, str] = (
    "Something went wrong",
    "Atlas couldn't complete that request.",
)


def _wants_html_error(request: Request) -> bool:
    """True when a browser navigation should get the themed HTML error page.

    HTMX swaps and JSON/API clients fall through to the default JSON body so
    their fetch/swap logic is unaffected.
    """
    accept = request.headers.get("accept", "")
    return "text/html" in accept and request.headers.get("hx-request") != "true"

# Paths the setup-redirect middleware should leave alone.
_SETUP_BYPASS_PREFIXES: tuple[str, ...] = (
    "/setup",
    "/static",
    "/health",
    "/_docs",
    "/openapi.json",
    "/api/settings",  # appearance prefs are usable before bootstrap
)

# Paths that must be reachable without an auth cookie.
# /setup is included so first-run users can reach the wizard before any
# session cookie exists — without this, /setup -> 401 -> redirect-loop deadlock.
_AUTH_BYPASS_PREFIXES: tuple[str, ...] = (
    "/auth",
    "/setup",
    "/static",
    "/health",
    "/_docs",
    "/openapi.json",
)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    if is_initialized():
        try:
            from platform_atlas.core.init_env import sync_bundled_files
            sync_bundled_files()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Bundled file sync failed at startup: %s", exc)
        try:
            init_context()
            logger.info("Atlas context initialized for WebUI (tier=%s)", _safe_tier())
        except Exception as exc:  # noqa: BLE001 — startup must never crash the server
            logger.warning("Atlas context not initialized at startup: %s", exc)
        # Continuous-audit scheduler. Starts unconditionally — its tick is a
        # cheap fs check, and the loop is a no-op when no env has it enabled.
        try:
            from platform_atlas_webui.services.continuous import get_scheduler
            await get_scheduler().start()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Continuous-audit scheduler failed to start: %s", exc)
    else:
        logger.info("Atlas not initialized — first-run setup required at /setup")
    yield
    try:
        from platform_atlas_webui.services.continuous import get_scheduler
        await get_scheduler().stop()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Continuous scheduler shutdown noisy: %s", exc)


def create_app(settings: WebUISettings | None = None) -> FastAPI:
    """Build the FastAPI app and wire in startup, static assets, and routes."""
    if settings is None:
        settings = WebUISettings.from_env()

    app = FastAPI(
        title="Platform Atlas",
        description="Optional WebUI for the Platform Atlas CLI.",
        version=ATLAS_VERSION,
        docs_url="/_docs" if settings.reload else None,
        redoc_url=None,
        lifespan=_lifespan,
    )

    # CSRF-exempt prefixes (read-only or auth-protected by nonce instead)
    _CSRF_BYPASS_PREFIXES: tuple[str, ...] = (
        "/auth",
        "/static",
        "/health",
        "/_docs",
        "/openapi.json",
    )
    # SSE stream endpoints are GET — they never need CSRF
    _CSRF_BYPASS_SUFFIXES: tuple[str, ...] = ("/stream",)

    @app.middleware("http")
    async def _enforce_csrf(request: Request, call_next):
        if request.method not in ("POST", "PATCH", "PUT", "DELETE"):
            return await call_next(request)
        path = request.url.path or "/"
        for prefix in _CSRF_BYPASS_PREFIXES:
            if path == prefix or path.startswith(prefix + "/"):
                return await call_next(request)
        for suffix in _CSRF_BYPASS_SUFFIXES:
            if path.endswith(suffix):
                return await call_next(request)
        # Check header first (AJAX), then fall through to form body below
        token = request.headers.get("X-CSRF-Token")
        cookie_value = request.cookies.get(COOKIE_NAME)
        if token and validate_csrf_token(token, cookie_value):
            return await call_next(request)
        # For form-encoded POSTs, we need to read the body. We cache it so
        # the downstream route can still read it.
        content_type = request.headers.get("content-type", "")
        if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
            body = await request.body()
            from urllib.parse import parse_qs
            form_data = parse_qs(body.decode(errors="replace"))
            token = (form_data.get("csrf_token") or [""])[0]
            if validate_csrf_token(token, cookie_value):
                # Re-attach body so the route handler can still parse it
                async def _receive():
                    return {"type": "http.request", "body": body, "more_body": False}
                request._receive = _receive  # noqa: SLF001
                return await call_next(request)
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "CSRF validation failed"}, status_code=403)

    @app.middleware("http")
    async def _enforce_auth(request: Request, call_next):
        path = request.url.path or "/"
        for prefix in _AUTH_BYPASS_PREFIXES:
            if path == prefix or path.startswith(prefix + "/"):
                return await call_next(request)
        cookie_value = request.cookies.get(COOKIE_NAME)
        if not validate_session_cookie(cookie_value):
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "Authentication required"}, status_code=401)
        return await call_next(request)

    @app.middleware("http")
    async def _redirect_when_uninitialized(request: Request, call_next):
        # Hot path: once configured, the middleware is a single fs check.
        if is_initialized():
            return await call_next(request)
        path = request.url.path or "/"
        for prefix in _SETUP_BYPASS_PREFIXES:
            if path == prefix or path.startswith(prefix + "/") or path == prefix:
                return await call_next(request)
        return RedirectResponse(url="/setup", status_code=303)

    # Themed error page for browser navigations. Covers 404/405/403 (and any
    # other HTTPException, including the 500s Starlette wraps). HTMX swaps and
    # JSON/API clients keep the default JSON body so their handling is
    # unchanged — see _wants_html_error.
    @app.exception_handler(StarletteHTTPException)
    async def _render_http_exception(request: Request, exc: StarletteHTTPException):
        if not _wants_html_error(request):
            # Same shape Starlette's default handler produces.
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        heading, default_msg = _ERROR_COPY.get(exc.status_code, _ERROR_COPY_DEFAULT)
        # Prefer a meaningful detail string over the generic line, but ignore
        # the bare status phrase Starlette uses as detail when none was given
        # (e.g. "Not Found") — the friendlier default reads better there.
        from http import HTTPStatus
        try:
            status_phrase = HTTPStatus(exc.status_code).phrase
        except ValueError:
            status_phrase = ""
        detail = exc.detail if isinstance(exc.detail, str) else ""
        message = detail if (detail and detail != status_phrase) else default_msg
        try:
            return _ERROR_TEMPLATES.TemplateResponse(
                request,
                "errors/error.html",
                {
                    "request": request,
                    "status_code": exc.status_code,
                    "heading": heading,
                    "message": message,
                    "asset_version": _ASSET_VERSION,
                },
                status_code=exc.status_code,
            )
        except Exception:  # noqa: BLE001 — never let error rendering itself 500
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    app.add_middleware(AuditMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    # Compress text responses ≥1 KB. Server-rendered Jinja pages run 50–200 KB
    # uncompressed; gzip cuts wire bytes by ~5–10× for HTML/CSS/JS.
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    if _STATIC_DIR.is_dir():
        # Subclass StaticFiles so we can stamp a long Cache-Control on hashed
        # asset paths. Without this, browsers revalidate every static asset
        # on every page load, even though they never change between releases.
        class _CachedStaticFiles(StaticFiles):
            async def get_response(self, path, scope):
                response = await super().get_response(path, scope)
                if isinstance(response, _StarletteResponse) and response.status_code == 200:
                    # 1 year. Templates already cache-bust via ?v=ATLAS_VERSION.
                    response.headers.setdefault(
                        "Cache-Control", "public, max-age=31536000, immutable"
                    )
                return response

        app.mount(
            settings.static_url_prefix,
            _CachedStaticFiles(directory=str(_STATIC_DIR)),
            name="static",
        )

    register_routes(app)
    return app


def _safe_tier() -> str:
    try:
        from platform_atlas.core.context import ctx
        return ctx().tier
    except Exception:
        return "unknown"
