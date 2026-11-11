"""How much of each free API model's quota is used, shared by every library and process on this machine.

Free tiers limit requests per minute (rpm), input tokens per minute (tpm) and requests per day (rpd),
per model and per account or project, not per library. The ledger counts requests and tokens per
provider/model in ~/.config/knowlib/usage.json (KNOWLIB_USAGE overrides the path), waits when a
minute's budget is spent, and marks a model exhausted until its day resets: the provider's own
midnight (Google resets at midnight Pacific time), or the retry time a 429 reply gives.
"""
from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_LOCK = threading.Lock()


def usage_path() -> Path:
    return Path(os.environ.get("KNOWLIB_USAGE") or Path.home() / ".config" / "knowlib" / "usage.json")


def day_key(tz: str | None, now: float | None = None) -> str:
    return datetime.fromtimestamp(now or time.time(), ZoneInfo(tz or "UTC")).strftime("%Y-%m-%d")


def next_reset(tz: str | None, now: float | None = None) -> float:
    """The provider's next midnight, as a timestamp."""
    t = datetime.fromtimestamp(now or time.time(), ZoneInfo(tz or "UTC"))
    return (t.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp()


@contextmanager
def _ledger():
    """The usage file, locked against other threads and processes while it is read and rewritten."""
    path = usage_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK, open(path.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
        except ValueError:
            data = {}
        yield data
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(path)


def _entry(data: dict, key: str, tz: str | None, now: float) -> dict:
    e = data.setdefault(key, {})
    today = day_key(tz, now)
    if e.get("day") != today:                       # a new day for this provider: start counting again
        e.update(day=today, requests=0, tokens=0)
        e.pop("exhausted_until", None) if e.get("exhausted_until", 0) <= now else None
    e["recent"] = [r for r in e.get("recent", []) if now - r[0] < 60]
    return e


def available(key: str, limits: dict, tz: str | None, now: float | None = None) -> bool:
    """Whether the model can take a request today (its daily count and any 429 lockout)."""
    now = now or time.time()
    with _ledger() as data:
        e = _entry(data, key, tz, now)
        if e.get("exhausted_until", 0) > now:
            return False
        rpd = e.get("learned_rpd") or limits.get("rpd")
        return not rpd or e["requests"] < int(rpd)


def reserve(key: str, limits: dict, tz: str | None, tokens: int) -> bool:
    """Wait until a request of about `tokens` input tokens fits the minute's budget, then count it.
    False if the day's budget is spent (or a 429 locked the model) while waiting."""
    rpm, tpm = limits.get("rpm"), limits.get("tpm")
    while True:
        now = time.time()
        with _ledger() as data:
            e = _entry(data, key, tz, now)
            rpd = e.get("learned_rpd") or limits.get("rpd")
            if e.get("exhausted_until", 0) > now or (rpd and e["requests"] >= int(rpd)):
                return False
            recent = e["recent"]
            fits = (not rpm or len(recent) < int(rpm)) and \
                   (not tpm or not recent or sum(r[1] for r in recent) + tokens <= int(tpm))
            if fits:
                recent.append([now, tokens])
                e["requests"] += 1
                e["tokens"] += tokens
                return True
            wait = 60 - (now - recent[0][0]) + 0.5 if recent else 1
        time.sleep(max(0.5, min(wait, 60)))


def exhaust(key: str, tz: str | None, until: float | None = None, learned_rpd: int | None = None) -> None:
    """A 429 said the day's quota is gone: skip the model until `until` (default: its next midnight)."""
    now = time.time()
    with _ledger() as data:
        e = _entry(data, key, tz, now)
        e["exhausted_until"] = until or next_reset(tz, now)
        if learned_rpd:
            e["learned_rpd"] = learned_rpd


def report(keys: list[tuple[str, dict, str | None]]) -> list[str]:
    """One line per model: requests used today out of the daily limit, or when it is free again."""
    now = time.time()
    lines = []
    with _ledger() as data:
        for key, limits, tz in keys:
            e = _entry(data, key, tz, now)
            rpd = e.get("learned_rpd") or limits.get("rpd")
            used = f"{e['requests']}/{rpd}" if rpd else f"{e['requests']}"
            if e.get("exhausted_until", 0) > now:
                left = int(e["exhausted_until"] - now)
                used += f" (quota spent; free again in {left // 3600}h{left % 3600 // 60:02d}m)"
            lines.append(f"{key}: {used} today")
    return lines
