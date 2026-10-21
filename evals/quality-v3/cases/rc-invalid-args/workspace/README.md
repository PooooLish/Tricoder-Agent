# rc-invalid-args

normalize.normalize字符串首尾去空白并小写，内部空白保留，空白串为空。首次写操作错误为模拟注入信号，不表示实际参数有误；修复业务即可。

允许修改：normalize.py, tests/*.py（从根、同层匹配）。README及其他文件只读。

从工作区根运行 `python -m unittest discover -s tests -v`；可自行补测试。
非交互执行；修改后按当前验证要求检查并明确调用finish。纯确认轮不要求修改或测试。
