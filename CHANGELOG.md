# Changelog — Platform Atlas WebUI

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [2.3.0] - 2026-08-20

Requires `platform-atlas >=2.3.0,<3.0`.

### Added

- **Additional Validation Modules page under Settings** — `/config/avc-modules` lists every extended validation check with a checkbox, plus a reset-to-default action, matching the CLI's new `config edit` > Advanced menu. Both surfaces read and write the same setting, so a check disabled from one is disabled on the other. The standalone "RBAC authorization collection" checkbox on the main Settings page is gone — RBAC is now just one more module in this list, still off by default since it's privacy-sensitive.

### Changed

- **Horizon theme now matches the CLI report's colors and fonts exactly** — light mode's paper background, ink text, and terracotta accent are pulled directly from the report design, and dark mode has been redesigned around the report's own dark navy palette instead of an unrelated color set. Horizon is now the only WebUI theme using the report's serif and system fonts rather than the site-wide font pairing every other theme uses.
- **WebUI report generation now produces the single `report.html`** — matches the CLI's 2.3.0 report redesign: Compliance, Operational, and Architecture render as pages in one file instead of three separate reports, and session export bundles the new single report.

### Removed

- **Separate Operational and Architecture report-viewer routes are gone** — now that the CLI produces one report file instead of three, those sections are pages inside the single report view rather than their own WebUI URLs.

---

## [2.2.0] - 2026-07-06

Requires `platform-atlas >=2.2.0,<3.0`.

### Added

- **`network_policy` setting on the Settings page** — the Behaviour section of `/config` now exposes the `network_policy` field (`allow` / `disallow`) introduced in platform-atlas 2.2.0, letting users block all third-party outbound connections directly from the WebUI.
- **`enable_rbac_collection` setting on the Settings page** — lets you turn on Platform 6 RBAC capture from the WebUI, matching the CLI's 2.2.0 config field. The RBAC tab itself isn't rendered in the WebUI report viewer yet, so captured data won't display here until that lands in a future release.

### Changed

- **Switching to Extended no longer flips the tier before credentials are verified** — clicking Switch to Extended used to write the tier immediately, so abandoning the credential-check page left the environment half-configured in Extended. The tier now switches only once every required credential is confirmed present and you click Finish upgrade; leaving any other way keeps you in Standard, unchanged. Mirrors the CLI's guided `tier upgrade` walkthrough.

---

## [2.1.0] - 2026-06-24

Requires `platform-atlas >=2.1.0,<3.0`.

### Added

- **ControlMaster SSH socket manager** — environments that use ControlMaster SSH now have a dedicated **SSH Sockets** page (`/environments/{name}/sockets`). It shows the live status of every CM node's socket (`open`, `stale`, `not found`, or `unconfigured`), surfaces a copy-pasteable `ssh -M` open command for each node that needs one, and provides a **Clean stale** button that removes stale socket files in one click so fresh connections can be opened. The page is invisible to environments with no ControlMaster nodes — only the affected environment's detail page shows the **SSH Sockets** button in its Topology card. Mirrors the CLI's `platform-atlas env sockets` command.

- **Support bundle environment context bar** — a prominent "Active environment — confirm before collecting" panel above the bundle form shows the active environment name, tier, and every connection target (Platform URL, SSH hostname, or Gateway URL) before you submit, with a link to switch environments if the wrong one is active.

### Added

- **Select both Gateway 4 and Gateway 5 together** — SaaS and Extended environment forms now support the `gw4-gw5` gateway kind, letting you audit a GW4 API target and a GW5 node in a single run rather than choosing one or the other.

### Fixed

- **GW4 API target missing for `gw4-gw5` environments that have a GW5 topology node** — the core `Config.targets` / `Config.all_targets` fix (CLI 2.1.0) eliminates silent GW4 collector drop-out that affected runs started from the WebUI runner.

### Changed

- **Increased font sizes across the entire WebUI for better readability** — base body size raised from 13 px to 15 px; all hardcoded sizes in `atlas.css` and inline template styles scaled proportionally (11 px descriptive text → 13 px, 12 px labels → 14 px, 13 px body → 15 px). Display numbers and large headings are unchanged.

---

## [2.0.0] - 2026-06-16

Requires `platform-atlas >=2.0.0,<3.0`.

### Added

- **SaaS tier — full WebUI support for the CLI's single-gateway audit mode.** Audits one standalone **GW4 or GW5** with no Platform, MongoDB, or Redis anywhere.
  - **Create from the Environments page:** the tier choice gains **SaaS** (create-only — tier and gateway kind are fixed for life). The form pre-picks your **global default tier**, so a SaaS install defaults to SaaS with Standard/Extended one click away. Picking it swaps to a three-step **Identity → Gateway → Review** flow — choose the kind, then only that gateway's questions (**GW4**: API URL/username + optional *"Collect deeper config over SSH"*; **GW5**: SSH / Docker Compose / Helm picker). The form builds a `gateway_only` topology server-side (or none for an API-only GW4); SaaS inputs use their own field names so they can't collide with the Extended step.
  - **Tier resolution accepts `saas` everywhere** it previously allowed only two values (template context, config service, tier/credentials/config routes), so SaaS renders correctly app-wide — dashboard hero, session/report tier chips (SaaS in Itential Pink), job timeline badge, diff pickers, config and ruleset pages.
  - **Conversion is blocked with plain-language errors** on the form (tier select locked, gateway kind fixed on edit) and the server (`save_environment` rejects SaaS↔Standard/Extended and kind changes); `/tier` explains a SaaS env keeps its own tier, and `config`'s default-tier mirror never overwrites a SaaS overlay.
  - **Runs honor the SaaS shape:** capture jobs label the tier, never run MongoDB aggregation pipelines, and preflight runs gateway SSH but never Kubernetes. The report job produces the CLI's **single merged `03_report.html`** (compliance + embedded Architecture Overview) — no `04_operational.html` or `05_arch.html` — with matching timeline messages.
  - **Credentials pages are SaaS-honest:** availability derives from the CLI's per-tier applicable sets, so SaaS shows the SSH passphrase and GW4 password as usable while Platform/MongoDB/Redis keys read as not in this tier; no Platform client secret is demanded. The support-bundle page takes the config-snapshot path with a SaaS label.
  - **Profiles are tier-scoped too:** under SaaS the Rulesets page offers only `saas-gateway4` / `saas-gateway5` (each enabling just that gateway's `IAG-` rules); Standard/Extended never see them. The activate endpoint rejects a cross-tier profile posted directly.
  - **First-run setup offers SaaS too.** The onboarding tier step gains a third card; picking SaaS swaps Connect from Platform OAuth to the gateway questions (GW4 API + optional SSH, or the GW5 SSH/Compose/Helm source) and hides the platform-tier GW4 extras. It creates your first environment as SaaS and writes **SaaS as the global default tier** (new envs pre-pick it; each can still choose Standard/Extended), then walks you to preflight and the gateway-scoped architecture form.

- **Legacy 2023.x rulesets/profiles hidden everywhere unless the active env is marked legacy** (`legacy_profile` set). Hiding now happens centrally in the core manager, covering the two surfaces that previously leaked 2023 entries — the **Cmd+K search palette** and the **Continuous audit** config page. The activate endpoint rejects a hidden legacy ruleset/profile posted directly; legacy-marked envs are unaffected.
- **SaaS environments see nothing Legacy.** 2023.x is a Platform concept, so the form's "Legacy" fieldset disappears for SaaS and the server strips `legacy_profile` from every SaaS save (self-healing stale markers too), so a gateway-only audit can never be marked legacy.

- **Environment-form SSH — key picker, inline passphrase, and a real connection test.** The SaaS gateway SSH block now offers the same `~/.ssh` private-key dropdown as Extended (discovered keys, custom-path fallback, "None — use SSH agent") instead of a bare path field. Both SSH sections gained an **SSH key passphrase** field that saves into the environment's own credential store (where capture reads it), with "blank = keep current" (Vault envs point at the `ssh_passphrase` KV key, since Vault is read-only). A **Test SSH connection** button on the SaaS block tries host/user/port/key/passphrase with the same `SSHTransport` capture uses — failures render the transport's plain-language reason inline; a blank passphrase tests with the stored one, so editing never requires retyping it.

- **Export Platform assets with a support bundle** — an optional **Add Platform assets** step lets you pick Workflows, JSON Transformations (JST), JSON Forms, or whole Projects from the active environment; Atlas exports them over Platform OAuth into an `exports/` folder in the bundle ZIP with an `EXPORT_MANIFEST.json`. Replaces manually exporting each item from the Platform UI and attaching it to the ticket. Exported as-is (no redaction). Works in Standard and Extended (Platform OAuth only, no SSH). The CLI bundle is unchanged; artifact export is WebUI-only.
- **Asset picker modal** — a tab per asset type (live count + selected badge), type-ahead search, and pagination. Tabs pre-load for instant switching; selections persist across tabs and searches with a running footer count. Backed by a new `GET /support-bundle/assets/{type}` endpoint that searches and paginates server-side, so environments with thousands of workflows never load the whole list at once. Themed with anime.js motion, and it surfaces a clear message if the Platform can't be reached.
- **Choose a credential backend — OS Keyring, Encrypted Local File, or HashiCorp Vault — in the setup wizard and environment form.** Both offer all three explicitly (mirroring the CLI), **OS Keyring recommended**. Selecting Vault now reveals inline (no save-and-reopen) where Vault's own connection settings live (OS keyring recommended, or the encrypted file when the keyring isn't usable). Atlas uses exactly the store you pick and never auto-switches.
- **Credentials page reports the chosen store honestly** — an encrypted-local-file environment (machine-bound `~/.atlas/credentials.enc`, common on headless Linux hosts) is labeled as the encrypted file with an amber notice, never dressed up as "OS Keyring." If the file can't be read here (missing key salt, or made on another machine/user) a plain-language recovery message explains how to recreate it. Storing/removing secrets from the browser is unaffected.
- **The report's rule detail now explains *why* a rule was skipped, color-coded.** A Skipped rule's slide-over shows a callout in one of three families: **Couldn't reach this system** (bronze — subsystem down or config unreadable; for an unreachable MongoDB/Redis/Gateway it includes the collector's real error, e.g. *"authentication failed at db.internal:27017"*), **No data collected for this check** (slate — section collected but the setting absent), or **Conditional check — not applicable** (periwinkle — skipped by a dependency or version gate). The Skipped pill gets a matching colored dot. Fully-unreachable subsystems' rules, which previously vanished, are now resurfaced as enriched "couldn't connect" skips. Mirrors the CLI report; the hues track every theme. Relies on the skip-reason classification in `platform-atlas` 2.0.0.
- **Gateway 5 config source picker in the environment form** (create + edit, Extended). The standalone and HA2 topology sections gain an **Automation Gateway 5** selector with three sources — **SSH** (`GATEWAY_*` via `printenv`), **Docker Compose file**, or **Helm values file** — matching the CLI. Choosing a file reveals a path input and saves an SSH-less node (`transport: gateway5_file`) so a containerized GW5 can be audited with no SSH; SSH reveals the host field. The picker pre-fills on edit and survives validation errors; "None" removes the gateway. Requires `platform-atlas >=2.0.0`.
- **Gateway 5 server-config source in the environment form and setup wizard.** The GW5 source picker gains a 4th option — **SSH (server config)** — that reads the server's `gateway.conf` over SSH (a config-path field appears beside the SSH host). Builds a normal SSH node carrying `gateway5_conf_path`; covers standalone/HA2, SaaS, and first-run setup, and pre-fills on edit.

### Changed

- **Setup wizard's Mode step redesigned as an equal-thirds accordion.** The three tier cards (Standard / Extended / SaaS) share one fixed-height row — the selected one expands to its full bullets while the others compress to spines (icon, name, one-line pitch), so SaaS is no longer an odd-one-out stretched below the pair. Panels stagger in, hovering a spine lifts it with accent cues, and arrow keys still move the native `tier` radio group; narrow windows become a vertical accordion. Fixed measures mean text never re-wraps on hover/expand. The selection contract (posted `tier`, the SaaS Connect-step swap) is unchanged.
- **Profile dropdowns no longer offer "— None —".** An audit always runs with a profile, so the Rulesets activate form and session create form list only real profiles (preselecting the active one, or the first visible). When none is visible, a disabled "no profiles available" placeholder renders.
- **Minimum `platform-atlas` raised to 2.0.0** — the explicit encrypted-local-file backend the WebUI now offers only exists in 2.0.0+, so the dependency metadata and startup check require it.
- **Settings page reframed for multiple environments.** A read-only **Active environment** panel shows the tier and credential store in effect (with links to change them), and the editable tier / credential-backend fields are now labeled **defaults for new environments** — each environment carries its own. Leftover single-environment framing is gone. No change to what `config.json` stores.
- **Page-title accents across the WebUI.** Every header's secondary phrase (the Dashboard's "at a glance", Environments' "audit targets", the active ruleset's profile, a job's id, …) now renders in the theme **accent** color instead of muted grey. (The "no active environment" placeholder stays muted — it marks an absence.)
- **Dashboard active-session card restyled.** Dropped the fixed pink/purple glow — the "live session" emphasis now comes from a subtle theme-**accent** gradient inside the panel (and an accent-coloured capture pulse), so the card fits every palette instead of always reading pink.

### Fixed

- **Standard-tier environments no longer ask Extended-only questions in the form.** When the active tier was Extended, selecting "Standard" still showed the SSH private key, topology, and Kubernetes step — `is_standard` was a snapshot of the *active* tier and couldn't react to the per-env dropdown. The wizard is now tier-reactive: Standard hides the whole SSH/Topology/Kubernetes step (and its Review rows) and collapses to three steps; Extended brings it back. Standard captures never used those fields.
- **Credentials page is honest about the encrypted-file backend and lets you manage it.** It no longer claims the OS keyring is "broken" and was "fallen back" to — the file is presented as the deliberate choice it is, in a neutral (not amber) banner. **Set / Edit / Remove** now work for the file backend (they were keyring-only, so file users couldn't change a secret). And the active environment's tier is now its *actual* tier — the page read the root-config tier, so a Standard env could show "extended mode."
- **The Tier page reflects the active environment's tier.** It read the root-config tier (could show "Extended" while Standard was active); it now resolves the active env's overlay tier (and `ATLAS_TIER`), matching the topbar.
- **Config Doctor stops flagging a chosen encrypted-file backend as a warning, and links credential fixes into the app.** A deliberately-selected encrypted file now reports OK instead of a yellow warning (an *unreadable* file still fails; an insecure/broken OS keyring still warns). Rows that said "run `platform-atlas config credentials`" now link to the in-app Credentials page. The CLI `config doctor` is unchanged. The summary tile is relabeled "Verdict" → "Summary" and no longer reports "Warnings present" when there are none.
- **Support-bundle jobs can no longer hang indefinitely** — a 30-minute backstop fails a stuck job (e.g. an unresponsive Platform connection during artifact export) instead of tying up a worker thread.
- **Settings saves now take effect immediately — Debug logging included.** Saving wrote `config.json` but never reloaded the in-process Atlas context, so toggles like **Debug logging** (collector metrics, session `debug.log` detail) silently did nothing until a restart. The save now re-initializes the context the way the tier switch does, a **"Settings saved." toast** confirms it, and the Debug toggle gained a hint explaining what it controls.
- **Saving Settings no longer resets a SaaS global default to Standard.** The "Default tier" select had no SaaS option, so a SaaS-default install fell back to "Standard" and any save rewrote the global default. The select now offers SaaS, only recognized tier values are written, and a SaaS default never touches existing environments' own tiers.

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
