import json
from types import SimpleNamespace

from persome.evomem.schema_miner import SchemaMiner


def _resp(payload):
    msg = SimpleNamespace(
        content="```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```",
        tool_calls=[],
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason="stop")],
        usage=SimpleNamespace(total_tokens=0),
    )


def _fake(resp):
    def call(_messages):
        return resp

    return call


def test_mine_schema_parses_fenced_json():
    payload = {
        "central_proposition": "\u7528\u6237\u504f\u597d\u6781\u7b80\u5de5\u5177\u94fe",
        "supporting_summary": "\u591a\u6b21\u9009\u62e9 uv/ruff \u800c\u975e\u91cd\u578b\u6846\u67b6",
        "expected_inferences": [
            "\u4f1a\u62d2\u7edd\u5f15\u5165\u5927\u4f9d\u8d56",
            "\u504f\u597d\u547d\u4ee4\u884c",
        ],
        "confidence": 0.8,
    }
    miner = SchemaMiner(llm_call=_fake(_resp(payload)))
    r = miner.mine_schema(
        [
            "\u7528 uv \u7ba1\u4f9d\u8d56",
            "\u7528 ruff \u800c\u975e black+flake8",
            "\u62d2\u7edd litellm",
        ]
    )
    assert r.success is True
    assert r.central_proposition == "\u7528\u6237\u504f\u597d\u6781\u7b80\u5de5\u5177\u94fe"
    assert "\u4f1a\u62d2\u7edd\u5f15\u5165\u5927\u4f9d\u8d56" in r.expected_inferences
    assert r.confidence == 0.8


def test_mine_schema_handles_bad_json_gracefully():
    bad = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="not json", tool_calls=[]),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(total_tokens=0),
    )
    miner = SchemaMiner(llm_call=_fake(bad))
    r = miner.mine_schema(["x"])
    assert r.success is False
