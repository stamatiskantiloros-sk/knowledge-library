"""manifest.json: what has been processed, from which original, and how.

Keyed by the input's key (relative inbox path, session folder, or URL). Re-runs skip
inputs whose content hash is unchanged; provenance queries drive selective reprocessing
("everything transcribed with a smaller Whisper model").
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from collections.abc import Iterator

from .contract import now_iso


class Manifest:
    def __init__(self, path: Path):
        self.path = Path(path)
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.data = {"version": 1, "entries": {}}
        self.entries: dict[str, dict] = self.data["entries"]

    def save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def needs_processing(self, key: str, content_hash: str) -> bool:
        e = self.entries.get(key)
        return not e or e.get("hash") != content_hash or e.get("status") in ("failed", "stale")

    def record_success(self, key: str, *, content_hash: str, source_id: str, ingestor: str,
                       pipeline: dict, flags: list[str], local_only: bool = False) -> None:
        prev = self.entries.get(key, {})
        self.entries[key] = {
            "hash": content_hash, "status": "done", "source_id": source_id,
            "ingestor": ingestor, "pipeline": pipeline, "flags": flags,
            "first_ingested_at": prev.get("first_ingested_at", now_iso()),
            "updated_at": now_iso(), "synthesized": False,
            # confidential once means confidential after: re-ingesting without the flag does not clear it
            "local_only": bool(local_only or prev.get("local_only")),
        }
        self.save()

    def record_failure(self, key: str, *, content_hash: str, ingestor: str, error: str) -> None:
        e = self.entries.setdefault(key, {})
        e.update({"hash": content_hash, "status": "failed", "ingestor": ingestor,
                  "error": error[-2000:], "updated_at": now_iso()})
        self.save()

    # ---- queries -------------------------------------------------------------
    def done(self) -> Iterator[tuple[str, dict]]:
        for k, e in self.entries.items():
            if e.get("status") == "done":
                yield k, e

    def match(self, where: dict[str, str]) -> list[str]:
        """Keys whose provenance matches, e.g. {"transcription.model": "small"}.
        Values match by substring so "small" catches "whisper-small" and "distil-small"."""
        hits = []
        for k, e in self.entries.items():
            ok = True
            for path, want in where.items():
                val: Any = e.get("pipeline", {})
                for part in path.split("."):
                    val = val.get(part) if isinstance(val, dict) else None
                if val is None or str(want).lower() not in str(val).lower():
                    ok = False
                    break
            if ok:
                hits.append(k)
        return hits

    def mark_stale(self, keys: list[str]) -> None:
        for k in keys:
            if k in self.entries:
                self.entries[k]["status"] = "stale"
        self.save()

    def pending_synthesis(self) -> list[str]:
        return [e["source_id"] for _, e in self.done() if not e.get("synthesized")]

    def mark_synthesized(self, source_ids: list[str]) -> int:
        n = 0
        for _, e in self.done():
            if e["source_id"] in source_ids:
                e["synthesized"] = True
                e["synthesized_at"] = now_iso()
                n += 1
        self.save()
        return n
