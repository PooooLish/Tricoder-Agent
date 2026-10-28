# cf-cache-copy-isolation

cache.RecordCache.put(key,value)/get(key)和service.load_record(cache,key)：key字符串，value为可嵌套JSON兼容dict/list；put后原值变化不污染缓存，get结果变化不污染下次get，不同key独立；缺失key抛KeyError；service调用get。

允许修改：cache.py, service.py, tests/*.py（从根、同层匹配）。README及其他文件只读。

从工作区根运行 `python -m unittest discover -v`；可自行补测试。
非交互执行；修改后按当前验证要求检查并明确调用finish。纯确认轮不要求修改或测试。

检查后查看宿主 pending_paths / blocking_checks：失败检查须修复并按同一检查复验，覆盖不足须补相关检查；读文件不补覆盖，检查后再修改需要重新检查。语法检查不能替代必要的业务测试；任务完成后明确调用 finish。定向测试可用于诊断，不作为唯一默认交付检查。纯确认轮只确认并 finish，不运行无关测试。
