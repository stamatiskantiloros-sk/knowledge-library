"""Live run status: a progress line on stderr and .work/progress.json, which `knowlib status` reads.
In a terminal the line redraws in place; in a log file it is written every LOG_EVERY_S seconds."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

LOG_EVERY_S = 30
REDRAW_S = 0.5
BAR_WIDTH = 20
_s: dict = {}
_path: Path | None = None
_logged = 0.0
_live = False          # a redrawn line is on screen without its newline


def start(work_dir: Path, total: int) -> None:
    global _path
    _path = work_dir / "progress.json"
    _s.clear()
    _s.update(running=True, started=time.time(), total=total, index=0, finished=0, spent_s=0.0, item="")
    _save()


def item(index: int, key: str) -> None:
    end_line()
    _s.update(index=index, item=key, item_started=time.time(), stage="", done=0, stage_total=0)
    _save()


def item_done() -> None:
    end_line()
    _s["finished"] = _s.get("finished", 0) + 1
    _s["spent_s"] = _s.get("spent_s", 0.0) + time.time() - _s.get("item_started", time.time())
    _save()


def stage(name: str, done: int = 0, total: int = 0) -> None:
    """Report a stage of the current item. total 0 means a step that cannot count its progress."""
    global _logged, _live
    if not _s:
        return
    if name != _s.get("stage"):
        end_line()
        _s["stage_started"] = time.time()
        _logged = 0.0
    _s.update(stage=name, done=done, stage_total=total)
    _save()
    now = time.time()
    if sys.stderr.isatty():
        if now - _logged >= REDRAW_S or done == total:
            _logged = now
            print(f"\r\033[K           {summary(_s)}", end="", file=sys.stderr, flush=True)
            _live = True
            if total and done == total:
                end_line()
    elif now - _logged >= LOG_EVERY_S or done == total:
        _logged = now
        print(f"           {summary(_s)}", file=sys.stderr, flush=True)


def end_line() -> None:
    """Finish a redrawn line, so the next message starts on its own line."""
    global _live
    if _live:
        print(file=sys.stderr, flush=True)
        _live = False


def finish() -> None:
    end_line()
    _s.update(running=False, finished_at=time.time())
    _save()


def _fmt(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} s"
    return f"~{seconds / 60:.0f} min" if seconds < 5400 else f"~{seconds / 3600:.1f} h"


def bar(done: int, total: int) -> str:
    filled = round(BAR_WIDTH * done / total) if total else 0
    return "[" + "█" * filled + "░" * (BAR_WIDTH - filled) + "]"


def summary(s: dict) -> str:
    parts = [f"item {s.get('index', 0)}/{s.get('total', 0)}"]
    if s.get("stage_total"):
        parts.append(f"{s['stage']} {bar(s.get('done', 0), s['stage_total'])} {s.get('done', 0)}/{s['stage_total']}")
    elif s.get("stage"):
        parts.append(f"{s['stage']} (running for {_fmt(time.time() - s.get('stage_started', time.time()))})")
    if s.get("done") and s.get("stage_total"):
        left = (time.time() - s["stage_started"]) / s["done"] * (s["stage_total"] - s["done"])
        parts.append(f"{_fmt(left)} left in this stage")
    if s.get("finished") and s.get("total", 0) > s["finished"]:
        left = s["spent_s"] / s["finished"] * (s["total"] - s["finished"])
        parts.append(f"{_fmt(left)} left overall")
    return " · ".join(parts)


def _save() -> None:
    if _path:
        _path.write_text(json.dumps(_s, ensure_ascii=False, indent=1), encoding="utf-8")


def read(work_dir: Path) -> dict | None:
    p = work_dir / "progress.json"
    return json.loads(p.read_text()) if p.exists() else None
