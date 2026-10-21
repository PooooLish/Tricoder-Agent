# sf-tail-boundary

sequence_ops.tail(values,count)：count是非负整数；末尾count项保序，0返回[]，超过长度返回全部，不修改输入；负count不评分。

允许修改：sequence_ops.py, tests/*.py（从根、同层匹配）。README及其他文件只读。

从工作区根运行 `python -m unittest discover -s tests -v`；可自行补测试。
非交互执行；修改后按当前验证要求检查并明确调用finish。纯确认轮不要求修改或测试。
