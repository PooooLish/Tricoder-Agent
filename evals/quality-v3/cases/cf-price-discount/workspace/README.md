# cf-price-discount

pricing.final_price和checkout.total：非负price，discount_percent为0—100，返回price乘以剩余百分比；包括小数，不要求额外舍入。

允许修改：pricing.py, tests/*.py（从根、同层匹配）。README及其他文件只读。

从工作区根运行 `python -m unittest discover -s tests -v`；可自行补测试。
非交互执行；修改后按当前验证要求检查并明确调用finish。纯确认轮不要求修改或测试。
