# Platform Atlas WebUI

![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-005571?style=flat-square&logo=fastapi&logoColor=white)
![License](https://img.shields.io/badge/license-GNU%20GPLv3-green?style=flat-square)
![Distribution](https://img.shields.io/badge/distribution-wheel-blue?style=flat-square)

> Browser-based companion to [Platform Atlas](https://github.com/itential/platform-atlas) — manage sessions, run captures, watch jobs stream live, and browse compliance reports without touching the CLI.

Platform Atlas WebUI is an optional, separately-distributed wheel that adds a browser interface on top of the same capture, validation, and reporting engines that power the CLI. It ships as its own wheel (`platform-atlas-webui`) so customers who don't allow web or server components can install only the core CLI wheel and never have any web source on disk.

---

## Table of Contents

- [Why a WebUI](#why-a-webui)
- [Features](#features)
- [Screens](#screens)
- [Requirements](#requirements)
- [Install](#install)
- [Quick Start](#quick-start)
- [CLI Reference](#cli-reference)
- [Daemon Mode](#daemon-mode)
- [Configuration](#configuration)
- [Security Model](#security-model)
- [Themes](#themes)
- [Architecture](#architecture)
- [Local Development](#local-development)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## Why a WebUI

The core Platform Atlas CLI is fast, scriptable, and ideal for automation pipelines. The WebUI is for everything else:

- Giving a non-CLI user a guided path through their first audit
- Running a session interactively while watching output arrive in real time
- Comparing two sessions side-by-side without exporting JSON
- Getting a single-pane view of reports, fleet health, and continuous-audit history
- Configuring environments, credentials, rulesets, and tiers without editing config files by hand

The CLI is unchanged — the WebUI is purely additive. Sessions, environments, and reports created in either tool are instantly visible in the other because they share the same `~/.atlas/` directory.

---

## Features

- **Full session lifecycle** — Create, activate, capture, validate, generate reports, and delete sessions. Pagination on the session list (20 / 50 / 100 per page).
- **Live job streaming** — Long-running operations (preflight, capture, validate, report, full pipeline) run in a background thread and stream events to the browser via Server-Sent Events. Per-job toggle between a human-friendly summary and raw progress logs.
- **Pipeline progress bar** — "Run full pipeline" jobs show a clean three-stop bar (Capture → Validate → Report) that advances as each stage completes.
- **Preflight checks panel** — Shows phase headers and pass/fail rows as they arrive, with an animated indicator for the check currently in flight.
- **Environment management** — Create, edit, activate, and delete environments. Credential storage (OS keyring or HashiCorp Vault) is handled inline.
- **Tier toggle** — Switch between Standard (Platform OAuth + IAG4 API) and Extended (full SSH / Mongo / Redis / Gateway audit) tiers. Rule counts and target requirements update automatically.
- **Ruleset picker** — Choose the active ruleset and profile per session. Changes take effect on the next validation run.
- **Architecture form** — Multi-section form for capturing infrastructure-as-deployed metadata that feeds the Architecture & Maintenance report.
- **Diff view** — Compare any two sessions and surface what changed between them.
- **Reports** — Browse and open generated compliance, operational, and architecture reports directly in the browser.
- **Fleet view** — Aggregate health across all environments — per-environment pass rate, continuous-audit state, and unacknowledged drift counters in one place.
- **Continuous audit** — Schedule recurring audits and inspect the full run history.
- **Notifications & alerts** — Route alerts to Slack or any JSON webhook when failures cross a threshold.
- **First-run setup wizard** — A guided experience that creates the first environment, stores credentials, picks a tier, and runs preflight before turning you loose.
- **Theme system** — Seven palettes with independent light and dark modes. Your choice persists server-side per OS user.
- **Built-in TLS** — A self-signed certificate is generated automatically on first launch (365-day validity) and stored at `~/.atlas/webui-cert.pem`. The SHA-256 fingerprint is printed at startup so you can verify it before clicking through the browser warning.
- **OS-user binding** — Every browser session is authenticated against a token derived from the local OS user. A single-use nonce URL is minted each time the server starts and burns after the first click.
- **Daemon mode** — Run the WebUI as a backgrounded service with a PID file and log file. Control it with `stop`, `status`, and `restart` subcommands.
- **Reduced-motion aware** — All animations and transitions honor the `prefers-reduced-motion` OS setting.

---

## Screens

| Surface | Route | Purpose |
|---|---|---|
| Dashboard | `/` | At-a-glance KPIs and quick actions |
| Sessions | `/sessions` | Session list, detail, run actions |
| Environments | `/environments` | Environment CRUD, activation, credentials |
| Rulesets | `/rulesets` | Active ruleset and profile picker |
| Tier | `/tier` | Standard ⇄ Extended toggle and overview |
| Preflight | `/preflight` | Connectivity checks with live output |
| Jobs | `/jobs` | Job list, detail, live SSE stream |
| Reports | `/reports` | Browse generated HTML reports |
| Architecture | `/architecture` | Multi-section infrastructure form |
| Diff | `/diff` | Compare two sessions |
| Fleet | `/fleet` | Multi-environment health summary |
| Continuous | `/continuous` | Scheduled audit runs and history |
| Notifications | `/notifications` | Alert routing and webhook config |
| Alerts | `/alerts` | Live alert feed with acknowledge |
| Config | `/config` | Configuration overview and credentials |
| Setup | `/setup` | First-run wizard |
| Platform API | `/platform-api` | Platform OAuth credential helper |

---

## Requirements

- **Python** `>=3.11,<4.0`
- **OS** — Linux (RHEL/Rocky 8+9, Ubuntu) or macOS. Daemon mode is POSIX-only; on Windows, run the WebUI under your service supervisor of choice.
- **Browser** — Any modern Chromium, Firefox, or Safari. The UI uses `EventSource`, `@property`, and View Transitions — release-channel browsers from 2024 onward work without polyfills.
- **`platform-atlas` core** — installed alongside (Poetry pulls it automatically; pip install both wheels for production).
- **Optional** — `keyring` (included with `platform-atlas`) for OS-keyring credential storage, or `hvac` for HashiCorp Vault.

---

## Install

Download both wheels from the [latest GitHub Release](https://github.com/itential/platform-atlas-webui/releases) and install together:

```bash
pip install platform_atlas-X.Y.Z-py3-none-any.whl \
            platform_atlas_webui-X.Y.Z-py3-none-any.whl

platform-atlas-webui
```

Customers who do not allow web or server components can install only the core wheel and use the CLI as normal:

```bash
pip install platform_atlas-X.Y.Z-py3-none-any.whl
platform-atlas
```

---

## Quick Start

```bash
# 1. Launch (foreground, default port 8765)
platform-atlas-webui

# 2. A one-time login URL is printed in the terminal:
#       https://127.0.0.1:8765/auth?nonce=...
#    Open it in your browser. You'll see a certificate warning —
#    that's expected (the cert is self-signed). Verify the SHA-256
#    fingerprint printed in the terminal matches what your browser
#    shows, then click "Advanced → Proceed".
#
# 3. First time? The setup wizard walks you through creating an
#    environment, storing credentials, and picking a tier.
```

That's it. The WebUI reads from the same `~/.atlas/` directory the CLI uses, so any sessions or environments you've already created show up immediately.

---

## CLI Reference

```text
platform-atlas-webui [--host HOST] [--port PORT] [--reload]
                     [--allow-remote] [--log-level LEVEL]
                     [--reset-tls] [--reset-token] [--no-browser]
                     [--daemon]
                     <action> ...
```

### Run flags

| Flag | Default | Effect |
|---|---|---|
| `--host` | `127.0.0.1` | Bind host. Loopback only unless `--allow-remote` is also passed. |
| `--port` | `8765` | Bind port. |
| `--reload` | off | Enable uvicorn hot-reload (development only). Cannot combine with `--daemon`. |
| `--allow-remote` | off | Allow binding to non-loopback interfaces. Required for `0.0.0.0` or `::`. |
| `--log-level` | `info` | One of `debug`, `info`, `warning`, `error`, `critical`. |
| `--reset-tls` | off | Regenerate the self-signed certificate and exit. |
| `--reset-token` | off | Regenerate the OS-user binding token (invalidates all existing browser sessions). |
| `--no-browser` | off | Skip auto-opening the default browser on launch. |
| `--daemon` | off | Detach to the background, write a PID file, log to `~/.atlas/webui.log`. |

### Subcommands

| Action | Purpose |
|---|---|
| `stop` | Stop the running daemon (SIGTERM via PID file). |
| `status` | Report whether a daemon is running, its PID, and log path. |
| `restart` | Stop the existing daemon and start a fresh one. Run flags accepted to change host/port. |
| `login-url` | Print a fresh single-use login URL (valid for 60 s). Handy after a daemon restart. |

### Examples

```bash
# Foreground on a custom port
platform-atlas-webui --port 9000

# Bind to all interfaces for LAN access
platform-atlas-webui --host 0.0.0.0 --allow-remote

# Don't auto-open the browser (useful over SSH with port-forwarding)
platform-atlas-webui --no-browser

# Run as a background daemon
platform-atlas-webui --daemon

# Restart the daemon on a different port
platform-atlas-webui restart --port 9001

# Get a fresh login URL after a daemon restart
platform-atlas-webui login-url
```

---

## Daemon Mode

`--daemon` double-forks the process, redirects output to `~/.atlas/webui.log`, and writes the PID to `~/.atlas/webui.pid`. The server keeps running after you close your terminal or log out.

```bash
platform-atlas-webui --daemon       # start in the background
platform-atlas-webui status         # is it running?
tail -f ~/.atlas/webui.log          # follow the log
platform-atlas-webui login-url      # get a fresh login URL
platform-atlas-webui restart        # stop + relaunch
platform-atlas-webui stop           # graceful shutdown
```

Daemon mode is POSIX-only (Linux and macOS). On Windows, run the WebUI under your service supervisor of choice.

> `--reload` and `--daemon` cannot be combined. Use `--reload` during development; use `--daemon` when you want the server to stay running.

---

## Configuration

All flags have environment-variable equivalents. Flags take precedence over environment variables.

| Variable | Maps to | Default |
|---|---|---|
| `ATLAS_WEBUI_HOST` | `--host` | `127.0.0.1` |
| `ATLAS_WEBUI_PORT` | `--port` | `8765` |
| `ATLAS_WEBUI_RELOAD` | `--reload` (`1` / `true` to enable) | `0` |
| `ATLAS_WEBUI_LOG_LEVEL` | `--log-level` | `info` |
| `ATLAS_WEBUI_ALLOW_REMOTE` | `--allow-remote` (`1` / `true` to enable) | `0` |

If `ATLAS_WEBUI_HOST` is set to a public address (`0.0.0.0` or `::`) without `ATLAS_WEBUI_ALLOW_REMOTE=1`, the WebUI quietly falls back to `127.0.0.1`. This is intentional — a misconfigured environment variable shouldn't accidentally expose the UI on a shared host.

---

## Security Model

The WebUI is designed as a single-user, local-first tool. Multiple layers of defense are stacked so no single misconfiguration opens a broad attack surface:

1. **Loopback-only by default.** The server binds to `127.0.0.1` unless `--allow-remote` is explicitly passed. A public-bind check runs before uvicorn even starts.

2. **Self-signed TLS.** A certificate is generated automatically on first launch and stored at `~/.atlas/webui-cert.pem`. The SHA-256 fingerprint is printed at startup — verify it against what your browser shows before clicking through the warning. Rotate at any time with `--reset-tls`.

3. **OS-user binding.** Sessions are authenticated against a token that only the OS user who owns `~/.atlas/` can read. Running `--reset-token` invalidates every active browser session immediately.

4. **One-time login nonce.** Each server start mints a fresh URL containing a short-lived nonce. The nonce is valid for 60 seconds and burns on the first use — it's exchanged for a signed session cookie and never accepted again.

5. **CSRF protection.** Every form submission and AJAX mutation (POST / PATCH / DELETE) carries a per-session HMAC token. Requests without a valid token are rejected before they reach any route handler.

6. **Path-traversal guard.** Every file-serving route (report HTML, JSON exports, log tails) clamps the requested path to a known-safe ancestor before opening the file. Escaped paths are rejected outright.

7. **Secret redaction in logs.** The access log is filtered before anything reaches disk or stdout — `?nonce=` query strings, auth cookies, and known credential header values are stripped.

8. **Security response headers.** Every response includes `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, a `Permissions-Policy` blocking camera/microphone/geolocation, and a Content Security Policy.

For a shared or remote deployment, bind to loopback and put the WebUI behind a reverse proxy (nginx, Caddy) that handles real TLS and any additional authentication you require.

---

## Themes

Seven palettes, each with an independent light and dark mode:

| Theme | Character |
|---|---|
| **Itential** (default) | Brand navy + Itential blue. |
| Aurora | Cool blue/purple with subtle gradients. |
| Horizon | Warm amber and peach tones. |
| Obsidian | High-contrast black and white. |
| Meadow | Earthy greens, low saturation. |
| Carbon | Dense monochrome, text-forward. |
| Dracula | The official Dracula color spec; light variant uses the Alucard palette. |

Switch themes from the gear icon in the top-right corner. Your preference is saved server-side per OS user.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                    platform-atlas-webui                      │
│                                                              │
│   FastAPI app (uvicorn + TLS)                                │
│   ├── routes/      ── HTTP endpoints, one file per surface   │
│   ├── services/    ── Background job runner, SSE registry    │
│   ├── security/    ── TLS, tokens, CSRF, path safety         │
│   ├── templates/   ── Jinja2 templates, base + per-surface   │
│   └── static/      ── CSS, JS, fonts, images                 │
│                                                              │
└────────────────────────────┬─────────────────────────────────┘
                             │ imports
                             ▼
┌──────────────────────────────────────────────────────────────┐
│                      platform-atlas                          │
│                                                              │
│   Capture engine ── Collectors (SSH, Mongo, Redis, OAuth)    │
│   Validation engine ── Rule evaluators, operators            │
│   Reporting engine ── Compliance / Operational / Arch HTML   │
│   Session manager ── ~/.atlas/sessions/<name>/               │
│   AtlasContext ── Singleton config + ruleset state           │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

Long-running operations run as background jobs inside `JobRegistry`. Each job gets a logger it can call `info()`, `phase()`, `check()`, and `debug()` on; events broadcast over an SSE channel to every subscribed browser tab in real time. The job runners call the **same** capture, validate, and report functions the CLI uses — there is no duplicated business logic between the two tools.

`~/.atlas/` is the single source of truth for both. A session created in the WebUI is immediately visible to the CLI, and vice versa.

---

## Local Development

```bash
# Install dependencies (from the repo root)
poetry install

# Start the dev server with hot-reload
poetry run platform-atlas-webui --reload --log-level debug

# Build a distributable wheel and sdist (writes to dist/)
poetry build
```

A few things worth knowing when developing:

- **CSS changes** require a hard refresh (`Ctrl+Shift+R`) after the first load because static assets are cached with `Cache-Control: immutable`. Bumping `__version__` in `platform_atlas_webui/__init__.py` forces every browser to re-fetch on the next visit.
- **Adding a route** — create a file in `routes/`, register it in `routes/__init__.py`, and add a matching template directory under `templates/<name>/`.
- **Adding a long-running operation** — write a sync function in `services/runners.py` that takes a `JobLogger` as its first argument. Submit it from your route via `get_registry().submit(name, runner, **kwargs)`. SSE streaming to the browser is automatic.
- **Theme tokens** live in `static/css/atlas.css` under `:root[data-theme="<name>"]` blocks. Component styles reference these variables exclusively — no hardcoded colors.

---

## Troubleshooting

**The browser warns about the certificate.**
This is expected — the cert is self-signed and your browser doesn't recognize it as a trusted authority. Verify the SHA-256 fingerprint printed at launch matches what your browser shows in the certificate details, then click `Advanced → Proceed`. The warning won't repeat for that hostname and port.

**My browser session expired.**
Run `platform-atlas-webui login-url` to get a fresh one-time login URL, or restart the server. The OS-user binding token is durable; it's just your browser cookie that expired.

**`--daemon` says "Cannot combine --daemon with --reload".**
Pick one. The hot-reloader spawns a child process that would re-enter the startup code and try to daemonize itself — it doesn't work. Use `--reload` during development and `--daemon` when you want the server to stay running.

**Static assets look stale after upgrading.**
Hard refresh once (`Ctrl+Shift+R`). Static files are cached for one year; the cache key changes automatically with the version, so future upgrades handle themselves.

**I want to access the WebUI from another machine on my network.**
`platform-atlas-webui --host 0.0.0.0 --allow-remote`. Without `--allow-remote`, any non-loopback bind is rejected before the server starts.

**I want to run it behind a reverse proxy.**
Bind to loopback (`--host 127.0.0.1`), let nginx or Caddy terminate TLS with a real certificate, and proxy to `http://127.0.0.1:8765`.

**The daemon won't stop.**
Check `~/.atlas/webui.pid`. If the process listed there is no longer running, `platform-atlas-webui stop` will detect the stale PID file and clean it up. If the process is genuinely stuck, run `kill -9 $(cat ~/.atlas/webui.pid)` and delete the PID file manually.

---

## License

GPL-3.0-or-later — the same license as the core [Platform Atlas](https://github.com/itential/platform-atlas) project. See [LICENSE](LICENSE).
