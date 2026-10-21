# cf-cache-copy-isolation

cache.RecordCache.put(key,value)/get(key)和service.load_record(cache,key)：key字符串，value为可嵌套JSON兼容dict/list；put后原值变化不污染缓存，get结果变化不污染下次get，不同key独立；缺失key抛KeyError；service调用get。

允许修改：cache.py, service.py, tests/*.py（从根、同层匹配）。README及其他文件只读。

从工作区根运行 `python -m unittest discover -s tests -v`；可自行补测试。
非交互执行；修改后按当前验证要求检查并明确调用finish。纯确认轮不要求修改或测试。
