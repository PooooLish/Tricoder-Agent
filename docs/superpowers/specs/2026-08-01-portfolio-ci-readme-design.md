# Tricoder CLI 简历展示与 CI 设计

## 背景

Tricoder CLI 已经具备多 Provider、原生结构化工具调用、Session 独立记忆、斜杠命令、工具审计和真实 API 冒烟测试等 MVP 能力。当前仓库缺少自动化持续集成，也没有在 README 首屏集中说明这些工程亮点，招聘者难以快速判断项目是否可运行、是否经过跨平台验证。

本轮目标是把项目整理成可信、可复现的简历实操项目。暂不创建 Git 标签、GitHub Release、PyPI 包或自动发布流程。

## 方案比较

### 方案一：工程可信度优先（采用）

增加跨平台 CI，并重构 README 的信息层级，让读者能够在几分钟内理解项目、完成本地运行并看到验证证据。

优点：改动小、收益直接，最适合当前 MVP；不引入发布凭据和供应链复杂度。

局限：暂时没有安装包、演示 GIF 或正式版本发布。

### 方案二：文档深度优先

在方案一基础上补充 ADR、模块级架构文档和完整设计取舍。

优点：适合深入技术面试；局限：短期文档成本较高，且 README 首屏收益有限。

### 方案三：视觉展示优先

增加终端录屏、GIF、截图和演示视频。

优点：吸引力强；局限：素材容易随 CLI 变化而过期，且不能替代自动化验证。

## CI 设计

新增 `.github/workflows/ci.yml`，使用单一测试作业和矩阵策略：

- 触发条件：推送到 `main`、Pull Request，以及手动触发。
- 操作系统：`ubuntu-latest`、`windows-latest`。
- Python：3.11、3.12。
- 权限：仅授予 `contents: read`。
- 环境准备：使用 `actions/checkout@v6` 和 `actions/setup-python@v6`，明确指定 Python 版本。
- 安装：`python -m pip install -e .`。
- 验证：运行 `python -m unittest discover -s tests -v` 和 `python -m compileall -q src tests`。

矩阵共生成四个任务，可证明最低支持版本和主流开发版本在 Windows、Linux 上都可用。当前项目没有锁文件且仅有一个受版本范围约束的运行时依赖，因此本轮不启用依赖缓存，避免产生不稳定或收益有限的缓存键。

CI 不读取 `.env.local`，不配置 Provider 密钥，也不执行真实 API 冒烟测试。这样可以避免凭据进入自动化环境、外部 API 波动导致随机失败和意外费用。真实 API 测试继续作为开发者本地的显式操作。

所选 Action 主版本以当前官方仓库文档为准：

- [actions/checkout](https://github.com/actions/checkout)
- [actions/setup-python](https://github.com/actions/setup-python)
- [GitHub Actions Python 构建与测试文档](https://docs.github.com/actions/automating-builds-and-tests/building-and-testing-python)

## README 信息架构

README 保留已有准确内容，但调整首屏和导航顺序：

1. 项目标题、定位和 CI 徽章。
2. 核心亮点：三家 Provider、原生 structured tool calling、Session、斜杠命令、安全边界和审计。
3. 5 分钟快速体验：创建环境、安装、配置示例、启动 CLI。
4. 简洁架构图：CLI/Session/Agent Core/Provider/Tool Runtime 的数据流。
5. 能力边界：本地工具、审批、安全限制和失败处理。
6. 测试说明：单元测试、CI 覆盖范围、真实 API 冒烟测试的本地运行方式。
7. Provider 扩展说明、项目结构和后续路线。

README 中不写无法由代码或测试验证的性能数字，不展示真实密钥，不把本地冒烟测试描述成 CI 保证。已有详细章节能复用则移动或精简，避免重复维护两套说明。

## 安全与隐私

- `.env.local` 继续由 `.gitignore` 排除。
- CI 工作流不声明任何 Provider Secret。
- 示例统一使用占位符，不复制本机配置。
- README 明确区分无密钥单元测试与付费/联网的真实 API 冒烟测试。
- 本轮不增加具有写权限的 GitHub Token，也不执行发布操作。

## 验证与验收

本地验证：

1. `python -m unittest discover -s tests -v` 通过。
2. `python -m compileall -q src tests` 通过。
3. 检查 workflow YAML 结构和 Git 差异。
4. 搜索新增内容，确认不存在 API Key、Token 或本机私有路径。

远端验证：

1. 将改动推送到 `main` 后查看 GitHub Actions。
2. Ubuntu/Windows 与 Python 3.11/3.12 四个矩阵任务全部成功。
3. README 的 CI 徽章正确指向当前仓库和工作流。

验收标准：CI 四个任务全绿；README 首屏能够说明项目价值并给出可复制的快速体验；仓库中没有新增密钥或发布凭据；不创建标签或 Release。

## 暂不纳入范围

- GitHub Release、版本标签和自动发布。
- PyPI、wheel、sdist 和制品签名。
- 依赖锁定策略调整。
- 真实 API 的云端定时测试。
- GIF、视频和独立项目网站。
