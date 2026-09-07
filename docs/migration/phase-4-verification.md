# Hcode 迁移 Phase 4 验证记录

日期：2026-09-03

范围：Extension 描述与生命周期宿主、ToolRegistry 动态注册/来源/风险边界、
严格扩展配置模型和脱敏 doctor。本阶段没有安装依赖、启动真实扩展或进入 MCP
实现。

## 实现结果

- `ExtensionDescriptor` 只允许规范 ID、固定 kind、无 URL/userinfo/query 的安全
  source 标识、精确布尔 enabled 和显式 trust；Host 构造期拒绝无效 descriptor。
- `ExtensionHost` 按声明顺序启动、逆序停止，重复 stop 幂等；启动抛错后会先清理
  可能已部分分配的资源，清理失败则保留到后续 stop 重试。单个启动、停止、工具
  列表或 prompt 失败转换成固定 `ExtensionFailure`，不会复制底层异常正文。
- 启动取消会逆序清理已经启动的 provider 并传播 `CancellationError`；禁用和重复
  ID provider 不启动。两个扩展声明相同工具名时双方都被排除；注册时内置工具优先。
- `ToolRegistry.register()` 要求 `ToolOrigin` 的 kind、ID 和风险声明，handler 必须
  绑定当前 `ToolContext`。注册期冻结并结构化校验 Schema，防止扩展注册后篡改
  Provider schema 或运行校验规则。
- 动态工具与内置工具共用参数验证、同步/异步入口、审批、只读限制、取消、输出
  spill 和 Agent 审计。非 read 风险需要审批；只读模式提前拒绝；dangerous 使用
  专用审批类别，`fullaccess` 也不能自动通过。
- 动态工具意外异常只返回固定安全失败；Agent 审计增加 `{kind,id,risk}` 来源，
  仍不记录未知工具参数正文或异常文本。
- 新增 `ExtensionsConfig`、`MCPConfig`、`SkillsConfig`、`HooksConfig`、
  `WorktreeConfig` 和 `AgentsConfig`，全部采用关闭/最小权限默认值。
- `.tricoder.toml` 的扩展命名空间严格拒绝未知字段、非法类型、重复 ID、非 stdio
  transport、带路径 command、越界 Skill 目录、负预算和明文凭据键/参数。
- `credential_env` 只保存变量名。项目声明必须再由可信进程环境
  `TRICODER_EXTENSION_ENV_ALLOWLIST` 逐项授权；工作区 `.env.local` 不能授予该
  权限。未授权引用不解析存在性，防止项目枚举或继承任意进程秘密。
- `ProviderConfig.api_key` 设为 `repr=False`，确保整个 `AppConfig` repr 不再泄露
  Provider 或扩展凭据值。doctor 仅显示安全扩展元数据与凭据状态，不创建扩展。

本阶段围绕 TriCoder 既有 ToolRegistry、配置优先级和审计边界独立实现，仅遵循
已批准迁移规范中的 Extension Host 生命周期，不复制 Hcode 实现源码，因此无需
新增 Hcode NOTICE 文件范围。

## TDD 与回归场景

- Host：discover、禁用项、顺序启动、逆序/重复 stop、部分启动失败后的清理与重试、
  取消回滚、无效 descriptor、扩展间同名冲突和内置名称优先。
- Registry：动态 Schema 校验、Schema 冻结、外部 ToolContext 拒绝、来源查询、
  异步执行、write 审批、read-only 拒绝、异常隔离和安全来源审计。
- Config：默认全关、未知 transport、重复 ID、明文 secret、越界路径、非布尔
  enabled、负预算、未知安全字段、凭据环境名/存在性，以及项目不能自行授权进程
  Secret。
- Doctor：不创建 Provider/扩展，只显示 ID、mcp、project、有效状态与凭据状态，
  不出现真实值。
- 新行为均先观察预期 RED；循环导入、Schema 形状校验和配置 repr 泄露均在集成
  回归中被实际捕获并修复。

## 最新验证证据

环境：Windows，Python 3.11.6，项目现有 `.venv`。

| 检查 | 结果 | 覆盖范围 |
| --- | --- | --- |
| Phase 4 focused suite | exit 0；387 tests，23.309s，OK | Host、Registry、Config、doctor、Agent、Runtime |
| `python -B -m unittest discover -s tests -q` | exit 0；631 tests，95.451s，OK；4 skipped | 全项目回归 |
| `python -B -m compileall -q src tests` | exit 0 | 源码与测试语法/字节码编译 |
| `python -B -m tricoder eval evals\smoke --dry-run --no-color` | exit 0；3/3 validated | Eval 定义与隔离装配 |
| `workspace.py doctor tricoder-cli` | exit 0；0 findings | 项目交接契约 |
| `git diff --check` | exit 0 | 当前差异空白与冲突标记 |

4 个跳过项为既有平台能力测试；本阶段没有把跳过项描述为已覆盖。

## 限制与剩余风险

- Phase 4 只有 fake provider 生命周期测试；没有 MCP SDK、server 子进程、Skill
  parser 或 Hook engine，配置中的 enabled 只是声明，当前 Runtime 不启动它们。
- 动态 handler 是 TriCoder 自有 adapter 的进程内接口，不是加载任意项目 Python
  代码的插件 API。未来 MCP/Hook 只能通过受控 adapter 进入，不能导入项目代码。
- `ToolOrigin` 风险由受信任 adapter 声明；项目配置无权直接构造 handler。Phase 5
  必须根据 MCP tool 能力采取更保守的默认风险，并在未知时 fail closed。
- allowlist 只建立授权声明；真实凭据注入、子进程环境最小化和生命周期审计属于
  Phase 5，尚未实现。
- 当前仅验证 Windows/Python 3.11.6；Linux/macOS 和 Python 3.12 仍需 CI 验证。
- Phase 5 需要再次明确批准，并在安装官方 MCP SDK 前重新确认依赖与锁文件范围。
