<div align="center">

# 🐋 Self-Iteration Dashboard

**把本地自迭代循环 `iter_cycle.py` 的结果搬进 AstrBot 聊天窗口 — 默认只读，绝不自作主张。**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-v4-4B8BBE)](https://github.com/AstrBotDevs/AstrBot)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/shingflash/astrbot_plugin_selfdrive/pulls)

[English](#-english) · [中文](#-中文)

</div>

---

## 📖 English

### What it does

Reads the JSON output of a local `iter_cycle.py` (a self-iteration loop script) and renders it in chat.
It never starts an iteration on its own — every `/selfdrive` call is a **read**, with one exception:
`/selfdrive commit`, admin-only, forwards `--commit` to the script.

### ✨ Features

- **Read-only by default** — no side effects unless you explicitly commit
- **Schema-agnostic** — formats whatever fields `iter_cycle.py --json` returns
- **LLM tool included** — `self_iter_status` lets the model look up state on demand, write nothing
- **Path-configurable** — point it at any directory, no hard-coded paths

### 📦 Installation

1. Clone into your AstrBot plugin directory:

   ```bash
   cd data/plugins
   git clone https://github.com/shingflash/astrbot_plugin_selfdrive.git
   ```

2. Fill in `self_dir` (required) in the dashboard config.
3. Reload the plugin, or restart AstrBot.

### ⚙️ Configuration

| Key | Default | Description |
|---|---|---|
| `self_dir` | — | **Required.** Absolute path of the directory containing `iter_cycle.py` |
| `python` | empty | Interpreter path; empty uses AstrBot's own interpreter |
| `timeout` | `120` | Per-run timeout in seconds |

### 💬 Commands

| Command | Permission | Description |
|---|---|---|
| `/selfdrive` | everyone | Core state plus the suggested task |
| `/selfdrive detail` | everyone | Full JSON returned by `iter_cycle.py` |
| `/selfdrive commit` | **admin** | Runs `--commit` (writes decision + `log.md`) |

LLM tool: `self_iter_status` — the model queries it on demand, writes nothing.

### 📐 Contract

The plugin only reads `iter_cycle.py --json` output and formats whatever fields exist; it makes no assumptions about your business schema.

- `pick` — the suggested task
- `blocked` — the reason nothing was picked

---

## 📖 中文

### 它做什么

读取本地 `iter_cycle.py`（自迭代循环脚本）的 JSON 输出，在聊天里展示。
它自己从不主动触发迭代——每次 `/selfdrive` 都只是**读**，唯一例外是 `/selfdrive commit`：仅管理员可用，会带 `--commit` 转发给脚本。

### ✨ 特点

- **默认只读** —— 除非你明确 commit，否则不产生任何副作用
- **不绑定 schema** —— `iter_cycle.py --json` 给什么字段就格式化什么
- **自带 LLM 工具** —— `self_iter_status` 让模型按需查状态，不写任何东西
- **路径可配** —— 指向任意目录，不内置本机路径

### 📦 安装

1. 克隆进 AstrBot 插件目录：

   ```bash
   cd data/plugins
   git clone https://github.com/shingflash/astrbot_plugin_selfdrive.git
   ```

2. 在面板里填 `self_dir`（必填）。
3. 重载插件，或重启 AstrBot。

### ⚙️ 配置

| 项 | 默认 | 说明 |
|---|---|---|
| `self_dir` | — | **必填**。存放 `iter_cycle.py` 的目录绝对路径 |
| `python` | 空 | 解释器路径；留空则用 AstrBot 自己的解释器 |
| `timeout` | `120` | 单次运行超时（秒） |

### 💬 命令

| 命令 | 权限 | 作用 |
|---|---|---|
| `/selfdrive` | 所有人 | 核心状态 + 建议干的任务 |
| `/selfdrive detail` | 所有人 | `iter_cycle.py` 返回的完整 JSON |
| `/selfdrive commit` | **管理员** | 执行 `--commit`（写决策 + `log.md`） |

LLM 工具：`self_iter_status` —— 模型按需查询，不写任何东西。

### 📐 约定

插件只读 `iter_cycle.py --json` 的输出，有什么字段就格式化什么，不对业务 schema 做假设。

- `pick` —— 建议任务
- `blocked` —— 没挑出任务的原因

---

<div align="center">

### 📄 License

[MIT](LICENSE) · 有问题欢迎开 issue / PR

</div>
