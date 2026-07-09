"""`persome doctor` — the BYO-key install self-check (src/persome/doctor.py).

Every branch is exercised offline via monkeypatch: no network, no LLM, no real
TCC probe. Contract pinned here:

* three-state checks (ok / fail / warn); warnings NEVER count as failures;
* env-file perms gate (0600 vs group/other bits vs missing);
* base-URL reachability is warn-only on network errors;
* helper checks honour the env override and never compile;
* AX trust maps True→ok, False→fail, probe error / non-macOS→warn (unknown);
* port check passes when our own daemon holds the port.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from persome import doctor, paths


@pytest.fixture
def clean_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(var, raising=False)


# ── env file ──────────────────────────────────────────────────────────────────


def test_env_file_missing_fails(ac_root: Path, clean_llm_env: None) -> None:
    c = doctor.check_env_file()
    assert c.status == "fail"
    assert "missing" in c.detail


def test_env_file_missing_but_shell_key_warns(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-synthetic")
    c = doctor.check_env_file()
    assert c.status == "warn"


def test_env_file_0600_ok(ac_root: Path) -> None:
    p = paths.env_file()
    p.write_text("ANTHROPIC_API_KEY=sk-test-synthetic\n")
    p.chmod(0o600)
    assert doctor.check_env_file().status == "ok"


def test_env_file_loose_perms_fail(ac_root: Path) -> None:
    p = paths.env_file()
    p.write_text("ANTHROPIC_API_KEY=sk-test-synthetic\n")
    p.chmod(0o644)
    c = doctor.check_env_file()
    assert c.status == "fail"
    assert "chmod 600" in c.detail


def test_env_file_owner_only_stricter_than_0600_ok(ac_root: Path) -> None:
    p = paths.env_file()
    p.write_text("ANTHROPIC_API_KEY=sk-test-synthetic\n")
    p.chmod(0o400)
    assert doctor.check_env_file().status == "ok"


# ── API key ───────────────────────────────────────────────────────────────────


def test_api_key_set_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-synthetic")
    c = doctor.check_api_key()
    assert c.status == "ok"
    # never leak the key value
    assert "sk-test-synthetic" not in c.detail


def test_api_key_missing_fails(ac_root: Path, clean_llm_env: None) -> None:
    assert doctor.check_api_key().status == "fail"


# ── base URL (warn-only) ──────────────────────────────────────────────────────


def test_base_url_reachable_ok(monkeypatch: pytest.MonkeyPatch, clean_llm_env: None) -> None:
    import httpx

    def fake_head(url: str, **kw: object) -> object:
        assert url == "https://api.anthropic.com"  # default when unset
        return object()

    monkeypatch.setattr(httpx, "head", fake_head)
    c = doctor.check_base_url()
    assert c.status == "ok"
    assert "(default)" in c.detail


def test_base_url_unreachable_warns_never_fails(
    monkeypatch: pytest.MonkeyPatch, clean_llm_env: None
) -> None:
    import httpx

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.invalid/anthropic")

    def fake_head(url: str, **kw: object) -> object:
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "head", fake_head)
    c = doctor.check_base_url()
    assert c.status == "warn"
    assert "gateway.invalid" in c.detail


# ── Swift helpers ─────────────────────────────────────────────────────────────


def _make_exec(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("#!/bin/sh\n")
    p.chmod(0o755)
    return p


def test_helpers_env_override_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    helper = _make_exec(tmp_path / "mac-ax-helper")
    watcher = _make_exec(tmp_path / "mac-ax-watcher")
    monkeypatch.setenv("PERSOME_AX_HELPER", str(helper))
    monkeypatch.setenv("PERSOME_AX_WATCHER", str(watcher))
    checks = doctor.check_helpers()
    assert [c.status for c in checks] == ["ok", "ok"]


def test_helpers_env_override_not_executable_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bogus = tmp_path / "nope"
    bogus.write_text("")  # exists but not executable
    monkeypatch.setenv("PERSOME_AX_HELPER", str(bogus))
    monkeypatch.delenv("PERSOME_AX_WATCHER", raising=False)
    checks = doctor.check_helpers()
    by_name = {c.name: c for c in checks}
    assert by_name["mac-ax-helper"].status == "fail"


def test_helpers_missing_fail_with_install_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PERSOME_AX_HELPER", raising=False)
    monkeypatch.delenv("PERSOME_AX_WATCHER", raising=False)
    monkeypatch.setattr(doctor, "_helper_candidates", lambda name: [tmp_path / name])
    checks = doctor.check_helpers()
    assert all(c.status == "fail" for c in checks)
    assert "install.sh" in checks[0].detail


# ── AX trust ──────────────────────────────────────────────────────────────────


def test_ax_trust_granted_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.platform, "system", lambda: "Darwin")
    import persome.capture.ax_capture as ax_capture

    monkeypatch.setattr(ax_capture, "ax_trusted", lambda: True)
    assert doctor.check_ax_trust().status == "ok"


def test_ax_trust_denied_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.platform, "system", lambda: "Darwin")
    import persome.capture.ax_capture as ax_capture

    monkeypatch.setattr(ax_capture, "ax_trusted", lambda: False)
    c = doctor.check_ax_trust()
    assert c.status == "fail"
    assert "Accessibility" in c.detail


def test_ax_trust_probe_error_warns_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.platform, "system", lambda: "Darwin")
    import persome.capture.ax_capture as ax_capture

    def boom() -> bool:
        raise RuntimeError("no TCC")

    monkeypatch.setattr(ax_capture, "ax_trusted", boom)
    c = doctor.check_ax_trust()
    assert c.status == "warn"
    assert "unknown" in c.detail


def test_ax_trust_non_macos_warns_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.platform, "system", lambda: "Linux")
    c = doctor.check_ax_trust()
    assert c.status == "warn"
    assert "unknown" in c.detail


# ── data root ─────────────────────────────────────────────────────────────────


def test_root_writable_ok(ac_root: Path) -> None:
    assert doctor.check_root_writable().status == "ok"


def test_root_unwritable_fails(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self: Path, *a: object, **kw: object) -> None:
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(Path, "write_text", boom)
    assert doctor.check_root_writable().status == "fail"


# ── port ──────────────────────────────────────────────────────────────────────


def test_port_free_ok(ac_root: Path) -> None:
    # grab an ephemeral port, close it — now known-free
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert doctor.check_port("127.0.0.1", port).status == "ok"


def test_port_taken_by_stranger_fails(ac_root: Path) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        c = doctor.check_port("127.0.0.1", port)
    assert c.status == "fail"
    assert "config.toml" in c.detail


def test_port_taken_by_our_daemon_ok(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        monkeypatch.setattr(doctor, "_running_daemon_pid", lambda: 12345)
        c = doctor.check_port("127.0.0.1", port)
    assert c.status == "ok"
    assert "12345" in c.detail


def test_running_daemon_pid_reads_pid_file(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    paths.pid_file().write_text(str(os.getpid()))  # our own pid is always alive
    assert doctor._running_daemon_pid() == os.getpid()
    paths.pid_file().write_text("not-a-pid")
    assert doctor._running_daemon_pid() is None


# ── run_checks / exit-code aggregation ────────────────────────────────────────


def test_run_checks_merges_env_file_first(
    ac_root: Path, clean_llm_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key written ONLY to the env file must be visible to check_api_key."""
    import httpx

    p = paths.env_file()
    p.write_text("ANTHROPIC_API_KEY=sk-test-synthetic\n")
    p.chmod(0o600)
    monkeypatch.setattr(httpx, "head", lambda url, **kw: object())
    checks = doctor.run_checks("127.0.0.1", 0)
    by_name = {c.name: c for c in checks}
    assert by_name["env file"].status == "ok"
    assert by_name["ANTHROPIC_API_KEY"].status == "ok"


def test_has_failure_ignores_warnings() -> None:
    warns = [doctor.Check("a", "warn"), doctor.Check("b", "ok")]
    assert doctor.has_failure(warns) is False
    assert doctor.has_failure([*warns, doctor.Check("c", "fail")]) is True


def test_cli_doctor_exit_code(
    ac_root: Path, clean_llm_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end through the typer command: a broken install exits 1, and a
    green one exits 0 — warnings alone never flip the exit code."""
    from typer.testing import CliRunner

    from persome.cli import app

    runner = CliRunner()
    # Broken: no env file, no key, no helpers → exit 1. (Network stubbed out —
    # the default test gate is offline.)
    import httpx

    monkeypatch.setattr(httpx, "head", lambda url, **kw: object())
    monkeypatch.delenv("PERSOME_AX_HELPER", raising=False)
    monkeypatch.delenv("PERSOME_AX_WATCHER", raising=False)
    monkeypatch.setattr(doctor, "_helper_candidates", lambda name: [])
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1

    # Green (fail-able checks all pass; base URL warn is fine).
    green = [doctor.Check("env file", "ok"), doctor.Check("base URL", "warn")]
    monkeypatch.setattr(doctor, "run_checks", lambda host, port: green)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
