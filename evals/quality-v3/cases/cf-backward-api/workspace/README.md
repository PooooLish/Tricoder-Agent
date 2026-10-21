# cf-backward-api

converter.to_slug及旧slugify都保留：转小写并将连续空白分隔为单连字符，空字符串为空；client.route返回/docs/+slug。允许包装或别名。

允许修改：converter.py, tests/*.py（从根、同层匹配）。README及其他文件只读。

从工作区根运行 `python -m unittest discover -s tests -v`；可自行补测试。
非交互执行；修改后按当前验证要求检查并明确调用finish。纯确认轮不要求修改或测试。
