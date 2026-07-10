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
import json
import shutil
import signal
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import __version__, integrity, paths
from . import config as config_mod
from . import env_file as env_file_mod
from . import logger as logger_mod
from .store import entries as entries_mod
from .store import fts, index_md

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Local-first screen-context memory and personal modeling for macOS.",
)
console = Console()


def _init() -> config_mod.Config:
    paths.ensure_dirs()
    # Logger first so the integrity check's JSON-line output lands in daemon.log.
    logger_mod.setup(console=False)
    # Quarantine a corrupt DB / config before anything tries to open them (#202).
    # Runs on every CLI entry through _init (start / status / …) so a damaged
    # file is recovered the moment the user next touches the daemon.
    integrity.check_and_recover()
    created = config_mod.write_default_if_missing()
    if created:
        console.print(f"[green]Created default config at {paths.config_file()}[/green]")
    return config_mod.load()


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_pid() -> int | None:
    try:
        pid = int(paths.pid_file().read_text().strip())
    except (FileNotFoundError, ValueError):
        return None
    return pid if _is_pid_alive(pid) else None


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
        # `captures.timestamp` is stored aware-local (e.g. ...T18:32:48+08:00);
        # SQLite's `datetime('now')` is UTC with a space separator, so a SQL
        # `>= datetime('now', ?)` lexicographic compare is dominated by the
        # `T`-vs-space separator and degrades to "since the start of today"
        # (#586 class). Compute the cutoff in the column's own format and bind it.
        cutoff = (datetime.now().astimezone() - timedelta(minutes=int(hours * 60))).isoformat()
        rows = conn.execute(
            "SELECT timestamp FROM captures WHERE timestamp >= ? ORDER BY timestamp",
            (cutoff,),
        ).fetchall()
    if not rows:
        return 0, None
    timestamps = [datetime.fromisoformat(r[0]) for r in rows]
    if len(timestamps) < 2:
        return len(timestamps), None
    gaps = [(timestamps[i + 1] - timestamps[i]).total_seconds() for i in range(len(timestamps) - 1)]
    return len(timestamps), max(gaps)


def _install_source() -> str:
    """Return the editable-install source path recorded in direct_url.json."""
    try:
        import importlib.metadata

        dist = importlib.metadata.distribution("persome-core")
        raw = dist.read_text("direct_url.json")
        if raw:
            data = json.loads(raw)
            url = str(data.get("url", ""))
            if url.startswith("file://"):
                return url[7:]
            return url
    except Exception:  # noqa: BLE001
        pass
    return "unknown"


def _last_capture_info() -> tuple[str | None, str | None]:
    """Return ``(timestamp, app_name)`` of the most recent capture buffer file.

    Returns ``(None, None)`` when the buffer directory is empty or missing.
    """
    buf = paths.capture_buffer_dir()
    if not buf.exists():
        return None, None
    json_files = sorted(p for p in buf.iterdir() if p.suffix == ".json")
    if not json_files:
        return None, None
    try:
        data = json.loads(json_files[-1].read_bytes())
        ts = data.get("timestamp")
        meta = data.get("window_meta") or {}
        app = meta.get("app_name")
        return ts, app
    except (OSError, ValueError):
        return json_files[-1].stem, None


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


@app.command()
def start(
    foreground: bool = typer.Option(False, "--foreground", "-f", help="Run in this terminal."),
    capture_only: bool = typer.Option(False, "--capture-only", help="Skip the writer loop."),
) -> None:
    """Start the Persome daemon."""
    cfg = _init()
    # Source the owner-only env file before any fork so the daemon and every
    # subsystem reading os.environ see the same values regardless of launcher.
    env_file_mod.load_env_file(paths.env_file())
    pid = _read_pid()
    if pid:
        console.print(f"[yellow]Already running (pid {pid})[/yellow]")
        raise typer.Exit(1)

    from . import daemon

    if foreground:
        console.print("[bold]Persome starting in foreground[/bold] — Ctrl+C to stop.")
        _watch_parent_death()  # exit if the Persome app that spawned us (--foreground child) dies
        daemon.run(cfg, capture_only=capture_only)
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

    # Background: double-fork
    if os.fork() != 0:
        console.print("[green]Persome started in background.[/green]")
        console.print(f"Logs: {paths.logs_dir()}")
        return
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
    os._exit(0)


@app.command()
def stop(timeout: int = typer.Option(10, help="Seconds to wait for the daemon to exit.")) -> None:
    """Stop the daemon and wait for it to fully exit."""
    import time

    _init()
    pid = _read_pid()
    if not pid:
        console.print("[yellow]Daemon not running.[/yellow]")
        raise typer.Exit(1)
    os.kill(pid, signal.SIGTERM)
    console.print(f"[green]Sent SIGTERM to pid {pid}.[/green]")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _is_pid_alive(pid):
            console.print("[green]Daemon stopped.[/green]")
            return
        time.sleep(0.2)
    console.print(
        f"[yellow]Daemon (pid {pid}) did not exit within {timeout}s — it may still be running.[/yellow]"
    )


@app.command()
def pause() -> None:
    """Pause capture (daemon stays up but skips captures)."""
    paths.ensure_dirs()
    paths.paused_flag().write_text(datetime.now().isoformat())
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
    # Source the env file (LLM secrets SoT) before the per-stage model-health
    # probe below. ping_stage builds the Anthropic client with
    # provider_api_key("anthropic") == os.environ["ANTHROPIC_API_KEY"]; without
    # this load the `status` process has no creds, so every stage falsely reports
    # an auth error ("Could not resolve authentication method") even when the
    # daemon — which loads env before forking in start() — is perfectly healthy.
    env_file_mod.load_env_file(paths.env_file())
    pid = _read_pid()
    paused = paths.paused_flag().exists()

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

    buf = paths.capture_buffer_dir()
    if buf.exists():
        bufs = sorted(p for p in buf.iterdir() if p.suffix == ".json")
        last = bufs[-1].name if bufs else "(none)"
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
        tlb_row = conn.execute("SELECT COUNT(*), MAX(end_time) FROM timeline_blocks").fetchone()
        tlb_count = tlb_row[0] if tlb_row else 0
        tlb_last = tlb_row[1] if tlb_row and tlb_row[1] else "(none)"
        table.add_row("Timeline", f"{tlb_count} blocks, last end: {tlb_last}")

    stages = ("timeline", "reducer", "classifier", "compact")
    ping_results = _ping_stages(cfg, stages)
    for stage in stages:
        m = cfg.model_for(stage)
        ping = _format_ping(ping_results.get(stage))
        table.add_row(f"Model ({stage})", f"{m.model}   {ping}")

    console.print(table)


@app.command()
def doctor() -> None:
    """Self-check a bring-your-own-key install (offline; zero LLM calls).

    Prints one ✓/✗/⚠ line per prerequisite — env file present + private (0600),
    ANTHROPIC_API_KEY configured, base URL reachable (HEAD, warn-only), Swift
    capture helpers compiled, macOS Accessibility trust, data root writable,
    daemon port available. Exits 1 if any check FAILS; warnings never fail.
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


def _ping_stages(cfg: config_mod.Config, stages: tuple[str, ...]) -> dict:
    """Probe each stage's configured model, deduping identical configs.

    Returns a dict keyed by stage name -> PingResult. Pings run in parallel
    so a single hung provider can't stretch the wait past the per-call
    timeout.
    """
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import replace

    from .writer.llm import PingResult, ping_stage

    # Dedup by (model, resolved base_url, resolved api key) — common case is
    # one model for all four stages, which should hit the network once.
    dedup: dict[tuple[str, str, str], list[str]] = {}
    for stage in stages:
        m = cfg.model_for(stage)
        provider = config_mod.infer_provider(m.model)
        base_url = m.base_url or (config_mod.provider_base_url(provider) or "")
        api_key = config_mod.provider_api_key(provider) or ""
        key = (m.model, base_url, api_key)
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
    _init()

    # env file (OPENAI_* embeddings creds live there) exactly like `persome start`,
    # else the stdio server's read path can never activate hybrid dense and an
    # LLM client silently gets a weaker memory than the in-daemon server.
    env_file_mod.load_env_file(paths.env_file())
    from .mcp import server as mcp_server

    mcp_server.run_stdio()


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
        Path(json_out).write_text(
            _json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
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
        Path(json_out).write_text(
            _json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
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
    from .config import load as load_cfg
    from .store import fts
    from .writer import root_synthesis as rs

    cfg = load_cfg()
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
    from .config import load as load_cfg
    from .store import fts
    from .writer import correct as correct_mod

    cfg = load_cfg()
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
        Path(json_out).write_text(
            _json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
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
        Path(json_out).write_text(
            _json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2),
            encoding="utf-8",
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

    llm_call = None
    if llm:
        from . import config as config_mod
        from . import paths as paths_mod
        from .writer import llm as llm_mod

        cfg = config_mod.load(paths_mod.config_file())

        def llm_call(messages):  # noqa: F811
            return llm_mod.call_llm(cfg, "relation_extractor", messages=messages, json_mode=True)

    with fts.cursor() as conn:
        report = audit_mod.run_audit(conn, n=n, seed=seed or None, llm_call=llm_call)
    _Path(json_out).write_text(_json.dumps(report, ensure_ascii=False, indent=2))
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
        Path(json_out).write_text(
            _json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )


install_app = typer.Typer(help="Register the MCP server with common LLM clients.")
app.add_typer(install_app, name="install")

uninstall_app = typer.Typer(help="Remove Persome's MCP entry from LLM clients.")
app.add_typer(uninstall_app, name="uninstall")

launchagent_app = typer.Typer(
    help="Manage the macOS LaunchAgent so launchd owns the daemon lifecycle."
)
app.add_typer(launchagent_app, name="launchagent")


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
    """Add (or refresh) Persome's entry in Claude Code's MCP config.

    Always installs the current URL/transport — if an entry named ``name`` already
    exists at the given scope, it is removed and re-registered.
    """
    cfg = _init()
    from .mcp import server as mcp_server

    if cfg.mcp.transport not in ("sse", "streamable-http"):
        console.print(
            f"[red]MCP transport is {cfg.mcp.transport!r}; install requires sse or streamable-http.[/red]"
        )
        raise typer.Exit(1)
    if not cfg.mcp.auto_start:
        console.print(
            "[yellow]Warning: mcp.auto_start is false — the daemon won't host the server.[/yellow]"
        )

    claude_bin = shutil.which("claude")
    if not claude_bin:
        console.print(
            "[red]`claude` CLI not found on PATH.[/red] "
            "Install Claude Code first, or edit ~/.claude.json manually."
        )
        raise typer.Exit(1)

    url = mcp_server.endpoint_url(cfg)
    transport_flag = "sse" if cfg.mcp.transport == "sse" else "http"

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
        "--transport",
        transport_flag,
        name,
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        console.print(f"[red]claude mcp add failed:[/red]\n{result.stderr or result.stdout}")
        raise typer.Exit(result.returncode)

    verb = "Updated" if replaced else "Registered"
    console.print(f"[green]{verb} {name!r} in Claude Code ({scope} scope).[/green]")
    console.print(f"  URL: {url}")
    console.print("  Make sure the daemon is running (`persome start`) so the server is reachable.")


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
    persome_bin = shutil.which("persome")
    if not persome_bin:
        console.print(
            "[red]`persome` not found on PATH.[/red]\n"
            "Install it globally first with [cyan]uv tool install .[/cyan] "
            "(from the repo), then rerun this command."
        )
        raise typer.Exit(1)

    cfg_path = _claude_desktop_config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)

    data = _load_claude_desktop_config(cfg_path)
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        console.print(f"[red]`mcpServers` in {cfg_path} is not an object.[/red]")
        raise typer.Exit(1)

    replaced = name in servers
    servers[name] = {
        "command": persome_bin,
        "args": ["mcp"],
    }

    cfg_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    verb = "Updated" if replaced else "Registered"
    console.print(f"[green]{verb} {name!r} in Claude Desktop config.[/green]")
    console.print(f"  file: {cfg_path}")
    console.print(f"  command: {persome_bin} mcp")
    _restart_reminder("pick up the new entry")


@install_app.command("codex")
def install_codex(
    name: str = typer.Option("persome", help="MCP server name shown to the client."),
) -> None:
    """Add (or refresh) Persome's entry in Codex CLI's MCP config.

    Codex CLI supports streamable-HTTP MCP servers via ``codex mcp add <name> --url <URL>``,
    so we register the daemon's always-on endpoint. The CLI and the IDE extension
    share this config, so a single install covers both clients.

    Every invocation is idempotent — if an entry named ``name`` already exists,
    it is removed and re-registered with the current URL.
    """
    cfg = _init()
    from .mcp import server as mcp_server

    if cfg.mcp.transport not in ("sse", "streamable-http"):
        console.print(
            f"[red]MCP transport is {cfg.mcp.transport!r}; install requires sse or streamable-http.[/red]"
        )
        raise typer.Exit(1)
    if not cfg.mcp.auto_start:
        console.print(
            "[yellow]Warning: mcp.auto_start is false — the daemon won't host the server.[/yellow]"
        )

    codex_bin = shutil.which("codex")
    if not codex_bin:
        console.print(
            "[red]`codex` CLI not found on PATH.[/red] "
            "Install Codex first (https://github.com/openai/codex), "
            "or edit ~/.codex/config.toml manually."
        )
        raise typer.Exit(1)

    url = mcp_server.endpoint_url(cfg)

    remove = subprocess.run(
        [codex_bin, "mcp", "remove", name],
        capture_output=True,
        text=True,
        check=False,
    )
    replaced = remove.returncode == 0

    cmd = [codex_bin, "mcp", "add", name, "--url", url]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        console.print(f"[red]codex mcp add failed:[/red]\n{result.stderr or result.stdout}")
        raise typer.Exit(result.returncode)

    verb = "Updated" if replaced else "Registered"
    console.print(f"[green]{verb} {name!r} in Codex CLI.[/green]")
    console.print(f"  URL: {url}")
    console.print("  Make sure the daemon is running (`persome start`) so the server is reachable.")


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
    """Add (or refresh) Persome's entry in opencode's MCP config.

    opencode (https://opencode.ai) reads ``~/.config/opencode/opencode.json``
    and supports remote streamable-HTTP MCP servers natively, so we register
    the daemon's always-on endpoint.

    Every invocation is idempotent — an existing entry named ``name`` is
    overwritten with the current URL; other `mcp` entries are preserved.
    """
    cfg = _init()
    from .mcp import server as mcp_server

    if cfg.mcp.transport not in ("sse", "streamable-http"):
        console.print(
            f"[red]MCP transport is {cfg.mcp.transport!r}; install requires sse or streamable-http.[/red]"
        )
        raise typer.Exit(1)
    if not cfg.mcp.auto_start:
        console.print(
            "[yellow]Warning: mcp.auto_start is false — the daemon won't host the server.[/yellow]"
        )

    cfg_path = _opencode_config_path()
    jsonc_path = cfg_path.with_suffix(".jsonc")
    if jsonc_path.exists():
        url = mcp_server.endpoint_url(cfg)
        console.print(
            f"[red]Found {jsonc_path} — can't safely edit JSONC (comments would be lost).[/red]\n"
            "Add this entry under the `mcp` key manually:\n"
            f'  "{name}": {{"type": "remote", "url": "{url}", "enabled": true}}'
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

    url = mcp_server.endpoint_url(cfg)
    replaced = name in servers
    servers[name] = {
        "type": "remote",
        "url": url,
        "enabled": True,
    }

    cfg_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    verb = "Updated" if replaced else "Registered"
    console.print(f"[green]{verb} {name!r} in opencode config.[/green]")
    console.print(f"  URL: {url}")
    console.print("  Make sure the daemon is running (`persome start`) so the server is reachable.")


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

        if cfg.mcp.transport not in ("sse", "streamable-http"):
            console.print(
                f"[red]--http requires mcp.transport to be sse or streamable-http, "
                f"got {cfg.mcp.transport!r}.[/red]"
            )
            raise typer.Exit(1)
        url = mcp_server.endpoint_url(cfg)
        transport_label = "sse" if cfg.mcp.transport == "sse" else "http"
        entry: dict[str, object] = {"url": url, "transport": transport_label}
        summary = f"{transport_label} → {url}"
    else:
        persome_bin = shutil.which("persome") or "persome"
        entry = {"command": persome_bin, "args": ["mcp"]}
        summary = f"stdio → {persome_bin} mcp"

    payload = {"mcpServers": {name: entry}}
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")

    console.print(f"[green]Wrote {out_path}[/green]")
    console.print(f"  server: {name} ({summary})")
    console.print(
        "[dim]Point your agent framework at this file, or merge `mcpServers` "
        "into its existing MCP config.[/dim]"
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
    cfg_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")

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
    cfg_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")

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


@model_app.command("build")
def model_build(
    wait_seconds: float = typer.Option(
        30.0, "--wait-seconds", min=0.0, help="Seconds to wait for another build."
    ),
    no_wait: bool = typer.Option(False, "--no-wait", help="Return busy immediately."),
) -> None:
    """Run the shared one-shot Point/Line/Face/Volume/Root build."""
    from .model import ModelBuildBusy, run_model_build

    cfg = _init()
    try:
        result = run_model_build(cfg, wait_seconds=0.0 if no_wait else wait_seconds)
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
    console.print(f"manifest: {result.manifest_path}")


@model_app.command("export")
def model_export(
    out: str = typer.Option("", "--out", help="Output JSON path (default: root exports dir)."),
    raw: bool = typer.Option(False, "--raw", help="Include unredacted local text."),
) -> None:
    """Export the current versioned model snapshot; redacted by default."""
    from .model import export_snapshot, load_last_manifest

    _init()
    if raw:
        console.print("[yellow]warning: --raw may contain sensitive personal data[/yellow]")
    target = Path(out).expanduser() if out else None
    with fts.cursor() as conn:
        path = export_snapshot(
            conn,
            out_path=target,
            redact=not raw,
            build_metadata=load_last_manifest(),
        )
    console.print(f"model snapshot: {path}")


@model_app.command("status")
def model_status_cmd() -> None:
    """Show live model readiness, geometry counts, and the last build id."""
    from .model import load_last_manifest, model_status

    _init()
    with fts.cursor() as conn:
        status = model_status(conn)
    last = load_last_manifest()
    counts = status["stats"]
    console.print(
        f"[bold]model: {'ready' if status['ready'] else 'not ready'}[/bold]  "
        f"points={counts['points']} lines="
        f"{counts['evolution_lines'] + counts['relation_lines']} "
        f"faces={counts['faces']} volumes={counts['volumes']} roots={counts['roots']}"
    )
    if status["issues"]:
        console.print(f"issues: {', '.join(status['issues'])}")
    console.print(f"last build: {last.get('build_id') if last else 'none'}")


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
def rebuild_captures_index() -> None:
    """Backfill captures_fts from capture-buffer/*.json on disk.

    Re-runnable: existing rows are upserted via INSERT OR REPLACE, so this
    is safe to invoke any time the captures index has fallen out of sync
    (e.g. fresh upgrade onto a populated buffer, or an FTS write the
    capture worker logged but didn't commit).
    """
    import json

    _init()
    buf = paths.capture_buffer_dir()
    if not buf.exists():
        console.print("[yellow]No capture-buffer directory; nothing to rebuild.[/yellow]")
        return

    files = sorted(p for p in buf.iterdir() if p.is_file() and p.suffix == ".json")
    if not files:
        console.print("[yellow]capture-buffer is empty; nothing to rebuild.[/yellow]")
        return

    indexed = 0
    skipped = 0
    with fts.cursor() as conn:
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
        f"[green]Captures index rebuilt: {indexed} indexed, {skipped} skipped "
        f"(of {len(files)} files).[/green]"
    )


@app.command()
def config() -> None:
    """Print the resolved config path and contents."""
    _init()
    p = paths.config_file()
    console.print(f"[bold]{p}[/bold]")
    console.print(p.read_text())


@app.command()
def chat() -> None:
    """Interactive chat with memory-aware tool calling."""
    cfg = _init()
    from .chat import run_chat_sync

    run_chat_sync(cfg)


clean_app = typer.Typer(help="Delete past data. Destructive — use with care.")
app.add_typer(clean_app, name="clean")


def _confirm(prompt: str, yes: bool) -> bool:
    if yes:
        return True
    return typer.confirm(prompt, default=False)


def _warn_if_running() -> None:
    pid = _read_pid()
    if pid:
        console.print(
            f"[yellow]Warning: daemon is running (pid {pid}). "
            "Consider `persome stop` first — new data may arrive mid-clean.[/yellow]"
        )


def _clean_captures() -> int:
    buf = paths.capture_buffer_dir()
    if not buf.exists():
        return 0
    n = 0
    for p in buf.iterdir():
        if p.suffix == ".json" and p.is_file():
            p.unlink()
            n += 1
    return n


def _clean_timeline() -> int:
    with fts.cursor() as conn:
        n: int = conn.execute("SELECT COUNT(*) FROM timeline_blocks").fetchone()[0]
        conn.execute("DELETE FROM timeline_blocks")
    return n


def _clean_memory() -> tuple[int, int]:
    """Delete memory Markdown files + reset entries/files tables. Returns (files, entries)."""
    mem = paths.memory_dir()
    files = 0
    if mem.exists():
        for p in mem.rglob("*.md"):
            if p.is_file():
                p.unlink()
                files += 1
    with fts.cursor() as conn:
        entries = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        conn.execute("DELETE FROM entries")
        conn.execute("DELETE FROM files")
    return files, entries


def _clean_writer_state() -> bool:
    p = paths.writer_state()
    if p.exists():
        p.unlink()
        return True
    return False


@clean_app.command("captures")
def clean_captures(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Delete all files in the capture buffer."""
    _init()
    buf = paths.capture_buffer_dir()
    count = sum(1 for p in buf.iterdir() if p.suffix == ".json") if buf.exists() else 0
    console.print(f"About to delete {count} capture file(s) under {buf}")
    _warn_if_running()
    if not _confirm("Proceed?", yes):
        console.print("[yellow]Aborted.[/yellow]")
        raise typer.Exit(1)
    n = _clean_captures()
    console.print(f"[green]Deleted {n} capture file(s).[/green]")


@clean_app.command("timeline")
def clean_timeline(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Delete all timeline blocks (short-window activity summaries)."""
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
    """Delete all memory Markdown files and reset the FTS index."""
    _init()
    mem = paths.memory_dir()
    md_count = sum(1 for _ in mem.rglob("*.md")) if mem.exists() else 0
    with fts.cursor() as conn:
        entry_count = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        file_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    console.print(
        f"About to delete {md_count} Markdown file(s) under {mem} "
        f"and reset {entry_count} entries / {file_count} files in the index."
    )
    _warn_if_running()
    if not _confirm("Proceed?", yes):
        console.print("[yellow]Aborted.[/yellow]")
        raise typer.Exit(1)
    files, entries = _clean_memory()
    console.print(
        f"[green]Deleted {files} Markdown file(s); cleared {entries} index entries.[/green]"
    )


@clean_app.command("all")
def clean_all(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Delete captures, timeline blocks, memory, and writer state. Config is kept."""
    _init()
    buf = paths.capture_buffer_dir()
    mem = paths.memory_dir()
    capture_count = sum(1 for p in buf.iterdir() if p.suffix == ".json") if buf.exists() else 0
    md_count = sum(1 for _ in mem.rglob("*.md")) if mem.exists() else 0
    with fts.cursor() as conn:
        entry_count = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        tlb_count = conn.execute("SELECT COUNT(*) FROM timeline_blocks").fetchone()[0]

    console.print(
        "[bold red]This will delete:[/bold red]\n"
        f"  - {capture_count} capture file(s)\n"
        f"  - {tlb_count} timeline block(s)\n"
        f"  - {md_count} memory Markdown file(s) and {entry_count} index entries\n"
        f"  - writer state\n"
        "[bold]Config ({}) is kept.[/bold]".format(paths.config_file())
    )
    _warn_if_running()
    if not _confirm("Proceed with full wipe?", yes):
        console.print("[yellow]Aborted.[/yellow]")
        raise typer.Exit(1)

    c = _clean_captures()
    t = _clean_timeline()
    f, e = _clean_memory()
    s = _clean_writer_state()
    console.print(
        f"[green]Done. Removed {c} captures, {t} timeline blocks, "
        f"{f} memory files, {e} index entries, writer_state={s}.[/green]"
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
