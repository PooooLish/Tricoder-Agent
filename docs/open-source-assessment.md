# 开源项目评估

调研日期：2026-07-30。结论不构成法律或安全保证。

| 候选 | 审查版本/状态 | 许可证 | 维护与安全信号 | 技术适配与成本 | 复用边界 |
|---|---|---|---|---|---|
| [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) | v2.3.0；2026-05 发布 | MIT | 活跃发布，提供安全策略；主打极简线性循环 | Python、CLI 和线性历史高度适配 | 仅参考“模型—命令—结果回填”的高层思想，不复制代码 |
| [Aider](https://github.com/Aider-AI/aider) | v0.86.0；2025-08 发布 | Apache-2.0 | 成熟项目、发布较多；仓库未显示 GitHub 安全公告入口 | Python CLI、代码上下文和 diff 交互有参考价值，但整体较大 | 仅参考终端交互与变更展示，不复制代码 |
| [OpenCode](https://github.com/anomalyco/opencode) | v1.17.7；2026-06 发布 | MIT | 高活跃度并有 SECURITY.md；大型权限系统仍持续修复 | TypeScript 大型单体仓库，远超本 MVP | 仅参考 Provider/工具/权限分层，不复制代码 |
| [OpenHands](https://github.com/OpenHands/openhands) | v1.8.0；2026-06 发布 | 核心 MIT；`enterprise/` 另有许可证 | 成熟且活跃；授权边界需特别注意 | Python/TypeScript、多服务与容器架构过重 | 不参考企业目录；仅了解完整 Agent 平台边界 |

## 决策

选择 `reference`：独立实现 Python 标准库 MVP，只借鉴公开文档中的高层架构思想，不克隆、下载、复制、改写或集成候选仓库代码。

理由：

- `greenfield` 会忽略成熟项目已经验证过的简单线性循环和权限分层思想。
- `integrate` 会引入远超 MVP 的依赖与许可证维护面，并掩盖简历项目的核心实现。
- `fork` 的代码量、升级负担与安全面不适合可解释的 MVP。

三家模型接入以各自官方 API 文档为准。实现采用 OpenAI-compatible Chat Completions 的公共子集，并以契约测试约束差异。

## UI 依赖补充评估

| 依赖 | 审查版本/日期 | 许可证 | 维护与兼容性 | 决策与边界 |
|---|---|---|---|---|
| [Rich](https://github.com/Textualize/rich) | 15.0.0；2026-04 发布，2026-07-30 审查 | MIT | PyPI 标记为 Production/Stable；支持 Windows、macOS、Linux 和 Python 3.9+ | `integrate`：只用于 CLI 渲染，不进入 Agent、Provider、策略、工具或审计核心 |

Rich 的 MIT 许可证允许本项目按许可证条款集成。项目不复制 Rich 源码；通过 PyPI 依赖声明使用，并由依赖包保留其许可证与归属。

## Hcode 能力迁移补充评估

调研日期：2026-09-03。Phase 0 完成只读证据；Phase 3 按下述边界实施。
这些结论不代表已经批准安装依赖，也不构成法律保证。

### 源码来源与许可证

| 项目 | 核对结果 | 决策 |
| --- | --- | --- |
| Hcode | 本地只读仓库 `git@github.com:PooooLish/Hcode.git`，基线 `d77927f7f2079b103c8077d321f0b24077c75800`；根 `LICENSE` 为 MIT，版权为 `Copyright (c) 2026 PooooLish`；`pyproject.toml` 也声明 MIT | Phase 0 为 `reference`；Phase 3 将 `hcode/conversation.py` 与 `hcode/context/manager.py` 的 usage-anchor/估算思路适配到 TriCoder，并已更新 `NOTICE`；spill 安全边界为独立 `rewrite` |
| Hcode 待迁移文件 | 对受版本控制的 `hcode/`、`tests/`、`docs/` 和项目元数据进行标识搜索，未发现额外 SPDX、第三方源码说明、vendored 代码声明或额外许可证 | 当前来源清晰；实现阶段仍需按实际选取文件复核 |
| TriCoder | 用户于 2026-09-03 选择 MIT；已创建根 `LICENSE` 和 `NOTICE`，并在 `pyproject.toml` 声明 MIT；仍没有 `SECURITY.md` | `approve`：允许在遵守 MIT 归属记录和 TriCoder 安全边界的前提下进行 `reference`、`adapt` 或独立 `rewrite` |

如果复制或实质性改编 Hcode 实现，MIT 条款要求在相关副本或实质部分中保留
Hcode 的版权与许可文本。TriCoder 已建立对应许可证和 `NOTICE`；Phase 3 已登记
实际参考/目标文件。后续阶段仍必须逐项追加新的实质性来源与目标。

### 候选 1：官方 Python MCP SDK

| 字段 | 评估 |
| --- | --- |
| Need | 为 TriCoder 提供 MCP client、stdio/HTTP transport、协议类型和工具调用生命周期；初版只计划启用 stdio client |
| 官方包名 | `mcp` |
| 发布者与官方来源 | Model Context Protocol（LF Projects）；[官方仓库](https://github.com/modelcontextprotocol/python-sdk)、[PyPI](https://pypi.org/project/mcp/)、[安装说明](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/get-started/installation.md) |
| 当日可验证版本 | `2.1.1`，PyPI 发布于 2026-08-25；v2 是当前稳定线，1.x 仅维护关键修复 |
| License | MIT |
| 安全公告与查询日期 | 2026-09-04 查询[官方安全公告](https://github.com/modelcontextprotocol/python-sdk/security/advisories)、PyPI 元数据与官方 issue。已发布 High 公告的已知受影响范围止于 1.x；另有未作为 advisory 发布的 [Streamable HTTP client SSRF/protocol-confusion issue #3358](https://github.com/modelcontextprotocol/python-sdk/issues/3358) 声称影响 v2，尚未独立验证。Phase 5 仅启用 stdio，不启用 HTTP；任何未来 HTTP 采用前必须重新审查。PyPI 的 vulnerability 列表为空不构成未来无漏洞保证 |
| Python 兼容性 | Python `>=3.10`；兼容 TriCoder 声明的 Python `>=3.11`。2026-09-04 已在本地 Python 3.11.6 `.venv` 安装并解析依赖闭包 |
| 维护状态 | Production/Stable；v2 稳定线和 v1 维护线均有明确版本策略，2.1.1 使用 PyPI Trusted Publishing 并带发布来源证明 |
| 传递依赖 | 2.1.1 元数据包含 `mcp-types==2.1.1`、`anyio`、`httpx2`、`jsonschema`、`pydantic`、`pyjwt[crypto]`、`python-multipart`、`sse-starlette`、`starlette`、`uvicorn`、`opentelemetry-api`、typing 组件；Windows 还含 `pywin32`。不应安装 `mcp[cli]`，以避免非必需 `typer`/`python-dotenv` |
| 安装脚本或本地二进制影响 | 官方 wheel 为 `py3-none-any`，但传递依赖可能选择平台 wheel；Windows 的 `pywin32` 用于 stdio 子进程。没有执行安装器进行动态验证 |
| 网络、权限与数据边界 | stdio 会启动外部 server 进程；HTTP transport 会出站联网并扩大鉴权、DNS rebinding 和会话隔离风险。首版仅允许显式配置、默认关闭的 stdio；命令、环境、cwd、输出和超时必须经过 TriCoder 策略与审计，不启用 SDK server/HTTP/OAuth 能力 |
| manifest / lockfile 变化 | 已获批准：`pyproject.toml` 增加精确 direct dependency `mcp==2.1.1`；`requirements.lock` 记录 Windows/Python 3.11.6 干净环境的精确运行时闭包，不含 editable 本项目或工具包 |
| Decision | Phase 0 为 `approve-with-conditions` 候选；Phase 5 已于 2026-09-04 获得明确批准并安装 `mcp==2.1.1`、记录锁文件。运行边界仍为可选导入、仅 stdio、fake server 契约覆盖且非 MCP 启动不受影响 |
| Selected version | `mcp==2.1.1`（不安装 `[cli]` extra） |
| Reuse mode | `reference`：Hcode 仅提供生命周期与测试场景参考，适配层独立实现 |
| License | MIT；保留项目 LICENSE/NOTICE 证据 |
| Current limitation | 首份 requirements.lock 记录 Windows/Python 3.11 解析结果，不宣称跨平台或全哈希可重现 |
| Runtime scope | 只启用 stdio client；HTTP/SSE/OAuth/resources/prompts 不进入 Phase 5 |
| 回滚/移除 | 优先禁用 `[mcp]`，关闭路径不导入第三方 SDK。完整移除代码须单独获批：先迁移或保留核心 `tools/handlers.py` 与 `tools/__init__.py` 依赖的 `tricoder.mcp.schema` 校验器，并解除 CLI/SessionRuntime 的 MCP 导入和接线，再移除适配层、direct dependency 并重建锁文件；不能直接删除整个 `src/tricoder/mcp/`。最后验证内置工具和禁用路径 |

### 候选 2：PyYAML

| 字段 | 评估 |
| --- | --- |
| Need | 若 TriCoder 要完整兼容 Hcode 的 YAML front matter、`skill.yaml` 及列表/布尔/整数等字段，需要安全 YAML 解析器 |
| 官方包名 | `PyYAML`（import 名 `yaml`） |
| 发布者与官方来源 | YAML/Python 社区维护；[官方仓库](https://github.com/yaml/pyyaml)、[PyPI](https://pypi.org/project/PyYAML/) |
| 当日可验证版本 | `6.0.3`，发布于 2025-09-25 |
| License | MIT |
| 安全公告与查询日期 | 2026-09-03 查询 [OSV 的 PyPI/pyyaml 结果](https://osv.dev/list?ecosystem=PyPI&q=pyyaml)与[官方 GitHub advisories](https://github.com/yaml/pyyaml/security/advisories)。OSV 显示的 PyYAML 记录是已有修复的旧版本问题；官方仓库当前无已发布 advisory。解析不可信输入仍必须使用 `yaml.safe_load` 并做结构/资源限制 |
| Python 兼容性 | Python `>=3.8`；兼容 TriCoder Python `>=3.11` |
| 维护状态 | PyPI 标记 Production/Stable；官方仓库最新 release 为 6.0.3 |
| 传递依赖 | 发布元数据无运行时传递依赖 |
| 安装脚本或本地二进制影响 | 同时提供源码包和多平台 CPython wheel；实现包含可选 LibYAML/Cython 扩展。应从锁定 wheel 安装，避免不必要的本地编译；本阶段未下载或执行 |
| 网络、权限与数据边界 | 解析本地 Skill/Agent 文档，不应联网或执行标签；只读获准根目录，先做文件字节上限，再 `safe_load`，随后严格字段 schema、类型、数量与嵌套深度校验 |
| manifest / lockfile 预计变化 | 经用户批准后，`pyproject.toml` 增加一个 direct dependency，锁文件固定 wheel/hash；本阶段均未修改 |
| Decision | `approve-with-conditions`：仅当 Phase 6 明确要求兼容 Hcode/通用 Skill YAML 时采用；禁止 `yaml.load`、Python 对象标签、无界 aliases/输入和隐式宽松字段 |
| 推荐 pin | manifest 建议 `PyYAML>=6.0.3,<7`，锁文件固定解析版本和 hash |
| 回滚/移除 | 若语法契约缩减为标准库可表达的子集，移除依赖并以受限 parser 替换；删除 manifest 条目并重建锁文件 |

### 候选 3：Python 标准库受限 front matter 解析

| 字段 | 评估 |
| --- | --- |
| Need | 在不新增 YAML 依赖时解析少量已知元数据字段 |
| 官方包名 | 无第三方包；使用 `pathlib`、`re`、`tomllib` 或自建严格的逐行 `key: value` 子集解析 |
| 发布者与官方来源 | Python 标准库 |
| 当日可验证版本 | 随 TriCoder Python `>=3.11`；`tomllib` 只解析 TOML，不解析 YAML |
| License | Python Software Foundation License；自有 parser 随 TriCoder 许可证 |
| 安全公告与查询日期 | 2026-09-03；没有新增包公告面，但自建 parser 会产生语法歧义、边界和维护风险 |
| Python 兼容性 | Python 3.11+；当前项目 `.venv` 3.10 不满足此前提 |
| 维护状态 | 标准库稳定；自建 YAML 子集由 TriCoder 自行维护 |
| 传递依赖 / 二进制 | 无 |
| 网络、权限与数据边界 | 不联网、不执行代码；仍需路径、大小、行数、字段、类型和重复键限制 |
| manifest / lockfile 预计变化 | 无 |
| Decision | `defer` 作为完整 Hcode YAML 兼容方案：标准库没有 YAML parser，声称兼容会失实。若产品明确接受较窄语法，可另行 `approve` 为 TriCoder 自有格式，但必须记录不兼容项并提供拒绝式错误 |
| 推荐 pin | 不适用 |
| 回滚/移除 | parser 为独立模块；恢复到 PyYAML adapter 或禁用 Skills 加载，不影响 Agent 核心 |

## 迁移依赖结论

### 2026-09-07：可验证 stdio 补救的复用决策

```text
Decision: approve-with-conditions.
Official mcp==2.1.1 reuse mode: integrate for ClientSession, protocol types,
and narrowly wrapped platform process helpers. TriCoder stdio orchestration:
greenfield. No SDK source is copied; no dependency, lockfile, or NOTICE change.
The adapter is version-bound and must fail closed if required capabilities are
missing. An SDK upgrade requires a fresh dependency/security review.
```

本次不改变包、不选择新依赖，继续以现有 2026-09-04 的 SDK 身份、许可证、
安全公告与运行时审查为证据来源。只读取锁定 SDK 源码核对接口，不复制或实质性
改编 SDK 的 stdio 编排实现；依赖升级必须重新进行依赖与安全审查。

2026-09-07 最终修复沿用同一复用决定：Windows Job 通过已验证的 pinned-SDK
私有 mapping 转移给 adapter；关闭使用现有 pywin32 `CloseHandle`，asyncio
底层 transport 由独立窄 wrapper 关闭，均不调用会吞错误的 SDK close helper。
这些 wrapper 按本项目的布尔证据/所有权契约独立编写，不复制或实质改编上游
函数；没有新增依赖、升级、下载、NOTICE 或许可证变化。版本绑定私有接口与
未交付给 adapter 的 SDK 内部资源仍是复审边界。

- MCP 首选官方 `mcp` 2.x，而不是自动继承 Hcode 的 1.x pin。
- YAML：完整兼容选择 PyYAML 6.0.3 系列；零依赖方案只能支持明确、受限且不冒充
  YAML 兼容的格式。
- Phase 0 的两个第三方候选都只是 `approve-with-conditions` 技术评估；随后 MCP
  已在 Phase 5 得到安装及 manifest/lockfile 变更授权。PyYAML 仍须在 Phase 6
  单独明确授权，MCP 授权不扩展到其他依赖或升级。
