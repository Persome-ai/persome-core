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
        "central_proposition": "用户偏好极简工具链",
        "supporting_summary": "多次选择 uv/ruff 而非重型框架",
        "expected_inferences": ["会拒绝引入大依赖", "偏好命令行"],
        "confidence": 0.8,
    }
    miner = SchemaMiner(llm_call=_fake(_resp(payload)))
    r = miner.mine_schema(["用 uv 管依赖", "用 ruff 而非 black+flake8", "拒绝 litellm"])
    assert r.success is True
    assert r.central_proposition == "用户偏好极简工具链"
    assert "会拒绝引入大依赖" in r.expected_inferences
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
