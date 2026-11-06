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
    assert any("also discussed in, not cited: deck (wacc " in m for m in found)  # names the matching term


def test_review_round_trip_through_the_session(library):
    from knowlib import notereview
    from knowlib.providers.agent import AgentJobsWaiting
    write(library, "wacc", note("wacc", "WACC",
        "## Key points\n- Disney's WACC was 8.5% [@book, p. 53].\n"
        "- WACC is always the right discount rate for every project [@book, p. 53].\n"
        "- Beta measures market risk [@book, p. 52].\n", ["book"]))
    session = {"notes_review": {"backend": "agent", "agent_model": "sonnet"}}
    with pytest.raises(AgentJobsWaiting):
        notereview.run(library, overrides=session)
    jobs = json.loads((library / ".work" / "agent" / "jobs.json").read_text())
    assert len(jobs) == 1 and jobs[0]["model"] == "sonnet"
    prompt = jobs[0]["prompt"]
    assert prompt.count("[P1] book, p. 53") == 1 and "[P2] book, p. 52" in prompt   # each passage sent once
    assert "Disney" in prompt and "3. Beta measures market risk" in prompt
    Path(jobs[0]["answer"]).write_text(json.dumps({"claims": [
        {"n": 1, "verdict": "supported", "reason": "'WACC was 8.5% for Disney'"},
        {"n": 2, "verdict": "unsupported", "reason": "the passage says nothing about every project"},
        {"n": 3, "verdict": "partly", "reason": "'measures market risk' but nothing more"}]}))
    found = notereview.run(library, overrides=session)
    assert [f.level for f in found] == ["error", "warning"]
    assert "unsupported: WACC is always" in found[0].message and "every project" in found[0].message
    assert notereview.run(library, overrides=session) == found  # cached: no new jobs


def test_review_keeps_local_only_sources_on_the_machine(library):
    from knowlib import notereview
    from knowlib.providers.base import LocalOnlyViolation
    m = json.loads((library / "manifest.json").read_text())
    m["entries"]["book"]["local_only"] = True
    (library / "manifest.json").write_text(json.dumps(m))
    write(library, "wacc", note("wacc", "WACC", "## Key points\n- Disney's WACC was 8.5% [@book, p. 53].\n", ["book"]))
    session = {"notes_review": {"backend": "agent", "agent_model": "sonnet"}}
    found = notereview.run(library, overrides=session)          # nothing to send, so no job and no wait
    assert len(found) == 1 and "not reviewed: cites local_only book" in found[0].message
    with pytest.raises(LocalOnlyViolation):
        notereview.run(library, overrides={"local_only": True, **session})


def test_local_only_sticks_in_the_manifest(tmp_path):
    from knowlib.manifest import Manifest
    m = Manifest(tmp_path / "manifest.json")
    m.record_success("a.pdf", content_hash="h", source_id="a", ingestor="pdf", pipeline={}, flags=[], local_only=True)
    m.record_success("a.pdf", content_hash="h2", source_id="a", ingestor="pdf", pipeline={}, flags=[])
    assert m.entries["a.pdf"]["local_only"] is True


def test_reviewer_is_configured_like_any_model():
    from knowlib.config import DEFAULTS, deep_merge
    from knowlib.notereview import reviewer_section
    cfg = deep_merge(DEFAULTS, {"frame_selection": {"backend": "openai", "openai_model": "fast",
                                                    "caption_model": {"openai_model": "strong", "context_length": 16384}}})
    s = reviewer_section(cfg)
    assert s["openai_model"] == "strong" and s["backend"] == "openai"           # default: the strong local model
    s = reviewer_section(deep_merge(cfg, {"notes_review": {"openai_model": "other-family", "context_length": 32768}}))
    assert (s["openai_model"], s["context_length"]) == ("other-family", 32768)  # notes_review wins
    s = reviewer_section(deep_merge(cfg, {"notes_review": {"backend": "agent", "agent_model": "sonnet"}}))
    assert (s["backend"], s["agent_model"]) == ("agent", "sonnet")
