# sf-empty-input

stats_ops.mean：数字列表空时0.0，否则真实平均值；支持混合正负、小数和单元素，不修改输入。

允许修改：stats_ops.py, tests/*.py（从根、同层匹配）。README及其他文件只读。

从工作区根运行 `python -m unittest discover -s tests -v`；可自行补测试。
非交互执行；修改后按当前验证要求检查并明确调用finish。纯确认轮不要求修改或测试。
