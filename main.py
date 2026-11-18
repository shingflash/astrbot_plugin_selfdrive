"""Self-iteration dashboard: surface iter_cycle.py results inside AstrBot.

Read-only by default. The only write path is `--commit`, gated behind an
admin check.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@register(
    "astrbot_plugin_selfdrive",
    "shingflash",
    "Self-iteration dashboard: read iter_cycle.py state and next-step advice",
    "2.0.0",
)
class SelfDrivePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.self_dir = Path(str(self.config.get("self_dir") or "").strip())
        self.python = str(self.config.get("python") or sys.executable)
        try:
            self.timeout = int(self.config.get("timeout", 120) or 120)
        except (TypeError, ValueError):
            self.timeout = 120
        logger.info(f"[selfdrive] self_dir = {self.self_dir or '(unset)'}")

    # ---------------- core ----------------

    def _run(self, *args: str) -> tuple[int, str, str]:
        if not str(self.self_dir):
            return 1, "", "self_dir is not configured (directory holding iter_cycle.py)"
        script = self.self_dir / "iter_cycle.py"
        if not script.exists():
            return 1, "", f"script not found: {script}"
        try:
            r = subprocess.run(
                [self.python, str(script), *args],
                capture_output=True, timeout=self.timeout, cwd=str(self.self_dir),
            )
        except subprocess.TimeoutExpired:
            return 1, "", f"timed out after {self.timeout}s"
        except Exception as e:
            return 1, "", str(e)
        return (r.returncode,
                r.stdout.decode("utf-8", "replace"),
                r.stderr.decode("utf-8", "replace"))

    @staticmethod
    def _summary(d: dict) -> str:
        """Format whatever iter_cycle.py returns; never assume business fields."""
        lines: list[str] = []
        core = [f"{k} {d[k]}" for k in ("pain", "level", "gate") if d.get(k) is not None]
        if core:
            lines.append(" / ".join(core))
        skip = {"pain", "level", "gate", "pick", "blocked"}
        for k, v in d.items():
            if k in skip or v is None or isinstance(v, (dict, list)):
                continue
            lines.append(f"{k}: {v}")
        pick = d.get("pick") or {}
        if isinstance(pick, dict) and pick:
            lines.append(f"suggestion: {pick.get('title', '(untitled)')}")
            meta = [f"{k} {v}" for k, v in pick.items()
                    if k != "title" and v not in (None, "", [], {})]
            if meta:
                lines.append(" | ".join(meta))
        else:
            lines.append(f"no candidate ({d.get('blocked') or 'empty'})")
        return "\n".join(lines) if lines else "(iter_cycle.py returned no readable fields)"

    # ---------------- commands ----------------

    @filter.command("selfdrive")
    async def cmd_selfdrive(self, event: AstrMessageEvent, arg: str = ""):
        """Show this iteration: /selfdrive, /selfdrive detail, /selfdrive commit (admin only)"""
        a = (arg or "").strip().lower()
        if a in ("commit", "record"):
            fn = getattr(event, "is_admin", None)
            try:
                is_admin = bool(fn()) if callable(fn) else False
            except Exception:
                is_admin = False
            if not is_admin:
                yield event.plain_result("Commit writes decision + log.md; admins only.")
                return
            code, out, err = self._run("--commit", "--json")
            if code != 0:
                yield event.plain_result(f"commit failed: {(err or out).strip()[:300]}")
                return
            try:
                d = json.loads(out)
                yield event.plain_result("committed.\n" + self._summary(d))
            except Exception:
                yield event.plain_result("committed.\n" + out.strip()[:600])
            return

        code, out, err = self._run("--json")
        if code != 0:
            yield event.plain_result(f"run failed: {(err or out).strip()[:300]}")
            return
        try:
            d = json.loads(out)
        except Exception:
            yield event.plain_result(out.strip()[:800] or "no output")
            return
        if a in ("detail", "all", "verbose"):
            yield event.plain_result(json.dumps(d, ensure_ascii=False, indent=1)[:1500])
            return
        yield event.plain_result(self._summary(d))

    # ---------------- llm tool ----------------

    @filter.llm_tool(name="self_iter_status")
    async def self_iter_status(self, event: AstrMessageEvent) -> str:
        """Read the self-iteration state: pain, level, gate, and the next suggested task.

        Use when the user asks what to do next, how far the loop got, or whether
        there is budget left. Read-only: writes nothing, calls no model.
        """
        code, out, err = self._run("--json")
        if code != 0:
            return f"self-iteration unavailable: {(err or out).strip()[:200]}"
        try:
            d = json.loads(out)
        except Exception:
            return out.strip()[:400]
        return self._summary(d)
