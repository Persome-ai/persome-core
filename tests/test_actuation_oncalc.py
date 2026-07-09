"""On-device acceptance harness for the actuation layer — proves REAL clicks.

Compiles `mac-ax-actuator`, drives the stock **Calculator** (a native AX-rich app, no Electron
needed), clicks the digit buttons via the actuator's AX-path `act`, and asserts each click's
before/after **AX diff** shows the display value changing — i.e. the click really landed. Modeled on
mediar-ai/mcp-server-macos-use's diff-as-feedback.

`@pytest.mark.macos` — needs a GUI + Accessibility trust, so it's OUT of the offline Linux gate
(like the other Swift-helper tests). Run on a Mac with AX permission:
    uv run pytest -m macos tests/test_actuation_oncalc.py -s
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.macos

_SRC = Path(__file__).resolve().parents[1] / "resources" / "mac-ax-actuator.swift"


def _compile(tmp_path: Path) -> Path:
    binary = tmp_path / "mac-ax-actuator"
    r = subprocess.run(
        [
            "swiftc",
            "-O",
            "-framework",
            "Cocoa",
            "-framework",
            "ApplicationServices",
            str(_SRC),
            "-o",
            str(binary),
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        pytest.skip(f"swiftc unavailable / compile failed: {r.stderr[:300]}")
    return binary


def _run(binary: Path, *args: str) -> dict:
    out = subprocess.run([str(binary), *args], capture_output=True).stdout
    return json.loads(out.decode("utf-8", "replace"))


def _calc_pid() -> str | None:
    r = subprocess.run(["pgrep", "-x", "Calculator"], capture_output=True, text=True)
    return r.stdout.split()[0] if r.stdout.strip() else None


def test_actuator_real_clicks_calculator(tmp_path: Path) -> None:
    binary = _compile(tmp_path)

    if not _run(binary, "trust").get("trusted"):
        pytest.skip("process is not Accessibility-trusted — grant it to run real-click harness")

    subprocess.run(["open", "-a", "Calculator"], check=False)
    time.sleep(1.5)
    pid = _calc_pid()
    if pid is None:
        pytest.skip("Calculator not available")

    try:

        def fresh_button(label: str) -> str:
            # FRESH snapshot each step: a click mutates the tree, so a stale id is (correctly)
            # rejected by the actuator's label-hash guard — re-resolve right before acting.
            snap = _run(binary, "snapshot", "--pid", pid)
            assert snap.get("ok"), snap
            return next(
                e["id"]
                for e in snap["elements"]
                if e.get("label") == label and e.get("role") == "AXButton"
            )

        effects = 0
        for label in ("1", "2", "3"):
            res = _run(binary, "act", "--no-cursor", "--pid", pid, "--id", fresh_button(label), "--verb", "press")
            assert res.get("ok"), f"click {label} did not perform: {res}"
            # The display element's value change shows up in the AX diff → the click really landed.
            changed = [d for d in res.get("diff", []) if d.get("change") in ("changed", "appeared")]
            assert changed, (
                f"click {label} produced no observable AX diff (did it really click?): {res}"
            )
            effects += 1

        assert effects == 3
    finally:
        subprocess.run(["osascript", "-e", 'tell application "Calculator" to quit'], check=False)


def test_python_stack_gate_real_click(tmp_path: Path, monkeypatch) -> None:
    """The full PRODUCTION path: persome.actuation.actuator + gate.Gate do a REAL click on Calculator
    and verify it from the AX diff — proving the Python layer, not just the binary."""
    binary = _compile(tmp_path)
    if not _run(binary, "trust").get("trusted"):
        pytest.skip("not Accessibility-trusted")
    monkeypatch.setenv("PERSOME_AX_ACTUATOR", str(binary))

    from persome.actuation import actuator, gate

    subprocess.run(["open", "-a", "Calculator"], check=False)
    time.sleep(1.5)
    if _calc_pid() is None:
        pytest.skip("Calculator not available")
    try:
        snap = actuator.snapshot(app="Calculator")
        assert snap.get("ok"), snap
        seven = next(
            e for e in snap["elements"] if e.get("label") == "7" and e.get("role") == "AXButton"
        )
        g = gate.Gate(
            confirm=lambda s: True,  # "7" isn't a side-effect label, so confirm is never consulted
            perform=lambda *, verb, element_id, text: actuator.act(
                app="Calculator", element_id=element_id, verb=verb, text=text, show_cursor=False
            ),
        )
        res = g.run(
            verb="press", element_id=seven["id"], label="7", bundle_id=snap.get("bundle_id", "")
        )
        assert res["ok"] and res["verified"] and not res["gated"], res
    finally:
        subprocess.run(["osascript", "-e", 'tell application "Calculator" to quit'], check=False)


def _quit(app: str) -> None:
    subprocess.run(["osascript", "-e", f'tell application "{app}" to quit'], check=False)


def _trusted_binary(tmp_path: Path) -> Path:
    binary = _compile(tmp_path)
    if not _run(binary, "trust").get("trusted"):
        pytest.skip("not Accessibility-trusted")
    return binary


def test_calculator_arithmetic_value(tmp_path: Path) -> None:
    """A stronger assertion than 'something changed': clicking 7 then 8 makes the display read 78."""
    binary = _trusted_binary(tmp_path)
    subprocess.run(["open", "-a", "Calculator"], check=False)
    time.sleep(1.5)
    if _calc_pid() is None:
        pytest.skip("Calculator not available")
    pid = _calc_pid()
    try:
        _run(binary, "act", "--no-cursor", "--pid", pid, "--verb", "key", "--keys", "escape")  # clear
        time.sleep(0.2)

        def click(label: str) -> dict:
            snap = _run(binary, "snapshot", "--pid", pid)
            eid = next(
                e["id"] for e in snap["elements"]
                if e.get("label") == label and e.get("role") == "AXButton"
            )
            return _run(binary, "act", "--no-cursor", "--pid", pid, "--id", eid, "--verb", "press")

        click("7")
        res = click("8")
        afters = [d.get("after", "") for d in res.get("diff", [])]
        assert any("78" in a for a in afters), f"display should read 78; diff afters={afters}"
    finally:
        _quit("Calculator")


def test_clickxy_coordinate_click(tmp_path: Path) -> None:
    """Coordinate (CGEvent) click at a button's bbox center also performs a real click."""
    binary = _trusted_binary(tmp_path)
    subprocess.run(["open", "-a", "Calculator"], check=False)
    time.sleep(1.5)
    pid = _calc_pid()
    if pid is None:
        pytest.skip("Calculator not available")
    try:
        _run(binary, "act", "--no-cursor", "--pid", pid, "--verb", "key", "--keys", "escape")
        time.sleep(0.2)
        snap = _run(binary, "snapshot", "--pid", pid)
        nine = next(
            e for e in snap["elements"]
            if e.get("label") == "9" and e.get("role") == "AXButton" and e.get("bbox")
        )
        x, y, w, h = nine["bbox"]
        res = _run(binary, "act", "--no-cursor", "--pid", pid, "--verb", "clickxy",
                   "--x", str(int(x + w / 2)), "--y", str(int(y + h / 2)))
        assert res.get("ok")
        afters = [d.get("after", "") for d in res.get("diff", [])]
        assert any("9" in a for a in afters), f"display should show 9; diff afters={afters}"
    finally:
        _quit("Calculator")


def test_textedit_cgevent_typing(tmp_path: Path) -> None:
    """CGEvent `type` lands Unicode (incl. Chinese) into a native app's focused text area."""
    binary = _trusted_binary(tmp_path)
    subprocess.run(
        ["osascript", "-e", 'tell application "TextEdit" to activate',
         "-e", 'tell application "TextEdit" to make new document'],
        check=False,
    )
    time.sleep(1.5)
    msg = "Persome actuation 测试 42"
    try:
        snap = _run(binary, "snapshot", "--app", "TextEdit")
        ta = next((e for e in snap["elements"] if e["role"] == "AXTextArea" and e.get("bbox")), None)
        if ta is None:
            pytest.skip("TextEdit text area not found")
        x, y, w, h = ta["bbox"]
        _run(binary, "act", "--no-cursor", "--app", "TextEdit", "--verb", "clickxy",
             "--x", str(int(x + w / 2)), "--y", str(int(y + 30)))
        time.sleep(0.3)
        _run(binary, "act", "--no-cursor", "--app", "TextEdit", "--verb", "type", "--text", msg)
        time.sleep(0.4)
        snap2 = _run(binary, "snapshot", "--app", "TextEdit")
        got = [e.get("value", "") for e in snap2["elements"]
               if e["role"] == "AXTextArea" and msg in (e.get("value") or "")]
        assert got, "typed text did not land in the TextEdit area"
    finally:
        subprocess.run(
            ["osascript", "-e", 'tell application "TextEdit" to close every document saving no'],
            check=False,
        )
        _quit("TextEdit")


def test_overlay_subcommand_runs(tmp_path: Path) -> None:
    """The debug overlay subcommand renders boxes for an app and exits cleanly."""
    binary = _trusted_binary(tmp_path)
    subprocess.run(["open", "-a", "Calculator"], check=False)
    time.sleep(1.2)
    if _calc_pid() is None:
        pytest.skip("Calculator not available")
    try:
        r = subprocess.run(
            [str(binary), "overlay", "--app", "Calculator", "--seconds", "1"],
            capture_output=True, text=True, timeout=10,
        )
        assert r.returncode == 0
        assert "overlay:" in r.stderr and "boxes" in r.stderr
    finally:
        _quit("Calculator")


_BROWSER_BUNDLES = ("com.tab-browser.Tabbit", "com.google.Chrome", "com.apple.Safari")


def test_browser_web_ax_addressable(tmp_path: Path) -> None:
    """A running Chromium/WebKit browser exposes a readable web AX tree (AXWebArea + labeled
    buttons + an editable field) — i.e. web apps like Gmail are AX-addressable by the actuator.
    Best-effort: skips if no browser with web content is running."""
    binary = _trusted_binary(tmp_path)
    snap = None
    for bundle in _BROWSER_BUNDLES:
        s = _run(binary, "snapshot", "--app", bundle)
        if s.get("ok") and any(e["role"] == "AXWebArea" for e in s.get("elements", [])):
            snap = s
            break
    if snap is None:
        pytest.skip("no browser with a web AX tree is running")

    els = snap["elements"]
    assert any(e["role"] == "AXWebArea" for e in els), "browser should expose AXWebArea"
    labeled_buttons = [e for e in els if e["role"] == "AXButton" and (e.get("label") or "").strip()]
    editable = [e for e in els if e.get("editable") or e["role"] in ("AXTextField", "AXTextArea")]
    assert len(labeled_buttons) >= 3, f"web buttons should carry labels; got {len(labeled_buttons)}"
    assert editable, "web page should expose an editable field (e.g. a search / compose box)"
