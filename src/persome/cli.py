"""Persome CLI — start / stop / pause / resume / status / doctor / mcp / writer."""

from __future__ import annotations

import os

if not os.environ.get("SSL_CERT_FILE"):
    try:
        import certifi

        os.environ["SSL_CERT_FILE"] = certifi.where()
    except ImportError:
        pass

import contextlib
import fcntl
import json
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs, urljoin, urlparse

import typer
from rich.console import Console
from rich.table import Table

from . import __version__, integrity, paths, runtime_pid
from . import config as config_mod
from . import env_file as env_file_mod
from . import logger as logger_mod
from .capture.timestamps import newest_capture_path, parse_capture_timestamp
from .providers import ProviderSpec
from .store import entries as entries_mod
from .store import fts, index_md

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Local-first screen-context memory and personal modeling for macOS.",
)
console = Console()

_DAEMON_STARTUP_TIMEOUT_SECONDS = 15.0
_DAEMON_STARTUP_RETRY_SECONDS = 0.1
_DAEMON_STARTUP_HTTP_TIMEOUT_SECONDS = 1.0
_DAEMON_STARTUP_STOP_TIMEOUT_SECONDS = 5.0
_DAEMON_STARTUP_KILL_TIMEOUT_SECONDS = 2.0


def _fail_if_runtime_state_is_ambiguous() -> None:
    problem = runtime_pid.unresolved_runtime_reason()
    if problem is None:
        return
    console.print(f"[red]Unsafe Runtime lifecycle state: {problem}.[/red]")
    console.print(
        "[yellow]Stop the owning Desktop app or LaunchAgent and retry; "
        "Persome will not start a second writer.[/yellow]"
    )
    raise typer.Exit(1)


def _acquire_daemon_lock():  # type: ignore[no-untyped-def]
    handle = None
    try:
        handle = paths.open_private_lock_file(paths.daemon_lock_file())
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError, RuntimeError) as exc:
        if handle is not None:
            handle.close()
        raise RuntimeError("another Persome Runtime is already starting or running") from exc
    return handle


def _daemon_lock_is_held() -> bool:
    try:
        handle = paths.open_private_lock_file(paths.daemon_lock_file())
    except (OSError, RuntimeError):
        return True
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _init(
    *,
    starting_runtime: bool = False,
    recover_integrity: bool = True,
) -> config_mod.Config:
    paths.ensure_dirs()
    # Every initialized CLI command gets the same owner-only Runtime secrets as
    # the daemon. In particular, one-shot commands such as `model build` run
    # LLM stages in this process and must not depend on a prior `persome start`
    # having sourced the file. load_env_file deliberately preserves an
    # already-exported shell value, so per-invocation debugging overrides keep
    # precedence over the durable profile.
    env_file_mod.load_env_file(paths.env_file())
    _fail_if_runtime_state_is_ambiguous()
    # Logger first so the integrity check's JSON-line output lands in daemon.log.
    logger_mod.setup(console=False)
    # The daemon owns active SQLite writes. A status/MCP/second-start invocation
    # must never quarantine or rebuild an index while that process has it open.
    # When stopped, this remains the first database-touching operation.
    if recover_integrity and not _read_pid() and (starting_runtime or not _daemon_lock_is_held()):
        recovered = integrity.check_and_recover()
        if paths.integrity_recovery_pending().exists():
            console.print(
                "[red]Persome database recovery is incomplete.[/red] "
                f"Inspect `{paths.integrity_recovery_marker()}` and the retained "
                "quarantine files, repair the reported source problem, then rerun "
                "`persome model status` to resume recovery. Run `persome doctor` after "
                "it completes. Model builds remain disabled until then."
            )
        elif any(item.kind == "database" for item in recovered):
            console.print(
                "[yellow]Persome recovered a damaged local database. "
                "The previous model build is no longer trusted; run "
                "`persome model build` after startup.[/yellow]"
            )
    database_pending = paths.integrity_recovery_pending().exists()
    authority_pending = paths.integrity_config_recovery_pending().exists()
    if authority_pending:
        console.print(
            "[red]Persome write authority is unresolved.[/red] "
            f"Inspect `{paths.integrity_config_recovery_pending()}` and set "
            "`[evomem].write_authority` in config.toml explicitly to `markdown` or `evomem`, "
            "then rerun `persome model status`."
        )
    if starting_runtime and (database_pending or authority_pending):
        console.print(
            "[red]Runtime start is blocked until integrity recovery establishes a safe "
            "database and write authority.[/red]"
        )
        raise typer.Exit(2)
    created = config_mod.write_default_if_missing()
    if created:
        console.print(f"[green]Created default config at {paths.config_file()}[/green]")
    return config_mod.load()


def _is_pid_alive(pid: int) -> bool:
    process = runtime_pid.resolve_recorded_process()
    return bool(process is not None and process.pid == pid)


def _read_pid() -> int | None:
    process = runtime_pid.resolve_recorded_process()
    return process.pid if process is not None else None


def _daemon_uptime() -> str:
    """Return a human-readable uptime string for the running daemon.

    Reads the PID file's mtime as a proxy for daemon start time (the
    daemon overwrites it on each launch). Returns ``"stopped"`` when
    the daemon is not running.
    """
    pid = _read_pid()
    if not pid:
        return "stopped"
    try:
        mtime = paths.pid_file().stat().st_mtime
        now = datetime.now().astimezone()
        delta = now - datetime.fromtimestamp(mtime).astimezone()
        h, r = divmod(int(delta.total_seconds()), 3600)
        m = r // 60
        if h >= 24:
            return f"{h // 24}d {h % 24}h"
        if h:
            return f"{h}h {m}m"
        return f"{m}m"
    except OSError:
        return "unknown"


def _capture_continuity(hours: float = 1.0) -> tuple[int, float | None]:
    """Return (count, max_gap_seconds) for captures in the last ``hours``.

    Uses the ``captures`` table which is written synchronously on every capture,
    so the result reflects the current state with no processing lag.
    Returns ``(0, None)`` when there are no captures in the window.
    """
    with fts.cursor() as conn:
        # New captures are canonical UTC, but upgrades can retain aware-local
        # rows. SQLite's date conversion compares the underlying instant.
        cutoff = (datetime.now(UTC) - timedelta(minutes=int(hours * 60))).isoformat(
            timespec="microseconds"
        )
        rows = conn.execute(
            "SELECT timestamp FROM captures "
            "WHERE persome_epoch(timestamp) >= persome_epoch(?) "
            "ORDER BY persome_epoch(timestamp)",
            (cutoff,),
        ).fetchall()
    if not rows:
        return 0, None
    timestamps = [parsed for r in rows if (parsed := parse_capture_timestamp(r[0])) is not None]
    if not timestamps:
        return 0, None
    if len(timestamps) < 2:
        return len(timestamps), None
    gaps = [(timestamps[i + 1] - timestamps[i]).total_seconds() for i in range(len(timestamps) - 1)]
    return len(timestamps), max(gaps)


def _install_source() -> str:
    """Return the editable-install source path recorded in direct_url.json."""
    import importlib.metadata

    for distribution_name in ("personal-model", "persome-core"):
        try:
            dist = importlib.metadata.distribution(distribution_name)
            raw = dist.read_text("direct_url.json")
            if raw:
                data = json.loads(raw)
                url = str(data.get("url", ""))
                if url.startswith("file://"):
                    return url[7:]
                return url
        except Exception:  # noqa: BLE001
            continue
    return "unknown"


def _last_capture_info() -> tuple[str | None, str | None]:
    """Return ``(timestamp, app_name)`` of the most recent capture buffer file.

    Returns ``(None, None)`` when the buffer directory is empty or missing.
    """
    buf = paths.capture_buffer_dir()
    if not buf.exists():
        return None, None
    latest = newest_capture_path(p for p in buf.iterdir() if p.suffix == ".json")
    if latest is None:
        return None, None
    try:
        data = json.loads(latest.read_bytes())
        ts = data.get("timestamp")
        meta = data.get("window_meta") or {}
        app = meta.get("app_name")
        return ts, app
    except (OSError, ValueError):
        return latest.stem, None


def _health_status(pid: int | None, last_ts: str | None) -> tuple[str, str]:
    """Return ``(label, style)`` for daemon health.

    ``style`` is a Rich-style string suitable for ``console.print``.
    """
    if not pid:
        return "stopped", "red"
    if not last_ts:
        return "running (no captures yet)", "yellow"
    try:
        last = datetime.fromisoformat(last_ts)
        age = (datetime.now(last.tzinfo) - last).total_seconds()
    except (ValueError, TypeError):
        return "running", "green"
    if age < 300:  # 5 minutes
        return "healthy", "green"
    return "stale (no captures in >5m)", "yellow"


# ─── commands ─────────────────────────────────────────────────────────────


def _watch_parent_death() -> None:
    """Exit the daemon if the app that spawned it dies (Stage 2 child-process lifetime).

    The Persome app runs the daemon as a child Process and terminates it on clean quit — but a
    force-quit / crash of the app sends no signal, and the child would be reparented (to
    launchd/init) and keep capturing. When the app spawns us it passes ``PERSOME_PARENT_PID``; this
    tiny watcher polls ``getppid()`` and force-exits once we're reparented (ppid no longer matches),
    so the daemon truly "dies with the app". No-op when ``PERSOME_PARENT_PID`` is unset (e.g. a plain
    ``persome start`` from a terminal, where the daemon is meant to outlive the shell).
    """
    raw = os.environ.get("PERSOME_PARENT_PID")
    if not raw:
        return
    try:
        parent = int(raw)
    except ValueError:
        return

    def _watch() -> None:
        import time

        while True:
            time.sleep(3)
            if os.getppid() != parent:
                # Parent (the Persome app) is gone. On a CLEAN quit it already SIGTERM'd us and the
                # graceful shutdown (uvicorn → daemon finally: force-end the active session, unlink
                # .pid) is in flight; on a CRASH no signal was sent. Either way trigger that SAME
                # graceful path — NOT an abrupt os._exit, which would strand the session as `active`
                # and leak the pidfile (#codex P2) — then hard-exit only as a backstop if shutdown
                # overruns.
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(15)
                os._exit(0)

    threading.Thread(target=_watch, name="parent-death-watch", daemon=True).start()


def _same_runtime_process(
    left: runtime_pid.ProcessIdentity,
    right: runtime_pid.ProcessIdentity,
) -> bool:
    """Compare the generation-bound identity fields used during startup."""
    return (
        left.pid == right.pid
        and left.generation == right.generation
        and left.runtime_started_at == right.runtime_started_at
    )


def _probe_background_runtime(
    cfg: config_mod.Config,
    expected_process: runtime_pid.ProcessIdentity | None = None,
) -> tuple[str, str, runtime_pid.ProcessIdentity | None]:
    """Probe one background-start attempt.

    The returned state is ``ready``, ``retry``, or ``fatal``. An HTTP-enabled
    Runtime must answer its authenticated status route with this root, version,
    and generation-bound PID. A random service already listening on the port
    therefore cannot make ``persome start`` report a false success.
    """
    process = runtime_pid.resolve_recorded_process()
    if process is None:
        if expected_process is not None:
            return "fatal", "the new Runtime process exited during startup", expected_process
        return "retry", "waiting for the new Runtime process receipt", None
    if process.generation is None or process.runtime_started_at is None:
        return "retry", "waiting for the generation-bound Runtime receipt", process
    if expected_process is not None and not _same_runtime_process(process, expected_process):
        return "fatal", "the Runtime process identity changed during startup", expected_process

    http_enabled = bool(cfg.mcp.auto_start and cfg.mcp.transport in {"sse", "streamable-http"})
    if not http_enabled:
        return "ready", "the Runtime process receipt is valid", process

    import httpx

    from .security.auth import auth_headers, loopback_http_url

    try:
        url = loopback_http_url(cfg.mcp.host, cfg.mcp.port, "/status")
        response = httpx.get(
            url,
            headers=auth_headers(),
            timeout=_DAEMON_STARTUP_HTTP_TIMEOUT_SECONDS,
            trust_env=False,
        )
    except (RuntimeError, ValueError) as exc:
        return "fatal", f"local HTTP authentication is not configured: {exc}", process
    except httpx.HTTPError as exc:
        return "retry", f"waiting for the local HTTP Runtime ({type(exc).__name__})", process

    if response.status_code >= 500:
        return (
            "retry",
            f"waiting for the local HTTP Runtime (HTTP {response.status_code})",
            process,
        )
    if response.status_code != 200:
        return (
            "fatal",
            f"port {cfg.mcp.port} answered HTTP {response.status_code}, not the expected "
            "authenticated Persome Runtime",
            process,
        )
    try:
        payload = response.json()
    except (UnicodeError, ValueError):
        return "fatal", f"port {cfg.mcp.port} returned a non-Persome response", process
    data = payload.get("data") if isinstance(payload, dict) else None
    if not (
        isinstance(payload, dict)
        and payload.get("success") is True
        and isinstance(data, dict)
        and data.get("version") == __version__
        and data.get("root") == str(paths.root())
    ):
        return (
            "fatal",
            f"port {cfg.mcp.port} is in use by a different or incompatible service",
            process,
        )
    if data.get("daemon") != f"running pid {process.pid}":
        return (
            "retry",
            f"waiting for port {cfg.mcp.port} to serve the new Runtime generation",
            process,
        )
    return "ready", "the authenticated local HTTP Runtime is ready", process


def _wait_for_background_start(
    cfg: config_mod.Config,
    *,
    timeout_seconds: float = _DAEMON_STARTUP_TIMEOUT_SECONDS,
) -> tuple[bool, str, runtime_pid.ProcessIdentity | None]:
    """Wait a bounded time for one exact daemon generation to become usable."""
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    expected_process: runtime_pid.ProcessIdentity | None = None
    last_process: runtime_pid.ProcessIdentity | None = None
    detail = "the Runtime did not publish a readiness receipt"
    while True:
        state, detail, process = _probe_background_runtime(cfg, expected_process)
        if process is not None:
            last_process = process
        if (
            expected_process is None
            and process is not None
            and process.generation is not None
            and process.runtime_started_at is not None
        ):
            expected_process = process
        if state == "ready":
            return True, detail, expected_process or last_process
        if state == "fatal":
            return False, detail, expected_process or last_process
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, f"startup timed out: {detail}", expected_process or last_process
        time.sleep(min(_DAEMON_STARTUP_RETRY_SECONDS, remaining))


def _terminate_failed_background_start(
    process: runtime_pid.ProcessIdentity | None,
) -> bool:
    """Stop only the generation observed during this failed start attempt."""
    if process is None:
        return not _daemon_lock_is_held()
    if not runtime_pid.signal_process(process, signal.SIGTERM):
        return runtime_pid.resolve_recorded_process() is None and _clear_failed_background_receipts(
            process
        )
    if runtime_pid.wait_for_exit(process, _DAEMON_STARTUP_STOP_TIMEOUT_SECONDS):
        # SIGTERM can land before the daemon installs its cleanup handler. In
        # that startup window the process exits but leaves its generation-bound
        # PID/state receipts behind. Clear only receipts that still name the
        # exact observed generation; a normal graceful shutdown has already
        # removed them, so this is an idempotent no-op there.
        return _clear_failed_background_receipts(process)
    if not runtime_pid.signal_process(process, signal.SIGKILL):
        return runtime_pid.resolve_recorded_process() is None and _clear_failed_background_receipts(
            process
        )
    if not runtime_pid.wait_for_exit(process, _DAEMON_STARTUP_KILL_TIMEOUT_SECONDS):
        return False
    return _clear_failed_background_receipts(process)


def _clear_failed_background_receipts(process: runtime_pid.ProcessIdentity) -> bool:
    """Remove stale PID/state files only when they still name ``process``.

    A normal SIGTERM shutdown removes both itself. This is the SIGKILL fallback:
    validate every available receipt before unlinking so a concurrently started
    generation can never lose its lifecycle identity.
    """
    if runtime_pid.same_process_is_running(process):
        return False
    state = runtime_pid.read_runtime_generation()
    if state is not None and not (
        process.generation is not None
        and state.pid == process.pid
        and state.generation == process.generation
        and state.started_at == process.runtime_started_at
    ):
        return False
    record = runtime_pid.read_pid_file()
    if record is not None and not (
        record.pid == process.pid
        and (record.generation is None or record.generation == process.generation)
    ):
        return False
    for receipt, parsed in (
        (paths.pid_file(), record),
        (paths.runtime_state_file(), state),
    ):
        if parsed is None:
            try:
                receipt.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                return False
            # An unreadable or malformed receipt cannot be safely attributed.
            return False
    for receipt in (paths.pid_file(), paths.runtime_state_file()):
        with contextlib.suppress(FileNotFoundError):
            receipt.unlink()
    return not paths.pid_file().exists() and not paths.runtime_state_file().exists()


@app.command()
def start(
    foreground: bool = typer.Option(False, "--foreground", "-f", help="Run in this terminal."),
    capture_only: bool = typer.Option(False, "--capture-only", help="Skip the writer loop."),
) -> None:
    """Start the Persome daemon."""
    # Avoid even configuration/integrity writes when the caller only asked to
    # start an already-running daemon. This is also the cross-process guard for
    # editor-launched MCP clients that share the same local database.
    _fail_if_runtime_state_is_ambiguous()
    if pid := _read_pid():
        console.print(f"[yellow]Already running (pid {pid})[/yellow]")
        raise typer.Exit(1)
    try:
        daemon_lock = _acquire_daemon_lock()
    except RuntimeError as exc:
        console.print(f"[yellow]{exc}.[/yellow]")
        raise typer.Exit(1) from exc
    _fail_if_runtime_state_is_ambiguous()
    # Source checkouts and upgrades may start without re-running install.sh.
    # Provision/repair the local HTTP boundary before generalized env loading,
    # so a malformed value in the file cannot masquerade as a shell override.
    # The helper is idempotent and preserves every unrelated provider secret.
    env_file_mod.ensure_local_api_token(paths.env_file())
    cfg = _init(starting_runtime=True)
    if pid := _read_pid():
        console.print(f"[yellow]Already running (pid {pid})[/yellow]")
        raise typer.Exit(1)

    from . import daemon

    if foreground:
        console.print("[bold]Persome starting in foreground[/bold] — Ctrl+C to stop.")
        _watch_parent_death()  # exit if the Persome app that spawned us (--foreground child) dies
        daemon.run(cfg, capture_only=capture_only)
        daemon_lock.close()
        # Hard-exit instead of returning — `daemon.run` has already done the clean
        # shutdown (cancelled tasks, force-ended the session, logged "daemon
        # stopped"), so all durable state is committed (SQLite WAL is crash-safe;
        # the next boot immediately recovers any open session). Falling through to normal
        # interpreter exit instead runs CPython finalization, which tears down the
        # OpenSSL state that a background LLM/embedding worker (`run_in_executor`)
        # may still be blocked inside (`_ssl__SSLSocket_read`) — freeing it under
        # that thread SIGSEGVs on EVERY shutdown (the daemon runs `start
        # --foreground` under launchd, so this is the bootout/app-quit path; ~10
        # SIGSEGV crash reports/day under app-relaunch churn). The background path
        # below already hard-exits for the same reason; mirror it here.
        os._exit(0)

    # Background: double-fork. The parent stays alive just long enough to prove
    # that this exact new generation owns its PID receipt and authenticated HTTP
    # endpoint. Its copy of the lifetime lock is closed before polling; the
    # grandchild retains the inherited file description for its whole life.
    first_child_pid = os.fork()
    if first_child_pid != 0:
        daemon_lock.close()
        try:
            ready, detail, process = _wait_for_background_start(cfg)
        except Exception as exc:  # noqa: BLE001 - startup still needs safe cleanup
            ready = False
            detail = f"startup verification failed ({type(exc).__name__})"
            process = None
            with contextlib.suppress(Exception):
                process = runtime_pid.resolve_recorded_process()
        if ready:
            console.print("[green]Persome started in background.[/green]")
            console.print(f"Logs: {paths.logs_dir()}")
            return
        try:
            cleaned = _terminate_failed_background_start(process)
        except Exception:  # noqa: BLE001 - preserve the actionable failure path
            cleaned = False
        console.print(f"[red]Persome did not start correctly:[/red] {detail}")
        if cleaned:
            console.print("[yellow]The incomplete background Runtime was stopped.[/yellow]")
        else:
            console.print(
                "[yellow]Persome could not confirm that the incomplete Runtime stopped; "
                "run `persome stop` before retrying.[/yellow]"
            )
        if cfg.mcp.auto_start and cfg.mcp.transport in {"sse", "streamable-http"}:
            console.print(
                f"Check port {cfg.mcp.port} with "
                f"`lsof -nP -iTCP:{cfg.mcp.port} -sTCP:LISTEN`, then retry."
            )
        console.print(f"Logs: {paths.logs_dir()}")
        console.print("Next: persome doctor")
        raise typer.Exit(1)
    os.setsid()
    if os.fork() != 0:
        os._exit(0)
    # Redirect stdio to /dev/null. After dup2 the original fd is no longer
    # needed; closing it avoids leaking one descriptor per daemon start.
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    if devnull > 2:
        os.close(devnull)
    daemon.run(cfg, capture_only=capture_only)
    daemon_lock.close()
    os._exit(0)


@app.command()
def stop(timeout: int = typer.Option(10, help="Seconds to wait for the daemon to exit.")) -> None:
    """Stop the daemon and wait for it to fully exit."""
    _init()
    process = runtime_pid.resolve_recorded_process()
    if process is None:
        console.print("[yellow]Daemon not running.[/yellow]")
        raise typer.Exit(1)
    if not runtime_pid.signal_process(process, signal.SIGTERM):
        console.print("[red]Daemon identity changed before it could be stopped.[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Sent SIGTERM to pid {process.pid}.[/green]")
    if runtime_pid.wait_for_exit(process, timeout):
        console.print("[green]Daemon stopped.[/green]")
        return
    console.print(
        f"[yellow]Daemon (pid {process.pid}) did not exit within {timeout}s — it may still be running.[/yellow]"
    )


@app.command()
def pause() -> None:
    """Pause capture (daemon stays up but skips captures)."""
    paths.ensure_dirs()
    paths.atomic_write_private_text(paths.paused_flag(), datetime.now().isoformat())
    console.print("[yellow]Capture paused.[/yellow]")


@app.command()
def resume() -> None:
    """Resume capture."""
    with contextlib.suppress(FileNotFoundError):
        paths.paused_flag().unlink()
    console.print("[green]Capture resumed.[/green]")


@app.command()
def status() -> None:
    """Show daemon status + memory stats."""
    cfg = _init()
    # Source the env file before resolving/probing the selected provider.
    env_file_mod.load_env_file(paths.env_file())
    from . import launchagent
    from . import onboarding as onboarding_mod
    from .capture.ocr_health import inspect as inspect_ocr
    from .providers import resolve_profile

    default_profile = resolve_profile(cfg.model_for("default"))
    ocr_health = inspect_ocr(cfg.capture)
    pid = _read_pid()
    paused = paths.paused_flag().exists()

    runtime_state = onboarding_mod._runtime_state(pid) if pid is not None else None
    permission_state = runtime_state.get("permissions") if runtime_state is not None else None
    accessibility = (
        str(permission_state.get("accessibility", "unknown"))
        if isinstance(permission_state, dict)
        else "not_applicable"
        if cfg.capture.source == "ingest"
        else "unknown"
    )
    runtime_generation = (
        str(runtime_state.get("generation", "unknown")) if runtime_state is not None else "unknown"
    )
    owner = "background"
    if launchagent.is_loaded():
        binary = launchagent.configured_runtime_binary()
        owner = (
            "launchagent"
            if binary is not None and launchagent.owns_recorded_runtime(binary)
            else "launchagent (ownership mismatch)"
        )

    uptime = _daemon_uptime()
    last_ts, last_app = _last_capture_info()
    health_label, health_style = _health_status(pid, last_ts)

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("Version", __version__)
    table.add_row("Root", str(paths.root()))
    table.add_row("Daemon", f"[green]running pid {pid}[/green]" if pid else "[red]stopped[/red]")
    table.add_row("Uptime", uptime)
    table.add_row("Health", f"[{health_style}]{health_label}[/{health_style}]")
    table.add_row("Capture", "[yellow]paused[/yellow]" if paused else "active")
    table.add_row("Capture Source", cfg.capture.source)
    table.add_row("Runtime Owner", owner if pid is not None else "stopped")
    table.add_row("Generation", runtime_generation)
    accessibility_style = (
        "green"
        if accessibility in {"granted", "not_applicable"}
        else "red"
        if accessibility == "denied"
        else "yellow"
    )
    table.add_row(
        "Accessibility",
        f"[{accessibility_style}]{accessibility}[/{accessibility_style}]",
    )
    ocr_style = "green" if ocr_health.ready else "yellow" if not ocr_health.enabled else "red"
    table.add_row(
        "OCR",
        f"[{ocr_style}]{ocr_health.state}[/{ocr_style}] ({ocr_health.tier})",
    )
    permission_style = (
        "green"
        if ocr_health.screen_recording == "granted"
        else "yellow"
        if ocr_health.screen_recording == "not_applicable"
        else "red"
    )
    table.add_row(
        "Screen Recording",
        f"[{permission_style}]{ocr_health.screen_recording}[/{permission_style}]",
    )

    if last_ts:
        try:
            last_dt = datetime.fromisoformat(last_ts)
            age = (datetime.now(last_dt.tzinfo) - last_dt).total_seconds()
            if age < 60:
                ago = "just now"
            elif age < 3600:
                ago = f"{int(age // 60)}m ago"
            else:
                ago = f"{int(age // 3600)}h ago"
            table.add_row("Last Capture", f"{ago} ({last_app})" if last_app else ago)
        except (ValueError, TypeError):
            table.add_row("Last Capture", last_ts)
    else:
        table.add_row("Last Capture", "(none)")

    cap_count, max_gap = _capture_continuity(hours=1.0)
    if cap_count == 0:
        table.add_row("Captures (1h)", "[yellow]0[/yellow]")
    elif max_gap is None:
        table.add_row("Captures (1h)", f"{cap_count}")
    else:
        gap_m = max_gap / 60
        if gap_m < 5:
            gap_str = f"[green]max gap {gap_m:.1f}m[/green]"
        elif gap_m < 15:
            gap_str = f"[yellow]max gap {gap_m:.1f}m[/yellow]"
        else:
            gap_str = f"[red]max gap {gap_m:.1f}m[/red]"
        table.add_row("Captures (1h)", f"{cap_count}  {gap_str}")

    table.add_row("Install", _install_source())
    credential = "ready" if default_profile.credential_ready else "credential missing"
    table.add_row(
        "LLM",
        f"{default_profile.provider_label} / {default_profile.protocol} / "
        f"{default_profile.model} ({credential})",
    )

    buf = paths.capture_buffer_dir()
    if buf.exists():
        bufs = [p for p in buf.iterdir() if p.suffix == ".json"]
        latest = newest_capture_path(bufs)
        last = latest.name if latest else "(none)"
        table.add_row("Buffer", f"{len(bufs)} files, last: {last}")

    with fts.cursor() as conn:
        sess_row = conn.execute(
            "SELECT COUNT(*), SUM(status='reduced'), SUM(status='ended'), SUM(status='failed')"
            " FROM sessions"
        ).fetchone()
        if sess_row and sess_row[0]:
            total, reduced, ended, failed = sess_row
            table.add_row(
                "Sessions",
                f"{total} total ({reduced or 0} reduced, {ended or 0} ended, {failed or 0} failed)",
            )
        else:
            table.add_row("Sessions", "(none)")
        active = fts.list_files(conn, include_dormant=False)
        dormant = [f for f in fts.list_files(conn, include_dormant=True) if f.status == "dormant"]
        total_entries = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        table.add_row(
            "Memory",
            f"{len(active)} active files, {len(dormant)} dormant, {total_entries} entries",
        )
        tlb_row = conn.execute(
            "SELECT COUNT(*), "
            "(SELECT end_time FROM timeline_blocks "
            "ORDER BY persome_epoch(end_time) DESC LIMIT 1) "
            "FROM timeline_blocks"
        ).fetchone()
        tlb_count = tlb_row[0] if tlb_row else 0
        tlb_last = tlb_row[1] if tlb_row and tlb_row[1] else "(none)"
        table.add_row("Timeline", f"{tlb_count} blocks, last end: {tlb_last}")

    stages = ("timeline", "reducer", "classifier", "compact")
    ping_results = _ping_stages(cfg, stages)
    for stage in stages:
        m = cfg.model_for(stage)
        profile = resolve_profile(m)
        ping = _format_ping(ping_results.get(stage))
        table.add_row(
            f"Model ({stage})",
            f"{profile.provider}/{profile.model} ({profile.protocol})   {ping}",
        )

    console.print(table)


@app.command()
def doctor() -> None:
    """Self-check a bring-your-own-provider install (offline; zero LLM calls).

    Prints one ✓/✗/⚠ line per prerequisite — env file present + private (0600),
    local API bearer token present, selected provider credential configured,
    endpoint reachable (HEAD, warn-only), Swift capture helpers compiled, macOS
    Accessibility and Screen Recording trust, local OCR readiness, data root
    writable, daemon port available. Exits 1 if any check FAILS; warnings never
    fail.
    """
    from . import doctor as doctor_mod

    # No _init(): doctor must stay read-only (no config write, no integrity
    # recovery, no DB open) so it is safe to run on a broken install.
    cfg = config_mod.load()
    checks = doctor_mod.run_checks(cfg.mcp.host, cfg.mcp.port)
    marks = {"ok": "[green]✓[/green]", "fail": "[red]✗[/red]", "warn": "[yellow]⚠[/yellow]"}
    for c in checks:
        detail = f"  [dim]{c.detail}[/dim]" if c.detail else ""
        console.print(f"{marks[c.status]} {c.name}{detail}")
    if doctor_mod.has_failure(checks):
        raise typer.Exit(code=1)


@app.command()
def onboard(
    tier: str | None = typer.Option(
        None,
        "--tier",
        help="Enable/change OCR tier (tiny | small | medium); omitted preserves prior intent.",
    ),
    gui: bool = typer.Option(
        True,
        "--gui/--no-gui",
        help="Use native macOS dialogs (falls back to terminal prompts).",
    ),
    preserve_policy: bool = typer.Option(
        False,
        "--preserve-policy",
        hidden=True,
        help="Verify the existing capture policy without changing OCR configuration.",
    ),
    expected_owner: str = typer.Option(
        "any",
        "--expect-owner",
        hidden=True,
        help="Require final Runtime ownership (any | launchagent | background).",
    ),
) -> None:
    """Verify capture permissions/policy, Runtime ownership, and live readiness."""
    from . import onboarding as onboarding_mod

    _init()
    env_file_mod.load_env_file(paths.env_file())
    try:
        proof = onboarding_mod.onboard(
            tier=tier,
            gui=gui,
            preserve_policy=preserve_policy,
            expected_owner=expected_owner,
        )
    except onboarding_mod.OnboardingCancelled as exc:
        console.print(f"[yellow]Onboarding stopped: {exc}.[/yellow]")
        raise typer.Exit(1) from exc
    except onboarding_mod.OnboardingError as exc:
        console.print(f"[red]Onboarding failed: {exc}.[/red]")
        raise typer.Exit(1) from exc

    if proof.mode == "ingest":
        console.print("[green]✓ Trusted ingest capture mode ready[/green]")
    else:
        console.print("[green]✓ Accessibility granted[/green]")
        if proof.screen_recording == "granted":
            console.print("[green]✓ Screen Recording granted[/green]")
        if proof.ocr == "ready":
            console.print("[green]✓ Isolated local OCR worker ready[/green]")
        else:
            ocr_message = {
                "disabled": "disabled by saved policy",
                "disabled_by_environment": "disabled by PERSOME_DISABLE_OCR",
                "runtime_unavailable": "unavailable on this Mac",
                "models_missing": "missing its bundled model files",
            }.get(proof.ocr, proof.ocr)
            console.print(f"[yellow]• AX capture ready; local OCR is {ocr_message}[/yellow]")
    if proof.health == "ok":
        console.print(
            f"[green]✓ Persome running and healthy[/green] (pid {proof.pid}, owner {proof.owner})"
        )
    else:
        console.print(
            f"[yellow]✓ Persome running with {proof.health} optional features[/yellow] "
            f"(pid {proof.pid}, owner {proof.owner})"
        )
    if proof.capture_path is not None:
        console.print(f"[green]✓ Fresh capture verified[/green] ({proof.capture_path})")
    elif proof.receipt == "ingest-ready":
        console.print("[green]✓ Authenticated ingest runner verified[/green]")
    else:
        state = proof.receipt.removeprefix("privacy-")
        console.print(f"[green]✓ Capture privacy state preserved[/green] ({state})")


@app.command()
def update(
    source: Annotated[
        Path | None,
        typer.Option("--source", help="Use a local checkout instead of official main."),
    ] = None,
) -> None:
    """Update the installed Persome Runtime and prove it is healthy."""
    from . import updater

    if updater.is_external_package_install():
        console.print(
            "[yellow]This Persome CLI is managed by a Python package manager.[/yellow]\n"
            "Upgrade the public distribution, then re-run Runtime proof:\n\n"
            "  [bold]uv tool upgrade personal-model[/bold]\n"
            "  [bold]persome onboard[/bold]\n\n"
            "For pipx or pip installations, upgrade [bold]personal-model[/bold] with that "
            "manager instead."
        )
        raise typer.Exit(1)

    updater.claim_legacy_foreground()
    launchagent_was_loaded = False
    runtime_may_have_changed = False

    def fail_update(exc: BaseException, recovery_error: str = "") -> None:
        cancelled = isinstance(
            exc,
            (KeyboardInterrupt, updater.UpdateCancelled, updater.UpdateSignal),
        )
        if cancelled:
            if recovery_error:
                console.print(
                    f"[red]Update cancelled, but Runtime recovery failed: {recovery_error}[/red]"
                )
            else:
                message = (
                    "Update cancelled; the previous Runtime was restored."
                    if runtime_may_have_changed
                    else "Update cancelled before the Runtime was changed."
                )
                console.print(f"[yellow]{message}[/yellow]")
            if isinstance(exc, (updater.UpdateCancelled, updater.UpdateSignal)):
                code = exc.exit_code
            else:
                code = 130
            raise typer.Exit(code) from exc
        suffix = f" Recovery also failed: {recovery_error}" if recovery_error else ""
        console.print(f"[red]Update failed: {exc}.{suffix}[/red]")
        raise typer.Exit(1) from exc

    try:
        with updater.catch_update_signals(), updater.update_lock():
            # Recovery may already be moving virtualenv directories and
            # lifecycle ownership. Repeated terminal signals must not leave
            # that deterministic repair half-applied.
            console.print("[dim]Checking for an interrupted update to recover...[/dim]")
            with updater.ignore_update_signals():
                updater.recover_pending_update()
            updater.ensure_no_pending_update()
            if source is None:
                console.print("[dim]Downloading the latest official main revision...[/dim]")
            else:
                console.print(f"[dim]Inspecting local update source {source}...[/dim]")
            with updater.acquire_source(source) as update_source:
                try:
                    label = "official main" if update_source.official else str(update_source.path)
                    console.print(
                        f"[bold]Updating Persome from {label}[/bold] "
                        f"([dim]{update_source.revision[:12]}[/dim])"
                    )
                    launchagent_was_loaded = updater.launchagent_should_be_restored()
                    updater.begin_update_transaction(launchagent_was_loaded)
                    runtime_may_have_changed = True
                    updater.stop_runtime(launchagent_was_loaded=launchagent_was_loaded)
                    console.print("[green]✓ Previous Runtime stopped[/green]")
                    updater.run_installer(update_source)
                    updater.mark_update_phase(launchagent_was_loaded, "prepared")
                    updater.activate_runtime(launchagent_was_loaded)
                    updater.prove_runtime(launchagent_was_loaded)
                    with updater.ignore_update_signals():
                        updater.mark_update_phase(launchagent_was_loaded, "committing")
                        cleanup = updater.commit_prepared_install()
                        updater.clear_update_state()
                        updater.cleanup_committed_install(cleanup)
                    runtime_may_have_changed = False
                except (
                    KeyboardInterrupt,
                    updater.UpdateError,
                    updater.UpdateSignal,
                ) as exc:
                    recovery_error = ""
                    if runtime_may_have_changed:
                        try:
                            with updater.ignore_update_signals():
                                updater.rollback_and_recover(launchagent_was_loaded)
                        except updater.UpdateError as recovery_exc:
                            recovery_error = str(recovery_exc)
                    fail_update(exc, recovery_error)
    except (KeyboardInterrupt, updater.UpdateError, updater.UpdateSignal) as exc:
        fail_update(exc)

    console.print(
        "[green]✓ Persome update complete[/green] — configuration, credentials, and personal "
        "data were preserved."
    )


def _ping_stages(cfg: config_mod.Config, stages: tuple[str, ...]) -> dict:
    """Probe each stage's configured model, deduping identical configs.

    Returns a dict keyed by stage name -> PingResult. Pings run in parallel
    so a single hung provider can't stretch the wait past the per-call
    timeout.
    """
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import replace

    from .providers import resolve_profile
    from .writer.llm import PingResult, ping_stage

    # Dedup by effective profile — common case is
    # one model for all four stages, which should hit the network once.
    dedup: dict[tuple[str, str, str, str], list[str]] = {}
    for stage in stages:
        m = cfg.model_for(stage)
        profile = resolve_profile(m)
        key = (profile.protocol, profile.model, profile.base_url, profile.api_key or "")
        dedup.setdefault(key, []).append(stage)

    results: dict = {}
    if not dedup:
        return results
    with ThreadPoolExecutor(max_workers=min(4, len(dedup))) as pool:
        future_to_stages = {
            pool.submit(ping_stage, cfg, members[0]): members for members in dedup.values()
        }
        for future, members in future_to_stages.items():
            try:
                res = future.result(timeout=12.0)
            except Exception as exc:  # noqa: BLE001
                err_label = type(exc).__name__
                for stage in members:
                    m = cfg.model_for(stage)
                    results[stage] = PingResult(
                        stage=stage,
                        model=m.model,
                        ok=False,
                        latency_ms=None,
                        error=err_label,
                    )
                continue
            for stage in members:
                # Reuse the same PingResult across stages that share a config,
                # but tag each with its own stage name so callers can map back.
                results[stage] = replace(res, stage=stage)
    return results


def _format_ping(res) -> str:  # type: ignore[no-untyped-def]
    """Render a PingResult as a short Rich-styled cell."""
    if res is None:
        return "[dim]?[/dim]"
    if res.mocked:
        return "[dim]✓ mocked[/dim]"
    if res.ok:
        latency = f"{res.latency_ms} ms" if res.latency_ms is not None else "ok"
        return f"[green]✓[/green] {latency}"
    err = res.error or "failed"
    return f"[red]✗[/red] {err}"


@app.command()
def mcp() -> None:
    """Run the MCP server (stdio). For LLM client config."""
    # Per-client MCP startup must never run database recovery. Besides adding a
    # full SQLite scan to every handshake, an older daemon may not publish the
    # current PID/lock receipts and could be writing while a newer stdio client
    # decides the database is offline. Daemon startup and explicit maintenance
    # commands retain the recovery path.
    # Stdio stdout is the JSON-RPC transport. Keep first-run config notices and
    # lifecycle diagnostics visible on stderr without corrupting the protocol.
    with contextlib.redirect_stdout(sys.stderr):
        _init(recover_integrity=False)
    from .mcp import server as mcp_server

    mcp_server.run_stdio()


ocr_app = typer.Typer(help="Configure and inspect on-device OCR for AX-poor apps.")
app.add_typer(ocr_app, name="ocr")


def _open_screen_recording_settings() -> None:
    from .onboarding import open_screen_recording_settings

    open_screen_recording_settings()


@ocr_app.command("setup")
def ocr_setup(
    tier: str = typer.Option("tiny", "--tier", help="OCR tier: tiny | small | medium."),
    open_settings: bool = typer.Option(
        True,
        "--open-settings/--no-open-settings",
        help="Open Screen Recording settings when permission is not granted.",
    ),
) -> None:
    """Enable local OCR, request Screen Recording, and verify the worker."""
    from .capture import ocr_local, screen_recording
    from .ocr_setup import VALID_TIERS, save_ocr_config

    _init()
    env_file_mod.load_env_file(paths.env_file())
    if tier not in VALID_TIERS:
        console.print(f"[red]Unsupported OCR tier {tier!r}: choose {', '.join(VALID_TIERS)}.[/red]")
        raise typer.Exit(2)
    if ocr_local.disabled_by_environment():
        console.print(
            "[red]OCR is disabled by PERSOME_DISABLE_OCR. Remove that variable and retry.[/red]"
        )
        raise typer.Exit(1)
    if not ocr_local.runtime_available():
        console.print(
            "[red]The local Paddle OCR runtime is unavailable on this architecture.[/red] "
            "AX capture remains available."
        )
        raise typer.Exit(1)
    if not ocr_local.models_available(tier):
        console.print(f"[red]Bundled PP-OCRv6 {tier} model weights are missing.[/red]")
        raise typer.Exit(1)

    console.print("Requesting macOS Screen Recording permission for local OCR...")
    screen_recording.request_screen_recording()
    with console.status("Starting the isolated local OCR worker..."):
        engine_ready = ocr_local.warm(tier)
    if not engine_ready:
        console.print("[red]The local OCR worker could not initialize. Nothing was enabled.[/red]")
        raise typer.Exit(1)

    save_ocr_config(enabled=True, tier=tier, config_path=paths.config_file())
    permission_ready = screen_recording.has_screen_recording()
    if not permission_ready:
        if open_settings:
            _open_screen_recording_settings()
        console.print(
            "[yellow]OCR is enabled, but Screen Recording is not granted yet.[/yellow]\n"
            "Enable the terminal or Persome runtime entry shown in System Settings -> "
            "Privacy & Security -> Screen Recording, then restart the daemon."
        )
        raise typer.Exit(1)

    console.print(f"[green]✓ Local OCR enabled and ready[/green] ({tier}, isolated worker)")
    if _read_pid() is not None:
        console.print("Restart the daemon to load the updated OCR configuration.")


@ocr_app.command("status")
def ocr_status(
    check: bool = typer.Option(False, "--check", help="Start the worker and verify its engine."),
) -> None:
    """Show OCR configuration, runtime, models, and permission state."""
    from .capture import ocr_health, ocr_local

    cfg = _init()
    env_file_mod.load_env_file(paths.env_file())
    health = ocr_health.inspect(cfg.capture)
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("State", health.state)
    table.add_row("Enabled", "yes" if health.enabled else "no")
    table.add_row("Tier", health.tier)
    table.add_row("Runtime", "available" if health.runtime_available else "unavailable")
    table.add_row("Models", "available" if health.models_available else "missing")
    table.add_row("Screen Recording", health.screen_recording)
    table.add_row("Detail", health.detail)
    console.print(table)
    if check:
        if not health.enabled or health.disabled_by_environment:
            raise typer.Exit(1)
        with console.status("Checking the isolated local OCR worker..."):
            ready = ocr_local.warm(health.tier)
        if not ready:
            console.print("[red]✗ OCR worker check failed[/red]")
            raise typer.Exit(1)
        console.print("[green]✓ OCR worker is ready[/green]")
    if health.enabled and not health.ready:
        raise typer.Exit(1)


@ocr_app.command("disable")
def ocr_disable() -> None:
    """Disable OCR fallback without changing screenshot retention."""
    from .ocr_setup import save_ocr_config

    cfg = _init()
    save_ocr_config(
        enabled=False,
        tier=cfg.capture.ocr_tier,
        config_path=paths.config_file(),
    )
    console.print(
        "[yellow]Local OCR disabled. Run `persome onboard` to apply and verify the saved "
        "policy.[/yellow]"
    )


@app.command("ocr-selftest")
def ocr_selftest(
    image: str = typer.Argument(..., help="Path to an image file to OCR."),
    tier: str = typer.Option("tiny", help="OCR tier: tiny | small."),
) -> None:
    """Run on-device OCR over an image and print the recognized text.

    Verifies the bundled PP-OCRv6 runtime end-to-end (model load + inference). Exits
    non-zero on failure so it can gate a packaged build.
    """
    from pathlib import Path

    from .capture import ocr_local

    data = Path(image).read_bytes()
    text = ocr_local.recognize(data, tier)
    if text is None:
        typer.echo("OCR FAILED: recognize() returned None", err=True)
        raise typer.Exit(code=1)
    typer.echo(text)


@app.command("_ocr-worker", hidden=True)
def _ocr_worker() -> None:
    """Isolated OCR worker loop (internal — spawned by the daemon, not for direct use).

    Reads length-prefixed OCR requests on stdin and writes results on stdout. Paddle is
    imported ONLY in this process, so a native SIGSEGV kills just the worker and the daemon
    fails open + respawns (see #403 / the ocr-subprocess-isolation spec).
    """
    from .capture import ocr_worker

    paths.ensure_dirs()
    logger_mod.setup(console=False)  # file sinks only — keep stdout a clean data channel
    raise typer.Exit(code=ocr_worker.serve())


@app.command("delta-report")
def delta_report(
    limit: int = typer.Option(10, help="Show the N most recent shadow deltas."),
    json_out: str = typer.Option("", help="Also write the structured report JSON here."),
) -> None:
    """Inspect the memory_delta shadow channel (Memory-rebuild Phase 0).

    Read-only consumer of the ``memory_deltas`` table: aggregate per-head item
    counts across the latest delta of each session, gate-drop totals, and the
    most recent rows — the observability half of the shadow dual-run until the
    Phase-1 parity eval lands. Zero-LLM.
    """
    import json as _json
    from pathlib import Path

    from .store import fts
    from .store import memory_deltas as deltas_store

    with fts.cursor() as conn:
        agg = deltas_store.stats(conn)
        rows = deltas_store.recent(conn, limit=limit)
    typer.echo(
        f"memory_deltas: {agg['rows']} row(s) over {agg['sessions']} session(s); "
        f"heads {agg['heads']}; dropped by gates {agg['dropped_by_gates']}"
    )
    for row in rows:
        try:
            heads = {k: len(v) for k, v in _json.loads(row["payload"]).items()}
        except (TypeError, ValueError):
            heads = {}
        typer.echo(
            f"  [{row['created_at']}] session={row['session_id']} status={row['status']} "
            f"dropped={row['dropped']} {heads}"
        )
    if json_out:
        report = {"aggregate": agg, "recent": [dict(r) for r in rows]}
        paths.atomic_write_private_text(
            Path(json_out),
            _json.dumps(report, ensure_ascii=False, indent=2),
        )


@app.command("root-report")
def root_report(
    json_out: str = typer.Option("", help="Also write the structured report JSON here."),
) -> None:
    """Inspect the level-3 root apex (Memory Root Apex, 2026-07-04 spec).

    Read-only: the single live root's provenance/status/token-count + preview, plus the
    cold-start fallback state (no root yet → residency falls back to resident_faces). Zero-LLM.
    """
    import json as _json
    from pathlib import Path

    from .store import fts
    from .store import schema_faces as faces_store
    from .writer.root_synthesis import estimate_tokens

    with fts.cursor() as conn:
        root = faces_store.resident_root(conn)
        resident_fallback = faces_store.resident_faces(conn) if root is None else []
    if root is None:
        typer.echo("root: (none yet) — residency falls back to resident_faces")
        typer.echo(f"  fallback resident_faces: {len(resident_fallback)} active face(s)")
        report = {"root": None, "fallback_faces": len(resident_fallback)}
    else:
        text = root["signature"] or ""
        typer.echo(
            f"root: {root['face_id']}  status={root['status']}  provenance={root['provenance']}  "
            f"~{estimate_tokens(text)} tok  volumes={len(_json.loads(root['members'] or '[]'))}  "
            f"anchors={len(_json.loads(root['anchors'] or '[]'))}  obs={root['observations']}"
        )
        typer.echo("  ── apex ──")
        for line in text.splitlines():
            typer.echo(f"  {line}")
        report = {"root": dict(root), "tokens": estimate_tokens(text)}
    if json_out:
        paths.atomic_write_private_text(
            Path(json_out),
            _json.dumps(report, ensure_ascii=False, indent=2),
        )


@app.command("root-synth")
def root_synth(
    dry_run: bool = typer.Option(False, "--dry-run", help="Synthesize + print, do NOT write."),
) -> None:
    """Manually trigger one root apex synthesis (Memory Root Apex).

    The same pass the schema-tick runs nightly — run it now so the first root is visible
    without waiting for 00:15. Calls the real LLM. ``--dry-run`` prints the would-be apex
    without upserting.
    """
    from .store import fts
    from .writer import root_synthesis as rs

    cfg = _init()
    with fts.cursor() as conn:
        if dry_run:
            # Gather + call + gates, but roll back the upsert by using a savepoint.
            conn.execute("SAVEPOINT root_dry")
            res = rs.synthesize_root(cfg, conn)
            conn.execute("ROLLBACK TO root_dry")
            conn.execute("RELEASE root_dry")
        else:
            res = rs.synthesize_root(cfg, conn)
    typer.echo(f"root-synth: {res.reason}  {res.face_id or '-'}")
    if res.reason != "written":
        raise typer.Exit(code=0 if res.reason in ("skip_empty_input",) else 1)


@app.command("correct")
def correct_cmd(
    correction: str = typer.Argument(
        ..., help="Natural-language correction, for example: 'Peach is my teammate, not me.'"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Preview supersede and retype operations without writing."
    ),
) -> None:
    """Apply a supervised memory correction and retain its source receipts."""
    from .store import fts
    from .writer import correct as correct_mod

    cfg = _init()
    with fts.cursor() as conn:
        res = correct_mod.update_memory(cfg, conn, correction, source="user", dry_run=dry_run)
    typer.echo(f"correct: {res.kind}  ok={res.ok}")
    for a in res.applied:
        typer.echo(f"  - {a}")
    if res.reason:
        typer.echo(f"  reason: {res.reason}")
    if not res.ok and not dry_run and res.kind == "noop":
        raise typer.Exit(code=0)


@app.command("as-of")
def as_of_cmd(
    file: str = typer.Option("", "--file", help="Identity file name, for example person-alex.md."),
    node: str = typer.Option("", "--node", help="A node_id anywhere on a supersede chain."),
    t: str = typer.Option(..., "--t", help="ISO timestamp T to resolve at (e.g. 2026-03-01)."),
    user_id: str = typer.Option("default", help="evomem user scope."),
) -> None:
    """Resolve evo_nodes as of T (Memory-rebuild §1.4 bitemporal node API).

    Read-only twin of the relation graph's edge-side as-of: transaction-clock
    replay (created & un-superseded at T) + validity-window filter. Pass
    --file for an identity's whole node-set, or --node for one chain's
    version at T. Zero-LLM.
    """
    from datetime import datetime as _dt

    from .evomem.as_of import node_as_of, nodes_as_of
    from .store import fts

    try:
        ts = _dt.fromisoformat(t)
    except ValueError:
        typer.echo(f"unparseable --t: {t!r} (want ISO, e.g. 2026-03-01 or 2026-03-01T12:00:00)")
        raise typer.Exit(1) from None
    if bool(file) == bool(node):
        typer.echo("pass exactly one of --file / --node")
        raise typer.Exit(1)
    with fts.cursor() as conn:
        if file:
            got = nodes_as_of(conn, file_name=file, t=ts, user_id=user_id)
            typer.echo(f"{file} as of {ts.isoformat()}: {len(got)} live node(s)")
            for n in got:
                windowed = n.valid_from or n.valid_until
                window = f" [{n.valid_from or '…'} → {n.valid_until or '…'}]" if windowed else ""
                typer.echo(f"  {n.node_id}{window}  {n.content[:80]}")
        else:
            one = node_as_of(conn, node_id=node, t=ts, user_id=user_id)
            if one is None:
                typer.echo(f"chain of {node}: no live version at {ts.isoformat()}")
                raise typer.Exit(1)
            typer.echo(f"chain of {node} at {ts.isoformat()}: {one.node_id}  {one.content[:80]}")


@app.command("faces-report")
def faces_report(
    limit: int = typer.Option(20, help="Show at most N live faces."),
    json_out: str = typer.Option("", help="Also write the structured report JSON here."),
) -> None:
    """Inspect the schema_faces unified schema object (Memory-rebuild Phase 2).

    Read-only consumer of the ``schema_faces`` table (§4.5): every live face's
    provenance (mined | emergent | both), status (shadow to active promotion),
    footprint stability across re-mines (the resampling gate's input), and the
    current resident projection preview. Zero-LLM.
    """
    import json as _json
    import sqlite3 as _sqlite3
    from pathlib import Path

    from .store import fts
    from .store import schema_faces as faces_store

    with fts.cursor() as conn:
        faces_store.ensure_schema(conn)
        conn.row_factory = _sqlite3.Row
        rows = list(
            conn.execute(
                "SELECT * FROM schema_faces WHERE valid_to IS NULL"
                " ORDER BY status, observations DESC LIMIT ?",
                (limit,),
            )
        )
        resident = faces_store.resident_faces(conn)
    typer.echo(f"schema_faces: {len(rows)} live face(s) shown (limit {limit})")
    for row in rows:
        fps = _json.loads(row["footprints"])
        stab = faces_store.stability(fps)
        typer.echo(
            f"  [{row['status']:6}] L{row['level']} {row['provenance']:8} "
            f"obs={row['observations']} conf={row['confidence']:.2f} "
            f"snapshots={len(fps)} stability={stab:.2f}  {row['signature'][:60]}"
        )
    block = faces_store.render_residency(resident)
    if block:
        typer.echo("\n" + block)
    if json_out:
        report = {
            "faces": [dict(r) for r in rows],
            "residency": [dict(r) for r in resident],
        }
        paths.atomic_write_private_text(
            Path(json_out),
            _json.dumps(report, ensure_ascii=False, indent=2),
        )


@app.command("contradictions")
def contradictions_cmd(
    all_rows: bool = typer.Option(False, "--all", help="Show adjudicated rows too."),
    json_out: str = typer.Option("", help="Also write the structured report JSON here."),
) -> None:
    """List the semantic-contradiction adjudication queue (memory-rebuild §4.4).

    Read-only view of ``memory_contradictions``: pairs the nightly self-check
    flagged as mutually exclusive, waiting for a HUMAN verdict
    (``contradictions-resolve``). The flagged entries carry
    an unresolved-conflict warning in recall until adjudicated. Zero-LLM.
    """
    import json as _json
    from pathlib import Path

    from .store import contradictions as contradictions_store
    from .store import fts

    with fts.cursor() as conn:
        rows = contradictions_store.list_rows(conn, status=None if all_rows else "open")
    typer.echo(f"memory_contradictions: {len(rows)} row(s){'' if all_rows else ' open'}")
    for row in rows:
        typer.echo(
            f"  [{row['status']:9}] {row['pair_key']}  ({row['path']})  {row['reason'][:50]}"
        )
        typer.echo(f"     A {row['a_id']}: {row['a_body'][:70]}")
        typer.echo(f"     B {row['b_id']}: {row['b_body'][:70]}")
    if json_out:
        paths.atomic_write_private_text(
            Path(json_out),
            _json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2),
        )


@app.command("contradictions-resolve")
def contradictions_resolve(
    pair_key: str = typer.Argument(..., help="pair_key from `persome contradictions`."),
    keep: str = typer.Option(
        "", help="entry_id the human judges correct (marks the row resolved)."
    ),
    dismiss: bool = typer.Option(False, help="Not actually a contradiction — dismiss the row."),
) -> None:
    """Record the human verdict on a flagged pair and clear the ⚠ marks.

    ``--keep <entry_id>`` = A wins/B wins (row → resolved; superseding the
    loser stays a separate, deliberate memory edit — this command never
    deletes facts). ``--dismiss`` = the judge was wrong, both facts stand.
    Either way both entries' ``conflicted`` metadata is cleared and the pair
    is permanently silenced for the nightly check.
    """
    from .store import contradictions as contradictions_store
    from .store import fts
    from .writer import contradiction_check as check_mod

    if bool(keep) == dismiss:
        typer.echo("pass exactly one of --keep <entry_id> / --dismiss")
        raise typer.Exit(1)
    with fts.cursor() as conn:
        rows = {r["pair_key"]: r for r in contradictions_store.list_rows(conn, status=None)}
        row = rows.get(pair_key)
        if row is None:
            typer.echo(f"unknown pair_key: {pair_key}")
            raise typer.Exit(1)
        if keep and keep not in (row["a_id"], row["b_id"]):
            typer.echo(f"--keep must be one of {row['a_id']} / {row['b_id']}")
            raise typer.Exit(1)
        contradictions_store.close(
            conn,
            pair_key,
            status="resolved" if keep else "dismissed",
            keep_id=keep or None,
        )
        check_mod.clear_conflicted(conn, row["a_id"], row["b_id"])

        # with it — close open edges whose quote came from the losing text.
        closed_edges: list[str] = []
        if keep:
            from .store import relation_edges as edges_store

            loser_id = row["b_id"] if keep == row["a_id"] else row["a_id"]
            loser = conn.execute(
                "SELECT content FROM entries WHERE entry_id = ?", (loser_id,)
            ).fetchone()
            if loser is not None:
                closed_edges = edges_store.close_edges_quoted_in(conn, loser["content"] or "")
    tail = f"; closed {len(closed_edges)} losing-source edge(s)" if keep and closed_edges else ""
    typer.echo(f"{pair_key}: {'resolved, kept ' + keep if keep else 'dismissed'}; ⚠ cleared{tail}")


@app.command("edge-audit")
def edge_audit(
    n: int = typer.Option(20, help="Number of shadow edges to sample, stratified by evidence."),
    seed: int = typer.Option(0, help="Random seed; zero selects a fresh sample."),
    llm: bool = typer.Option(
        False, "--llm", help="Also ask an LLM whether the evidence entails each relation."
    ),
    json_out: str = typer.Option("edge_audit_report.json", help="JSON report output path."),
) -> None:
    """Sample relation edges for structural and optional semantic hallucinations."""
    import json as _json
    from pathlib import Path as _Path

    from .evomem import edge_audit as audit_mod
    from .store import fts

    cfg = _init()
    llm_call = None
    if llm:
        from .writer import llm as llm_mod

        def llm_call(messages):  # noqa: F811
            return llm_mod.call_llm(cfg, "relation_extractor", messages=messages, json_mode=True)

    with fts.cursor() as conn:
        report = audit_mod.run_audit(conn, n=n, seed=seed or None, llm_call=llm_call)
    paths.atomic_write_private_text(
        _Path(json_out), _json.dumps(report, ensure_ascii=False, indent=2)
    )
    rate = report["hallucination_rate"]
    typer.echo(
        f"sampled {report['sample_size']} shadow edges → "
        f"{report['hallucination_count']} hallucinated (rate {rate:.1%})"
        f"{' (with semantic review)' if report['semantic_tier'] else ' (structural only)'}"
    )
    for pred, b in sorted(report["by_predicate"].items()):
        typer.echo(f"  {pred}: {b['hallucinated']}/{b['sampled']}")
    for e in report["edges"]:
        if e["verdict"] != "valid":
            typer.echo(f"  ✗ {e['src']} -{e['predicate']}→ {e['dst']}: {'; '.join(e['notes'])}")
    typer.echo(f"report → {json_out}")


@app.command("entity-retype")
def entity_retype(
    name: str = typer.Argument(..., help="Exact entity display name from person-<name>.md."),
    kind: str = typer.Option(
        "", help="Retype as org, project, or artifact when the entity kind is wrong."
    ),
    to_shadow: bool = typer.Option(
        False, "--to-shadow", help="Shadow a generic class, role, or unresolved identity."
    ),
    alias_of: str = typer.Option(
        "", "--alias-of", help="Merge this alias into the supplied canonical entity name."
    ),
) -> None:
    """Apply one human-reviewed retype, shadow, or alias-merge operation."""
    from . import config as config_mod
    from . import paths as paths_mod
    from .evomem import retype as retype_mod

    chosen = [bool(kind), to_shadow, bool(alias_of)]
    if sum(chosen) != 1:
        typer.echo("pass exactly one of --kind / --to-shadow / --alias-of")
        raise typer.Exit(1)
    if kind:
        res = retype_mod.retype_entity(name, kind)
        typer.echo(
            f"{res.old_file} → {res.new_file} (evo={res.evo_rows} entries={res.entry_rows}"
            f" md={'renamed' if res.md_renamed else 'absent'})"
        )
    elif to_shadow:
        res = retype_mod.shadow_entity(name)
        typer.echo(f"{res.old_file}: {res.shadowed} nodes -> shadow (receipts retained)")
    else:
        cfg = config_mod.load(paths_mod.config_file())
        res = retype_mod.merge_alias(name, alias_of, cfg)
        typer.echo(
            f"merged {name} into {alias_of} as an alias; "
            f"{res.old_file} {res.shadowed} nodes -> shadow"
        )


@app.command("decay-report")
def decay_report(
    candidates: bool = typer.Option(
        False, help="Also preview tonight's candidate clusters (zero-LLM dry scan)."
    ),
    json_out: str = typer.Option("", help="Also write the structured report JSON here."),
) -> None:
    """Inspect text-axis graded forgetting (memory-rebuild §1.5-5).

    Read-only: lists the decayed summaries already landed (decayed:N tag +
    their abstracted-from source counts), and — with --candidates — the
    clusters the nightly pass would pick right now (`[memory_decay]` config,
    no LLM call). Zero-LLM.
    """
    import json as _json
    from pathlib import Path

    from . import config as config_mod
    from . import paths
    from .store import fts
    from .writer import memory_decay as decay_mod

    cfg = config_mod.load(paths.config_file())
    with fts.cursor() as conn:
        conn.row_factory = __import__("sqlite3").Row
        rows = list(
            conn.execute(
                "SELECT id, path, timestamp, tags, content FROM entries"
                " WHERE superseded = 0 AND (tags LIKE '%decayed:1%' OR tags LIKE '%decayed:2%')"
                " ORDER BY timestamp DESC"
            )
        )
        cands = (
            decay_mod.find_decay_clusters(
                conn,
                after_days=cfg.memory_decay.after_days,
                cluster_min=cfg.memory_decay.cluster_min,
                cluster_max=cfg.memory_decay.cluster_max,
                max_clusters=cfg.memory_decay.max_clusters_per_night,
            )
            if candidates
            else []
        )
    typer.echo(
        f"memory decay: enabled={cfg.memory_decay.enabled}; {len(rows)} live decayed summar"
        f"{'y' if len(rows) == 1 else 'ies'}"
    )
    for row in rows:
        tags = (row["tags"] or "").split()
        tier = "L2" if "decayed:2" in tags else "L1"
        sources = next((t for t in tags if t.startswith("abstracted-from:")), ":").split(":", 1)[1]
        n_src = len([s for s in sources.split(",") if s])
        typer.echo(f"  [{tier}] {row['path']}  <-{n_src} sources  {row['content'][:60]}")
    if candidates:
        typer.echo(f"tonight's candidates: {len(cands)} cluster(s)")
        for cl in cands:
            typer.echo(
                f"  tier-{cl.tier} {cl.path}: {len(cl.entry_ids)} entr(y|ies), oldest {cl.oldest_ts}"
            )
    if json_out:
        report = {
            "summaries": [dict(r) for r in rows],
            "candidates": [
                {"path": c.path, "tier": c.tier, "entry_ids": c.entry_ids, "oldest": c.oldest_ts}
                for c in cands
            ],
        }
        paths.atomic_write_private_text(
            Path(json_out),
            _json.dumps(report, ensure_ascii=False, indent=2),
        )


install_app = typer.Typer(help="Register the MCP server with common LLM clients.")
app.add_typer(install_app, name="install")

uninstall_app = typer.Typer(help="Remove Persome's MCP entry from LLM clients.")
app.add_typer(uninstall_app, name="uninstall")

launchagent_app = typer.Typer(
    help="Manage the macOS LaunchAgent so launchd owns the daemon lifecycle."
)
app.add_typer(launchagent_app, name="launchagent")

llm_app = typer.Typer(help="Choose and verify the Runtime's LLM provider.")
app.add_typer(llm_app, name="llm")


def _interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _llm_credential_summary(spec: ProviderSpec) -> str:
    if not spec.key_required:
        return "[dim]local, no key[/dim]"
    if os.environ.get(spec.discovery_api_key_env):
        return "[green]key found[/green]"
    return "[dim]API key required[/dim]"


def _choose_llm_provider(specs: Sequence[ProviderSpec]) -> ProviderSpec:
    for index, spec in enumerate(specs, start=1):
        flags = [_llm_credential_summary(spec)]
        if spec.advanced:
            flags.append("[yellow]advanced[/yellow]")
        console.print(f"  [bold cyan]{index:>2}[/bold cyan]. {spec.label}  {' | '.join(flags)}")
    while True:
        choice = typer.prompt("Choose a provider", type=int)
        if 1 <= choice <= len(specs):
            return specs[choice - 1]
        console.print(f"[red]Enter a number from 1 to {len(specs)}.[/red]")


@llm_app.command("providers")
def llm_providers(
    details: bool = typer.Option(
        False,
        "--details",
        help="Show protocol, default model, endpoint, and credential storage.",
    ),
) -> None:
    """List supported presets and mark credentials already found locally."""
    from .providers import LLM_API_KEY_ENV, PROVIDERS

    env_file_mod.load_env_file(paths.env_file())
    for spec in PROVIDERS:
        flags = [_llm_credential_summary(spec)]
        if spec.advanced:
            flags.append("[yellow]advanced[/yellow]")
        console.print(f"[bold]{spec.label}[/bold] [dim]({spec.id})[/dim]  {' | '.join(flags)}")
        if details:
            credential = "none" if not spec.key_required else LLM_API_KEY_ENV
            console.print(
                f"  Protocol: {spec.protocol}\n"
                f"  Default model: {spec.default_model}\n"
                f"  Endpoint: {spec.base_url or 'configured during advanced setup'}\n"
                f"  Runtime credential: {credential}\n"
                f"  [dim]{spec.description}[/dim]\n"
            )
    if not details:
        console.print(
            "\n[dim]Run `persome llm providers --details` for technical routing details.[/dim]"
        )
    console.print(
        "[dim]Provider presets choose the endpoint and model automatically. "
        "Azure and custom providers use an advanced setup path.[/dim]"
    )


@llm_app.command("status")
def llm_status(
    check: bool = typer.Option(False, "--check", help="Run a live completion and tool-call probe."),
) -> None:
    """Show the effective provider profile without revealing its key."""
    from .llm_setup import probe_profile
    from .providers import resolve_profile

    env_file_mod.load_env_file(paths.env_file())
    cfg = config_mod.load()
    profile = resolve_profile(cfg.model_for("default"))
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("Provider", f"{profile.provider_label} ({profile.provider})")
    table.add_row("Protocol", profile.protocol)
    table.add_row("Model", profile.model)
    table.add_row("Endpoint", profile.base_url or "provider default")
    if not profile.key_required:
        credential = "[green]not required[/green] (local endpoint)"
    elif profile.credential_ready:
        credential = f"[green]set[/green] via {profile.api_key_env}"
        if profile.credential_migration_required:
            credential = f"[green]set[/green] (ready to migrate to {profile.api_key_env})"
    else:
        credential = f"[red]missing[/red] ({profile.api_key_env})"
    table.add_row("Credential", credential)
    table.add_row("Configuration", "legacy compatibility" if profile.legacy else "explicit profile")
    console.print(table)
    if profile.legacy:
        console.print(
            "[yellow]This installation still uses the pre-provider compatibility route. "
            "Run `persome llm setup` to verify and migrate it explicitly.[/yellow]"
        )
    elif profile.credential_migration_required:
        console.print(
            "[yellow]The key is being read through a provider-specific compatibility fallback. "
            "Run `persome llm setup` to store it as PERSOME_LLM_API_KEY.[/yellow]"
        )
    if check:
        if not profile.credential_ready:
            console.print(
                f"[red]✗ {profile.api_key_env} is missing. Run `persome llm setup`.[/red]"
            )
            raise typer.Exit(1)
        with console.status("Testing completion and tool calling..."):
            result = probe_profile(profile)
        if not result.completion_ok:
            console.print(f"[red]✗ Probe failed:[/red] {result.error}")
            raise typer.Exit(1)
        console.print(f"[green]✓ Completion works[/green] ({result.latency_ms} ms)")
        if result.tool_call_ok:
            console.print("[green]✓ Tool calling works[/green]")
        else:
            console.print("[yellow]⚠ Tool calling was not confirmed for this model.[/yellow]")
            if result.error:
                console.print(f"[dim]{result.error}[/dim]")
            raise typer.Exit(1)


@llm_app.command("setup")
def llm_setup(
    provider: str = typer.Option(
        "", "--provider", help="Provider id from `persome llm providers`."
    ),
    model: str = typer.Option("", "--model", help="Advanced: override the preset model."),
    base_url: str = typer.Option("", "--base-url", help="Advanced: override the endpoint."),
    api_key_env: str = typer.Option(
        "", "--api-key-env", help="Advanced: import the key from this environment variable."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Accept detected/default values."),
    allow_no_tools: bool = typer.Option(
        False,
        "--allow-no-tools",
        help="Save even when tool calling cannot be confirmed (modeling may degrade).",
    ),
    skip_check: bool = typer.Option(
        False,
        "--skip-check",
        help="Save without a live probe (not recommended).",
    ),
) -> None:
    """Choose a provider, enter its key, verify it, and save the Runtime profile."""
    from .llm_setup import probe_profile, save_profile
    from .providers import (
        LLM_API_KEY_ENV,
        PROVIDERS,
        detected_providers,
        make_profile,
        provider_spec,
        resolve_profile,
    )

    paths.ensure_dirs()
    config_mod.write_default_if_missing()
    env_file_mod.load_env_file(paths.env_file())
    cfg = config_mod.load()
    current = resolve_profile(cfg.model_for("default"))
    interactive = _interactive_terminal() and not yes

    selected = None
    keep_current = False
    declined_current = False
    if provider:
        selected = provider_spec(provider)
        if selected is None:
            console.print(f"[red]Unknown provider {provider!r}. Run `persome llm providers`.[/red]")
            raise typer.Exit(2)
    elif current.credential_ready:
        if interactive:
            console.print(f"Current provider: [bold]{current.provider_label}[/bold]")
            keep_current = typer.confirm("Use and verify this provider?", default=True)
            declined_current = not keep_current
        else:
            keep_current = True

    if selected is None and not keep_current:
        # A direct "no" means the next provider must be an explicit user
        # choice. Do not silently select any discovered alternative (or a
        # regional preset that shares the current credential).
        detected = [] if declined_current else detected_providers()
        if len(detected) == 1:
            selected = detected[0]
            console.print(f"[green]Found an existing {selected.label} API key.[/green]")
        elif len(detected) > 1:
            if not interactive:
                names = ", ".join(spec.id for spec in detected)
                console.print(
                    f"[red]Multiple credentials detected ({names}); pass --provider.[/red]"
                )
                raise typer.Exit(2)
            console.print("Multiple provider credentials were found:")
            selected = _choose_llm_provider(tuple(detected))
        elif not interactive:
            console.print(
                "[red]No configured LLM credential found; pass --provider and export its key.[/red]"
            )
            raise typer.Exit(2)
        else:
            console.print("Choose the LLM provider Persome should use:")
            selected = _choose_llm_provider(PROVIDERS)

    chosen_key_env = LLM_API_KEY_ENV
    source_key_env = api_key_env
    if keep_current:
        provider_id = current.provider
        protocol = current.protocol
        chosen_model = model or current.model
        chosen_base_url = base_url or current.base_url
        api_key = os.environ.get(source_key_env) if source_key_env else current.api_key
    else:
        assert selected is not None
        provider_id = selected.id
        protocol = selected.protocol
        chosen_model = model or selected.default_model
        chosen_base_url = base_url or os.environ.get(selected.resolved_base_url_env, "")
        chosen_base_url = chosen_base_url or selected.base_url
        if source_key_env:
            api_key = os.environ.get(source_key_env)
        else:
            api_key = os.environ.get(selected.discovery_api_key_env)
            # The neutral key belongs to the active profile. Reuse it only
            # when reconfiguring that same provider, never after a switch.
            if api_key is None and selected.id == current.provider:
                api_key = current.api_key

    selected_spec = selected or provider_spec(provider_id)
    provider_label = selected_spec.label if selected_spec is not None else current.provider_label
    advanced_setup = bool(
        (selected_spec and selected_spec.advanced) or base_url or model or api_key_env
    )
    if interactive and not keep_current and selected_spec and selected_spec.advanced:
        console.print(
            "[yellow]Advanced setup:[/yellow] this provider needs deployment-specific "
            "routing details."
        )
        api_key = (os.environ.get(source_key_env) if source_key_env else None) or api_key
        if not base_url:
            chosen_base_url = typer.prompt("API endpoint", default=chosen_base_url or "")
        if not model:
            chosen_model = typer.prompt("Model id", default=chosen_model)
    if not chosen_base_url:
        console.print("[red]An API endpoint is required.[/red]")
        raise typer.Exit(2)
    if not chosen_model:
        console.print("[red]A model id is required.[/red]")
        raise typer.Exit(2)
    parsed_endpoint = urlparse(chosen_base_url)
    if parsed_endpoint.scheme not in {"http", "https"} or not parsed_endpoint.netloc:
        console.print("[red]The API endpoint must be an absolute http(s) URL.[/red]")
        raise typer.Exit(2)
    if source_key_env and (not source_key_env.isascii() or not source_key_env.isidentifier()):
        console.print("[red]The API key environment variable name is invalid.[/red]")
        raise typer.Exit(2)

    key_required = selected.key_required if selected is not None else current.key_required
    if key_required and not api_key:
        if not interactive:
            console.print(
                f"[red]No API key was found. Set {LLM_API_KEY_ENV} or run setup "
                "interactively.[/red]"
            )
            raise typer.Exit(2)
        api_key = typer.prompt(
            f"{provider_label} API key",
            hide_input=True,
        )

    profile = make_profile(
        provider_id,
        model=chosen_model,
        base_url=chosen_base_url,
        api_key_env=chosen_key_env,
        api_key=api_key,
        protocol=protocol,
    )

    while not skip_check:
        with console.status("Testing completion and tool calling before saving..."):
            result = probe_profile(profile)
        if not result.completion_ok:
            console.print(f"[red]✗ Provider check failed. Nothing was saved.[/red] {result.error}")
            if not interactive:
                raise typer.Exit(1)
            if advanced_setup:
                if not typer.confirm("Edit advanced settings and retry?", default=True):
                    raise typer.Exit(1)
                chosen_base_url = typer.prompt("API endpoint", default=profile.base_url)
                chosen_model = typer.prompt("Model id", default=profile.model)
                replacement = typer.prompt(
                    "New API key (Enter to keep the current key)",
                    default="",
                    show_default=False,
                    hide_input=True,
                )
                api_key = replacement or profile.api_key
            else:
                if not typer.confirm("Try a different API key?", default=True):
                    raise typer.Exit(1)
                api_key = typer.prompt(
                    f"{provider_label} API key",
                    hide_input=True,
                )
            profile = make_profile(
                provider_id,
                model=chosen_model,
                base_url=chosen_base_url,
                api_key_env=chosen_key_env,
                api_key=api_key,
                protocol=protocol,
            )
            continue
        console.print(f"[green]✓ Completion works[/green] ({result.latency_ms} ms)")
        if result.tool_call_ok:
            console.print("[green]✓ Tool calling works[/green]")
            break
        console.print(
            "[yellow]The endpoint completed a prompt, but this model did not call the test "
            "tool. Persome modeling relies on tool calling.[/yellow]"
        )
        if result.error:
            console.print(f"[dim]{result.error}[/dim]")
        if allow_no_tools or (
            interactive and typer.confirm("Save this limited model anyway?", default=False)
        ):
            break
        if not advanced_setup:
            console.print(
                "[red]The provider preset did not pass the tool-call check. Nothing was saved."
                "[/red]"
            )
            raise typer.Exit(1)
        if not interactive or not typer.confirm("Choose another model and retry?", default=True):
            console.print("[red]Nothing was saved.[/red]")
            raise typer.Exit(1)
        chosen_model = typer.prompt("Model id", default=profile.model)
        profile = make_profile(
            provider_id,
            model=chosen_model,
            base_url=chosen_base_url,
            api_key_env=chosen_key_env,
            api_key=api_key,
            protocol=protocol,
        )

    save_profile(profile, config_path=paths.config_file(), env_path=paths.env_file())
    console.print(f"[green]✓ Saved {profile.provider_label} as the Runtime LLM profile.[/green]")
    console.print(f"  Config: {paths.config_file()}")
    if profile.api_key:
        console.print(f"  Secret: {paths.env_file()} (mode 0600)")
    console.print("  Next: persome llm status --check")


def _default_daemon_binary() -> str:
    """Best-effort path to the daemon executable used in the plist.

    When running from the PyInstaller bundle, ``sys.executable`` *is* the
    ``persome`` binary. Otherwise (dev/editable install) fall back to the
    resolved ``persome`` shim on PATH, then to ``sys.executable``."""
    if getattr(sys, "frozen", False):
        return sys.executable
    shim = shutil.which("persome")
    if shim:
        return shim
    return sys.executable


def _stdio_mcp_command() -> list[str]:
    """Return an exact local command for clients that can spawn MCP over stdio."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "mcp"]
    shim = shutil.which("persome")
    if shim:
        return [shim, "mcp"]
    # Source checkouts and isolated test environments may not have installed a
    # console shim.  The module entry point is equivalent and remains exact.
    return [sys.executable, "-m", "persome", "mcp"]


def _write_private_json(path: Path, payload: dict) -> None:
    """Atomically write a client config without leaving a world-readable secret."""
    if path.is_symlink():
        raise RuntimeError(f"refusing to replace symlinked config: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


@launchagent_app.command("install")
def launchagent_install(
    binary: str = typer.Option(
        "",
        "--binary",
        help="Path to the persome daemon binary baked into the plist. "
        "Defaults to the current executable.",
    ),
) -> None:
    """Write the LaunchAgent plist and bootstrap it into launchd."""
    from . import launchagent

    paths.ensure_dirs()
    resolved = binary or _default_daemon_binary()
    target = launchagent.install(resolved)
    console.print(f"[green]LaunchAgent installed → {target}[/green]")
    console.print(f"  Label:   {launchagent.LABEL}")
    console.print(f"  Program: {resolved} start --foreground")


@launchagent_app.command("uninstall")
def launchagent_uninstall() -> None:
    """Boot the LaunchAgent out of launchd and remove its plist."""
    from . import launchagent

    launchagent.uninstall()
    console.print("[green]LaunchAgent removed.[/green]")


@launchagent_app.command("status")
def launchagent_status() -> None:
    """Report whether launchd currently manages the daemon."""
    from . import launchagent

    loaded = launchagent.is_loaded()
    exists = launchagent.plist_path().exists()
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("Label", launchagent.LABEL)
    table.add_row("Plist", str(launchagent.plist_path()))
    table.add_row("Plist file", "[green]present[/green]" if exists else "[red]missing[/red]")
    table.add_row("Loaded", "[green]yes[/green]" if loaded else "[red]no[/red]")
    console.print(table)
    if not loaded:
        raise typer.Exit(1)


@install_app.command("claude-code")
def install_claude_code(
    name: str = typer.Option("persome", help="MCP server name shown to the client."),
    scope: str = typer.Option("user", help="Claude Code scope: user | local | project."),
) -> None:
    """Add Persome to Claude Code as an owner-local stdio MCP subprocess."""
    _init()

    claude_bin = shutil.which("claude")
    if not claude_bin:
        console.print(
            "[red]`claude` CLI not found on PATH.[/red] "
            "Install Claude Code first, or edit ~/.claude.json manually."
        )
        raise typer.Exit(1)

    stdio_command = _stdio_mcp_command()

    remove = subprocess.run(
        [claude_bin, "mcp", "remove", "-s", scope, name],
        capture_output=True,
        text=True,
        check=False,
    )
    replaced = remove.returncode == 0

    cmd = [
        claude_bin,
        "mcp",
        "add",
        "-s",
        scope,
        name,
        "--",
        *stdio_command,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        console.print(f"[red]claude mcp add failed:[/red]\n{result.stderr or result.stdout}")
        raise typer.Exit(result.returncode)

    verb = "Updated" if replaced else "Registered"
    console.print(f"[green]{verb} {name!r} in Claude Code ({scope} scope).[/green]")
    console.print(f"  command: {' '.join(stdio_command)}")


def _claude_desktop_config_path() -> Path:
    return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"


def _load_claude_desktop_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        console.print(
            f"[red]Could not parse {path}:[/red] {exc}\n"
            "Fix the JSON or move the file aside and rerun."
        )
        raise typer.Exit(1) from exc
    if not isinstance(data, dict):
        console.print(f"[red]Unexpected top-level shape in {path} (expected object).[/red]")
        raise typer.Exit(1)
    return data


def _restart_reminder(action: str) -> None:
    console.print(
        f"[yellow]Claude Desktop must be fully quit (Cmd+Q) and reopened to {action}.[/yellow]"
    )
    console.print(
        "[dim]The app only reads claude_desktop_config.json at launch. You won't need to "
        "re-login — restart is enough, your session persists.[/dim]"
    )


@install_app.command("claude-desktop")
def install_claude_desktop(
    name: str = typer.Option("persome", help="MCP server name shown to the client."),
) -> None:
    """Add (or refresh) Persome's entry in Claude Desktop's MCP config.

    Claude Desktop's JSON config only accepts stdio servers (remote SSE/HTTP
    must be added via Settings → Integrations UI), so we register
    ``persome mcp`` as a subprocess command.

    Every invocation is idempotent — existing entries with the same name are
    overwritten with the current absolute path.
    """
    stdio_command = _stdio_mcp_command()

    cfg_path = _claude_desktop_config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)

    data = _load_claude_desktop_config(cfg_path)
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        console.print(f"[red]`mcpServers` in {cfg_path} is not an object.[/red]")
        raise typer.Exit(1)

    replaced = name in servers
    servers[name] = {
        "command": stdio_command[0],
        "args": stdio_command[1:],
    }

    try:
        _write_private_json(cfg_path, data)
    except (OSError, RuntimeError) as exc:
        console.print(f"[red]Could not write {cfg_path}:[/red] {exc}")
        raise typer.Exit(1) from exc

    verb = "Updated" if replaced else "Registered"
    console.print(f"[green]{verb} {name!r} in Claude Desktop config.[/green]")
    console.print(f"  file: {cfg_path}")
    console.print(f"  command: {' '.join(stdio_command)}")
    _restart_reminder("pick up the new entry")


@install_app.command("codex")
def install_codex(
    name: str = typer.Option("persome", help="MCP server name shown to the client."),
) -> None:
    """Add Persome to Codex as an owner-local stdio MCP subprocess."""
    _init()

    codex_bin = shutil.which("codex")
    if not codex_bin:
        console.print(
            "[red]`codex` CLI not found on PATH.[/red] "
            "Install Codex first (https://github.com/openai/codex), "
            "or edit ~/.codex/config.toml manually."
        )
        raise typer.Exit(1)

    stdio_command = _stdio_mcp_command()

    remove = subprocess.run(
        [codex_bin, "mcp", "remove", name],
        capture_output=True,
        text=True,
        check=False,
    )
    replaced = remove.returncode == 0

    cmd = [codex_bin, "mcp", "add", name, "--", *stdio_command]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        console.print(f"[red]codex mcp add failed:[/red]\n{result.stderr or result.stdout}")
        raise typer.Exit(result.returncode)

    verb = "Updated" if replaced else "Registered"
    console.print(f"[green]{verb} {name!r} in Codex CLI.[/green]")
    console.print(f"  command: {' '.join(stdio_command)}")


def _opencode_config_path() -> Path:
    return Path.home() / ".config" / "opencode" / "opencode.json"


def _load_opencode_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        console.print(
            f"[red]Could not parse {path}:[/red] {exc}\n"
            "If your config is JSONC (with comments), edit the `mcp` section manually."
        )
        raise typer.Exit(1) from exc
    if not isinstance(data, dict):
        console.print(f"[red]Unexpected top-level shape in {path} (expected object).[/red]")
        raise typer.Exit(1)
    return data


@install_app.command("opencode")
def install_opencode(
    name: str = typer.Option("persome", help="MCP server name shown to the client."),
) -> None:
    """Add Persome to opencode as an owner-local stdio MCP subprocess."""
    _init()
    stdio_command = _stdio_mcp_command()

    cfg_path = _opencode_config_path()
    jsonc_path = cfg_path.with_suffix(".jsonc")
    if jsonc_path.exists():
        manual_entry = json.dumps(
            {"type": "local", "command": stdio_command, "enabled": True},
            ensure_ascii=False,
        )
        console.print(
            f"[red]Found {jsonc_path} — can't safely edit JSONC (comments would be lost).[/red]\n"
            "Add this entry under the `mcp` key manually:\n"
            f'  "{name}": {manual_entry}'
        )
        raise typer.Exit(1)

    existed = cfg_path.exists()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    data = _load_opencode_config(cfg_path)
    if not existed:
        data["$schema"] = "https://opencode.ai/config.json"

    servers = data.setdefault("mcp", {})
    if not isinstance(servers, dict):
        console.print(f"[red]`mcp` in {cfg_path} is not an object.[/red]")
        raise typer.Exit(1)

    replaced = name in servers
    servers[name] = {
        "type": "local",
        "command": stdio_command,
        "enabled": True,
    }

    try:
        _write_private_json(cfg_path, data)
    except (OSError, RuntimeError) as exc:
        console.print(f"[red]Could not write {cfg_path}:[/red] {exc}")
        raise typer.Exit(1) from exc

    verb = "Updated" if replaced else "Registered"
    console.print(f"[green]{verb} {name!r} in opencode config.[/green]")
    console.print(f"  command: {' '.join(stdio_command)}")


@install_app.command("mcp-json")
def install_mcp_json(
    name: str = typer.Option("persome", help="MCP server name written into the config."),
    filename: str = typer.Option("mcp.json", help="Output filename (written to CWD)."),
    http: bool = typer.Option(
        False,
        "--http",
        help="Emit a URL-based entry using the configured HTTP endpoint instead of stdio.",
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite if the file exists."),
) -> None:
    """Generate a generic MCP config in the current directory.

    Shape matches the ``mcpServers`` object used by most local agent
    frameworks (Cursor, Cline, Continue, Zed, Windsurf, custom tools). Drop
    the emitted file next to your agent's config or merge its contents into
    an existing one.
    """
    cfg = _init()
    out_path = Path.cwd() / filename
    if out_path.exists() and not force:
        console.print(f"[red]{out_path} already exists.[/red] Use --force to overwrite.")
        raise typer.Exit(1)

    if http:
        from .mcp import server as mcp_server
        from .security.auth import auth_headers

        if cfg.mcp.transport not in ("sse", "streamable-http"):
            console.print(
                f"[red]--http requires mcp.transport to be sse or streamable-http, "
                f"got {cfg.mcp.transport!r}.[/red]"
            )
            raise typer.Exit(1)
        url = mcp_server.endpoint_url(cfg)
        transport_label = "sse" if cfg.mcp.transport == "sse" else "http"
        env_file_mod.load_env_file(paths.env_file())
        try:
            headers = auth_headers()
        except RuntimeError as exc:
            console.print(f"[red]Cannot create authenticated HTTP config:[/red] {exc}")
            raise typer.Exit(1) from exc
        entry: dict[str, object] = {
            "url": url,
            "transport": transport_label,
            "headers": headers,
        }
        summary = f"{transport_label} → {url}"
    else:
        stdio_command = _stdio_mcp_command()
        entry = {"command": stdio_command[0], "args": stdio_command[1:]}
        summary = f"stdio → {' '.join(stdio_command)}"

    payload = {"mcpServers": {name: entry}}
    try:
        _write_private_json(out_path, payload)
    except (OSError, RuntimeError) as exc:
        console.print(f"[red]Could not write {out_path}:[/red] {exc}")
        raise typer.Exit(1) from exc

    console.print(f"[green]Wrote {out_path}[/green]")
    console.print(f"  server: {name} ({summary})")
    console.print(
        "[dim]Point your agent framework at this file, or merge `mcpServers` "
        "into its existing MCP config.[/dim]"
    )
    if http:
        console.print(
            "[yellow]This owner-only file contains the local API bearer token; "
            "do not commit or share it.[/yellow]"
        )


@uninstall_app.command("claude-code")
def uninstall_claude_code(
    name: str = typer.Option("persome", help="MCP server name to remove."),
    scope: str = typer.Option("user", help="Claude Code scope the entry was installed at."),
) -> None:
    """Remove Persome's entry from Claude Code's MCP config.

    Scope must match whatever ``install claude-code`` used (default ``user``).
    Missing entries are treated as success — the command is idempotent.
    """
    claude_bin = shutil.which("claude")
    if not claude_bin:
        console.print(
            "[red]`claude` CLI not found on PATH.[/red] "
            "Install Claude Code first, or edit ~/.claude.json manually."
        )
        raise typer.Exit(1)

    result = subprocess.run(
        [claude_bin, "mcp", "remove", "-s", scope, name],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        console.print(f"[green]Removed {name!r} from Claude Code ({scope} scope).[/green]")
        return

    combined = (result.stderr + result.stdout).lower()
    if "no mcp server" in combined or "not found" in combined:
        console.print(f"[yellow]No {name!r} entry at {scope} scope — nothing to remove.[/yellow]")
        return

    console.print(f"[red]claude mcp remove failed:[/red]\n{result.stderr or result.stdout}")
    raise typer.Exit(result.returncode)


@uninstall_app.command("codex")
def uninstall_codex(
    name: str = typer.Option("persome", help="MCP server name to remove."),
) -> None:
    """Remove Persome's entry from Codex CLI's MCP config.

    Missing entries are treated as success — the command is idempotent.
    """
    codex_bin = shutil.which("codex")
    if not codex_bin:
        console.print(
            "[red]`codex` CLI not found on PATH.[/red] "
            "Install Codex first, or edit ~/.codex/config.toml manually."
        )
        raise typer.Exit(1)

    result = subprocess.run(
        [codex_bin, "mcp", "remove", name],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        console.print(f"[green]Removed {name!r} from Codex CLI.[/green]")
        return

    combined = (result.stderr + result.stdout).lower()
    if "no mcp server" in combined or "not found" in combined or "does not exist" in combined:
        console.print(f"[yellow]No {name!r} entry in Codex config — nothing to remove.[/yellow]")
        return

    console.print(f"[red]codex mcp remove failed:[/red]\n{result.stderr or result.stdout}")
    raise typer.Exit(result.returncode)


@uninstall_app.command("opencode")
def uninstall_opencode(
    name: str = typer.Option("persome", help="MCP server name to remove."),
) -> None:
    """Remove Persome's entry from opencode's MCP config.

    Missing config / missing entry are treated as success — the command is
    idempotent.
    """
    cfg_path = _opencode_config_path()
    if not cfg_path.exists():
        console.print(f"[yellow]No opencode config at {cfg_path} — nothing to remove.[/yellow]")
        return

    data = _load_opencode_config(cfg_path)
    servers = data.get("mcp")
    if not isinstance(servers, dict) or name not in servers:
        console.print(f"[yellow]No {name!r} entry in opencode config — nothing to remove.[/yellow]")
        return

    del servers[name]
    try:
        _write_private_json(cfg_path, data)
    except (OSError, RuntimeError) as exc:
        console.print(f"[red]Could not write {cfg_path}:[/red] {exc}")
        raise typer.Exit(1) from exc

    console.print(f"[green]Removed {name!r} from opencode config.[/green]")


@uninstall_app.command("claude-desktop")
def uninstall_claude_desktop(
    name: str = typer.Option("persome", help="MCP server name to remove."),
) -> None:
    """Remove Persome's entry from Claude Desktop's MCP config.

    Missing config / missing entry are treated as success — the command is
    idempotent.
    """
    cfg_path = _claude_desktop_config_path()
    if not cfg_path.exists():
        console.print(
            f"[yellow]No Claude Desktop config at {cfg_path} — nothing to remove.[/yellow]"
        )
        return

    data = _load_claude_desktop_config(cfg_path)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or name not in servers:
        console.print(
            f"[yellow]No {name!r} entry in Claude Desktop config — nothing to remove.[/yellow]"
        )
        return

    del servers[name]
    try:
        _write_private_json(cfg_path, data)
    except (OSError, RuntimeError) as exc:
        console.print(f"[red]Could not write {cfg_path}:[/red] {exc}")
        raise typer.Exit(1) from exc

    console.print(f"[green]Removed {name!r} from Claude Desktop config.[/green]")
    _restart_reminder("finalize the removal")


timeline_app = typer.Typer(help="Timeline (short-window activity blocks) subcommands.")
app.add_typer(timeline_app, name="timeline")


@timeline_app.command("tick")
def timeline_tick_cmd() -> None:
    """Build any closed timeline windows right now (synchronous)."""
    cfg = _init()
    from .timeline import tick as tick_mod

    produced = tick_mod.tick_now(cfg)
    console.print(f"[green]Produced {produced} block(s).[/green]")


@timeline_app.command("list")
def timeline_list(
    limit: int = typer.Option(12, "--limit", "-n", help="How many recent blocks to show."),
) -> None:
    """Show the most recent timeline blocks (oldest → newest)."""
    _init()
    from .timeline import store as tls

    with fts.cursor() as conn:
        blocks = tls.query_recent(conn, limit=limit)
    if not blocks:
        console.print("[yellow]No timeline blocks yet.[/yellow]")
        return
    for b in blocks:
        apps = ", ".join(b.apps_used) or "—"
        console.print(
            f"[bold]{b.start_time.strftime('%Y-%m-%d %H:%M')}"
            f"–{b.end_time.strftime('%H:%M')}[/bold] "
            f"({b.capture_count} captures, apps: {apps})"
        )
        for e in b.entries:
            console.print(f"  - {e}")


writer_app = typer.Typer(help="Writer subcommands.")
app.add_typer(writer_app, name="writer")

model_app = typer.Typer(help="Build, inspect, and export the personal model.")
app.add_typer(model_app, name="model")

_MODEL_VIEWER_STARTUP_ATTEMPTS = 20
_MODEL_VIEWER_STARTUP_RETRY_SECONDS = 0.25
_MODEL_VIEWER_REMINDER_LOG = "model-open-reminder.log"


def _model_open_command() -> list[str]:
    """Return a relocation-safe command for a detached model-viewer reminder."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "persome"]


def _schedule_model_open(after_seconds: float) -> tuple[int, Path]:
    """Start one detached, one-shot process that opens the viewer after a delay."""
    if after_seconds <= 0:
        raise ValueError("the model viewer delay must be greater than zero")
    paths.ensure_dirs()
    log_path = paths.logs_dir() / _MODEL_VIEWER_REMINDER_LOG
    env = os.environ.copy()
    env["PERSOME_ROOT"] = str(paths.root())
    command = [
        *_model_open_command(),
        "model",
        "open",
        "--scheduled-after-seconds",
        str(after_seconds),
    ]
    with paths.open_private_append_text(log_path) as log_handle:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=True,
            env=env,
        )
    return process.pid, log_path


def _local_viewer_base_url(cfg: config_mod.Config) -> str:
    """Return a loopback URL for the daemon viewer, never a LAN HTTP origin."""
    from .security.auth import loopback_http_url

    return loopback_http_url(cfg.mcp.host, cfg.mcp.port)


@model_app.command("build")
def model_build(
    wait_seconds: float = typer.Option(
        30.0, "--wait-seconds", min=0.0, help="Seconds to wait for another build."
    ),
    no_wait: bool = typer.Option(False, "--no-wait", help="Return busy immediately."),
) -> None:
    """Run the shared one-shot Point/Line/Face/Volume/Root build."""
    from .model import ModelBuildBusy, ModelRecoveryIncomplete, run_model_build

    cfg = _init()
    try:
        result = run_model_build(cfg, wait_seconds=0.0 if no_wait else wait_seconds)
    except ModelRecoveryIncomplete as exc:
        console.print(f"[red]model build blocked: {exc}[/red]")
        raise typer.Exit(2) from exc
    except ModelBuildBusy as exc:
        console.print(f"[yellow]busy: {exc}[/yellow]")
        raise typer.Exit(2) from exc
    counts = result.stats
    console.print(
        f"[bold]model build: {result.status}[/bold]  "
        f"points={counts['points']} lines="
        f"{counts['evolution_lines'] + counts['relation_lines']} "
        f"faces={counts['faces']} volumes={counts['volumes']} roots={counts['roots']}"
    )
    cross_domain = getattr(result, "stages", {}).get("cross_domain_sweeper", {})
    if cross_domain.get("status") == "complete":
        deferred = int(cross_domain.get("pairs_deferred", 0))
        console.print(
            "cross-domain: "
            f"probed={int(cross_domain.get('pairs_probed', 0))} "
            f"deferred={deferred} limit={int(cross_domain.get('probe_limit', 0))}"
        )
        if deferred:
            console.print(
                "[yellow]Deferred pairs stay queued for later scheduled or explicit builds.[/yellow]"
            )
    console.print(f"manifest: {result.manifest_path}")


@model_app.command("export")
def model_export(
    out: str = typer.Option("", "--out", help="Output JSON path (default: root exports dir)."),
    raw: bool = typer.Option(False, "--raw", help="Include unredacted local text."),
) -> None:
    """Export the current versioned model snapshot; redacted by default."""
    from .model import build_live_snapshot, export_snapshot

    _init()
    if raw:
        console.print("[yellow]warning: --raw may contain sensitive personal data[/yellow]")
    target = Path(out).expanduser() if out else None
    with fts.cursor() as conn:
        snapshot = build_live_snapshot(conn, redact=not raw)
        path = export_snapshot(
            conn,
            out_path=target,
            snapshot_data=snapshot,
        )
    console.print(f"model snapshot: {path}")


@model_app.command("status")
def model_status_cmd() -> None:
    """Show live model readiness, geometry counts, and the last build id."""
    from .model import build_live_snapshot, model_status

    _init()
    with fts.cursor() as conn:
        snapshot = build_live_snapshot(conn)
        status = model_status(conn, snapshot_data=snapshot)
    last = snapshot["build"]
    build_status = str(last["status"])
    if build_status == "not_built":
        readiness = "not built"
    elif build_status == "building":
        readiness = "building"
    else:
        readiness = "ready" if status["ready"] else "not ready"
    issues = list(status["issues"])
    if build_status == "not_built":
        issues.append("no_completed_build")
    elif build_status == "building":
        issues.append("build_in_progress")
    counts = status["stats"]
    console.print(
        f"[bold]model: {readiness}[/bold]  "
        f"points={counts['points']} lines="
        f"{counts['evolution_lines'] + counts['relation_lines']} "
        f"faces={counts['faces']} volumes={counts['volumes']} roots={counts['roots']}"
    )
    if issues:
        console.print(f"issues: {', '.join(issues)}")
    console.print(f"build status: {build_status}")
    console.print(f"last build: {last.get('build_id') or 'none'}")


@model_app.command("open")
def model_open(
    after: int = typer.Option(
        0,
        "--after",
        min=0,
        help="Open automatically after this many minutes; 0 opens now.",
    ),
    scheduled_after_seconds: float = typer.Option(
        0.0,
        "--scheduled-after-seconds",
        min=0.0,
        hidden=True,
    ),
) -> None:
    """Open the local model viewer through a short-lived browser capability."""
    if after and scheduled_after_seconds:
        console.print("[red]Choose either --after or the internal scheduled delay, not both.[/red]")
        raise typer.Exit(2)
    if after:
        try:
            _pid, log_path = _schedule_model_open(after * 60)
        except (OSError, RuntimeError, ValueError) as exc:
            console.print(f"[red]Could not schedule the model viewer:[/red] {exc}")
            raise typer.Exit(1) from exc
        unit = "minute" if after == 1 else "minutes"
        console.print(
            f"[bold green]✓ Your personal model will open automatically in {after} {unit}.[/bold green]"
        )
        console.print("  Keep Persome running so it can learn from real activity.")
        console.print("  Open it now: [bold]persome model open[/bold]")
        console.print(f"  Reminder log: {log_path}")
        return
    if scheduled_after_seconds:
        time.sleep(scheduled_after_seconds)

    import webbrowser

    import httpx

    cfg = _init()
    if cfg.mcp.transport not in {"sse", "streamable-http"}:
        console.print("[red]The model viewer requires the daemon HTTP transport.[/red]")
        raise typer.Exit(1)

    from .security.auth import BROWSER_BOOTSTRAP_PATH, auth_headers

    try:
        base_url = _local_viewer_base_url(cfg)
        headers = auth_headers()
        for attempt in range(_MODEL_VIEWER_STARTUP_ATTEMPTS):
            try:
                response = httpx.post(
                    f"{base_url}{BROWSER_BOOTSTRAP_PATH}",
                    headers=headers,
                    timeout=5.0,
                    trust_env=False,
                )
                break
            except httpx.ConnectError:
                # `persome start` returns after forking, just before uvicorn has
                # necessarily bound its loopback socket. Retry only that
                # connection-refused startup race. Any HTTP response (notably
                # 401/403) is handled below immediately instead of being hidden
                # behind retries. A genuinely stopped Runtime fails
                # immediately instead of looking like another CLI hang.
                runtime_starting = _read_pid() is not None or _daemon_lock_is_held()
                if not runtime_starting or attempt + 1 == _MODEL_VIEWER_STARTUP_ATTEMPTS:
                    raise
                time.sleep(_MODEL_VIEWER_STARTUP_RETRY_SECONDS)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {401, 403}:
            console.print(
                "[red]Local model viewer authentication failed[/red] "
                f"(HTTP {exc.response.status_code}). The daemon and this CLI may be using "
                "different owner credentials; restart Persome, then retry."
            )
        else:
            console.print(
                "[red]The local model viewer request failed[/red] "
                f"(HTTP {exc.response.status_code}). Check `persome status`, then retry."
            )
        raise typer.Exit(1) from exc
    except (RuntimeError, ValueError, httpx.HTTPError) as exc:
        console.print(
            "[red]Could not authorize the local model viewer.[/red] "
            "Confirm the daemon is running with `persome status`, then retry."
        )
        raise typer.Exit(1) from exc

    data = payload.get("data") if isinstance(payload, dict) else None
    relative_url = data.get("bootstrap_url") if isinstance(data, dict) else None
    parsed = urlparse(relative_url) if isinstance(relative_url, str) else None
    try:
        query = parse_qs(parsed.query, strict_parsing=True) if parsed is not None else {}
    except ValueError:
        query = {}
    if (
        parsed is None
        or parsed.scheme
        or parsed.netloc
        or parsed.path != BROWSER_BOOTSTRAP_PATH
        or parsed.fragment
        or set(query) != {"nonce"}
        or len(query["nonce"]) != 1
    ):
        console.print("[red]The daemon returned an invalid browser capability.[/red]")
        raise typer.Exit(1)

    bootstrap_url = urljoin(f"{base_url}/", relative_url)
    if not webbrowser.open(bootstrap_url, new=2):
        console.print("[red]The system browser could not be opened.[/red]")
        raise typer.Exit(1)
    console.print("[green]Opened the authenticated local model viewer.[/green]")


@model_app.callback(invoke_without_command=True)
def model_default(ctx: typer.Context) -> None:
    """Open the authenticated model viewer when no subcommand is given."""
    if ctx.invoked_subcommand is None:
        model_open()


@writer_app.command("run")
def writer_run() -> None:
    """Reduce pending sessions and finish their personal-model stages."""
    cfg = _init()
    from .writer import agent

    result = agent.run(cfg)
    console.print(
        f"[bold]reduced={result.reduced} "
        f"classified={result.classified} "
        f"modeled={result.modeled} "
        f"written={len(result.written_ids)}[/bold]"
    )
    for s in result.summaries:
        console.print(f"  - {s}")


@app.command("capture-once")
def capture_once() -> None:
    """Perform one capture immediately (useful for testing)."""
    cfg = _init()
    from .capture import ax_capture, scheduler

    provider = ax_capture.create_provider(
        depth=cfg.capture.ax_depth, timeout=cfg.capture.ax_timeout_seconds
    )
    path = scheduler.capture_once(cfg.capture, provider)
    if path:
        console.print(f"[green]Wrote {path}[/green]")
    else:
        console.print("[red]Capture skipped or failed (check logs).[/red]")
        raise typer.Exit(1)


@app.command("rebuild-index")
def rebuild_index() -> None:
    """Rebuild the FTS retrieval projection from the current write authority's truth.

    Markdown authority replays ``memory/*.md``. Evomem authority projects
    canonical nodes while retaining direct Markdown event logs. Use
    ``evomem-restore-from-markdown`` only for canonical-store disaster recovery.
    """
    _init()
    with fts.cursor() as conn:
        files_count, entry_count = entries_mod.rebuild_index(conn)
        index_md.rebuild(conn)
    console.print(f"[green]Rebuilt: {files_count} files, {entry_count} entries.[/green]")


@app.command("vector-backfill")
def vector_backfill(
    limit: int = typer.Option(0, "--limit", help="Backfill at most N entries; zero means all."),
    embed: bool = typer.Option(
        False, "--embed", help="Run one embedding pass immediately after enqueueing."
    ),
) -> None:
    """Enqueue every live entry that lacks a dense retrieval vector."""
    cfg = _init()
    from . import vectors_tick

    enqueued = vectors_tick.backfill(cfg, limit=(limit or None))
    console.print(
        f"[green]Enqueued {enqueued} entr{'y' if enqueued == 1 else 'ies'} for embedding.[/green]"
    )
    if embed:
        if not cfg.search.hybrid_enabled:
            console.print("[yellow]--embed skipped: [search] hybrid_enabled is off.[/yellow]")
            return
        embedded, queued = vectors_tick.run_embed_once(cfg)
        console.print(f"[green]Embedded {embedded} this pass ({queued} still queued).[/green]")


@app.command("evomem-restore-from-markdown")
def evomem_restore_from_markdown(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Parse, map, and report without snapshotting or writing."
    ),
) -> None:
    """Lossily reconstruct canonical evomem nodes from Markdown projections."""
    _init()
    from .evomem import restore as restore_mod

    try:
        report = restore_mod.import_from_markdown(dry_run=dry_run)
    except restore_mod.RestoreError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    mode = " (dry-run)" if report.dry_run else ""
    console.print(
        f"Restore{mode}: {report.files} file(s) parsed"
        f" ({report.skipped_event_files} event-* skipped, Q2) → {report.nodes} node(s)."
    )
    if report.dry_run:
        return
    console.print(
        f"Retrieval projection replayed: {report.projection_files} file(s),"
        f" {report.projection_entries} entr(ies)."
    )
    if report.ok:
        console.print("[green]§3.3 self-check passed after restore.[/green]")
        console.print(
            "[yellow]Warning: this recovery is approximate. Timestamps have minute-level "
            "precision, and writes inside a projection-lag window cannot be recovered.[/yellow]"
        )
        return
    for v in report.violations:
        console.print(f"[red]integrity violation: {v.check}: {v.detail}[/red]")
    raise typer.Exit(1)


@app.command("evomem-backfill")
def evomem_backfill(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Parse, map, and compare chain heads without snapshotting or writing.",
    ),
) -> None:
    """Idempotently backfill canonical evomem nodes from Markdown and side tables."""
    _init()
    from .evomem import backfill as backfill_mod

    try:
        report = backfill_mod.run_backfill(dry_run=dry_run)
    except backfill_mod.BackfillError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    mode = " (dry-run)" if report.dry_run else ""
    console.print(
        f"Backfill{mode}: {report.files} files, {report.scanned_entries} entries scanned → "
        f"{report.backfilled_nodes} nodes, {report.skipped_event} event-* entries skipped (Q2)."
    )
    for edge in report.dangling_edges:
        console.print(f"[yellow]dangling #superseded-by edge dropped: {edge}[/yellow]")
    if report.ok:
        console.print("[green]Closing assertions passed: integrity + head-set equality.[/green]")
        if not dry_run:
            console.print(
                "Incremental shadow writes now keep evo_nodes current after each primary "
                "write. Rerun this command after a shadow_write_lag alert."
            )
        return
    for v in report.violations:
        console.print(f"[red]integrity violation: {v.check}: {v.detail}[/red]")
    if report.heads_only_evo:
        console.print(
            f"[red]active heads only in evo_nodes: {', '.join(report.heads_only_evo)}[/red]"
        )
    if report.heads_only_fts:
        console.print(
            f"[red]live heads only in entries (FTS projection): "
            f"{', '.join(report.heads_only_fts)}[/red]"
        )
    raise typer.Exit(1)


@app.command("evomem-project-markdown")
def evomem_project_markdown(
    out: str | None = typer.Option(
        None,
        "--out",
        help="Output directory; defaults to <root>/projection-md and rejects live memory/.",
    ),
    file: str | None = typer.Option(
        None,
        "--file",
        help="Project only one file name, for example project-x.md.",
    ),
    live: bool = typer.Option(
        False,
        "--live",
        help="Project all canonical files into live memory/. Requires evomem authority "
        "unless --force is also supplied.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="With --live, allow overwrite while Markdown is authoritative. Use only "
        "during an explicit authority rollback.",
    ),
) -> None:
    """Deterministically generate readable Markdown from canonical evomem nodes."""
    _init()
    from .evomem import inversion as inversion_mod
    from .store import projector as projector_mod

    if live:
        if not inversion_mod.evomem_active() and not force:
            console.print(
                "[red]write_authority=markdown; overwriting live memory/ is allowed only "
                "during an explicit rollback. Add --force after confirming.[/red]"
            )
            raise typer.Exit(1)
        with fts.cursor() as conn:
            names = inversion_mod.project_live_all(conn)
        misses = inversion_mod.miss_count()
        console.print(
            f"[green]Projected {len(names)} file(s) → {paths.memory_dir()}[/green]"
            + (
                f" [yellow]({misses} cumulative projection miss(es) — see logs)[/yellow]"
                if misses
                else ""
            )
        )
        return

    out_dir = Path(out) if out is not None else paths.root() / "projection-md"
    try:
        with fts.cursor() as conn:
            if file is not None:
                target = projector_mod.project_file(conn, file, out_dir=out_dir)
                console.print(f"[green]Projected {file} → {target}[/green]")
                return
            report = projector_mod.project_all(conn, out_dir=out_dir)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(
        f"Projected {len(report.files)} file(s), {report.nodes} node(s) → {report.out_dir}"
        + (
            f" ({report.skipped_unrouted} unrouted node(s) skipped)"
            if report.skipped_unrouted
            else ""
        )
    )


@app.command("evomem-import-markdown")
def evomem_import_markdown(
    file: str = typer.Argument(..., help="Projected file to import, for example project-x.md."),
) -> None:
    """Import safe manual additions from a projected Markdown file."""
    _init()
    from .evomem import inversion as inversion_mod

    try:
        with fts.cursor() as conn:
            report = inversion_mod.import_markdown_file(conn, file)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    if report.imported:
        console.print(f"[green]Imported {len(report.imported)} new entr(ies):[/green]")
        for eid in report.imported:
            console.print(f"  + {eid}")
    else:
        console.print("No new entries to import.")
    if report.reprojected:
        console.print(f"[green]Reprojected {report.file_name} to canonical form.[/green]")
    if report.conflicts:
        console.print(
            "[yellow]Manual review required; the file was preserved and the alert remains:[/yellow]"
        )
        for c in report.conflicts:
            console.print(f"  ! {c}")
        raise typer.Exit(2)


@app.command("rebuild-captures-index")
def rebuild_captures_index(
    merge: bool = typer.Option(
        False,
        "--merge",
        help="Upsert surviving buffer JSON without deleting older snapshot-backed rows.",
    ),
) -> None:
    """Reconcile captures_fts exactly from capture-buffer/*.json on disk.

    By default stale rows are cleared before every surviving JSON is indexed.
    ``--merge`` preserves existing rows and is the safe mode after restoring an
    older database snapshot whose historical captures may predate buffer
    retention.
    """
    import json

    _init()
    buf = paths.capture_buffer_dir()
    files = sorted(p for p in buf.iterdir() if p.is_file() and p.suffix == ".json")

    indexed = 0
    skipped = 0
    with fts.cursor() as conn:
        # Exact rebuild is reconciliation, not just an upsert pass. Recovery
        # merge intentionally preserves older snapshot rows whose source JSON
        # has already aged out of the bounded capture buffer.
        if not merge:
            conn.execute("DELETE FROM captures")
        for p in files:
            try:
                data = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                skipped += 1
                console.print(f"[yellow]skip {p.name}: {exc}[/yellow]")
                continue
            meta = data.get("window_meta") or {}
            focused = data.get("focused_element") or {}
            try:
                fts.insert_capture(
                    conn,
                    id=p.stem,
                    timestamp=data.get("timestamp", ""),
                    app_name=meta.get("app_name") or "",
                    bundle_id=meta.get("bundle_id") or "",
                    window_title=meta.get("title") or "",
                    focused_role=focused.get("role") or "",
                    focused_value=focused.get("value") or "",
                    visible_text=data.get("visible_text") or "",
                    url=data.get("url") or "",
                )
                indexed += 1
            except Exception as exc:  # noqa: BLE001
                skipped += 1
                console.print(f"[yellow]skip {p.name}: {exc}[/yellow]")
            if indexed % 200 == 0 and indexed > 0:
                console.print(f"  indexed {indexed} / {len(files)}…")

    console.print(
        f"[green]Captures index {'merged' if merge else 'rebuilt'}: "
        f"{indexed} indexed, {skipped} skipped "
        f"(of {len(files)} files).[/green]"
    )


@app.command()
def config() -> None:
    """Print the resolved config path and contents."""
    _init()
    p = paths.config_file()
    console.print(f"[bold]{p}[/bold]")
    console.print(p.read_text())


clean_app = typer.Typer(help="Delete past data. Destructive — use with care.")
app.add_typer(clean_app, name="clean")


def _confirm(prompt: str, yes: bool) -> bool:
    if yes:
        return True
    return typer.confirm(prompt, default=False)


def _require_stopped_for_clean() -> None:
    pid = _read_pid()
    if pid:
        console.print(
            f"[red]Refusing to clean while the daemon is running (pid {pid}). "
            "Run `persome stop` first so no writer can retain or recreate deleted data.[/red]"
        )
        raise typer.Exit(1)


def _quarantined_index_db_artifacts() -> tuple[Path, ...]:
    """Return integrity-quarantine copies that may retain personal DB pages."""
    return tuple(sorted(paths.root().glob(f"{paths.index_db().name}.corrupt.*")))


def _quarantined_index_db_mains() -> tuple[Path, ...]:
    sidecar_suffixes = ("-wal", "-shm", "-journal", ".wal", ".shm", ".journal")
    return tuple(
        artifact
        for artifact in _quarantined_index_db_artifacts()
        if not artifact.name.endswith(sidecar_suffixes)
    )


def _remove_quarantined_index_sidecars() -> None:
    mains = set(_quarantined_index_db_mains())
    for artifact in _quarantined_index_db_artifacts():
        if artifact not in mains:
            artifact.unlink(missing_ok=True)


def _private_atomic_crash_artifacts(*targets: Path) -> tuple[Path, ...]:
    """Find known private temp inodes that a SIGKILL may strand."""
    found: set[Path] = set()
    for target in targets:
        found.update(target.parent.glob(f".{target.name}.*"))
        # Also cover the pre-helper spelling for dot-prefixed state files.
        found.update(target.parent.glob(f".{target.name.lstrip('.')}.*"))
    return tuple(sorted(found))


def _clean_captures() -> tuple[int, int]:
    from .evomem import backup as evo_backup

    # Recovery copies are part of the deletion boundary too.
    evo_backup.scrub_snapshots(("captures",))
    evo_backup.scrub_database_copies(("captures",), _quarantined_index_db_mains())
    _remove_quarantined_index_sidecars()
    buf = paths.capture_buffer_dir()
    with fts.cursor() as conn:
        rows = int(conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0])
        conn.execute("DELETE FROM captures")
        fts.purge_deleted_content(conn)
    files = 0
    if buf.exists():
        for p in tuple(buf.iterdir()):
            files += _remove_path(p)
    return files, rows


def _clean_timeline() -> int:
    from .evomem import backup as evo_backup

    evo_backup.scrub_snapshots(("timeline_blocks",))
    evo_backup.scrub_database_copies(("timeline_blocks",), _quarantined_index_db_mains())
    _remove_quarantined_index_sidecars()
    with fts.cursor() as conn:
        n: int = conn.execute("SELECT COUNT(*) FROM timeline_blocks").fetchone()[0]
        conn.execute("DELETE FROM timeline_blocks")
        fts.purge_deleted_content(conn)
    return n


_MODEL_TABLES = (
    "memory_deltas",
    "memory_contradictions",
    "relation_edges",
    "schema_faces",
    "cross_domain_probe_state",
    "evo_nodes",
    "projection_state",
    "entry_metadata",
    "entry_retrieval_stats",
    "entry_temporal",
    "entry_vectors",
    "vector_queue",
    "entries",
    "files",
)


def _remove_path(path: Path) -> int:
    """Remove one data artifact and return the number of files it contained."""
    if path.is_symlink():
        path.unlink(missing_ok=True)
        return 1
    if not path.exists():
        return 0
    if path.is_dir():
        count = sum(1 for item in path.rglob("*") if item.is_file())
        shutil.rmtree(path)
        return count
    path.unlink()
    return 1


def _clean_memory() -> tuple[int, int, int, int]:
    """Delete all personal-model projections, canonical state, and exports."""
    mem = paths.memory_dir()
    files = _remove_path(mem)
    paths.ensure_private_dir(mem)
    with fts.cursor() as conn:
        entries = int(conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0])
        model_rows = 0
        for table in _MODEL_TABLES:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name=?",
                (table,),
            ).fetchone()
            if exists is None:
                continue
            if table not in {"entries", "files"}:
                model_rows += int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            conn.execute(f"DELETE FROM {table}")
        fts.purge_deleted_content(conn)
    artifacts = sum(
        _remove_path(path)
        for path in (
            paths.exports_dir(),
            paths.backup_dir(),
            paths.root() / "projection-md",
            paths.model_build_manifest(),
            paths.model_build_lock(),
            paths.session_model_lock(),
            paths.integrity_recovery_marker(),
            paths.integrity_recovery_pending(),
            paths.integrity_config_recovery_pending(),
        )
    )
    artifacts += sum(
        _remove_path(path)
        for path in _private_atomic_crash_artifacts(
            paths.model_build_manifest(),
            paths.integrity_recovery_marker(),
            paths.integrity_recovery_pending(),
            paths.integrity_config_recovery_pending(),
        )
    )
    # Integrity-quarantine copies live beside the active DB, outside backup/.
    # They are not trustworthy enough to preserve selectively once the user
    # explicitly erases the model, so remove every database page/journal copy.
    artifacts += sum(_remove_path(path) for path in _quarantined_index_db_artifacts())
    return files, entries, model_rows, artifacts


@clean_app.command("captures")
def clean_captures(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Delete all files in the capture buffer."""
    _require_stopped_for_clean()
    _init()
    buf = paths.capture_buffer_dir()
    count = sum(1 for p in buf.iterdir() if p.suffix == ".json") if buf.exists() else 0
    console.print(f"About to delete {count} capture file(s) under {buf}")
    if not _confirm("Proceed?", yes):
        console.print("[yellow]Aborted.[/yellow]")
        raise typer.Exit(1)
    files, rows = _clean_captures()
    console.print(
        f"[green]Deleted {files} capture file(s) and {rows} indexed capture row(s).[/green]"
    )


@clean_app.command("timeline")
def clean_timeline(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Delete all timeline blocks (short-window activity summaries)."""
    _require_stopped_for_clean()
    _init()
    with fts.cursor() as conn:
        count = conn.execute("SELECT COUNT(*) FROM timeline_blocks").fetchone()[0]
    console.print(f"About to delete {count} timeline block(s).")
    if not _confirm("Proceed?", yes):
        console.print("[yellow]Aborted.[/yellow]")
        raise typer.Exit(1)
    n = _clean_timeline()
    console.print(f"[green]Deleted {n} timeline block(s).[/green]")


@clean_app.command("memory")
def clean_memory(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Delete all memory files and reset the FTS index."""
    _require_stopped_for_clean()
    _init()
    mem = paths.memory_dir()
    memory_count = sum(1 for p in mem.rglob("*") if p.is_file()) if mem.exists() else 0
    with fts.cursor() as conn:
        entry_count = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        file_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        model_count = sum(
            int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in _MODEL_TABLES
            if table not in {"entries", "files"}
        )
    console.print(
        f"About to delete {memory_count} memory file(s) under {mem} "
        f"and reset {entry_count} entries / {file_count} files / "
        f"{model_count} canonical model row(s), plus exports and backups."
    )
    if not _confirm("Proceed?", yes):
        console.print("[yellow]Aborted.[/yellow]")
        raise typer.Exit(1)
    files, entries, model_rows, artifacts = _clean_memory()
    console.print(
        f"[green]Deleted {files} memory file(s), {entries} index entries, "
        f"{model_rows} canonical model row(s), and {artifacts} export/backup artifact(s).[/green]"
    )


@clean_app.command("all")
def clean_all(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Delete all personal data while keeping config, env, and the installed venv."""
    _require_stopped_for_clean()
    _init()
    buf = paths.capture_buffer_dir()
    mem = paths.memory_dir()
    capture_count = sum(1 for p in buf.iterdir() if p.suffix == ".json") if buf.exists() else 0
    memory_count = sum(1 for p in mem.rglob("*") if p.is_file()) if mem.exists() else 0
    with fts.cursor() as conn:
        entry_count = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        tlb_count = conn.execute("SELECT COUNT(*) FROM timeline_blocks").fetchone()[0]

    console.print(
        "[bold red]This will delete:[/bold red]\n"
        f"  - {capture_count} capture file(s)\n"
        f"  - {tlb_count} timeline block(s)\n"
        f"  - {memory_count} memory file(s) and {entry_count} index entries\n"
        "  - canonical model, exports, backups, and logs\n"
        "  - legacy Chat-era chat-history/ and skills/ trees (if present)\n"
        "[bold]Config, env, and the installed venv are kept.[/bold]"
    )
    if not _confirm("Proceed with full wipe?", yes):
        console.print("[yellow]Aborted.[/yellow]")
        raise typer.Exit(1)

    known_personal_data = (
        paths.capture_buffer_dir(),
        paths.memory_dir(),
        paths.logs_dir(),
        paths.exports_dir(),
        paths.backup_dir(),
        paths.root() / "projection-md",
        # Legacy Chat-era data written by versions that still shipped the Chat
        # feature; keep purging it so a full wipe stays a full wipe.
        paths.root() / "chat-history",
        paths.root() / "skills",
        paths.index_db(),
        paths.index_db().with_name(f"{paths.index_db().name}-wal"),
        paths.index_db().with_name(f"{paths.index_db().name}-shm"),
        paths.index_db().with_name(f"{paths.index_db().name}-journal"),
        paths.writer_state(),
        paths.model_build_manifest(),
        paths.model_build_lock(),
        paths.session_model_lock(),
        paths.paused_flag(),
        paths.integrity_recovery_marker(),
        paths.integrity_recovery_pending(),
        paths.integrity_config_recovery_pending(),
        paths.pid_file(),
    )
    quarantined_personal_data = tuple(paths.root().glob("*.corrupt.*"))
    atomic_crash_data = _private_atomic_crash_artifacts(
        paths.model_build_manifest(),
        paths.integrity_recovery_marker(),
        paths.integrity_recovery_pending(),
        paths.integrity_config_recovery_pending(),
        paths.pid_file(),
        paths.paused_flag(),
        paths.writer_state(),
    )
    removed = sum(
        _remove_path(path)
        for path in (*known_personal_data, *quarantined_personal_data, *atomic_crash_data)
    )
    console.print(
        f"[green]Done. Removed {removed} personal data artifact(s). "
        "Config, env, and the installed venv were kept.[/green]"
    )


# ─── debug subcommands ────────────────────────────────────────────────────

debug_app = typer.Typer(help="Diagnostics for in-flight pipeline stages.")
app.add_typer(debug_app, name="debug")


@debug_app.command("chat-captures")
def debug_chat_captures(
    app: str = typer.Option(
        ...,
        "--app",
        "-a",
        help="App name to query (for example, 'Feishu' or 'WeChat'). Must match capture data.",
    ),
    start: str = typer.Option(
        ...,
        "--start",
        "-s",
        help="ISO-8601 start timestamp (e.g. '2026-05-22T09:00:00').",
    ),
    end: str = typer.Option(
        ...,
        "--end",
        "-e",
        help="ISO-8601 end timestamp (e.g. '2026-05-22T12:00:00').",
    ),
    max_bytes: int = typer.Option(
        12_000,
        "--max-bytes",
        help="Maximum output bytes (most recent content kept on truncation).",
    ),
) -> None:
    """Inspect reconstructed chat output against locally captured data.

    Queries the live index.db and prints the reconstructed conversation with
    scroll-gap markers, so you can verify what the classifier will see.
    """
    _init()
    from .writer.chat_extractor import extract_chat_messages

    with fts.cursor() as conn:
        text, snapshot_count, gap_count = extract_chat_messages(conn, app, start, end, max_bytes)

    if not text:
        console.print(
            f"[yellow]No captures found for app={app!r} between {start} and {end}.[/yellow]"
        )
        return

    console.print(
        f"[bold]Chat captures:[/bold] app=[cyan]{app}[/cyan]  "
        f"snapshots=[green]{snapshot_count}[/green]  "
        f"gaps=[{'red' if gap_count else 'green'}]{gap_count}[/{'red' if gap_count else 'green'}]"
    )
    console.print(f"[dim]window: {start} → {end}[/dim]\n")
    console.print(text)


if __name__ == "__main__":
    app()
