# cf-package-api-migration

textkit.core.normalize_label已实现strip+lower且只读；包textkit同时导出新normalize_label及旧clean_label，report.label_line使用新导出输出label=<结果>；修实际导入链，空串可用。

允许修改：textkit/__init__.py, report.py, tests/*.py（从根、同层匹配）。README及其他文件只读。

从工作区根运行 `python -m unittest discover -s tests -v`；可自行补测试。
非交互执行；修改后按当前验证要求检查并明确调用finish。纯确认轮不要求修改或测试。
