# cf-module-interface

api.display(first,last)按单空格连接并title；修复formatter与api实际调用契约，可修改其中一个或两个文件，不得以同名class冒充模块。

允许修改：formatter.py, api.py, tests/*.py（从根、同层匹配）。README及其他文件只读。

从工作区根运行 `python -m unittest discover -s tests -v`；可自行补测试。
非交互执行；修改后按当前验证要求检查并明确调用finish。纯确认轮不要求修改或测试。
