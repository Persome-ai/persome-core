"""Regression: daemon.run() must hard-exit on shutdown, never wedge on the
default-executor join.

The bug: ``daemon.run`` used ``asyncio.run(_run(...))``. ``_run`` does the full
durable shutdown itself, but ``asyncio.run`` THEN calls
``loop.shutdown_default_executor()``, which *joins* the default-executor threads.
At app-quit/launchd-bootout the daemon routinely has a blocking LLM/embedding
call in flight on that executor (every off-loop blocking call goes through
``asyncio.to_thread`` → the default executor), frequently parked in a TLS read
(``_ssl__SSLSocket_read``). Joining a worker that never returns wedges the
shutdown forever → launchd SIGKILL / an OpenSSL-teardown SIGSEGV (≈10 crash
reports/day under relaunch churn).

The fix: ``daemon.run`` drives the loop manually and ``os._exit(0)`` the instant
``_run`` returns — before any executor join. This test drives the REAL
``daemon.run`` in a subprocess with a stubbed ``_run`` that leaves one
never-returning job on the default executor, and asserts the process exits
cleanly within a tight bound instead of hanging.
"""

from __future__ import annotations

import subprocess
import sys

# Child program: stub persome.daemon._run so it parks a forever-blocking job on the
# DEFAULT executor (mimicking an in-flight SSL embed/LLM call at SIGTERM) and then
# returns, exactly like the real _run after the stop signal. The fixed run() must
# hard-exit(0) without joining that worker. On the old asyncio.run() path this
# child would hang in shutdown_default_executor() and hit the timeout below.
_CHILD = r"""
import asyncio, threading
import persome.daemon as d

_blocking_started = threading.Event()

async def fake_run(cfg, *, capture_only=False, hard_exit=False):
    loop = asyncio.get_running_loop()
    def _block():
        _blocking_started.set()
        threading.Event().wait()   # never returns — like a parked _ssl__SSLSocket_read
    loop.run_in_executor(None, _block)          # default executor; never awaited
    await asyncio.to_thread(_blocking_started.wait)  # ensure the worker is parked
    return  # durable shutdown already done in the real _run by this point

d._run = fake_run
d.run(object(), capture_only=True)   # must os._exit(0); if it returns we print FAIL
print("REACHED-AFTER-RUN")           # unreachable: run() hard-exits
"""


def test_run_hard_exits_with_blocked_executor_worker() -> None:
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD],
        capture_output=True,
        text=True,
        timeout=15,  # generous; the fix exits in well under 1s, a wedge would hit this
    )
    # Clean hard-exit(0); never fell through to the post-run print (that line means
    # run() returned instead of hard-exiting), and never wedged into the timeout.
    assert proc.returncode == 0, (
        f"expected clean exit 0, got {proc.returncode}\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr[-2000:]!r}"
    )
    assert "REACHED-AFTER-RUN" not in proc.stdout, (
        "run() returned to the caller instead of hard-exiting — the os._exit guard is gone"
    )


def test_run_nonzero_exit_on_crash() -> None:
    """A crash inside _run hard-exits non-zero (and still never joins the executor)."""
    child = (
        "import persome.daemon as d\n"
        "async def boom(cfg, *, capture_only=False, hard_exit=False):\n"
        "    raise RuntimeError('boom')\n"
        "d._run = boom\n"
        "d.run(object(), capture_only=True)\n"
        "print('REACHED-AFTER-RUN')\n"
    )
    proc = subprocess.run([sys.executable, "-c", child], capture_output=True, text=True, timeout=15)
    assert proc.returncode == 1, f"expected exit 1 on crash, got {proc.returncode}"
    assert "REACHED-AFTER-RUN" not in proc.stdout
