"""Cross-process serialization for one Markdown memory source."""

from __future__ import annotations

import multiprocessing
import os
from pathlib import Path


def _hold_file_lock(
    root: str,
    path: str,
    acquired: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
) -> None:
    os.environ["PERSOME_ROOT"] = root
    from persome.store.files import file_lock

    with file_lock(Path(path)):
        acquired.set()
        if not release.wait(timeout=10):
            raise RuntimeError("test lock release timed out")


def test_file_lock_serializes_separate_processes(ac_root) -> None:
    ctx = multiprocessing.get_context("spawn")
    first_acquired = ctx.Event()
    first_release = ctx.Event()
    second_acquired = ctx.Event()
    second_release = ctx.Event()
    path = ac_root / "memory" / "user-profile.md"

    first = ctx.Process(
        target=_hold_file_lock,
        args=(str(ac_root), str(path), first_acquired, first_release),
    )
    second = ctx.Process(
        target=_hold_file_lock,
        args=(str(ac_root), str(path), second_acquired, second_release),
    )
    first.start()
    try:
        assert first_acquired.wait(timeout=5)
        second.start()
        assert not second_acquired.wait(timeout=0.25)
        first_release.set()
        assert second_acquired.wait(timeout=5)
        second_release.set()
    finally:
        first_release.set()
        second_release.set()
        first.join(timeout=5)
        if second.pid is not None:
            second.join(timeout=5)
        if first.is_alive():
            first.terminate()
        if second.pid is not None and second.is_alive():
            second.terminate()

    assert first.exitcode == 0
    assert second.exitcode == 0
