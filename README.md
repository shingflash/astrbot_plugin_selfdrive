# 自迭代外置大脑

把 `iter_cycle.py` 的一圈结果搬进 AstrBot，只读不烧模型。

## 配置

| 项 | 说明 |
|---|---|
| `self_dir` | **必填**，`iter_cycle.py` 所在目录的绝对路径 |
| `python` | 解释器，留空用 AstrBot 自己的 python |
| `timeout` | 单次执行超时，默认 120 秒 |

## 用法

- `/自迭代`：核心状态 + 建议目标
- `/自迭代 详细`：`iter_cycle.py` 输出的完整 JSON
- `/自迭代 记录`：管理员专用，跑 `--commit` 写 decision 与 log.md
- 工具 `self_iter_status`：模型自己查，不写文件

## 契约

插件只读 `iter_cycle.py --json` 的输出，按字段存在与否通用格式化，
不假定任何业务字段；`pick` 是建议对象，`blocked` 是阻塞原因。
