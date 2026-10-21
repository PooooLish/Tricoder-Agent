# cf-page-merge

pagination.merge_pages及service.collect：按页与页内顺序以整数id稳定去重，第一条完整记录胜出，空页不终止，空输入[]；不修改输入页或记录，无HTTP/cursor服务。

允许修改：pagination.py, service.py, tests/*.py（从根、同层匹配）。README及其他文件只读。

从工作区根运行 `python -m unittest discover -s tests -v`；可自行补测试。
非交互执行；修改后按当前验证要求检查并明确调用finish。纯确认轮不要求修改或测试。
