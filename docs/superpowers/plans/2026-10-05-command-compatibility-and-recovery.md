# TriCoder 命令兼容、工具说明与错误恢复执行计划

> 实施状态（2026-10-05）：S0—S5 已完成；最终 Windows / Python 3.11.6 全量回归 1384 项通过、12 项条件跳过，独立审查的命令 PATH 边界发现已修复并复核关闭。阶段证据见 `runtime/command-compatibility/`。

> 原始执行提示：使用 `superpowers:executing-plans` 按任务实施；每阶段先补失败回归，再最小修改、验证和自审。该提示保留作实施方法记录，当前实现状态以上述状态行与运行证据为准。

**Goal：** 普通 Python 编码任务不再因版本查询、`python3` 名称或简单 unittest 目标写法而被不必要地终止；模型和用户能理解可纠正错误，同时保留真正的权限拒绝和未知副作用停止边界。

**Architecture：** 在现有 CommandPolicy 内进行有限、确定性的命令归一化；工具层只把经过完整预检查的少量形式错误转换为可重新规划的参数错误。沿用 ToolError、工具批次、审批和验证证据机制，不重写 Agent 循环。

**Tech Stack：** Python 3.11+、unittest、现有 CLI/Rich/Textual、原生与 legacy_json 工具协议；不新增依赖。

**Spec：** 本文件第 1—5 节为设计约束和验收规格，第 6 节为逐步执行任务。

**状态：** 已实施。本文接口已按当前代码落地；最终行为与限制以源码、测试和 `runtime/command-compatibility/` 证据为准。

## 1. 已确认问题与本轮范围

用户在新工作区启用 `fullaccess`，要求创建计算器与 unittest 测试，遇到两类失败：

1. 模型表示准备查询 Python 版本，随后 `run_command` 返回 `policy_denied / stop_task`；截图未展示实际命令参数。
2. 创建文件后，模型运行 `python3 -m unittest -v test_calculator`，再次被拒绝，任务无法获得测试证据。

当前策略调用已验证：

| 当前输入 | 当前结果 | 原因 |
| --- | --- | --- |
| `python --version` / `python -V` | 拒绝 | 被错误送入脚本参数校验 |
| `python3 -m unittest -v test_calculator` | 拒绝 | 程序名称仅接受 python/python.exe/py/py.exe |
| `python -m unittest -v test_calculator` | 拒绝 | unittest 点名目标只接受工作区 `.py` 文件 |
| `python -m unittest discover -v` | 通过策略校验 | 已支持的测试发现形式 |

第二张截图的文字总结是模型输出，不能将其中“整个 run_command 被禁止”的推断作为事实。第一张截图没有展示命令参数，版本查询是与代码复现吻合的判断，不应编造原始调用日志。

相关代码入口：

- `src/tricoder/policy.py`：命令解析、可信解释器解析、unittest 参数规则。
- `src/tricoder/tools/command.py`：审批、进程启动、unknown_effects、验证证据。
- `src/tricoder/tools/__init__.py`：异常脱敏、副作用归一化、工具风险与输出预算。
- `src/tricoder/models.py::tool_failure`：参数错误默认 REPLAN，策略/审批拒绝默认 STOP_TASK。
- `src/tricoder/engine/tool_batch.py`：本批首次失败后回填 skipped，决定下一轮或停止。
- `src/tricoder/engine/telemetry.py`：调用 CommandPolicy.audit_metadata 生成脱敏审计信息。
- `src/tricoder/protocols.py`：工具结果与系统提示词。
- `src/tricoder/presentation/{console,tui}.py`：审批和结果显示。

本轮解决上述问题及其直接相关的说明、错误反馈和回归。**不做 Docker、评测平台接入、工作区扫描范围修改、审批授权缓存、新增权限级别、Provider 扩展或大规模拆文件。**

## 2. 全局约束

- 唯一实施仓库：`D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli`。先读 `../../AGENTS.md`、`AGENTS.md`、README、pyproject 和 project.md。
- 以当前工作树为基线；保留第一轮、第二轮重构及所有既有未提交/未跟踪文件，不能从 HEAD 覆盖，也不直接恢复历史提交。
- 不读取 `.env.local`、凭据目录、真实 Session 数据库；不调用真实 Provider；不安装依赖、启动 Docker、提交或推送。
- 保留 `strict / relaxed / fullaccess` 与独立 `read_only` 的现有语义。版本查询通过策略后仍走既有命令审批；本轮不扩大 relaxed 自动批准范围。
- `shell=False`、可信可执行文件解析、工作区与敏感路径检查、命令参数白名单、进程树清理、超时/输出预算保持有效。
- 不新增 `python -c`、任意模块、任意 Shell、包安装或任意 Python 版本选择能力；不能以“fullaccess”跳过校验。
- `APPROVAL_DENIED`、真正的 `POLICY_DENIED`、取消、清理失败和未知副作用仍停止任务。外部扩展/MCP 不能通过自报可恢复错误改变信任边界。
- 只用合成文件和 fake Provider 验证；新增执行证据放 `runtime/command-compatibility/`。真实进程测试只执行合成代码及固定 Python 版本查询。
- 新导入使用重构后的规范路径，如 `tricoder.process.control`、`tricoder.session.runtime`；不要重新引入旧兼容壳作为生产依赖。

## 3. 方案选择与目标行为

不采用“取消命令门禁”：没有 OS 沙箱时影响过大。也不采用“只改提示词”：真实拒绝链路和未知副作用问题仍存在。

采用 **有限命令兼容 + 精确预执行反馈 + 保守恢复**。

### 3.1 Python 程序名归一化

接受 `python`、`python.exe`、`py`、`py.exe`，新增 `python3`、`python3.exe` 作为同一受信解释器的别名。

- 所有别名都解析到 `trusted_python_executable()`，不在 PATH 中寻找模型指定的另一套解释器。
- 明确告诉用户和模型：`python3` 表示本会话使用的 Python，不是安装或切换 Python 版本。
- 不接受 `python3.12`、`python2`、`py -3.12`、绝对/相对可执行程序路径；不扩大其他程序名规则。
- 审批展示原始请求及归一化后的实际 argv；执行必须使用同一 argv。argv 展示只用于文本，不重新拼接为 Shell 命令执行。

### 3.2 固定版本查询

只接受“允许的解释器别名 + 单个 `--version` 或 `-V`”，归一化执行为：

```text
<当前受信 Python 的绝对路径> -I --version
```

- `-I` 仅由本地构造；不因此允许模型提供任意解释器启动选项。
- 拒绝夹带脚本、`-c`、`-m`、重定向或额外参数的版本查询变体。
- 增加精确的本地分类 `CommandPolicy.is_information_command(args: list[str]) -> bool`，只识别本轮固定形式；不能按字符串包含 `version` 判定。
- 该命令不构成验证：`verification_passed=None`，无 `VerificationEvidence`，不增加测试通过状态。
- 正常退出、未超时、未超量、清理已确认时，不新增文件副作用；必须保留执行前已有的 unknown_effects、验证失败与修改记录。
- 启动后的取消、异常、超时或清理不确定沿用保守停止语义。不能通过提前返回绕开取消、审计、输出预算和清理。
- **关键：** 当前普通非验证命令会设置 `scope.unknown_effects=True`。仅放行版本字符串会导致随后仍因 UNKNOWN 停止；必须连同成功后的状态归一化一并实现并测试。

### 3.3 unittest 的有限兼容

继续支持已有 `discover` 和相对 `.py` 文件目标；新增简单的单段模块名简写：

```text
python3 -m unittest -v test_calculator
  → <受信 Python> -m unittest -v test_calculator.py
```

仅当目标匹配 ASCII 标识符 `[A-Za-z_][A-Za-z0-9_]*`，且执行目录下确实存在相应 `.py` 普通文件，通过工作区/敏感路径校验时才归一化。

- 不调用 importlib、find_spec，不导入用户模块做解析；不搜索 site-packages 或 PYTHONPATH。
- 不支持 `pkg.test_module`、`test_module.TestCase.test_method`、包目录、namespace package 的新解析能力。给出固定建议使用 `discover` 或明确 `.py` 文件。
- 找不到对应本地文件时，给出可纠正的参数错误；不得改为搜索外部模块。
- 保持已存在目标的符号链接/真实路径安全规则，不为简写增加绕过。
- 不强制测试文件必须名为 `test_*`；映射必须对应真实本地文件。

### 3.4 校验目录与执行目录一致

目前 CommandPolicy 按工作区解析路径，run_command 另有 cwd。新增归一化不能在工作区根验证文件、却在另一个目录执行同名目标。

将接口扩展为：

```python
CommandPolicy.validate(command: str, *, cwd: Path | None = None) -> list[str]
```

- 旧调用不传 cwd 时保留既有默认行为。
- RunCommandTool 先通过 WorkspacePolicy 解析有效 cwd 并确认目录，再将其传入校验器；包括普通脚本、unittest 文件目标和 discover 的路径参数，都按有效 cwd 解释，最后仍受原工作区边界约束。
- 不通过临时修改共享 CommandPolicy 属性实现 cwd 切换；使用局部值，避免不同调用相互污染。
- 保留既有显式禁止的路径形式，不把本轮变成放宽 `..`、绝对路径或 Git 选项的工作。
- `CommandPolicy(None)` 缺少工作区证明时，不启用模块简写自动映射；返回受控可纠正错误，不借用进程 cwd 推定授权范围。
- 不宣称这能冻结脚本全部依赖或建立进程沙箱。

### 3.5 审计分类同步

当前 `audit_metadata(command)` 会重新调用 validate，且把非 `-m` 的 Python 命令全部记为 script。新增 cwd 和固定版本查询后，必须同步修正：

- 为 `audit_metadata` 增加可选关键字 cwd，默认行为兼容旧调用；telemetry 先安全解析请求 cwd，再传入。无效 cwd 仅记录固定无效标记，不回退到根目录假装合法。
- 固定版本查询记为独立信息类别，例如 `execution_kind=information`、`information_kind=python_version`，不得把 `-I` 当脚本路径。
- 审计依旧是请求的脱敏分类，不是进程启动成功或测试通过的证据；事后重新校验失败不能覆盖实际 ToolResult，也不能触发额外执行。
- 保留现有脱敏字段边界，不记录原始命令、完整 argv、自由参数或新增绝对路径。

## 4. 错误分类与工具说明

### 4.1 最小类型扩展，不改协议错误枚举

在 policy.py 中新增 `CommandFormReason` 枚举及 `CommandFormError(PolicyArgumentError)`：

- `DIRECT_TOOL_ENTRYPOINT`：已知测试工具应通过 `python -m` 启动。
- `LOCAL_TEST_TARGET_REQUIRED`：unittest 简写没有可确定的本地目标，或当前不支持该目标形式。

异常只保存枚举理由，反馈从本地固定文本映射取得；禁止把任意异常原文、原始参数、绝对路径或环境变量拼进反馈。

RunCommandTool 仅在**命令预检查阶段**捕获该类型，转换成：

```text
code=invalid_argument, recovery=replan, retryable=false
output=固定原因说明 + 一条受支持的命令形式示例
```

沿用现有 ToolError 和工具消息结构，无须新增顶层 ErrorCode 或修改 SQLite schema。不要在整个执行阶段捕获此类型并一概降级，也不要让普通扩展异常获得这一待遇。

安全校验优先：只有整条输入已经排除 Shell 元字符、越界/敏感路径和禁止选项后，才允许标记可纠正形式错误。带 `--rootdir` 等禁用参数或外部路径的直接 pytest 调用，仍是 POLICY_DENIED；不能因为最先看见 `pytest` 就提前返回 REPLAN。

| 情况 | 结果 |
| --- | --- |
| `pytest -q` 等已知直接入口，等价模块调用通过现有完整校验 | 不执行，INVALID_ARGUMENT / REPLAN，建议 `python -m pytest -q` |
| unittest 简写无对应文件，其他参数均安全 | 不执行，INVALID_ARGUMENT / REPLAN |
| unittest 类/方法 dotted 目标，其他参数均安全 | 不执行，INVALID_ARGUMENT / REPLAN，建议 discover/文件目标 |
| 不支持的程序、安装命令、`python -c`、任意模块、组合命令 | POLICY_DENIED / STOP_TASK |
| 越界/敏感路径、禁止选项、只读、审批后身份变化 | 保持 POLICY_DENIED / STOP_TASK |
| 用户拒绝 | APPROVAL_DENIED / STOP_TASK |
| 启动后副作用未知、清理失败 | 保持 STOP_TASK，不能当作形式错误 |

对未知 PolicyError 保留当前固定脱敏提示，不按异常文本关键词决定是否可恢复。本轮只精准改善已证明的兼容场景，不能机械地把所有 PolicyError 改为 INVALID_ARGUMENT。

### 4.2 恢复流程

复用现有批次机制：首次可纠正错误后，本批剩余动作全部 skipped；下一轮 Provider 根据反馈重新提出动作。不得在工具内部自动执行建议命令，也不得复用被拒绝操作的审批。

- `retryable` 仍为 false；REPLAN 表示允许重新规划，不是自动重放。
- 继续受现有 max_rounds 限制，不在此轮增加无限重试或新计数状态机。
- 拒绝后重提的写入或命令仍走原审批路径；用户明确拒绝后不能通过改写命令继续。
- UNKNOWN 优先于 REPLAN；已有不确定状态不能被一次参数纠正清零。

### 4.3 模型与用户看到的信息

更新 RunCommandTool.description 与 `protocols.py` 的共享工具使用约束，确保原生和 legacy_json 两种模式都知道：

- 优先用专用文件/搜索工具；run_command 是有白名单的执行器。
- Python 别名使用同一个会话解释器；标准测试示例为 `python -m unittest discover -v`。
- 版本查询可选，不是每次任务必须执行的准备步骤。
- 常见拒绝示例：`python -c`、pip 安装、Shell 管道、任意程序。
- 收到 INVALID_ARGUMENT / REPLAN 可调整受支持写法；收到 POLICY_DENIED / APPROVAL_DENIED 则停止，不能绕过。
- 一条命令被拒绝不代表整个 run_command 不可用；不要凭泛化错误编造权限或环境原因。
- finish 工具调用成功不等于整个任务测试通过，需依据本地验证结果总结。

说明中的例子必须有真实策略测试支持，不宣称支持任意 Python 命令或任意 unittest 目标。

审批界面显示原请求、归一化 argv、有效 cwd 和超时；明确 `python3` 使用会话解释器。仅在本地审批 UI 展示请求，不把原始命令/argv 新增到审计、SQLite 或错误反馈中。

修正 console.approve 把 apply_patch、MCP、工作区确认等一律标作“命令执行”的问题：通过固定 action→标题映射区分类型，未知 action 用“操作需要审批”。TUI 如已有通用标题无需多余重构，但两种 UI 必须显示一致的预执行原因和审批详情。

## 5. 审查重点

1. **形式纠错冒充越权恢复：** 直接工具名后夹带外部路径/禁止参数，仍停止且零进程、零审批。
2. **检查 A、执行 B：** cwd 为子目录且根目录存在同名测试时，按实际执行目录解析；顺序切换 cwd 不污染共享对象。
3. **查询版本被当作测试：** 修改文件后仅查询版本，再 finish，仍不可判定测试通过；先前 UNKNOWN 不消失。
4. **批次内悄悄执行：** 可纠正错误后剩余动作 skipped；下一轮重新审批；拒绝/取消后不继续。
5. **诊断泄漏和扩展伪造：** 错误含假密钥、终端控制字符或绝对路径时不外传；MCP/扩展不能伪造本地可恢复来源。

以上五项分别在 S1—S4 的测试中固定，不以“工具能导入”代替验证。

## 6. 逐步实施

### S0：基线与失败表征

**读取：** 第 1 节文件、tests/test_policy.py、test_tools.py、test_tool_errors.py、test_effect_state.py、test_verification_evidence.py、test_session_runtime.py、test_protocols.py。

**交付：** `runtime/command-compatibility/baseline.md`，记录当前 Git 状态、涉及文件内容指纹、实际测试结果；不存真实用户数据。

- [ ] 检查第二轮目录归类已存在，确认命令工具实际调用新 process/session 路径。
- [ ] 运行受影响原有测试，记录已有失败和平台跳过，不能把上一轮 1355 项结果当成本轮新鲜结果。
- [ ] 为截图相关场景增加目标行为回归：固定版本查询，以及第二张截图总结中的 unittest 命令；确认迁移前因对应原因失败，不把第一张截图的具体命令当作已有日志事实。
- [ ] 记录已有 `test_rejects_unsafe_script_execution` 将 `python --version` 当作拒绝项；后续只调整该项，保留越界/敏感/任意代码样例。

### S1：命令归一化与 cwd 一致性

**修改：** policy.py、tools/command.py、engine/telemetry.py。
**测试：** test_policy.py；必要时新建 `tests/test_command_compatibility.py` 放本轮跨层回归。
**接口：** 第 3 节的 `validate(..., cwd=...)`、`is_information_command(...)`，保持返回 `list[str]`。

- [ ] 先写失败测试：六个支持别名；两种版本选项；固定 argv；简单模块到本地文件；无目标文件；dotted 目标；非法版本/程序/选项；测试文件链接逃逸。
- [ ] 增加 cwd 子目录、不同 cwd 连续调用及输入规范化前后边界一致性的测试。
- [ ] 实施有限归一化；证明没有导入用户模块、没有调用 shell、没有解析另一套 PATH Python。
- [ ] 对每个允许样例检查**实际传给执行器的 argv/cwd**；只断言 validate 没抛异常不够。
- [ ] 覆盖信息命令的审计类别、子目录目标审计、无效 cwd 和脱敏字段；审计失败不改写执行结果。
- [ ] 聚焦测试通过后自审，记录兼容规则和仍不支持的形式。

### S2：版本查询的副作用与验证语义

**修改：** tools/command.py；如必要，最小调整 tools/__init__.py 的本地结果归一化。
**测试：** test_command_compatibility.py、test_effect_state.py、test_verification_evidence.py。
**接口：** 使用 S1 的信息命令分类，继续返回现有 ToolResult。

- [ ] 先写失败测试：正常查询不新增 UNKNOWN；不产生验证证据；之前的失败/UNKNOWN 不被清除；修改后仅查询版本不能通过 finish 门禁。
- [ ] 覆盖版本查询的启动失败、超时、取消、输出超限、清理失败；保持已有停止与资源归属语义。
- [ ] 实施信息命令成功分支，保留任务权限、审批、审计及输出预算。
- [ ] 验证普通脚本、pytest/unittest 与 Git 的原有副作用语义未被信息命令分类波及。

### S3：可纠正错误反馈与安全停止边界

**修改：** policy.py、tools/command.py；若需要复用固定反馈映射，可在 policy.py 定义小型纯映射，不增加通用诊断框架。
**测试：** test_tool_errors.py、test_command_compatibility.py、test_agent.py 或 test_agent_async.py。
**接口：** 第 4 节的 CommandFormReason/CommandFormError；沿用 INVALID_ARGUMENT、REPLAN 与现有协议字段。

- [ ] 先写失败测试：直接 pytest 的安全写法收到明确修正提示；不支持的 unittest 目标收到 discover/文件建议；零审批、零子进程。
- [ ] 写混合输入测试：`pytest --rootdir=...`、外部路径、敏感路径、Shell 组合等不能被形式纠错降级。
- [ ] 实施只在 run_command 预检查阶段捕获的固定错误映射；不更改全局 POLICY_DENIED 默认停止。
- [ ] 用 fake Provider 编排两轮：第一轮产生可纠正错误和后续写调用，后者 skipped；第二轮给出合规命令并正常走审批和执行。
- [ ] 对照用户拒绝、read_only、UNKNOWN、取消、MCP 伪造情况，确认无下一轮绕过；保持现有 max_rounds 上限。
- [ ] 在两种协议中验证模型收到可读固定提示；审计仍不含命令正文或假秘密。

### S4：工具说明、审批呈现与权限矩阵

**修改：** tools/command.py、protocols.py、presentation/console.py；仅在确有展示缺口时改 presentation/tui.py。
**测试：** test_protocols.py、test_ui.py、test_tui.py、test_session_runtime.py、test_command_compatibility.py。
**接口：** 不新增权限或 CLI 参数，继续使用现有 approver(action, detail)。

- [ ] 补充符合实际能力的工具描述和共享说明，原生/legacy_json 均可见；避免复制完整白名单，保留几个经过测试的典型样例。
- [ ] 添加归一化 argv/cwd 审批展示与固定 action 标题测试，文本按字面渲染，不执行或解释为终端富文本指令。
- [ ] 测试 strict 和 relaxed 的版本/测试命令仍审批；fullaccess 自动批准内置命令；read_only 拒绝命令；MCP 危险审批不变。
- [ ] 确认 CLI 单次 run 与交互 Session 两条工具装配路径均获得新行为；无需将单次 run 改成可切换会话权限。

### S5：真实合成任务与交付

**修改：** README.md、project.md；本计划可更新实施状态，不修改历史重构结论。
**测试：** test_command_compatibility.py 及最终完整回归。

- [ ] 使用合成工作区、fake Provider 和真实现有 Python：创建计算器与测试；运行 `python3 --version`；运行 `python3 -m unittest -v test_calculator`；确认有真实测试输出、有效验证证据及正确完成状态。
- [ ] 故意制造一个错误实现，确认测试失败不会被 finish 文案掩盖。再覆盖“仅查询版本便 finish”的未验证结果。
- [ ] 覆盖 native/legacy 协议、sync/async 工具入口，既有取消和清理测试保持通过。
- [ ] 在最终源码上跑完整 unittest、内存语法编译、CLI --help 和 git diff --check；若修改后有失败，解决或准确记录，不删测试/跳过关键场景求通过。
- [ ] 更新 README：支持形式、解释器别名语义、预执行纠错与硬拒绝区别、权限和非沙箱边界。
- [ ] 更新 project.md 和实施记录：改动、验证数字、失败/跳过、未验证平台、后续工作；不宣称已完成真实模型端到端验证。

## 7. 验证命令

在项目根使用现有虚拟环境。阶段按涉及范围选择，不需要每阶段重复完整测试：

```powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_policy tests.test_command_compatibility -q
.\.venv\Scripts\python.exe -B -m unittest tests.test_tools tests.test_tool_errors tests.test_effect_state tests.test_verification_evidence -q
.\.venv\Scripts\python.exe -B -m unittest tests.test_protocols tests.test_ui tests.test_tui tests.test_session_runtime tests.test_cli -q
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -q
.\.venv\Scripts\python.exe -B -m tricoder --help
git diff --check
```

`test_command_compatibility.py` 是本计划建议新文件，创建前不要把模块不存在误记为业务回归。新增用例须有明确 red→green 证据；旧的大测试文件只调整本轮明确改变的断言。

语法检查优先用 Python 内置 compile 在内存中检查 src/tests，避免把大量 pycache 写入工作区并再次触发快照上限。不得用真实工作区里的用户代码验证执行隔离。

## 8. 完成标准

- [ ] 两个实际问题有回归：版本查询与 `python3 -m unittest -v test_calculator` 在受支持条件下可执行。
- [ ] 信息查询不会冒充测试或新增不必要 UNKNOWN，不会清除既有不确定状态。
- [ ] 支持的脚本/测试路径在有效 cwd 下校验，仍受原工作区边界限制。
- [ ] 可纠正错误限于本地完整预检查阶段，失败调用零执行；下一轮重新走审批。
- [ ] 越权、用户拒绝、取消、未知副作用和清理失败保持停止；没有自动扩大权限。
- [ ] 工具说明与真实能力一致，两种协议和两种交互 UI 的反馈可理解。
- [ ] 合成任务真实测试成功与失败均有证据；完整回归无未解释失败，平台跳过准确记录。
- [ ] 未实现沙箱、扫描范围修复、审批缓存或其他范围外功能；没有安装依赖或自动提交。

## 9. 可直接交给 coding session 的提示词

```text
请在 D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli 实施命令兼容、工具说明与错误恢复修复。

先读 ../../AGENTS.md、AGENTS.md、README.md、pyproject.toml、project.md，以及：
docs/superpowers/plans/2026-10-05-command-compatibility-and-recovery.md

按文档 S0—S5 小步实施，每阶段补失败回归、最小修改、聚焦验证和自审后继续。本次授权修改计划范围内的源码、测试和项目文档，不是只输出建议。

目标：支持 python3 作为当前受信 Python 的别名；支持精确版本查询；把简单 unittest 模块名安全归一到执行目录下已存在的本地 .py 文件；校验目录与 cwd 一致；对少量执行前且无副作用的形式错误给出明确反馈并允许下一轮重新规划。

重点防止两类假修复：只在白名单放行版本查询，却因 unknown_effects 仍然停止；把所有 policy_denied 改成可重试，导致用户拒绝或越权后继续。版本查询不是验证证据，不能清除已有 UNKNOWN/验证失败；用户拒绝、只读、敏感/越界路径、禁止选项、取消和清理失败继续停止。REPLAN 不是工具内部自动执行替代命令。

保留 strict/relaxed/fullaccess、read_only、shell=False、可信解释器解析、参数白名单、文件审批、进程清理和原有协议边界。模型提示词和用户界面必须反映真实规则，不把单条命令拒绝描述成整个工具不可用。

保留全部已有未提交和未跟踪改动，使用第二轮重构后的规范模块路径。不读取 .env.local、凭据或真实会话数据库，不调用真实 Provider，不安装依赖、启动 Docker、提交/推送或执行破坏性 Git 操作。不要混入工作区扫描范围、沙箱、评测平台或审批缓存改造。

使用 fake Provider、合成工作区和现有 Python 完成计算器任务的真实进程验证；最后运行完整回归，更新 README.md、project.md 和 runtime/command-compatibility/ 中的证据。汇报实际改动、验证结果、剩余限制；未验证的能力不能宣称通过。
```
