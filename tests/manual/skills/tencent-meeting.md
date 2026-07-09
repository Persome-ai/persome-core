# 技能：腾讯会议（com.tencent.meeting）· 预约会议

腾讯会议主窗口是腾讯自绘框架（WeMeetFramework）：**首页的大图标按钮是像素图，AX 读不到**；
但「预定会议」表单一旦打开，AX 树就完整可读写（懒加载——内容出现才建树）。所以策略是 **OCR 只用于首页那一下，表单内全用 AX**。

## 建会流程
1. `activate("meeting")`。
2. 首页「预定会议」是像素按钮 → `ocr_locate("预定会议")` 拿屏幕坐标 → `clickxy` 打开表单。
   （表单弹出后，clickxy 的 diff 会列出 `+[N]` 新出现的可操作元素，直接用这些编号，不必再 snapshot。）
3. **主题**：在 diff/snapshot 里找 `AXTextField`（占位「请输入会议主题」）→ `ax_set_value index=N text="<会议主题>"`。
4. **提交**：在动作返回的「当前可操作元素」里找 `AXButton "预定"`（底部那个）→ `ax_press index=N`。
5. **拿链接**：press 预定后**立刻直接调 `get_meeting_link("meeting")`**。结果页整段邀请（含
   `https://meeting.tencent.com/…`）是 AX 可读文本，工具会自己读出链接进剪贴板，**并且会自动处理任何残留弹窗
   （如「成员提前入会…我知道了」）后重试**。所以**提交后什么都不用查、不用 ocr、不用找按钮——直接 get_meeting_link**。
   不要去点像素的「复制全部信息」按钮。

## 坑
- 时间下拉是像素绘制（无 AX）；除非必须改时间，否则用默认值，别和它纠缠。
- AX `ax_set_value`/`ax_press` 不移动光标、不抢焦点，可后台静默执行；只有首页那一下 `clickxy` 会动一下光标（点完会自动弹回）。
