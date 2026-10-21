# cf-record-schema-compat

records.normalize(record)及loader.load_records(text)：JSON列表，字段为字符串，v1 id/full_name、v2 id/name；输出只保留id/name；name存在时优先（包括空串）；缺两者抛ValueError，保留id、丢弃额外字段、不修改输入。

允许修改：records.py, loader.py, tests/*.py（从根、同层匹配）。README及其他文件只读。

从工作区根运行 `python -m unittest discover -s tests -v`；可自行补测试。
非交互执行；修改后按当前验证要求检查并明确调用finish。纯确认轮不要求修改或测试。
