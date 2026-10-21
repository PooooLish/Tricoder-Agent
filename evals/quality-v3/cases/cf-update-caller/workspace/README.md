# cf-update-caller

calculator.multiply已正确且只读；只修report.product_line调用，返回product=<a*b>，支持负数和零。

允许修改：report.py, tests/*.py（从根、同层匹配）。README及其他文件只读。

从工作区根运行 `python -m unittest discover -s tests -v`；可自行补测试。
非交互执行；修改后按当前验证要求检查并明确调用finish。纯确认轮不要求修改或测试。
