# TriCoder 手动测试沙盒

此目录用于手动体验 TriCoder 的代码读取、修改审批和命令执行流程。

- 可以在这里创建任意练习代码。
- 不要放置真实密钥、私人数据或重要文件。
- 自动化测试仍位于项目根目录的 `tests/`。
- 本目录不是独立 Git 仓库，也不会自动安装依赖。

## 使用示例

1. **读取文件**：使用 `list_files` 查看目录结构，用 `read_file` 查看文件内容。
2. **修改文件**：先 `read_file` 获取精确文本，再通过 `edit_file` 替换内容。
3. **运行命令**：例如，创建一个 Python 测试脚本 `hello.py`：

   ```python
   # hello.py
   print("Hello, TriCoder!")
   ```

   然后用 `run_command` 执行 `python hello.py` 验证输出。

通过以上三步即可体验完整的读取、审批修改与命令执行流程。

## 贪吃蛇小游戏

本目录提供了一个控制台版贪吃蛇游戏。

### 文件

- `snake_game.py` — 游戏主体：核心逻辑 `SnakeGame`、终端渲染 `TerminalRenderer`、入口 `main()`
- `test_snake_game.py` — 核心逻辑的自动测试

### 启动游戏

```bash
python snake_game.py
```

### 操作方式

| 按键 | 功能 |
|------|------|
| 方向键 / W A S D | 控制蛇移动 |
| `r` | 重新开始 |
| `q` | 退出游戏 |

### 游戏规则

- 蛇每吃到一个食物（`*`）长度增长 1，分数 +1
- 撞墙或撞到自己则游戏结束
- 填满整个棋盘即可获胜

### 运行测试

```bash
python -m pytest test_snake_game.py
```
