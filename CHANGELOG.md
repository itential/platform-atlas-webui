# Changelog — Platform Atlas WebUI

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.1.1] - 2026-06-05

Requires `platform-atlas >=1.8,<2.0`.

### Added

- **Friendly error pages** — 404 / 500 / CSRF now render a themed page with a way back to the dashboard instead of raw JSON (API and HTMX requests still get JSON).
- **First-run dashboard helper** — a dismissible "Get started" prompt appears when no environments exist yet.
- **Credential pre-check on the capture step** — warns when the bound environment has no stored credentials before you run capture.

### Changed

- **Theme readability tweak — sidebar section labels now stand out in every palette** — the navigation group headings (Overview, Audit, Configuration, Continuous, System) render in each theme's accent color — a bright highlight in dark mode, a darker matching shade in light mode — and are bumped up 1px. They previously used the most-dimmed text token and faded into the sidebar background in several themes.
- **Switching to Standard** now warns that Extended-tier sessions become view-only.
- **Reports empty state** links straight to creating a session.
- **Accessibility** — error toasts announce assertively, the continuous-audit pill is labelled, and the force-stop control now appears after ~75s (was 5 min) and is announced to screen readers.

### Fixed

- **Report fonts are self-hosted** — no external CDN, so the report view renders correctly on air-gapped installs.
- **Environment save errors** re-render the form with your input intact instead of crashing to an error page.
- **Job stream** shows a clear failure with reload / Jobs links instead of spinning forever when the connection drops.
- **Continuous-audit empty state** no longer references the run button by the wrong name.

### Security

- **Reflected XSS hardening in the `/check-kubectl` endpoint** — the user-supplied `path` query parameter is now HTML-escaped (`markupsafe.escape`) before being reflected into the returned HTML fragment, closing a cross-site scripting vector where a crafted path (e.g. one containing `<img onerror=…>`) was echoed back unescaped (Snyk CWE-79).

## [1.1.0] - 2026-06-01

Requires `platform-atlas >=1.8,<2.0`.

### Added

- **Ruleset update notice** — a slim banner appears on the dashboard when a ruleset update is available but has not yet been applied. Reads `~/.atlas/.ruleset_update_available.json`; disappears on the next page load once the file is gone.
- **Windows 11 support** — WebUI entry point (`main.py`) now reconfigures `stdout`/`stderr` to UTF-8 before any output so Unicode characters (✓ ✘ ● em-dashes) render correctly on Windows consoles. `daemon.py` already gates daemonization on `os.name == "posix"` and uses `getattr(signal, "SIGKILL", signal.SIGTERM)` for safe cross-platform process termination. Additional hardening:
  - `logging.FileHandler` now passes `encoding="utf-8"` — fixes a `UnicodeEncodeError` crash on non-UTF-8 Windows locales when job output contains Unicode characters (em-dashes in rule messages, ✓/✘ in status events).
  - `os.chmod()` calls in `security/tls.py`, `security/tokens.py`, `security/audit.py`, and `services/setup.py` are now skipped on Windows (`if os.name == "posix"` / early return) — they were silent no-ops but are now explicitly gated for clarity.
- **Support Bundle page** (`/support-bundle`) — collects Platform health endpoints, SSH logs and system info (Extended tier), and a redacted Atlas config snapshot into a ZIP. Form takes a ticket number, optional description, and log-window days. Job streams live via SSE; a Download button appears on completion. The download link is single-use — the temp file is removed after the first download.
- **Resume interrupted captures** — when a capture is interrupted mid-run, a checkpoint file records which modules completed. The session detail page now shows a banner with **Resume** (pick up from where it left off) and **Start over** (discard checkpoint) buttons. The capture runner creates and manages the checkpoint transparently; it is cleared automatically on successful completion.
- **Rule suppression in the ruleset table** — each rule row now has a **Suppress** button that expands an inline reason form (min 10 chars required). Suppressed rules show an amber **Suppressed** pill, the justification reason in italic below it, and a **Restore** button. Calls `/rulesets/suppress` and `/rulesets/unsuppress`, which write to the active environment's `skip_rules` list (not the profile). Profile-disabled rules continue to show a muted **Disabled in profile** pill with no toggle.
- **Architecture warnings panel** — the `/architecture` page now renders a warnings strip above the form. Each warning is a color-coded card (orange = latency risk, blue = availability risk) derived from the collected datacenter and HA topology data. Also exposed via `GET /architecture/warnings` for API consumers.

### Performance

- **Report viewmodel HTTP caching** — `GET /reports/{name}/viewmodel` now returns `Cache-Control: private, max-age=120`. The viewmodel JSON is already persisted to disk at report-generation time and only changes on `?refresh=1`, so the browser no longer re-downloads the full payload (often 500 KB+) on every tab switch or HTMX back/forward. Force-refresh responses use `no-store` so they always re-fetch.
- **Rules search debounce** — `atlasReportRulesFilter` now batches rapid text-input events into a single DOM pass after 180 ms of inactivity, preventing a full table scan and DOM mutation on every keystroke. Status chip clicks (which change the filter without changing the search text) are still applied immediately.
- **Parquet column projection in session summary** — `get_session_summary()` passes an explicit `columns=[...]` list to `pd.read_parquet()`, reading only the 9 fields it uses rather than deserializing the entire DataFrame. Avoids unnecessary I/O on large validation files.
- **Session summary in-process cache** — `get_session_summary()` caches its result for 60 seconds once `validation_completed` is true, so repeated calls from the post-pipeline mini-report (SSE close + frontend poll) skip the Parquet read entirely.
- **Tier resolution in-process cache** — `resolve_active_tier()` now caches its result for 1 second, covering burst reads within a single page-load cycle where `list_sessions()` and `get_session()` each call it independently. Cache is invalidated immediately by `update_config()` and `mirror_tier_to_active_overlay()` on any tier write.

---

## [1.0.0] - 2026-05-13

Initial release. The WebUI works together with CLI `platform-atlas` 1.7.x.

### Added

- **Full session lifecycle in the browser** — create, capture, validate, and report against any environment without leaving the page; live job output streamed via Server-Sent Events; force-kill button appears on long-running jobs after 60 s
- **Environment management** — create, edit, activate, and delete environments; activation atomically restores tier, ruleset, and profile alongside the active environment
- **Tier switcher** — Standard / Extended toggle with a confirmation modal explaining what changes; tier overview page renders both tiers as cards with a "when to use" hint and an active-tier accent
- **Reports browser** — direct links to compliance, operational, and architecture HTML reports for every session
- **`/fleet`** — multi-environment compliance overview from local cache (read-only); per-env tier, last session age, pass rate, continuous-audit state, unacked alerts
- **`/continuous`** — status, settings, run history; Alerting section with `alert_policy` (any | regression) and rule-number `watchlist` chip editor; always-on topbar pill with state + last-run age
- **`/alerts`** — drift timeline with ack / ack-all; bell icon in the topbar with unacked count; deleted-rule rendering as `(rule deleted)` muted text when an alert references a rule no longer in the ruleset
- **`/notifications`** — Slack incoming webhooks and generic JSON webhooks (HMAC-SHA256 signing optional); per-environment channels persisted on the env overlay
- **Lightweight daemon mode** — `platform-atlas-webui --daemon` detaches via POSIX double-fork, writes `~/.atlas/webui.pid`, logs to `~/.atlas/webui.log`; `stop`, `status`, `restart` subcommands operate on the PID file. Linux + macOS
- **`platform-atlas-webui login-url`** — mints a fresh nonce-signed login URL on demand, useful after a daemon restart where the URL is otherwise tucked into the log
- **Aurora & Horizon theme system** — palette and light/dark mode are independent axes:
  - Aurora — confident, technical (deep navy + electric blue) — default
  - Horizon — warm, editorial (charcoal + terracotta)
  - Topbar moon/sun toggles mode only and preserves the chosen palette
- **Settings page theme picker** — Aurora/Horizon swatch cards with mini-palette previews; Light/Dark mode segmented control alongside
- **Sidebar Upgrade-to-Extended panel is dismissible** — × button persists `webui_upgrade_panel_dismissed` so it doesn't return on every page load
- **Dashboard zero-state helpers** — KPI tiles showing `0` render a one-line italic teaching helper; the audit-activity heatmap collapses to a "no captures in last 48h" prompt with direct links to start a session or run preflight
- **Header TIER pill standardized** — same structure as ORG/ENV pills (label + mono value), no longer a celebratory color-coded chip
- **Tabular numerals** applied across counters, timestamps, durations, deltas, version strings, and `code`/`pre` blocks so digit-heavy columns don't shift between values
- **Section legend scope helpers** — Settings fieldsets include a small mono-formatted scope label (e.g. `workspace-scoped, persisted to config.json`, `secrets in OS keyring or Vault`)
- **Motion tokens + responsive layout** — `--ease-standard|emphasized|decel`, `--dur-fast|base|slow` design tokens with `prefers-reduced-motion` collapse; `<960px` sidebar collapses to icon rail, `<720px` hero + grid-2 reflow; `.toolbar` gains `overflow-x: auto`; ack-row fade + chip-pop on alert acknowledgment; standardized `.pill`, `.chip`, `.tile` transitions
- **Themed status classes** — `.pill--state-*`, `.card--error`, `.card--warn`, `tr.is-drifted`, `.tier-card.is-active` replace inline `oklch()` styles in the topbar pill, run-detail page, and tier overview
- **Form helper** — generic `data-loading-label` + `data-confirm-action` driver for loading state and confirmations on Ack / Ack-all / Run-now / Disable / Test / Remove
- **Credentials reconfigure form** — added `AppRole (Wrapped)` option (was missing); `token_file_path` is now an editable input pre-filled from saved config; `vault_wrapping_token` and `vault_token_file_path` now submit and persist correctly
- **Setup wizard** — first-run redirect-to-setup middleware bootstraps a fresh install before any other route is reachable
- `webui_theme`, `webui_accent`, `webui_mode`, and `webui_upgrade_panel_dismissed` fields persisted to `config.json`

### Changed

- **Appearance config migration** — `webui_theme` no longer holds `light|dark` (that is now `webui_mode`); legacy accent values (`cyan|amber|violet|lime|mono`) are migrated transparently on read (`cyan|violet|mono → aurora`, `amber|lime → horizon`) so existing config.json files keep working

### Fixed

- Tier page not reflecting a tier change until hard-refresh — page now reads tier directly from disk on every load and includes `Cache-Control: no-store`; changing the tier reloads the in-memory context immediately
- Environment Activate button appearing to do nothing — `ctx()` singleton was not refreshed after writing the new `active_environment` to disk, so the list page rendered stale state
- Session list showing stale active-session indicator after switching sessions (same stale-context pattern)
- Fresh installs deadlocked at first run — `/setup` was not in `_AUTH_BYPASS_PREFIXES`, so the redirect-to-setup middleware hit a 401 before the wizard could load
- `--reload` / `ATLAS_WEBUI_RELOAD=1` crashed immediately — uvicorn requires an import-string for reload; the dev path now passes the factory correctly
- Dashboard / sessions / reports / diff routes blocking the event loop — sync session-metadata reads and `pd.read_parquet` calls now run via `run_in_threadpool`; reports list no longer reopens every `session.json` a second time, halving disk I/O
- Job cancellation could deliver two terminal SSE events and leave half-open network connections — `cancel()` now signals a cooperative `threading.Event` that workers consult at safe checkpoints; status flips only when the worker actually unwinds
- `JobRegistry._lock` was an `asyncio.Lock` bound to the first request's loop — switched to `threading.Lock` so the registry survives reload and test loops cleanly
- Terminal SSE event silently dropped when a subscriber's queue was full — full queues now drop the oldest event to make room for the close signal
- Report links broken when audited log content contained literal `03_report.html` / `04_operational.html` / `05_arch.html` strings — anchor-targeted regex now only mutates `href="…"` attributes
- `services/config.update_config` had a read-modify-write race — concurrent `PATCH /api/settings/appearance` could lose the earlier writer's update; now serialized via a process-wide lock
- Temp files leaked into `/tmp` indefinitely — diff renders and JSON exports now delete their temp file via a `BackgroundTask` after the response streams
- Session activation only moved the active-session pointer, leaving the previous session's environment, ruleset, and profile in place — now restores the full context atomically via `activate_session_context`, matching the CLI
- Session creation read tier from root `config.json` only, recording the wrong tier when the active environment overlay disagreed — now resolves tier through the same overlay-aware path as `load_config()`
- `POST /config` wrote tier to root `config.json` only; the active environment overlay's `tier` would silently undo the change — tier is now mirrored into the active overlay alongside the root write
- `/continuous/run-now` returns `429` when a run is already in flight for the env, instead of silently launching a duplicate

### Security

- **Self-signed TLS** auto-generated on first launch (`~/.atlas/.webui-cert.pem`); SHA-256 fingerprint printed to stderr; `--reset-tls` to regenerate
- **OS-user binding** via filesystem token (`~/.atlas/.webui-token`); browser auto-opens with a one-time nonce URL; all routes require a signed session cookie; `--reset-token` invalidates all sessions
- **Session cookies** signed with `HMAC(SHA-256(token ⊕ cookie_secret), session_id)` — a process running as the same OS user can no longer forge cookies from `~/.atlas/.webui-token` alone. Cookie secret persists at `~/.atlas/.webui-cookie-secret` (mode 0600) so browser sessions survive `restart`, reboots, and auto-restarts; rotating either file invalidates every outstanding cookie
- **Stateless HMAC CSRF tokens** on every `POST` / `PATCH` / `DELETE`; hidden input injected in all forms; AJAX calls send `X-CSRF-Token` header
- **Path-traversal guard** (`security/paths.py`) on all `FileResponse` routes
- **Credential redaction** (`security/redact.py`) in all exception log handlers; uvicorn access log strips query strings
- **Response headers** on every response: `Content-Security-Policy`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, `Strict-Transport-Security`, `Permissions-Policy`
- **Self-hosted fonts** — Google Fonts CDN removed; Inter, JetBrains Mono, and Instrument Serif self-hosted as WOFF2 under `static/fonts/`
- **Inline scripts extracted** — SSE inline script moved from `jobs/detail.html` to `static/js/job-stream.js`
- **Append-only audit log** at `~/.atlas/webui-audit.log` (chmod 600, rotates at 10 MB); one JSON line per state-changing request including OS user, path, status, session ID, and redacted form payload
- `_used_nonces` is now mutated under a lock — two parallel `/auth?nonce=…` requests for the same nonce can no longer both succeed
- CSP per-response nonce is generated and threaded through templates; enforcement against inline scripts is staged (still `script-src 'self' 'unsafe-inline'` until templates' `onclick=`/`onchange=` handlers migrate to delegated listeners)
- Setup error rendering shows a generic message — full exception class/message/traceback no longer reaches the browser
- Job records no longer carry `metadata["traceback"]` — full tracebacks are server-log only and can no longer be exposed via the job-detail view
- `POST /architecture/save` caps body at 256 KB and validates field shapes (status enum, section name allowlist) before persisting — disk-fill DoS and stored-data-into-report-XSS paths closed
- Compliance / operational / architecture reports are served with a sandboxed CSP (`default-src 'none'`, `connect-src` blocked, `form-action 'none'`, `base-uri 'none'`) — captured data rendered in a report can no longer reach back into the WebUI's authenticated origin

### Performance

- HTML responses are gzip-compressed (≥1 KB)
- `/static` assets ship with `Cache-Control: public, max-age=31536000, immutable`
- Dashboard reads the session list once per request (not twice) and reuses it for the heatmap aggregation
- `os_scheduler.status()` cached per-env for 30 s with explicit invalidation on install / uninstall — the topbar pill no longer shells out 4–5 times to `systemctl` / `launchctl` on every page render
