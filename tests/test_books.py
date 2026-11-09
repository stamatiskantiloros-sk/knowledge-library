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
    assert "Read again by sonnet (in 2 pieces)" in chapter1 and "was cut off" not in chapter1
    assert "- **Term0** (p. 1)" in chapter1 and "- **Term1** (p. 2)" in chapter1 and "Beta" not in chapter1


def test_a_part_cut_off_at_the_token_limit_is_asked_again_then_read_in_pieces(tmp_path, monkeypatch):
    from knowlib.providers.mock import MockProvider
    root = tmp_path / "lib"
    book((root / "inbox").mkdir(parents=True) or root / "inbox" / "test-book.pdf")
    (root / "config.yaml").write_text("frame_selection: {backend: mock}\npdf: {backend: pymupdf, fallbacks: []}\n"
                                      "digest: {min_pages: 3}\n")
    process(root)
    asked = []

    def generate(self, images, prompt, *, json_mode=False, max_tokens=None):
        whole = '"Chapter 1 Risk", pages 1–2' in prompt
        asked.append((whole, max_tokens))
        self.last_finish = "length" if whole else "stop"
        page = prompt.split("=== Page ")[-1].split(" ")[0]
        return ("### Key terms\n- **Beta** (p. 1): risk.\n- **Cut" if whole
                else f"### Key terms\n- **Term{page}** (p. {page}): fine.")
    monkeypatch.setattr(MockProvider, "generate", generate)
    text = Path(run(root)[0]["digest"]).read_text()
    assert [t for w, t in asked if w] == [8000, 16000]      # asked again with twice the tokens, still cut off
    chapter1 = text.split("## Chapter 1 Risk")[1].split("## Chapter 2")[0]
    assert "Read again by mock (in 2 pieces)" in chapter1 and "was cut off" not in chapter1
    assert "**Term1** (p. 1)" in chapter1 and "**Term2** (p. 2)" in chapter1 and "**Cut" not in chapter1
    asked.clear()
    run(root, force=True)                                   # cached with the tokens it had: not asked again
    assert asked == []
    run(root, force=True, overrides={"digest": {"max_tokens": 9000}})     # more room: asked again
    assert [t for w, t in asked if w] == [9000, 18000] and not [1 for w, _ in asked if not w]


def _book_with_chart(tmp_path):
    """book.pdf with a chart on page 1 and a logo on every page, and Docling-style figure markers."""
    import fitz
    from PIL import Image, ImageDraw, ImageFont
    from knowlib.ingestors.figures import marker
    font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 36)
    chart = Image.new("RGB", (800, 500), "white")
    d = ImageDraw.Draw(chart)
    for i, h in enumerate((120, 260, 380)):
        d.rectangle((100 + i * 220, 450 - h, 260 + i * 220, 450), fill=(40, 90, 200))
        d.text((110 + i * 220, 455 - h - 45), f"{h} units", fill="black", font=font)
    d.text((250, 10), "Inventory by quarter", fill="black", font=font)
    chart.save(tmp_path / "chart.png")
    logo = Image.new("RGB", (300, 120), (200, 30, 30))
    ImageDraw.Draw(logo).text((20, 30), "ACME", fill="white", font=font)
    logo.save(tmp_path / "logo.png")
    doc = fitz.open()
    md = []
    for p in range(1, 4):
        page = doc.new_page()
        page.insert_image(fitz.Rect(400, 20, 560, 84), filename=str(tmp_path / "logo.png"))
        boxes = [(400, 20, 560, 84)]
        if p == 1:
            page.insert_image(fitz.Rect(72, 150, 472, 400), filename=str(tmp_path / "chart.png"))
            page.insert_image(fitz.Rect(72, 420, 90, 438), filename=str(tmp_path / "chart.png"))
            boxes += [(72, 150, 472, 400), (72, 420, 90, 438)]
        md.append(f"<!-- page {p} (pdf {p}) -->\n\n" + "\n\n".join(marker(p, k, b) for k, b in enumerate(boxes, 1)))
    doc.save(tmp_path / "book.pdf")
    (tmp_path / "lib").mkdir()
    return "\n\n".join(md)


def test_an_empty_figure_reply_is_flagged_kept_and_asked_again(tmp_path, monkeypatch):
    """A model that spends every token thinking returns nothing: the figure is flagged as not read,
    its crop kept, and nothing cached, so the next run asks again; it is never taken as decorative."""
    import json
    import knowlib.providers as providers
    from knowlib.config import load_config
    from knowlib.contract import SourceWriter
    from knowlib.ingestors.figures import read_figures

    class Thinker:
        backend, model, last_finish = "openai", "thinker", None
        def generate(self, images, prompt, *, json_mode=False, max_tokens=None):
            self.last_finish = "length"
            return ""
    monkeypatch.setattr(providers, "get_vision_provider", lambda section, cfg: Thinker())
    md = _book_with_chart(tmp_path)
    cfg = load_config(tmp_path / "lib", {"frame_selection": {"backend": "mock"}})
    w = SourceWriter(tmp_path / "sources", "book-1")
    out, flags, stats = read_figures(tmp_path / "book.pdf", md, w, cfg, tmp_path)
    assert stats["read"] == 0 and stats["skipped"]["not read"] == 1 and "decorative" not in stats["skipped"]
    assert any("not read (it stopped at the token limit or in a loop before answering, 3 times" in f
               for f in flags), flags
    assert "![figure](assets/figures/fig_0001_2.png)" in out and "not read, see the flags" in out
    assert (w.assets / "figures" / "fig_0001_2.png").exists()
    assert json.loads((tmp_path / "figures_cache.json").read_text() if (tmp_path / "figures_cache.json").exists() else "{}") == {}
    w.abort()


def test_figures_in_a_pdf_are_read_and_decoration_is_skipped(tmp_path):
    """A chart is cropped and read; an icon (small) and a logo on every page (repeated) are not."""
    import fitz
    from PIL import Image, ImageDraw, ImageFont
    from knowlib.config import load_config
    from knowlib.contract import SourceWriter
    from knowlib.ingestors.figures import marker, read_figures
    font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 36)
    chart = Image.new("RGB", (800, 500), "white")
    d = ImageDraw.Draw(chart)
    for i, h in enumerate((120, 260, 380)):
        d.rectangle((100 + i * 220, 450 - h, 260 + i * 220, 450), fill=(40, 90, 200))
        d.text((110 + i * 220, 455 - h - 45), f"{h} units", fill="black", font=font)
    d.text((250, 10), "Inventory by quarter", fill="black", font=font)
    chart.save(tmp_path / "chart.png")
    logo = Image.new("RGB", (300, 120), (200, 30, 30))
    ImageDraw.Draw(logo).text((20, 30), "ACME", fill="white", font=font)
    logo.save(tmp_path / "logo.png")
    doc = fitz.open()
    md = []
    for p in range(1, 4):
        page = doc.new_page()
        page.insert_image(fitz.Rect(400, 20, 560, 84), filename=str(tmp_path / "logo.png"))
        boxes = [(400, 20, 560, 84)]
        if p == 1:
            page.insert_image(fitz.Rect(72, 150, 472, 400), filename=str(tmp_path / "chart.png"))
            page.insert_image(fitz.Rect(72, 420, 90, 438), filename=str(tmp_path / "chart.png"))
            boxes += [(72, 150, 472, 400), (72, 420, 90, 438)]
        md.append(f"<!-- page {p} (pdf {p}) -->\n\n" + "\n\n".join(marker(p, k, b) for k, b in enumerate(boxes, 1)))
    doc.save(tmp_path / "book.pdf")
    (tmp_path / "lib").mkdir()
    cfg = load_config(tmp_path / "lib", {"frame_selection": {"backend": "mock"}})
    w = SourceWriter(tmp_path / "sources", "book-1")
    out, flags, stats = read_figures(tmp_path / "book.pdf", "\n\n".join(md), w, cfg, tmp_path)
    assert stats["read"] == 1 and stats["skipped"] == {"small": 1, "repeated": 3}, stats
    assert "![figure](assets/figures/fig_0001_2.png)" in out and "Figure, read by the vision model" in out
    assert "260 units" in out and out.count("<!-- image: repeated -->") == 3 and "<!-- image: small -->" in out
    assert sorted(p.name for p in (w.assets / "figures").iterdir()) == ["fig_0001_2.png"]
    w.abort()


def test_figure_check_ignores_layout_and_spaced_signs_but_not_wrong_numbers():
    """The figure check drops headings, code fences and dotted leaders, which OCR never sees, and
    a sign OCR spaced apart or dropped still matches; a different or opposite number is flagged."""
    from knowlib.ingestors.figures import DECORATIVE_RE, for_check
    from knowlib.media.ocr import compare
    reading = ("# Journal entry\n```\n| Cash (+A) ............ | +3,000 |\n| Notes Payable (+L) . . . . . . | 8,000 |\n```\n"
               "Figure: an entry")
    ocr_text = "Cash (+A)\n+ 3,000\nNotes Payable (+L)\n8,000"
    assert not compare(for_check(reading), ocr_text, 0.75)["flag"]
    assert compare(for_check(reading.replace("8,000", "9,000")), ocr_text, 0.75)["numbers_only_vlm"] == ["9.000"]
    assert compare("Cash +3,000", "Cash -3,000", 0.5)["flag"]
    assert DECORATIVE_RE.search("It is a photo of a train.\n\nDECORATIVE")
    assert not DECORATIVE_RE.search("Decorative trim costs 3,000")


def test_a_figure_reading_cut_off_at_the_token_limit_says_so(tmp_path, monkeypatch):
    import knowlib.providers as providers
    from knowlib.config import load_config
    from knowlib.contract import SourceWriter
    from knowlib.ingestors.figures import read_figures

    budgets = []

    class Long:
        backend, model, last_finish = "openai", "long", None
        def generate(self, images, prompt, *, json_mode=False, max_tokens=None):
            budgets.append(max_tokens)
            self.last_finish = "length"
            return "| Cash | 120 |\n| Sales | 260"
    monkeypatch.setattr(providers, "get_vision_provider", lambda section, cfg: Long())
    md = _book_with_chart(tmp_path)
    cfg = load_config(tmp_path / "lib", {"frame_selection": {"backend": "mock"}})
    w = SourceWriter(tmp_path / "sources", "book-1")
    out, flags, stats = read_figures(tmp_path / "book.pdf", md, w, cfg, tmp_path)
    assert stats["read"] == 1 and "[CUT OFF" in out
    assert any("the reading is cut off" in f for f in flags), flags
    assert budgets == [8000, 16000, 32000]                  # each retry gets twice the tokens
    w.abort()


def test_a_figure_reading_stuck_in_a_loop_is_asked_again(tmp_path, monkeypatch):
    """A thinking loop is random: an empty reply is retried, and the second answer is used."""
    import knowlib.providers as providers
    from knowlib.config import load_config
    from knowlib.contract import SourceWriter
    from knowlib.ingestors.figures import read_figures

    class Flaky:
        backend, model, last_finish, calls = "openai", "flaky", None, 0
        def generate(self, images, prompt, *, json_mode=False, max_tokens=None):
            self.calls += 1
            self.last_finish = "length" if self.calls == 1 else "stop"
            return "" if self.calls == 1 else "120 units 260 units 380 units\nFigure: a bar chart"
    flaky = Flaky()
    monkeypatch.setattr(providers, "get_vision_provider", lambda section, cfg: flaky)
    md = _book_with_chart(tmp_path)
    cfg = load_config(tmp_path / "lib", {"frame_selection": {"backend": "mock"}})
    w = SourceWriter(tmp_path / "sources", "book-1")
    out, flags, stats = read_figures(tmp_path / "book.pdf", md, w, cfg, tmp_path)
    assert flaky.calls == 2 and stats["read"] == 1 and "not read" not in stats["skipped"], (flaky.calls, stats)
    assert not any("not read" in f or "cut off" in f for f in flags), flags
    w.abort()


def test_a_figure_the_model_server_refuses_is_kept_as_not_read(tmp_path, monkeypatch):
    """A connection error is not a reading: the figure keeps its crop and is flagged, never decorative."""
    import knowlib.providers as providers
    from knowlib.config import load_config
    from knowlib.contract import SourceWriter
    from knowlib.ingestors.figures import read_figures

    class Down:
        backend, model, last_finish = "openai", "down", None
        def generate(self, images, prompt, *, json_mode=False, max_tokens=None):
            raise OSError("Connection refused")
    monkeypatch.setattr(providers, "get_vision_provider", lambda section, cfg: Down())
    md = _book_with_chart(tmp_path)
    cfg = load_config(tmp_path / "lib", {"frame_selection": {"backend": "mock"}})
    w = SourceWriter(tmp_path / "sources", "book-1")
    out, flags, stats = read_figures(tmp_path / "book.pdf", md, w, cfg, tmp_path)
    assert stats["skipped"].get("not read") == 1 and "decorative" not in out, (stats, out)
    assert "not read, see the flags" in out and (w.assets / "figures" / "fig_0001_2.png").exists()
    assert any("not read: Connection refused" in f for f in flags), flags
    w.abort()


def _detail_provider(per_px):
    """A provider whose server gives an image per_px(width, height) tokens."""
    from PIL import Image

    class Server:
        backend, model, last_finish, asked = "openai", "server", None, 0
        def image_tokens(self, image):
            with Image.open(image) as img:
                return per_px(*img.size)
        def generate(self, images, prompt, *, json_mode=False, max_tokens=None):
            self.asked += 1
            self.last_finish = "stop"
            return "120 units 260 units 380 units\nFigure: a bar chart"
    return Server()


def _read_chart(tmp_path, monkeypatch, server, **figures):
    import knowlib.providers as providers
    from knowlib.config import load_config
    from knowlib.contract import SourceWriter
    from knowlib.ingestors.figures import read_figures
    monkeypatch.setattr(providers, "get_vision_provider", lambda section, cfg: server)
    md = (tmp_path / "book.md").read_text() if (tmp_path / "book.md").exists() else _book_with_chart(tmp_path)
    (tmp_path / "book.md").write_text(md)
    cfg = load_config(tmp_path / "lib", {"frame_selection": {"backend": "mock"}, "pdf": {"figures": figures}})
    w = SourceWriter(tmp_path / "sources", "book-1")
    try:
        return read_figures(tmp_path / "book.pdf", md, w, cfg, tmp_path)
    finally:
        w.abort()


def test_a_server_that_caps_image_tokens_stops_figure_reading(tmp_path, monkeypatch):
    """Every image at ~290 tokens whatever its size: dense figures would lose digits, so stop and say why."""
    import pytest
    from knowlib.ingestors.figures import DetailCapped
    capped = _detail_provider(lambda w, h: 290)
    with pytest.raises(DetailCapped, match="caps image tokens"):
        _read_chart(tmp_path, monkeypatch, capped)
    assert capped.asked == 0


def test_a_capped_server_with_detail_check_warn_reads_and_flags(tmp_path, monkeypatch):
    capped = _detail_provider(lambda w, h: 290)
    out, flags, stats = _read_chart(tmp_path, monkeypatch, capped, detail_check="warn")
    assert stats["read"] == 1 and any("caps image tokens" in f for f in flags), flags


def test_a_server_that_keeps_detail_passes_the_check_and_cached_figures_skip_it(tmp_path, monkeypatch):
    full = _detail_provider(lambda w, h: w * h // 1024)
    out, flags, stats = _read_chart(tmp_path, monkeypatch, full)
    assert stats["read"] == 1 and not any("caps" in f for f in flags), flags
    capped = _detail_provider(lambda w, h: 290)                 # all cached now: nothing to check
    out, flags, stats = _read_chart(tmp_path, monkeypatch, capped)
    assert stats["read"] == 1 and capped.asked == 0
