"""Route registration — collect all routers in one place for app.py to mount."""

from fastapi import FastAPI

from platform_atlas_webui.routes import (
    auth as _auth,
    dashboard as _dashboard,
    sessions as _sessions,
    environments as _environments,
    rulesets as _rulesets,
    reports as _reports,
    tier as _tier,
    health as _health,
    jobs as _jobs,
    config as _config,
    credentials as _credentials,
    preflight as _preflight,
    diff as _diff,
    setup as _setup,
    settings as _settings,
    architecture as _architecture,
    # Platform API — DISABLED. Kept as scaffolding (empty router). To restore,
    # re-flesh routes/platform_api.py and services/platform_api.py and uncomment
    # the include_router call below.
    platform_api as _platform_api,
    continuous as _continuous,
    alerts as _alerts,
    notifications as _notifications,
    fleet as _fleet,
    search as _search,
    support_bundle as _support_bundle,
)


def register_routes(app: FastAPI) -> None:
    """Attach every router to the FastAPI app."""
    app.include_router(_auth.router)
    app.include_router(_setup.router)
    app.include_router(_dashboard.router)
    app.include_router(_sessions.router)
    app.include_router(_environments.router)
    app.include_router(_rulesets.router)
    app.include_router(_reports.router)
    app.include_router(_tier.router)
    app.include_router(_health.router)
    app.include_router(_jobs.router)
    app.include_router(_config.router)
    app.include_router(_credentials.router)
    app.include_router(_preflight.router)
    app.include_router(_diff.router)
    app.include_router(_settings.router)
    app.include_router(_architecture.router)
    # Platform API — DISABLED. The router has no handlers; mounting it is a no-op
    # but kept here as a one-line restoration point.
    app.include_router(_platform_api.router)
    app.include_router(_continuous.router)
    app.include_router(_alerts.router)
    app.include_router(_notifications.router)
    app.include_router(_fleet.router)
    app.include_router(_search.router)
    app.include_router(_support_bundle.router)
