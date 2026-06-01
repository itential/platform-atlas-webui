#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Platform Atlas WebUI entry point.

Two execution modes:

* **Bare / start mode** (default).  Runs uvicorn in the foreground exactly
  as the previous releases did.  Adding ``--daemon`` forks the process,
  detaches from the terminal, redirects stdio to ``~/.atlas/webui.log`` and
  writes the PID to ``~/.atlas/webui.pid``.

* **Subcommands.**  ``stop``, ``status``, ``restart`` operate on the daemon
  via the PID file.  ``login-url`` mints a fresh login URL (useful after
  a daemon restart, since the URL is otherwise tucked away in the log).

Backwards compatible: every previously-valid invocation
(``platform-atlas-webui [--host …] [--port …]``) keeps working unchanged.
"""

from __future__ import annotations

import argparse
import logging
import sys

# -- Windows UTF-8 bootstrap ----------------------------------------------
# Reconfigure stdout/stderr to UTF-8 on Windows before anything prints.
# Windows consoles default to a locale code page (cp1252/cp850) that cannot
# represent Unicode characters (✓ ✘ ● em-dashes, box-drawing, etc.).
if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        try:
            if hasattr(_s, "reconfigure"):
                _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    del _s
# -------------------------------------------------------------------------

from platform_atlas_webui.app import create_app
from platform_atlas_webui.config import WebUISettings


# ─────────────────────────────────────────────────────────────────────
# Argument parsing
# ─────────────────────────────────────────────────────────────────────

def _add_run_args(p: argparse.ArgumentParser) -> None:
    """Flags shared by the bare invocation and the implicit `start` flow."""
    p.add_argument("--host", default=None, help="Bind host (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=None, help="Bind port (default: 8765)")
    p.add_argument(
        "--reload",
        action="store_true",
        help="Enable uvicorn reload (development only). Cannot combine with --daemon.",
    )
    p.add_argument(
        "--allow-remote",
        action="store_true",
        help="Allow binding to non-loopback interfaces. Off by default.",
    )
    p.add_argument(
        "--log-level",
        default=None,
        choices=["debug", "info", "warning", "error", "critical"],
        help="uvicorn log level (default: info)",
    )
    p.add_argument(
        "--reset-tls",
        action="store_true",
        help="Regenerate the self-signed TLS certificate and exit.",
    )
    p.add_argument(
        "--reset-token",
        action="store_true",
        help="Regenerate the OS-user binding token (invalidates existing browser sessions).",
    )
    p.add_argument(
        "--no-browser",
        action="store_true",
        help="Skip auto-opening the browser on launch.",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help=(
            "Suppress server and application logs on the console. "
            "All logs are still written to ~/.atlas/webui.log."
        ),
    )
    p.add_argument(
        "--daemon",
        action="store_true",
        help="Detach to the background, write a PID file, log to ~/.atlas/webui.log.",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="platform-atlas-webui",
        description="Run the optional Platform Atlas WebUI.",
    )
    _add_run_args(parser)

    sub = parser.add_subparsers(
        dest="action", required=False, metavar="<action>",
        title="Process control",
    )

    # stop / status / restart operate on the PID file
    sub.add_parser("stop", help="Stop the running daemonized WebUI (SIGTERM via PID file)")
    sub.add_parser("status", help="Show whether a daemonized WebUI is running")

    restart_p = sub.add_parser(
        "restart",
        help="Stop the running daemon and start a fresh one in --daemon mode",
    )
    # Restart always re-spawns daemonized; expose the run flags so the user
    # can change host/port at restart time without stopping manually first.
    _add_run_args(restart_p)

    login_p = sub.add_parser(
        "login-url",
        help="Print a fresh, single-use login URL (valid for 60s).",
    )
    login_p.add_argument("--host", default=None,
                         help="Host to use in the URL (default: 127.0.0.1 or whatever the running daemon is bound to)")
    login_p.add_argument("--port", type=int, default=None,
                         help="Port to use in the URL (default: 8765 or whatever the running daemon is bound to)")

    return parser


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return _build_parser().parse_args(argv)


# ─────────────────────────────────────────────────────────────────────
# Run-mode (start) — the original entrypoint behavior, plus --daemon
# ─────────────────────────────────────────────────────────────────────

def _resolve_settings(args: argparse.Namespace) -> WebUISettings:
    base = WebUISettings.from_env()
    return WebUISettings(
        host=args.host or base.host,
        port=args.port if args.port is not None else base.port,
        reload=args.reload or base.reload,
        log_level=args.log_level or base.log_level,
        allow_remote=args.allow_remote or base.allow_remote,
    )


def _is_compatible_atlas(version: str) -> bool:
    """Return True if *version* satisfies the >=1.7,<2.0 constraint."""
    try:
        parts = version.split(".")
        major, minor = int(parts[0]), int(parts[1])
        return (major == 1 and minor >= 7) or (major > 1 and major < 2)
    except (ValueError, IndexError):
        return True  # unparseable — don't block startup


def _run_server(args: argparse.Namespace) -> int:
    """Foreground (or about-to-fork) launch path. Returns a process exit code."""
    settings = _resolve_settings(args)
    quiet = getattr(args, "quiet", False)

    # Public-bind guard.
    if settings.host in ("0.0.0.0", "::") and not settings.allow_remote:
        print(
            "Refusing to bind to a public interface without --allow-remote "
            "(or ATLAS_WEBUI_ALLOW_REMOTE=1).",
            file=sys.stderr,
        )
        return 2

    # --reload + --daemon doesn't make sense: the reloader spawns a child
    # process that would re-run main() and try to daemonize again.
    if args.daemon and settings.reload:
        print(
            "Cannot combine --daemon with --reload. Pick one.",
            file=sys.stderr,
        )
        return 2

    try:
        import uvicorn
    except ImportError:
        print(
            "uvicorn is not installed. Install the WebUI extras: "
            "pip install platform-atlas-webui",
            file=sys.stderr,
        )
        return 1

    # ── Version banner + CLI library check ────────────────────────────────
    # Print early — before any potentially slow operations — so the terminal
    # is never silent while the process is working (TLS key generation can
    # take a moment on first run, especially without a warm Python cache).
    from platform_atlas_webui import __version__ as WEBUI_VERSION
    from platform_atlas_webui.security import tls as tls_mod

    print(f"\nPlatform Atlas WebUI v{WEBUI_VERSION}\n", file=sys.stderr)

    # Verify the platform-atlas CLI library is present and version-compatible.
    # The package declares platform-atlas >=1.7,<2.0 as a hard dependency, so
    # pip normally guarantees this — but an in-place upgrade or manual install
    # can leave a mismatched version behind. Showing it here also gives operators
    # an immediate sanity-check that the right library is backing the WebUI.
    print("  CLI  Checking platform-atlas library...", end="", flush=True, file=sys.stderr)
    try:
        from platform_atlas.core._version import __version__ as ATLAS_VERSION
        _atlas_ok = _is_compatible_atlas(ATLAS_VERSION)
        if _atlas_ok:
            print(f" v{ATLAS_VERSION}  ✓\n", file=sys.stderr)
        else:
            print(
                f" v{ATLAS_VERSION}  ✗ (expected >=1.7,<2.0 — run: pip install "
                f"\"platform-atlas>=1.7,<2.0\")\n",
                file=sys.stderr,
            )
            return 1
    except ImportError:
        print(
            "\n\n  CLI  ERROR: platform-atlas library not found.\n"
            "       The WebUI requires platform-atlas >=1.7,<2.0.\n"
            "       Install it with: pip install \"platform-atlas>=1.7,<2.0\"\n",
            file=sys.stderr,
        )
        return 1

    # ── TLS ────────────────────────────────────────────────────────────────
    _cert_exists = tls_mod.CERT_FILE.is_file() and not args.reset_tls
    if _cert_exists:
        print("  TLS  Checking certificate...", end="", flush=True, file=sys.stderr)
    else:
        print("  TLS  Generating self-signed certificate...", end="", flush=True, file=sys.stderr)

    try:
        fingerprint, newly_generated = tls_mod.ensure_cert(reset=args.reset_tls)
    except Exception as exc:
        print(
            f"\n\n  TLS  ERROR: Could not load or generate certificate: {exc}\n"
            "       Run with --reset-tls to regenerate.\n",
            file=sys.stderr,
        )
        return 3

    if newly_generated:
        print(
            f" done\n"
            f"       Path:        {tls_mod.CERT_FILE}\n"
            f"       Fingerprint: {fingerprint}\n"
            f"       (Browser will warn once on first connect — verify the\n"
            f"        fingerprint matches before clicking \"Advanced → Proceed\".)\n",
            file=sys.stderr,
        )
    else:
        print(f" valid (expires {tls_mod.expiry_date()})\n", file=sys.stderr)

    if args.reset_tls:
        print("  TLS certificate regenerated. Restart without --reset-tls to launch.", file=sys.stderr)
        return 0

    # ── Logging ────────────────────────────────────────────────────────────
    # Always write to ~/.atlas/webui.log so logs survive --quiet foreground
    # runs and are accessible after the fact. Console output is suppressed
    # in --quiet mode; in normal mode both destinations receive logs.
    from platform_atlas_webui.security.redact import install_uvicorn_access_filter
    install_uvicorn_access_filter()

    _log_file = tls_mod.CERT_FILE.parent / "webui.log"
    _fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    _handlers: list[logging.Handler] = [logging.FileHandler(str(_log_file), encoding="utf-8")]
    if not quiet:
        _handlers.append(logging.StreamHandler())

    logging.basicConfig(
        level=settings.log_level.upper(),
        format=_fmt,
        handlers=_handlers,
        force=True,
    )

    # ── Token + nonce ──────────────────────────────────────────────────────
    from platform_atlas_webui.security.tokens import (
        generate_nonce, load_token, reset_token as _reset_token,
    )

    if getattr(args, "reset_token", False):
        _reset_token()
        print("  Auth Token regenerated. Existing browser sessions invalidated.\n", file=sys.stderr)

    _token_exists = tls_mod.CERT_FILE.parent.joinpath(".webui-token").is_file()
    if not _token_exists:
        print("  Auth Generating login token...", end="", flush=True, file=sys.stderr)
    else:
        print("  Auth Verifying login token...", end="", flush=True, file=sys.stderr)

    load_token()  # ensure token file exists before server starts
    print(" done\n", file=sys.stderr)

    nonce = generate_nonce()
    base_url = f"https://{settings.host}:{settings.port}"
    login_url = f"{base_url}/auth?nonce={nonce}"
    print(f"  Listening on {base_url}", file=sys.stderr)
    print(f"  Login URL:  {login_url}", file=sys.stderr)
    if quiet:
        print(f"  Logs        {_log_file}", file=sys.stderr)
    print(file=sys.stderr)

    # Daemon mode never opens a browser — there's no controlling terminal
    # to attach a window to, and the parent process exits immediately
    # after the fork.
    open_browser = not args.no_browser and not args.daemon
    if open_browser:
        import webbrowser
        print("  Opening browser...\n", file=sys.stderr)
        webbrowser.open(login_url)

    # ── Daemonize (optional) ──────────────────────────────────────────────
    if args.daemon:
        from platform_atlas_webui import daemon as daemon_mod
        if not daemon_mod.is_supported():
            print(
                "  --daemon is POSIX-only. On Windows, run the WebUI under "
                "your service supervisor of choice.",
                file=sys.stderr,
            )
            return 2
        log_path = daemon_mod.log_file()
        pid_path = daemon_mod.pid_file()
        print(
            f"  Detaching to background ─ pid: {pid_path}  log: {log_path}\n",
            file=sys.stderr,
        )
        try:
            # Returns in the grandchild only. Both intermediate parents
            # call os._exit(0) inside daemonize().
            daemon_mod.daemonize(pidfile=pid_path, logfile=log_path)
        except daemon_mod.DaemonError as exc:
            print(f"  ✘ {exc}", file=sys.stderr)
            return 4

    # ── Server ─────────────────────────────────────────────────────────────
    _uvicorn_log_level = "warning" if quiet else settings.log_level
    common_kwargs: dict = {
        "host": settings.host,
        "port": settings.port,
        "log_level": _uvicorn_log_level,
        "ssl_certfile": str(tls_mod.CERT_FILE),
        "ssl_keyfile": str(tls_mod.KEY_FILE),
    }
    if quiet:
        common_kwargs["access_log"] = False

    if settings.reload:
        # --reload is mutually exclusive with --daemon (validated above)
        # so this branch is always foreground.
        print("  Starting server (--reload mode)...\n", file=sys.stderr)
        uvicorn.run(
            "platform_atlas_webui.app:create_app",
            factory=True, reload=True, **common_kwargs,
        )
    else:
        print("  Starting server...", end="", flush=True, file=sys.stderr)
        app = create_app(settings)
        print(" ready\n", file=sys.stderr)
        uvicorn.run(app, **common_kwargs)
    return 0


# ─────────────────────────────────────────────────────────────────────
# Subcommand handlers
# ─────────────────────────────────────────────────────────────────────

def _handle_stop() -> int:
    from platform_atlas_webui import daemon as daemon_mod
    if not daemon_mod.is_supported():
        print("Daemon mode is POSIX-only.", file=sys.stderr)
        return 2
    try:
        stopped, pid = daemon_mod.stop_daemon()
    except daemon_mod.DaemonError as exc:
        print(f"✘ {exc}", file=sys.stderr)
        return 1
    if stopped:
        print(f"✓ Stopped Atlas WebUI (PID {pid}).", file=sys.stderr)
        return 0
    if pid is not None:
        print(
            f"No running WebUI; cleaned up stale PID file (was PID {pid}).",
            file=sys.stderr,
        )
    else:
        print("No running WebUI; nothing to stop.", file=sys.stderr)
    return 0


def _handle_status() -> int:
    from platform_atlas_webui import daemon as daemon_mod
    if not daemon_mod.is_supported():
        print("Daemon mode is POSIX-only.", file=sys.stderr)
        return 2
    running, pid = daemon_mod.status()
    pid_path = daemon_mod.pid_file()
    log_path = daemon_mod.log_file()
    if running:
        print(
            f"● Atlas WebUI is running\n"
            f"    PID:      {pid}\n"
            f"    PID file: {pid_path}\n"
            f"    Log:     {log_path}",
            file=sys.stderr,
        )
        return 0
    if pid is not None:
        print(
            f"○ Atlas WebUI is NOT running (stale PID file referenced {pid}).\n"
            f"    PID file: {pid_path}",
            file=sys.stderr,
        )
        return 3
    print("○ Atlas WebUI is NOT running.", file=sys.stderr)
    return 3


def _handle_restart(args: argparse.Namespace) -> int:
    """Stop the existing daemon (if any), then start a new one daemonized."""
    rc = _handle_stop()
    if rc not in (0, 3):
        return rc
    # Force daemon mode for restart — the whole point of restart is to
    # leave a backgrounded process behind.
    args.daemon = True
    return _run_server(args)


def _handle_login_url(args: argparse.Namespace) -> int:
    """Mint a fresh login URL using the on-disk binding token.

    Useful after a daemon restart, where the URL was printed to
    ``~/.atlas/webui.log`` and the user wants a clean copy at the
    terminal. The nonce is good for 60 seconds and burns on first use.
    """
    from platform_atlas_webui.security.tokens import generate_nonce, load_token

    base = WebUISettings.from_env()
    host = args.host or base.host
    port = args.port if args.port is not None else base.port

    load_token()
    nonce = generate_nonce()
    print(f"https://{host}:{port}/auth?nonce={nonce}")
    return 0


# ─────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    """Platform Atlas WebUI Main Entrypoint."""
    args = _parse_args(argv)
    action = getattr(args, "action", None)

    if action is None:
        # Bare invocation — original "run server" behavior, with optional
        # --daemon detach.
        return _run_server(args)
    if action == "stop":
        return _handle_stop()
    if action == "status":
        return _handle_status()
    if action == "restart":
        return _handle_restart(args)
    if action == "login-url":
        return _handle_login_url(args)
    # argparse already rejects unknown subcommands; this branch is
    # defensive only.
    print(f"Unknown action: {action}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
