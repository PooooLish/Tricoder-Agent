# rc-provider-transient

counter.increment：任何整数加1，包括负数、零、大整数。本题无故障注入，仅评分业务正确性；保留题ID用于追溯。

允许修改：counter.py, tests/*.py（从根、同层匹配）。README及其他文件只读。

从工作区根运行 `python -m unittest discover -s tests -v`；可自行补测试。
非交互执行；修改后按当前验证要求检查并明确调用finish。纯确认轮不要求修改或测试。
