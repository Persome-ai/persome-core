"""Pydantic models for the Persome HTTP REST API."""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

# ─── Generic envelope ──────────────────────────────────────────────────────


class ApiResponse(BaseModel):
    """松散信封：``data`` 形状不在契约里（``anyOf[object,array,string,null]``）。

    历史默认信封。新接口应改用 :class:`DataResponse` 携带显式 ``data`` 形状，
    让 ``openapi.json`` 把 payload schema 也纳入契约（drift 闸即升级为"形状闸"，
    见 ``DataResponse`` docstring 与 issue #539）。这里保留它仅为尚未迁移的
    存量接口兜底，迁移完成前不要删。
    """

    success: bool = Field(default=True, description="请求是否成功")
    data: dict | list | str | None = Field(default=None, description="响应数据")


_DataT = TypeVar("_DataT")


class DataResponse(BaseModel, Generic[_DataT]):
    """带形状的信封：``DataResponse[XxxResponse]`` 让 ``data`` 形状进入契约。

    与 :class:`ApiResponse` 的线上 JSON 字节完全一致（``{success, data}``），
    区别只在 ``openapi.json``：``data`` 不再是 ``anyOf[object,...]``，而是
    ``$ref`` 指向具体 schema。因此现有 ``tests/test_openapi_drift.py``（逐字节
    比对）自动升级为"响应体形状闸"——任何 ``XxxResponse`` 字段变更都会改动
    ``openapi.json``、被 drift 测试拦截，无需新增测试设施。app 端反序列化据此
    有了机器可校验的契约锚点（#539）。

    用法::

        @router.get("/runs", response_model=DataResponse[RunsResponse])
        def runs() -> DataResponse[RunsResponse]:
            return DataResponse(data=RunsResponse(...))
    """

    success: bool = Field(default=True, description="请求是否成功")
    data: _DataT = Field(description="响应数据（形状由具体类型参数声明）")


class ErrorResponse(BaseModel):
    success: bool = Field(default=False, description="请求是否成功")
    error: str = Field(description="错误信息")
    detail: str | None = Field(default=None, description="详细错误信息")


# ─── Status ────────────────────────────────────────────────────────────────


class ModelPing(BaseModel):
    stage: str = Field(description="LLM 阶段名称，如 timeline/reducer/classifier/compact")
    model: str = Field(description="使用的模型名称")
    ok: bool = Field(description="该阶段模型是否可连通")
    latency_ms: int | None = Field(default=None, description="ping 延迟（毫秒）")
    error: str | None = Field(default=None, description="ping 失败时的错误信息")


class StatusResponse(BaseModel):
    version: str = Field(description="Persome 版本号")
    root: str = Field(description="数据根目录路径")
    daemon: str = Field(description="守护进程状态，如 'running pid 12345' 或 'stopped'")
    uptime: str = Field(description="运行时长，如 '2h 30m' 或 'stopped'")
    health: str = Field(description="健康标签：healthy/stale/running/stopped")
    capture: str = Field(description="捕获状态：active/paused")
    last_capture: str | None = Field(default=None, description="最近一次捕获时间描述")
    buffer: str | None = Field(default=None, description="缓冲区文件统计")
    sessions: str | None = Field(default=None, description="会话统计")
    memory: str | None = Field(default=None, description="记忆文件统计")
    timeline: str | None = Field(default=None, description="时间线块统计")
    models: dict[str, ModelPing] | None = Field(
        default=None, description="各阶段 LLM 连通性探测结果"
    )


# ─── Memory ────────────────────────────────────────────────────────────────


class MemoryFile(BaseModel):
    path: str = Field(description="记忆文件路径")
    description: str = Field(description="文件描述")
    tags: list[str] = Field(description="标签列表")
    status: str = Field(description="文件状态，如 active/dormant/archived")
    entry_count: int = Field(description="条目数量")
    created: str = Field(description="创建时间 ISO8601")
    updated: str = Field(description="更新时间 ISO8601")


class MemoryEntry(BaseModel):
    id: str = Field(description="条目 ID")
    timestamp: str = Field(description="条目时间戳 ISO8601")
    tags: list[str] = Field(description="标签列表")
    body: str = Field(description="条目正文内容")
    superseded_by: str | None = Field(default=None, description="被哪个条目替代")
    confidence: str | None = Field(
        default=None, description="记忆可靠度：high/medium/low（元认知层，缺省为未标注）"
    )
    conflicted: bool = Field(default=False, description="是否与其他记忆冲突且未裁决")
    occurred_at: str | None = Field(
        default=None, description="事件实际发生时间 ISO8601（区别于写入时间 timestamp）"
    )


class MemoryReadResponse(BaseModel):
    path: str = Field(description="文件路径")
    description: str = Field(description="文件描述")
    tags: list[str] = Field(description="标签列表")
    status: str = Field(description="文件状态")
    updated: str = Field(description="更新时间")
    entry_count: int = Field(description="条目数量")
    entries: list[MemoryEntry] = Field(description="条目列表")


# ─── Search ────────────────────────────────────────────────────────────────


class SearchHit(BaseModel):
    id: str = Field(description="条目 ID")
    path: str = Field(description="所属文件路径")
    timestamp: str = Field(description="时间戳 ISO8601")
    content: str = Field(description="匹配内容片段")
    rank: float = Field(description="BM25 相关性得分")


class CaptureHit(BaseModel):
    timestamp: str = Field(description="捕获时间戳 ISO8601")
    app_name: str = Field(description="应用名称")
    bundle_id: str = Field(description="应用 bundle ID")
    window_title: str = Field(description="窗口标题")
    url: str = Field(description="URL（如果有）")
    snippet: str = Field(description="匹配文本片段")
    rank: float = Field(description="BM25 相关性得分")
    file_stem: str = Field(description="捕获文件名（不含扩展名）")
    focused_role: str = Field(description="焦点元素角色")
    focused_value_preview: str = Field(description="焦点元素值预览（前 200 字符）")


# ─── Captures ──────────────────────────────────────────────────────────────


class CaptureHeadline(BaseModel):
    time: str = Field(description="捕获时间 HH:MM")
    app_name: str = Field(description="应用名称")
    window_title: str = Field(description="窗口标题")
    focused_role: str = Field(description="焦点元素角色")
    file_stem: str = Field(description="捕获文件名")


class CaptureFulltext(BaseModel):
    timestamp: str = Field(description="捕获时间戳 ISO8601")
    app_name: str = Field(description="应用名称")
    window_title: str = Field(description="窗口标题")
    url: str = Field(description="URL（如果有）")
    focused_role: str = Field(description="焦点元素角色")
    focused_value: str | None = Field(description="焦点元素值")
    visible_text: str = Field(description="可见文本内容")
    file_stem: str = Field(description="捕获文件名")


class TimelineBlock(BaseModel):
    start_time: str = Field(description="块起始时间 ISO8601")
    end_time: str = Field(description="块结束时间 ISO8601")
    entries: list[str] = Field(description="时间线条目列表（LLM 归一化后的结构化内容）")
    apps_used: list[str] = Field(description="该时段使用的应用列表")
    capture_count: int = Field(description="该时段捕获次数")


class CurrentContextResponse(BaseModel):
    recent_captures_headline: list[CaptureHeadline] = Field(description="最近捕获摘要列表")
    recent_captures_fulltext: list[CaptureFulltext] = Field(description="最近捕获全文列表")
    recent_timeline_blocks: list[TimelineBlock] = Field(description="最近时间线块列表")


class RecentCaptureResponse(BaseModel):
    timestamp: str | None = Field(default=None, description="捕获时间戳 ISO8601")
    file: str = Field(description="捕获文件名")
    app_name: str | None = Field(default=None, description="应用名称")
    bundle_id: str | None = Field(default=None, description="应用 bundle ID")
    window_title: str | None = Field(default=None, description="窗口标题")
    url: str | None = Field(default=None, description="URL")
    focused_element: dict = Field(description="焦点元素信息")
    visible_text: str = Field(description="可见文本内容")
    screenshot_stripped: bool = Field(description="截图是否已剥离")
    screenshot_b64: str | None = Field(default=None, description="截图 base64")
    screenshot_mime: str | None = Field(default=None, description="截图 MIME 类型")


# ─── Actions ───────────────────────────────────────────────────────────────


class ActionItem(BaseModel):
    id: int = Field(description="行动项 ID")
    kind: str = Field(description="行动项类型")
    status: str = Field(description="状态：proposed/done/dismissed")
    confidence: float = Field(description="置信度 0-1")
    when_text: str = Field(description="时间描述文本")
    with_: list[str] = Field(alias="with", description="相关人或渠道")
    channel: str = Field(description="渠道")
    rationale: str = Field(description="推理依据")
    source_block_ids: list[str] = Field(description="来源时间块 ID 列表")
    created_at: str | None = Field(default=None, description="创建时间 ISO8601")

    class Config:
        populate_by_name = True


class MarkActionBody(BaseModel):
    action: str = Field(
        default="done", description="操作类型：done（标记完成）或 dismissed（标记忽略）"
    )
    note: str = Field(default="", description="备注说明")


class SetIntentStatusBody(BaseModel):
    status: str = Field(
        description="意图状态：open（待处理）/ consumed（已采纳）/ dismissed（已忽略）"
    )


class OutcomeBody(BaseModel):
    """反向闭环 G4（spec 2026-06-26 §3.1.2）：一条执行结果回写。**content-free 红线**——
    只含枚举/布尔/计数/时长，零屏幕文本/产物正文/秘密。app 的 FollowUp/supervised 收尾时发。

    ``extra="forbid"``：把 content-free 红线钉在 API 边界——任何未声明字段（可能夹带屏幕
    文本/产物正文）直接 422 拒绝，而非被 Pydantic 默默丢弃。"""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(description="意图 kind（meeting/calendar/reminder/...）")
    status: str = Field(description="哪个执行器产出：followup | supervised")
    success: bool = Field(description="是否真正做成了用户采纳的事")
    intent_id: int | None = Field(default=None, description="服务的意图行 id（无则 null）")
    executor_tier: str | None = Field(default=None, description="能力档位标签（枚举）")
    artifact_verified: bool | None = Field(default=None, description="产物已校验（FollowUp）")
    placed: bool | None = Field(default=None, description="已粘入聚焦字段（永不发送）")
    awaited_confirm: bool | None = Field(default=None, description="曾暂停等用户确认")
    reschedule_suggested: bool | None = Field(default=None, description="提议了改期")
    elapsed_ms: int | None = Field(default=None, description="耗时 ms")


class MemoryArtifact(BaseModel):
    """One produced artifact reference. Only ``type`` is stored durably; ``url`` is
    ephemeral/leak-prone and is dropped at ingest (宁缺毋滥)."""

    type: str = Field(description="产物类型枚举（meeting_link/document/draft/...）")
    url: str | None = Field(default=None, description="产物链接（不落库，仅传递）")


class MemoryIngestBody(BaseModel):
    """反向闭环 G1（spec 2026-06-26 §3.1.3）：**唯一**允许带内容的反向通道。app 侧已
    蒸馏脱敏的 task-outcome ``summary`` 灌入记忆，落 ``task-outcome-*.md``（evo_nodes 豁免）。
    隐私分级最严：summary 已在 app 侧红线过滤，daemon 侧再过一道 ``privacy.scrub``，
    命中即整条丢弃（宁缺毋滥）。按 ``task_id`` 幂等。``extra="forbid"`` 钉死字段集。"""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(default="task-outcome", description="记忆类（恒为 task-outcome）")
    task_id: str = Field(description="app 任务 UUID — 幂等键")
    title: str = Field(description="app 蒸馏的简短标题")
    summary: str = Field(description="app 蒸馏脱敏后的简短结构化产物正文")
    intent_id: int | None = Field(default=None, description="服务的意图行 id（无则 null）")
    artifacts: list[MemoryArtifact] | None = Field(default=None, description="产物引用（仅类型落库）")
    ts: str | None = Field(default=None, description="ISO8601 完成时刻")


class CaptureIngestBody(BaseModel):
    """Swift "Persome" 主程序推送的一帧 capture（``capture.source = "ingest"`` 模式）。

    采集层（AX 树 + 焦点窗口截图）已搬进持有 Accessibility / Screen-Recording 的 Swift
    进程；daemon 收到后只跑富化→落库→意图快路 hook，自身不再需要任何系统权限。字段与
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


class IntentItem(BaseModel):
    """``/intents`` 单条意图（``intent.ontology.Intent.to_dict()`` 的契约镜像）。

    稳定的信封字段显式声明形状；``kind`` 是 OPEN 字符串（场景包可新增，见
    ontology），``payload``/``fire_config``/``evidence`` 是有意开放的结构，
    故保持松散 dict/list——契约只锁住"哪些字段一定在、是什么标量类型"，不谎称
    payload 是闭集。新增稳定字段时这里同步即触发 drift 闸。
    """

    kind: str = Field(description="意图类型（OPEN：meeting/calendar/reminder/assignment/…）")
    scope: str = Field(description="所属场景 id：timeline / <meeting-id> / session-<id> / …")
    confidence: float = Field(description="识别置信度 0-1")
    rationale: str = Field(description="识别依据")
    status: str = Field(description="open / armed / consumed / dismissed / expired")
    ts: str = Field(description="识别时间戳 ISO8601")
    payload: dict = Field(description="kind 相关结构化字段（开放，如 when_text/with/channel）")
    evidence: list[dict] = Field(description="溯源证据列表（source/ref_id/entry_index/quote）")
    id: int | None = Field(default=None, description="持久化行 id；未落库为 null")
    fire_on: str = Field(default="", description="休眠触发事件键（L7）；空串=即时意图")
    fire_config: dict = Field(default_factory=dict, description="触发参数（开放）")
    fired_at: str | None = Field(default=None, description="触发时间 ISO8601；未触发为 null")
    schema_sources: list[str] = Field(
        default_factory=list, description="识别上下文中在场的 schema-*.md 文件名（共现归因）"
    )
    resolved_at: str | None = Field(default=None, description="承诺时点 ISO8601；不可解析为 null")
    valid_until: str | None = Field(default=None, description="过期时点 ISO8601；不可解析为 null")


class IntentsResponse(BaseModel):
    """``/intents`` 响应体：意图列表 + 计数。"""

    intents: list[IntentItem] = Field(description="按 ts 倒序（最新在前）的意图列表")
    count: int = Field(description="intents 数量")


class RecallPackItem(BaseModel):
    """``/recall/pack`` 单条结构化召回事实（``intent.recall.RecallItem`` 的契约镜像）。

    供主动任务 prompt 注入：``content`` 是干净片段，``cite`` 是可追溯句柄
    （``mem:<path>`` / ``schema:<file>`` / ``intent:<id>`` / ``block:<id>``）。
    ``capture_stem`` / ``timeline_block_id`` 是 scene/timeline 项的 RAW 捕获句柄
    （stem 同时索引截图与 axtree；仅回句柄字符串，绝不内联字节）。"""

    layer: str = Field(
        description="召回层：schema/behavior/fact/semantic/scene_intent/event/timeline"
    )
    content: str = Field(description="干净片段（无 [path] 前缀）")
    cite: str = Field(description="可追溯引用句柄")
    score: float | None = Field(default=None, description="语义余弦或意图置信度；无则 null")
    confidence: str | None = Field(
        default=None, description="记忆可靠度：low（仅标低置信）；否则 null"
    )
    conflicted: bool = Field(default=False, description="该记忆是否冲突未裁决")
    capture_stem: str | None = Field(default=None, description="截图/axtree 捕获 stem；无则 null")
    timeline_block_id: int | None = Field(
        default=None, description="时间线块 id（慢路）；无则 null"
    )


class RecallPackResponse(BaseModel):
    """``/recall/pack`` 响应体：分层带引用的召回项 + 预算/稠密/计数元信息。"""

    scope: str = Field(description="召回所属场景 id")
    intent_id: int | None = Field(default=None, description="按意图召回时的行 id；否则 null")
    items: list[RecallPackItem] = Field(description="结构化召回项（按层优先级排序）")
    counts: dict = Field(description="各层命中计数")
    budget: dict = Field(description="预算口径：max_chars/used/squeezed")
    dense: dict = Field(description="稠密层状态：enabled（配置开关）/active（实际触发）")


class CorrectWorkThreadBody(BaseModel):
    """WorkThread 纠错闭集（spec 2026-06-12 §六-3；HUD chip 的零成本开关）。"""

    action: str = Field(
        description="纠错动作闭集：confirm（划分是对的）/ not_this（不是一条真实的线）/ "
        "rename（改名，配 rename 字段）/ merge（两条是一件事，配 into_id）/ "
        "pin（人工确认线：免疫 merge 吸收与 stale 收割）"
    )
    rename: str = Field(default="", description="action=rename 时的新标题")
    into_id: str = Field(default="", description="action=merge 时的吸收方 thread id")


# ─── Book pages ──────────────────────────────────────────────────────────────


class BookPageItem(BaseModel):
    id: str = Field(description="书页 ID（= 文件名 stem，如 page-2026-07-08）")
    title: str = Field(description="书页标题")
    date: str = Field(description="该页对应的日期 YYYY-MM-DD")
    kind: str = Field(description="memory 类型，恒为 book_page")
    is_draft: bool = Field(description="是否为草稿（未 Review）")
    source_refs: list[str] = Field(default_factory=list, description="来源引用列表")


class BookPageDetail(BaseModel):
    id: str = Field(description="书页 ID")
    title: str = Field(description="书页标题")
    date: str = Field(description="该页对应的日期 YYYY-MM-DD")
    is_draft: bool = Field(description="是否为草稿（未 Review）")
    body: list[str] = Field(description="正文段落数组（按空行切段）")


class ReviewBody(BaseModel):
    reviewed: bool = Field(description="是否标记为已 Review（去草稿横幅）")


# ─── Agent now ───────────────────────────────────────────────────────────────


class AgentSubStatus(BaseModel):
    text: str = Field(description="一行子状态描述，源自真实信号（dream 事件 / 最近活动）")
    ts: str | None = Field(default=None, description="该子状态对应的时间戳 ISO8601（如可得）")


class AgentNowResponse(BaseModel):
    title: str = Field(description="当前 agent 正在做什么的标题，源自真实状态")
    status: str = Field(description="运行态：running（有 dream 在跑）/ idle（空闲快照）")
    started_at: str | None = Field(
        default=None,
        description="running 时为当前 dream 的 started_at ISO8601，app 据此自走计时器；idle 为 null",
    )
    elapsed_seconds: int | None = Field(
        default=None,
        description="running 时已运行秒数（基于 started_at 服务端算一次）；idle 为 null",
    )
    capture: str = Field(description="捕获状态：active / paused / stopped")
    last_activity_ts: str | None = Field(
        default=None, description="最近一次屏幕捕获的时间戳 ISO8601；无则 null"
    )
    sub_status: list[AgentSubStatus] = Field(
        default_factory=list,
        description="最多 3 条子状态行，全部源自真实信号；无来源时省略对应行（不编造）",
    )


# ─── Agenda ────────────────────────────────────────────────────────────────


class AgendaItem(BaseModel):
    time_label: str = Field(description="时间描述文本，源自意图 payload 的 when_text（自然语言）")
    title: str = Field(description="日程标题，源自意图 rationale / kind 摘要")
    kind: str = Field(description="意图类型，如 meeting / calendar / reminder")
    ts: str = Field(description="该意图被识别的时间戳 ISO8601")
    source: str = Field(description="数据来源标识，如 intent")
    with_: list[str] = Field(
        default_factory=list, alias="with", description="相关人/参与者，源自 payload.with"
    )

    class Config:
        populate_by_name = True


class AgendaResponse(BaseModel):
    range: str = Field(description="查询范围：today / week")
    items: list[AgendaItem] = Field(
        default_factory=list, description="按识别时间倒序的日程项；无真实数据则为空列表"
    )
    count: int = Field(description="items 数量")


class RunCard(BaseModel):
    id: int = Field(description="行 id（在其 source 表内唯一）")
    source: str = Field(description="来源表：'agent_run' | 'dream'")
    kind: str = Field(description="run 类型：dream / bootstrap / <executor>")
    title: str = Field(description="人话标签")
    status: str = Field(description="queued / running / committed / skipped / failed / cancelled")
    trigger: str = Field(description="派发来源：manual / daily-tick / user / chat")
    enqueued_at: str = Field(description="入队时刻 ISO8601（队列卡时间锚）")
    started_at: str | None = Field(default=None, description="开跑时刻；NULL = 仍 queued")
    ended_at: str | None = Field(default=None, description="结束时刻")
    progress: float | None = Field(
        default=None, description="0..1 真实进度；NULL = 不定态（绝不编造）"
    )
    progress_label: str = Field(default="", description="真实子步骤行")
    summary: str = Field(default="", description="结果摘要")


class RunsResponse(BaseModel):
    range: str = Field(description="查询范围：day / week / month")
    items: list[RunCard] = Field(default_factory=list, description="按锚点时间倒序")
    count: int = Field(description="items 数量")


# ─── Config ────────────────────────────────────────────────────────────────


class ConfigResponse(BaseModel):
    config: dict = Field(description="完整配置对象（嵌套结构，含 models/capture 等）")


# ─── Agent runs write-side (Phase 1b) ────────────────────────────────────────


class CreateRunRequest(BaseModel):
    kind: str = Field(description="run 类型：dream / bootstrap")
    title: str = Field(default="", description="人话标签（可选；空串时 registry 给默认）")
    payload: dict = Field(default_factory=dict, description="executor 参数（可选）")


class PatchRunRequest(BaseModel):
    action: str = Field(description="当前只支持 cancel")


class RunEventItem(BaseModel):
    id: int = Field(description="事件行 id")
    ts: str = Field(description="事件时间戳 ISO8601")
    type: str = Field(description="事件类型：progress / stage_start / stage_end / …")
    payload: dict = Field(description="事件 payload")


class RunDetailResponse(BaseModel):
    id: int = Field(description="run id")
    kind: str = Field(description="run 类型")
    title: str = Field(description="人话标签")
    status: str = Field(description="queued / running / committed / skipped / failed / cancelled")
    trigger: str = Field(description="派发来源")
    dispatch_source: str = Field(description="dispatch 来源")
    enqueued_at: str = Field(description="入队时刻 ISO8601")
    started_at: str | None = Field(default=None, description="开跑时刻；NULL = 仍 queued")
    ended_at: str | None = Field(default=None, description="结束时刻")
    progress: float | None = Field(default=None, description="0..1 真实进度")
    progress_label: str = Field(default="", description="子步骤描述")
    summary: str = Field(default="", description="结果摘要")
    error: str = Field(default="", description="失败原因")
    events: list[RunEventItem] = Field(default_factory=list, description="事件列表（按时间正序）")
