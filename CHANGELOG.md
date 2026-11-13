# CHANGELOG

## 1.1.0

- 去本机路径：`self_dir` 默认值置空，改为面板必填
- 格式化改为通用模式：只显示 `iter_cycle.py` 真给出的字段，不再硬编码余额/工资等业务字段
- `/自迭代 详细` 输出完整 JSON，不再白名单截断
- `timeout` 配置非数字时兜底 120 秒

## 1.0.0

- 接入 iter_cycle.py：`/自迭代` 指令 + `self_iter_status` 工具
