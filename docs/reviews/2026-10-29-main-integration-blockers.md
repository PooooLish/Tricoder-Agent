# 2026-10-29 main 集成阻塞记录

## 当前结论

尚未合并。用户已授权合入main，并明确不再需要Python 3.12；CI已仅保留Windows/Python 3.11。PR为 https://github.com/PooooLish/Tricoder-Agent/pull/1 。远端候选为 `cca55358e89b571026276db6c488a342e88f1e03`；本地与远端main仍为 `a81ec2457ece970756b00424393e5f2df9253858`。

不得将本地全量通过写成PR CI通过，也不得将移除3.12写成修复3.12。新发现需要coding session处理；reviewer本轮未修改生产逻辑。本文及交接更新仅作本地文档提交，不触发第三次无代码变更的CI重试。

## 已完成的集成与环境修复

- 已推送三个提交：`51f9da2`量化指标，`eedeb28`交付指引及quality-v3.1，`cca5535`干净检出CI修复。PR共252个文件；真实凭据、用户数据库、runtime原始日志未纳入。
- `.github/workflows/ci.yml`仅3.11，并在测试前创建runtime；`.gitattributes`保持evals文件精确字节；`tests/test_git_diff_availability.py`仅将测试临时根路径resolve；README同步准备方式。
- 初次CI的428项runtime缺失错误、4项换行哈希失败、2项路径模拟失败在后续运行不再出现。
- 全部原始证据保留在 `runtime/mg28/`，没有覆盖旧日志；Agent生产代码及题库内容保持已复审版本。

## 实际验证

| 验证 | 结果 |
| --- | --- |
| 本地完整unittest（CI修复前） | 2259项，2244通过、15跳过，0失败/错误；632.117秒 |
| 本地compileall | 通过 |
| 干净检出专项（CI修复后） | 22项，21通过、1项符号链接权限跳过 |
| autocrlf=true检出 | 修复前766份eval文件中641份变更；修复后766份均字节一致 |
| CI `40721403072` attempt 1，Python3.11.9 | 2259项，1失败、4跳过，398.137秒；编译步骤未执行 |
| 失败时间排序测试本地重复 | 连续50次通过，100个实际公开/隐藏验证子进程，无清理失败 |
| 同一提交CI attempt 2 | 2259项，1错误、4跳过，550.954秒；编译步骤未执行 |
| 资源所有权注入探针 | 返回值和异常两条路径均丢失资源所有权，见下文 |

第一次CI失败为 `tests/test_eval_quality_v3_review_fixes.py:37` 的公开验证结果断言，入口为第57行的 `test_reviewer_time_control`。只记录了 `verification_error`，没有底层异常/清理详情，根因未确认；第二次完整CI该测试通过。

第二次CI失败为 `tests/test_eval_runner.py:369` 的输出超限场景结束后，第39行 `self.temporary.cleanup()` 报WinError 32，后续目录删除回调亦出现WinError 5。测试主体的输出超限/marker断言没有报失败，错误发生在tearDown；不能将其报告为正常测试通过。日志没有持锁PID，不能据此断言一定是Agent残留子进程，也不能直接归因于杀毒软件或宿主环境。

## 独立确认的问题：P2，评测验证器丢失清理失败资源

位置：`src/tricoder/evals/runner.py:327`（捕获异常后直接映射）和第335行（cleanup_failed返回值映射）。

`run_bounded_process`通过 `BoundedProcessResult.cleanup_resource` 或 `ProcessExecutionUncertain.cleanup_resource` 交还尚未清理完的精确资源。评测器在无外层TaskCleanupScope时直接返回不含资源所有者的VerificationResult，既不保留该对象，也不交给上层所有者。`src/tricoder/process/control.py:77` 只有存在cleanup scope时才会自行retain；普通eval验证调用不建立该scope。失败映射不能代替资源所有权交接。

独立注入两条路径：均返回verification_error；weakref在回收后失效；资源cleanup调用次数0；返回结果没有资源引用。探针没有创建真进程，仅证明所有权传递缺口，不是WinError32的实机复现。相关映射代码在当前main中已经存在，并非本轮量化指标新增；它与上述两次CI失败的因果关系仍未证实。

涉及实机清理的后续检查位置为 `src/tricoder/process/control.py:259` 的finish、291行的进程树终止、323行起的Windows Job关闭，以及创建/分配Job的路径。不可仅凭当前证据预设某一Windows API就是根因。

## 下一步及验收要求

1. 在测试/受控诊断中记录安全的失败阶段、异常类型和WinError、bounded结果的timeout/cleanup/output标志，以及精确所属资源状态；不得输出凭据、完整环境或私有数据。
2. 分别处理验证器返回值和异常携带的资源：保证失败后仍有明确所有者与有界清理路径；不能重置既有终止期限、盲目重放命令，或靠删除报错/忽略cleanup_errors掩盖问题。保持验证器结果的安全序列化边界。
3. 用确定性回归覆盖两种资源交还路径，结合Windows真实子进程验证输出超限后的退出及工作目录立即清理。若目录占用属于外部环境，先提供可核实证据，再设计测试隔离方案。
4. 修复后运行针对性回归、完整2259项（新增测试相应增加）及compileall，并在Windows/Python3.11干净CI通过后独立复审。不得放宽默认权限、题库判分、取消/超时规则；不推进Replan，不调用真实模型，不安装本机依赖，不读取.env.local。
5. 本地/远端main保持不动。未关闭上述阻塞项之前不合并，不以挑选一次绿色重跑代替缺陷处理。原搜索超时共享期限P2仍按原复审结论关闭；本问题属于eval验证器失败资源交接。

## Coding session提示词

> 先读取工作区与项目AGENTS.md、README.md、pyproject.toml，以及docs/reviews/2026-10-29-main-integration-blockers.md和当前reviewer交接记录。当前分支codex/eval-metrics-delivery-guidance，PR #1；main尚未合并。Python3.12不再作为CI要求。保留已有提交与runtime/mg28全部证据。
>
> 优先为eval runner的cleanup_resource所有权缺口建立确定性回归，再修复返回值/异常两条失败路径的资源保留、上层交接和有界清理。同时为两次Windows3.11 CI失败补足不泄密的诊断，验证它们是否与该缺口相关，不把推测写成根因。保持已有终止期限、取消行为、验证判分与默认权限；禁止通过跳过测试、忽略目录清理失败或无依据sleep掩盖问题。不要自动恢复3.12、推进Replan、调用真实模型、安装依赖、读取.env.local或真实用户库。完成后提供变更清单、独立新日志、针对性/全量/干净CI结果并更新交接，交reviewer复审后再合并。

## 证据索引

- `full-result.json`、`full.log`、`compileall-result.json`：本地全量。
- `clean-checkout-focused.json/log`、`checkout-red.json`、`checkout-green.json`：环境修复验证。
- `ci-recheck-full.log`、`ci-recheck-result.json`：attempt1失败。`ci-recheck-unit-result.json`因GH日志标注UNKNOWN STEP而未解析出计数；准确计数见原日志及`ci-unreproduced-failure.json`，不能将null当作0。
- `verifier-repeat50.json/log`：时间排序连续50次本地验证。
- `ci-confirm-full.log`、`ci-confirm-result.json`、`ci-confirm-unit-result.json`：attempt2失败。summary中skipped为null是解析器只提取OK行所致；原日志明确skipped=4。
- `verifier-ownership.json`、`probe_verifier_ownership.py`：资源所有权探针。
- `integration-result.json`：最终状态blocked，非合并成功收据。
