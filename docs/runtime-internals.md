# Runtime internals（运行时隐形知识）

这份文档记录"踩了才知道"的运行时行为。代码里能看到的东西不在这里。

排错触发条件 / actionable 诊断步骤 → 这里只放 reference。

---

## 1. LLM key 存在哪

**运行时单一来源**：`~/.persome/env`（dotenv 格式，chmod 0600）

- App 端：`EnvVault`（`lib/core/service/env_vault.dart`）读写这个文件
- Daemon 端：`cli.py:start` 在 fork 前 `load_env_file()` 读这个文件，再合并进 `os.environ`
- 两端共享同一个文件，没有 Keychain 副本，没有 App Container 副本

**覆盖路径**：设置 `PERSOME_ROOT` env var → 用 `$PERSOME_ROOT/env` 替代 `~/.persome/env`。`EmbeddedDaemonService` 启动 daemon 时会透传这个 env var。

**Env 页 Save 触发的链路**：

```
用户填 key → EnvVault.writeAll() → 原子写 ~/.persome/env
           → EmbeddedDaemonService.restart()
           → daemon stop + 等健康检查失效 + daemon start
           → 新 daemon fork 读新 key → chat 生效
```

**构建时 env 如何嵌入 .app**（与运行时分离，不要混淆）：

- 构建阶段 `xcode_embed_python.sh` 读**仓库根 `.env`**，写入 `.app/Contents/Resources/env.default`
- 每次启动，`EnvVault.applyBundleDefaults()` 把 `env.default` 里缺失的 key 补入 `~/.persome/env`
- 用户已有的值不会被覆盖

---

## 2. Auth token 存在哪

`flutter_secure_storage` + macOS legacy file keychain（**不是** Data Protection Keychain）：

```dart
// lib/core/provider/secure_storage.dart
const bool _kSignedRelease =
    bool.fromEnvironment('SIGNED_RELEASE', defaultValue: false);
const _kMacOsOptions = MacOsOptions(
  usesDataProtectionKeychain: _kSignedRelease,
);
```

- **ad-hoc 签名（团队内部 .app）**：`_kSignedRelease = false` → legacy file keychain → 首次弹一次授权对话框，之后正常
- **Developer ID 签名（正式发布）**：需要 `--dart-define=SIGNED_RELEASE=true` 构建 → Data Protection Keychain

### 常见症状与根因

| 症状 | 根因 | 解法 |
|---|---|---|
| `pkill persome` 后重开要填邀请码 | legacy file keychain 需要用户授权，授权弹窗被忽视或错误的 Keychain 模式 | 确认用 ad-hoc 签名的 .app（不要用 `--dart-define=SIGNED_RELEASE=true`） |
| `usesDataProtectionKeychain: true` + ad-hoc 签名 = 静默失败 | `-34018 errSecMissingEntitlement`，不 throw 只是每次读空 | 换成 `false`（默认） |
| 授权弹窗提示旧版(legacy)应用想使用钥匙串 | 首次使用 legacy keychain 正常提示 | 点"始终允许"，只出现一次 |

---

## 3. Daemon 生命周期

**owner = launchd（issue #194）**：bundled `.app` 运行时，daemon 由 macOS LaunchAgent
（label `com.persome.runtime`，plist 落 `~/Library/LaunchAgents/`）托管，生命周期
**独立于 App**——App crash / 退出都不影响 daemon。

- **注册**：App 启动时 `EmbeddedDaemonService.ensureStarted()` 调 `persome launchagent install`，
  写 plist 并 `launchctl bootstrap`。该命令会先 `bootout` 旧 job 再用当前 binary 路径重载，
  所以 `.app` 升级后 daemon 干净切换；上次 crash 残留的 orphan 也被替换。
- **launchd 拉起**：plist `RunAtLoad=true` + `KeepAlive=true`——登录即启动，进程退出（crash /
  `stop` / OOM）launchd 自动重启。这是 launchd 原生版的 auto-restart（#170）。
- **重启**：App 的 `restart()` 走 `launchctl kickstart -k`（让 launchd 重启自己的 job），
  不再自己 spawn，避免和 `KeepAlive` 抢。
- **关闭 App 不会停 daemon**：intentional——launchd 拥有生命周期。`main.dart` 的退出 hook
  仅在**非 launchd（dev/legacy fallback）**模式才 stop。
- **显式停止**：App Backend 页面 "Stop" 按钮 → launchd 模式下走 `launchagent uninstall`
  （bootout + 删 plist，否则 `KeepAlive` 会立刻重拉）；命令行 `persome launchagent uninstall`。
- **日志**：launchd 把 daemon 的 stdout/err 写到 `~/.persome/logs/launchd.{out,err}.log`，
  和其它 per-component log 一起被 diagnostic bundle（#168）收集。
- **legacy / dev fallback**：`launchctl` 不可用（部分 CI sandbox）或 agent 加载失败时，回退到
  原来的 `persome start` 双 fork（`os.fork()` in `cli.py`）+ PID 文件路径。`flutter run`
  无 bundled binary → 纯 no-op，需手动 `persome start`。
- **PID 文件**：daemon 自己管 `~/.persome/persome.pid`；`stop`（legacy 路径）向 PID 发信号。
  launchd 模式不依赖它。

### daemon 僵尸进程诊断与清理

PID 文件残留（daemon crash 后没清）会让 CLI `persome start` 认为 daemon 已在运行而拒绝启动：

```bash
# 症状：start 报 "daemon already running"，但 curl health 无响应
cat ~/.persome/persome.pid
kill -0 "$(cat ~/.persome/persome.pid)" 2>/dev/null && echo "alive" || echo "stale PID"

# 清理
rm ~/.persome/persome.pid
persome start
curl -s http://127.0.0.1:8742/health
```

注意：launchd 模式下 daemon 由 launchd 拉起，不依赖 PID 文件，所以从 App 启动一般不受
stale PID 影响。只有 legacy fallback / 纯 CLI `persome start` 路径才会被残留 PID 误导。

launchd 模式排查：

```bash
launchctl print "gui/$(id -u)/com.persome.runtime"   # job 状态 + 最近退出码
persome launchagent status                            # 简版：plist + loaded?
tail -f ~/.persome/logs/launchd.err.log               # daemon stderr
```

### daemon 启动时的 env 继承

`EmbeddedDaemonService._systemEnvWhitelist()` 只透传：

```
PATH, HOME, USER, LOGNAME, SHELL, LANG, LC_ALL, LC_CTYPE, TMPDIR, PERSOME_ROOT
```

LLM key **不**从 App 的进程 env 透传——它们全部走文件路径（`~/.persome/env`）。GUI 启动和 CLI `persome start` 行为一致。

---

## 4. 构建时 vs 运行时（最容易搞混）

| | 构建时 | 运行时 |
|---|---|---|
| **文件** | 仓库根 `.env`（git-ignored） | `~/.persome/env`（用户 home） |
| **谁读** | `xcode_embed_python.sh`（打包脚本） | `EnvVault`（Dart App）+ `cli.py`（Python daemon） |
| **目的** | 把 key 嵌入 `.app/Contents/Resources/env.default` | 让 App UI 和 daemon 共享同一份 key |
| **关系** | 构建时 `.env` → bundle `env.default` → 启动时补入 `~/.persome/env` | `~/.persome/env` 是运行时 SoT |

---

## 5. 数据目录总览（`~/.persome/`）

| 文件/目录 | 内容 | 谁写 |
|---|---|---|
| `env` | LLM key（dotenv，chmod 600） | App via EnvVault + daemon startup |
| `persome.pid` | daemon PID | daemon |
| `index.db` | SQLite 主库（会话、事件、记忆索引；全表结构见 [`db-schema.sql`](db-schema.sql)） | daemon |
| `captures/` | AX 截图缓冲区 | daemon |
| `memory/` | 压缩记忆 Markdown | daemon |

`PERSOME_ROOT` 可以整体重定向到另一个目录（测试用）。

---

## 6. 快速诊断清单

```bash
# 1. daemon 是否在跑？
curl -s http://127.0.0.1:8742/health

# 2. key 是否已写入运行时文件？
cat ~/.persome/env | sed 's/=.*/=<redacted>/'

# 3. bundle default 有没有嵌入？
cat dist/Persome.app/Contents/Resources/env.default | sed 's/=.*/=<redacted>/'

# 4. auth token 在哪？（legacy file keychain 路径）
security find-generic-password -s "flutter_secure_storage" -a "BEARER_TOKEN" 2>/dev/null && echo "found"

# 5. daemon log（最近 50 行）
tail -50 ~/.persome/persome.log 2>/dev/null || echo "no log"
```
