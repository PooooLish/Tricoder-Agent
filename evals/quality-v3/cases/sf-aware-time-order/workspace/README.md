# sf-aware-time-order

time_order.sort_events(events)：记录timestamp为带Z或±HH:MM偏移的合法ISO8601时间；按UTC时刻升序，相同时稳定，空列表可用；不修改输入列表或记录内容。

允许修改：time_order.py, tests/*.py（从根、同层匹配）。README及其他文件只读。

从工作区根运行 `python -m unittest discover -s tests -v`；可自行补测试。
非交互执行；修改后按当前验证要求检查并明确调用finish。纯确认轮不要求修改或测试。
