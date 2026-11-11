"""Handbooks: one book per lecture, written in the order the lecture was taught.

It reads only finished source Markdown (sources/<id>/source.md, or <file>.md beside it in place), so ingestion stays the job of the rest of
knowlib. The lecture sets the content: each section is written from its own slides alone. Course
materials reach the book only as a checked note added afterwards, because a course PDF can hold the
lecture's whole worked example and the writer then runs ahead of the section. A number in the text
that no source supports is sent back once and then listed in review.md, as is every slide the OCR
check flagged.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .. import progress
from ..contract import slugify
from ..manifest import Manifest
from ..media.ocr import numbers
from ..providers import get_vision_provider, parse_json, release_all
from ..runner import open_library, process

log = logging.getLogger("knowlib")

PLAN_PROMPT = """Below are the slides of one recorded lecture, in the order they were taught.
Divide the lecture into consecutive sections a student can read as chapters of a book. Keep the
lecture's own order and cover every slide exactly once. Leave out nothing; a section may be one slide.

{slides}

Return JSON only: {{"title": "<title of the lecture>",
 "sections": [{{"title": "<short heading>", "first_slide": <number>, "last_slide": <number>}}]}}"""

WRITE_PROMPT = """You are writing one section of a study book that records a lecture.
A student who missed the class will read it instead of watching the recording.

Section: {title}

What was taught, slide by slide:
{material}

Rules:
- Write flowing prose in {language}, following the order of the lecture. Do not reorganise it.
- Stay faithful to what was taught. Keep the lecturer's definitions, examples and reasoning, and give a topic as much space as it got in class. Remove filler and repetition.
- Write only what this section's slides and the lecturer's words contain. Do not continue into later steps, and do not add explanations, consequences or conclusions the lecturer did not state.
- "What it shows" is an automatic description of the image. Use it to understand the picture, never as a source of claims about the subject.
- Do not count rows or items, or state a total, unless the slide or the lecturer does.
- Report only what this section contains. Do not link back to earlier steps or ahead to later ones, sum up the procedure, or comment on the method. Say "the lecturer said" only for what the lecturer's words here contain.
- Do not describe decorative images. Call the speaker "the lecturer", without he or she.
- Copy every number, formula and table exactly from the slide text. Never compute a value that was not shown or said. Do not add a Total or any label the slide lacks.
- Use Markdown tables for tables and LaTeX for formulas.
- To place a slide's image, write [[slide N]] on its own line where the reader needs it. Use it for charts, tables, diagrams and screens, not for plain text slides.
- Do not write the section heading, an introduction to the book, or closing remarks.
Output only the section text."""

BRIEF_RULES = """You are writing the study book of one recorded lecture, for a student who will read it instead of watching.

1. Read every slide below in order, then write draft.md beside this brief: a # title, then ## sections in the order taught. The chapter breaks are yours.
2. Open the image of every flagged slide, every slide with a table, formula, chart or diagram, and every slide whose text looks wrong. The image decides. List each correction in corrections.md: slide, what the text said, what the image shows.
3. Stay faithful. Keep the lecturer's definitions, examples and reasoning, and give a topic as much space as it got in class. Remove filler and repetition. Each topic appears once.
4. Write only what the lecture contains: no links ahead, summaries or comments on the method. Attribute to the lecturer only what the transcript says. "What it shows" is an automatic description, never a source of claims.
5. Copy every number, formula and table exactly. Do not compute, count or total anything that was not shown or said.
6. If the lecturer says something wrong, keep it and add a note saying so, plainly.
7. Add a note only where the student cannot follow without it: a line starting "> **Note (not from class):**", one or two sentences, naming its source; for a book, also its page, from the `<!-- page N (pdf M) -->` line above the passage (N is the printed page). The passages below are the closest matches only: search all the course materials, listed at the end, for what the lecture leaves unclear, for conflicts with it, and to verify a note.
8. Write [[slide N]] on its own line where the reader needs an image: charts, tables, diagrams, screens. Use LaTeX for formulas and Markdown tables, with the same number of cells in every row.
9. End each section with its slides and times in italics. Call the speaker "the lecturer", without he or she.

When done, run: knowlib handbook --finish --output <the folder above this one>"""

TABLE_PROMPT = """{text}

A table above has rows with a different number of cells from its header, so values sit under the wrong headings.
Rewrite the section with every row matching its header, copying each table from this material. Keep everything else unchanged.

{material}

Output only the section text."""

NOTE_PROMPT = """Below is one section of a study book that records a lecture, then passages from the course materials.

{text}

Passages:
{passages}

Almost every section needs nothing. Add a note only if the student cannot follow this section or the next one without something missing here that a passage states. Say nothing the next slide of the lecture will give anyway. Use only the passages and never mention them; quote only their exact words.
Return JSON only: {{"note": null}} or {{"note": "<one or two sentences>", "passage": <number>}}"""

FIX_PROMPT = """{text}

The section above contains these numbers, which appear in no slide or transcript of this section: {bad}.
Rewrite it, removing or correcting each one using only this material. Keep everything else unchanged.

{material}

Output only the section text."""


@dataclass
class Slide:
    n: int
    start: str
    end: str
    text: str = ""
    caption: str = ""
    spoken: str = ""
    frame: str = ""
    flagged: bool = False
    from_ocr: bool = False
    ocr: str = ""

    def usable(self) -> bool:
        """A slide with no reading and nothing said is an opening image or a failed reading."""
        return bool(self.spoken or (self.text and not self.from_ocr))

    def material(self) -> str:
        text = "" if self.from_ocr else self.text
        parts = [f"[Slide {self.n}, {self.start}]"]
        if text:
            parts.append(f"On the slide:\n{text}")
        if self.caption:
            parts.append(f"What it shows: {self.caption}")
        if self.spoken:
            parts.append(f"The lecturer said: {self.spoken}")
        return "\n".join(parts)


@dataclass
class Lecture:
    source_id: str
    key: str
    title: str
    folder: Path
    slides: list[Slide] = field(default_factory=list)


def _between(block: str, start: str, ends: tuple[str, ...]) -> str:
    i = block.find(start)
    if i < 0:
        return ""
    body = block[i + len(start):]
    cuts = [body.find(e) for e in ends if e in body]
    return (body[:min(cuts)] if cuts else body).strip()


def parse_source(path: Path, source_id: str = "", key: str = "") -> Lecture:
    """path: the lecture's Markdown, or (separate mode) its sources/<id>/ folder. Frame paths in
    the Markdown are relative to its folder, in both layouts."""
    path = Path(path)
    md = path if path.suffix == ".md" else path / "source.md"
    folder = md.parent
    raw = md.read_text(encoding="utf-8")
    meta = yaml.safe_load(raw.split("---", 2)[1]) or {}
    body = raw.split("---", 2)[2].split("\n## Dropped frames", 1)[0]   # not slides
    lecture = Lecture(source_id or folder.name, key, str(meta.get("title", folder.name)), folder)
    labels = ("**OCR text", "**Figure:**", "**Spoken:**")
    head, *blocks = re.split(r"^## Slide ", body, flags=re.M)
    before = head.split("## Before the first slide")[1].split("\n", 1)[-1].strip() if "## Before the first slide" in head else ""
    for b in blocks:
        m = re.match(r"(\d+)\s*\(([\d:]+)[–-]([\d:]+)\)", b)
        if not m:
            continue
        frame = re.search(r"\[full resolution\]\(([^)]+)\)", b) or re.search(r"!\[[^\]]*\]\(([^)]+)\)", b)
        lecture.slides.append(Slide(
            int(m[1]), m[2], m[3],
            text=_between(b, "**On slide (OCR):**", labels) or _between(b, "**On slide:**", labels),
            caption=_between(b, "**Figure:**", ("**Spoken:**",)),
            spoken=_between(b, "**Spoken:**", ("\n## ",)),
            frame=frame.group(1) if frame else "",
            flagged="**OCR check:**" in b, from_ocr="**On slide (OCR):**" in b,
            ocr=" ".join(re.findall(r"\*\*OCR text[^\n]*\n(.*?)(?=\n\*\*|\Z)", b, re.S))))
    if before and lecture.slides:
        lecture.slides[0].spoken = f"{before} {lecture.slides[0].spoken}".strip()
    return lecture


def valid_plan(plan: dict, slides: list[Slide]) -> list[dict]:
    """Sections that cover every slide once, in order. Anything else: one section per run of 4."""
    nums = [s.n for s in slides]
    secs = plan.get("sections") if isinstance(plan, dict) else None
    try:
        covered = [n for s in secs for n in range(int(s["first_slide"]), int(s["last_slide"]) + 1)]
    except (TypeError, KeyError, ValueError):
        covered = []
    if covered == nums:
        return secs
    return [{"title": f"Part {i // 4 + 1}", "first_slide": nums[i], "last_slide": nums[min(i + 3, len(nums) - 1)]}
            for i in range(0, len(nums), 4)]


def unsupported_numbers(text: str, sources: str) -> list[str]:
    """Numbers in the written text that the section's sources do not contain; labelled notes and
    [[slide N]] markers are left out."""
    prose = "\n".join(l for l in text.splitlines() if not l.startswith("> **Note"))
    prose = re.sub(r"\[\[slide \d+\]\]", "", prose)
    known = set(numbers(sources))
    return sorted({n for n in numbers(prose) if n not in known and len(n.rstrip("%")) > 1})


def relevant_passages(lecture_text: str, materials: list[tuple[str, str]], top: int = 3, size: int = 1200) -> list[tuple[str, str]]:
    """The course-material passages sharing the most uncommon words with this part of the lecture."""
    words = {w for w in re.findall(r"\w{5,}", lecture_text.casefold())}
    scored = []
    for title, text in materials:
        for i in range(0, len(text), size):
            chunk = text[i:i + size]
            hits = len(words & set(re.findall(r"\w{5,}", chunk.casefold())))
            if hits >= 5:
                scored.append((hits, title, chunk))
    scored.sort(key=lambda r: -r[0])
    return [(t, c) for _, t, c in scored[:top]]


def checked_note(reply: dict, passages: list[tuple[str, str]]) -> tuple[str, str]:
    """(note line, problem). A note must be short and every number and quote must be in its passage."""
    note = reply.get("note") if isinstance(reply, dict) else None
    if not note:
        return "", ""
    try:
        title, passage = passages[int(reply.get("passage")) - 1]
    except (TypeError, ValueError, IndexError):
        return "", f"note dropped, no passage given: {note}"
    flat = " ".join(passage.split()).casefold()
    quotes = re.findall(r'["“]([^"”]+)["”]', note)
    bad = [q for q in quotes if " ".join(q.split()).casefold().rstrip(".,") not in flat]
    bad += sorted(set(numbers(note)) - set(numbers(passage)))
    if bad or len(note) > 500 or "passage" in note.casefold():
        return "", f"note dropped, not supported or too long: {note}"
    return f"> **Note (not from class):** {note} *(Source: {title})*", ""


def missing_table_numbers(text: str, slide: Slide) -> list[str]:
    """Numbers of a slide's table that the written text lost."""
    table = "\n".join(l for l in slide.text.splitlines() if l.lstrip().startswith("|"))
    return sorted(set(numbers(table)) - set(numbers(text)))


def _tables(text: str) -> list[list[str]]:
    """Each Markdown table as its lines."""
    tables, current = [], []
    for line in text.splitlines() + [""]:
        if line.lstrip().startswith("|"):
            current.append(line.strip())
        elif current:
            tables.append(current)
            current = []
    return tables


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def ragged_tables(text: str) -> bool:
    return any(len({len(_cells(l)) for l in table}) > 1 for table in _tables(text))


def drop_repeated_tables(text: str, previous: set[str]) -> tuple[str, set[str]]:
    """Replace a table the section before already gave; returns the text and this section's tables."""
    seen = set()
    for table in _tables(text):
        body = [_cells(l) for l in table if not set(l) <= set("|-: ")]
        key = repr(body)
        seen.add(key)
        if key in previous:
            text = text.replace("\n".join(table), "*(The same table as in the previous section.)*")
    return text, seen


CUT_RE = re.compile(r"\n\[CUT OFF at (?:the token limit|(\d+) tokens)\]$")


class Cache:
    """Finished model replies, so an interrupted book continues where it stopped."""

    def __init__(self, path: Path):
        import threading
        self.path = path
        self.data = json.loads(path.read_text()) if path.exists() else {}
        self._lock = threading.Lock()

    def ask(self, provider, prompt: str, tokens: int) -> str:
        return self.ask_checked(provider, prompt, tokens, retries=0)[0]

    def ask_checked(self, provider, prompt: str, tokens: int, retries: int = 1) -> tuple[str, bool]:
        """(reply, whether the token limit cut it off). A cut-off reply is asked again with twice the
        tokens, up to `retries` times. One still cut off is cached with the tokens it had, and asked
        again only by a run that allows more."""
        models = getattr(provider, "cache_models", None) or [provider.model]
        keys = [hashlib.sha256(f"{m}\n{prompt}".encode()).hexdigest() for m in models]
        key = keys[0]
        most = tokens * 2 ** retries
        hit = next((self.data[k] for k in keys if k in self.data), None)
        if hit is not None:
            m = CUT_RE.search(hit)
            if not m:
                return hit, False
            if most <= int(m.group(1) or most):        # no more room than last time: the same answer
                return hit[:m.start()], True
        reply, cut, used = "", False, tokens
        for n in range(1 + retries):
            used = tokens * 2 ** n
            reply = provider.generate([], prompt, max_tokens=used).strip()
            cut = getattr(provider, "last_finish", None) == "length"
            if reply and not cut:
                break
        if not reply:                             # e.g. the model spent every token thinking: ask again next run
            return reply, cut
        answered = getattr(provider, "answered_by", None)     # a chain: store it under the model that wrote it
        if answered and answered != models[0]:
            key = hashlib.sha256(f"{answered}\n{prompt}".encode()).hexdigest()
        self.put(key, reply + (f"\n[CUT OFF at {used} tokens]" if cut else ""))
        return reply, cut

    @staticmethod
    def key(model, prompt: str) -> str:
        return hashlib.sha256(f"{model}\n{prompt}".encode()).hexdigest()

    def lookup(self, provider, prompt: str) -> str | None:
        """A cached reply to this prompt from any model the provider may answer with."""
        models = getattr(provider, "cache_models", None) or [provider.model]
        return next((self.data[k] for k in (self.key(m, prompt) for m in models) if k in self.data), None)

    def put(self, key: str, value: str) -> None:
        """Store one reply and save the file (safe from several threads)."""
        with self._lock:
            self.data[key] = value
            self.path.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")


def write_lecture(lecture: Lecture, materials: list[tuple[str, str]], provider, cfg: dict, out: Path) -> dict:
    hb = cfg["handbook"]
    out.mkdir(parents=True, exist_ok=True)
    cache = Cache(out / ".cache.json")
    tokens = int(hb.get("max_tokens", 6000))
    slides = lecture.slides
    listing = "\n".join(f"Slide {s.n} ({s.start}): {(s.caption or s.text)[:160]!r} / {s.spoken[:120]!r}" for s in slides)
    try:
        plan = parse_json(cache.ask(provider, PLAN_PROMPT.format(slides=listing), tokens))
    except ValueError:
        plan = {}
    sections = valid_plan(plan, slides)
    book = [f"# {plan.get('title') or lecture.title}", ""]
    issues: list[str] = []
    used_notes = 0
    previous: set[str] = set()
    for i, sec in enumerate(sections, 1):
        progress.stage("handbook sections", i, len(sections))
        part = [s for s in slides if int(sec["first_slide"]) <= s.n <= int(sec["last_slide"])]
        issues += [f"slide {s.n} left out: no reliable text and nothing said" for s in part if not s.usable()]
        used = [s for s in part if s.usable()]
        if not used:
            continue
        material = "\n\n".join(s.material() for s in used)
        text = cache.ask(provider, WRITE_PROMPT.format(title=sec["title"], material=material,
                                                       language=hb.get("language", "English")), tokens)
        grounds = material + "\n" + "\n".join(s.ocr for s in used)
        bad = unsupported_numbers(text, grounds)
        if bad:
            text = cache.ask(provider, FIX_PROMPT.format(text=text, bad=", ".join(bad), material=material), tokens)
            bad = unsupported_numbers(text, grounds)
        if bad:
            issues.append(f"{sec['title']}: numbers no source supports: {', '.join(bad)}")
        if ragged_tables(text):
            text = cache.ask(provider, TABLE_PROMPT.format(text=text, material=material), tokens)
        if ragged_tables(text):
            issues.append(f"{sec['title']}: a table has rows of different lengths")
        issues += [f"slide {s.n}: table numbers not in the text: {', '.join(lost)}" for s in used
                   if f"[[slide {s.n}]]" not in text and (lost := missing_table_numbers(text, s))]
        text, previous = drop_repeated_tables(text, previous)
        body = _place_figures(text, used, lecture, out, issues)
        passages = relevant_passages(material, materials)
        if passages:
            listing = "\n\n".join(f"Passage {n}, from {t}:\n{c}" for n, (t, c) in enumerate(passages, 1))
            try:
                reply = parse_json(cache.ask(provider, NOTE_PROMPT.format(text=text, passages=listing), tokens))
            except ValueError:
                reply = {}
            note, problem = checked_note(reply, passages)
            if problem:
                issues.append(f"{sec['title']}: {problem}")
            if note:
                body += "\n\n" + note
                used_notes += 1
        span = f"Slide {part[0].n}" if len(part) == 1 else f"Slides {part[0].n}–{part[-1].n}"
        book += [f"## {sec['title']}", "", body, "", f"*{span}, {part[0].start}–{part[-1].end}*", ""]
    (out / "handbook.md").write_text("\n".join(book), encoding="utf-8")
    flagged = [s for s in slides if s.flagged]
    review = [f"# Review: {lecture.title}", "", f"Source: {lecture.source_id}", "", "## To check", ""]
    review += [f"- {x}" for x in issues] or ["None."]
    review += ["", "## Slides the OCR check flagged", ""]
    review += [f"- Slide {s.n} ({s.start})" for s in flagged] or ["None."]
    (out / "review.md").write_text("\n".join(review) + "\n", encoding="utf-8")
    pdf = _export(out)
    return {"lecture": lecture.title, "sections": len(sections), "issues": len(issues), "flagged": len(flagged),
            "notes": used_notes, "output": str(out), "pdf": pdf}


def _place_figures(text: str, part: list[Slide], lecture: Lecture, out: Path, issues: list[str]) -> str:
    by_n = {s.n: s for s in part}

    def image(m: re.Match) -> str:
        s = by_n.get(int(m[1]))
        if not s or not s.frame:
            issues.append(f"marker for slide {m[1]} has no image in this section")
            return ""
        src = lecture.folder / s.frame
        (out / "assets").mkdir(exist_ok=True)
        shutil.copy2(src, out / "assets" / src.name)
        return f"![Slide {s.n}, {s.start}](assets/{src.name})"

    return re.sub(r"\[\[slide (\d+)\]\]", image, text)


CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
PDF_CSS = """<style>
body { max-width: none; font: 11pt/1.45 Georgia, serif; }
img { max-width: 100%; display: block; margin: 1em auto; }
table { border-collapse: collapse; margin: 1em 0; }
th, td { border: 1px solid #bbb; padding: 3px 8px; }
blockquote { border-left: 3px solid #bbb; margin-left: 0; padding-left: 1em; color: #333; }
h2 { break-after: avoid; } img, table { break-inside: avoid; }
</style>
"""


def _export(out: Path) -> str | None:
    """Pandoc writes MathML and Chrome prints it; WeasyPrint does not render MathML."""
    if not shutil.which("pandoc") or not CHROME.exists():
        return None
    (out / ".style.html").write_text(PDF_CSS, encoding="utf-8")
    r = subprocess.run(["pandoc", "handbook.md", "-s", "--mathml", "--metadata", "pagetitle=Handbook",
                        "-H", ".style.html", "-o", ".print.html"], cwd=out, capture_output=True, text=True)
    if r.returncode == 0:
        r = subprocess.run([str(CHROME), "--headless", "--disable-gpu", "--no-pdf-header-footer",
                            f"--print-to-pdf={out / 'handbook.pdf'}", (out / ".print.html").as_uri()],
                           capture_output=True, text=True)
    (out / ".style.html").unlink(missing_ok=True)
    (out / ".print.html").unlink(missing_ok=True)
    if r.returncode != 0 or not (out / "handbook.pdf").exists():
        log.warning("pdf export failed: %s", r.stderr.strip()[-300:])
        return None
    return str(out / "handbook.pdf")


def _select(root: Path, input_dir: Path | None, only: list[str] | None):
    """Ingest what is new, then return the config, the chosen lectures and the course materials."""
    overrides = {"library": {"inbox": str(input_dir)}} if input_dir else None
    process(root, only_lectures=only, overrides=overrides)   # course materials are always ingested
    ctx, _ = open_library(root, overrides)
    entries = Manifest(ctx.layout.manifest).entries
    prefix = f"{input_dir.name}/" if input_dir else ""
    mine = {k: e for k, e in entries.items() if e.get("status") == "done" and k.startswith(prefix)}
    lectures, materials = [], []
    for key, e in sorted(mine.items(), key=lambda kv: lecture_order(kv[0])):
        md = ctx.layout.source_md(e.get("source_id", ""), e)
        if not md.exists():
            continue
        if e.get("ingestor") == "media":
            if not only or any(o.casefold() in key.casefold() for o in only):
                lectures.append((key, e, md))
        else:
            text = md.read_text(encoding="utf-8").split("---", 2)[-1]
            materials.append((key, text, e["source_id"], md, ctx.layout.digest_md(e["source_id"], e)))
    if not lectures:
        raise SystemExit(f"no processed lecture matches {only}" if only else "no processed lectures")
    return ctx, lectures, materials


def make(root: Path, output: Path, *, input_dir: Path | None = None, only: list[str] | None = None) -> list[dict]:
    """Ingest what is new under input_dir, then write one handbook per lecture into output."""
    ctx, lectures, materials = _select(root, input_dir, only)
    provider = get_vision_provider({**ctx.config["frame_selection"], **ctx.config["handbook"]}, ctx.config)
    progress.start(ctx.work_dir, len(lectures))
    results = []
    for n, (key, e, md) in enumerate(lectures, 1):
        progress.item(n, f"handbook: {key}")
        lecture = parse_source(md, e["source_id"], key)
        results.append(write_lecture(lecture, [m[:2] for m in materials], provider, ctx.config, Path(output) / slugify(lecture.title)))
        progress.item_done()
    release_all()
    progress.finish()
    return results


def prepare(root: Path, output: Path, *, input_dir: Path | None = None, only: list[str] | None = None) -> list[str]:
    """Write a brief per lecture for an agent to write the book from, instead of a local model."""
    _, lectures, materials = _select(root, input_dir, only)
    briefs = []
    for key, e, md in lectures:
        lecture = parse_source(md, e["source_id"], key)
        out = Path(output) / slugify(lecture.title)
        out.mkdir(parents=True, exist_ok=True)
        (out / ".lecture.json").write_text(json.dumps({"folder": str(md), "source_id": e["source_id"], "key": key}))
        (out / "brief.md").write_text(brief(lecture, materials), encoding="utf-8")
        briefs.append(str(out / "brief.md"))
    return briefs


def brief(lecture: Lecture, materials: list[tuple[str, str]]) -> str:
    lines = [f"# Brief: {lecture.title}", "", BRIEF_RULES, "", "## Slides", ""]
    for s in lecture.slides:
        lines += [f"### Slide {s.n} ({s.start}–{s.end})", "", f"Image: {lecture.folder / s.frame}"]
        if s.flagged:
            lines.append("OCR check: FLAGGED, read the image")
        if s.from_ocr:
            lines.append("The model's reading failed: this text is raw OCR, read the image")
        lines += ["", s.material(), ""]
        if s.ocr:
            lines += [f"Classic OCR: {s.ocr}", ""]
    passages = relevant_passages("\n".join(s.material() for s in lecture.slides), [m[:2] for m in materials], top=6)
    lines += ["## Course-material passages", ""]
    lines += [f"From {t}:\n{c}\n" for t, c in passages] or ["None."]
    lines += ["## All course materials", "", "Search these with `rg -i \"<term>\" <path>`.", ""]
    for t, _, sid, md, digest in materials:
        lines.append(f"- {t} [@{sid}]: {md}" + (f"\n  Digest (find pages here, then read and cite the book): {digest}"
                                               if digest.exists() else ""))
    if not materials:
        lines.append("None.")
    return "\n".join(lines) + "\n"


def finish(output: Path) -> list[dict]:
    """Check each draft.md under output, then write handbook.md, review.md and the PDF."""
    results = []
    for meta in sorted(Path(output).glob("*/.lecture.json")):
        out = meta.parent
        if (out / "draft.md").exists():
            results.append(finish_lecture(out, json.loads(meta.read_text())))
    return results


def finish_lecture(out: Path, meta: dict) -> dict:
    folder = Path(meta["folder"])
    lecture = parse_source(folder, meta["source_id"], meta["key"])
    draft = (out / "draft.md").read_text(encoding="utf-8")
    grounds = (out / "brief.md").read_text(encoding="utf-8")
    issues = [f"numbers no source supports: {', '.join(bad)}" for bad in [unsupported_numbers(draft, grounds)] if bad]
    if ragged_tables(draft):
        issues.append("a table has rows of different lengths")
    shown = draft + "\n".join(s.text for s in lecture.slides if f"[[slide {s.n}]]" in draft)   # a later build of the slide
    issues += [f"slide {s.n}: table numbers not in the text: {', '.join(lost)}" for s in lecture.slides
               if f"[[slide {s.n}]]" not in draft and (lost := missing_table_numbers(shown, s))]
    (out / "handbook.md").write_text(_place_figures(draft, lecture.slides, lecture, out, issues), encoding="utf-8")
    flagged = [s for s in lecture.slides if s.flagged]
    corrections = out / "corrections.md"
    review = [f"# Review: {lecture.title}", "", f"Source: {lecture.source_id}", "", "## To check", ""]
    review += [f"- {x}" for x in issues] or ["None."]
    review += ["", "## Corrections made from the images", "",
               corrections.read_text(encoding="utf-8").strip() if corrections.exists() else "None."]
    review += ["", "## Slides the OCR check flagged", ""]
    review += [f"- Slide {s.n} ({s.start})" for s in flagged] or ["None."]
    (out / "review.md").write_text("\n".join(review) + "\n", encoding="utf-8")
    return {"lecture": lecture.title, "issues": len(issues), "flagged": len(flagged),
            "output": str(out), "pdf": _export(out)}


def lecture_order(key: str) -> tuple:
    """Dated names first by date, then natural order: lecture 2 before lecture 10."""
    m = re.search(r"(\d{4})[-_.](\d{1,2})[-_.](\d{1,2})", key)
    d = re.search(r"(?<!\d)(\d{1,2})[-_.](\d{1,2})[-_.](\d{4})(?!\d)", key)
    date = (int(m[1]), int(m[2]), int(m[3])) if m else (int(d[3]), int(d[2]), int(d[1])) if d else (9999, 0, 0)
    return date, [int(p) if p.isdigit() else p.casefold() for p in re.split(r"(\d+)", key)]
