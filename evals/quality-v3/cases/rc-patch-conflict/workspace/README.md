# rc-patch-conflict

tags.replace_tag(text,old,new)替换所有出现；old非空，new可为空，无匹配不变。首次写失败是模拟工具冲突，业务不包含重试计数。

允许修改：tags.py, tests/*.py（从根、同层匹配）。README及其他文件只读。

从工作区根运行 `python -m unittest discover -s tests -v`；可自行补测试。
非交互执行；修改后按当前验证要求检查并明确调用finish。纯确认轮不要求修改或测试。
