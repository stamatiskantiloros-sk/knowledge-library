"""Unit tests for knowlib.handbook.figures. No real model and no network: providers are fakes."""
import json
import shutil

import pytest
from PIL import Image

from knowlib.handbook import figures

HAS_DOT = shutil.which("dot") is not None
HAS_RENDER = HAS_DOT and shutil.which("rsvg-convert") is not None
needs_dot = pytest.mark.skipif(not HAS_DOT, reason="graphviz 'dot' is not installed")
needs_render = pytest.mark.skipif(not HAS_RENDER, reason="graphviz 'dot' or 'rsvg-convert' is not installed")

LABELS = ["Inputs", "Operative activities and processes", "12%"]

VALID_DOT = """digraph G {
  graph [fontname="Georgia"];
  node [shape=box style="rounded,filled" fillcolor="#f3eff0" fontname="Georgia"];
  a [label="Inputs"];
  b [label="Operative activities\\nand processes"];
  c [label="12%"];
  a -> b -> c;
}
"""
MISSING_DOT = """digraph G {
  node [shape=box fontname="Georgia"];
  a [label="Inputs"];
  b [label="Operative activities\\nand processes"];
  a -> b;
}
"""
BROKEN_DOT = "digraph G { a -> ; ->\n"
OK_CHECK = '{"ok": true, "issues": []}'


class Fake:
    """A provider stand-in. Replies are returned in order; the last one repeats."""

    def __init__(self, replies, answered_by="fake-model"):
        self.replies = list(replies)
        self.answered_by = answered_by
        self.calls = 0
        self.prompts = []

    def generate(self, images, prompt, json_mode=False):
        self.calls += 1
        self.prompts.append(prompt)
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


def cfg(**figs):
    return {"handbook": {"figures": {"revisions": 1, "compile_retries": 2, **figs}}}


def make_folder(tmp_path, idea="Inputs feed operative activities", slide_image=None, name="fig_025_flow"):
    d = tmp_path / name
    d.mkdir()
    brief = {"slide": 25, "slide_image": str(slide_image) if slide_image else "", "idea": idea,
             "form": "flow", "tool": "graphviz", "labels": LABELS, "layout": "left to right",
             "emphasis": "operative activities"}
    (d / "brief.json").write_text(json.dumps(brief))
    return d


@pytest.fixture
def slide(tmp_path):
    path = tmp_path / "slide_025.png"
    Image.new("RGB", (40, 30), "white").save(path)
    return path


# 1. norm and missing_labels

def test_norm_treats_curly_and_straight_quotes_alike():
    assert figures.norm("Farmers’ share “maize”") == figures.norm("Farmers' share \"maize\"")


def test_norm_treats_en_dash_and_minus_as_hyphen():
    assert figures.norm("Land–labour") == figures.norm("Land-labour")
    assert figures.norm("Profit − cost") == figures.norm("Profit - cost")


def test_norm_collapses_whitespace_and_case():
    assert figures.norm("  Operative\n  ACTIVITIES ") == "operative activities"


def test_missing_labels_accepts_quote_and_dash_variants():
    text = "Farmers’ inputs – 12% Inputs"
    assert figures.missing_labels(text, ["Farmers' inputs - 12%", "Inputs"]) == []


def test_missing_labels_accepts_a_label_split_over_lines():
    text = "Operative activities\nand processes"
    assert figures.missing_labels(text, ["Operative activities and processes"]) == []


def test_missing_labels_accepts_a_hyphenated_word_split_over_lines():
    assert figures.missing_labels("Land-\nlabour", ["Land-labour"]) == []


def test_missing_labels_ignores_case():
    assert figures.missing_labels("OPERATIVE ACTIVITIES", ["operative activities"]) == []


def test_missing_labels_reports_only_the_genuinely_missing_label():
    text = "Inputs Operative activities\nand processes"
    assert figures.missing_labels(text, ["Inputs", "Operative activities and processes", "Yield 40%"]) == ["Yield 40%"]


# 2. strip_fences

def test_strip_fences_removes_a_dot_fence():
    assert figures.strip_fences("Here you go:\n```dot\ndigraph G { a -> b }\n```\n") == "digraph G { a -> b }\n"


def test_strip_fences_leaves_plain_code_alone_but_ends_it_with_a_newline():
    assert figures.strip_fences("digraph G { a -> b }") == "digraph G { a -> b }\n"


# 3. unsafe_python

@pytest.mark.parametrize("code", [
    "import numpy as np\nimport matplotlib.pyplot as plt\nx = np.arange(3)\n"
    "fig, ax = plt.subplots(figsize=(3.4, 2))\nax.plot(x)\n",
    "import matplotlib.pyplot as plt\nimport numpy\n",
])
def test_unsafe_python_allows_matplotlib_and_numpy(code):
    assert figures.unsafe_python(code) is None


@pytest.mark.parametrize("code", [
    "import os",
    "open('x')",
    "from pathlib import Path",
    "__import__('os')",
    "plt.savefig('a.png')",
])
def test_unsafe_python_rejects_files_imports_and_saving(code):
    assert figures.unsafe_python(code) is not None


def test_unsafe_python_allows_a_from_import_of_numpy():
    assert figures.unsafe_python("from numpy import linspace\n") is None


def test_unsafe_python_rejects_a_forbidden_module_after_an_allowed_one():
    assert figures.unsafe_python("import numpy, os") is not None


def test_unsafe_python_rejects_reflection_and_dunders():
    assert figures.unsafe_python("x = getattr(plt, 'savefig')") is not None
    assert figures.unsafe_python("plt.__class__.__bases__") is not None
    assert figures.unsafe_python("from . import x") is not None


# 4. svg_text

def test_svg_text_strips_tags_and_style_blocks_and_unescapes_ampersands():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg"><style>text { font-family: Georgia; }</style>'
           '<title>G</title><text>Tom &amp; Jerry</text><text>Operative &#39;work&#39;</text></svg>')
    text = figures.svg_text(svg)
    assert "Tom & Jerry" in text
    assert "Operative 'work'" in text
    assert "font-family" not in text
    assert "<" not in text and "&amp;" not in text


# 5. render (graphviz)

@needs_render
def test_render_graphviz_writes_svg_and_png_and_returns_the_labels(tmp_path):
    (tmp_path / "fig.dot").write_text(VALID_DOT)
    text = figures.render(tmp_path, "graphviz", {})
    assert (tmp_path / "fig.svg").exists()
    assert (tmp_path / "fig.png").exists()
    assert "Inputs" in text
    assert "Operative activities" in text and "and processes" in text
    assert "12%" in text


@needs_dot
def test_render_graphviz_raises_figure_error_for_broken_dot(tmp_path):
    (tmp_path / "fig.dot").write_text(BROKEN_DOT)
    with pytest.raises(figures.FigureError):
        figures.render(tmp_path, "graphviz", {})


# 6. Maker.make with fake providers

def providers(writer, reviser=None, checker=None):
    return {"writer": writer,
            "reviser": reviser or Fake([VALID_DOT], "fake-reviser"),
            "checker": checker or Fake([OK_CHECK], "fake-checker")}


@needs_render
def test_make_passes_when_the_writer_gets_everything_right(tmp_path, slide):
    d = make_folder(tmp_path, slide_image=slide)
    writer = Fake([VALID_DOT], "fake-writer")
    checker = Fake([OK_CHECK], "fake-checker")
    reviser = Fake([VALID_DOT])
    report = figures.Maker(cfg(), names=["Maria Papadopoulos"],
                           providers=providers(writer, reviser, checker)).make(d)
    assert report["result"] == "passed checks"
    assert json.loads((d / "report.json").read_text())["result"] == "passed checks"
    assert writer.calls == 1
    assert reviser.calls == 0
    assert report["rounds"][0]["model"] == "fake-writer"
    assert report["rounds"][0]["missing_labels"] == []
    assert (d / "fig.png").exists()


@needs_render
def test_make_calls_the_reviser_when_a_label_is_missing(tmp_path, slide):
    d = make_folder(tmp_path, slide_image=slide)
    writer = Fake([MISSING_DOT], "fake-writer")
    reviser = Fake([VALID_DOT], "fake-reviser")
    report = figures.Maker(cfg(), names=["Maria Papadopoulos"],
                           providers=providers(writer, reviser)).make(d)
    assert report["result"] == "passed checks"
    assert writer.calls == 1
    assert reviser.calls == 1
    assert report["rounds"][0]["missing_labels"] == ["12%"]
    assert report["rounds"][1]["model"] == "fake-reviser"


@needs_render
def test_make_retries_after_broken_code_and_records_the_error(tmp_path, slide):
    d = make_folder(tmp_path, slide_image=slide)
    writer = Fake([BROKEN_DOT, VALID_DOT], "fake-writer")
    report = figures.Maker(cfg(), names=["Maria Papadopoulos"],
                           providers=providers(writer)).make(d)
    assert report["result"] == "passed checks"
    assert writer.calls == 2
    assert any("error" in r for r in report["rounds"])
    assert "Your previous code failed" in writer.prompts[1]


@needs_render
def test_make_stops_without_calling_the_writer_when_the_brief_names_a_person(tmp_path, slide):
    d = make_folder(tmp_path, idea="Survey by Maria Papadopoulos of the co-op", slide_image=slide)
    writer = Fake([VALID_DOT])
    checker = Fake([OK_CHECK])
    report = figures.Maker(cfg(), names=["Maria Papadopoulos"],
                           providers=providers(writer, checker=checker)).make(d)
    assert report["result"].startswith("stopped:")
    assert writer.calls == 0
    assert checker.calls == 0
    assert not (d / "fig.png").exists()
    assert not (d / "fig.svg").exists()
    assert (d / "report.json").exists()


@needs_render
def test_make_reports_an_unclear_issue_when_the_checker_reply_is_not_json(tmp_path, slide):
    d = make_folder(tmp_path, slide_image=slide)
    writer = Fake([VALID_DOT])
    checker = Fake(["the figure looks fine to me"], "fake-checker")
    report = figures.Maker(cfg(revisions=0), names=["Maria Papadopoulos"],
                           providers=providers(writer, checker=checker)).make(d)
    issues = report["rounds"][0]["compare"]["issues"]
    assert issues[0]["kind"] == "unclear"
    assert report["rounds"][0]["compare"]["ok"] is False
    assert report["result"] == "issues left"


# 7. sheet

def test_sheet_writes_review_html_with_both_folders_and_the_verdict(tmp_path):
    judged = make_folder(tmp_path, name="fig_025_flow")
    (judged / "fig.png").write_bytes(b"\x89PNG")
    (judged / "verdict.json").write_text(json.dumps({"verdict": "accept", "reason": "Matches the slide"}))
    make_folder(tmp_path, name="fig_031_table")
    out = figures.sheet(tmp_path)
    assert out == tmp_path / "review.html"
    page = out.read_text()
    assert "fig_025_flow" in page
    assert "fig_031_table" in page
    assert "accept" in page
    assert "Matches the slide" in page
    assert "not judged" in page


# 8. The finish step places what the user approved instead of the slide screenshot

def test_finish_places_approved_figures_tables_and_drops(tmp_path):
    import json as _json
    from PIL import Image
    from knowlib.handbook.maker import Lecture, Slide, _place_figures, without_times
    for n in (1, 2, 3, 4):
        Image.new("RGB", (8, 6), "white").save(tmp_path / f"slide_{n:03d}.png")
    slides = [Slide(n, "0:00", "1:00", frame=f"slide_{n:03d}.png") for n in (1, 2, 3, 4)]
    lec = Lecture("lec", "L/", "Lecture 1", tmp_path, slides)
    out = tmp_path / "out"
    figs = out / "figures"
    for name, n, verdict, extra in [("01-flow", 1, "accept", {"width": "wide"}), ("02-table", 2, "table", {}),
                                    ("03-gone", 3, "drop", {})]:
        d = figs / name
        d.mkdir(parents=True)
        (d / "brief.json").write_text(_json.dumps({"slide": n, **extra}))
        (d / "verdict.json").write_text(_json.dumps({"verdict": verdict, "reason": "checked"}))
    Image.new("RGB", (10, 10), "white").save(figs / "01-flow" / "fig.png")
    (figs / "02-table" / "table.md").write_text("| A | B |\n| - | - |\n| 1 | 2 |\n")
    issues = []
    text = _place_figures("[[slide 1]]\n\n[[slide 2]]\n\n[[slide 3]]\n\n[[slide 4]]", slides, lec, out, issues)
    assert "![Slide 1, 0:00](assets/fig_001.png){.kl-fig .wide}" in text      # drawn figure, wide
    assert "| 1 | 2 |" in text and "Table: Slide 2, 0:00" in text            # table with its caption
    assert "slide_003" not in text and "Slide 3" not in text                  # dropped
    assert "![Slide 4, 0:00](assets/slide_004.png)" in text                   # no figure: the slide
    assert (out / "assets" / "fig_001.png").exists() and not issues
    assert "Table: Slide 2\n" in without_times(text) + "\n"


def test_finish_places_typst_figures_as_svg_and_keeps_a_png_for_the_privacy_check(tmp_path):
    import json as _json
    from PIL import Image
    from knowlib.handbook.maker import Lecture, Slide, _place_figures
    Image.new("RGB", (8, 6), "white").save(tmp_path / "slide_001.png")
    slides = [Slide(1, "0:00", "1:00", frame="slide_001.png")]
    lec = Lecture("lec", "L/", "Lecture 1", tmp_path, slides)
    out = tmp_path / "out"
    d = out / "figures" / "01-cross"
    d.mkdir(parents=True)
    (d / "brief.json").write_text(_json.dumps({"slide": 1, "tool": "typst"}))
    (d / "verdict.json").write_text(_json.dumps({"verdict": "accept", "reason": "checked"}))
    Image.new("RGB", (10, 10), "white").save(d / "fig.png")
    (d / "fig.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>')
    issues = []
    text = _place_figures("[[slide 1]]", slides, lec, out, issues)
    assert "![Slide 1, 0:00](assets/fig_001.svg){.kl-fig}" in text and not issues
    assert (out / "assets" / "fig_001.svg").exists() and (out / "assets" / "fig_001.png").exists()


def test_privacy_check_reads_the_png_beside_a_placed_svg(tmp_path, monkeypatch):
    from knowlib.handbook import maker
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "fig_001.svg").write_text("<svg/>")
    (tmp_path / "assets" / "fig_001.png").write_bytes(b"png")
    (tmp_path / "handbook.md").write_text("# H\n\n![Slide 1](assets/fig_001.svg)\n")
    read = []
    monkeypatch.setattr("knowlib.media.ocr.apple_vision", lambda path, oc: read.append(path.name) or "ab@example.org")
    got = maker.personal_information(tmp_path, {})
    assert read == ["fig_001.png"] and any("fig_001.svg shows personal information" in g for g in got)
