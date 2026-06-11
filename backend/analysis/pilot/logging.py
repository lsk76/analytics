"""
Tiny logging helper for opinion-monitor commands.

Goal: when a command runs in background (or as a worker), we still want to
know how far it got. Plain stdout disappears into the docker logs and is hard
to find later. So every command opens a timestamped log file in
/app/backend/_pilot_logs/ and writes progress lines there.

Usage in a Django command:

    from analysis.pilot.logging import open_log
    log, log_path = open_log("monitor_collect", task_slug=task.slug)
    log("start ...")        # writes to both stdout AND the log file
    log(f"  chunk 1: +123")
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Tuple

LOG_DIR = Path("/app/backend/_pilot_logs")


def open_log(cmd_name: str, task_slug: str | None = None,
             extra_tag: str | None = None) -> Tuple[Callable[[str], None], Path]:
    """Open a log file under LOG_DIR and return (log_fn, path).

    log_fn(msg) writes msg to the log AND to stdout, with auto-flush so
    a tail of the file shows progress in real time.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    parts = [cmd_name, ts]
    if task_slug: parts.append(task_slug.replace("/", "_"))
    if extra_tag: parts.append(extra_tag)
    path = LOG_DIR / ("_".join(parts) + ".log")
    fp = open(path, "w", buffering=1)
    fp.write(f"# {datetime.now(timezone.utc).isoformat()} | "
             f"{cmd_name} | task={task_slug or '-'} | extra={extra_tag or '-'}\n\n")
    fp.flush()

    def log(msg: str = "") -> None:
        line = msg if msg.endswith("\n") else msg + "\n"
        # mirror to stdout (so the user still sees it interactively)
        try:
            sys.stdout.write(line); sys.stdout.flush()
        except Exception:
            pass
        try:
            fp.write(line); fp.flush()
        except Exception:
            pass

    return log, path
