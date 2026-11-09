"""A library in place: the user's own folder, with each source's Markdown beside the original."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowlib.runner import process

from .test_books import book


def scanned(path: Path) -> Path:
    """A one-page PDF with no text layer, so it gets a page render (an image link to rewrite)."""
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.draw_rect(fitz.Rect(72, 72, 300, 200), fill=(0.2, 0.4, 0.8))
    doc.save(path)
    return path


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "Course"
    (root / "Lecturer" / "week 1").mkdir(parents=True)
    book(root / "textbook.pdf")
    scanned(root / "Lecturer" / "week 1" / "scan.pdf")
    (root / "Lecturer" / "notes.md").write_text("# My notes\n\nFIFO sends the oldest costs to cost of goods sold.\n")
    (root / "_knowlib").mkdir()
    (root / "_knowlib" / "config.yaml").write_text(
        "library: {mode: inplace}\nframe_selection: {backend: mock}\npdf: {backend: pymupdf, fallbacks: []}\n"
        "digest: {min_pages: 3}\n")
    return root


def test_sources_land_beside_their_originals(folder):
    rep = process(folder)
    assert not rep["failed"], rep["failed"]
    for out in ("textbook.pdf.md", "Lecturer/notes.md.md", "Lecturer/week 1/scan.pdf.md"):
        text = (folder / out).read_text()
        assert "generated_by: knowlib" in text and f"original: {out[:-3]}" in text
    manifest = json.loads((folder / "_knowlib" / "manifest.json").read_text())["entries"]
    scan = manifest["Lecturer/week 1/scan.pdf"]
    assert scan["output"] == "Lecturer/week 1/scan.pdf.md"
    # the scan's page render lives under _knowlib/assets/<id>/ and its link climbs out of two folders
    render = folder / "_knowlib" / "assets" / scan["source_id"] / "page_001.png"
    assert render.exists()
    link = f"../../_knowlib/assets/{scan['source_id']}/page_001.png"
    assert link in (folder / "Lecturer/week 1/scan.pdf.md").read_text()
    assert ((folder / "Lecturer/week 1") / link).resolve() == render.resolve()
    assert not (folder / "sources").exists() and not (folder / "inbox").exists()


def test_a_second_run_does_not_ingest_its_own_output(folder):
    process(folder)
    rep = process(folder)
    assert rep["new"] == [] and rep["updated"] == [] and rep["unchanged"] == 3
    assert not any(k.endswith(".md.md") or "_knowlib" in k for k in rep["unsupported"])


def test_notes_and_digests_work_in_place(folder):
    from knowlib import digest, notecheck
    from knowlib.providers.agent import AgentJobsWaiting
    process(folder)
    sid = json.loads((folder / "_knowlib" / "manifest.json").read_text())["entries"]["textbook.pdf"]["source_id"]
    (folder / "notes").mkdir(exist_ok=True)
    (folder / "notes" / "risk.md").write_text(f"---\ntitle: Beta\nsources: [{sid}]\n---\n# Beta\n\n## Key points\n"
                                              f"- Beta measures the market risk of an asset [@{sid}, p. 1].\n"
                                              f"- Utilities have a beta of 0.6 [@{sid}, p. 1].\n")
    (folder / "INDEX.md").write_text("# Index\n\n- [[risk]]\n")
    found = notecheck.run(folder)
    assert [f.message for f in found if f.level != "info"] == [
        f"[@{sid}, p. 1] does not contain 0.6; it is on p. 2: Utilities have a beta of 0.6 ."]
    session = {"digest": {"backend": "agent", "agent_model": "haiku"}}
    with pytest.raises(AgentJobsWaiting):
        digest.run(folder, overrides=session)
    for j in json.loads((folder / "_knowlib" / "work" / "agent" / "jobs.json").read_text()):
        Path(j["answer"]).write_text("### Key terms\n- **Beta** (p. 1): market risk\n")
    (result,) = digest.run(folder, overrides=session)
    assert Path(result["digest"]) == folder / "textbook.pdf.digest.md"
    assert process(folder)["new"] == []                       # the digest is not crawled as a source


def test_setup_in_place_writes_answers_and_agent_files(tmp_path):
    from knowlib.setup import init
    root = tmp_path / "Course"
    (root / "Lecturer").mkdir(parents=True)
    book(root / "Lecturer" / "textbook.pdf")
    cfg = init(root, mode="inplace", skip=["Old*"], local_only=False, language="en",
               fast="qwen-9b", strong="qwopus-27b", reviewer="gemma-12b", interactive=False)
    assert cfg == root / "_knowlib" / "config.yaml"
    import yaml
    answers = yaml.safe_load(cfg.read_text())
    assert answers["library"] == {"mode": "inplace", "ignore": ["Old*"]}
    assert answers["frame_selection"]["openai_model"] == "qwen-9b"
    assert answers["frame_selection"]["caption_model"]["openai_model"] == "qwopus-27b"
    assert answers["notes_review"]["openai_model"] == "gemma-12b"
    agents = (root / "AGENTS.md").read_text()
    assert "{{" not in agents and "_knowlib/sources.jsonl" in agents and "sources/<id>" not in agents
    assert "@AGENTS.md" in (root / "CLAUDE.md").read_text()
    assert "_knowlib/assets/" in (root / ".ignore").read_text()
    (root / "AGENTS.md").write_text("my own edits")
    init(root, mode="inplace", interactive=False)                 # a second setup keeps the user's edits
    assert (root / "AGENTS.md").read_text() == "my own edits"
    (root / "_knowlib" / "config.yaml").write_text(
        "library: {mode: inplace}\nframe_selection: {backend: mock}\npdf: {backend: pymupdf, fallbacks: []}\n")
    process(root)
    index = (root / "INDEX.md").read_text()
    assert "<!-- knowlib:sources -->" in index and "[textbook.pdf.md](Lecturer/textbook.pdf.md)" in index
    row = json.loads((root / "_knowlib" / "sources.jsonl").read_text().splitlines()[0])
    assert row["markdown"] == "Lecturer/textbook.pdf.md" and row["size"] == "6 pages" and row["synthesized"] is False
    process(root)                                                 # the table is replaced, not appended
    assert (root / "INDEX.md").read_text().count("<!-- knowlib:sources -->") == 1


def test_setup_separate_keeps_the_classic_layout(tmp_path):
    from knowlib.setup import init
    root = tmp_path / "lib"
    init(root, mode="separate", local_only=True, language="el", interactive=False)
    assert (root / "inbox").is_dir() and (root / "sources").is_dir() and (root / "index.md").exists()
    claude = (root / "CLAUDE.md").read_text()
    assert "{{" not in claude and "sources/<id>/source.md" in claude
    import yaml
    assert yaml.safe_load((root / "config.yaml").read_text())["local_only"] is True
    assert not (root / "_knowlib").exists() and (root / "config.example.yaml").exists()


def screenshot(path: Path, text: str) -> Path:
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (900, 240), "white")
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 40)
    except OSError:
        font = ImageFont.load_default()
    ImageDraw.Draw(img).text((30, 90), text, fill="black", font=font)
    img.save(path)
    return path


def test_a_folders_screenshots_become_one_source(folder):
    from knowlib import notecheck
    qs = folder / "Ch12 Qs"
    qs.mkdir()
    screenshot(qs / "Screenshot 1.png", "Question 1: Inventory turnover is 6.5")
    screenshot(qs / "Screenshot 2.png", "Question 2: Days to sell is 56")
    rep = process(folder)
    assert not rep["failed"], rep["failed"]
    assert "Ch12 Qs/*images" in [n["key"] if isinstance(n, dict) else n for n in rep["new"]] or \
        "Ch12 Qs/*images" in json.loads((folder / "_knowlib" / "manifest.json").read_text())["entries"]
    md = (folder / "Ch12 Qs" / "Ch12 Qs.images.md").read_text()
    assert "## Image 1: Screenshot 1.png" in md and "## Image 2: Screenshot 2.png" in md
    assert "../_knowlib/assets/" in md and not any(k.endswith(".png") for k in rep["unsupported"])
    sid = json.loads((folder / "_knowlib" / "manifest.json").read_text())["entries"]["Ch12 Qs/*images"]["source_id"]
    assert sid.startswith("ch12-qs-images-")
    (folder / "notes").mkdir(exist_ok=True)
    (folder / "notes" / "turnover.md").write_text(f"---\ntitle: Turnover\nsources: [{sid}]\n---\n# Turnover\n\n## Key points\n"
                                                   f"- Days to sell is 56 [@{sid}, image 2].\n"
                                                   f"- Days to sell is 56 [@{sid}, image 1].\n")
    (folder / "INDEX.md").write_text("# Index\n\n- [[turnover]]\n")
    msgs = [f.message for f in notecheck.run(folder, only=["citations"]) if f.level != "info"]
    assert len(msgs) == 1 and "image 1" in msgs[0] and "56" in msgs[0]
    assert process(folder)["new"] == []                       # unchanged images are not read again
    screenshot(qs / "Screenshot 3.png", "Question 3: FIFO")
    assert [u for u in process(folder)["updated"]]            # a new screenshot updates the folder's source


def test_output_with_a_long_frontmatter_is_never_ingested(folder):
    """A book with hundreds of flags has a long frontmatter; its Markdown must still be recognized as
    knowlib's own, both when the mark comes first (new files) and when it comes after every flag (older ones)."""
    from knowlib.layout import open_layout
    process(folder)
    old = folder / "old.pdf.md"
    old.write_text("---\nid: old-1\nflags:\n" + "".join(f"- 'flag {i} " + "x" * 80 + "'\n" for i in range(300))
                   + "generated_by: knowlib\n---\n# Old\n")
    lay = open_layout(folder)[0]
    assert lay.is_generated(old) and lay.is_generated(folder / "textbook.pdf.md")
    assert (folder / "textbook.pdf.md").read_text().splitlines()[2] == "generated_by: knowlib"
    assert not lay.is_generated(folder / "Lecturer" / "notes.md")
    rep = process(folder, force=True, only=["textbook.pdf"])
    assert "old.pdf.md" not in json.dumps(rep["new"] + rep["updated"]), rep
