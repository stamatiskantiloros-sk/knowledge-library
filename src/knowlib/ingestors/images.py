"""Loose images: screenshots, phone photos of handouts, scanned pages saved as pictures.

All the images directly in one folder become one source ("<folder> (images)"), in file-name order,
which for screenshots and phone photos is the order they were taken. Each image gets a section
"## Image N: <file name>" with a copy of the image and its text, so a note cites it as
[@id, image N].

The text comes from the vision model (images.model, default the frame_selection model), which also
says what a chart shows and which answer a quiz screenshot marks. Classic OCR (ocr_crosscheck)
reads every image as well; where the two disagree, above all on numbers, the image is flagged so
it is checked by eye. With images.describe: false only classic OCR runs: fast, but tables lose
their layout and nothing says which answer is marked.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

from .. import progress
from ..contract import SourceRecord, hash_file, make_id, stage
from ..media import ocr
from ..media.frames import resized_copy
from .base import IMAGE_EXT, Context, Ingestor, IngestResult, InputItem, register

log = logging.getLogger("knowlib")

READ_PROMPT = """This image is a screenshot or photo of study material (a page, a slide, a quiz question, a table).
Transcribe ALL text exactly as written, in its original language and reading order, as Markdown.
Keep tables as Markdown tables and every number exactly as shown.
If it is a quiz question, write each option on its own line starting "- ", and end the line with what
the image shows beside that option, in square brackets, only as shown: [selected] if its box or
circle is ticked or filled, [green check] or [red X] if such a mark is next to it, for example
"- balance sheet [selected] [green check]". Do not explain what the marks mean.
If it has a chart or diagram, add a last line starting "Figure:" saying what it shows.
Do not add commentary or answer the question yourself."""


def for_ocr(text: str) -> str:
    """The model's reply without what OCR cannot see: [marks], "Figure:" lines, list numbering."""
    lines = [ln for ln in text.splitlines() if not re.match(r"\s*\**(Figure|Marks?|Selected)\b", ln, re.I)]
    text = "\n".join(re.sub(r"^\s*(?:\d+[.)]|[-*•]|☐|☑|☒)\s+", "", ln) for ln in lines)
    return re.sub(r"\[(?:selected|green check|red x|not selected)\]", "", text, flags=re.I)


def title_for(item: InputItem) -> str:
    name = item.folder.name if item.folder else "images"
    return item.sidecar.get("title") or f"{name} (images)"


def read_image(path: Path, view: Path, provider, cache: dict, cfg: dict) -> tuple[str, str, list[str]]:
    """(text, who read it, flags) for one image."""
    oc = cfg["ocr_crosscheck"]
    flags: list[str] = []
    if provider is None:
        texts = [t for t in (_ocr(n, view, oc) for n in oc.get("backends", [])) if t]
        return (texts[0] if texts else ""), ",".join(oc.get("backends", [])) or "none", flags
    prompt_v = hashlib.sha256(READ_PROMPT.encode()).hexdigest()[:8]
    key = f"{hash_file(path)}:{provider.backend}:{provider.model}:{prompt_v}"
    if key not in cache:
        try:
            cache[key] = provider.generate([view], READ_PROMPT).strip()
        except (OSError, RuntimeError) as e:          # the model is gone or would not load: OCR instead
            log.warning("vision model failed on %s (%s); using OCR", path.name, e)
            texts = [t for t in (_ocr(n, view, oc) for n in oc.get("backends", [])) if t]
            flags.append("the vision model failed; text from classic OCR only: check tables and marked answers")
            return (texts[0] if texts else ""), "ocr", flags
    text = cache[key]
    check = ocr.crosscheck(view, for_ocr(text), cfg)
    if check["flag"]:
        detail = "; ".join(f"{n}: numbers only the model saw {r['numbers_only_vlm']}, only OCR saw "
                           f"{r['numbers_only_ocr']}" for n, r in check["backends"].items() if r["flag"])
        flags.append(f"the model's text and OCR disagree, check the image ({detail})")
    return text, f"{provider.backend}:{provider.model}", flags


def _ocr(name: str, path: Path, oc: dict) -> str:
    try:
        return ocr.BACKENDS[name](path, oc)
    except Exception as e:  # noqa: BLE001 - a missing OCR backend must not stop ingestion
        log.warning("%s OCR failed on %s: %s", name, path.name, e)
        return ""


@register
class ImageIngestor(Ingestor):
    name = "images"

    def can_handle(self, item: InputItem) -> bool:
        return item.kind == "images"

    def ingest(self, item: InputItem, ctx: Context) -> IngestResult:
        cfg = ctx.config
        ic = cfg["images"]
        title = title_for(item)
        content_hash = item.content_hash or hashlib.sha256(
            "".join(p.name + hash_file(p) for p in item.paths).encode()).hexdigest()
        sid = make_id(title, content_hash)
        provider = None
        if ic.get("describe", True):
            from ..providers import get_vision_provider
            provider = get_vision_provider(ic.get("model") or cfg["frame_selection"], cfg)
        cache_path = ctx.work_for("images") / "read_cache.json"
        cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
        w = ctx.writer(sid)
        try:
            body, flags, readers = [f"# {title}"], [], set()
            for n, path in enumerate(item.paths, 1):
                progress.stage("images", n, len(item.paths))
                ext = ".jpg" if path.suffix.lower() in (".jpg", ".jpeg", ".heic") else ".png"
                view = resized_copy(path, w.asset_path(f"image_{n:03d}{ext}"), int(ic.get("max_px", 1568)))
                text, who, img_flags = read_image(path, view, provider, cache, cfg)
                cache_path.write_text(json.dumps(cache, ensure_ascii=False))
                readers.add(who)
                flags += [f"image {n} ({path.name}): {f}" for f in img_flags]
                if not text:
                    flags.append(f"image {n} ({path.name}): no text found: read the image")
                body.append(f"## Image {n}: {path.name}\n\n![](assets/{view.name})\n\n{text or '(no text found)'}")
            pipeline = {"image_read": stage(sorted(readers)[0] if len(readers) == 1 else ", ".join(sorted(readers)),
                                            package="knowlib"),
                        "ocr_crosscheck": {"backends": cfg["ocr_crosscheck"].get("backends", [])}}
            rec = SourceRecord(id=sid, title=title, source_type="images",
                               origin=[f"inbox/{item.key}"] + [p.name for p in item.paths],
                               hash=content_hash, pipeline=pipeline, flags=flags,
                               stats={"images": len(item.paths)})
            md = w.commit(rec, "\n\n".join(body))
        except Exception:
            w.abort()
            raise
        finally:
            if provider is not None and hasattr(provider, "close"):
                provider.close()
        return IngestResult(sid, md, pipeline, flags)
