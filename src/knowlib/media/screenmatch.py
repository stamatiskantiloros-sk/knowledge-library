"""Which page of the course materials each recorded screen shows.

A screen recording is a guide to what was taught and when; where the lecture's own documents are in
the folder (its slide decks, handouts, assignment), they are the source. Each kept screen is matched,
on this Mac, to the document page it shows:

1. The PDF viewer's page counter ("16 / 19" in the toolbar), read by OCR. It is trusted when its
   total is the page count of one of the documents (several with that count: the text decides).
2. Otherwise the words on the screen against the words of every page: the page that contains most of
   the screen's words and shares the most with it, ties going to the page nearest the last match.

A screen that matches nothing (a web page, a video, a file browser) keeps its recording time as its
only reference. The match is written into the lecture file as a **From:** line, with the page drawn
from the document as an image, so the handbook cites and shows the material, not the screen.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

STOP = {"the", "and", "for", "are", "with", "that", "this", "from", "you", "your", "our", "its", "not", "can", "will",
        "has", "have", "was", "were", "but", "all", "any", "per", "how", "what", "who", "why", "into", "one", "two"}
COUNTER = re.compile(r"(?<![\d/:.])(\d{1,3}) ?/ ?(\d{1,3})(?![\d/:.])")
MIN_SHARED = 6          # words a screen must share with a page to match it by text
MIN_CONTAINED = 0.6     # share of the screen's words that must be on the page
MIN_JACCARD = 0.3


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]{3,}", (text or "").lower())) - STOP


@dataclass
class Doc:
    path: Path
    label: str               # how the handbook names it: "deck 2-2", "Productivity assignment"
    unit: str                # "slide" for landscape pages, "page" for portrait ones
    pages: list[set[str]] = field(default_factory=list)

    def cite(self, pages: list[int]) -> str:
        nums = sorted(set(pages))
        runs, a = [], nums[0]
        for x, y in zip(nums, nums[1:] + [None]):
            if y != x + 1:
                runs.append(f"{a}" if a == x else f"{a}–{x}")
                a = y
        unit = self.unit + ("s" if len(nums) > 1 else "")
        return f"{self.label}, {unit} {', '.join(runs)}"


@dataclass
class Match:
    doc: Doc
    page: int
    how: str                 # "page counter" or "text"

    def cite(self) -> str:
        return self.doc.cite([self.page])


def default_label(path: Path) -> str:
    """'2-2_LocationLayout-iMBA_26-27-Students.pdf' -> 'deck 2-2'; otherwise the name in words."""
    stem = path.stem
    if m := re.match(r"(\d+(?:-\d+)?)[_ ]", stem):
        return f"deck {m[1]}"
    stem = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", stem)
    stem = re.sub(r"[_-]+", " ", stem)
    stem = re.sub(r"\b(Students?|Student version|ALL|\d{2,4})\b", "", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    return stem[:1].upper() + stem[1:] if stem else path.stem


def load_docs(folder: Path, labels: dict | None = None, only: list[str] | None = None) -> list[Doc]:
    """The PDFs of a lecture folder (or those named in `only`), with each page's words."""
    import pymupdf
    labels = labels or {}
    paths = [folder / n for n in only] if only else sorted(Path(folder).glob("*.pdf"))
    docs = []
    for p in paths:
        if not p.exists() or p.suffix.lower() != ".pdf":
            continue
        try:
            d = pymupdf.open(p)
        except Exception:  # noqa: BLE001 - a damaged or encrypted PDF is simply not a source here
            continue
        if not d.page_count:
            continue
        first = d[0].rect
        docs.append(Doc(p, str(labels.get(p.name) or default_label(p)),
                        "slide" if first.width > first.height else "page",
                        [words(pg.get_text()) for pg in d]))
    return docs


def _text_scores(w: set[str], docs: list[Doc]):
    for doc in docs:
        for i, pw in enumerate(doc.pages, 1):
            shared = len(w & pw)
            if shared:
                yield shared / len(w), shared / len(w | pw), shared, doc, i


def _counter(ocr: str, w: set[str], docs: list[Doc]) -> Match | None:
    for n, total in COUNTER.findall(ocr or ""):
        n, total = int(n), int(total)
        if total < 3 or not 1 <= n <= total:
            continue
        same = [d for d in docs if len(d.pages) == total]
        if len(same) == 1:
            return Match(same[0], n, "page counter")
        if same:                                        # several documents this long: their words decide
            best = max(same, key=lambda d: len(w & d.pages[n - 1]))
            return Match(best, n, "page counter")
    return None


def match_screens(screens: list[tuple[str, str]], docs: list[Doc]) -> list[Match | None]:
    """screens: (text read from the screen, OCR of the whole frame) in the order shown."""
    out: list[Match | None] = []
    last: Match | None = None
    for text, ocr in screens:
        w = words(text) or words(ocr)
        found = _counter(ocr, w, docs) if docs else None
        if found is None and w:
            cands = [c for c in _text_scores(w, docs)
                     if c[2] >= MIN_SHARED and c[0] >= MIN_CONTAINED and c[1] >= MIN_JACCARD]
            if cands:
                near = _best(cands)

                def distance(c):
                    if last and c[3] is last.doc:
                        return abs(c[4] - last.page)
                    return 10_000 + c[4]
                c = min(near, key=distance)
                found = Match(c[3], c[4], "text")
        out.append(found)
        if found:
            last = found
    _between_neighbours(screens, out)
    return out


NEIGHBOUR_CONTAINED = 0.15   # a screen between two matches of one document needs only this


def _between_neighbours(screens: list[tuple[str, str]], out: list[Match | None]) -> None:
    """A screen left unmatched between two matches of the same document (a page scrolled half into
    view, a slide whose text the reading garbled) is matched within the pages between them."""
    for i, m in enumerate(out):
        if m is not None:
            continue
        before = next((x for x in reversed(out[:i]) if x), None)
        after = next((x for x in out[i + 1:] if x), None)
        if not before or not after or before.doc is not after.doc or before.page > after.page:
            continue
        w = words(screens[i][0]) or words(screens[i][1])
        same = before.page == after.page                # left and back again: that page, if it shares words
        cands = [c for c in _text_scores(w, [before.doc]) if before.page <= c[4] <= after.page
                 and c[2] >= MIN_SHARED and (same or c[0] >= NEIGHBOUR_CONTAINED)] if w else []
        if cands:
            out[i] = Match(before.doc, min(c[4] for c in _best(cands)), "between neighbours")


def _best(cands: list[tuple]) -> list[tuple]:
    """The pages holding the most of the screen's words; among them the closest overall (a screen of an
    early build of a slide is closer to that build than to the later ones, which hold its words too).
    More than one when pages repeat."""
    top_c = max(c[0] for c in cands)
    near = [c for c in cands if c[0] >= top_c - 0.03]
    top_j = max(c[1] for c in near)
    return [c for c in near if c[1] >= top_j - 0.02]


def page_image(doc: Doc, page: int, out_dir: Path, dpi: int = 150) -> Path:
    """The document page drawn as an image (kept once per page)."""
    import pymupdf
    out = Path(out_dir) / f"{re.sub(r'[^A-Za-z0-9._-]+', '_', doc.path.stem)}-{page:03d}.png"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        pymupdf.open(doc.path)[page - 1].get_pixmap(dpi=dpi).save(str(out))
    return out


def from_line(m: Match, image: str) -> str:
    """The lecture file's line for a matched screen: what to cite · which file · the page's image."""
    return f"**From:** {m.cite()} · {m.doc.path.name} p. {m.page} ({m.how}) · [page image]({image})"


FROM = re.compile(r"^\*\*From:\*\* (?P<cite>.+?) · (?P<file>.+?) p\. (?P<page>\d+) \((?P<how>[^)]*)\)"
                  r"(?: · \[page image\]\((?P<image>[^)]+)\))?\s*$", re.M)


def relabel(md: Path, folder: Path, labels: dict | None = None, ocr=None) -> dict:
    """Add (or redo) the **From:** lines of a lecture file already written, reading each screen's
    image with Apple Vision for the page counter. `ocr(path) -> str` lets a test skip Apple Vision."""
    from .redact import lines as vision_lines
    md = Path(md)
    raw = md.read_text(encoding="utf-8")
    if labels is None and (Path(folder) / "session.yaml").exists():
        import yaml
        labels = (yaml.safe_load((Path(folder) / "session.yaml").read_text(encoding="utf-8")) or {}).get("labels")
    docs = load_docs(folder, labels)
    raw = FROM.sub("", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    blocks = list(re.finditer(r"^## Slide (\d+) \([^\n]*\n", raw, re.M))
    screens, images = [], []
    for i, b in enumerate(blocks):
        body = raw[b.end(): blocks[i + 1].start() if i + 1 < len(blocks) else len(raw)]
        img = re.search(r"\[full resolution\]\(([^)]+)\)", body) or re.search(r"!\[[^\]]*\]\(([^)]+)\)", body)
        path = md.parent / img.group(1) if img else None
        text = body.split("**On slide", 1)[-1].split("**OCR text", 1)[0].split("**Figure:**", 1)[0] \
            .split("**Spoken:**", 1)[0] if "**On slide" in body else ""
        if path and path.exists():
            seen = ocr(path) if ocr else "\n".join(t for t, _ in vision_lines(path))
        else:
            seen = ""
        screens.append((text, seen))
        images.append(img)
    matches = match_screens(screens, docs)
    assets = md.parent / "assets" if (md.parent / "assets").is_dir() else None
    if assets is None:                                   # a library's sources: images are under _knowlib/assets/<id>
        first = next((m for m in images if m), None)
        assets = (md.parent / first.group(1)).parent if first else md.parent / "assets"
        if assets.name == "view":
            assets = assets.parent
    out, pos = [], 0
    for b, img, m in zip(blocks, images, matches):
        end = b.end() + img.end() if img else b.end()      # img was found in the block's body
        line_end = raw.find("\n", end)
        line_end = len(raw) if line_end < 0 else line_end
        out.append(raw[pos:line_end])
        if m:
            pic = page_image(m.doc, m.page, assets / "pages")
            out.append("\n" + from_line(m, _rel(pic, md.parent)))
        pos = line_end
    out.append(raw[pos:])
    md.write_text("".join(out), encoding="utf-8")
    by = {}
    for m in matches:
        key = m.how if m else "none"
        by[key] = by.get(key, 0) + 1
    return {"screens": len(matches), **by}


def _rel(path: Path, base: Path) -> str:
    import os
    return Path(os.path.relpath(path, base)).as_posix()
