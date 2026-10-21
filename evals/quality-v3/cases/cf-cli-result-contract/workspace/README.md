# cf-cli-result-contract

cli.main(argv)->int及python cli.py入口：恰好两个必需整数参数，成功stdout一行十进制和、stderr空、退出0；缺失/非法参数返回及退出2、stdout空、stderr非空诊断；支持负整数。main应返回码而非抛SystemExit，模块入口负责退出。arithmetic.add已经正确且只读。

允许修改：cli.py, tests/*.py（从根、同层匹配）。README及其他文件只读。

从工作区根运行 `python -m unittest discover -s tests -v`；可自行补测试。
非交互执行；修改后按当前验证要求检查并明确调用finish。纯确认轮不要求修改或测试。
