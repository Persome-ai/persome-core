"""Shared pytest fixtures. All tests operate on a tmp PERSOME_ROOT."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# Ensure src/ is on path when tests run from mutmut's mutants/ directory
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture
def ac_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "persome"
    root.mkdir()
    monkeypatch.setenv("PERSOME_ROOT", str(root))
    # Import paths after env var is set; also reset any cached modules
    from persome import paths
    from persome.evomem import shadow as evo_shadow

    paths.ensure_dirs()
    # The shadow-write miss counter is process-global (it tracks evo_nodes lag
    # in the live daemon). Reset per test so its threshold alert can't fire at
    # an order-dependent point and leak an integrity_alert publish into an
    # unrelated test's captured event stream.
    evo_shadow.reset_misses()
    return root


class FakeLLM:
    """Drop-in replacement for ``persome.writer.llm.call_llm``.

    Supports two modes:

    * **Scripted multi-turn** — ``add_script(stage, [resp1, resp2, ...])`` returns
      responses in FIFO order.  Used for classifier tool-call loops.
    * **Per-stage canned JSON** — ``set_default(stage, json_text)`` returns a
      single-turn response with that text as the content.  Used for reducer /
      timeline single-turn calls.

    Every invocation is recorded in ``calls`` for later assertion.
    """

    def __init__(self) -> None:
        self.scripts: dict[str, list[Any]] = {}
        self.defaults: dict[str, str] = {}
        self.calls: list[dict[str, Any]] = []

    def add_script(self, stage: str, responses: list[Any]) -> None:
        """Queue a list of response objects for ``stage``."""
        self.scripts[stage] = list(responses)

    def set_default(self, stage: str, text: str) -> None:
        """Set a canned JSON text response for ``stage``."""
        self.defaults[stage] = text

    def __call__(
        self,
        cfg: Any,
        stage: str,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        json_mode: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append({"stage": stage, "messages": messages, "tools": tools, "extra": extra})
        if stage in self.scripts and self.scripts[stage]:
            return self.scripts[stage].pop(0)

        from persome.writer.llm import _MOCK_DEFAULTS, _build_response

        text = self.defaults.get(stage) or _MOCK_DEFAULTS.get(stage, "")
        return _build_response(text)


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    """Monkeypatch ``call_llm`` with a ``FakeLLM`` instance for the test."""
    fake = FakeLLM()
    monkeypatch.setattr(
        "persome.writer.llm.call_llm",
        fake,
    )
    return fake


def _load_llm_fixture(stage: str, name: str) -> str:
    """Read a fixture JSON file from ``tests/fixtures/llm/<stage>/<name>.json``."""
    return (Path(__file__).parent / "fixtures" / "llm" / stage / f"{name}.json").read_text(
        encoding="utf-8"
    )


@pytest.fixture
def load_llm_fixture():
    """Return a callable ``(stage, name) -> str`` for loading LLM fixture JSON."""
    return _load_llm_fixture


def _load_capture_fixture(app: str, name: str) -> dict[str, Any]:
    """Read a capture JSON from ``tests/fixtures/captures/<app>/<name>.json``.

    Harvested real captures are team-local (gitignored — they carry real screen
    content); when one is absent the calling test skips instead of erroring, so
    a fresh public clone stays green while team machines run the full set."""
    import json

    path = Path(__file__).parent / "fixtures" / "captures" / app / f"{name}.json"
    if not path.exists():
        pytest.skip(f"team-local capture fixture not present: captures/{app}/{name}.json")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def load_capture_fixture():
    """Return a callable ``(app, name) -> dict`` for loading a capture JSON."""
    return _load_capture_fixture
