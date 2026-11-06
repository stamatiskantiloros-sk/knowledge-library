"""The ingestor interface and registry.

Every ingestor implements can_handle(item) and ingest(item, ctx) -> IngestResult, and writes
sources/<id>/ through SourceWriter. New ingestors register with @register and need no changes
anywhere else.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("knowlib")

AUDIO_EXT = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".aif", ".aiff", ".caf", ".wma"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mts", ".wmv"}
SUBTITLE_EXT = {".srt", ".vtt"}
PDF_EXT = {".pdf"}
OFFICE_EXT = {".docx", ".pptx", ".xlsx", ".odt", ".odp", ".rtf", ".doc", ".ppt"}
HTML_EXT = {".html", ".htm"}
URL_EXT = {".url", ".webloc"}
IGNORE_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", "session.yaml", "session.yml"}


@dataclass
class InputItem:
    """One unit of work: a single file, a media session (several files), or a URL."""
    kind: str                     # "file" | "session" | "url"
    key: str                      # manifest key: inbox-relative path, session folder, or URL
    paths: list[Path] = field(default_factory=list)
    url: str | None = None
    sidecar: dict[str, Any] = field(default_factory=dict)
    folder: Path | None = None    # session folder (or the file's folder)
    content_hash: str | None = None   # set by the runner (cached), reused by ingestors

    @property
    def path(self) -> Path:
        return self.paths[0]

    @property
    def suffix(self) -> str:
        return self.path.suffix.lower() if self.paths else ""


@dataclass
class Context:
    root: Path
    config: dict
    sources_dir: Path
    work_dir: Path
    inbox: Path
    manifest: Any = None
    force: bool = False
    layout: Any = None            # knowlib.layout.Layout: where sources, assets and caches go
    item: Any = None              # the InputItem being ingested, set by the runner

    def writer(self, source_id: str):
        """A SourceWriter for the current item, in this library's layout."""
        from ..contract import SourceWriter
        if self.layout is None or not self.layout.inplace:
            return SourceWriter(self.sources_dir, source_id)
        return SourceWriter(self.sources_dir, source_id, layout=self.layout,
                            md=self.layout.output_for(self.item, source_id))

    def work_for(self, name: str) -> Path:
        p = self.work_dir / name
        p.mkdir(parents=True, exist_ok=True)
        return p


@dataclass
class IngestResult:
    source_id: str
    source_md: Path
    pipeline: dict
    flags: list[str]


class Ingestor(ABC):
    name: str = "base"

    @abstractmethod
    def can_handle(self, item: InputItem) -> bool: ...

    @abstractmethod
    def ingest(self, item: InputItem, ctx: Context) -> IngestResult: ...


_REGISTRY: list[Ingestor] = []


def register(cls):
    _REGISTRY.append(cls())
    return cls


def registry() -> list[Ingestor]:
    return list(_REGISTRY)


def find_ingestor(item: InputItem) -> Ingestor | None:
    for ing in _REGISTRY:
        if ing.can_handle(item):
            return ing
    return None


def first_working(backends: list[str], runners: dict, *args, **kwargs):
    """Try backends in order; return (backend_name, result, errors). Import errors and
    tool failures fall through to the next backend so a missing optional dependency
    degrades gracefully instead of stopping the batch."""
    errors = []
    for b in backends:
        fn = runners.get(b)
        if fn is None:
            errors.append(f"{b}: unknown backend")
            continue
        try:
            return b, fn(*args, **kwargs), errors
        except Exception as e:  # noqa: BLE001 - deliberate fallthrough
            log.warning("backend %s failed: %s", b, e)
            errors.append(f"{b}: {type(e).__name__}: {e}")
    raise RuntimeError("all backends failed: " + "; ".join(errors))
