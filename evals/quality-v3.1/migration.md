# V3 → V3.1 指引迁移

从当前已复审的quality-v3逐字节复制。仅suite的id/benchmark_version、四实验experiment_id、30份workspace/README.md及本层方法说明变化；新增source-v3-sha256.json。suite title保持原值，suite引用仍为各自目录的`.`。case-matrix与source-v2.2清单保留历史来源，不重写业务映射。

根目录unittest discover是默认交付检查；定向tests发现可诊断，但不自动证明根源码覆盖。检查后看pending/blocking，必要业务测试不得以compileall替代，显式finish仍必要。纯确认无修改不要求测试。

新旧业务定义、workspace源码/public测试、hidden逐字节一致，30quality/0contract、22dev/8holdout、16/16000以及故障、steps、scope、AND不变。跨版本分数不能归因于仅Agent改进。本轮只离线建设，不授权真实模型复测。
