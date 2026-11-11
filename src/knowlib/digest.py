"""Book digests: a finding aid for a PDF too long to read whole, written once and reused by every
handbook that cites the book.

The contents and the list of tables and figures come from the PDF itself (assets/outline.json,
written at ingestion), so their page numbers are exact. A model reads one chapter at a time for
what the PDF cannot give: key terms, formulas and worked examples. The writer uses the digest to
find pages, then reads them in source.md and cites the book, never the digest.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

from . import progress
from .contract import now_iso, read_frontmatter
from .handbook.maker import Cache
from .ingestors.pdf import PAGE_RE
from .manifest import Manifest
from .providers import get_vision_provider
from .providers.agent import AgentPending, stop_if_waiting
from .providers.openai_compat import NotEnoughMemory
from .runner import open_library

log = logging.getLogger("knowlib")

DIGEST_PROMPT = """Below is {where} of the textbook "{title}". Each page begins with a line such as "=== Page 52 ===", the page number printed in the book; everything up to the next such line is on that page.

Write a digest that a writer will use to find the right pages, not to replace them. Use only what the text says. End every item with the page it is on, read from the nearest "=== Page" line above it.

Use exactly these headings:

### Key terms
- **Term** (p. N): what the book says it is, in one sentence close to its wording.

### Formulas
- **Name** (p. N): the formula as the book writes it, then what its symbols mean.

### Worked examples
- **Company or setting** (pp. N–M): what it shows and the main numbers it reaches.

Leave out tables, figures and section headings: they are listed separately. Write "(none)" under a heading with nothing in it.

Text:

{text}"""

SKIP = re.compile(r"^(cover|title page|copyright|dedication|about the authors?|acknowledg|"
                  r"(table of )?contents|index|eula|bibliography|references)\b", re.I)


def pages(source_md: Path) -> dict[int, tuple[str, str]]:
    """{pdf page: (printed page, text)} from the page markers in source.md."""
    body = source_md.read_text(encoding="utf-8").split("\n---\n", 1)[-1]
    parts = PAGE_RE.split(body)
    return {int(parts[i + 1]): (parts[i], parts[i + 2].strip()) for i in range(1, len(parts) - 2, 3)}


def parts(toc: list[dict], last_pdf: int, page_chars: dict[int, int], max_chars: int) -> list[dict]:
    """Chapters as page spans, from the top level of the outline. A chapter longer than max_chars
    is split at its next outline level, and a part that is still too long, by pages."""
    def spans(entries, end):
        return [(e, e["pdf"], (entries[i + 1]["pdf"] - 1) if i + 1 < len(entries) else end)
                for i, e in enumerate(entries)]

    def size(a, b):
        return sum(page_chars.get(p, 0) for p in range(a, b + 1))

    if not toc:
        return []
    top = min(e["level"] for e in toc)
    out = []
    for entry, a, b in spans([e for e in toc if e["level"] == top], last_pdf):
        if SKIP.match(entry["title"]) or b < a:
            continue
        if size(a, b) <= max_chars:
            out.append({"title": entry["title"], "first": a, "last": b})
            continue
        inner = [e for e in toc if e["level"] == top + 1 and a < e["pdf"] <= b]
        cuts = [a] + [e["pdf"] for e in inner] + [b + 1]
        group = a
        for i in range(1, len(cuts)):
            # close the group before the cut that would make it too long, unless that leaves a
            # scrap (a chapter's opening page); then the page split below cuts the long section
            if size(group, cuts[i] - 1) > max_chars and cuts[i - 1] > group \
                    and size(group, cuts[i - 1] - 1) > max_chars // 4:
                out.append({"title": entry["title"], "first": group, "last": cuts[i - 1] - 1})
                group = cuts[i - 1]
        out.append({"title": entry["title"], "first": group, "last": b})
    return [{**part, "first": a, "last": b}            # a single section longer than max_chars
            for part in out for a, b in by_pages(part["first"], part["last"], page_chars, max_chars)]


def by_pages(first: int, last: int, page_chars: dict[int, int], max_chars: int) -> list[tuple[int, int]]:
    """first..last cut into runs of whole pages, each at most max_chars where a page allows."""
    runs, a, size = [], first, 0
    for p in range(first, last + 1):
        if p > a and size + page_chars.get(p, 0) > max_chars:
            runs.append((a, p - 1))
            a, size = p, 0
        size += page_chars.get(p, 0)
    return runs + [(a, last)]


CITE_RE = re.compile(r"\bpp?\.\s*([0-9ivxlc]+)(?:\s*[–-]\s*([0-9ivxlc]+))?", re.I)


def mark_stray_pages(reply: str, labels: list[str]) -> str:
    """Mark with ⚠ each line citing a page that is not in the part the model was given."""
    allowed = {l.casefold() for l in labels}
    out = []
    for line in reply.splitlines():
        cited = [p for pair in CITE_RE.findall(line) for p in pair if p]
        out.append(line + (" ⚠" if any(p.casefold() not in allowed for p in cited) else ""))
    return "\n".join(out)


def drop_repeats(reply: str) -> tuple[str, bool]:
    """(reply without repeated item lines, whether it looped). A small model can repeat the same
    items until the token limit cuts it off, so the part's later pages may be missing."""
    seen, out, repeats = set(), [], 0
    for line in reply.splitlines():
        if line.startswith("- ") and line in seen:
            repeats += 1
            continue
        seen.add(line)
        out.append(line)
    if repeats >= 3 and out and out[-1].startswith("- **") and not out[-1].rstrip().endswith((".", ")", "⚠")):
        out.pop()                                      # the item the token limit cut off
    return "\n".join(out), repeats >= 3


def merge(replies: list[str]) -> str:
    """Replies for consecutive pieces of one part, combined under each heading in order."""
    heads: dict[str, list[str]] = {}
    for reply in replies:
        head = None
        for line in reply.splitlines():
            if line.startswith("### "):
                head = line
                heads.setdefault(head, [])
            elif head and line.strip() and line.strip() != "(none)":
                heads[head].append(line)
    return "\n\n".join(f"{h}\n" + ("\n".join(items) or "(none)") for h, items in heads.items())


BATCH_PROMPT = """Below are {n} separate tasks, each between its === TASK lines; each asks for the digest of one part of a book. Do every task exactly as it says, using only that task's own text.

Write the digests in task order. Begin each with a line holding only "=== DIGEST <task number> ===", then that digest with its three headings. Write nothing else.

{tasks}"""

HEADINGS = ("### Key terms", "### Formulas", "### Worked examples")
DIGEST_MARK = re.compile(r"^=== DIGEST (\d+) ===\s*$", re.M)


def digest_in_batches(prompts: list[str], provider, cache: Cache, dc: dict) -> int:
    """Ask a long-context API model for several parts' digests per request, up to its token budget,
    and cache each digest under that part's own prompt, so the per-part pass finds it. A part whose
    digest is missing, incomplete or cut off is left for the per-part pass. Returns how many were done."""
    from .providers.chain import estimate_tokens
    budget = provider.budget()
    if budget <= 0 or len(prompts) < 2:
        return 0
    most, out_cap = int(dc.get("batch_parts", 4)), int(dc.get("batch_max_tokens", 60000))
    batches, cur, size = [], [], 0
    for p in prompts:
        t = estimate_tokens(p) + 30
        if cur and (size + t > budget or len(cur) >= most):
            batches.append(cur)
            cur, size = [], 0
        cur.append(p)
        size += t
    batches.append(cur)
    done = 0
    for b, batch in enumerate(batches, 1):
        if len(batch) == 1:
            continue
        log.info("digest: batch %d/%d, %d parts", b, len(batches), len(batch))
        tasks = "\n\n".join(f"=== TASK {n} ===\n{p}\n=== END TASK {n} ===" for n, p in enumerate(batch, 1))
        try:
            reply = provider.generate([], BATCH_PROMPT.format(n=len(batch), tasks=tasks), max_tokens=out_cap)
        except (OSError, RuntimeError) as e:
            if isinstance(e, NotEnoughMemory):
                raise
            log.warning("digest batch %d failed (%s); its parts go one at a time", b, str(e)[:120])
            if "no model in the chain" in str(e):
                break
            continue
        cut = getattr(provider, "last_finish", None) == "length"
        marks = list(DIGEST_MARK.finditer(reply))
        for k, m in enumerate(marks):
            n = int(m.group(1))
            if not 1 <= n <= len(batch):
                continue
            last = k + 1 == len(marks)
            if last and cut:
                continue                               # the token limit cut this one off
            text = reply[m.end():marks[k + 1].start() if not last else len(reply)].strip()
            if all(h in text for h in HEADINGS):
                cache.put(cache.key(provider.answered_by or provider.model, batch[n - 1]), text)
                done += 1
    return done


def digest_book(source_md: Path, assets: Path, cfg: dict, provider, cache: Cache, fallback=None) -> tuple[str | None, int]:
    """(digest text, pending jobs). The text is None while session jobs are still unanswered.
    A part the provider repeats itself on is read again by the fallback provider, in smaller pieces,
    once the provider has finished the whole book, so the two models are never loaded together."""
    fm = read_frontmatter(source_md)
    book = json.loads((assets / "outline.json").read_text(encoding="utf-8"))
    pg = pages(source_md)
    dc = cfg["digest"]
    page_chars = {p: len(t) for p, (_, t) in pg.items()}
    chunks = parts(book["toc"], max(pg) if pg else 0, page_chars, int(dc["max_chars"]))
    label = lambda p: pg.get(p, (str(p), ""))[0]
    tokens = int(dc.get("max_tokens", 8000))

    def prompt_for(i: int, first: int, last: int) -> str:
        # the prompt depends only on its own pages, so re-splitting the book keeps the other parts cached
        span = range(first, last + 1)
        where = f"a part (\"{chunks[i]['title']}\", pages {label(first)}–{label(last)})"
        text = "\n\n".join(f"=== Page {label(p)} ===\n{pg.get(p, ('', ''))[1]}" for p in span)
        return DIGEST_PROMPT.format(where=where, title=fm.get("title", ""), text=text)

    def ask(model, i: int, first: int, last: int) -> tuple[str, bool]:
        reply, cut = cache.ask_checked(model, prompt_for(i, first, last), tokens, retries=int(dc.get("retries", 1)))
        reply, looped = drop_repeats(reply.strip())
        lines = reply.splitlines()
        if cut and lines and not lines[-1].rstrip().endswith((".", ")", "⚠", "(none)")):
            reply = "\n".join(lines[:-1])             # the item the token limit cut off
        return reply, looped or cut

    if hasattr(provider, "budget") and dc.get("batch", True):
        todo = [prompt_for(i, part["first"], part["last"]) for i, part in enumerate(chunks)]
        digest_in_batches([p for p in todo if cache.lookup(provider, p) is None], provider, cache, dc)
    replies: dict[int, tuple[str, bool]] = {}
    pending = 0
    for i, part in enumerate(chunks):
        progress.stage("digest parts", i, len(chunks))
        try:
            replies[i] = ask(provider, i, part["first"], part["last"])
        except AgentPending:
            pending += 1
    reread = {}
    progress.stage("digest parts", len(chunks), len(chunks))
    looped = [i for i, (_, loop) in replies.items() if loop]
    reader = fallback or provider                      # without a fallback, the same model reads it in pieces
    if looped and not pending:
        if fallback:
            provider.close()                           # free the memory before the larger model loads
        for n, i in enumerate(looped):
            progress.stage("rereading looped parts", n, len(looped))
            first, last = chunks[i]["first"], chunks[i]["last"]
            pieces = by_pages(first, last, page_chars, int(dc.get("fallback_max_chars", 60000)))
            if len(pieces) == 1:                       # whole again could be the very request that failed
                half = -(-sum(page_chars.get(p, 0) for p in range(first, last + 1)) // 2)
                pieces = by_pages(first, last, page_chars, half)
            got = []
            for a, b in pieces:
                try:
                    got.append(ask(reader, i, a, b))
                except AgentPending:
                    pending += 1
                except NotEnoughMemory:
                    raise
                except (OSError, RuntimeError) as e:   # could not load, or the server stalled
                    log.warning("rereading part %d failed (%s); keeping the first reply", i + 1, e)
                    break
            if len(got) < len(pieces):
                continue
            replies[i] = (merge([t for t, _ in got]), any(loop for _, loop in got))
            reread[i] = len(pieces)
        progress.stage("rereading looped parts", len(looped), len(looped))
    if pending:
        return None, pending

    by = (f"{provider.model} one part at a time" if provider.backend != "chain" else
          f"API models in a chain ({', '.join(provider.cache_models)}), several parts per request where they fit")
    if reread:
        by += f"; the {len(reread)} parts it repeated itself on or ran out of tokens in were read again by {reader.model}"
    out = ["# Digest: " + fm.get("title", source_md.stem), "",
           f"A finding aid for this book. Find the pages here, then read them in `{source_md}`, where each "
           "page starts with `<!-- page N (pdf M) -->`; cite the book and its page, never this digest. The contents "
           f"and the tables and figures come from the PDF itself. The rest was written by {by}; its pages can be "
           "off by one, and items marked ⚠ cite a page outside the part.", "",
           "## Contents", ""]
    top = min((e["level"] for e in book["toc"]), default=1)
    out += [f"{'  ' * (e['level'] - top)}- {e['title']}, p. {e['page']}" for e in book["toc"]]
    out += ["", "## Tables and figures", ""]
    out += [f"- {c['kind']} {c['number']}: {c['title']}, p. {c['page']}" for c in book["captions"]] or ["None found."]
    for i, part in enumerate(chunks):
        labels = [label(p) for p in range(part["first"], part["last"] + 1)]
        reply, looped = replies[i]
        out += ["", f"## {part['title']} (pp. {labels[0]}–{labels[-1]})", ""]
        if i in reread:
            pieces = "whole" if reread[i] == 1 else f"in {reread[i]} pieces"
            out += [f"> Read again by {reader.model} ({pieces}) after {provider.model} repeated itself "
                    "or ran out of tokens.", ""]
        if looped:
            out += ["> The model repeated itself or ran out of tokens here and was cut off, so items from the "
                    "later pages of this part may be missing. Search the book for them.", ""]
        out.append(mark_stray_pages(reply, labels))
    return "\n".join(out) + "\n", 0


def is_book(source_md: Path, assets: Path, min_pages: int) -> bool:
    fm = read_frontmatter(source_md)
    return (assets / "outline.json").exists() and int((fm.get("stats") or {}).get("pages", 0)) >= min_pages


def run(root: Path, *, ids: list[str] | None = None, force: bool = False, overrides: dict | None = None) -> list[dict]:
    """Write each book's digest (sources/<id>/digest.md, or <file>.digest.md beside it in place)
    when it is missing or older than its source."""
    ctx, _ = open_library(root, overrides)
    cfg = ctx.config
    cfg.setdefault("agent_dir", str(ctx.work_dir / "agent"))
    dc = cfg["digest"]
    provider = get_vision_provider({**cfg["frame_selection"], **dc}, cfg)
    fallback = get_vision_provider({**cfg["frame_selection"], **dc, **dc["fallback_model"]}, cfg) \
        if dc.get("fallback_model") else None
    lay = ctx.layout
    entries = {e["source_id"]: e for e in Manifest(lay.manifest).entries.values() if e.get("source_id")}
    books = [(sid, md) for sid, md in sorted(lay.all_sources(Manifest(lay.manifest).entries).items())
             if (not ids or sid in ids) and is_book(md, lay.assets_dir(sid), int(dc["min_pages"]))]
    results = []
    progress.start(ctx.work_dir, len(books))
    try:
        for n, (sid, md) in enumerate(books, 1):
            progress.item(n, f"digest: {sid}")
            digest = lay.digest_md(sid, entries.get(sid))
            source_hash = hashlib.sha256(md.read_bytes()).hexdigest()[:16]
            if digest.exists() and not force and f"source_hash: {source_hash}" in digest.read_text(encoding="utf-8")[:400]:
                results.append({"source": sid, "status": "current", "digest": str(digest)})
                continue
            (ctx.work_dir / "digest").mkdir(parents=True, exist_ok=True)
            text, pending = digest_book(md, lay.assets_dir(sid), cfg, provider,
                                        Cache(ctx.work_dir / "digest" / f"{sid}.json"), fallback)
            if text is None:
                results.append({"source": sid, "status": "waiting", "pending": pending})
                continue
            # generated_by keeps a digest beside its book out of the crawl in a library in place
            head = (f"---\nsource: {sid}\nsource_hash: {source_hash}\nmodel: {provider.model}\nwritten: {now_iso()}\n"
                    f"generated_by: knowlib\n---\n\n")
            digest.write_text(head + text, encoding="utf-8")
            results.append({"source": sid, "status": "written", "digest": str(digest)})
            progress.item_done()
    finally:
        progress.finish()
        provider.close()
        if fallback:
            fallback.close()
    if any(r["status"] == "waiting" for r in results):
        stop_if_waiting(Path(cfg["agent_dir"]))
    return results
