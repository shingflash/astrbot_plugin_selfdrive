# CHANGELOG

## 1.0.2

- All in-code instruction text switched to English: command `/selfdrive`,
  parameter aliases `commit` / `detail`, log and reply strings
- Removed hardcoded machine paths; `self_dir` is a required config value
- Generic formatter: only fields actually returned by `iter_cycle.py` are shown
- Non-numeric `timeout` falls back to 120

## 1.0.0

- Initial release: `/selfdrive` command + `self_iter_status` tool
