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
