"""knowlib check-notes on a hand-built library: each check catches its planted problem and passes good notes."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowlib.notecheck import Source, claim_numbers, key, run

BOOK = """---
title: Finance Book
---
<!-- page 52 (pdf 68) -->
Beta measures market risk. The market premium is 5.5% over 1,000 firms.

<!-- page 53 (pdf 69) -->
"The cost of capital is the rate of return investors require." WACC was 8.5% for Disney.

<!-- page 54 (pdf 70) -->
Unlevered beta is 0.9 for the sector.
"""

LECTURE = """---
title: Lecture 4
---
# Lecture 4

## Slide 1 (00:00–01:30)

**On slide:** WACC = E/V × Re + D/V × Rd × (1 − T)

## Slide 2 (01:30–03:00)

**On slide:** Example: WACC of 8.5% with a tax rate of 25%.
"""

DECK = """---
title: Deck
---
## Slide 1: Capital structure

Debt ratio of 40% is the target. WACC falls as debt rises, then climbs again.
WACC and capital structure, WACC and taxes, WACC again.
"""


def note(slug: str, title: str, body: str, sources: list[str], aliases=(), updated="2026-11-06") -> str:
    meta = {"title": title, "aliases": list(aliases), "sources": sources, "updated": updated}
    return "---\n" + json.dumps(meta) + "\n---\n# " + title + "\n\nSummary.\n\n" + body


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "lib"
    for sid, text in (("book", BOOK), ("lecture-4", LECTURE), ("deck", DECK)):
        (root / "sources" / sid).mkdir(parents=True)
        (root / "sources" / sid / "source.md").write_text(text)
    (root / "notes").mkdir()
    entries = {sid: {"source_id": sid, "status": "done", "synthesized": sid != "deck", "updated_at": "2026-11-01T10:00:00"}
               for sid in ("book", "lecture-4", "deck")}
    (root / "manifest.json").write_text(json.dumps({"version": 1, "entries": entries}))
    (root / "index.md").write_text("# Index\n\n- [[wacc]]\n- [[cost-of-capital]]\n<!-- - [[example]] -->\n")
    return root


def write(root: Path, slug: str, text: str) -> None:
    (root / "notes" / f"{slug}.md").write_text(text)


def messages(findings, check, level=None):
    return [f.message for f in findings if f.check == check and (level is None or f.level == level)]


def test_number_matching():
    assert {8.5, 1000.0} <= claim_numbers("WACC of 8.50% across 1,000 firms")
    assert 0.6 in claim_numbers("beta 0,6")                     # decimal comma
    assert 7.0 not in claim_numbers("step 7")                   # small counts are not checked
    assert key("Net Present Values") == key("net present value")


def test_locators():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        for sid, text in (("book", BOOK), ("lecture", LECTURE)):
            (Path(d) / sid).mkdir()
            (Path(d) / sid / "source.md").write_text(text)
        book, lecture = Source(Path(d) / "book"), Source(Path(d) / "lecture")
        assert "Disney" in book.region("p. 53")[0] and "0.9" in book.region("pp. 53–54")[0]
        assert book.region("p. 99")[1] == "has no page 99"
        assert "tax rate" in lecture.region("slide 2, 02:10")[0]
        assert "runs 01:30–03:00" in lecture.region("slide 2, 05:00")[1]
        assert "tax rate" in lecture.region("02:00")[0]
        assert "not a page" in lecture.region("chapter 3")[1]


def test_good_notes_pass(library):
    write(library, "wacc", note("wacc", "Weighted average cost of capital",
        "## Key points\n- Disney's WACC was 8.5% [@book, p. 53].\n"
        "- The formula is E/V × Re + D/V × Rd × (1 − T) [@lecture-4, slide 1, 00:40].\n"
        "- The deck targets a debt ratio of 40% [@deck, § Capital structure].\n\n## Related\n- [[cost-of-capital]]\n",
        ["book", "lecture-4", "deck"], aliases=["WACC"]))
    write(library, "cost-of-capital", note("cost-of-capital", "Cost of capital",
        '## Key points\n- "The cost of capital is the rate of return investors require." [@book, p. 53]\n',
        ["book"]))
    findings = run(library)
    assert [f for f in findings if f.level in ("error", "warning")] == [], findings


def test_citation_problems(library):
    write(library, "wacc", note("wacc", "WACC",
        "## Key points\n- The premium is 6.5% [@book, p. 52].\n"            # wrong number
        "- Unlevered beta is 0.9 [@book, p. 53].\n"                         # one page off
        "- Slide 9 shows it [@lecture-4, slide 9].\n"                       # no such slide
        "- Ghost source [@nowhere, p. 1].\n"                                # no such source
        "- WACC rises with leverage beyond the optimum.\n",                 # no citation
        ["book", "lecture-4"]))
    found = messages(run(library, only=["citations"]), "citations")
    assert any("does not contain 6.5" in m for m in found)
    assert any("does not contain 0.9; it is on p. 54" in m for m in found)
    assert any("has no slide 9" in m for m in found)
    assert any("[@nowhere] is not a source" in m for m in found)
    assert any(m.startswith("no citation") for m in found)


def test_duplicates(library):
    write(library, "wacc", note("wacc", "Weighted average cost of capital",
        "## Key points\n- Disney's WACC was 8.5% for the year and the firm kept it [@book, p. 53].\n", ["book"],
        aliases=["WACC"]))
    write(library, "weighted-average-cost-of-capital", note("weighted-average-cost-of-capital", "WACC",
        "## Key points\n- Disney's WACC was 8.5% for the year and the firm kept it [@book, p. 53].\n", ["book"]))
    found = run(library, only=["duplicates"])
    assert any("both are called" in m for m in messages(found, "duplicates", "warning"))
    assert any("same claim as in" in m for m in messages(found, "duplicates", "info"))


def test_consolidation(library):
    write(library, "wacc", note("wacc", "WACC",
        "## Key points\n- Disney's WACC was 8.5% [@book, p. 53].\n\n## Related\n- [[capm]]\n",
        ["book", "lecture-4"], updated="2026-10-30"))
    found = messages(run(library, only=["consolidation"]), "consolidation")
    assert any("listed in sources but never cited: lecture-4" in m for m in found)
    assert any("[[capm]] has no note" in m for m in found)
    assert any("[[cost-of-capital]] has no note" in m for m in found)       # index points to a missing note
    assert not any("[[example]]" in m for m in found)                       # commented-out example ignored
    assert any("lecture-4 is marked synthesized but no note cites it" in m for m in found)
    assert any("book was reprocessed on 2026-11-01, after this note" in m for m in found)
    assert any("also discussed in, not cited: deck" in m for m in found)    # the deck talks about WACC
