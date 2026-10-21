# cf-parameter-validation

validator.valid_quantity：输入为整数且不含bool；仅1—100有效。service.accept同步返回ok或invalid，不要求字符串转换。

允许修改：validator.py, tests/*.py（从根、同层匹配）。README及其他文件只读。

从工作区根运行 `python -m unittest discover -s tests -v`；可自行补测试。
非交互执行；修改后按当前验证要求检查并明确调用finish。纯确认轮不要求修改或测试。
