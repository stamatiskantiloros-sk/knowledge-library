"""Where a library keeps things. Two modes, chosen at setup (library.mode):

separate  the original design: originals in inbox/, each source in sources/<id>/source.md with
          its images in sources/<id>/assets/, caches in .work/, config.yaml and manifest.json at
          the top.
inplace   the library is an existing folder of material, used as it is. Every original gets its
          Markdown beside it, named after the whole file name (inventory.pdf -> inventory.pdf.md,
          a lecture folder X/ -> X/X.lecture.md, a folder's loose images -> X/X.images.md), so it never collides with the user's own .md
          files. Images, caches, the manifest and config.yaml live in one visible folder at the
          top (library.data, default _knowlib/). Notes, INDEX.md, glossary.md and AGENTS.md sit at
          the top as in separate mode.

Everything that needs a source's files asks this module, so ingestors, checks, digests and
handbooks work the same in both modes. Notes cite the stable source id either way.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

GENERATED_MARK = "generated_by: knowlib"


def open_layout(root: Path, overrides: dict | None = None):
    """(Layout, Manifest) for a library, read-only: for commands that look things up."""
    from .config import load_config
    from .manifest import Manifest
    lay = Layout(Path(root).expanduser().resolve(), load_config(root, overrides))
    return lay, Manifest(lay.manifest)


class Layout:
    def __init__(self, root: Path, cfg: dict):
        self.root = Path(root)
        lc = cfg["library"]
        self.inplace = lc.get("mode") == "inplace"
        self.data = self.root / lc.get("data", "_knowlib")
        self.notes = self.root / lc["notes"]
        if self.inplace:
            self.inbox = self.root
            self.work = self.data / "work"
            self.sources = self.data / "assets"           # per-source assets live here
            self.manifest = self.data / "manifest.json"
        else:
            self.inbox = self.root / lc["inbox"]
            self.work = self.root / lc["work"]
            self.sources = self.root / lc["sources"]
            self.manifest = self.root / "manifest.json"

    # ---- where a new source goes ------------------------------------------------------------

    def output_for(self, item, source_id: str) -> Path:
        """The Markdown path for an item being ingested."""
        if not self.inplace:
            return self.sources / source_id / "source.md"
        if item.kind == "session" and item.key.endswith("/"):
            folder = self.inbox / item.key.rstrip("/")
            return folder / f"{folder.name}.lecture.md"
        if item.kind == "images":
            folder = item.folder or self.inbox
            return folder / f"{folder.name}.images.md"
        if item.kind == "url":
            slug = re.sub(r"[^a-z0-9]+", "-", re.sub(r"^https?://(www\.)?", "", item.url or "").lower()).strip("-")[:60]
            return (item.folder or self.inbox) / f"{slug}.web.md"
        return self.inbox / f"{item.key}.md"

    def assets_dir(self, source_id: str) -> Path:
        return self.sources / source_id if self.inplace else self.sources / source_id / "assets"

    # ---- finding existing sources -----------------------------------------------------------

    def source_md(self, source_id: str, entry: dict | None = None) -> Path:
        if self.inplace and entry and entry.get("output"):
            return self.root / entry["output"]
        return self.sources / source_id / "source.md"

    def digest_md(self, source_id: str, entry: dict | None = None) -> Path:
        md = self.source_md(source_id, entry)
        return md.with_name(md.name[:-3] + ".digest.md") if self.inplace else md.with_name("digest.md")

    def all_sources(self, entries: dict) -> dict[str, Path]:
        """{source id: its Markdown} for every finished source that exists on disk."""
        out = {}
        for e in entries.values():
            sid = e.get("source_id")
            if e.get("status") == "done" and sid:
                md = self.source_md(sid, e)
                if md.exists():
                    out[sid] = md
        return out

    def remove(self, source_id: str, entry: dict | None = None) -> None:
        """Delete a source's generated files (when its id changes)."""
        if self.inplace:
            md = self.source_md(source_id, entry)
            if md.exists() and self.is_generated(md):
                md.unlink()
            shutil.rmtree(self.assets_dir(source_id), ignore_errors=True)
        else:
            shutil.rmtree(self.sources / source_id, ignore_errors=True)

    # ---- links from the Markdown to its images -----------------------------------------------

    def asset_prefix(self, md: Path, source_id: str) -> str:
        """How the Markdown at md refers to its assets folder: 'assets' in separate mode, a
        relative path such as '../_knowlib/assets/<id>' in place."""
        if not self.inplace:
            return "assets"
        return Path(os.path.relpath(self.assets_dir(source_id), md.parent)).as_posix()

    @property
    def index(self) -> Path:
        """INDEX.md in place (it sits among the user's folders), index.md in a separate library."""
        return self.root / ("INDEX.md" if self.inplace else "index.md")

    def is_generated(self, path: Path) -> bool:
        """True for files knowlib wrote, so the crawler never ingests its own output."""
        if path.suffix.lower() != ".md":
            return False
        try:                     # anywhere in the frontmatter: older files have it after every flag
            with open(path, encoding="utf-8", errors="ignore") as f:
                if f.readline().strip() != "---":
                    return False
                for line in f:
                    if line.strip() == "---":
                        return False
                    if line.startswith(GENERATED_MARK):
                        return True
        except OSError:
            pass
        return False
