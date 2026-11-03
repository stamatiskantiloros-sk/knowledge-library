"""Long PDFs: printed page markers, formulas from the text layer, the outline, and book digests."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from knowlib.digest import mark_stray_pages, pages, parts, run
from knowlib.ingestors.pdf import _docling, _formula_text, _pymupdf, outline
from knowlib.providers.agent import AgentJobsWaiting
from knowlib.runner import process


def book(path: Path) -> Path:
    """Six pages: two numbered i-ii, then 1-4, with bookmarks, a caption, a mention and a figure."""
    import fitz
    doc = fitz.open()
    for lines in (["Cover"], ["Preface", "Why this book."],
                  ["CHAPTER 1", "Risk", "Beta measures the market risk of an asset."],
                  ["as shown in", "Table 1.1.", "Table 1.1", "BETAS BY SECTOR", "Utilities 0.6"],
                  ["CHAPTER 2", "Return", "Table 2.1 summarizes returns by decade."],
                  ["Figure 2.1", "Growth of $1", "End of chapter."]):
        page = doc.new_page()
        for i, line in enumerate(lines):
            page.insert_text((72, 72 + 18 * i), line, fontsize=11)
    doc.set_page_labels([{"startpage": 0, "prefix": "", "style": "r", "firstpagenum": 1},
                         {"startpage": 2, "prefix": "", "style": "D", "firstpagenum": 1}])
    doc.set_toc([[1, "Cover", 1], [1, "Preface", 2], [1, "Chapter 1 Risk", 3], [2, "Betas", 4],
                 [1, "Chapter 2 Return", 5]])
    doc.set_metadata({"title": "Test Book"})
    doc.save(path)
    return path


def test_outline_uses_printed_pages_and_skips_mentions(tmp_path):
    o = outline(book(tmp_path / "b.pdf"))
    assert {"level": 1, "title": "Chapter 1 Risk", "page": "1", "pdf": 3} in o["toc"]
    caps = {f"{c['kind']} {c['number']}": c for c in o["captions"]}
    assert caps["Table 1.1"]["title"] == "BETAS BY SECTOR" and caps["Table 1.1"]["page"] == "2"
    assert caps["Figure 2.1"]["title"] == "Growth of $1" and caps["Figure 2.1"]["page"] == "4"
    assert caps["Table 2.1"]["title"].startswith("(caption not in the text layer")   # only ever mentioned


@pytest.mark.parametrize("runner", [_pymupdf, _docling])
def test_pages_start_with_the_printed_number(tmp_path, runner):
    md = runner(book(tmp_path / "b.pdf"), {})
    assert "<!-- page ii (pdf 2) -->" in md and "<!-- page 1 (pdf 3) -->" in md
    (tmp_path / "source.md").write_text(f"---\ntitle: t\n---\n\n{md}")
    pg = pages(tmp_path / "source.md")
    assert pg[3][0] == "1" and "market risk" in pg[3][1] and "market risk" not in pg[4][1]


def test_formula_text_comes_from_the_box_on_the_page(tmp_path):
    import fitz
    from docling_core.types.doc import BoundingBox, CoordOrigin
    doc = fitz.open()
    page = doc.new_page()              # 842 points high
    page.insert_text((72, 100), "Value = CF / (r - g)", fontsize=11)
    page.insert_text((72, 300), "Unrelated text", fontsize=11)
    box = BoundingBox(l=60, t=842 - 85, r=400, b=842 - 105, coord_origin=CoordOrigin.BOTTOMLEFT)
    assert _formula_text(doc, SimpleNamespace(page_no=1, bbox=box)) == "Value = CF / (r - g)"


def test_parts_split_long_chapters_and_skip_front_matter():
    toc = [{"level": 1, "title": "Cover", "pdf": 1}, {"level": 1, "title": "Chapter 1", "pdf": 2},
           {"level": 2, "title": "1.1", "pdf": 2}, {"level": 2, "title": "1.2", "pdf": 5},
           {"level": 1, "title": "Chapter 2", "pdf": 9}, {"level": 1, "title": "Index", "pdf": 11}]
    chars = {p: 100 for p in range(1, 12)}
    got = parts(toc, 11, chars, max_chars=350)
    assert [(p["title"], p["first"], p["last"]) for p in got] == [
        ("Chapter 1", 2, 4), ("Chapter 1", 5, 7), ("Chapter 1", 8, 8), ("Chapter 2", 9, 10)]
    # a chapter whose first section starts after a short opening page keeps that page
    toc = [{"level": 1, "title": "Chapter 1", "pdf": 1}, {"level": 2, "title": "1.1", "pdf": 2}]
    got = parts(toc, 6, {1: 20, **{p: 100 for p in range(2, 7)}}, max_chars=350)
    assert [(p["first"], p["last"]) for p in got] == [(1, 4), (5, 6)]


def test_mark_stray_pages():
    reply = "- **Beta** (p. 52): risk\n- **Alpha** (p. 99): excess return\n- **Disney** (pp. 52–53): example"
    out = mark_stray_pages(reply, ["52", "53"]).splitlines()
    assert not out[0].endswith("⚠") and out[1].endswith("⚠") and not out[2].endswith("⚠")


def test_drop_repeats_marks_a_looping_reply():
    from knowlib.digest import drop_repeats
    item = "- **Beta** (p. 52): market risk."
    text, looped = drop_repeats("### Key terms\n" + "\n".join([item, "- **Alpha** (p. 53): excess."] * 4)
                                + "\n- **Gamma** (p. 5")
    assert looped and text.count(item) == 1 and "Gamma" not in text
    assert drop_repeats("### Formulas\n(none)\n### Worked examples\n(none)") == (
        "### Formulas\n(none)\n### Worked examples\n(none)", False)


def test_digest_waits_for_the_session_then_writes(tmp_path):
    root = tmp_path / "lib"
    book((root / "inbox").mkdir(parents=True) or root / "inbox" / "test-book.pdf")
    (root / "config.yaml").write_text("frame_selection: {backend: mock}\npdf: {backend: pymupdf, fallbacks: []}\n"
                                      "digest: {min_pages: 3}\n")
    process(root)
    session = {"digest": {"backend": "agent", "agent_model": "haiku"}}
    with pytest.raises(AgentJobsWaiting):
        run(root, overrides=session)
    jobs = json.loads((root / ".work" / "agent" / "jobs.json").read_text())
    assert len(jobs) == 3                                   # preface and two chapters, not the cover
    chapter1 = next(j for j in jobs if '"Chapter 1 Risk"' in j["prompt"])
    assert "=== Page 1 ===" in chapter1["prompt"] and "market risk" in chapter1["prompt"]
    for j in jobs:
        Path(j["answer"]).write_text("### Key terms\n- **Beta** (p. 1): market risk\n- **Alpha** (p. 99): stray\n")
    (result,) = run(root, overrides=session)
    text = Path(result["digest"]).read_text()
    assert result["status"] == "written" and "source_hash: " in text
    assert "- Table 1.1: BETAS BY SECTOR, p. 2" in text and "- Chapter 1 Risk, p. 1" in text
    assert "## Chapter 1 Risk (pp. 1–2)" in text and "(p. 99): stray ⚠" in text
    assert run(root, overrides=session)[0]["status"] == "current"


def test_a_looped_part_is_read_again_in_pieces(tmp_path):
    from knowlib.digest import merge
    assert merge(["### Key terms\n- a (p. 1)\n### Formulas\n(none)", "### Key terms\n- b (p. 2)\n### Formulas\n- f (p. 2)"]) \
        == "### Key terms\n- a (p. 1)\n- b (p. 2)\n\n### Formulas\n- f (p. 2)"
    root = tmp_path / "lib"
    book((root / "inbox").mkdir(parents=True) or root / "inbox" / "test-book.pdf")
    (root / "config.yaml").write_text("frame_selection: {backend: mock}\npdf: {backend: pymupdf, fallbacks: []}\n"
                                      "digest: {min_pages: 3}\n")
    process(root)
    session = {"digest": {"backend": "agent", "agent_model": "haiku", "fallback_max_chars": 40,
                          "fallback_model": {"agent_model": "sonnet"}}}
    jobs_file = root / ".work" / "agent" / "jobs.json"
    with pytest.raises(AgentJobsWaiting):
        run(root, overrides=session)
    for j in json.loads(jobs_file.read_text()):
        loop = "\n".join(["- **Beta** (p. 1): risk."] * 5) if '"Chapter 1 Risk"' in j["prompt"] else "- **Ok** (p. 3): fine."
        Path(j["answer"]).write_text("### Key terms\n" + loop + "\n")
    with pytest.raises(AgentJobsWaiting):                   # the 9B looped on chapter 1: sonnet reads it in pieces
        run(root, overrides=session)
    jobs = [j for j in json.loads(jobs_file.read_text()) if j["model"] == "sonnet"]
    assert len(jobs) == 2 and all('"Chapter 1 Risk"' in j["prompt"] for j in jobs)
    for n, j in enumerate(jobs):
        Path(j["answer"]).write_text(f"### Key terms\n- **Term{n}** (p. {n + 1}): read again.\n")
    text = Path(run(root, overrides=session)[0]["digest"]).read_text()
    chapter1 = text.split("## Chapter 1 Risk")[1].split("## Chapter 2")[0]
    assert "Read again by sonnet (in 2 pieces)" in chapter1 and "repeated itself here" not in chapter1
    assert "- **Term0** (p. 1)" in chapter1 and "- **Term1** (p. 2)" in chapter1 and "Beta" not in chapter1
