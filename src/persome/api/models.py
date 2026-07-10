"""Pydantic models for the Persome HTTP REST API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# ─── Generic envelope ──────────────────────────────────────────────────────


class ApiResponse(BaseModel):
    """Common response envelope for the local runtime API."""

    success: bool = Field(default=True, description="请求是否成功")
    data: dict | list | str | None = Field(default=None, description="响应数据")


class ModelPing(BaseModel):
    stage: str = Field(description="LLM 阶段名称，如 timeline/reducer/classifier/compact")
    model: str = Field(description="使用的模型名称")
    ok: bool = Field(description="该阶段模型是否可连通")
    latency_ms: int | None = Field(default=None, description="ping 延迟（毫秒）")
    error: str | None = Field(default=None, description="ping 失败时的错误信息")


class CaptureIngestBody(BaseModel):
    """Swift "Persome" 主程序推送的一帧 capture（``capture.source = "ingest"`` 模式）。

    采集层（AX 树 + 焦点窗口截图）已搬进持有 Accessibility / Screen-Recording 的 Swift
    进程；daemon 收到后只跑富化与落库，自身不再需要任何系统权限。字段与
    daemon 自采路径 ``_build_capture`` 的产物对齐，缺省宽松（缺字段降级，不崩）。
    """

    timestamp: str | None = Field(default=None, description="ISO8601 采集时间；缺省则服务端补")
    trigger: dict[str, Any] | None = Field(
        default=None, description="触发元数据 {event_type, bundle_id, window_title, details?}"
    )
    window_meta: dict[str, Any] = Field(
        default_factory=dict, description="{app_name, title, bundle_id}"
    )
    ax_tree: dict[str, Any] | None = Field(
        default=None, description="AX 树 raw_json（apps→windows→elements），缺省视为 AX 不可用"
    )
    ax_metadata: dict[str, Any] | None = Field(default=None, description="AX 采集元数据")
    screenshot: dict[str, Any] | None = Field(
        default=None,
        description="{image_base64, mime_type, width, height}（明文，由服务端按配置加密落盘）",
    )
    ocr_jpeg_b64: str | None = Field(
        default=None, description="AX 贫瘠窗口的焦点窗口截图 JPEG（base64），供本地 OCR 兜底"
    )
    ocr_tier: str | None = Field(default=None, description="OCR 档位覆盖；缺省用配置值")
