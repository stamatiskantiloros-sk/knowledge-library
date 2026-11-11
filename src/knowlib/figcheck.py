"""A second vision model reads the figures where one reading is not enough to trust.

Classic OCR already checks each figure's numbers (ingestors/figures.py), but as a bag of numbers: a
right number in the wrong row of a table passes, and where OCR and the model disagree, nobody says
which is right. So a second model, of another family than the one that read the book, reads two
kinds of figure again: those OCR flagged, and tables. Tables are compared row by row (a row is known
by its first cell), other figures by their numbers. Disagreements are reported for the agent's QA,
with the figure's page, so a person or the agent can look at the figure and correct the notes.

`knowlib check-figures` runs it on books already ingested; with pdf.figures.second_model set,
ingestion calls the same check on new books.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .contract import read_frontmatter
from .ingestors.figures import CUT_OFF, DECORATIVE_RE, FIGURE_PROMPT

NUM_RE = re.compile(r"(?<![\w.])[-−]?\d[\d,]*(?:\.\d+)?%?")


def _num(token: str) -> str:
    """'1,400' and '1400', '20.00' and '20', '−3' and '-3' are the same number."""
    t = token.replace(",", "").replace("−", "-").rstrip("%")
    if "." in t:
        t = t.rstrip("0").rstrip(".")
    return t + ("%" if token.endswith("%") else "")


def numbers(text: str) -> Counter:
    return Counter(_num(t) for t in NUM_RE.findall(text.replace(CUT_OFF, "")))


def table_rows(text: str) -> dict[str, Counter]:
    """{first cell: numbers in the rest of the row} for each Markdown table row with numbers."""
    rows = {}
    for line in text.splitlines():
        if not line.strip().startswith("|") or re.match(r"^\s*\|[\s:|-]+\|\s*$", line):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        nums = numbers(" ".join(cells[1:]))
        if cells and cells[0] and nums:
            rows[re.sub(r"\s+", " ", cells[0].lower())] = nums
    return rows


def compare(first: str, second: str) -> list[str]:
    """What the two readings disagree on, worded for the QA report (empty when they agree)."""
    out = []
    a_rows, b_rows = table_rows(first), table_rows(second)
    shared = [k for k in a_rows if k in b_rows]
    if len(shared) >= 2:                               # both read it as a table: row by row
        for k in shared:
            if a_rows[k] != b_rows[k]:
                only_a = sorted((a_rows[k] - b_rows[k]).elements())
                only_b = sorted((b_rows[k] - a_rows[k]).elements())
                out.append(f"row '{k}': {', '.join(only_a) or '-'} in the book's reading, "
                           f"{', '.join(only_b) or '-'} in the second")
        return out
    a, b = numbers(first), numbers(second)
    only_a, only_b = sorted((a - b).elements()), sorted((b - a).elements())
    if len(only_a) + len(only_b) >= 2 or (only_a and only_b):
        out.append(f"numbers only in the book's reading: {', '.join(only_a[:8]) or '-'}; "
                   f"only in the second: {', '.join(only_b[:8]) or '-'}")
    return out


def _cached_reading(cache: dict, digest: str, prompt_v: str, skip_model: str) -> str | None:
    for k, v in cache.items():
        parts = k.split(":")
        if parts[0] == digest and parts[-1] == prompt_v and parts[-2] != skip_model:
            return v
    return None


def check_book(sid: str, source_md: Path, assets: Path, cache_path: Path, provider, *,
               scope: str = "flagged+tables", parallel: int = 1, tokens: int = 24000,
               limit: int | None = None) -> list[dict]:
    """Have `provider` read again the book's flagged figures and tables, and compare. The second
    readings are cached beside the first, under the second model's name."""
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    prompt_v = hashlib.sha256(FIGURE_PROMPT.encode()).hexdigest()[:8]
    flagged = {int(m.group(1)) for f in (read_frontmatter(source_md).get("flags") or [])
               if (m := re.match(r"figure on pdf page (\d+): the model's text and OCR disagree", str(f)))}
    printed = {int(m.group(2)): m.group(1) for m in
               re.finditer(r"<!-- page (\S+) \(pdf (\d+)\) -->", source_md.read_text(encoding="utf-8"))}
    second_model = provider.model
    todo = []
    for crop in sorted((assets / "figures").glob("fig_*.png")):
        pdf_page = int(crop.stem.split("_")[1])
        digest = hashlib.sha256(crop.read_bytes()).hexdigest()
        first = _cached_reading(cache, digest, prompt_v, second_model)
        if not first or DECORATIVE_RE.search(first):
            continue
        is_table = len(table_rows(first)) >= 2
        if ("flagged" in scope and pdf_page in flagged) or ("tables" in scope and is_table) or scope == "all":
            todo.append((crop, digest, pdf_page, first, "OCR flagged" if pdf_page in flagged else "table"))

    lock = threading.Lock()
    if limit:
        todo = todo[:limit]

    def second(item):
        crop, digest, pdf_page, first, why = item
        backend = "openai" if provider.backend == "chain" else provider.backend
        key = f"{digest}:{backend}:{second_model}:{prompt_v}"
        if key not in cache:
            try:
                reply = provider.generate([crop], FIGURE_PROMPT, max_tokens=tokens).strip()
            except (OSError, RuntimeError) as e:
                return {"page": printed.get(pdf_page, str(pdf_page)), "pdf_page": pdf_page, "figure": crop.name,
                        "why": why, "error": str(e)[:200]}
            model = getattr(provider, "answered_by", None) or second_model
            key = f"{digest}:{backend}:{model}:{prompt_v}"
            with lock:                                 # saved at once: a stopped run keeps what it read
                cache[key] = reply
                cache_path.write_text(json.dumps(cache, ensure_ascii=False))
        return {"page": printed.get(pdf_page, str(pdf_page)), "pdf_page": pdf_page, "figure": crop.name, "why": why,
                "disagreements": compare(first, cache[key])}

    with ThreadPoolExecutor(max(1, parallel)) as ex:
        return list(ex.map(second, todo))


def report(sid: str, results: list[dict]) -> str:
    bad = [r for r in results if r.get("disagreements")]
    failed = [r for r in results if r.get("error")]
    lines = [f"# Figure cross-check: {sid}", "",
             f"{len(results)} figures read a second time ({sum(r['why'] == 'table' for r in results)} tables, "
             f"{sum(r['why'] == 'OCR flagged' for r in results)} flagged by OCR): {len(bad)} disagree, "
             f"{len(results) - len(bad) - len(failed)} agree, {len(failed)} not read.", ""]
    for r in bad:
        lines.append(f"- p. {r['page']} ({r['figure']}, {r['why']}): " + "; ".join(r["disagreements"][:6]))
    for r in failed:
        lines.append(f"- p. {r['page']} ({r['figure']}): the second model could not read it ({r['error']})")
    return "\n".join(lines) + "\n"
