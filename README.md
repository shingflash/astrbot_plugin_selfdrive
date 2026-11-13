# Self-iteration dashboard

Surface `iter_cycle.py` results inside AstrBot. Read-only by default.

## Configuration

| Key | Description |
|---|---|
| `self_dir` | **Required**. Absolute path of the directory containing `iter_cycle.py` |
| `python` | Interpreter path; empty uses AstrBot's own interpreter |
| `timeout` | Per-run timeout in seconds, default 120 |

## Usage

- `/selfdrive` - core state plus the suggested task
- `/selfdrive detail` - full JSON returned by `iter_cycle.py`
- `/selfdrive commit` - admins only; runs `--commit` (writes decision + log.md)
- LLM tool `self_iter_status` - model queries it on demand, writes nothing

## Contract

The plugin only reads `iter_cycle.py --json` output and formats whatever
fields exist; it assumes no business schema. `pick` is the suggested task,
`blocked` is the reason nothing was picked.
