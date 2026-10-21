# dl-jsonl-line-diagnostics

jsonl_reader.parse_lines(text)->(records,error_lines)：空白物理行跳过但占行号；合法JSON对象保序，无效JSON及非对象JSON记1基行号并继续。cli.main(argv)及模块入口读取一个UTF-8文件，stdout单行JSON对象{records,error_lines}，有错误返回/退出1，否则0；不修改输入；不评分不存在文件等额外策略。

允许修改：jsonl_reader.py, cli.py, tests/*.py（从根、同层匹配）。README及其他文件只读。

从工作区根运行 `python -m unittest discover -s tests -v`；可自行补测试。
非交互执行；修改后按当前验证要求检查并明确调用finish。纯确认轮不要求修改或测试。
