"""`persome doctor` — the BYO-provider install self-check.

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
from persome.capture import ocr_health, ocr_local, screen_recording
from persome.config import CaptureConfig
from persome.providers import LLM_API_KEY_ENV, LLM_BASE_URL_ENV, PROVIDERS


@pytest.fixture
def clean_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    variables = {
        LLM_API_KEY_ENV,
        LLM_BASE_URL_ENV,
        "PERSOME_SCREENSHOT_KEY",
        "PERSOME_LOCAL_API_TOKEN",
    }
    for spec in PROVIDERS:
        variables.add(spec.discovery_api_key_env)
        if spec.resolved_base_url_env:
            variables.add(spec.resolved_base_url_env)
    for var in variables:
        monkeypatch.delenv(var, raising=False)


# ── env file ──────────────────────────────────────────────────────────────────


def test_env_file_missing_fails(ac_root: Path, clean_llm_env: None) -> None:
    c = doctor.check_env_file()
    assert c.status == "fail"
    assert "missing" in c.detail


def test_env_file_missing_but_shell_key_warns(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(LLM_API_KEY_ENV, "sk-test-synthetic")
    c = doctor.check_env_file()
    assert c.status == "warn"


def test_env_file_0600_ok(ac_root: Path) -> None:
    p = paths.env_file()
    p.write_text(f"{LLM_API_KEY_ENV}=sk-test-synthetic\n")
    p.chmod(0o600)
    assert doctor.check_env_file().status == "ok"


def test_env_file_loose_perms_fail(ac_root: Path) -> None:
    p = paths.env_file()
    p.write_text(f"{LLM_API_KEY_ENV}=sk-test-synthetic\n")
    p.chmod(0o644)
    c = doctor.check_env_file()
    assert c.status == "fail"
    assert "chmod 600" in c.detail


def test_env_file_owner_only_stricter_than_0600_ok(ac_root: Path) -> None:
    p = paths.env_file()
    p.write_text(f"{LLM_API_KEY_ENV}=sk-test-synthetic\n")
    p.chmod(0o400)
    assert doctor.check_env_file().status == "ok"


# ── API key ───────────────────────────────────────────────────────────────────


def test_api_key_set_ok(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LLM_API_KEY_ENV, "sk-test-synthetic")
    c = doctor.check_api_key()
    assert c.status == "ok"
    # never leak the key value
    assert "sk-test-synthetic" not in c.detail


def test_api_key_missing_fails(ac_root: Path, clean_llm_env: None) -> None:
    assert doctor.check_api_key().status == "fail"


def test_screenshot_key_set_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSOME_SCREENSHOT_KEY", "ab" * 32)
    c = doctor.check_screenshot_key()
    assert c.status == "ok"
    assert "ab" * 32 not in c.detail


def test_screenshot_key_missing_warns(clean_llm_env: None) -> None:
    c = doctor.check_screenshot_key()
    assert c.status == "warn"
    assert "rerun install.sh" in c.detail


def test_local_api_token_set_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    token = "local-token-" + "a" * 40
    monkeypatch.setenv("PERSOME_LOCAL_API_TOKEN", token)
    c = doctor.check_local_api_token()
    assert c.status == "ok"
    assert token not in c.detail


def test_local_api_token_missing_or_invalid_fails(clean_llm_env: None) -> None:
    assert doctor.check_local_api_token().status == "fail"


def test_sqlite_secure_fts_supported() -> None:
    assert doctor.check_sqlite_secure_fts().status == "ok"


def test_sqlite_secure_fts_rejects_old_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.sqlite3, "sqlite_version_info", (3, 41, 2))
    monkeypatch.setattr(doctor.sqlite3, "sqlite_version", "3.41.2")

    check = doctor.check_sqlite_secure_fts()

    assert check.status == "fail"
    assert "3.42+" in check.detail


# ── base URL (warn-only) ──────────────────────────────────────────────────────


def test_base_url_reachable_ok(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch, clean_llm_env: None
) -> None:
    import httpx

    def fake_head(url: str, **kw: object) -> object:
        assert url == "https://api.anthropic.com"  # default when unset
        return object()

    monkeypatch.setattr(httpx, "head", fake_head)
    c = doctor.check_base_url()
    assert c.status == "ok"
    assert "legacy route" in c.detail


def test_base_url_unreachable_warns_never_fails(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch, clean_llm_env: None
) -> None:
    import httpx

    monkeypatch.setenv(LLM_BASE_URL_ENV, "https://gateway.invalid/anthropic")

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
    monkeypatch.setattr(doctor, "_helper_sources", lambda name: [])
    checks = doctor.check_helpers()
    assert all(c.status == "fail" for c in checks)
    assert "reinstall" in checks[0].detail


def test_helpers_bundled_source_warns_without_compiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PERSOME_AX_HELPER", raising=False)
    monkeypatch.delenv("PERSOME_AX_WATCHER", raising=False)
    for name in ("mac-ax-helper", "mac-ax-watcher"):
        (tmp_path / f"{name}.swift").write_text("// synthetic")
    monkeypatch.setattr(doctor, "_helper_candidates", lambda name: [tmp_path / name])
    monkeypatch.setattr(doctor, "_helper_sources", lambda name: [tmp_path / f"{name}.swift"])
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/swiftc")

    checks = doctor.check_helpers()

    assert all(c.status == "warn" for c in checks)
    assert all("installer" in c.detail for c in checks)
    assert not any((tmp_path / name).exists() for name in ("mac-ax-helper", "mac-ax-watcher"))


def test_helpers_bundled_source_without_swiftc_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PERSOME_AX_HELPER", raising=False)
    monkeypatch.delenv("PERSOME_AX_WATCHER", raising=False)
    for name in ("mac-ax-helper", "mac-ax-watcher"):
        (tmp_path / f"{name}.swift").write_text("// synthetic")
    monkeypatch.setattr(doctor, "_helper_candidates", lambda name: [tmp_path / name])
    monkeypatch.setattr(doctor, "_helper_sources", lambda name: [tmp_path / f"{name}.swift"])
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)

    checks = doctor.check_helpers()

    assert all(c.status == "fail" for c in checks)
    assert all("Command Line Tools" in c.detail for c in checks)


def test_doctor_ignores_historical_helper_cache_and_never_compiles(
    ac_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from persome.capture import ax_capture

    for name in ("mac-ax-helper", "mac-ax-watcher"):
        _make_exec(ac_root / "native" / "historical-digest" / name)
        (tmp_path / f"{name}.swift").write_text("// current source", encoding="utf-8")
    monkeypatch.setattr(
        doctor,
        "_helper_sources",
        lambda name: [tmp_path / f"{name}.swift"],
    )
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/swiftc")
    monkeypatch.setattr(doctor.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        ax_capture,
        "_maybe_compile",
        lambda *args, **kwargs: pytest.fail("doctor must never compile or write helpers"),
    )

    checks = doctor.check_helpers(CaptureConfig())
    trust = doctor.check_ax_trust(CaptureConfig())

    assert all(check.status == "warn" for check in checks)
    assert trust.status == "fail"
    assert not any(
        path.parent.name != "historical-digest" for path in (ac_root / "native").glob("*/*")
    )


# ── AX trust ──────────────────────────────────────────────────────────────────


def test_ax_trust_granted_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.platform, "system", lambda: "Darwin")
    import persome.capture.ax_capture as ax_capture

    monkeypatch.setattr(
        doctor,
        "_configured_helper_path",
        lambda name, env_var: Path("/synthetic") / name,
    )
    monkeypatch.setattr(ax_capture, "_binary_ax_trusted", lambda path: True)
    assert doctor.check_ax_trust().status == "ok"


def test_ax_trust_denied_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.platform, "system", lambda: "Darwin")
    import persome.capture.ax_capture as ax_capture

    monkeypatch.setattr(
        doctor,
        "_configured_helper_path",
        lambda name, env_var: Path("/synthetic") / name,
    )
    monkeypatch.setattr(ax_capture, "_binary_ax_trusted", lambda path: False)
    c = doctor.check_ax_trust()
    assert c.status == "fail"
    assert "Accessibility" in c.detail


def test_ax_trust_probe_error_warns_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.platform, "system", lambda: "Darwin")
    import persome.capture.ax_capture as ax_capture

    def boom(path: Path) -> bool:
        raise RuntimeError("no TCC")

    monkeypatch.setattr(
        doctor,
        "_configured_helper_path",
        lambda name, env_var: Path("/synthetic") / name,
    )
    monkeypatch.setattr(ax_capture, "_binary_ax_trusted", boom)
    c = doctor.check_ax_trust()
    assert c.status == "warn"
    assert "unknown" in c.detail


def test_ax_trust_non_macos_warns_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.platform, "system", lambda: "Linux")
    c = doctor.check_ax_trust()
    assert c.status == "warn"
    assert "unknown" in c.detail


def test_ingest_mode_skips_daemon_tcc_and_helper_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = CaptureConfig(source="ingest")
    import persome.capture.ax_capture as ax_capture

    monkeypatch.setattr(
        ax_capture,
        "_binary_ax_trusted",
        lambda path: pytest.fail("ingest must not probe daemon Accessibility"),
    )
    monkeypatch.setattr(
        screen_recording,
        "has_screen_recording",
        lambda: pytest.fail("ingest must not probe daemon Screen Recording"),
    )

    assert doctor.check_ax_trust(capture).status == "ok"
    assert doctor.check_screen_recording(capture).status == "ok"
    assert all(check.status == "ok" for check in doctor.check_helpers(capture))


# ── Screen Recording + local OCR ─────────────────────────────────────────────


def test_screen_recording_granted_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(screen_recording, "has_screen_recording", lambda: True)

    check = doctor.check_screen_recording(CaptureConfig(enable_ocr_fallback=True))

    assert check.status == "ok"


def test_screen_recording_denied_fails_when_ocr_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(screen_recording, "has_screen_recording", lambda: False)

    check = doctor.check_screen_recording(CaptureConfig(enable_ocr_fallback=True))

    assert check.status == "fail"
    assert "Screen Recording" in check.detail


def test_ocr_ready_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ocr_health.sys, "platform", "darwin")
    monkeypatch.setattr(ocr_local, "runtime_available", lambda: True)
    monkeypatch.setattr(ocr_local, "models_available", lambda tier: True)
    monkeypatch.setattr(ocr_local, "disabled_by_environment", lambda: False)
    monkeypatch.setattr(screen_recording, "has_screen_recording", lambda: True)

    check = doctor.check_ocr(CaptureConfig(enable_ocr_fallback=True))

    assert check.status == "ok"
    assert "isolated worker" in check.detail


def test_ocr_permission_block_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ocr_health.sys, "platform", "darwin")
    monkeypatch.setattr(ocr_local, "runtime_available", lambda: True)
    monkeypatch.setattr(ocr_local, "models_available", lambda tier: True)
    monkeypatch.setattr(ocr_local, "disabled_by_environment", lambda: False)
    monkeypatch.setattr(screen_recording, "has_screen_recording", lambda: False)

    check = doctor.check_ocr(CaptureConfig(enable_ocr_fallback=True))

    assert check.status == "fail"
    assert "permission_required" in check.detail


def test_enabled_ocr_without_architecture_backend_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ocr_local, "runtime_available", lambda: False)
    monkeypatch.setattr(ocr_local, "models_available", lambda tier: False)
    monkeypatch.setattr(ocr_local, "disabled_by_environment", lambda: False)

    check = doctor.check_ocr(CaptureConfig(enable_ocr_fallback=True))

    assert check.status == "fail"
    assert "runtime_unavailable" in check.detail


def test_ocr_disabled_is_visible_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ocr_health.sys, "platform", "darwin")
    monkeypatch.setattr(ocr_local, "runtime_available", lambda: True)
    monkeypatch.setattr(ocr_local, "models_available", lambda tier: True)
    monkeypatch.setattr(ocr_local, "disabled_by_environment", lambda: False)
    monkeypatch.setattr(screen_recording, "has_screen_recording", lambda: True)

    check = doctor.check_ocr(CaptureConfig(enable_ocr_fallback=False))

    assert check.status == "warn"
    assert "persome ocr setup" in check.detail


# ── data root ─────────────────────────────────────────────────────────────────


def test_root_writable_ok(ac_root: Path) -> None:
    assert doctor.check_root_writable().status == "ok"


def test_root_unwritable_fails(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **kw: object) -> None:
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(doctor.tempfile, "mkstemp", boom)
    assert doctor.check_root_writable().status == "fail"


def test_root_initialization_failure_is_reported(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: object, **kw: object) -> None:
        raise RuntimeError("unsafe data root")

    monkeypatch.setattr(doctor.paths, "ensure_private_dir", boom)

    check = doctor.check_root_writable()

    assert check.status == "fail"
    assert str(paths.root()) in check.detail
    assert "unsafe data root" in check.detail


def test_root_writable_probe_does_not_follow_symlink(ac_root: Path) -> None:
    victim = ac_root.parent / "doctor-victim.txt"
    victim.write_text("ORIGINAL", encoding="utf-8")
    victim.chmod(0o644)
    probe = paths.root() / ".doctor-write-probe"
    probe.symlink_to(victim)

    assert doctor.check_root_writable().status == "ok"
    assert victim.read_text(encoding="utf-8") == "ORIGINAL"
    assert victim.stat().st_mode & 0o777 == 0o644


def test_root_writable_probe_is_unlinked_before_write(ac_root: Path) -> None:
    assert doctor.check_root_writable().status == "ok"
    assert not list(paths.root().glob(".doctor-write-probe.*"))


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


def test_running_daemon_pid_rejects_unverified_pid_file(ac_root: Path) -> None:
    import os

    # A live PID is not enough: it must resolve to this user's Persome daemon.
    paths.pid_file().write_text(str(os.getpid()))
    assert doctor._running_daemon_pid() is None
    paths.pid_file().write_text("not-a-pid")
    assert doctor._running_daemon_pid() is None


# ── run_checks / exit-code aggregation ────────────────────────────────────────


def test_run_checks_merges_env_file_first(
    ac_root: Path, clean_llm_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key written ONLY to the env file must be visible to check_api_key."""
    import httpx

    p = paths.env_file()
    p.write_text(
        f"{LLM_API_KEY_ENV}=sk-test-synthetic\n"
        "PERSOME_LOCAL_API_TOKEN=local-token-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
    )
    p.chmod(0o600)
    monkeypatch.setattr(httpx, "head", lambda url, **kw: object())
    checks = doctor.run_checks("127.0.0.1", 0)
    by_name = {c.name: c for c in checks}
    assert by_name["env file"].status == "ok"
    assert by_name["LLM credential"].status == "ok"
    assert by_name["PERSOME_LOCAL_API_TOKEN"].status == "ok"
    assert by_name["SQLite secure FTS"].status == "ok"
    assert by_name["PERSOME_SCREENSHOT_KEY"].status == "warn"
    assert "Screen Recording" in by_name
    assert by_name["local OCR"].status == "warn"


def test_openai_profile_uses_selected_credential_and_endpoint(
    ac_root: Path, clean_llm_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    paths.config_file().write_text(
        """
[models.default]
provider = "openai"
protocol = "openai"
model = "gpt-4.1-mini"
base_url = "https://gateway.example/v1"
api_key_env = "PERSOME_LLM_API_KEY"
"""
    )
    monkeypatch.setenv(LLM_API_KEY_ENV, "synthetic")
    seen: list[str] = []
    monkeypatch.setattr(httpx, "head", lambda url, **kw: seen.append(url) or object())

    assert doctor.check_api_key().status == "ok"
    assert doctor.check_base_url().status == "ok"
    assert seen == ["https://gateway.example/v1"]


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
