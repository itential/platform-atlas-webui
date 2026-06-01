"""
Daemon helpers for the Atlas WebUI.

POSIX double-fork detachment, PID-file lifecycle, and start/stop/status
helpers used by the ``platform-atlas-webui`` entrypoint.

``platform-atlas-webui --daemon`` writes a PID file and runs uvicorn
detached from the controlling terminal. ``stop``, ``status``, and
``restart`` subcommands operate on the same PID file. If you want
auto-start on boot or auto-restart on crash, wrap the foreground
process under your service supervisor of choice (systemd, launchd,
supervisord, etc.) — Atlas does not ship its own unit files.

Windows is not supported here. ``is_supported()`` lets callers degrade
gracefully with a helpful error.
"""

from __future__ import annotations

import atexit
import errno
import logging
import os
import signal
import sys
import time
from pathlib import Path

from platform_atlas.core.paths import ATLAS_HOME

logger = logging.getLogger(__name__)


# ── Paths ─────────────────────────────────────────────────────────────

DEFAULT_PID_FILE = ATLAS_HOME / "webui.pid"
DEFAULT_LOG_FILE = ATLAS_HOME / "webui.log"


def is_supported() -> bool:
    """True on POSIX systems. Windows lacks os.fork() so daemonization is N/A."""
    return os.name == "posix"


def pid_file() -> Path:
    return DEFAULT_PID_FILE


def log_file() -> Path:
    return DEFAULT_LOG_FILE


# ── PID file helpers ──────────────────────────────────────────────────

def _read_pid(pidfile: Path) -> int | None:
    """Return the PID stored in ``pidfile``, or None if missing/garbage."""
    try:
        raw = pidfile.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.debug("PID file unreadable: %s", exc)
        return None
    try:
        pid = int(raw)
    except ValueError:
        logger.debug("PID file %s contains non-integer: %r", pidfile, raw)
        return None
    if pid <= 0:
        return None
    return pid


def _write_pid(pidfile: Path, pid: int) -> None:
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(f"{pid}\n", encoding="utf-8")
    try:
        pidfile.chmod(0o600)
    except OSError:
        # Filesystem may not honor mode (NFS, exotic FS); not worth failing.
        pass


def _process_alive(pid: int) -> bool:
    """True when the process exists and we can signal it. ``kill(pid, 0)``
    is the canonical POSIX existence check — no signal sent, just permission
    + presence verified."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by a different user. Treat as alive
        # so we don't accidentally double-launch.
        return True
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        if exc.errno == errno.EPERM:
            return True
        raise
    return True


def status(pidfile: Path | None = None) -> tuple[bool, int | None]:
    """Return ``(running, pid)``. ``pid`` may be non-None even when not
    running — that lets callers report a stale PID file."""
    pidfile = pidfile or DEFAULT_PID_FILE
    pid = _read_pid(pidfile)
    if pid is None:
        return False, None
    return _process_alive(pid), pid


# ── Daemonize ─────────────────────────────────────────────────────────

class DaemonError(RuntimeError):
    """Raised when daemonization can't proceed safely."""


def daemonize(
    *,
    pidfile: Path | None = None,
    logfile: Path | None = None,
    chdir: str = "/",
) -> None:
    """Detach the current process from its terminal and become a daemon.

    Implements the canonical Unix double-fork:
      1. First fork: parent exits, child becomes session leader via setsid.
      2. Second fork: child exits, grandchild can never re-acquire a TTY.
      3. Redirect stdin from /dev/null, stdout+stderr to ``logfile``.
      4. Write the grandchild's PID to ``pidfile``.

    The grandchild returns from this function; both parents exit cleanly
    via ``os._exit(0)`` so they don't run atexit hooks twice.

    Raises ``DaemonError`` if another instance is already running (PID
    file exists and the recorded PID is alive).
    """
    if not is_supported():
        raise DaemonError(
            "Daemon mode is POSIX-only. On Windows, run as a foreground "
            "process under your service supervisor of choice."
        )

    pidfile = pidfile or DEFAULT_PID_FILE
    logfile = logfile or DEFAULT_LOG_FILE

    # Refuse to double-launch — the existing instance owns the port and
    # the PID file. Caller can stop it first or use restart().
    running, existing_pid = status(pidfile)
    if running:
        raise DaemonError(
            f"Atlas WebUI is already running (PID {existing_pid}). "
            f"Stop it first with `platform-atlas-webui stop` or use "
            f"`platform-atlas-webui restart`."
        )
    # Stale PID file from a previous crash — drop it.
    if existing_pid is not None and pidfile.is_file():
        try:
            pidfile.unlink()
        except OSError:
            pass

    # ── First fork ────────────────────────────────────────────────────
    try:
        pid = os.fork()
    except OSError as exc:
        raise DaemonError(f"First fork failed: {exc}") from exc
    if pid > 0:
        # Original process exits without running atexit hooks the child
        # would also try to run.
        os._exit(0)

    # ── Become a session leader ───────────────────────────────────────
    os.setsid()

    # ── Second fork ───────────────────────────────────────────────────
    try:
        pid = os.fork()
    except OSError as exc:
        raise DaemonError(f"Second fork failed: {exc}") from exc
    if pid > 0:
        os._exit(0)

    # We are now the grandchild. We have no controlling terminal and
    # cannot acquire one (because we're not a session leader after the
    # second fork).

    # ── Working dir + umask ───────────────────────────────────────────
    try:
        os.chdir(chdir)
    except OSError:
        # Running from a directory that vanished mid-launch isn't fatal.
        pass
    os.umask(0o022)

    # ── Redirect stdio ────────────────────────────────────────────────
    sys.stdout.flush()
    sys.stderr.flush()

    fd_null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(fd_null, sys.stdin.fileno())
    os.close(fd_null)

    logfile.parent.mkdir(parents=True, exist_ok=True)
    fd_log = os.open(
        str(logfile),
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        0o600,
    )
    os.dup2(fd_log, sys.stdout.fileno())
    os.dup2(fd_log, sys.stderr.fileno())
    os.close(fd_log)

    # ── PID file ──────────────────────────────────────────────────────
    _write_pid(pidfile, os.getpid())

    # Best-effort cleanup. If we crash hard (SIGKILL, OOM) the file is
    # left behind and `status()` will detect it as stale on next start.
    def _cleanup_pidfile() -> None:
        try:
            current_pid = _read_pid(pidfile)
            if current_pid == os.getpid() and pidfile.is_file():
                pidfile.unlink()
        except OSError:
            pass

    atexit.register(_cleanup_pidfile)

    # SIGTERM should run atexit hooks (it doesn't by default — Python
    # only does that for SIGINT). Translate SIGTERM into a clean
    # SystemExit so uvicorn shuts down its lifespan handlers and our
    # PID-file cleanup runs.
    def _sigterm_handler(signum: int, frame) -> None:  # noqa: ARG001
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _sigterm_handler)


# ── Stop / restart ────────────────────────────────────────────────────

def stop_daemon(
    pidfile: Path | None = None,
    *,
    timeout: float = 10.0,
    poll_interval: float = 0.2,
) -> tuple[bool, int | None]:
    """Stop the running daemon via SIGTERM, escalating to SIGKILL on timeout.

    Returns ``(was_running, pid)``. ``was_running`` is False when there
    was no live process to stop (PID file missing or stale). A stale PID
    file is cleaned up either way.
    """
    pidfile = pidfile or DEFAULT_PID_FILE
    running, pid = status(pidfile)
    if not running:
        # Stale file — clean up so the next launch isn't confused.
        if pidfile.is_file():
            try:
                pidfile.unlink()
            except OSError:
                pass
        return False, pid

    assert pid is not None  # status returned running=True
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        # Race: died between status() and the signal. Treat as success.
        if pidfile.is_file():
            pidfile.unlink(missing_ok=True)
        return False, pid
    except PermissionError as exc:
        raise DaemonError(
            f"PID {pid} is not owned by this user; cannot signal."
        ) from exc

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(poll_interval)
        if not _process_alive(pid):
            if pidfile.is_file():
                pidfile.unlink(missing_ok=True)
            return True, pid

    # Hard escalate. Anything still alive after `timeout` s of SIGTERM
    # is either wedged or ignoring the signal.
    # signal.SIGKILL is POSIX-only; on Windows (where stop_daemon is guarded
    # by is_supported()) fall back to SIGTERM as a no-crash safety net.
    _sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
    try:
        os.kill(pid, _sigkill)
    except ProcessLookupError:
        pass
    time.sleep(0.5)
    if pidfile.is_file():
        pidfile.unlink(missing_ok=True)
    return True, pid
