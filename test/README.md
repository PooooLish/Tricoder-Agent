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
