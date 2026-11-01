"""Live run status: a line on stderr and .work/progress.json, which `knowlib status` reads."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

LOG_EVERY_S = 30
_s: dict = {}
_path: Path | None = None
_logged = 0.0


def start(work_dir: Path, total: int) -> None:
    global _path
    _path = work_dir / "progress.json"
    _s.clear()
    _s.update(running=True, started=time.time(), total=total, index=0, finished=0, spent_s=0.0, item="")
    _save()


def item(index: int, key: str) -> None:
    _s.update(index=index, item=key, item_started=time.time(), stage="", done=0, stage_total=0)
    _save()


def item_done() -> None:
    _s["finished"] = _s.get("finished", 0) + 1
    _s["spent_s"] = _s.get("spent_s", 0.0) + time.time() - _s.get("item_started", time.time())
    _save()


def stage(name: str, done: int = 0, total: int = 0) -> None:
    global _logged
    if not _s:
        return
    if name != _s.get("stage"):
        _s["stage_started"] = time.time()
    _s.update(stage=name, done=done, stage_total=total)
    _save()
    if time.time() - _logged >= LOG_EVERY_S or done == total:
        _logged = time.time()
        print(f"           {summary(_s)}", file=sys.stderr, flush=True)


def finish() -> None:
    _s.update(running=False, finished_at=time.time())
    _save()


def _fmt(seconds: float) -> str:
    return f"~{seconds / 60:.0f} min" if seconds < 5400 else f"~{seconds / 3600:.1f} h"


def summary(s: dict) -> str:
    parts = [f"item {s.get('index', 0)}/{s.get('total', 0)}"]
    if s.get("stage"):
        parts.append(f"{s['stage']} {s.get('done', 0)}/{s.get('stage_total', 0)}")
    if s.get("done") and s.get("stage_total"):
        left = (time.time() - s["stage_started"]) / s["done"] * (s["stage_total"] - s["done"])
        parts.append(f"{_fmt(left)} left in this stage")
    if s.get("finished"):
        left = s["spent_s"] / s["finished"] * (s["total"] - s["finished"])
        parts.append(f"{_fmt(left)} left overall")
    return " · ".join(parts)


def _save() -> None:
    if _path:
        _path.write_text(json.dumps(_s, ensure_ascii=False, indent=1), encoding="utf-8")


def read(work_dir: Path) -> dict | None:
    p = work_dir / "progress.json"
    return json.loads(p.read_text()) if p.exists() else None
