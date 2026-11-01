"""Vision through the Claude Code session, so no API key is needed. A reading is a file: knowlib
lists the frames it needs in .work/agent/jobs.json and stops, subagents write one answer per job,
and the next run picks the answers up. Everything before and after stays local and cached.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .base import VisionProvider


class AgentPending(Exception):
    """The reply to this request has not been written yet."""


class AgentJobsWaiting(Exception):
    """A source stops here until the session has answered its jobs."""


_waiting: dict[Path, dict[str, dict]] = {}


class AgentProvider(VisionProvider):
    backend = "agent"
    is_local = False

    def __init__(self, section: dict, folder: Path):
        self.model = section.get("agent_model", "sonnet")
        self.folder = folder

    def generate(self, images: list[Path], prompt: str, *, json_mode: bool = False,
                 max_tokens: int | None = None) -> str:
        names = "|".join(str(Path(p).resolve()) for p in images)   # frame names repeat across lectures
        job = hashlib.sha256(f"{self.model}\n{names}\n{prompt}".encode()).hexdigest()[:16]
        answer = self.folder / "answers" / f"{job}.md"
        if answer.exists():
            return answer.read_text(encoding="utf-8").strip()
        _waiting.setdefault(self.folder, {})[job] = {"id": job, "model": self.model, "prompt": prompt,
                                                     "images": [str(p) for p in images], "answer": str(answer)}
        raise AgentPending(job)


def stop_if_waiting(folder: Path) -> None:
    """Write the jobs this source is missing, on top of those of earlier sources in the run."""
    new = _waiting.pop(folder, None)
    if not new:
        return
    (folder / "answers").mkdir(parents=True, exist_ok=True)
    path = folder / "jobs.json"
    old = json.loads(path.read_text()) if path.exists() else []
    jobs = [j for j in old if j["id"] not in new and not Path(j["answer"]).exists()] + list(new.values())
    path.write_text(json.dumps(jobs, ensure_ascii=False, indent=1), encoding="utf-8")
    raise AgentJobsWaiting(f"{len(new)} jobs")
