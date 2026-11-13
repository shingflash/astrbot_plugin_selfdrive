# CHANGELOG

## 2.0.0

- Repo scope changed: from "dashboard plugin" to **loop core + thin shell**
- Added `core/` — 12 scripts: `iter_cycle` (scheduler/pick/gate/pain), `curiosity`,
  `ape_pool`, `practice_pool`, `inspiration`, `ledger`, `score_judge`, `score_report`,
  `score_panel`, `housekeep`, `collect_signals`, `perf_audit`, plus `score_rubric.json`
- Added `docs/SELF_EVOLUTION.md` (the method) and `docs/PRIORITY.md` (value ordering)
- Added `examples/goals.example.json`
- Added `.gitignore`: runtime state (`wallet.json`, `archive.jsonl`, `log.md`,
  `curiosity.md`, `iter_stats.json`, `round_costs.jsonl`) is generated locally and never committed
- Removed a mistakenly committed `__pycache__/main.cpython-314.pyc`
- README rewritten to explain the design (pools → pick → execute → machine review →
  archive → settle) instead of listing features

## Unreleased

- README rewritten as bilingual (English + 中文) with badges and a sectioned layout

## 1.0.2

- All in-code instruction text switched to English: command `/selfdrive`,
  parameter aliases `commit` / `detail`, log and reply strings
- Removed hardcoded machine paths; `self_dir` is a required config value
- Generic formatter: only fields actually returned by `iter_cycle.py` are shown
- Non-numeric `timeout` falls back to 120

## 1.0.0

- Initial release: `/selfdrive` command + `self_iter_status` tool
