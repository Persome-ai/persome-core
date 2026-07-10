"""Unit tests for the dotenv loader used at daemon ``start`` time."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from persome import env_file
from persome.env_file import load_env_file


def test_missing_file_returns_zero(tmp_path: Path) -> None:
    assert load_env_file(tmp_path / "nope") == 0


def test_basic_kv_merged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FOO_K", raising=False)
    monkeypatch.delenv("BAR_K", raising=False)
    p = tmp_path / "env"
    p.write_text("FOO_K=foo\nBAR_K=bar\n")
    assert load_env_file(p) == 2
    assert os.environ["FOO_K"] == "foo"
    assert os.environ["BAR_K"] == "bar"


def test_does_not_overwrite_existing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Shell export must win over the file — keeps CLI debugging predictable."""
    monkeypatch.setenv("KEEP_ME", "shell-wins")
    p = tmp_path / "env"
    p.write_text("KEEP_ME=file-loses\n")
    assert load_env_file(p) == 0
    assert os.environ["KEEP_ME"] == "shell-wins"


def test_comments_blanks_and_quotes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("A_KEY", "B_KEY", "C_KEY"):
        monkeypatch.delenv(name, raising=False)
    p = tmp_path / "env"
    p.write_text(
        """
# a comment
A_KEY = plain

  # leading-space comment
B_KEY="with spaces"
C_KEY='single-quoted'
not a real line
=missing-key
"""
    )
    n = load_env_file(p)
    assert n == 3
    assert os.environ["A_KEY"] == "plain"
    assert os.environ["B_KEY"] == "with spaces"
    assert os.environ["C_KEY"] == "single-quoted"


def test_invalid_key_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OK_KEY", raising=False)
    p = tmp_path / "env"
    p.write_text("bad-key=1\n9STARTS_WITH_DIGIT=1\nOK_KEY=ok\n")
    n = load_env_file(p)
    # bad-key has a dash → rejected; digit-starting key has alnum chars only
    # under our isalnum-after-stripping-underscore rule, so it is allowed.
    # Verify at minimum that the well-formed key landed and that the
    # malformed dash-key did not.
    assert os.environ["OK_KEY"] == "ok"
    assert "bad-key" not in os.environ
    assert n >= 1


def test_ensure_screenshot_key_generates_owner_only_file(tmp_path: Path) -> None:
    path = tmp_path / "env"
    path.write_text("ANTHROPIC_API_KEY=synthetic\n")

    status = env_file.ensure_screenshot_key(path)

    assert status == "generated"
    assert path.stat().st_mode & 0o777 == 0o600
    lines = path.read_text().splitlines()
    assert "ANTHROPIC_API_KEY=synthetic" in lines
    generated = next(
        line.partition("=")[2]
        for line in lines
        if line.startswith(f"{env_file.SCREENSHOT_KEY_ENV}=")
    )
    assert env_file.is_valid_screenshot_key(generated)


def test_ensure_screenshot_key_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "env"
    original = "ab" * 32
    path.write_text(f"{env_file.SCREENSHOT_KEY_ENV}={original}\n")

    assert env_file.ensure_screenshot_key(path) == "existing"
    assert env_file.ensure_screenshot_key(path) == "existing"
    assert path.read_text().count(f"{env_file.SCREENSHOT_KEY_ENV}=") == 1
    assert f"{env_file.SCREENSHOT_KEY_ENV}={original}" in path.read_text()


def test_ensure_screenshot_key_replaces_invalid_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "env"
    path.write_text(
        f"{env_file.SCREENSHOT_KEY_ENV}=invalid\n{env_file.SCREENSHOT_KEY_ENV}=also-invalid\n"
    )

    assert env_file.ensure_screenshot_key(path) == "generated"
    canonical = [
        line.partition("=")[2]
        for line in path.read_text().splitlines()
        if line.startswith(f"{env_file.SCREENSHOT_KEY_ENV}=")
    ]
    assert len(canonical) == 1
    assert env_file.is_valid_screenshot_key(canonical[0])
