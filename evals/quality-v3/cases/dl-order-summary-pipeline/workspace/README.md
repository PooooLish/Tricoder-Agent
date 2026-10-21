# dl-order-summary-pipeline

reader.read_orders(text)按CSV读取固定表头customer,amount,status为dict列表；totals.summarize(rows)及pipeline.build_report(text)只统计status恰为paid，amount是可带负号的整数字符串，按customer累加整数分；重复客户合并、空数据{}、其他状态忽略，customer按CSV原样保留（包括引号内逗号）；不涉及浮点货币、日期或网络。

允许修改：reader.py, totals.py, pipeline.py, tests/*.py（从根、同层匹配）。README及其他文件只读。

从工作区根运行 `python -m unittest discover -s tests -v`；可自行补测试。
非交互执行；修改后按当前验证要求检查并明确调用finish。纯确认轮不要求修改或测试。
