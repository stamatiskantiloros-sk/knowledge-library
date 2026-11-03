"""PDF ingestor: Docling by default, Marker or PyMuPDF as alternatives.

Each page starts with a marker holding the page number printed in the book, so notes can cite
pages. Formulas Docling cannot decode are filled from the PDF's text layer at the formula's
position. Pages that contain figures or charts are also rendered as images into assets/, so
charts are not reduced to nothing. Pages without a text layer are flagged and OCR'd (Tesseract
locally, or a vision LLM when pdf.scan_backend: vision_llm). The outline and the table and figure
captions go to assets/outline.json, with printed page numbers, for book digests.
"""
from __future__ import annotations

import json
import re
import subprocess
import shutil
from pathlib import Path

from .. import progress
from ..contract import SourceRecord, SourceWriter, hash_file, make_id, stage
from .base import PDF_EXT, Context, Ingestor, IngestResult, InputItem, first_working, register

FORMULA_GAP = "<!-- formula-not-decoded -->"
PAGE_RE = re.compile(r"^<!-- page (\S+) \(pdf (\d+)\) -->$", re.M)
CAPTION_RE = re.compile(r"^(Table|Figure|Exhibit|Chart)\s+(\d+[.\-]\d+[a-z]?):?\s*(.*)$", re.I)


def page_marker(doc, page_no: int) -> str:
    """`<!-- page 345 (pdf 361) -->`: the number printed on the page, then its place in the file."""
    return f"<!-- page {doc[page_no - 1].get_label() or page_no} (pdf {page_no}) -->"


def _formula_text(pdf, prov) -> str:
    """The text layer under a formula's box: the symbols survive, the layout does not."""
    import fitz
    page = pdf[prov.page_no - 1]
    box = prov.bbox.to_top_left_origin(page.rect.height)
    return page.get_text("text", clip=fitz.Rect(box.l, box.t, box.r, box.b)).strip()


def _docling(path: Path, cfg: dict) -> str:
    import fitz
    from docling.document_converter import DocumentConverter
    from docling_core.types.doc import DocItemLabel
    progress.stage("Docling conversion")
    doc = DocumentConverter().convert(str(path)).document
    pages = []
    with fitz.open(path) as pdf:
        for n, no in enumerate(sorted(doc.pages), 1):
            progress.stage("pages", n, len(doc.pages))
            md = doc.export_to_markdown(page_no=no, compact_tables=True)
            for item, _ in doc.iterate_items(page_no=no):
                if item.label == DocItemLabel.FORMULA and not item.text and FORMULA_GAP in md:
                    prov = next((p for p in item.prov if p.page_no == no), None)
                    text = _formula_text(pdf, prov) if prov else ""
                    # an empty fill keeps the gap, so the flag below still counts it
                    md = md.replace(FORMULA_GAP, f"```formula\n{text}\n```" if text else "\0", 1)
            pages.append(f"{page_marker(pdf, no)}\n\n{md.replace(chr(0), FORMULA_GAP).strip()}")
    return "\n\n".join(pages)


def _marker(path: Path, cfg: dict) -> str:
    from marker.converters.pdf import PdfConverter
    from marker.models import create_model_dict
    from marker.output import text_from_rendered
    text, _, _ = text_from_rendered(PdfConverter(artifact_dict=create_model_dict())(str(path)))
    return text


def _pymupdf(path: Path, cfg: dict) -> str:
    import fitz
    with fitz.open(path) as doc:
        try:
            import pymupdf4llm
            chunks = pymupdf4llm.to_markdown(str(path), page_chunks=True)
            texts = [c["text"] for c in chunks]
        except ImportError:
            texts = [p.get_text("text") for p in doc]
        return "\n\n".join(f"{page_marker(doc, i + 1)}\n\n{t.strip()}" for i, t in enumerate(texts))


RUNNERS = {"docling": _docling, "marker": _marker, "pymupdf": _pymupdf}


def _page_survey(path: Path, min_chars: int) -> list[dict]:
    import fitz
    out = []
    with fitz.open(path) as doc:
        for i, page in enumerate(doc):
            text = page.get_text("text").strip()
            imgs = [im for im in page.get_images(full=True)]
            area = page.rect.width * page.rect.height
            big_imgs = 0
            for im in imgs:
                for r in page.get_image_rects(im[0]):
                    if r.width * r.height > 0.04 * area:
                        big_imgs += 1
            drawings = len(page.get_drawings())
            out.append({"page": i + 1, "chars": len(text), "no_text": len(text) < min_chars,
                        "figure": big_imgs > 0 or drawings > 40})
    return out


def _render(path: Path, page_no: int, dest: Path, dpi: int) -> None:
    import fitz
    with fitz.open(path) as doc:
        doc[page_no - 1].get_pixmap(dpi=dpi).save(dest)


def _ocr_page(png: Path, cfg: dict, ctx: Context) -> tuple[str, str]:
    backend = cfg["pdf"].get("scan_backend", "tesseract")
    if backend == "vision_llm":
        from ..providers import get_vision_provider
        section = cfg["provider"].get("vision") or cfg["frame_selection"]
        prov = get_vision_provider(section, cfg)
        text = prov.generate([png], "Transcribe all text on this page exactly, in reading order, as "
                                    "Markdown. Keep tables as Markdown tables. Do not add commentary.")
        return text, f"{prov.backend}:{prov.model}"
    if backend == "tesseract" and shutil.which("tesseract"):
        langs = cfg["ocr_crosscheck"].get("tesseract_langs", "ell+eng")
        r = subprocess.run(["tesseract", str(png), "stdout", "-l", langs], capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout.strip(), f"tesseract:{langs}"
    return "", "none"


def outline(path: Path) -> dict:
    """The PDF's bookmarks and its table and figure captions, each with its printed page.

    A caption is a line that starts "Table 3.1" and has no running text after the number, or only
    a title in capitals; "Table 3.7 summarizes ..." and "as shown in / Table 3.7." are mentions.
    A number that is only ever mentioned (its caption is inside an image) is kept at its first
    mention, without a title.
    """
    import fitz
    toc, captions, mentions = [], {}, {}
    with fitz.open(path) as doc:
        for level, title, page in doc.get_toc():
            if page >= 1:
                toc.append({"level": level, "title": title.replace("\x00", "").strip(),
                            "page": doc[page - 1].get_label() or str(page), "pdf": page})
        for i, page in enumerate(doc):
            lines = [l.strip() for l in page.get_text("text").splitlines()]
            for j, line in enumerate(lines):
                m = CAPTION_RE.match(line)
                if not m:
                    continue
                kind, num, rest = m.group(1).title(), m.group(2), m.group(3).strip()
                entry = {"kind": kind, "number": num, "title": "", "page": page.get_label() or str(i + 1), "pdf": i + 1}
                letters = re.sub(r"[^A-Za-z]", "", rest)
                if rest and not (letters and sum(c.isupper() for c in letters) / len(letters) > 0.6):
                    mentions.setdefault(f"{kind} {num}", entry)
                    continue
                entry["title"] = rest or next((l for l in lines[j + 1:j + 3] if l and not CAPTION_RE.match(l)), "")
                captions.setdefault(f"{kind} {num}", entry)
    for key, entry in mentions.items():
        captions.setdefault(key, {**entry, "title": "(caption not in the text layer; page of first mention)"})
    order = lambda e: (e["pdf"], e["kind"], [int(x) for x in re.findall(r"\d+", e["number"])])
    return {"toc": toc, "captions": sorted(captions.values(), key=order)}


def pdf_title(path: Path, markdown: str) -> str:
    try:
        import fitz
        with fitz.open(path) as doc:
            t = (doc.metadata or {}).get("title", "").strip()
            if t and len(t) > 3 and not t.lower().startswith(("microsoft", "untitled")):
                return t
    except Exception:
        pass
    m = re.search(r"^#\s+(.+)$", markdown, re.M)
    return m.group(1).strip() if m else path.stem.replace("_", " ")


def ingest_pdf_file(path: Path, ctx: Context, *, origin: list[str], content_hash: str,
                    source_type: str = "pdf", extra_pipeline: dict | None = None,
                    title: str | None = None) -> IngestResult:
    """Shared by the PDF ingestor and the Office ingestor (slides rendered via PDF)."""
    cfg = ctx.config
    pc = cfg["pdf"]
    backends = [pc["backend"], *[b for b in pc.get("fallbacks", []) if b != pc["backend"]]]
    used, markdown, errors = first_working(backends, RUNNERS, path, cfg)
    flags = [f"backend fallback: {e}" for e in errors]
    title = title or pdf_title(path, markdown)
    sid = make_id(title, content_hash)
    w = SourceWriter(ctx.sources_dir, sid)
    try:
        survey = _page_survey(path, pc.get("min_text_chars", 25))
        scanned = [pg for pg in survey if pg["no_text"]]
        text_layer_chars = sum(pg["chars"] for pg in survey)
        extracted_chars = len(re.sub(r"[^\w]", "", markdown))
        # Docling (and recent PyMuPDF) OCR scanned pages themselves; only run our own OCR when
        # the backend's output is clearly missing the scanned pages' text
        backend_ocrd = bool(scanned) and extracted_chars - text_layer_chars > 40 * len(scanned)
        renders, ocr_sections, ocr_tool = [], [], None
        for pg in survey:
            progress.stage("page renders and OCR", pg["page"], len(survey))
            needs_render = (pc.get("render_figure_pages", True) and pg["figure"]) or pg["no_text"]
            if not needs_render:
                continue
            dest = w.asset_path(f"page_{pg['page']:03d}.png")
            _render(path, pg["page"], dest, pc.get("render_dpi", 200))
            renders.append((pg, dest.name))
            if not pg["no_text"]:
                continue
            if backend_ocrd:
                flags.append(f"page {pg['page']}: no text layer, text from {used}'s OCR - check against the page image")
                continue
            text, ocr_tool = _ocr_page(dest, cfg, ctx)
            if text:
                ocr_sections.append(f"### Page {pg['page']} (OCR)\n\n![](assets/{dest.name})\n\n{text}")
                flags.append(f"page {pg['page']}: no text layer, OCR'd with {ocr_tool}")
            else:
                flags.append(f"page {pg['page']}: no text layer and no OCR available")
        body = [f"# {title}", markdown.strip()]
        if ocr_sections:
            body += ["## Scanned pages (OCR)", *ocr_sections]
        if backend_ocrd:
            body.append("## Scanned page images\n\n" + "\n".join(
                f"- Page {pg['page']}: ![](assets/{n})" for pg, n in renders if pg["no_text"]))
        gaps = markdown.count(FORMULA_GAP)
        filled = markdown.count("```formula\n")
        if filled:
            flags.append(f"{filled} formulas read from the PDF text layer: the symbols are kept, the "
                         "layout is not, so check fractions and sub- and superscripts against the page")
        if gaps:
            flags.append(f"{gaps} formulas missing: neither {used} nor the text layer had them")
        book = outline(path)
        if book["toc"] or book["captions"]:
            w.asset_path("outline.json").write_text(json.dumps(book, ensure_ascii=False, indent=1), encoding="utf-8")
        figs = [(pg, n) for pg, n in renders if pg["figure"] and not pg["no_text"]]
        if figs:
            body.append("## Page renders (figures and charts)\n\n"
                        "Pages with figures, charts or diagrams, rendered so they can be inspected directly.")
            body += [f"- Page {pg['page']}: ![](assets/{n})" for pg, n in figs]
        pipeline = {"extraction": stage(used, package={"pymupdf": "pymupdf", "docling": "docling",
                                                        "marker": "marker-pdf"}[used])}
        if renders:
            pipeline["page_render"] = stage("pymupdf", dpi=pc.get("render_dpi", 200))
        if ocr_tool and ocr_tool != "none":
            pipeline["scan_ocr"] = {"tool": ocr_tool}
        pipeline.update(extra_pipeline or {})
        rec = SourceRecord(id=sid, title=title, source_type=source_type, origin=origin,
                           hash=content_hash, pipeline=pipeline, flags=flags,
                           stats={"pages": len(survey), "page_renders": len(renders)})
        md = w.commit(rec, "\n\n".join(body))
    except Exception:
        w.abort()
        raise
    return IngestResult(sid, md, pipeline, flags)


@register
class PDFIngestor(Ingestor):
    name = "pdf"

    def can_handle(self, item: InputItem) -> bool:
        return item.kind == "file" and item.suffix in PDF_EXT

    def ingest(self, item: InputItem, ctx: Context) -> IngestResult:
        return ingest_pdf_file(item.path, ctx, origin=[f"inbox/{item.key}"],
                               content_hash=item.content_hash or hash_file(item.path),
                               title=item.sidecar.get("title"))
