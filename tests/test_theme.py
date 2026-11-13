"""The handbook's look: handbook.theme, set by `knowlib style`."""
import shutil

import pytest
import yaml

from knowlib.handbook import figures, theme

NOIR = ('palette="Ink and vermilion" main=#111111 accent=#D7301F tint=#f3f3f3 body_font="Georgia" '
        'heading_font="Didot" headings=block table_header=line boxes=square box_fill=outline line_weight=fine')


def test_resolve_fills_defaults_and_rejects_bad_values():
    t = theme.resolve({"main": "#112233", "heading_font": ""})
    assert t["main"] == "#112233" and t["accent"] == theme.DEFAULT["accent"] and t["heading_font"] is None
    for bad in ({"main": "red"}, {"boxes": "wavy"}, {"body_font": 'Georgia"; } body { color: red'}, {"colour": "#111111"}):
        with pytest.raises(ValueError):
            theme.resolve(bad)


def test_parse_reads_the_pickers_settings_line():
    t = theme.parse(NOIR)
    assert t["palette"] == "Ink and vermilion" and t["heading_font"] == "Didot" and t["boxes"] == "square"
    assert theme.resolve(t)["accent"] == "#d7301f"                      # colours are stored in lower case
    with pytest.raises(ValueError):
        theme.parse("main")


def test_css_carries_colours_fonts_and_the_chosen_variants():
    out = theme.css(theme.parse(NOIR))
    assert "--main: #111111" in out and "--accent: #d7301f" in out
    assert 'h1, h2, .kl-byline { font-family: "Didot", "Georgia"' in out
    assert "h2::before { content: counter(sec); background: var(--main)" in out            # block headings
    assert "th { background: none; color: var(--main)" in out                               # underlined table header
    plain = theme.css({})
    assert "h2::before" not in plain and "border-bottom" not in plain


def test_typst_style_follows_the_theme_and_is_marked_as_generated():
    out = theme.typst_style(theme.parse(NOIR))
    assert out.startswith(theme.MARK) and "{{" not in out
    assert 'rgb("#111111")' in out and "#let corner = 0" in out and '#let box-fill = "outline"' in out
    assert "#let line-w = 0.4pt" in out and '("Georgia", "Georgia")' in out
    assert "#let pill = true" in theme.typst_style({"boxes": "pill"})


def test_sync_figure_style_leaves_a_hand_written_file_alone(tmp_path):
    assert theme.sync_figure_style(tmp_path, None) == tmp_path / "style.typ"       # missing: written
    assert theme.sync_figure_style(tmp_path, None) is None                          # up to date
    assert theme.sync_figure_style(tmp_path, {"main": "#112233"}) is not None       # generated: refreshed
    (tmp_path / "style.typ").write_text("// my own style\n")
    assert theme.sync_figure_style(tmp_path, {"main": "#112233"}) is None
    assert (tmp_path / "style.typ").read_text() == "// my own style\n"
    assert theme.sync_figure_style(tmp_path, None, force=True) is not None


@pytest.mark.skipif(not shutil.which("typst"), reason="typst is not installed")
def test_a_figure_renders_with_the_generated_style(tmp_path):
    d = tmp_path / "01-box"
    d.mkdir()
    (d / "fig.typ").write_text('#import "../style.typ": *\n#show: figure-page\n'
                               '#cetz.canvas(length: 1mm, {\n  node((0, 0), 24, 8, [Stamping])\n'
                               '  arrow((12, 0), (18, 0))\n  node((30, 0), 24, 8, [Weld], kind: "key")\n})\n')
    fs = figures.settings({"handbook": {"theme": {"boxes": "pill", "box_fill": "solid"}}})
    text = figures.render(d, "typst", fs)
    assert "Stamping" in text and "Weld" in text and (d / "fig.png").exists()
    assert "#let pill = true" in (tmp_path / "style.typ").read_text()


def test_figure_settings_take_colours_and_font_from_the_theme():
    cfg = {"handbook": {"figures": {"main": "#5a0f1b", "font": "Georgia", "revisions": 1},
                        "theme": {"main": "#1f3d2f", "body_font": "Charter"}}}
    fs = figures.settings(cfg)
    assert fs["main"] == "#1f3d2f" and fs["font"] == "Charter" and fs["revisions"] == 1
    assert figures.settings({"handbook": {"figures": {"main": "#5a0f1b"}}})["main"] == "#5a0f1b"


def test_save_adds_and_replaces_the_theme_without_touching_the_rest(tmp_path):
    cfg = tmp_path / "_knowlib" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text("# my library\nlibrary:\n  ignore: [old]   # keep out\n\nhandbook:      # how handbooks look\n"
                   "  speaker: the professor   # not the lecturer\n  show_times: false\n\nprivacy:\n  names: [Maria]\n")
    theme.save(tmp_path, theme.parse(NOIR))
    text = cfg.read_text()
    assert "# keep out" in text and "# not the lecturer" in text and text.index("theme:") < text.index("privacy:")
    got = yaml.safe_load(text)
    assert got["handbook"]["theme"]["heading_font"] == "Didot" and got["handbook"]["speaker"] == "the professor"
    assert got["privacy"] == {"names": ["Maria"]} and cfg.with_suffix(".yaml.bak").exists()
    theme.save(tmp_path, {"main": "#1f3d2f"})                            # a second choice replaces the first
    got = yaml.safe_load(cfg.read_text())
    assert got["handbook"]["theme"]["main"] == "#1f3d2f" and "heading_font" not in got["handbook"]["theme"]
    assert cfg.read_text().count("theme:") == 1 and got["handbook"]["show_times"] is False


def test_save_makes_a_handbook_section_when_there_is_none(tmp_path):
    cfg = tmp_path / "_knowlib" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text("library:\n  ignore: [old]\n")
    theme.save(tmp_path, {"main": "#1f3d2f"})
    got = yaml.safe_load(cfg.read_text())
    assert got["handbook"]["theme"]["main"] == "#1f3d2f" and got["library"] == {"ignore": ["old"]}


def test_picker_page_carries_the_library_and_its_current_theme(tmp_path):
    (tmp_path / "_knowlib").mkdir()
    page = theme.picker(tmp_path, {"handbook": {"theme": {"main": "#1f3d2f", "headings": "rule"}}}).read_text()
    assert "KNOWLIB_" not in page and '"main": "#1f3d2f"' in page and '"headings": "rule"' in page
    assert str(tmp_path) in page and "knowlib style" in page
