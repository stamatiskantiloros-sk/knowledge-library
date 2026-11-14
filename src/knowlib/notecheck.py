"""Checks on the topic notes the agent writes: citations, duplicates and consolidation.

Notes are where knowledge from every book, deck and lecture is merged, one note per concept, each
claim cited as [@source-id, locator]. Nothing generated them by code, so these checks are what
keep them honest:

- citations: the source exists, the page, slide, time or section exists, and the claim's numbers
  and quotations appear there; claims without a citation are listed.
- duplicates: notes for the same concept under different names, notes built on the same
  citations, and the same claim written in two notes.
- consolidation: every synthesized source is cited somewhere, frontmatter matches the body,
  index.md and [[links]] are complete, notes older than a source they cite, and sources that
  discuss a note's topic but are not cited in it.

Errors mean something is broken; warnings need a look; info is a lead to follow.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from itertools import combinations
from pathlib import Path

from .contract import read_frontmatter
from .ingestors.pdf import PAGE_RE
from .manifest import Manifest

CITE = re.compile(r"\[(@[^\]]+)\]")
LINK = re.compile(r"\[\[([^\]|#]+)")
NUM = re.compile(r"(?<![\w.])[-+]?\d[\d.,]*%?")
QUOTE = re.compile(r"[\"“]([^\"”]{12,})[\"”]")
TIME = re.compile(r"^(\d{1,2}:)?\d{1,2}:\d{2}$")
COMMENT = re.compile(r"<!--.*?-->", re.S)
STOP = set("the a an of and or in on for to with by as is are its from at vs be this that".split())


@dataclass
class Finding:
    check: str          # citations | duplicates | consolidation
    level: str          # error | warning | info
    note: str
    message: str


# ---- text helpers ---------------------------------------------------------------------------

def norm(text: str) -> str:
    """Lowercase, accents removed, quotes and dashes unified, whitespace collapsed."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn").lower()
    text = text.translate(str.maketrans({"“": '"', "”": '"', "’": "'", "‘": "'", "–": "-", "—": "-", "−": "-"}))
    return re.sub(r"\s+", " ", text).strip()


def present(values: set[float], text: str) -> set[float]:
    """The values found in the text. A value also counts as found with the other sign:
    books print the sign apart ("- $3,353", "−A$30 billion") or leave it to the column."""
    have = number_values(text)
    return {v for v in values if v in have or -v in have}


def number_values(text: str) -> set[float]:
    """Numbers as values, so 8.5% matches 8.50 and 1,000 matches 1000. A comma before exactly
    three digits groups thousands; any other comma is a decimal comma."""
    out = set()
    text = re.sub(r"(\$[A-Z]{1,3})(?=\d)", r"\1 ", unicodedata.normalize("NFKC", text))   # $A2.53, $NZ10
    text = text.replace("{,}", ",").replace("\\,", ",")                                   # LaTeX 167{,}664
    for raw in NUM.findall(text):
        n = raw.rstrip("%").strip(".,")
        n = re.sub(r",(?=\d{3}(\D|$))", "", n).replace(",", ".")
        try:
            out.add(round(float(n), 6))
        except ValueError:
            pass
    return out


def claim_numbers(text: str) -> set[float]:
    """Numbers worth checking: leaves out small whole numbers, which are mostly counts and steps."""
    return {v for v in number_values(text) if not (v == int(v) and abs(v) < 10)}


def key(name: str) -> str:
    """A concept name reduced for comparison: 'Net Present Values' -> 'net present value'."""
    words = re.findall(r"[a-z0-9]+", norm(name).replace("-", " "))
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in words)


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{3,}", norm(CITE.sub("", text))) if w not in STOP}


# ---- notes and sources -----------------------------------------------------------------------

@dataclass
class Note:
    slug: str
    path: Path
    meta: dict
    body: str

    @property
    def names(self) -> list[str]:
        return [self.meta.get("title") or self.slug, self.slug.replace("-", " "), *(self.meta.get("aliases") or [])]


def load_notes(notes_dir: Path) -> list[Note]:
    out = []
    for p in sorted(notes_dir.glob("*.md")):
        if p.name.startswith("_"):                     # _qa-log.md and other working files
            continue
        text = p.read_text(encoding="utf-8")
        body = text.split("\n---\n", 1)[1] if text.startswith("---\n") and "\n---\n" in text[4:] else text
        out.append(Note(p.stem, p, read_frontmatter(p), COMMENT.sub("", body)))
    return out


def citations(line: str) -> list[tuple[str, str]]:
    """[(source id, locator)] in a line; [@a, p. 4; @b, slide 7] is two citations, and so is
    [@a, p. 4; p. 6]: a part without its own @id cites the same source as the part before it."""
    out = []
    for group in CITE.findall(line):
        sid = None
        for part in group.split(";"):
            m = re.match(r"\s*@([\w.\-]+)\s*,?\s*(.*)", part)
            if m:
                sid = m[1]
                out.append((sid, m[2].strip()))
            elif sid and part.strip():
                out.append((sid, part.strip()))
    return out


def claims(note: Note) -> list[tuple[str, str]]:
    """(section, line) for every claim: list items and paragraphs under a ## heading, except Related.
    The summary before the first ## heading restates cited points and is not checked."""
    out, section, joinable = [], None, False
    for line in note.body.splitlines():
        if line.startswith("## "):
            section, joinable = line[3:].strip(), False
            continue
        text = line.strip()
        if section is None or section.lower().startswith("related") or not text or text.startswith(("#", "|", "```", ">")):
            joinable = False
            continue
        # a display formula, the "where" that introduces its terms, or a citation on a line of its own
        # belongs to the claim it sits in: a block cited once, on its last line, is one claim
        if joinable and (text.startswith(("$$", "[@")) or re.fullmatch(r"where:?", text, re.I)):
            out[-1] = (section, out[-1][1] + " " + text)
            continue
        out.append((section, re.sub(r"^([-*]|\d+\.)\s+", "", text)))
        joinable = True
    return out


def at_time(slide: str, t: float, after: int = 1) -> str:
    """A slide cited at a time: what the slide shows, then the spoken paragraph at that time and the
    next one. A slide can carry half an hour of speech; the whole of it would bury the passage."""
    stamps = list(re.finditer(r"^\[([\d:]+)\]", slide, re.M))
    if not stamps:
        return slide
    shown = slide[:stamps[0].start()]
    i = max((k for k, s in enumerate(stamps) if seconds(s[1]) <= t + 1), default=0)
    j = min(i + 1 + after, len(stamps))
    return shown.rstrip() + "\n\n" + slide[stamps[i].start(): stamps[j].start() if j < len(stamps) else None].strip()


class Source:
    """One source's Markdown, cut into the places a citation can point to. path is the Markdown
    file, or (separate mode, and the tests) its sources/<id>/ folder."""

    def __init__(self, path: Path, source_id: str | None = None):
        path = Path(path)
        md = path if path.suffix == ".md" else path / "source.md"
        self.id = source_id or (md.parent.name if md.name == "source.md" else md.stem)
        raw = md.read_text(encoding="utf-8")
        self.text = raw.split("\n---\n", 1)[-1]
        parts = PAGE_RE.split(self.text)
        self.pages = {parts[i]: parts[i + 2] for i in range(1, len(parts) - 2, 3)}
        self.page_order = list(self.pages)
        self.slides: dict[int, tuple[str, float | None, float | None]] = {}
        for m in re.finditer(r"^## Slide (\d+)\b[^\n]*", self.text, re.M):
            span = re.search(r"\(([\d:]+)[–-]([\d:]+)\)", m[0])
            # a slide runs to the next slide (its own text may hold "## " headings), or to the end matter
            nxt = re.search(r"^## (?:Slide \d+\b|Dropped frames|Page renders|Transcript|Unmatched)", self.text[m.end():], re.M)
            end = m.end() + nxt.start() if nxt else -1
            self.slides[int(m[1])] = (self.text[m.start():end if end > 0 else None],
                                      seconds(span[1]) if span else None, seconds(span[2]) if span else None)
        self.normed = norm(self.text)
        self.times = [(seconds(m[1]), seconds(m[2]), m.start())
                      for m in re.finditer(r"^#{2,3} [^\n]*?\(?([\d:]+)[–-]([\d:]+)\)?\s*$", self.text, re.M)]

    def region(self, locator: str) -> tuple[str | None, str]:
        """(text at the locator, problem). None with a reason when the place cannot be found."""
        loc = locator.strip().rstrip(".")
        if not loc:
            return self.text, ""
        m = re.match(r"pp?\.\s*([0-9ivxlc]+)(?:\s*[–-]\s*([0-9ivxlc]+))?$", loc, re.I)
        if m:
            if not self.pages:
                return None, "has no page markers (ingested before they existed): reprocess it to check pages"
            first, last = m[1], m[2] or m[1]
            if first not in self.pages or last not in self.pages:
                return None, f"has no page {first if first not in self.pages else last}"
            i, j = self.page_order.index(first), self.page_order.index(last)
            return "\n".join(self.pages[p] for p in self.page_order[min(i, j):max(i, j) + 1]), ""
        m = re.match(r"(?:image|img\.?)\s+(\d+)$", loc, re.I)
        if m:
            h = re.search(rf"^## Image {m[1]}:.*$", self.text, re.M)
            if not h:
                return None, f"has no image {m[1]}"
            nxt = re.search(r"^## ", self.text[h.end():], re.M)
            return self.text[h.start():h.end() + (nxt.start() if nxt else len(self.text))], ""
        m = re.match(r"slide\s+(\d+)(?:\s*,\s*([\d:]+))?$", loc, re.I)
        if m:
            n = int(m[1])
            if n not in self.slides:
                return None, f"has no slide {n}"
            text, start, end = self.slides[n]
            if m[2] and start is not None and not (start - 1 <= seconds(m[2]) <= (end or start) + 1):
                return None, f"slide {n} runs {m_s(start)}–{m_s(end)}, not {m[2]}"
            if m[2]:
                return at_time(text, seconds(m[2])), ""
            return text, ""
        if TIME.match(loc):
            t = seconds(loc)
            hit = [i for i, (a, b, _) in enumerate(self.times) if a - 1 <= t <= b + 1]
            if not hit:
                return None, f"has no passage at {loc}"
            start = self.times[hit[0]][2]
            nxt = re.search(r"\n#{2,3} ", self.text[start + 1:])
            return self.text[start:start + 1 + nxt.start() if nxt else None], ""
        m = re.match(r"§\s*(.+)$", loc)
        if m:
            want = norm(m[1])
            for h in re.finditer(r"^(#{1,6}) (.+)$", self.text, re.M):
                if want in norm(h[2]):
                    nxt = re.search(rf"^#{{1,{len(h[1])}}} ", self.text[h.end():], re.M)
                    return self.text[h.start():h.end() + (nxt.start() if nxt else len(self.text))], ""
            return None, f"has no section '{m[1]}'"
        return None, f"locator '{locator}' is not a page (p. 4), slide (slide 12, 14:32), image (image 3), time (23:10) or section (§ Heading)"

    def neighbour(self, locator: str, values: set[float], quotes: list[str]) -> str:
        """The page a page off where the missing numbers and quotes are, if any."""
        m = re.match(r"pp?\.\s*([0-9ivxlc]+)", locator.strip(), re.I)
        if not m or m[1] not in self.pages:
            return ""
        i = self.page_order.index(m[1])
        for j in (i - 1, i + 1):
            if 0 <= j < len(self.page_order):
                t = self.pages[self.page_order[j]]
                if values <= present(values, t) and all(q in norm(t) for q in quotes):
                    return self.page_order[j]
        return ""


def seconds(ts: str) -> float:
    s = 0.0
    for part in ts.split(":"):
        s = s * 60 + float(part)
    return s


def m_s(t: float | None) -> str:
    return "?" if t is None else f"{int(t // 60):02d}:{int(t % 60):02d}"


# ---- the three checks ------------------------------------------------------------------------

def check_citations(notes: list[Note], sources: dict[str, Source]) -> list[Finding]:
    out = []
    for note in notes:
        for section, text in claims(note):
            cites = citations(text)
            if not cites:
                if len(text.split()) >= 4:              # a label or a lone term is not a claim
                    out.append(Finding("citations", "warning", note.slug, f"no citation ({section}): {text[:90]}"))
                continue
            claim = CITE.sub("", text)
            values = claim_numbers(claim)
            # a quotation may close on the writer's punctuation ("... on a particular date,")
            quotes = [norm(q).rstrip(",.;:") for q in QUOTE.findall(claim) if len(q.split()) >= 4]
            for sid, loc in cites:
                if sid not in sources:
                    out.append(Finding("citations", "error", note.slug, f"[@{sid}] is not a source in the library"))
                    continue
                region, problem = sources[sid].region(loc)
                if region is None:
                    level = "warning" if "no page markers" in problem else "error"
                    out.append(Finding("citations", level, note.slug, f"[@{sid}, {loc}]: the source {problem}"))
                    continue
                if len(cites) > 1:
                    continue                           # several sources: the numbers may come from any one
                missing = values - present(values, region)
                lost = [q for q in quotes if q not in norm(region)]
                if missing or lost:
                    what = ", ".join([*(f"{v:g}" for v in sorted(missing)), *(f'"{q[:40]}"' for q in lost)])
                    near = sources[sid].neighbour(loc, missing, lost)
                    hint = f"; it is on p. {near}" if near else ""
                    out.append(Finding("citations", "warning", note.slug,
                                       f"[@{sid}, {loc}] does not contain {what}{hint}: {claim.strip()[:80]}"))
    return out


def check_duplicates(notes: list[Note]) -> list[Finding]:
    out = []
    names = {n.slug: {key(x) for x in n.names if key(x)} for n in notes}
    cited = {n.slug: {(s, norm(l)) for _, t in claims(n) for s, l in citations(t)} for n in notes}
    vocab = {n.slug: words(n.body) for n in notes}
    for a, b in combinations(notes, 2):
        shared = names[a.slug] & names[b.slug]
        if shared:
            out.append(Finding("duplicates", "warning", a.slug, f"same concept as {b.slug}? both are called '{sorted(shared)[0]}'"))
            continue
        ca, cb = cited[a.slug], cited[b.slug]
        if len(ca & cb) >= 3 and len(ca & cb) / min(len(ca), len(cb)) >= 0.5:
            out.append(Finding("duplicates", "warning", a.slug,
                               f"shares {len(ca & cb)} of its citations with {b.slug}: one concept, or two that should link?"))
            continue
        va, vb = vocab[a.slug], vocab[b.slug]
        if va and vb and len(va & vb) / len(va | vb) >= 0.6:
            out.append(Finding("duplicates", "warning", a.slug, f"most of its wording is also in {b.slug}"))
    seen: dict[str, str] = {}
    for n in notes:
        for _, text in claims(n):
            k = norm(CITE.sub("", text))
            if len(k.split()) >= 8:
                if k in seen and seen[k] != n.slug:
                    out.append(Finding("duplicates", "info", n.slug, f"same claim as in {seen[k]}: {text[:70]}"))
                seen.setdefault(k, n.slug)
    return out


def check_consolidation(root: Path, notes: list[Note], sources: dict[str, Source], manifest: Manifest,
                        index_path: Path | None = None) -> list[Finding]:
    out = []
    slugs = {n.slug for n in notes}
    cited_anywhere: set[str] = set()
    entries = {e["source_id"]: e for e in manifest.entries.values() if e.get("source_id")}
    index_path = index_path or root / "index.md"
    index = COMMENT.sub("", index_path.read_text(encoding="utf-8")) if index_path.exists() else ""
    for note in notes:
        body_ids = {sid for _, t in claims(note) for sid, _ in citations(t)}
        cited_anywhere |= body_ids
        listed = set(note.meta.get("sources") or [])
        if body_ids - listed:
            out.append(Finding("consolidation", "warning", note.slug,
                               f"cited but missing from sources: {', '.join(sorted(body_ids - listed))}"))
        if listed - body_ids:
            out.append(Finding("consolidation", "warning", note.slug,
                               f"listed in sources but never cited: {', '.join(sorted(listed - body_ids))}"))
        if f"[[{note.slug}]]" not in index and f"notes/{note.slug}.md" not in index:
            out.append(Finding("consolidation", "warning", note.slug, f"not in {index_path.name}"))
        for link in sorted({x.strip() for x in LINK.findall(note.body)} - slugs):
            out.append(Finding("consolidation", "error", note.slug, f"[[{link}]] has no note"))
        updated = str(note.meta.get("updated") or "")
        for sid in sorted(body_ids):
            changed = str((entries.get(sid) or {}).get("updated_at") or "")[:10]
            if updated and changed and changed > updated:
                out.append(Finding("consolidation", "warning", note.slug,
                                   f"{sid} was reprocessed on {changed}, after this note ({updated}): recheck its citations"))
        # the merge check: who else discusses this concept and is not cited here
        # each term is counted on its own, so a lead shows what matched: a generic alias
        # ("average cost" inside "weighted average cost of capital") is then easy to dismiss
        terms = sorted({norm(x) for x in note.names if len(x) >= 4})
        hits = []
        for sid, s in sources.items():
            if sid in body_ids:
                continue
            counts = {t: len(re.findall(rf"\b{re.escape(t)}\b", s.normed)) for t in terms}
            counts = {t: n for t, n in counts.items() if n}
            if sum(counts.values()) >= 3:
                hits.append((sum(counts.values()), sid, counts))
        hits = sorted(hits, reverse=True)[:5]
        if hits:
            out.append(Finding("consolidation", "info", note.slug, "also discussed in, not cited: " + "; ".join(
                f"{sid} ({', '.join(f'{t} {n}×' for t, n in sorted(c.items(), key=lambda kv: -kv[1]))})"
                for _, sid, c in hits)))
    for link in sorted({x.strip() for x in LINK.findall(index)} - slugs):
        out.append(Finding("consolidation", "error", index_path.name, f"[[{link}]] has no note"))
    for sid, e in sorted(entries.items()):
        if e.get("synthesized") and sid not in cited_anywhere:
            out.append(Finding("consolidation", "error", "-", f"{sid} is marked synthesized but no note cites it"))
    return out


# ---- entry point -----------------------------------------------------------------------------

CHECKS = ("citations", "duplicates", "consolidation")


def run(root: Path, only: list[str] | None = None, note_slugs: list[str] | None = None) -> list[Finding]:
    from .layout import open_layout
    lay, manifest = open_layout(root)
    root = lay.root
    notes = load_notes(lay.notes)
    sources = {sid: Source(md, sid) for sid, md in lay.all_sources(manifest.entries).items()}
    picked = [n for n in notes if not note_slugs or n.slug in note_slugs]
    only = only or list(CHECKS)
    findings = []
    if "citations" in only:
        findings += check_citations(picked, sources)
    if "duplicates" in only:
        findings += [f for f in check_duplicates(notes) if not note_slugs or f.note in note_slugs]
    if "consolidation" in only:
        findings += [f for f in check_consolidation(root, notes, sources, manifest, lay.index)
                     if not note_slugs or f.note in note_slugs]
    lay.work.mkdir(parents=True, exist_ok=True)
    (lay.work / "notes_check.json").write_text(json.dumps([asdict(f) for f in findings], ensure_ascii=False, indent=1),
                                           encoding="utf-8")
    return findings


def report(findings: list[Finding], n_notes: int) -> str:
    order = {"error": 0, "warning": 1, "info": 2}
    count = {lvl: sum(f.level == lvl for f in findings) for lvl in order}
    lines = [f"{n_notes} notes: {count['error']} errors, {count['warning']} warnings, {count['info']} leads"]
    for f in sorted(findings, key=lambda f: (order[f.level], f.check, f.note)):
        lines.append(f"{f.level:7s} {f.check:13s} {f.note}: {f.message}")
    return "\n".join(lines)
