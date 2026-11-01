"""The output contract every ingestor writes.

    sources/<id>/source.md   extracted text with YAML frontmatter
    sources/<id>/assets/     frames, figures, page renders

QA, synthesis and search depend only on this, never on which ingestor or model produced it.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any

import yaml

SOURCE_TYPES = {"pdf", "office", "web", "audio", "video", "session", "text"}

# Greek -> Latin, so ids stay ASCII and work in any tool or URL.
_GREEK = str.maketrans({
    "α": "a", "β": "v", "γ": "g", "δ": "d", "ε": "e", "ζ": "z", "η": "i", "θ": "th",
    "ι": "i", "κ": "k", "λ": "l", "μ": "m", "ν": "n", "ξ": "x", "ο": "o", "π": "p",
    "ρ": "r", "σ": "s", "ς": "s", "τ": "t", "υ": "y", "φ": "f", "χ": "ch", "ψ": "ps", "ω": "o",
})


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def slugify(text: str, max_len: int = 48) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn").translate(_GREEK)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return (text[:max_len].rstrip("-")) or "source"


def make_id(title: str, content_hash: str) -> str:
    return f"{slugify(title)}-{content_hash[:8]}"


def hash_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def hash_many(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in sorted(paths, key=lambda p: p.name):
        h.update(p.name.encode())
        h.update(hash_file(p).encode())
    return h.hexdigest()


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def tool_version(package: str) -> str | None:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return None


def stage(tool: str, backend: str | None = None, model: str | None = None,
          package: str | None = None, **extra: Any) -> dict:
    """One provenance entry for the `pipeline` frontmatter field."""
    entry = {"tool": tool, "backend": backend or tool, "model": model,
             "version": tool_version(package or tool)}
    entry.update({k: v for k, v in extra.items() if v is not None})
    return {k: v for k, v in entry.items() if v is not None}


@dataclass
class SourceRecord:
    id: str
    title: str
    source_type: str
    origin: list[str]
    hash: str
    language: str | None = None
    original_date: str | None = None
    ingested_at: str = field(default_factory=now_iso)
    fetch_date: str | None = None
    pipeline: dict[str, dict] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    reliability: str | None = None
    related: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def frontmatter(self) -> dict:
        assert self.source_type in SOURCE_TYPES, self.source_type
        data = asdict(self)
        order = ["id", "title", "source_type", "origin", "original_date", "ingested_at",
                 "fetch_date", "language", "pipeline", "hash", "flags", "tags",
                 "reliability", "related", "stats"]
        return {k: data[k] for k in order if data[k] not in (None, [], {})}


class SourceWriter:
    """Builds sources/<id>/ in a staging folder, then swaps it in atomically,
    so a crash mid-ingest never leaves a half-written source behind."""

    def __init__(self, sources_dir: Path, source_id: str):
        self.final = Path(sources_dir) / source_id
        self.staging = Path(sources_dir) / f".{source_id}.tmp"
        if self.staging.exists():
            shutil.rmtree(self.staging)
        self.assets = self.staging / "assets"
        self.assets.mkdir(parents=True)

    def asset_path(self, name: str) -> Path:
        p = self.assets / name
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def commit(self, record: SourceRecord, body: str) -> Path:
        fm = yaml.safe_dump(record.frontmatter(), allow_unicode=True, sort_keys=False, width=100)
        (self.staging / "source.md").write_text(f"---\n{fm}---\n\n{body.strip()}\n", encoding="utf-8")
        if not any(self.assets.iterdir()):
            self.assets.rmdir()
        if self.final.exists():
            shutil.rmtree(self.final)
        self.staging.rename(self.final)
        return self.final / "source.md"

    def abort(self) -> None:
        shutil.rmtree(self.staging, ignore_errors=True)


def read_frontmatter(source_md: Path) -> dict:
    text = Path(source_md).read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    return yaml.safe_load(text[4:end]) or {} if end > 0 else {}
