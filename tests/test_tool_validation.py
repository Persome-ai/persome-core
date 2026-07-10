"""Tests for F7: tool input schema validation via Pydantic."""

from __future__ import annotations

from persome.writer.tools_schema import TOOL_INPUT_MODELS, AppendInput, DrillCaptureInput

# ──────────────────────────────────────────────────────────────────────────
# Test 1: append rejects missing required 'path' field
# ──────────────────────────────────────────────────────────────────────────


def test_append_rejects_missing_path():
    from pydantic import ValidationError

    try:
        AppendInput.model_validate({"content": "some text"})
        raise AssertionError("should have raised ValidationError")
    except ValidationError as exc:
        assert exc.error_count() >= 1


def test_append_rejects_missing_content():
    from pydantic import ValidationError

    try:
        AppendInput.model_validate({"path": "user-test.md"})
        raise AssertionError("should have raised ValidationError")
    except ValidationError:
        pass


# ──────────────────────────────────────────────────────────────────────────
# Test 2: drill_capture rejects bad text_limit
# ──────────────────────────────────────────────────────────────────────────


def test_drill_capture_rejects_bad_text_limit():
    from pydantic import ValidationError

    try:
        DrillCaptureInput.model_validate({"capture_id": "abc", "text_limit": -1})
        raise AssertionError("should have raised ValidationError")
    except ValidationError:
        pass


def test_drill_capture_rejects_missing_capture_id():
    from pydantic import ValidationError

    try:
        DrillCaptureInput.model_validate({"text_limit": 500})
        raise AssertionError("should have raised ValidationError")
    except ValidationError:
        pass


# ──────────────────────────────────────────────────────────────────────────
# Test 3: valid input passes through without error
# ──────────────────────────────────────────────────────────────────────────


def test_valid_append_passes():
    validated = AppendInput.model_validate(
        {
            "path": "user-test.md",
            "content": "# Test\n\nSome content",
            "tags": ["test"],
        }
    )
    assert validated.path == "user-test.md"
    assert validated.tags == ["test"]


def test_valid_drill_capture_passes():
    validated = DrillCaptureInput.model_validate(
        {
            "capture_id": "20260519T120000",
            "text_limit": 3000,
        }
    )
    assert validated.text_limit == 3000


# ──────────────────────────────────────────────────────────────────────────
# Test 4: TOOL_INPUT_MODELS contains expected entries
# ──────────────────────────────────────────────────────────────────────────


def test_tool_input_models_registry():
    required = {
        "append",
        "create",
        "supersede",
        "flag_compact",
        "read_memory",
        "search_memory",
        "drill_capture",
        "commit",
    }
    assert required.issubset(TOOL_INPUT_MODELS.keys())
