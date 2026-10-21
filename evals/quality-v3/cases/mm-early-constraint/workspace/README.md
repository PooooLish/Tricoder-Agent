# mm-early-constraint

记住约束：渲染器必须使用调用者传入的颜色，保留文本。

允许修改：renderer.py, tests/*.py（从根、同层匹配）。README及其他文件只读。

从工作区根运行 `python -m unittest discover -s tests -v`；可自行补测试。
非交互执行；修改后按当前验证要求检查并明确调用finish。纯确认轮不要求修改或测试。
