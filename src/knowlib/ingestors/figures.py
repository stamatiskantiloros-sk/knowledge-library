"""Figures inside PDFs: the charts, diagrams and tables-as-pictures that a text extractor skips.

Docling marks each picture with its box; _docling writes `<!-- figure P-K l,t,r,b -->` where the
picture was (P the PDF page, K its order on the page, the box in points from the top left).
read_figures() then, for every marker:

1. crops the box from the page at pdf.figures.dpi;
2. skips it, without a model, when it is small (an icon or bullet), repeated on several pages
   (a logo, a running header) or a photo with no text in it (a chapter opener);
3. has the vision model read the rest: all text and numbers exactly, what a chart or diagram
   shows; the model may answer DECORATIVE for what slipped through;
4. checks the model's numbers against classic OCR, as for slides, and flags disagreements;
5. writes the text where the marker was, on its page, so a note cites it as p. N.

Replies are cached by the crop's content and the model, so a book is read once.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import defaultdict
from pathlib import Path

from .. import progress
from ..media import ocr
from .images import for_ocr

CUT_OFF = "[CUT OFF: the reading stopped at the token limit]"
DECORATIVE_RE = re.compile(r"^\W*DECORATIVE\W*$", re.M)

log = logging.getLogger("knowlib")

FIG_RE = re.compile(r"<!-- figure (\d+)-(\d+) (-?[\d.]+),(-?[\d.]+),(-?[\d.]+),(-?[\d.]+) -->")

FIGURE_PROMPT = """This image is a figure cut from a page of a textbook or lecture document: a chart, diagram, table,
journal entry, T-account, form, worked example or illustration.
Transcribe every piece of text and every number in it exactly as printed, in reading order. Write a table,
journal entry or T-account as a Markdown table, with the account names and labels in their cells,
debits and credits in separate columns, and without the dotted leaders between a name and its amount.
Do not use headings or code blocks, and do not describe colours, arrows or positions.
Never estimate a value that is not printed, such as the length of a bar; give the axis range instead.
End with one line starting "Figure:" saying what it shows: for a chart, its axes, its series and the
printed values or the trend; for a diagram, what is connected to what.
If it carries nothing to learn (a photo, a logo, decoration), even with a little text such as a shop
sign, reply with the single word DECORATIVE and nothing else. A box of text, a list, a table or a
chart is never DECORATIVE."""


def for_check(text: str) -> str:
    """A figure reading without what OCR cannot see: marks, the Figure: line, headings, code fences,
    and the dotted leaders between an account and its amount."""
    text = "\n".join(ln for ln in for_ocr(text).splitlines() if not re.match(r"\s*(#|```)", ln))
    return re.sub(r"(?:[.…·•] ?){2,}|…", " ", text)                   # also ". . . ." spaced out


def figure_section(cfg: dict) -> dict:
    """pdf.figures.model, then images.model, then the strong local model (frame_selection's caption_model)."""
    fs = cfg["frame_selection"]
    return (cfg["pdf"].get("figures") or {}).get("model") or cfg["images"].get("model") or \
        {**fs, **(fs.get("caption_model") or {})}


def marker(page: int, k: int, box) -> str:
    return f"<!-- figure {page}-{k} {box[0]:.1f},{box[1]:.1f},{box[2]:.1f},{box[3]:.1f} -->"


def _ahash(img) -> str:
    small = img.convert("L").resize((12, 12))
    px = list(small.getdata())
    avg = sum(px) / len(px)
    return "".join("1" if p > avg else "0" for p in px)


def _flat(img) -> bool:
    """True for a drawn figure (few colours cover most of it), False for a photo."""
    q = img.convert("RGB").resize((96, 96)).quantize(colors=64)
    counts = sorted((c for c, _ in q.getcolors(64) or []), reverse=True)
    return sum(counts[:8]) / max(1, sum(counts)) > 0.7


def read_figures(path: Path, markdown: str, writer, cfg: dict, work: Path) -> tuple[str, list[str], dict]:
    """(markdown with figures read, flags, stats)."""
    fc = cfg["pdf"].get("figures") or {}
    found = list(FIG_RE.finditer(markdown))
    stats = {"figures": len(found), "read": 0, "skipped": defaultdict(int), "flagged": 0}
    if not found or not fc.get("read", True):
        return FIG_RE.sub("<!-- image -->", markdown), [], {"figures": len(found)}
    import fitz
    from PIL import Image
    dpi = int(fc.get("dpi", 200))
    min_side = float(fc.get("min_side_pt", 60))
    min_area = float(fc.get("min_page_share", 0.02))
    crops: dict[str, tuple[Path | None, str]] = {}          # marker text -> (crop, skip reason)
    hashes: dict[str, set[int]] = defaultdict(set)
    with fitz.open(path) as doc:
        for n, m in enumerate(found, 1):
            progress.stage("cropping figures", n, len(found))
            page_no, k = int(m[1]), int(m[2])
            l, t, r, b = (float(x) for x in m.groups()[2:])
            page = doc[page_no - 1]
            rect = fitz.Rect(l, t, r, b) & page.rect
            if rect.is_empty or min(rect.width, rect.height) < min_side or \
                    rect.width * rect.height < min_area * page.rect.width * page.rect.height:
                crops[m[0]] = (None, "small")
                continue
            dest = writer.asset_path(f"figures/fig_{page_no:04d}_{k}.png")
            page.get_pixmap(dpi=dpi, clip=rect).save(dest)
            crops[m[0]] = (dest, "")
            with Image.open(dest) as img:
                hashes[_ahash(img)].add(page_no)
    repeated = {h for h, pages in hashes.items() if len(pages) >= int(fc.get("repeat_pages", 3))}
    oc = cfg["ocr_crosscheck"]
    first_ocr = (oc.get("backends") or [None])[0]
    todo = []
    for text, (crop, why) in crops.items():
        if crop is None:
            continue
        with Image.open(crop) as img:
            if _ahash(img) in repeated:
                crops[text] = (None, "repeated")
                continue
            flat = _flat(img)
        if not flat and first_ocr:
            try:
                words = len(re.findall(r"\w{2,}", ocr.BACKENDS[first_ocr](crop, oc)))
            except Exception:  # noqa: BLE001 - no OCR: let the model decide
                words = 99
            if words < 3:
                crops[text] = (None, "photo without text")
                continue
        todo.append(text)
    for crop, why in crops.values():
        if crop is None:
            stats["skipped"][why] += 1
    readings, flags = _read(todo, crops, cfg, work, stats)
    out = markdown
    for m in found:
        crop, why = crops[m[0]]
        page_no = int(m[1])
        if m[0] in readings:
            rel = crop.relative_to(writer.assets).as_posix()
            body = readings[m[0]]
            out = out.replace(m[0], f"![figure](assets/{rel})\n\n> **Figure, read by the vision model:**\n>\n"
                                    + "\n".join(f"> {ln}" if ln else ">" for ln in body.splitlines()), 1)
        elif why == "not read":                                # kept, so it can be read by eye
            rel = crop.relative_to(writer.assets).as_posix()
            out = out.replace(m[0], f"![figure](assets/{rel})\n\n<!-- image: not read, see the flags -->", 1)
        else:
            out = out.replace(m[0], f"<!-- image: {why or 'decorative'} -->", 1)
    kept = {crops[t][0].name for t in readings} | {c.name for c, why in crops.values() if why == "not read"}
    for f in (writer.assets / "figures").glob("*.png") if (writer.assets / "figures").exists() else []:
        if f.name not in kept:
            f.unlink()                                         # keep only figures worth reading
    stats["skipped"] = dict(stats["skipped"])
    return out, flags, stats


class DetailCapped(RuntimeError):
    """The model server shows a large figure at the detail of a small one."""


def _detail_check(provider, crops: list, work: Path, mode: str) -> str | None:
    """Ask the server how many tokens the largest crop gets, and a copy at half its width and height.
    A server that keeps the detail gives the full crop about 4 times as many; under 2 times, it caps
    the image tokens and dense figures come back with wrong digits. Returns a flag, or raises."""
    if mode == "off" or not crops or not hasattr(provider, "image_tokens"):
        return None
    from PIL import Image
    big = max(crops, key=lambda c: Image.open(c).size[0] * Image.open(c).size[1])
    half = work / "detail_check_half.png"
    with Image.open(big) as img:
        if min(img.size) < 400:
            return None                                        # all small: nothing to lose
        img.resize((img.width // 2, img.height // 2), Image.LANCZOS).save(half)
    try:
        full, quarter = provider.image_tokens(big), provider.image_tokens(half)
    except (OSError, ValueError, KeyError) as e:                # the reading itself will say what is wrong
        log.warning("image detail check skipped (%s)", e)
        return None
    finally:
        half.unlink(missing_ok=True)
    if not full or not quarter or full >= 2 * quarter:
        return None
    msg = (f"the model server gives a {Image.open(big).size[0]}x{Image.open(big).size[1]} figure {full} image "
           f"tokens and a quarter of it {quarter}: it caps image tokens, so dense figures lose digits. "
           "Raise the server's image-token limit (llama.cpp: --image-max-tokens, about 2048), "
           "or set pdf.figures.detail_check: warn to read them anyway")
    if mode == "stop":
        raise DetailCapped(msg)
    log.warning(msg)
    return "figures: " + msg


def _read(todo: list[str], crops: dict, cfg: dict, work: Path, stats: dict) -> tuple[dict, list[str]]:
    if not todo:
        return {}, []
    from ..providers import get_vision_provider
    provider = get_vision_provider(figure_section(cfg), cfg)
    tokens = int((cfg["pdf"].get("figures") or {}).get("max_tokens", 8000))
    retries = int((cfg["pdf"].get("figures") or {}).get("retries", 2))
    stats["model"] = f"{provider.backend}:{provider.model}"
    cache_path = work / "figures_cache.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    prompt_v = hashlib.sha256(FIGURE_PROMPT.encode()).hexdigest()[:8]
    backend = "openai" if provider.backend == "chain" else provider.backend   # a chain's members are servers
    models = getattr(provider, "cache_models", None) or [provider.model]
    digests = {t: hashlib.sha256(crops[t][0].read_bytes()).hexdigest() for t in todo}

    def key_for(t: str, model) -> str:
        return f"{digests[t]}:{backend}:{model}:{prompt_v}"

    keys = {t: next((k for k in (key_for(t, m) for m in models) if k in cache and not cache[k].endswith(CUT_OFF)),
                    key_for(t, models[0])) for t in todo}
    to_ask = [crops[t][0] for t in todo if keys[t] not in cache or cache[keys[t]].endswith(CUT_OFF)]
    readings, flags = {}, []
    try:
        detail = _detail_check(provider, to_ask, work, (cfg["pdf"].get("figures") or {}).get("detail_check", "stop"))
        flags += [detail] if detail else []
        for n, text in enumerate(todo, 1):
            progress.stage("reading figures", n, len(todo))
            crop = crops[text][0]
            key = keys[text]
            if key not in cache or cache[key].endswith(CUT_OFF):
                page = text.split()[2].split("-")[0]
                try:
                    for n in range(1 + retries):   # a thinking loop is random and a big table needs room:
                        reply = provider.generate([crop], FIGURE_PROMPT,   # ask again with twice the tokens
                                                  max_tokens=tokens * 2 ** n).strip()
                        if reply and getattr(provider, "last_finish", None) != "length":
                            break
                except (OSError, RuntimeError) as e:
                    if type(e).__name__ == "NotEnoughMemory":
                        raise
                    log.warning("figure %s not read (%s)", crop.name, e)
                    flags.append(f"figure on pdf page {page} not read: {e}")
                    crops[text] = (crop, "not read")              # never taken as decorative
                    stats["skipped"]["not read"] += 1
                    continue
                if not reply:          # not cached, so asked again next run
                    why = (f"it stopped at the token limit or in a loop before answering, {1 + retries} times; "
                           "raise pdf.figures.max_tokens or retries"
                           if getattr(provider, "last_finish", None) == "length" else "the model's reply was empty")
                    flags.append(f"figure on pdf page {page} not read ({why}): read the figure")
                    crops[text] = (crop, "not read")
                    stats["skipped"]["not read"] += 1
                    continue
                if getattr(provider, "last_finish", None) == "length":
                    reply += "\n\n" + CUT_OFF
                if getattr(provider, "answered_by", None):          # a chain: under the model that read it
                    key = keys[text] = key_for(text, provider.answered_by)
                cache[key] = reply
                cache_path.write_text(json.dumps(cache, ensure_ascii=False))
            reply = cache[key]
            if DECORATIVE_RE.search(reply):                     # also after an explanation
                crops[text] = (crop, "decorative")
                stats["skipped"]["decorative"] += 1
                continue
            readings[text] = reply
            stats["read"] += 1
            if reply.endswith(CUT_OFF):
                flags.append(f"figure on pdf page {text.split()[2].split('-')[0]}: the reading is cut off "
                             "(the token limit or a loop): read the rest from the figure")
            check = ocr.crosscheck(crop, for_check(reply), cfg)
            if check["flag"]:
                stats["flagged"] += 1
                page = text.split()[2].split("-")[0]
                nums = sorted({x for r in check["backends"].values() if r["flag"] for x in r["numbers_only_vlm"]})
                flags.append(f"figure on pdf page {page}: the model's text and OCR disagree"
                             + (f" (numbers only the model saw: {', '.join(nums[:6])})" if nums else "")
                             + ": check the figure")
    finally:
        if hasattr(provider, "close"):
            provider.close()
    return readings, flags
