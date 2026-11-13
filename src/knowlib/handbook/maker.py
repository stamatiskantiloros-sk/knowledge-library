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
import html
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
from .. import privacy
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

BRIEF_RULES = """You are writing the study book of one recorded lecture: a synthesis of what was taught, for a student who will read it instead of attending. It follows the order in which the lecturer built the subject, but it is organised by idea, not by slide. The slides and what was said, below, are your sources, not your outline.

What the book is
1. Read everything below first: every slide, everything said, the course-material passages. Then write draft.md beside this brief: a # title, then ## sections, one per idea or topic, in the order the lecture develops them. Slide boundaries decide nothing: a section can draw on several slides and minutes of speech, or on a single remark.
2. Synthesise. Say what was taught as clearly and briefly as it can be said: its definitions, examples, reasoning and numbers. Merge what was said in several places, and make the connections between ideas explicit where the lecture implies them. Choose the form that serves understanding (a definition, a short worked example, a comparison table, a line on why it matters), not the form of the slide. Each idea appears once.
3. Teach only what the lecture taught. No summaries of your own, no links ahead, no comments on the method; attribute to the lecturer only what the transcript says. "What it shows" is an automatic description of a slide, never a source of claims.

Slides: text or picture, never both
4. Show a slide (write [[slide N]] on its own line) only where its picture carries what words cannot do as well: a model, a chart, a process or flow diagram, a table of data. The text around it then adds only what the picture does not say: the point, and what the lecturer said about it. A slide that is a list, a definition or a statement is written into the text and not shown.
5. Show each slide at its clearest: look at the slides before and after it, and pick the step where everything is visible and nothing covers it (not a dimmed build or a slide with an overlay); where a later slide repeats an earlier one and adds to it, show only the later one.
6. Cite the slides. After a definition, figure or fact taken from a slide, add a short "(slide N)", sparingly: once per fact, not after every sentence. End each section with its sources in italics on a line of their own: *Slides 10–13*. Never write "on the slide", "the slide says" or "Slide:": the citation and the arrangement show where things come from.
7. Open the image of every flagged slide, every slide with a table, formula, chart or diagram, and every slide whose text looks wrong. The image decides. List each correction in corrections.md: slide, what the text said, what the image shows.

Accuracy
8. Copy every number, formula and table exactly. Do not compute, count or total anything that was not shown or said. Use LaTeX for formulas and Markdown tables with the same number of cells in every row.
9. State things exactly as strongly as the source does: keep its hedges and its wording of degree ("that's a problem", not "it fails"; "can", "often", "may"). Do not upgrade an example into a rule or a reason into a definition.
10. Keep the lecturer's scope and qualifiers. "In general X, but not when Y" stays that, never "not X". A remark about one setting (personal life, one company, one example) is not stretched to another (business, every company). A number stays with what it was said about: "7 or 8 of 10 decisions in personal life involve money" does not become a claim about decisions at work.
11. Where the lecturer contradicts a slide, a course document or something said earlier (a grade weight that differs from the syllabus), give both, each with its source, and point the student to the document. Do not silently pick one.
12. Leave out what is incomplete: a name, reference or point the recording lost or garbled is dropped, not reported with a note about the gap.
13. If the lecturer says something wrong, keep it and add a note saying so, plainly.
14. Add knowledge from outside the lecture only where a student would otherwise trip: a line starting "> **Note (not from class):**", one or two sentences, naming its source; for a book, also its page, from the `<!-- page N (pdf M) -->` line above the passage (N is the printed page). The passages below are the closest matches only: search all the course materials, listed at the end, to find and verify a note.
15. A slide marked "Not discussed in the recording" was not taught in what was recorded: leave it out. Where a section cannot make sense without it, say in one line that it was shown but not discussed.

People
16. Call the speaker "the lecturer", without he or she.
17. Teach what the lecturer taught; mute the class. Leave out students' questions, answers, suggestions and comments, and class rounds of guesses. Keep a point only where the lecturer states or develops it, and then as the lecturer's point.
18. Name no one in the room. Students, staff and guests are "a student", "a colleague"; never write a name, email, employer or other personal detail of anyone there, even where the transcript or a slide shows one ([a person] marks a name taken out). Companies, organisations and public figures the lecture names are named.

Style
19. Be condensed: terse bullets, one idea each, fragments where they read well, short plain sentences elsewhere. No restating, no connective padding, no anecdotes or class chatter that carry no idea. Course admin only as far as a student must know it: grading, exam format and allowed aids, required readings, textbooks in one line. As a guide, about a third of the transcript's length or less (roughly 30 words per minute of lecture).
20. Be simple, focused and precise. Name what the lecture names: companies, products, places, years and numbers; never blur them into stand-ins ("a truck maker", "big customers"). Plain words a student understands without decoding: no shorthand labels or symbols in place of words ("Pick:", "=" for "means").


Check before finishing
21. Check the draft claim by claim against its sources: for each claim find the transcript passage (its time) or the slide it comes from and read it again. Fix or remove anything the passage does not say, or says less strongly, more narrowly or about something else. Check every number with what it was said about, and every place the lecturer and a document disagree (rule 11).
22. The finish step lists, under "To check" in review.md, sentences with absolute words (always, never, every, only, not...) whose source passage hedges. Reread each passage and fix the sentence, or leave it if the lecturer said it that strongly, then finish again.

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
    discussed: bool = True

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
    date: str = ""                       # when the lecture took place (original_date, from the recording)


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
    lecture = Lecture(source_id or folder.name, key, str(meta.get("title", folder.name)), folder,
                      date=str(meta.get("original_date") or "")[:10])
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
            discussed="*Not discussed in the recording.*" not in b,
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


def approved_figure(out: Path, n: int) -> tuple[Path, dict, dict] | None:
    """The figure folder (`knowlib figures`) for slide n with a verdict, if there is one: (folder, brief, verdict)."""
    root = out / "figures"
    if not root.is_dir():
        return None
    for d in sorted(root.iterdir()):
        b, v = d / "brief.json", d / "verdict.json"
        if b.exists() and v.exists():
            brief = json.loads(b.read_text(encoding="utf-8"))
            if brief.get("slide") == n:
                return d, brief, json.loads(v.read_text(encoding="utf-8"))
    return None


def _figure_mm(d: Path) -> float | None:
    """A drawn figure's printed width in mm, from its PDF (Typst draws at true size)."""
    if not (d / "fig.pdf").exists():
        return None
    import pymupdf
    with pymupdf.open(d / "fig.pdf") as doc:
        return doc[0].rect.width * 25.4 / 72


def figure_words(pdf: Path) -> list[dict]:
    """Each line of text in a figure's PDF with its box as a share of the figure (x, y, w in %) and its size
    in pt (Typst draws at true size, and the figure is placed at that size). Typst's PDF has real text; its
    SVG has only shapes."""
    import pymupdf
    out = []
    with pymupdf.open(pdf) as doc:
        page = doc[0]
        w, h = page.rect.width, page.rect.height
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    if span["text"].strip():
                        x0, y0, x1, _ = span["bbox"]
                        out.append({"t": span["text"], "x": round(x0 / w * 100, 3), "y": round(y0 / h * 100, 3),
                                    "w": round((x1 - x0) / w * 100, 3), "s": round(span["size"], 2)})
    return out


def with_text_layers(page: str, out: Path) -> str:
    """Wrap each placed SVG figure that has a `.text.json` beside it with its words as transparent text at
    the same places: the figure stays vector and looks the same, and the PDF can be searched and copied."""
    def wrap(m: re.Match) -> str:
        tag, stem = m[0], m[1]
        words = out / "assets" / f"{stem}.text.json"
        if not words.exists():
            return tag
        width = re.search(r'style="[^"]*width:\s*([\d.]+mm)', tag)
        img = re.sub(r'\sstyle="[^"]*"', "", tag).replace("<img", '<img style="width:100%;display:block;margin:0"', 1)
        spans = "".join(
            f'<span style="position:absolute;left:{w["x"]}%;top:{w["y"]}%;font-size:{w["s"]}pt;'
            f'white-space:pre">{html.escape(w["t"])}</span>'
            for w in json.loads(words.read_text(encoding="utf-8")))
        # no container query units here: container-type changes how Chrome balances the columns
        box = f'position:relative;margin:0 auto;max-width:100%;width:{width[1] if width else "100%"}'
        layer = ('<span class="kl-fig-text" aria-hidden="true" style="position:absolute;inset:0;overflow:hidden;color:transparent;'
                 'line-height:1;font-family:Georgia,serif">' + spans + "</span>")
        return f'<div class="kl-fig-box" style="{box}">{img}{layer}</div>'
    return re.sub(r'<img\s[^>]*?src="assets/(fig_\d+)\.svg"[^>]*?/?>', wrap, page, flags=re.S)


def _place_figures(text: str, part: list[Slide], lecture: Lecture, out: Path, issues: list[str]) -> str:
    """[[slide N]] becomes the slide's image, or what `knowlib figures` made for it and the user approved:
    a redrawn figure at its printed size ("accept"), a table ("table"), or nothing ("drop")."""
    by_n = {s.n: s for s in part}

    def image(m: re.Match) -> str:
        s = by_n.get(int(m[1]))
        if not s or not s.frame:
            issues.append(f"marker for slide {m[1]} has no image in this section")
            return ""
        if found := approved_figure(out, s.n):
            d, brief, verdict = found
            kind = verdict.get("verdict")
            if kind == "drop":
                return ""
            if kind == "table" and (d / "table.md").exists():
                return (d / "table.md").read_text(encoding="utf-8").strip() + f"\n\nTable: Slide {s.n}, {s.start}"
            if kind == "accept" and (d / "fig.png").exists():
                (out / "assets").mkdir(exist_ok=True)
                name = f"fig_{s.n:03d}.png"
                shutil.copy2(d / "fig.png", out / "assets" / name)   # also what the privacy check reads
                if brief.get("tool") == "typst" and (d / "fig.svg").exists():
                    # vector: sharp at any zoom. Typst's SVG draws text as paths, so it needs no fonts
                    name = f"fig_{s.n:03d}.svg"
                    shutil.copy2(d / "fig.svg", out / "assets" / name)
                    if (d / "fig.pdf").exists():               # its words, so Ctrl+F finds them in the PDF
                        (out / "assets" / f"fig_{s.n:03d}.text.json").write_text(
                            json.dumps(figure_words(d / "fig.pdf"), ensure_ascii=False), encoding="utf-8")
                attrs = ".kl-fig .wide" if brief.get("width") == "wide" else ".kl-fig"
                if mm := _figure_mm(d):
                    attrs += f" width={mm:.1f}mm"
                return f"![Slide {s.n}, {s.start}](assets/{name}){{{attrs}}}"
            if kind != "keep-slide":
                issues.append(f"slide {s.n}: figure verdict {kind!r} has nothing to place; the slide is shown")
        src = lecture.folder / s.frame
        (out / "assets").mkdir(exist_ok=True)
        shutil.copy2(src, out / "assets" / src.name)
        return f"![Slide {s.n}, {s.start}](assets/{src.name})"

    return re.sub(r"\[\[slide (\d+)\]\]", image, text)


CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
PDF_CSS = """<style>
body { max-width: none; font: 11pt/1.45 Georgia, serif; }
img { max-width: 100%; display: block; margin: 1em auto; }
figure:has(img.wide) { column-span: all; } img.kl-fig { height: auto; }
table { border-collapse: collapse; margin: 1em 0; }
th, td { border: 1px solid #bbb; padding: 3px 8px; }
blockquote { border-left: 3px solid #bbb; margin-left: 0; padding-left: 1em; color: #333; }
h2 { break-after: avoid; } img, table { break-inside: avoid; }
.kl-byline { text-align: center; color: #555; font-style: italic; margin: 0 0 1.5em; }
.kl-subtitle { text-align: center; color: #555; font-style: italic; margin: -.4em 0 1.2em; }
.kl-sign-off, .kl-tagline { text-align: center; margin-top: 2em; color: #555; }
.kl-tagline { font-style: italic; margin-top: 0.5em; }
</style>
"""


MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")


def long_date(iso: str) -> str:
    """2026-11-10 -> 10 November 2026; anything else as it is."""
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", iso or "")
    return f"{int(m[3])} {MONTHS[int(m[2]) - 1]} {m[1]}" if m and 1 <= int(m[2]) <= 12 else (iso or "")


def pdf_name(title: str, date: str = "", course: str = "") -> str:
    """The PDF's file name from the lecture: 'Lecture 1 - Introduction to Operations Management - 2026-11-10.pdf'.
    The course goes first when the title does not already name it."""
    name = re.sub(r"\s*:\s*", " - ", title.strip()) or "handbook"
    if course and course.casefold() not in name.casefold():
        name = f"{course} - {name}"
    if date:
        name += f" - {date}"
    name = re.sub(r'[\\/*?"<>|]+', "", name)                 # characters file systems reject
    return re.sub(r"\s+", " ", name).strip()[:150] + ".pdf"


def _sign_last_page(pdf: Path, text: str | list, colour: str = "#8a8285", size: float = 7.2) -> None:
    """The signature, once, small, in the bottom right corner of the last page: right-aligned, on the line
    of the page number and just left of it (or at the right margin if there is none). Several lines stack
    upwards from there, the last on the page number's line and the later ones a little smaller."""
    import pymupdf
    italic = Path("/System/Library/Fonts/Supplemental/Georgia Italic.ttf")
    font = pymupdf.Font(fontfile=str(italic)) if italic.exists() else pymupdf.Font("tiri")
    lines = [str(x) for x in (text if isinstance(text, list) else [text]) if str(x).strip()]
    sizes = [size if i == 0 else size * 0.9 for i in range(len(lines))]
    rgb = tuple(int(colour.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
    with pymupdf.open(pdf) as doc:
        page = doc[-1]
        w, h = page.rect.width, page.rect.height
        foot = [wd for wd in page.get_text("words") if wd[1] > h - 18 * 72 / 25.4 and wd[4].isdigit()]
        if foot:
            num = max(foot, key=lambda wd: wd[2])
            right, base = num[0] - 3 * 72 / 25.4, num[3] - 1.4
        else:
            right, base = w - 15 * 72 / 25.4, h - 8 * 72 / 25.4
        y = base
        for line, s in reversed(list(zip(lines, sizes))):
            x = right - font.text_length(line, fontsize=s)
            page.insert_text((x, y), line, fontsize=s, fontname="kl-sign",
                             fontfile=str(italic) if italic.exists() else None, color=rgb)
            y -= s * 1.3
        doc.saveIncr() if doc.can_save_incrementally() else doc.save(str(pdf) + ".tmp")
    if Path(str(pdf) + ".tmp").exists():
        Path(str(pdf) + ".tmp").replace(pdf)


def _export(out: Path, cfg: dict | None = None, root: Path | None = None, *, title: str = "",
            date: str = "") -> str | None:
    """Pandoc writes MathML and Chrome prints it; WeasyPrint does not render MathML. The library's
    handbook.style_file replaces the default style; its signature goes in every page's footer (a page
    margin box, beside the page number) and, with the tagline, at the end."""
    if not shutil.which("pandoc") or not CHROME.exists():
        return None
    import html
    hb = (cfg or {}).get("handbook") or {}
    css = PDF_CSS
    if hb.get("style_file"):
        path = Path(hb["style_file"]).expanduser()
        path = path if path.is_absolute() else (root or out) / path
        if path.exists():
            css = path.read_text(encoding="utf-8")
            css = css if "<style" in css else f"<style>\n{css}\n</style>\n"
        else:
            log.warning("handbook.style_file %s not found; using the default style", path)
    from . import theme as look
    if look.chosen(cfg or {}):                             # the library's theme wins over its style file
        head, sep, tail = css.rpartition("</style>")
        css = head + look.css(look.chosen(cfg)) + sep + tail
    # page numbers, and the signature if any, in the page footer; first in the style so a library's
    # own @page rule can restyle them (fonts do not inherit into page margins)
    footer = "@page { @bottom-right { content: counter(page); font-size: 8pt; color: #8a7a80; }"
    if hb.get("signature"):
        sig = str(hb["signature"]).replace("\\", "").replace('"', "'")
        footer += ' @bottom-center { content: "' + sig + '"; font-size: 7.5pt; color: #8a7a80; }'
    css = re.sub(r"<style[^>]*>", lambda m: m.group(0) + "\n" + footer + " }\n", css, count=1)
    (out / ".style.html").write_text(css, encoding="utf-8")
    before = ""
    after = (f'<div class="kl-sign-off">{html.escape(hb["signature"])}</div>\n' if hb.get("signature") else "") + \
            (f'<div class="kl-tagline">{html.escape(hb["tagline"])}</div>\n' if hb.get("tagline") else "")
    (out / ".before.html").write_text(before, encoding="utf-8")
    (out / ".after.html").write_text(after, encoding="utf-8")
    lang = {"english": "en", "greek": "el"}.get(str(hb.get("language", "English")).lower(), "en")
    source = "handbook.md"
    course = str(hb.get("course") or (root.name if root else "")).strip()
    lines = []                                             # under the title, in the PDF only
    if hb.get("subtitle", True) is not False:
        parts = [str(hb.get("subtitle") or "Lecture notes"), course, long_date(date)]
        lines.append(f'<p class="kl-subtitle">{html.escape(" · ".join(p for p in parts if p))}</p>')
    if hb.get("byline"):
        lines.append(f'<p class="kl-byline">{html.escape(str(hb["byline"]))}</p>')
    if lines:
        md = (out / "handbook.md").read_text(encoding="utf-8")
        block = "\n" + "\n".join(lines) + "\n"
        md = re.sub(r"^(# .+)$", lambda m: m.group(1) + "\n" + block, md, count=1, flags=re.M)
        (out / ".print.md").write_text(md, encoding="utf-8")
        source = ".print.md"
    name = pdf_name(title, date, course) if title else "handbook.pdf"
    pdf = out / name
    r = subprocess.run(["pandoc", source, "-s", "--math-method=mathml", "--metadata", f"pagetitle={title or 'Handbook'}",
                        "--metadata", f"title-meta={title or 'Handbook'}",
                        "--metadata", f"lang={lang}",                    # lets Chrome hyphenate justified text
                        "-H", ".style.html", "-B", ".before.html", "-A", ".after.html", "-o", ".print.html"],
                       cwd=out, capture_output=True, text=True)
    (out / ".before.html").unlink(missing_ok=True)
    (out / ".after.html").unlink(missing_ok=True)
    (out / ".print.md").unlink(missing_ok=True)
    if r.returncode == 0:
        page = out / ".print.html"
        page.write_text(with_text_layers(page.read_text(encoding="utf-8"), out), encoding="utf-8")
        r = subprocess.run([str(CHROME), "--headless", "--disable-gpu", "--no-pdf-header-footer",
                            f"--print-to-pdf={pdf}", (out / ".print.html").as_uri()],
                           capture_output=True, text=True)
    (out / ".style.html").unlink(missing_ok=True)
    (out / ".print.html").unlink(missing_ok=True)
    if r.returncode != 0 or not pdf.exists():
        log.warning("pdf export failed: %s", r.stderr.strip()[-300:])
        return None
    drop_blank_last_pages(pdf)
    import pymupdf
    with pymupdf.open(pdf) as doc:                         # the title readers and file browsers show
        doc.set_metadata({**(doc.metadata or {}), "title": title or "Handbook", "subject": long_date(date)})
        doc.saveIncr() if doc.can_save_incrementally() else doc.save(str(pdf) + ".tmp")
    if Path(str(pdf) + ".tmp").exists():
        Path(str(pdf) + ".tmp").replace(pdf)
    if hb.get("last_page_signature"):
        sig = hb["last_page_signature"]
        _sign_last_page(pdf, sig if isinstance(sig, list) else str(sig),
                        size=float(hb.get("last_page_signature_size") or 7.2))
    for old in out.glob("*.pdf"):                         # earlier builds under another name
        if old != pdf and (old.name == "handbook.pdf" or old.name.startswith(pdf.name.rsplit(" - ", 1)[0])):
            old.unlink()
    return str(pdf)


def drop_blank_last_pages(pdf: Path) -> int:
    """Chrome adds an empty page when two columns end exactly at the foot of the last page. Remove trailing
    pages that hold nothing but the page number (no other text, no drawings, no images); return how many."""
    import pymupdf
    with pymupdf.open(pdf) as doc:
        n = 0
        while len(doc) > 1:
            page = doc[-1]
            if page.get_text().strip().isdigit() and not page.get_drawings() and not page.get_images():
                doc.delete_page(-1)
                n += 1
            else:
                break
        tmp = pdf.with_suffix(".tmp.pdf")
        if n:
            doc.save(tmp, garbage=3, deflate=True)
    if n:
        tmp.replace(pdf)
    return n


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
    ctx, lectures, materials = _select(root, input_dir, only)
    briefs = []
    for key, e, md in lectures:
        lecture = parse_source(md, e["source_id"], key)
        out = Path(output) / slugify(lecture.title)
        out.mkdir(parents=True, exist_ok=True)
        (out / ".lecture.json").write_text(json.dumps({"folder": str(md), "source_id": e["source_id"], "key": key}))
        (out / "brief.md").write_text(brief(lecture, materials, notes_dir=ctx.layout.notes,
                                            names=privacy.names(ctx.config),
                                            speaker=(ctx.config.get("handbook") or {}).get("speaker") or "the lecturer"),
                                      encoding="utf-8")
        briefs.append(str(out / "brief.md"))
    return briefs


def brief(lecture: Lecture, materials: list[tuple[str, str]], *, notes_dir: Path | None = None,
          names: list[str] | None = None, speaker: str = "the lecturer") -> str:
    rules = BRIEF_RULES.replace("the lecturer", speaker).replace("The lecturer", speaker[:1].upper() + speaker[1:])
    lines = [f"# Brief: {lecture.title}", "", rules, "", "## Slides", ""]
    for s in lecture.slides:
        lines += [f"### Slide {s.n} ({s.start}–{s.end})", "", f"Image: {lecture.folder / s.frame}"]
        if not s.discussed:
            lines.append("Not discussed in the recording")
        if s.flagged:
            lines.append("OCR check: FLAGGED, read the image")
        if s.from_ocr:
            lines.append("The model's reading failed: this text is raw OCR, read the image")
        lines += ["", s.material(), ""]
        if s.ocr:
            lines += [f"Classic OCR: {s.ocr}", ""]
    others = [m for m in materials if Path(m[3]).parent != Path(lecture.folder)]   # its own deck is above already
    passages = relevant_passages("\n".join(s.material() for s in lecture.slides), [m[:2] for m in others], top=6)
    lines += ["## Course-material passages", ""]
    lines += [f"From {t}:\n{c}\n" for t, c in passages] or ["None."]
    lines += ["## All course materials", "", "Search these with `rg -i \"<term>\" <path>`.", ""]
    for t, _, sid, md, digest in materials:
        lines.append(f"- {t} [@{sid}]: {md}" + (f"\n  Digest (find pages here, then read and cite the book): {digest}"
                                               if digest.exists() else ""))
    if not materials:
        lines.append("None.")
    if notes_dir and Path(notes_dir).is_dir():
        lines += ["", f"Topic notes built from these materials, every claim with its page: {notes_dir} "
                  f"(map: {Path(notes_dir).parent / 'INDEX.md'}). Use them to find the pages; read the page "
                  "and cite the book, never a note."]
    text = "\n".join(lines) + "\n"
    return privacy.redact(text, names) if names is not None else text


def finish(output: Path) -> list[dict]:
    """Check each draft.md under output, then write handbook.md, review.md and the PDF."""
    output = Path(output).expanduser().resolve()           # the PDF export needs absolute file URLs
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
    issues += dropped_hedges(draft, lecture.slides)
    cfg = _library_config(folder)
    hb = cfg.get("handbook") or {}
    book = _place_figures(draft, lecture.slides, lecture, out, issues)
    if not hb.get("show_times", True):
        book = without_times(book)
    (out / "handbook.md").write_text(book, encoding="utf-8")
    issues += personal_information(out, cfg, allow=[hb.get("signature"), hb.get("tagline"), hb.get("byline")])
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
            "output": str(out), "pdf": _export(out, cfg, _library_root(folder), title=lecture.title, date=lecture.date)}


def _library_root(md: Path) -> Path | None:
    for d in [md.parent, *md.parents]:
        if (d / "_knowlib" / "config.yaml").exists():
            return d
    return None


def _library_config(md: Path) -> dict:
    """The config of the library the lecture belongs to (found by walking up to its _knowlib)."""
    from ..config import DEFAULTS, load_config
    root = _library_root(md)
    return load_config(root) if root else DEFAULTS


def without_times(text: str) -> str:
    """Slide captions and section endings without recording times: 'Slide 2' not 'Slide 2, 08:16'."""
    text = re.sub(r"(!\[Slide \d+), [\d:]+\]", r"\1]", text)
    text = re.sub(r"^(Slide \d+), [\d:]+$", r"\1", text, flags=re.M)
    text = re.sub(r"^(Table: Slide \d+), [\d:]+$", r"\1", text, flags=re.M)
    time = r"\d{1,2}:\d{2}(?::\d{2})?"                    # 08:16 or 1:30:30; a slide range has no colon
    return re.sub(rf"(\*Slides? [\d–, -]+?), {time}–{time}", r"\1", text)


ABSOLUTE = re.compile(r"\b(always|never|every\w*|all|none|no one|nothing|only|must|cannot|can't|impossible|"
                      r"entirely|completely|certainly|guaranteed|(?:is|are|was|were|does|do) not|isn't|aren't|doesn't|don't)\b", re.I)
HEDGE = re.compile(r"\b(in general|generally|usually|often|mostly|most|may|might|can|could|sometimes|tends?|"
                   r"typically|probably|likely|some|many|not always|except|unless|but|maybe|perhaps|almost|"
                   r"roughly|about|around|if|when)\b", re.I)


def _slides_of(section: str) -> set[int]:
    """The slides a draft section cites: [[slide N]], (slide N) and its *Slides 10–13, 16* ending."""
    cited = {int(a or b) for a, b in re.findall(r"\[\[slide (\d+)\]\]|\(slides? (\d+)", section, re.I)}
    for line in re.findall(r"^\*Slides? ([^*]+)\*", section, re.M):
        line = re.sub(r"\d{1,2}:\d{2}(?::\d{2})?", "", line)          # recording times, not slides
        for a, b in re.findall(r"(\d+)(?:\s*[–-]\s*(\d+))?", line):
            cited |= set(range(int(a), int(b or a) + 1))
    return cited


def dropped_hedges(draft: str, slides: list[Slide]) -> list[str]:
    """Sentences of the draft with an absolute word (always, never, every, not...) whose closest source
    sentence hedges and does not use that word: a scope or qualifier the draft may have lost. A cheap,
    noisy check: each is a pointer for the writer to reread the passage, not a finding."""
    by_n = {s.n: s for s in slides}
    out = []
    for section in re.split(r"^## ", draft, flags=re.M)[1:]:
        cited = [by_n[n] for n in sorted(_slides_of(section)) if n in by_n] or slides
        source = []                                       # (where, sentence)
        for s in cited:
            for line in f"{s.text}\n{s.spoken}".splitlines():
                t = re.match(r"\s*\[([\d:]+)\]", line)
                where = f"slide {s.n}, {t[1]}" if t else f"slide {s.n}"
                source += [(where, x.strip()) for x in re.split(r"(?<=[.!?])\s+", re.sub(r"^\s*\[[\d:]+\]\s*", "", line)) if x.strip()]
        for line in section.splitlines()[1:]:
            if line.startswith(("> ", "|", "*Slide", "[[", "![")):
                continue
            for sent in re.split(r"(?<=[.!?])\s+", line.replace("**", "").lstrip("-* ").strip()):
                word = ABSOLUTE.search(sent)
                if not word or HEDGE.search(sent):
                    continue
                words = {w for w in re.findall(r"\w{5,}", sent.casefold())}
                best = max(source, key=lambda ws: len(words & set(re.findall(r"\w{5,}", ws[1].casefold()))), default=None)
                if not best or len(words & set(re.findall(r"\w{5,}", best[1].casefold()))) < 2:
                    continue
                if HEDGE.search(best[1]) and not re.search(rf"\b{re.escape(word[1])}\b", best[1], re.I):
                    out.append(f"absolute claim, hedged source: \"{sent[:200]}\" ({best[0]}: \"{best[1][:200]}\"): "
                               "reread the passage and keep its scope and qualifiers")
    return out


def personal_information(out: Path, cfg: dict, allow: list | None = None) -> list[str]:
    """Personal information in the finished handbook: emails, phone numbers and privacy.names, in its
    text and, read by classic OCR on this machine, in every image it shows. A handbook with any is not
    ready to share."""
    names = privacy.names(cfg)
    issues = []
    text = (out / "handbook.md").read_text(encoding="utf-8")
    checked = text
    for a in allow or []:                                  # the author's own signature, declared in the config
        if a:
            checked = checked.replace(str(a), "")
    if why := privacy.reasons(checked, names):
        issues.append(f"personal information in the text ({', '.join(why)}): remove it")
    from ..media.ocr import apple_vision
    for img in sorted(set(re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text))):
        path = out / img
        if path.suffix == ".svg" and path.with_suffix(".png").exists():
            path = path.with_suffix(".png")                # OCR reads the same drawing as an image
        if not path.exists():
            continue
        try:
            seen = apple_vision(path, cfg.get("ocr_crosscheck") or {})
        except Exception as e:  # noqa: BLE001 - an image that cannot be read is checked by hand
            issues.append(f"{img}: could not be read to check for personal information ({e}): look at it")
            continue
        if why := privacy.reasons(seen, names):
            issues.append(f"{img} shows personal information ({', '.join(why)}): leave the image out or crop it")
    return issues


def lecture_order(key: str) -> tuple:
    """Dated names first by date, then natural order: lecture 2 before lecture 10."""
    m = re.search(r"(\d{4})[-_.](\d{1,2})[-_.](\d{1,2})", key)
    d = re.search(r"(?<!\d)(\d{1,2})[-_.](\d{1,2})[-_.](\d{4})(?!\d)", key)
    date = (int(m[1]), int(m[2]), int(m[3])) if m else (int(d[3]), int(d[2]), int(d[1])) if d else (9999, 0, 0)
    return date, [int(p) if p.isdigit() else p.casefold() for p in re.split(r"(\d+)", key)]
