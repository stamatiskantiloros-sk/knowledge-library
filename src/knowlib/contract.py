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

SOURCE_TYPES = {"images", "pdf", "office", "web", "audio", "video", "session", "text"}

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


def contents(body: str, limit: int = 80) -> str:
    """A short table of contents from the ## headings, each with the page it starts on when the
    text has page markers, so an agent can jump to a place without reading the whole source.
    Empty when there are too few headings to help or too many to read."""
    page, entries = None, []
    for line in body.splitlines():
        m = re.match(r"<!-- page (\S+) \(pdf \d+\) -->$", line)
        if m:
            page = m[1]
        elif line.startswith("## ") and not line.startswith("## Contents"):
            entries.append(line[3:].strip() + (f", p. {page}" if page else ""))
    if not 3 <= len(entries) <= limit:
        return ""
    return "## Contents\n\n" + "\n".join(f"- {e}" for e in entries) + "\n"


class SourceWriter:
    """Writes one source atomically, so a crash mid-ingest never leaves a half-written source.

    Separate mode builds sources/<id>/ (source.md and assets/) in a staging folder and swaps it in.
    In place (layout.inplace) the Markdown goes beside the original, its images to the layout's
    assets folder, and image links are rewritten to point there."""

    def __init__(self, sources_dir: Path, source_id: str, *, layout=None, md: Path | None = None):
        self.layout, self.id = layout, source_id
        self.inplace = bool(layout and layout.inplace)
        if self.inplace:
            self.md = Path(md)
            self.final = layout.assets_dir(source_id)
            self.staging = layout.data / "staging" / source_id
        else:
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
        fm = record.frontmatter()                     # the mark near the top, before a long list of flags
        meta = {"id": fm.pop("id"), "generated_by": "knowlib", **fm} if "id" in fm else {"generated_by": "knowlib", **fm}
        body = body.strip()
        toc = contents(body)
        if toc:                                        # after the title line
            head, _, rest = body.partition("\n")
            body = f"{head}\n\n{toc}\n{rest.lstrip()}" if head.startswith("# ") else f"{toc}\n{body}"
        if self.inplace:
            rel = self.md.relative_to(self.layout.root).as_posix()
            # a lecture or image folder's Markdown stands for the whole folder
            meta["original"] = (rel.rsplit("/", 1)[0] + "/" if "/" in rel else "./") \
                if self.md.name.endswith((".lecture.md", ".images.md")) else rel[:-3]
            body = body.replace("](assets/", f"]({self.layout.asset_prefix(self.md, self.id)}/")
        fm = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, width=100)
        text = f"---\n{fm}---\n\n{body}\n"
        if not self.inplace:
            (self.staging / "source.md").write_text(text, encoding="utf-8")
            if not any(self.assets.iterdir()):
                self.assets.rmdir()
            if self.final.exists():
                shutil.rmtree(self.final)
            self.staging.rename(self.final)
            return self.final / "source.md"
        if self.final.exists():
            shutil.rmtree(self.final)
        if any(self.assets.iterdir()):
            self.final.parent.mkdir(parents=True, exist_ok=True)
            self.assets.rename(self.final)
        tmp = self.md.with_name(f".{self.md.name}.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.md)
        shutil.rmtree(self.staging, ignore_errors=True)
        return self.md

    def abort(self) -> None:
        shutil.rmtree(self.staging, ignore_errors=True)


def read_frontmatter(source_md: Path) -> dict:
    text = Path(source_md).read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    return yaml.safe_load(text[4:end]) or {} if end > 0 else {}
