"""Office ingestor (.docx, .pptx, .xlsx, .odt, .rtf ...).

Documents: Docling by default, Pandoc as fallback (Pandoc also extracts embedded images).
Presentations: written slide by slide - slide image (via LibreOffice -> PDF -> PNG, when
LibreOffice is installed), slide text and speaker notes - the same shape as video slides.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..contract import SourceRecord, SourceWriter, hash_file, make_id, stage
from .base import OFFICE_EXT, Context, Ingestor, IngestResult, InputItem, first_working, register


_RUN = re.compile(r"<w:r\b(?:(?!</w:r>).)*?</w:r>", re.S)
_TEXT = re.compile(r"(<w:t\b[^>]*>)([^<]*)(</w:t>)")


def _mark_highlights(xml: str) -> tuple[str, int]:
    """Wrap the text of marked runs in ==...== (Markdown's highlight), which Docling keeps as text.
    A run is marked when it is highlighted, or both bold and underlined: authors use either to carry
    meaning a converter drops, such as the correct option in an answer key. Adjacent marked runs merge
    into one span."""
    count = 0

    def run(m: re.Match) -> str:
        nonlocal count
        r = m.group(0)
        props = re.search(r"<w:rPr>.*?</w:rPr>", r, re.S)
        p = props.group(0) if props else ""
        hl = props and (re.search(r'<w:highlight w:val="(?!none")', p)
                        or (re.search(r'<w:b(?: w:val="(?:1|true|on)")?/>', p)
                            and re.search(r'<w:u w:val="(?!none")', p)))
        if not hl or not "".join(t[1] for t in _TEXT.findall(r)).strip():
            return r
        count += 1
        texts = list(_TEXT.finditer(r))
        first, last = texts[0], texts[-1]
        r = r[:last.start(2)] + last.group(2) + "==" + r[last.end(2):]
        return r[:first.start(2)] + "==" + r[first.start(2):]

    out = _RUN.sub(run, xml)
    # merge spans split across runs: "==C==" "==.==" -> "==C.=="
    out = re.sub(r"==(</w:t>(?:(?!<w:t\b).)*?<w:t\b[^>]*>)==", r"\1", out, flags=re.S)
    return out, count


def _with_marked_highlights(path: Path, tmp: Path) -> Path:
    """A copy of a .docx with highlighted text marked, or the file itself when nothing is highlighted."""
    import zipfile
    if path.suffix.lower() != ".docx":
        return path
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8")
        marked, n = _mark_highlights(xml)
        if not n:
            return path
        copy = tmp / path.name
        with zipfile.ZipFile(copy, "w", zipfile.ZIP_DEFLATED) as out:
            for info in z.infolist():
                out.writestr(info, marked.encode("utf-8") if info.filename == "word/document.xml"
                             else z.read(info.filename))
    return copy


def _docling(path: Path, w: SourceWriter) -> str:
    from docling.document_converter import DocumentConverter
    with tempfile.TemporaryDirectory() as tmp:
        src = _with_marked_highlights(path, Path(tmp))
        md = DocumentConverter().convert(str(src)).document.export_to_markdown()
    md = md.replace(r"\=\=", "==")
    return re.sub(r"==(\S[^=\n]*?)(\s+)==", r"==\1==\2", md)   # "==C. ==" -> "==C.== "


def _pandoc(path: Path, w: SourceWriter) -> str:
    if not shutil.which("pandoc"):
        raise RuntimeError("pandoc not installed")
    media = w.assets / "media"
    r = subprocess.run(["pandoc", str(path), "-t", "gfm", "--wrap=none", f"--extract-media={media}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:500])
    # make image links relative to source.md
    return r.stdout.replace(str(w.assets), "assets")


def _python_pptx(path: Path, w: SourceWriter) -> str:
    return "\n\n".join(_pptx_sections(path, {}))


RUNNERS = {"docling": _docling, "pandoc": _pandoc, "python-pptx": _python_pptx}


def _pptx_slides(path: Path) -> list[dict]:
    from pptx import Presentation
    slides = []
    for i, s in enumerate(Presentation(str(path)).slides, 1):
        title, lines = None, []
        for shp in s.shapes:
            if shp.has_text_frame:
                txt = "\n".join(p.text for p in shp.text_frame.paragraphs if p.text.strip())
                if not txt:
                    continue
                if title is None and getattr(shp, "is_placeholder", False) and \
                        "TITLE" in str(shp.placeholder_format.type):
                    title = txt.replace("\n", " ")
                else:
                    lines.append(txt)
            if getattr(shp, "has_table", False) and shp.has_table:
                rows = [[c.text.strip() for c in r.cells] for r in shp.table.rows]
                if rows:
                    lines.append("| " + " | ".join(rows[0]) + " |\n|" + " --- |" * len(rows[0]) + "\n" +
                                 "\n".join("| " + " | ".join(r) + " |" for r in rows[1:]))
        notes = ""
        if s.has_notes_slide:
            notes = s.notes_slide.notes_text_frame.text.strip()
        slides.append({"n": i, "title": title, "text": "\n\n".join(lines), "notes": notes})
    return slides


def _pptx_sections(path: Path, images: dict[int, str]) -> list[str]:
    out = []
    for s in _pptx_slides(path):
        head = f"## Slide {s['n']}" + (f": {s['title']}" if s["title"] else "")
        parts = [head]
        if s["n"] in images:
            parts.append(f"![](assets/{images[s['n']]})")
        if s["text"]:
            parts.append(f"**On slide:**\n\n{s['text']}")
        if s["notes"]:
            parts.append(f"**Speaker notes:** {s['notes']}")
        out.append("\n\n".join(parts))
    return out


def _render_slides(path: Path, w: SourceWriter, dpi: int = 150) -> dict[int, str]:
    soffice = shutil.which("soffice") or shutil.which("libreoffice") or \
        ("/Applications/LibreOffice.app/Contents/MacOS/soffice"
         if Path("/Applications/LibreOffice.app/Contents/MacOS/soffice").exists() else None)
    if not soffice:
        return {}
    import fitz
    with tempfile.TemporaryDirectory() as td:
        subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", td, str(path)],
                       capture_output=True, timeout=600)
        pdfs = list(Path(td).glob("*.pdf"))
        if not pdfs:
            return {}
        out = {}
        with fitz.open(pdfs[0]) as doc:
            for i, page in enumerate(doc, 1):
                name = f"slide_{i:03d}.png"
                page.get_pixmap(dpi=dpi).save(w.asset_path(name))
                out[i] = name
        return out


def _office_title(path: Path) -> str | None:
    try:
        if path.suffix.lower() == ".pptx":
            from pptx import Presentation
            prs = Presentation(str(path))
            t = (prs.core_properties.title or "").strip()
            if t:
                return t
            first = prs.slides[0] if len(prs.slides) else None
            if first is not None and first.shapes.title is not None and first.shapes.title.text.strip():
                return first.shapes.title.text.strip()
        elif path.suffix.lower() == ".docx":
            import zipfile
            core = zipfile.ZipFile(path).read("docProps/core.xml").decode("utf-8", "ignore")
            m = re.search(r"<dc:title>([^<]+)</dc:title>", core)
            if m and m.group(1).strip():
                return m.group(1).strip()
    except Exception:  # noqa: BLE001 - title is cosmetic; fall back to the file name
        return None
    return None


@register
class OfficeIngestor(Ingestor):
    name = "office"

    def can_handle(self, item: InputItem) -> bool:
        return item.kind == "file" and item.suffix in OFFICE_EXT

    def ingest(self, item: InputItem, ctx: Context) -> IngestResult:
        oc = ctx.config["office"]
        path = item.path
        h = item.content_hash or hash_file(path)
        title = _office_title(path) or path.stem.replace("_", " ")
        sid = make_id(title, h)
        w = ctx.writer(sid)
        flags: list[str] = []
        pipeline: dict = {}
        try:
            if item.suffix in (".pptx", ".ppt", ".odp"):
                images = _render_slides(path, w) if oc.get("render_slides", True) else {}
                if images:
                    pipeline["slide_render"] = {"tool": "libreoffice+pymupdf"}
                elif oc.get("render_slides", True):
                    flags.append("slide images not rendered (LibreOffice not installed)")
                try:
                    sections = _pptx_sections(path, images)
                    pipeline["extraction"] = stage("python-pptx")
                    body = "\n\n".join([f"# {title}", *sections])
                except ImportError:
                    used, md, errs = first_working(["docling"], RUNNERS, path, w)
                    pipeline["extraction"] = stage(used)
                    body = f"# {title}\n\n{md}"
                    if images:
                        body += "\n\n## Slide images\n\n" + "\n".join(
                            f"- Slide {n}: ![](assets/{f})" for n, f in sorted(images.items()))
            else:
                backends = [oc["backend"], *[b for b in oc.get("fallbacks", []) if b != oc["backend"]
                                             and b != "python-pptx"]]
                used, md, errs = first_working(backends, RUNNERS, path, w)
                flags += [f"backend fallback: {e}" for e in errs]
                pipeline["extraction"] = stage(used)
                m = re.search(r"^#\s+(.+)$", md, re.M)
                body = md if m else f"# {title}\n\n{md}"
            rec = SourceRecord(id=sid, title=title, source_type="office", origin=[f"inbox/{item.key}"],
                               hash=h, pipeline=pipeline, flags=flags)
            md_path = w.commit(rec, body)
        except Exception:
            w.abort()
            raise
        return IngestResult(sid, md_path, pipeline, flags)
