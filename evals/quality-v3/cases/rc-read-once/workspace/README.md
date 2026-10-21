# rc-read-once

parser.parse_number解析合法有符号十进制整数文本，支持首尾空白。模拟首次read_file失败属于工具信号，继续完成业务修复；不评分非法文本。

允许修改：parser.py, tests/*.py（从根、同层匹配）。README及其他文件只读。

从工作区根运行 `python -m unittest discover -s tests -v`；可自行补测试。
非交互执行；修改后按当前验证要求检查并明确调用finish。纯确认轮不要求修改或测试。
