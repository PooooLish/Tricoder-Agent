# sf-csv-records

csv_records.parse_records(text)解析合法CSV，默认excel方言（与csv.reader一致），逗号分隔、双引号引用及双写转义，CRLF/LF及引号内换行、空字段；空文档[]；不要求非法CSV容错。

允许修改：csv_records.py, tests/*.py（从根、同层匹配）。README及其他文件只读。

从工作区根运行 `python -m unittest discover -s tests -v`；可自行补测试。
非交互执行；修改后按当前验证要求检查并明确调用finish。纯确认轮不要求修改或测试。
