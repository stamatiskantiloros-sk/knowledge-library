"""End-to-end: build a library from synthetic material with mock models and check the contract."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from PIL import Image

from knowlib.contract import read_frontmatter
from knowlib.manifest import Manifest
from knowlib.runner import process

from .fixtures import TEST_CONFIG, build_inbox

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")


@pytest.fixture(scope="module")
def lib(tmp_path_factory):
    root = tmp_path_factory.mktemp("library")
    info = build_inbox(root / "inbox", tmp_path_factory.mktemp("material"))
    (root / "config.yaml").write_text(TEST_CONFIG)
    report = process(root)
    return root, report, info


def _source(root: Path, report: dict, key_part: str) -> tuple[dict, str, Path]:
    entry = next(e for e in report["new"] if e["key"].endswith(key_part))
    d = root / "sources" / entry["source_id"]
    text = (d / "source.md").read_text(encoding="utf-8")
    return read_frontmatter(d / "source.md"), text, d


def test_report(lib):
    root, rep, _ = lib
    assert rep["failed"] == [], rep["failed"]
    assert any("archive.zip" in u for u in rep["unsupported"])
    keys = " ".join(e["key"] for e in rep["new"])
    for part in ("corporate-finance-l4/", "slides.pdf", "guest-talk.mp3", "regulatory-wacc-note.pdf",
                 "supplier-memo.docx", "kickoff.pptx", "tso-article.html"):
        assert part in keys
    # files are routed before the session, so the session can link its slide deck
    order = [e["key"] for e in rep["new"]]
    assert order.index(next(k for k in order if k.endswith("slides.pdf"))) < \
        order.index(next(k for k in order if k.endswith("l4/")))
    assert (root / ".work" / "last_run.json").exists()


def test_session_contract(lib):
    root, rep, info = lib
    fm, text, d = _source(root, rep, "corporate-finance-l4/")
    assert fm["source_type"] == "session"
    assert fm["title"] == "Corporate Finance - Lecture 4"
    assert fm["language"] == "en" and fm["tags"] == ["mba", "finance"]
    assert fm["original_date"] == "2026-10-28"
    for stage in ("session", "alignment", "transcription", "frames", "dedup", "frame_selection", "ocr_crosscheck"):
        assert stage in fm["pipeline"], stage
    assert fm["pipeline"]["session"]["primary_audio"] == "zoom_h1.m4a"
    # alignment recovered the camera lead
    assert abs(fm["pipeline"]["alignment"]["offset_s"] - info["lead_s"]) < 0.15
    slides_id = next(e["source_id"] for e in rep["new"] if e["key"].endswith("slides.pdf"))
    assert fm["related"] == [slides_id]
    assert (d / "assets" / "transcript.json").exists()


def test_session_slides(lib):
    root, rep, _ = lib
    fm, text, d = _source(root, rep, "corporate-finance-l4/")
    kept = fm["stats"]["kept"]
    assert 4 <= kept <= 5, kept                       # 5 slide states, speaker shot dropped
    assert fm["stats"]["selection"]["drop"] >= 1
    assert text.count("## Slide ") == kept
    assert "WACC" in text and "**Spoken:**" in text and "**On slide:**" in text
    # frames stored at native resolution, model view capped at 1568 px
    full = Image.open(d / "assets" / "frame_001.jpg")
    assert full.size == (1920, 1080)
    view = Image.open(d / "assets" / "view" / "frame_001.jpg")
    assert max(view.size) <= 1568
    assert "assets/view/frame_001.jpg" in text and "[full resolution](assets/frame_001.jpg)" in text
    # transcript was mapped onto the video timeline: first speech is not before 00:02
    tr = json.loads((d / "assets" / "transcript.json").read_text())
    assert tr[0]["start"] > 2.0


def test_scanned_deck_ocr(lib):
    root, rep, _ = lib
    fm, text, d = _source(root, rep, "slides.pdf")
    assert any("no text layer" in f for f in fm["flags"])
    assert fm["title"] == "Corporate Finance - Lecture 4 (slides)"
    assert text.count("Cost of capital") <= 2            # scanned pages are not OCR'd twice
    assert ("Scanned pages (OCR)" in text or "Scanned page images" in text)
    assert (d / "assets" / "page_001.png").exists() and "WACC" in text


def test_pdf_with_figure(lib):
    root, rep, _ = lib
    fm, text, d = _source(root, rep, "regulatory-wacc-note.pdf")
    assert fm["title"] == "Regulatory WACC note"
    assert (d / "assets" / "page_001.png").exists() and not (d / "assets" / "page_002.png").exists()
    assert "Page renders" in text
    assert fm["pipeline"]["extraction"]["tool"] in ("docling", "pymupdf")


def test_office(lib):
    root, rep, _ = lib
    fm, text, _ = _source(root, rep, "supplier-memo.docx")
    assert "| Price" in text and "40%" in text
    fm, text, _ = _source(root, rep, "kickoff.pptx")
    assert "## Slide 1: Project kickoff" in text and "Speaker notes:** Mention the budget" in text
    assert fm["title"] == "Project kickoff"


def test_web_and_audio(lib):
    root, rep, _ = lib
    fm, text, _ = _source(root, rep, "tso-article.html")
    assert fm["source_type"] == "web" and fm["title"] == "How gas TSOs earn returns"
    assert "Home | About" not in text                 # boilerplate removed
    assert text.count("# How gas TSOs earn returns") == 1
    fm, text, _ = _source(root, rep, "guest-talk.mp3")
    assert fm["source_type"] == "audio" and "## Transcript" in text and "[00:00]" in text


def test_rerun_is_incremental_and_reprocess(lib):
    root, rep, _ = lib
    again = process(root)
    assert again["new"] == [] and again["updated"] == [] and again["failed"] == []
    m = Manifest(root / "manifest.json")
    hits = m.match({"transcription.tool": "mock"})
    assert len(hits) == 2                             # the session and the mp3
    assert len(m.pending_synthesis()) == len(rep["new"])


def test_local_only_blocks_api(tmp_path):
    from .fixtures import ff, slide
    inbox = tmp_path / "lib" / "inbox"
    inbox.mkdir(parents=True)
    s = slide(tmp_path / "s.png", "Confidential", ["Internal figures"])
    ff("-loop", "1", "-t", "6", "-i", str(s), "-f", "lavfi", "-t", "6", "-i", "anoisesrc=a=0.1",
       "-vf", "fps=25,format=yuv420p", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(inbox / "board.mp4"))
    (tmp_path / "lib" / "config.yaml").write_text(
        "local_only: true\ntranscription: {backend: mock}\nframe_selection: {backend: api, api_model: x/y}\n")
    rep = process(tmp_path / "lib")
    assert rep["failed"] and "LocalOnlyViolation" in rep["failed"][0]["error"]
