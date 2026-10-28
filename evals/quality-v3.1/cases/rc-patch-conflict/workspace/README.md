# rc-patch-conflict

tags.replace_tag(text,old,new)替换所有出现；old非空，new可为空，无匹配不变。首次写失败是模拟工具冲突，业务不包含重试计数。

允许修改：tags.py, tests/*.py（从根、同层匹配）。README及其他文件只读。

从工作区根运行 `python -m unittest discover -v`；可自行补测试。
非交互执行；修改后按当前验证要求检查并明确调用finish。纯确认轮不要求修改或测试。

检查后查看宿主 pending_paths / blocking_checks：失败检查须修复并按同一检查复验，覆盖不足须补相关检查；读文件不补覆盖，检查后再修改需要重新检查。语法检查不能替代必要的业务测试；任务完成后明确调用 finish。定向测试可用于诊断，不作为唯一默认交付检查。纯确认轮只确认并 finish，不运行无关测试。
