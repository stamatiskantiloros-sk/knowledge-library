"""Plain-text and code ingestor: .md .txt .py .ipynb .r .sql .json .yaml .csv .tsv

Course code, notebooks and datasets become sources too, so the synthesis model can cite the
exact code a lecture used. Notebooks keep code and markdown cells (outputs dropped); CSVs are
summarized (shape, columns, first rows) rather than copied whole.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from ..contract import SourceRecord, SourceWriter, hash_file, make_id, stage
from .base import Context, Ingestor, IngestResult, InputItem, register

CODE_LANG = {".py": "python", ".r": "r", ".sql": "sql", ".json": "json", ".yaml": "yaml",
             ".yml": "yaml", ".sh": "bash", ".js": "javascript", ".m": "matlab"}
TEXT_EXT = {".md", ".txt"}
TABLE_EXT = {".csv", ".tsv"}
NOTEBOOK_EXT = {".ipynb"}
ALL_EXT = set(CODE_LANG) | TEXT_EXT | TABLE_EXT | NOTEBOOK_EXT
MAX_CHARS = 200_000


def _read(path: Path) -> str:
    raw = path.read_bytes()[:MAX_CHARS * 2]
    for enc in ("utf-8-sig", "cp1253", "latin-1"):      # cp1253 = Windows Greek
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _notebook(path: Path) -> str:
    nb = json.loads(path.read_text(encoding="utf-8"))
    lang = (nb.get("metadata", {}).get("kernelspec", {}) or {}).get("language", "python")
    out = []
    for cell in nb.get("cells", []):
        src = "".join(cell.get("source", []))
        if not src.strip():
            continue
        out.append(src if cell.get("cell_type") == "markdown" else f"```{lang}\n{src}\n```")
    return "\n\n".join(out)


def _table(path: Path, rows: int = 15) -> tuple[str, dict]:
    text = _read(path)
    delim = "\t" if path.suffix.lower() == ".tsv" else (csv.Sniffer().sniff(text[:5000], ",;\t|").delimiter
                                                       if text.strip() else ",")
    reader = list(csv.reader(text.splitlines(), delimiter=delim))
    if not reader:
        return "(empty file)", {"rows": 0}
    header, body = reader[0], reader[1:]
    md = ["| " + " | ".join(h.strip() or f"col{i + 1}" for i, h in enumerate(header)) + " |",
          "|" + " --- |" * len(header)]
    md += ["| " + " | ".join(c.strip()[:60] for c in r) + " |" for r in body[:rows]]
    info = {"rows": len(body), "columns": len(header)}
    summary = (f"Table with {len(body)} rows and {len(header)} columns: "
               f"{', '.join(h.strip() for h in header)[:1500]}.\n\nFirst {min(rows, len(body))} rows:\n\n")
    return summary + "\n".join(md), info


@register
class TextIngestor(Ingestor):
    name = "text"

    def can_handle(self, item: InputItem) -> bool:
        return item.kind == "file" and item.suffix in ALL_EXT

    def ingest(self, item: InputItem, ctx: Context) -> IngestResult:
        path, ext = item.path, item.suffix
        h = item.content_hash or hash_file(path)
        title = item.sidecar.get("title") or path.stem.replace("_", " ")
        flags, stats = [], {}
        if ext in NOTEBOOK_EXT:
            body = _notebook(path)
        elif ext in TABLE_EXT:
            body, stats = _table(path)
        elif ext in TEXT_EXT:
            body = _read(path)
        else:
            body = f"```{CODE_LANG[ext]}\n{_read(path).rstrip()}\n```"
        if len(body) > MAX_CHARS:
            body = body[:MAX_CHARS]
            flags.append(f"truncated to {MAX_CHARS} characters")
        if not body.strip():
            flags.append("empty file")
        sid = make_id(title, h)
        pipeline = {"extraction": stage("knowlib-text", kind=ext.lstrip("."))}
        w = SourceWriter(ctx.sources_dir, sid)
        try:
            rec = SourceRecord(id=sid, title=title, source_type="text", origin=[f"inbox/{item.key}"],
                               hash=h, pipeline=pipeline, flags=flags, stats=stats)
            md = w.commit(rec, f"# {title}\n\n*File: {path.name}*\n\n{body}")
        except Exception:
            w.abort()
            raise
        return IngestResult(sid, md, pipeline, flags)
