"""自迭代外置大脑：把 iter_cycle.py 的一圈结果搬进 AstrBot。

插件只读不烧模型：跑 --json 拿建议，格式化给主人看。
--commit（写 decision + log.md）只在管理员显式说「记录」时执行。
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
    "自迭代外置大脑：读 iter_cycle.py 的状态与下一步建议",
    "1.1.0",
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
        logger.info(f"[selfdrive] self_dir = {self.self_dir or '(未配置)'}")

    # ---------------- 内核 ----------------

    def _run(self, *args: str) -> tuple[int, str, str]:
        if not str(self.self_dir):
            return 1, "", "还没配置 self_dir（iter_cycle.py 所在目录）"
        script = self.self_dir / "iter_cycle.py"
        if not script.exists():
            return 1, "", f"找不到 {script}"
        try:
            r = subprocess.run(
                [self.python, str(script), *args],
                capture_output=True, timeout=self.timeout, cwd=str(self.self_dir),
            )
        except subprocess.TimeoutExpired:
            return 1, "", f"跑超时（>{self.timeout}s）"
        except Exception as e:
            return 1, "", str(e)
        return (r.returncode,
                r.stdout.decode("utf-8", "replace"),
                r.stderr.decode("utf-8", "replace"))

    @staticmethod
    def _summary(d: dict) -> str:
        """通用格式化：只显示 iter_cycle.py 真给出的字段，不假定任何业务字段。"""
        lines: list[str] = []
        core = [f"{label} {d[k]}" for k, label in
                (("pain", "痛"), ("level", "段位"), ("gate", "闸门"))
                if d.get(k) is not None]
        if core:
            lines.append(" / ".join(core))
        skip = {"pain", "level", "gate", "pick", "blocked"}
        for k, v in d.items():
            if k in skip or v is None or isinstance(v, (dict, list)):
                continue
            lines.append(f"{k}: {v}")
        pick = d.get("pick") or {}
        if isinstance(pick, dict) and pick:
            lines.append(f"建议：{pick.get('title', '(无标题)')}")
            meta = [f"{k} {v}" for k, v in pick.items()
                    if k != "title" and v not in (None, "", [], {})]
            if meta:
                lines.append(" · ".join(meta))
        else:
            lines.append(f"没有可选目标（{d.get('blocked') or '空的'}）")
        return "\n".join(lines) if lines else "(iter_cycle.py 没有输出可读字段)"

    # ---------------- 指令 ----------------

    @filter.command("自迭代")
    async def cmd_selfdrive(self, event: AstrMessageEvent, arg: str = ""):
        """看自迭代这一圈：/自迭代　/自迭代 详细　/自迭代 记录（管理员）"""
        a = (arg or "").strip()
        if a in ("记录", "commit"):
            fn = getattr(event, "is_admin", None)
            try:
                is_admin = bool(fn()) if callable(fn) else False
            except Exception:
                is_admin = False
            if not is_admin:
                yield event.plain_result("记录会写 decision 和 log.md，只给管理员")
                return
            code, out, err = self._run("--commit", "--json")
            if code != 0:
                yield event.plain_result(f"记录失败：{(err or out).strip()[:300]}")
                return
            try:
                d = json.loads(out)
                yield event.plain_result("已记录。\n" + self._summary(d))
            except Exception:
                yield event.plain_result("已记录。\n" + out.strip()[:600])
            return

        code, out, err = self._run("--json")
        if code != 0:
            yield event.plain_result(f"跑挂了：{(err or out).strip()[:300]}")
            return
        try:
            d = json.loads(out)
        except Exception:
            yield event.plain_result(out.strip()[:800] or "没有输出")
            return
        if a in ("详细", "detail", "all"):
            yield event.plain_result(json.dumps(d, ensure_ascii=False, indent=1)[:1500])
            return
        yield event.plain_result(self._summary(d))

    # ---------------- 给模型的工具 ----------------

    @filter.llm_tool(name="self_iter_status")
    async def self_iter_status(self, event: AstrMessageEvent) -> str:
        """查看自迭代当前状态：痛值、段位、闸门、下一步建议干什么。只读，不写文件、不烧模型。

        什么时候用：主人问"现在该干什么""自迭代到哪了""下一步做什么"。
        """
        code, out, err = self._run("--json")
        if code != 0:
            return f"自迭代算不出来：{(err or out).strip()[:200]}"
        try:
            d = json.loads(out)
        except Exception:
            return out.strip()[:400]
        return self._summary(d)
