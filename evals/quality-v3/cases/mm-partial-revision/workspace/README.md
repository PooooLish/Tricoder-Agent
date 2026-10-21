# mm-partial-revision

实现formatter.render(text,prefix)：返回prefix+text.strip()，正文保留大小写；本轮需要实际修改、验证并finish。

允许修改：formatter.py, tests/*.py（从根、同层匹配）。README及其他文件只读。

从工作区根运行 `python -m unittest discover -s tests -v`；可自行补测试。
非交互执行；修改后按当前验证要求检查并明确调用finish。纯确认轮不要求修改或测试。
