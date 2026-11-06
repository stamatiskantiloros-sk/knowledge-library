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
    root = tmp_path / "Accounting"
    (root / "Ballas" / "week 1").mkdir(parents=True)
    book(root / "textbook.pdf")
    scanned(root / "Ballas" / "week 1" / "scan.pdf")
    (root / "Ballas" / "notes.md").write_text("# My notes\n\nFIFO sends the oldest costs to cost of goods sold.\n")
    (root / "_knowlib").mkdir()
    (root / "_knowlib" / "config.yaml").write_text(
        "library: {mode: inplace}\nframe_selection: {backend: mock}\npdf: {backend: pymupdf, fallbacks: []}\n"
        "digest: {min_pages: 3}\n")
    return root


def test_sources_land_beside_their_originals(folder):
    rep = process(folder)
    assert not rep["failed"], rep["failed"]
    for out in ("textbook.pdf.md", "Ballas/notes.md.md", "Ballas/week 1/scan.pdf.md"):
        text = (folder / out).read_text()
        assert "generated_by: knowlib" in text and f"original: {out[:-3]}" in text
    manifest = json.loads((folder / "_knowlib" / "manifest.json").read_text())["entries"]
    scan = manifest["Ballas/week 1/scan.pdf"]
    assert scan["output"] == "Ballas/week 1/scan.pdf.md"
    # the scan's page render lives under _knowlib/assets/<id>/ and its link climbs out of two folders
    render = folder / "_knowlib" / "assets" / scan["source_id"] / "page_001.png"
    assert render.exists()
    link = f"../../_knowlib/assets/{scan['source_id']}/page_001.png"
    assert link in (folder / "Ballas/week 1/scan.pdf.md").read_text()
    assert ((folder / "Ballas/week 1") / link).resolve() == render.resolve()
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
    (root / "Ballas").mkdir(parents=True)
    book(root / "Ballas" / "textbook.pdf")
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
    assert "<!-- knowlib:sources -->" in index and "[textbook.pdf.md](Ballas/textbook.pdf.md)" in index
    row = json.loads((root / "_knowlib" / "sources.jsonl").read_text().splitlines()[0])
    assert row["markdown"] == "Ballas/textbook.pdf.md" and row["size"] == "6 pages" and row["synthesized"] is False
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
