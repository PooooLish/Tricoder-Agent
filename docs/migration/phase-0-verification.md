# Hcode 迁移 Phase 0 验证记录

验证日期：2026-09-03

平台：Windows `10.0.26200`

TriCoder HEAD：`72a501f12182f5b948b87d6d9281dd0e62e40e91`

Hcode HEAD：`d77927f7f2079b103c8077d321f0b24077c75800`

## 环境与基线

项目 `.venv` 已在用户明确授权后由系统 CPython `3.11.6` 重建，并以 editable
模式安装项目自身及 `pyproject.toml` 已声明的 Rich/Textual 依赖。旧 Python 3.10
环境保存在 `runtime/env-backups/venv-py310-20260903/`，没有删除。没有安装 MCP、
PyYAML 或其他迁移候选依赖。

| 命令 | 退出码 | 实际结果 |
| --- | ---: | --- |
| `.\.venv\Scripts\python.exe -B -m unittest discover -s tests` | 0 | `Ran 545 tests in 72.607s`；OK，4 skipped |
| `.\.venv\Scripts\python.exe -B -m compileall -q src tests` | 0 | 通过，无输出 |
| `.\.venv\Scripts\python.exe -B -m tricoder eval evals\smoke --dry-run --no-color` | 0 | `suite=smoke cases=3`；`fix-subtract`、`add-validation`、`cross-file-feature` 均为 `validated` |

当前 Python 3.11 基线为绿色。测试输出中的 Textual 慢任务诊断提示没有导致失败。

## 初始失败诊断与修复

| 字段 | 证据 |
| --- | --- |
| Symptom | 单测的 9 个 `_FailedTest` 和 Eval dry-run 均在 `src/tricoder/evals/loader.py:9` 抛出 `ModuleNotFoundError: No module named 'tomllib'` |
| Reproduction | 上表两条退出码 1 的精确命令稳定复现 |
| Hypothesis | 项目 `.venv` 的 Python 3.10 不含 Python 3.11 引入的标准库 `tomllib` |
| Disconfirming observation | 若同一解释器可解析 `tomllib`，或错误在 Python 3.11+ 环境仍出现，则该假设不成立 |
| Minimal experiment | `.\.venv\Scripts\python.exe -B -c "import sys, importlib.util; print(sys.version); print('tomllib_spec=', importlib.util.find_spec('tomllib'))"` |
| Result | 输出 Python `3.10.16` 且 `tomllib_spec= None`；与 `requires-python >=3.11` 的声明共同确认环境版本不兼容 |
| Action | 用户随后明确授权重建验证环境；旧环境可恢复地移入 `runtime/env-backups/`，新 `.venv` 使用 Python 3.11.6，并仅安装项目既有声明依赖 |

`compileall` 只验证语法编译，不执行导入，因此它通过并不能推翻上述根因。
重建后同一组命令全部通过，进一步确认初始失败属于解释器版本不兼容，而不是
TriCoder 功能代码缺陷。

## Git 与来源基线

- TriCoder：`main`，相对 `origin/main` 为 `ahead 19`，remote 为
  `git@github.com:PooooLish/Tricoder-Agent.git`。
- Hcode：`main` 与 `origin/main` 对齐，remote 为
  `git@github.com:PooooLish/Hcode.git`。
- Hcode 根 `LICENSE` 为 MIT，版权为 `Copyright (c) 2026 PooooLish`。
- TriCoder 没有根 `LICENSE` 或 `SECURITY.md`，`pyproject.toml` 没有许可证声明。
- 两份迁移设计/计划均存在，可按 UTF-8 完整读取。
- 进入本任务前的 TriCoder 修改和未跟踪文件均视为用户内容；本阶段没有还原、
  暂存或提交它们。

## 完成状态

用户已于 2026-09-03 选择 MIT，许可证门已经解决并建立了 `LICENSE`、`NOTICE`
和项目元数据；Python 3.11 基线已经转绿。用户随后明确授权删除
`test/README.md` 末尾的一个既存多余空行，全仓库 `git diff --check` 已通过。
Phase 0 完成，Phase 1 未开始。
