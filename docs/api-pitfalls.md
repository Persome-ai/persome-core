# FastAPI + Apifox 踩坑指南

> 通用指南。适用于任何使用 FastAPI/Pydantic 构建 API 并同步到 Apifox 的项目。

---

## 一、FastAPI / Pydantic 层

### 1.1 Query 参数空值注入空字符串

**现象**

客户端发送 `?since&until` 或 `?paths=`，FastAPI 不会把它们当作 `None`，而是注入 `""`（空字符串）或 `[""]`（含空字符串的列表）。

**影响**

| 参数类型 | 注入值 | `is not None` 行为 | 实际后果 |
|---|---|---|---|
| `str \| None` | `""` | 为真，进入过滤逻辑 | 空字符串参与比较，结果异常 |
| `list[str] \| None` | `[""]` | 为真，进入循环 | SQL 拼接 `path GLOB ''`，永远无匹配 |

**修复**

字符串参数用 truthy check：

```python
# 错误
if since is not None:
    clauses.append("timestamp >= ?")
    args.append(since)

# 正确
if since:
    clauses.append("timestamp >= ?")
    args.append(since)
```

列表参数先过滤空值：

```python
if path_patterns:
    path_patterns = [p for p in path_patterns if p]
    if path_patterns:  # 过滤后可能为空
        for pat in path_patterns:
            ...
```

---

### 1.2 Pydantic v2 bool 参数拒绝空值

**现象**

`?include_superseded&include_archived`（空值）会导致 422。Pydantic v2 只接受 `true/1/yes/on` 和 `false/0/no/off`。

**修复**

代码侧无法绕过。在文档/示例中明确标注 bool 参数必须填 `true`/`false`，不要留空。

---

### 1.3 `Annotated[T, Query(default=...)]` 语法陷阱

**现象**

```python
# Pydantic v2 会抛 AssertionError
include_dormant: Annotated[bool, Query(False, description="...")]
```

**原因**

Pydantic v2 不允许在 `Annotated` 的 `Query()` 中设置默认值。默认值必须放在 `Annotated` 外部。

**正确写法**

```python
include_dormant: Annotated[bool, Query(description="...")] = False
```

---

### 1.4 FastAPI 自动生成 spec 太薄

**现象**

`app.openapi()` 生成的 spec 缺少：
- `servers`
- 字段 `description`（除非代码显式写了 `Field(description=...)`）
- request/response 示例
- 错误响应（`HTTPException` 不会自动生成对应 status code 的响应定义）

**修复**

写一个 enrich 脚本，在自动生成的 spec 上叠加：
- `servers`
- 响应 schema 内联展开（不用 `$ref`，见 2.1）
- 字段 `description`
- 命名 `examples`
- 每个 `HTTPException` 对应的错误响应

---

## 二、Apifox 层

### 2.1 `$ref` 响应的命名 examples 被丢弃

**现象**

响应 schema 用 `$ref` 引用 `components/schemas/Xxx` 时，Apifox 导入会丢弃命名的 `examples`（复数），降级为单数 `example` 挂在 schema 旁。结果是示例进不了「响应示例」标签页。

**修复**

导入前把响应 schema **内联展开**（dereference），绝不在响应里用 `$ref`。字段 `description` 不受影响，正常补即可。

---

### 2.2 UI「参数值」列显示的是 `example` 不是 `default`

**现象**

spec 中参数有 `default`，但 Apifox UI 的「参数值」列为空。因为该列读取的是 `example` 字段。

**修复**

enrich 时给每个参数同时注入 `default` 和 `example`：

```python
if "example" not in pschema and "default" in pschema:
    pschema["example"] = pschema["default"]
```

对时间参数等有意义的具体值，单独注入真实示例而非复制 `default`。

---

### 2.3 数组参数不要写 JSON 语法

**现象**

在 Apifox 的 Query 参数值中填 `["event-*.md"]`，实际发送的是字符串 `"[\"event-*.md\"]"`，而不是数组格式的 `?paths=event-*.md`。

**正确做法**

- 直接写 `event-*.md`，不要方括号和引号
- 多个值用 Apifox 的多值输入或批量编辑

---

### 2.4 导入/导出 base path 是 `/v1` 不是 `/api/v1`

**现象**

curl 用 `https://api.apifox.com/api/v1/...` 会 302 重定向到帮助页，看起来像「没反应」。

**正确 base**

```
https://api.apifox.com/v1/...
```

**必需 headers**

```
X-Apifox-Api-Version: 2024-03-28
Authorization: Bearer <token>
Content-Type: application/json
```

---

### 2.5 Token 权限失效

**现象**

导入返回 `403012 No project maintainer privilege`。

**原因**

Personal Access Token 因项目权限调整失效。

**修复**

去 Apifox 后台重新生成 token。

---

## 三、工作流层

### 3.1 curl 被 RTK 等 hook 拦截

**现象**

shell 配置了 RTK（Rust Token Killer）等安全工具，`curl` 命令会被拦截返回模板响应。

**修复**

用全路径调用：

```bash
/usr/bin/curl ...
```

---

### 3.2 Daemon / 服务运行的是安装版代码

**现象**

改了代码、测试通过，但 API 行为没变。

**原因**

daemon 从虚拟环境启动，运行的是 `pip install` 后的包，不是当前 dev 目录的源码。

**修复**

开发时安装 editable 包：

```bash
uv pip install --python /path/to/venv/bin/python -e /path/to/project
```

### 3.3 手动编辑长字典后 CI `ruff format --check` 报红

**现象**

手动编辑 `build_apifox_spec.py` 等包含大量内联字典/列表字面量的文件后，本地 `ruff check` 通过，但 push 后 CI 的 `ruff format --check` 步骤报红。

**原因**

`ruff check` 只检查代码质量和规则（I/E/W 等），**不检查格式**。`ruff format --check` 才会检查缩进、换行、行宽等格式问题。手动添加 `example` 值或嵌套 schema 时，很容易让某行超过 88 字符，或者字典项的逗号/引号对齐不一致。

**修复**

push 前必须跑完整的格式+检查：

```bash
uv run ruff format
uv run ruff check
```

如果 `ruff format` 修改了文件，需要重新 stage 并 amend commit：

```bash
git add -A
git commit --amend --no-edit
```

**注意**：`git add -A` 会把工作区所有未跟踪文件也加进去。如果只想加已跟踪的修改，用 `git add -u`。

---

## 四、Checklist

每次同步到 Apifox 前：

- [ ] `servers` 已配置
- [ ] 响应 schema 内联展开，不用 `$ref`
- [ ] 字段 `description` 已补全
- [ ] 参数约束（`ge`/`le`/默认值/是否必填）已带上
- [ ] 每个 `HTTPException` 已落成对应 status code 的错误响应
- [ ] 命名 `examples` 用复数形式 `examples`
- [ ] 参数同时有 `default` 和 `example`
