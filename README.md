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
- [Atlas MCP Server](#atlas-mcp-server)
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
- **Environment management** — Create, edit, activate, and delete environments. Credential storage — OS keyring, encrypted local file, or HashiCorp Vault — is chosen and managed inline. The form also covers the Gateway 5 config source (SSH / Docker Compose / Helm / server `gateway.conf`), an SSH-key dropdown, passphrase storage, and an inline **Test SSH connection** button.
- **Three tiers** — Standard (Platform OAuth + IAG4 API), Extended (full SSH / Mongo / Redis / Gateway audit), and SaaS (a single standalone GW4 *or* GW5 with no Platform/Mongo/Redis). Standard ⇄ Extended toggles live; SaaS is fixed per environment at create time. Rule counts and target requirements update automatically.
- **Ruleset picker** — Choose the active ruleset and profile per session. Changes take effect on the next validation run.
- **Architecture form** — Multi-section form for capturing infrastructure-as-deployed metadata that feeds the Architecture & Maintenance report.
- **Diff view** — Compare any two sessions and surface what changed between them.
- **Reports** — Browse and open generated compliance, operational, and architecture reports directly in the browser. A skipped rule's detail explains *why* it was skipped (couldn't reach the system / no data / conditional), color-coded.
- **Support bundle & Platform asset export** — Collect a diagnostic ZIP (health endpoints, redacted config, Extended logs), and optionally export Workflows, JSON Transformations, JSON Forms, or whole Projects from the active environment into the bundle over Platform OAuth.
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
| Support Bundle | `/support-bundle` | Diagnostic ZIP + optional Platform asset export |
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
- **`platform-atlas` core** `>=3.0.0,<4.0` — installed alongside (Poetry pulls it automatically; pip install both wheels for production). The WebUI enforces this at startup.
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
                     [--reset-tls] [--no-tls] [--no-auth] [--reset-token] [--no-browser]
                     [--daemon] [--mcp-server] [--reset-mcp-token]
                     <action> ...
```

### Run flags

| Flag | Default | Effect |
|---|---|---|
| `--host` | `127.0.0.1` | Bind host. Loopback only unless `--allow-remote` is also passed. |
| `--port` | `8765` (`8766` under `--mcp-server`) | Bind port. |
| `--reload` | off | Enable uvicorn hot-reload (development only). Cannot combine with `--daemon`, and not supported under `--mcp-server`. |
| `--allow-remote` | off | Allow binding to non-loopback interfaces. Required for `0.0.0.0` or `::`. |
| `--log-level` | `info` | One of `debug`, `info`, `warning`, `error`, `critical`. |
| `--reset-tls` | off | Regenerate the self-signed certificate and exit. Shared between the browser UI and MCP server (same host identity). |
| `--no-tls` | off | **`--mcp-server` only.** Run over plain HTTP instead of HTTPS. The bearer token then travels unencrypted — see [Atlas MCP Server](#atlas-mcp-server) before using this. Rejected on the browser UI (its session cookies require HTTPS). |
| `--no-auth` | off | **`--mcp-server` only.** Disable bearer-token auth entirely — every request is accepted unauthenticated, no token to generate or rotate. See [Atlas MCP Server](#atlas-mcp-server) before using this. Rejected on the browser UI (it uses session-cookie auth, not a bearer token). |
| `--reset-token` | off | Regenerate the OS-user binding token (invalidates all existing browser sessions). |
| `--no-browser` | off | Skip auto-opening the default browser on launch. |
| `--daemon` | off | Detach to the background, write a PID file, log to `~/.atlas/webui.log` (or `~/.atlas/mcp-server.{pid,log}` under `--mcp-server`). |
| `--mcp-server` | off | Run the [Atlas MCP server](#atlas-mcp-server) instead of the browser UI — a separate mode, process, and daemon. |
| `--reset-mcp-token` | off | Regenerate the MCP server's bearer token. Only meaningful with `--mcp-server`. |

### Subcommands

| Action | Purpose |
|---|---|
| `stop [--mcp-server]` | Stop the running daemon (SIGTERM via PID file). `--mcp-server` targets the MCP daemon instead of the browser UI's. |
| `status [--mcp-server]` | Report whether a daemon is running, its PID, and log path. |
| `restart` | Stop the existing daemon and start a fresh one. Run flags accepted to change host/port/mode. |
| `login-url` | Print a fresh single-use login URL (valid for 60 s). Handy after a daemon restart. |
| `print-mcp-token` | Print the MCP server's bearer token — for `iagctl mcp server add --header`. |

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

## Atlas MCP Server

`platform-atlas-webui --mcp-server` runs a [Model Context Protocol](https://modelcontextprotocol.io) server exposing 16 read-only Atlas query tools — built for registering Atlas as an external tool in Itential's FlowMCP Gateway so FlowAI agents can query compliance data in natural language. It's a **separate mode, process, and daemon from the browser UI**: no browser routes, no session-cookie auth, its own bearer token, and (by default) its own port, so the two can run independently or side by side on one host.

This is intentionally read-only in this release — it never triggers a capture/validate/report run. See `design/MCP/*.md` in the `platform-atlas` repo for the full research history and phasing rationale.

### Available tools

| Tool | Answers |
|---|---|
| `list_environments` | Every configured environment, its tier, and latest audit status. |
| `list_sessions` | Recent sessions, newest first, optionally scoped to one environment. |
| `get_compliance_summary` | One environment's latest compliance numbers (pass/fail/skip, pass rate). |
| `fleet_top_fix` | Which rule failures recur across the most environments fleet-wide. |
| `explain_rule` | Look up one rule by number/name/path and its status in one environment. |
| `diff_sessions` | What changed (fixed/regressed/new/removed) between two sessions in one environment. |
| `rule_fleet_distribution` | Same lookup as `explain_rule`, but across *every* environment — is this a systemic policy gap or a one-off? |
| `fleet_severity_breakdown` | FAIL counts by severity, one environment or fleet-wide — what's most urgent right now. |
| `rule_category_health` | Pass/fail/skip grouped by rule category (gateway4, mongo_conf, ...) — which subsystem is weakest. |
| `stale_environments` | Environments that haven't been audited recently, or ever. |
| `session_history_trend` | Is one environment's pass rate improving, degrading, or flat over its last N audits? |
| `flaky_rules` | Rules that flip pass/fail repeatedly in one environment — instability, not a clean regression. |
| `compare_environments` | Rules where two environments disagree (one passes, one fails) on the same check. |
| `skip_reason_breakdown` | Why rules were skipped — a deliberate exception vs. "couldn't connect to collect this." |
| `fleet_tier_coverage` | Environment counts and average pass rate by tier (standard/extended/saas). |
| `fleet_regressions_since_last_audit` | What got worse, anywhere in the fleet, since each environment's last audit. |

### Quick start

```bash
# Foreground, for a first look
platform-atlas-webui --mcp-server

# The bearer token is printed once, on first generation — save it, or
# retrieve it again anytime:
platform-atlas-webui print-mcp-token
```

Register with Gateway5 — recommended: store the token as a Gateway secret first
(`--prompt-value` avoids it ever landing in shell history), then reference it
by name so it's never passed as plaintext in the registration command itself:

```bash
iagctl create secret atlas-mcp-token --prompt-value
# (paste the value from `platform-atlas-webui print-mcp-token` when prompted)

iagctl mcp server add atlas "https://<host>:8766/mcp" \
  --transport streamable-http \
  --header "Authorization=Bearer {{ secret \"atlas-mcp-token\" }}" \
  --description "Platform Atlas compliance audit tool-call API"
```

(Quicker, less secure alternative for a one-off local test: skip `create secret`
and pass the raw token directly — `--header "Authorization=Bearer <token>"`.)

Verify with `iagctl mcp server inspect atlas` and `iagctl mcp tool list atlas`.

> Note the URL has **no trailing slash** (`/mcp`, not `/mcp/`) — that's the
> canonical path; `/mcp/` 307-redirects to it. Also note `--header` takes
> `Name=Value` (an `=`), not `Name: Value` — different from a raw HTTP header
> line.

### Running it as a background service

Two independent ways to keep it running — pick one, don't combine them on the same instance:

**Option A — Atlas's own `--daemon` mode** (no service supervisor required):

```bash
platform-atlas-webui --mcp-server --daemon --allow-remote
platform-atlas-webui status --mcp-server
platform-atlas-webui stop --mcp-server
```

**Option B — systemd** (Linux hosts that have it — recommended for production, since systemd supervises restarts on crash/reboot for you):

A template unit is at `scripts/systemd/platform-atlas-mcp.service.example`. Copy it, fill in the paths/user, and enable it:

```bash
sudo cp scripts/systemd/platform-atlas-mcp.service.example /etc/systemd/system/platform-atlas-mcp.service
sudo systemctl daemon-reload
sudo systemctl enable --now platform-atlas-mcp
```

Under systemd, run the MCP server in the **foreground** (`ExecStart=... --mcp-server --allow-remote`, no `--daemon`) — systemd is already the process supervisor, so Atlas's own self-fork would only get in the way.

### Notes

- Bind posture mirrors the browser UI: loopback-only unless `--allow-remote` is passed. Since the whole point is letting Gateway5 reach it over the LAN, most real deployments will need `--allow-remote`. A specific interface IP (e.g. `--host 192.168.2.104`) doesn't require `--allow-remote` — that flag only guards the `0.0.0.0`/`::` wildcards.
- TLS uses the same self-signed certificate as the browser UI (same host identity), automatically regenerated to add whatever `--host` you bind to as a certificate SAN — no separate cert to manage, no manual `--reset-tls` needed when you change `--host`.
- **Self-signed TLS and strict clients:** some MCP clients (Gateway5's own outbound connection, in particular) verify the certificate chain strictly and reject a self-signed cert with `x509: certificate signed by unknown authority`, even once the SAN matches. The fully correct fix is making the client trust Atlas's cert — for Gateway5 that's `GATEWAY_APPLICATION_CA_CERTIFICATE_FILE` / config file `[application]` → `ca_certificate_file`, pointed at `~/.atlas/.webui-cert.pem`, set **on the Gateway5 host**, not here. If that's more than you want to deal with for a trusted-network setup, `--no-tls` runs the MCP server over plain HTTP instead and sidesteps the whole problem — see below.
- **`--no-tls`** disables TLS entirely for MCP mode (browser mode refuses it — its session cookies require HTTPS). The bearer token then travels in cleartext, so only use it on a network you trust, or behind something else terminating TLS in front of Atlas (a reverse proxy, an SSH tunnel, a VPN). Registration and calls just use `http://` instead of `https://` — everything else (auth, tools, audit log) is unchanged.
- The bearer token lives at `~/.atlas/.mcp-token` (mode 0600), independent of the browser UI's login token — rotating one never logs out the other. **It's a static secret**: it's generated once and never changes on its own — only `--reset-mcp-token`, or losing the file (e.g. a wiped `~/.atlas`), produces a new one. Update the Gateway5 registration only after an actual rotation.
- **`--no-auth`** disables bearer-token auth entirely for MCP mode (browser mode refuses it — it authenticates via session cookie, not a bearer token). Every request is accepted with no `Authorization` header at all — there's no token to generate, print, or register with `iagctl`. Registration drops the `--header` flag:

  ```bash
  iagctl mcp server add atlas "https://<host>:8766/mcp" --transport streamable-http
  ```

  Only use this on a network you trust, or behind something else terminating auth in front of Atlas. Combine with `--no-tls` for fully plaintext, unauthenticated access — appropriate for an isolated lab network, not a shared one. Tool calls are still written to the audit log either way (identified by the OS user running the Atlas process, since there's no per-client token to distinguish callers once auth is off).
- Tool calls are logged to `~/.atlas/mcp-audit.log` (who, what tool, when, outcome — JSON lines, rotated).

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
