"""Process inbox: route -> skip unchanged -> ingest -> record provenance -> report.

QA and synthesis are the agent's job (see CLAUDE.md); this writes .work/last_run.json so the
agent knows exactly which sources are new, which failed and which carry flags.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import sys
import time
import traceback
from pathlib import Path

from . import ingestors  # noqa: F401  - registers all ingestors
from . import progress
from .config import load_config
from .contract import hash_file, hash_text, now_iso
from .ingestors.base import Context, InputItem, find_ingestor
from .manifest import Manifest
from .providers.agent import AgentJobsWaiting
from .router import discover

log = logging.getLogger("knowlib")


def open_library(root: Path, overrides: dict | None = None, force: bool = False) -> tuple[Context, Manifest]:
    root = Path(root).expanduser().resolve()
    cfg = load_config(root, overrides)
    lc = cfg["library"]
    ctx = Context(root=root, config=cfg, sources_dir=root / lc["sources"], work_dir=root / lc["work"],
                  inbox=root / lc["inbox"], force=force)
    for d in (ctx.sources_dir, ctx.work_dir, ctx.inbox, root / lc["notes"]):
        d.mkdir(parents=True, exist_ok=True)
    ctx.manifest = Manifest(root / "manifest.json")
    return ctx, ctx.manifest


class HashCache:
    """sha256 of large videos is slow; reuse it while size and mtime are unchanged."""

    def __init__(self, path: Path):
        self.path = path
        self.data = json.loads(path.read_text()) if path.exists() else {}

    def file(self, p: Path) -> str:
        st = p.stat()
        k = str(p.resolve())
        hit = self.data.get(k)
        if hit and hit[0] == st.st_size and hit[1] == st.st_mtime:
            return hit[2]
        h = hash_file(p)
        self.data[k] = [st.st_size, st.st_mtime, h]
        return h

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data))


def item_hash(item: InputItem, hc: HashCache) -> str:
    if item.kind == "url":
        return hash_text(item.url or "")
    if item.kind == "session":
        import hashlib
        h = hashlib.sha256()
        for p in sorted(item.paths, key=lambda p: p.name):
            h.update(p.name.encode())
            h.update(hc.file(p).encode())
        if item.sidecar:
            h.update(json.dumps(item.sidecar, sort_keys=True, default=str).encode())
        return h.hexdigest()
    return hc.file(item.path)


def link_source(ctx: Context, key: str, source_id: str, layout: str) -> None:
    """A browsable tree in organized/, shaped by library.layout. Sources stay flat under sources/,
    so ids and citations never change when the layout does."""
    parts = key.rstrip("/").split("/")
    date = re.search(r"\d{4}[-_.]\d{1,2}[-_.]\d{1,2}|\d{1,2}[-_.]\d{1,2}[-_.]\d{4}", key)
    fields = {"course": parts[0], "folder": "/".join(parts[:-1]) or parts[0],
              "name": Path(parts[-1]).stem, "date": date.group(0) if date else "undated"}
    link = ctx.root / "organized" / layout.format(**fields)
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        link.unlink()
    link.symlink_to(ctx.sources_dir / source_id, target_is_directory=True)


def process(root: Path, *, force: bool = False, only: str | list[str] | None = None, dry_run: bool = False,
            overrides: dict | None = None, limit: int | None = None, only_lectures: list[str] | None = None) -> dict:
    ctx, manifest = open_library(root, overrides, force)
    ctx.config["agent_dir"] = str(ctx.work_dir / "agent")
    hc = HashCache(ctx.work_dir / "hash_cache.json")
    items = discover(ctx.inbox, ctx.config["library"].get("ignore"))
    layout = ctx.config["library"].get("layout")
    if ctx.inbox.name != ctx.config["library"]["inbox"]:     # an --input folder: keep same names apart
        for i in items:
            i.key = f"{ctx.inbox.name}/{i.key}"
    report = {"started_at": now_iso(), "new": [], "updated": [], "unchanged": 0, "failed": [],
              "unsupported": [], "flags": {}, "renamed": [], "waiting": [], "orphans": []}
    seen_keys = set()
    only_list = [only] if isinstance(only, str) else list(only or [])
    wanted = sum(1 for i in items if not only_list or any(o in i.key for o in only_list))
    if not dry_run:
        progress.start(ctx.work_dir, wanted)
    position = 0
    for item in items:
        seen_keys.add(item.key)
        if only_list and not any(o in item.key for o in only_list):
            continue
        position += 1
        if limit is not None and len(report["new"]) + len(report["updated"]) + len(report["failed"]) >= limit:
            break
        item.content_hash = item_hash(item, hc)
        if not force and not manifest.needs_processing(item.key, item.content_hash):
            report["unchanged"] += 1
            continue
        ing = find_ingestor(item)
        if ing is None:
            report["unsupported"].append(item.key)
            continue
        if only_lectures and ing.name == "media" and not any(o.casefold() in item.key.casefold() for o in only_lectures):
            continue
        if dry_run:
            report["new"].append({"key": item.key, "ingestor": ing.name, "dry_run": True})
            continue
        prev = manifest.entries.get(item.key, {})
        t0 = time.time()
        print(f"[{time.strftime('%H:%M:%S')}] {ing.name:6s} {item.key}", file=sys.stderr, flush=True)
        progress.item(position, item.key)
        try:
            res = ing.ingest(item, ctx)
        except AgentJobsWaiting as e:
            print(f"           waiting: {e} in {ctx.config['agent_dir']}/jobs.json", file=sys.stderr, flush=True)
            report["waiting"].append(item.key)
            progress.item_done()
            continue
        except Exception as e:  # noqa: BLE001 - one bad file must not stop the batch
            log.error("failed %s: %s", item.key, e)
            manifest.record_failure(item.key, content_hash=item.content_hash, ingestor=ing.name,
                                    error=traceback.format_exc())
            report["failed"].append({"key": item.key, "error": f"{type(e).__name__}: {e}"})
            progress.item_done()
            continue
        progress.item_done()
        old_id = prev.get("source_id")
        if old_id and old_id != res.source_id:
            shutil.rmtree(ctx.sources_dir / old_id, ignore_errors=True)
            report["renamed"].append({"from": old_id, "to": res.source_id})
        manifest.record_success(item.key, content_hash=item.content_hash, source_id=res.source_id,
                                ingestor=ing.name, pipeline=res.pipeline, flags=res.flags)
        entry = {"key": item.key, "source_id": res.source_id, "ingestor": ing.name,
                 "seconds": round(time.time() - t0, 1)}
        print(f"           done in {entry['seconds']}s -> sources/{res.source_id}"
              + (f"  ({len(res.flags)} flags)" if res.flags else ""), file=sys.stderr, flush=True)
        report["updated" if prev.get("status") in ("done", "stale") else "new"].append(entry)
        if res.flags:
            report["flags"][res.source_id] = res.flags
        hc.save()
        if layout:
            link_source(ctx, item.key, res.source_id, layout)
    hc.save()
    report["orphans"] = [k for k in manifest.entries if k not in seen_keys and not k.startswith("http")]
    report["finished_at"] = now_iso()
    report["pending_synthesis"] = manifest.pending_synthesis()
    if not dry_run:
        progress.finish()
        (ctx.work_dir / "last_run.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return report
