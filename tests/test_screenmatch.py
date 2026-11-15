"""Recorded screens matched to the document pages they show, and handbooks citing those pages."""
from pathlib import Path

from knowlib.handbook.maker import Slide, cite_sources, parse_source, pdf_name
from knowlib.media import screenmatch as sm


def doc(label, pages, unit="slide"):
    return sm.Doc(Path(f"{label}.pdf"), label, unit, [sm.words(p) for p in pages])


DECK = ["Operations management outline design quality deployment group exercise",
        "Product service design integrated process marketing engineering manufacturing functions",
        "Key attributes four products development efforts screwdriver printer car aircraft",
        "Key attributes four products development efforts screwdriver printer car aircraft "
        "supplier pyramid tier suppliers original equipment manufacturer",
        "Facility layout goals workers equipment process entity operate effectively"]


def test_default_labels_name_decks_by_their_numbering():
    assert sm.default_label(Path("2-2_LocationLayout-iMBA_26-27-Students.pdf")) == "deck 2-2"
    assert sm.default_label(Path("1_Intro-Concepts.pdf")) == "deck 1"
    assert sm.default_label(Path("ProductivityAssignment-Operations-2026.pdf")) == "Productivity Assignment Operations"


def test_cite_compresses_pages_into_runs():
    d = doc("deck 2-1", DECK)
    assert d.cite([3]) == "deck 2-1, slide 3"
    assert d.cite([4, 3, 5, 9, 3]) == "deck 2-1, slides 3–5, 9"


def test_the_viewers_page_counter_decides():
    a, b = doc("deck 2-1", DECK), doc("deck 2-2", DECK[:3])
    got = sm.match_screens([("nothing useful here", "Chrome File Edit  4 / 5  84%")], [a, b])
    assert (got[0].doc, got[0].page, got[0].how) == (a, 4, "page counter")
    # a count no document has, a page past the end, and a date are not counters
    assert sm.match_screens([("", "13/11 7 / 9 12 / 5")], [a, b]) == [None]


def test_text_match_prefers_the_build_the_screen_shows():
    d = doc("deck 2-1", DECK)
    early, full = sm.match_screens([
        ("Key attributes of four products and their development efforts: screwdriver, printer, car, aircraft", ""),
        ("Key attributes four products development efforts screwdriver printer car aircraft "
         "supplier pyramid tier suppliers original equipment manufacturer", "")], [d])
    assert early.page == 3 and full.page == 4 and early.how == "text"


def test_unrelated_screens_match_nothing():
    d = doc("deck 2-1", DECK)
    assert sm.match_screens([("Course news forum syllabus VPN instructions library link", "")], [d]) == [None]


def test_a_screen_between_two_matches_of_one_page_takes_that_page():
    d = doc("deck 2-1", DECK)
    screens = [("", "2 / 5"), ("product design integrated process marketing engineering manufacturing plus a long table of "
                               "activities concept planning detail testing refinement ramp production "
                               "articulate market opportunity collect customer needs identify lead users",
                               ""), ("", "2 / 5")]
    got = sm.match_screens(screens, [d])
    assert [m.page for m in got] == [2, 2, 2] and got[1].how == "between neighbours"


def _lecture(tmp: Path) -> Path:
    import pymupdf
    pdf = pymupdf.open()
    for text in DECK:
        page = pdf.new_page(width=800, height=450)            # landscape: slides
        page.insert_text((40, 60), text, fontsize=9)
    pdf.save(tmp / "2-1_Design.pdf")
    from PIL import Image
    assets = tmp / "assets"
    assets.mkdir()
    for i in (1, 2):
        Image.new("RGB", (40, 30), "white").save(assets / f"frame_{i:03d}.jpg")
    md = tmp / "lecture.md"
    md.write_text("---\ntitle: 'Lecture 2: Design'\noriginal_date: '2026-11-13'\n---\n\n# Lecture 2: Design\n\n"
                  "## Slide 1 (00:00–01:00)\n![Slide 1](assets/frame_001.jpg)\n\n**On slide:**\n\nFacility layout goals\n\n"
                  "**Spoken:**\n\nHere we go.\n\n"
                  "## Slide 2 (01:00–02:00)\n![Slide 2](assets/frame_002.jpg)\n\n**On slide:**\n\nCourse news forum\n\n"
                  "**Spoken:**\n\nMoodle.\n", encoding="utf-8")
    return md


def test_relabel_writes_from_lines_and_page_images(tmp_path):
    md = _lecture(tmp_path)
    r = sm.relabel(md, tmp_path, ocr=lambda p: "5 / 5" if p.stem == "frame_001" else "Moodle")
    assert r == {"screens": 2, "page counter": 1, "none": 1}
    text = md.read_text(encoding="utf-8")
    assert "**From:** deck 2-1, slide 5 · 2-1_Design.pdf p. 5 (page counter)" in text
    assert (tmp_path / "assets" / "pages" / "2-1_Design-005.png").exists()
    sm.relabel(md, tmp_path, ocr=lambda p: "5 / 5" if p.stem == "frame_001" else "Moodle")   # redone, not doubled
    assert md.read_text(encoding="utf-8").count("**From:**") == 1
    lec = parse_source(md)
    assert (lec.slides[0].cite, lec.slides[0].document, lec.slides[0].page) == ("deck 2-1, slide 5", "2-1_Design.pdf", 5)
    assert lec.slides[0].page_image == "assets/pages/2-1_Design-005.png" and not lec.slides[1].cite
    assert "· deck 2-1, slide 5]" in lec.slides[0].material()


def slides():
    return [Slide(87, "1:48:08", "1:49:39", cite="deck 2-2, slide 16"),
            Slide(88, "1:49:39", "1:50:59", cite="deck 2-2, slide 17"),
            Slide(89, "1:50:59", "1:52:38", cite="deck 2-2, slide 17"),
            Slide(93, "1:53:07", "1:53:10"),
            Slide(6, "06:49", "13:19", cite="deck 2-1, slide 3")]


def test_citations_name_the_document_page_not_the_screen():
    text = ("A fact (slide 87).\nTwo (slides 87–89).\n*Slides 87–89, 93*\n![Slide 87, 1:48:08](a.png)\n"
            "Table: Slide 6 (Ulrich & Eppinger, 2020)\nMixed (slides 6–7; deck 2-1, slide 4).")
    out = cite_sources(text, slides(), show_times=False)
    assert out.splitlines() == ["A fact (deck 2-2, slide 16).", "Two (deck 2-2, slides 16–17).",
                                "*Deck 2-2, slides 16–17*", "![Deck 2-2, slide 16](a.png)",
                                "Table: Deck 2-1, slide 3 (Ulrich & Eppinger, 2020)", "Mixed (deck 2-1, slides 3–4)."]
    assert "Shown in class at 1:53:07" in cite_sources("*Slide 93*", slides(), show_times=True)


def test_lectures_without_matched_screens_keep_their_slide_numbers():
    plain = [Slide(22, "10:00", "12:00")]
    assert cite_sources("A fact (slide 22).\n*Slides 20–22*", plain) == "A fact (slide 22).\n*Slides 20–22*"


def test_pdf_is_named_by_course_lecture_and_date():
    assert pdf_name("Lecture 2: Product, Service and Process Design", "2026-11-13", "Operations Management") == \
        "Operations Management - Lecture 2 - 2026-11-13.pdf"
    assert pdf_name("Session 3: WACC", "2026-11-12", "Corporate Finance") == "Corporate Finance - Session 3 - 2026-11-12.pdf"
    assert pdf_name("Operations Management review", "", "Operations Management") == "Operations Management review.pdf"


def test_a_range_leaves_out_screens_only_passed_through():
    ss = [Slide(49, "1:06:35", "1:06:49", cite="deck 2-1, slide 29"),      # 14 s: a flick back
          Slide(50, "1:06:49", "1:19:43", cite="deck 2-1, slide 26"),
          Slide(51, "1:19:43", "1:19:45"),                                  # 2 s, no document
          Slide(52, "1:19:45", "1:21:00", cite="pen specification, slide 1")]
    assert cite_sources("*Slides 49–52*", ss, False) == "*Deck 2-1, slide 26; pen specification, slide 1*"
    assert cite_sources("(slide 49)", ss, False) == "(deck 2-1, slide 29)"           # named on its own: kept
    assert cite_sources("(slide 51)", ss, True) == "(shown in class at 1:19:43)"


def test_a_short_screen_of_the_same_run_stays_in_the_range():
    ss = [Slide(13, "28:00", "28:59", cite="deck 2-1, slide 10"),
          Slide(14, "28:59", "29:18", cite="deck 2-1, slide 11"),          # 19 s, the next page: kept
          Slide(15, "29:18", "31:00", cite="deck 2-1, slide 12"),
          Slide(16, "31:00", "31:05", cite="deck 2-2, slide 1")]           # 5 s, another deck: left out
    assert cite_sources("*Slides 13–16*", ss, False) == "*Deck 2-1, slides 10–12*"
