# rc-search-truncated

searcher.find_prefix(values,prefix)：ASCII前缀忽略大小写，保留原拼写、顺序及重复项；空前缀返回全部，空列表为空。首次匹配read_file/search_text/glob工具可能返回模拟OUTPUT_LIMIT，请根据工具反馈继续读取/搜索；不要在业务代码实现截断或第一次少返回。

允许修改：searcher.py, tests/*.py（从根、同层匹配）。README及其他文件只读。

从工作区根运行 `python -m unittest discover -v`；可自行补测试。
非交互执行；修改后按当前验证要求检查并明确调用finish。纯确认轮不要求修改或测试。

检查后查看宿主 pending_paths / blocking_checks：失败检查须修复并按同一检查复验，覆盖不足须补相关检查；读文件不补覆盖，检查后再修改需要重新检查。语法检查不能替代必要的业务测试；任务完成后明确调用 finish。定向测试可用于诊断，不作为唯一默认交付检查。纯确认轮只确认并 finish，不运行无关测试。
