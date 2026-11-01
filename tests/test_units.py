from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest

from knowlib.contract import slugify
from knowlib.media.ocr import compare
from knowlib.media.transcribe import clean_repetitions, glossary_prompt, parse_subtitles
from knowlib.providers.base import parse_json
from knowlib.router import discover


def test_slugify_greek():
    assert slugify("Εταιρική Χρηματοοικονομική — Διάλεξη 4") == "etairiki-chrimatooikonomiki-dialexi-4"


def test_merge_repeats_needs_same_text_and_image(tmp_path):
    import numpy as np
    from PIL import Image, ImageDraw
    from knowlib.media.select import Kept, _merge_repeats

    def slide(name, dot, chart=False):
        img = Image.fromarray((np.random.default_rng(1).random((360, 640)) * 60 + 180).astype("uint8"))
        d = ImageDraw.Draw(img)
        d.ellipse([dot, 20, dot + 4, 24], fill=0)
        if chart:
            d.rectangle([100, 100, 500, 300], fill=0)
        path = tmp_path / name
        img.save(path, "PNG")
        return Kept(start=0, end=1, frame=str(path), caption="", slide_text="Same title", content_type="slide", reason="")

    a, b, c = slide("a.png", 10), slide("b.png", 300), slide("c.png", 300, chart=True)
    a.caption, b.start, b.candidates, c.start = "first", 5, [1], 9
    out, merged = _merge_repeats([a, b, c], {})
    assert merged == 1 and [k.start for k in out] == [0, 9]
    assert out[0].caption == "first" and out[0].frame == b.frame


def test_empty_slide_text_falls_back_to_ocr():
    from knowlib.media.select import Kept, fill_text_from_ocr

    empty = Kept(start=0, end=1, frame="f", caption="", slide_text="", content_type="demo", reason="")
    empty.ocr = {"backends": {"apple-vision": {"text": "rows 3"}}, "flag": True}
    fill_text_from_ocr(empty)
    assert (empty.slide_text, empty.text_source, empty.ocr["flag"]) == ("rows 3", "ocr", False)

    read = Kept(start=0, end=1, frame="f", caption="", slide_text="Title", content_type="slide", reason="")
    read.ocr = {"backends": {"apple-vision": {"text": "other"}}, "flag": True}
    fill_text_from_ocr(read)
    assert (read.slide_text, read.text_source, read.ocr["flag"]) == ("Title", "vlm", True)


def test_frame_follows_the_sample_that_shows_the_text(monkeypatch):
    from knowlib.media import select as sel

    sims = {"pane.jpg": 0.9, "nopane.jpg": 0.2}
    monkeypatch.setattr(sel, "crosscheck", lambda p, text, cfg: {"backends": {"o": {"similarity": sims[p.name]}}})
    k = sel.Kept(start=0, end=1, frame="nopane.jpg", caption="", slide_text="START TIME", content_type="demo",
                 reason="", samples=["pane.jpg", "nopane.jpg"])
    sel.match_frame_to_text(k, {})
    assert k.frame == "pane.jpg"


def test_split_prompt_decides_transcribes_then_captions(tmp_path):
    import json
    from PIL import Image
    from knowlib.config import DEFAULTS, deep_merge
    from knowlib.media.frames import Candidate
    from knowlib.media import select as sel
    from knowlib.providers import base

    class Fake(base.VisionProvider):
        backend, model = "fake", "m"

        def __init__(self):
            self.calls = []

        def generate(self, images, prompt, *, json_mode=False, max_tokens=None):
            self.calls.append((len(images), json_mode))
            if json_mode:
                return json.dumps({"decision": "keep", "best_frame": 1, "reason": "r", "content_type": "table"})
            return "```markdown\n| a |\n|---|\n| 1 |\n```"

    class F:
        def __init__(self, path):
            self.path = path

    paths = [tmp_path / "a.jpg", tmp_path / "b.jpg"]
    for p in paths:
        Image.new("RGB", (64, 64)).save(p)
    cfg = deep_merge(DEFAULTS, {"frame_selection": {"split": True}})
    fake = Fake()
    kept, _, _ = sel.select([Candidate(index=0, start=0, end=5, samples=[F(p) for p in paths])], [], fake, cfg, tmp_path)
    assert fake.calls == [(2, True), (1, False)]
    assert kept[0].frame == str(paths[1]) and kept[0].slide_text == "| a |\n|---|\n| 1 |" and kept[0].caption == ""


def test_only_text_slides_that_add_to_the_last_one_replace_it():
    from knowlib.media.select import Kept, _builds_on

    prev = Kept(start=0, end=1, frame="f", caption="", slide_text="Step 3 degrees of freedom", content_type="slide", reason="")
    assert _builds_on(prev, "Step 3 degrees of freedom\ndf = 2", "slide")
    assert not _builds_on(prev, "Step 4 critical value\nalpha 0.05 df 2", "slide")
    assert not _builds_on(prev, "Step 3 degrees of freedom\ndf = 2", "chart")


def test_sample_with_the_most_text_is_chosen(monkeypatch, tmp_path):
    from knowlib.config import DEFAULTS
    from knowlib.media import select as sel

    class F:
        def __init__(self, name):
            self.path = name

    monkeypatch.setitem(sel.BACKENDS, "apple-vision", lambda p, oc: {"a": "x", "b": "x y z", "c": "x y z"}[p])
    assert sel._richest_sample([F("a"), F("b"), F("c")], DEFAULTS).path == "c"


def test_cut_off_transcription_is_dropped(tmp_path):
    import json
    from PIL import Image
    from knowlib.config import DEFAULTS, deep_merge
    from knowlib.media.frames import Candidate
    from knowlib.media import select as sel
    from knowlib.providers import base

    class Loop(base.VisionProvider):
        backend, model, last_finish = "fake", "m", None

        def generate(self, images, prompt, *, json_mode=False, max_tokens=None):
            if json_mode:
                return json.dumps({"decision": "keep", "best_frame": 0, "reason": "r", "content_type": "demo"})
            self.last_finish = "length"
            return "\n" * 500

    class F:
        path = tmp_path / "a.jpg"

    Image.new("RGB", (64, 64)).save(F.path)
    cfg = deep_merge(DEFAULTS, {"frame_selection": {"split": True}})
    kept, _, _ = sel.select([Candidate(index=0, start=0, end=5, samples=[F])], [], Loop(), cfg, tmp_path)
    assert kept[0].slide_text == ""


def test_all_decisions_come_before_the_reader_loads(monkeypatch, tmp_path):
    import json
    from PIL import Image
    from knowlib.config import DEFAULTS, deep_merge
    from knowlib.media.frames import Candidate
    from knowlib.media import select as sel
    from knowlib.providers import base

    order = []

    class Decider(base.VisionProvider):
        backend, model = "fake", "decider"

        def generate(self, images, prompt, *, json_mode=False, max_tokens=None):
            order.append("decide")
            return json.dumps({"decision": "keep", "best_frame": 0, "reason": "r", "content_type": "demo"})

    class Reader(base.VisionProvider):
        backend, model = "fake", "reader"

        def generate(self, images, prompt, *, json_mode=False, max_tokens=None):
            order.append("read")
            return "text"

    class F:
        def __init__(self, path):
            self.path = path

    frames = [tmp_path / "a.jpg", tmp_path / "b.jpg"]
    for p in frames:
        Image.new("RGB", (64, 64)).save(p)
    cands = [Candidate(index=i, start=i, end=i + 1, samples=[F(p)]) for i, p in enumerate(frames)]
    monkeypatch.setattr(sel, "get_vision_provider", lambda section, cfg: Reader())
    monkeypatch.setattr(sel, "release_all", lambda: order.append("release"))
    cfg = deep_merge(DEFAULTS, {"frame_selection": {"split": True, "transcribe_model": {"openai_model": "r"}}})
    kept, _, _ = sel.select(cands, [], Decider(), cfg, tmp_path)
    assert order == ["decide", "decide", "release", "read", "read"]
    assert kept[0].slide_text == "text"          # identical frames merge into one


def test_hard_slides_go_to_the_caption_model():
    from knowlib.config import DEFAULTS, deep_merge
    from knowlib.media.select import Kept, wants_strong_caption

    cfg = deep_merge(DEFAULTS, {})

    def k(kind, text="Title", flag=False):
        out = Kept(start=0, end=1, frame="f", caption="", slide_text=text, content_type=kind, reason="")
        out.ocr = {"flag": flag}
        return out

    assert wants_strong_caption(k("chart"), cfg)
    assert wants_strong_caption(k("slide", "| a |\n| --- |\n| 1 |"), cfg)
    assert wants_strong_caption(k("demo", flag=True), cfg)
    assert not wants_strong_caption(k("slide"), cfg)


def test_slide_text_list_becomes_lines():
    from knowlib.media.select import _as_text
    assert _as_text(["Title", "• Point"]) == "Title\n• Point"
    assert _as_text("Title") == "Title"
    assert _as_text(None) == ""


def test_handbook_reads_source_md(tmp_path):
    from knowlib.handbook.maker import parse_source

    (tmp_path / "source.md").write_text("""---
title: Lecture 4
---
# Lecture 4

## Before the first slide (to 00:10)

Welcome back.

## Slide 1 (00:10–00:20)
![Slide 1](assets/view/frame_001.jpg) · [full resolution](assets/frame_001.jpg)
> **OCR check:** they disagree.

**On slide:**

Table 36

**OCR text (apple-vision):**

Table 38

**Figure:** A table.

**Spoken:**

This is the table.

## Slide 2 (00:20–00:30)
![Slide 2](assets/view/frame_002.jpg) · [full resolution](assets/frame_002.jpg)

**On slide (OCR):**

noise
""", encoding="utf-8")
    lec = parse_source(tmp_path)
    one, two = lec.slides
    assert (lec.title, one.text, one.caption, one.frame) == ("Lecture 4", "Table 36", "A table.", "assets/frame_001.jpg")
    assert one.flagged and one.spoken == "Welcome back. This is the table." and "38" in one.ocr
    assert two.from_ocr and "noise" not in two.material()


def test_handbook_plan_must_cover_every_slide():
    from knowlib.handbook.maker import Slide, valid_plan

    slides = [Slide(n, "00:00", "00:01") for n in range(1, 7)]
    good = {"sections": [{"title": "A", "first_slide": 1, "last_slide": 2}, {"title": "B", "first_slide": 3, "last_slide": 6}]}
    assert valid_plan(good, slides) == good["sections"]
    gap = {"sections": [{"title": "A", "first_slide": 1, "last_slide": 2}, {"title": "B", "first_slide": 4, "last_slide": 6}]}
    assert [s["last_slide"] for s in valid_plan(gap, slides)] == [4, 6]


def test_handbook_flags_numbers_without_a_source():
    from knowlib.handbook.maker import unsupported_numbers

    text = "The total is 200 and 55.6.\n[[slide 12]]\n> **Note (not from class):** see 1234."
    assert unsupported_numbers(text, "55.6 appears here") == ["200"]


def test_lectures_sort_by_date_then_number():
    from knowlib.handbook.maker import lecture_order

    keys = ["c/lecture 10.mp4", "c/lecture 2.mp4", "c/2024-03-05 intro.mp4", "c/14-02-2024 first.mp4"]
    assert sorted(keys, key=lecture_order) == ["c/14-02-2024 first.mp4", "c/2024-03-05 intro.mp4", "c/lecture 2.mp4", "c/lecture 10.mp4"]


def test_layout_links_to_flat_sources(tmp_path):
    from knowlib.runner import link_source, open_library

    ctx, _ = open_library(tmp_path)
    (ctx.sources_dir / "abc").mkdir()
    link_source(ctx, "Finance/2024-03-05 Lecture/video.mp4", "abc", "{course}/{date}/{name}")
    assert (tmp_path / "organized/Finance/2024-03-05/video").resolve() == (ctx.sources_dir / "abc").resolve()


def test_input_folder_keeps_same_names_apart(tmp_path):
    from knowlib.runner import process

    (tmp_path / "lib").mkdir()
    for course in ("A", "B"):
        (tmp_path / course).mkdir()
        (tmp_path / course / "notes.txt").write_text(f"course {course}\n")
        rep = process(tmp_path / "lib", overrides={"library": {"inbox": str(tmp_path / course)}})
        assert [e["key"] for e in rep["new"]] == [f"{course}/notes.txt"]


def test_parse_json_variants():
    assert parse_json('```json\n{"decision": "keep"}\n```')["decision"] == "keep"
    assert parse_json('Sure! {"a": {"b": "x}y"}, "c": 1} done')["c"] == 1
    assert parse_json(r'{"a": "5\" screen", "c": 1}')["c"] == 1
    assert parse_json(r'{"a": "ends in \\", "c": 1}')["c"] == 1
    with pytest.raises(ValueError):
        parse_json("no json here")


def test_ocr_compare_ignores_table_reading_order():
    vlm = "| | Rainy | Sunny |\n|---|---|---|\n| Delayed | 36 | 16 |\n| On time | 11 | 84 |"
    ocr = "Rainy\nDelayed\nOn time\nSunny\n36\n11\n16\n84"
    assert not compare(vlm, ocr, 0.75)["flag"]


def test_recheck_ignores_recurring_screen_text_but_not_numbers():
    from knowlib.media.ocr import recheck_without_recurring

    chrome = "Azure ML Studio\nDraft saved at 5:14:43 PM\n"
    frames = [("Rows 14", chrome + "Rows 14"), ("Rows 15", chrome + "Rows 16"), ("Mean 7.5", chrome + "Mean 7.5")]
    items = [(v, {"backends": {"o": {"text": o, "flag": True}}, "flag": True}) for v, o in frames]
    recheck_without_recurring(items, {})
    assert [c["flag"] for _, c in items] == [False, True, False]


def test_ocr_compare_flags_numbers():
    ok = compare("WACC = 8.5%\nDebt 640", "WACC = 8.5%\nDebt 640", 0.75)
    assert not ok["flag"]
    bad = compare("WACC = 8.5%\nDebt 640", "WACC = 8.6%\nDebt 640", 0.75)
    assert bad["flag"] and "8.5%" in bad["numbers_only_vlm"]
    accents = compare("Κόστος κεφαλαίου", "ΚΟΣΤΟΣ ΚΕΦΑΛΑΙΟΥ", 0.75)
    assert accents["similarity"] == 1.0


def test_subtitles_and_repetitions(tmp_path):
    srt = tmp_path / "a.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:03,500\nHello <i>there</i>\n\n2\n00:01:02,250 --> 00:01:04,000\nSecond\n")
    segs = parse_subtitles(srt)
    assert segs[0] == {"start": 1.0, "end": 3.5, "text": "Hello there"} and segs[1]["start"] == 62.25
    loop = [{"start": i, "end": i + 1, "text": "thank you"} for i in range(6)]
    cleaned, dropped = clean_repetitions(loop)
    assert len(cleaned) == 2 and dropped == 4
    assert glossary_prompt(["WACC", "ΔΕΣΦΑ"], 800) == "Glossary: WACC, ΔΕΣΦΑ."


def test_router_rules(tmp_path):
    inbox = tmp_path / "inbox"
    (inbox / "lecture").mkdir(parents=True)
    for f in ("lecture/video.mp4", "lecture/rec.wav", "lecture/notes.pdf"):
        (inbox / f).write_bytes(b"x")
    (inbox / "podcasts").mkdir()
    for f in ("podcasts/ep1.mp3", "podcasts/ep2.mp3", "podcasts/ep2.srt"):
        (inbox / f).write_bytes(b"x")
    (inbox / "two-recorders").mkdir()
    for f in ("two-recorders/a.wav", "two-recorders/b.wav"):
        (inbox / f).write_bytes(b"x")
    (inbox / "two-recorders" / "session.yaml").write_text("primary_audio: b.wav\n")
    (inbox / "reading.urls.txt").write_text("# list\nhttps://example.com/a\nnot a url\nhttps://example.com/b\n")
    items = discover(inbox)
    kinds = [(i.kind, i.key) for i in items]
    assert ("file", "lecture/notes.pdf") in kinds
    assert ("session", "lecture/") in kinds                      # one video + audio = one session
    assert ("session", "podcasts/ep1.mp3") in kinds and ("session", "podcasts/ep2.mp3") in kinds
    ep2 = next(i for i in items if i.key == "podcasts/ep2.mp3")
    assert [p.name for p in ep2.paths] == ["ep2.mp3", "ep2.srt"]  # subtitles attached by stem
    two = next(i for i in items if i.key == "two-recorders/")
    assert two.sidecar["primary_audio"] == "b.wav" and len(two.paths) == 2
    assert [i.url for i in items if i.kind == "url"] == ["https://example.com/a", "https://example.com/b"]
    # files first, sessions last
    assert [i.kind for i in items].index("session") > max(n for n, i in enumerate(items) if i.kind == "file")


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
def test_alignment_offset_drift_and_pause(tmp_path):
    from scipy.signal import resample_poly

    from knowlib.config import DEFAULTS, deep_merge
    from knowlib.media.align import align

    from .fixtures import SR, speechlike, write_wav
    src = speechlike(360, seed=11)
    pause_at, pause_len = 180 * SR, 20 * SR
    primary = np.concatenate([src[:pause_at], src[pause_at + pause_len:]])
    write_wav(tmp_path / "primary.wav", 0.9 * primary)
    drift = 2.0 / 3600                                   # exaggerated clock drift: 2 s per hour
    v = resample_poly(src, int(1e6 * (1 + drift)), int(1e6)).astype(np.float32)
    v = np.concatenate([np.zeros(int(4.2 * SR), np.float32), v])
    v = 0.15 * np.convolve(v, np.exp(-np.arange(600) / 150) / 40, mode="same") + \
        0.02 * np.random.default_rng(2).standard_normal(len(v)).astype(np.float32)
    write_wav(tmp_path / "video.wav", v)
    cfg = deep_merge(DEFAULTS, {"alignment": {"min_drift_span_s": 60}})   # short test clip
    m = align(tmp_path / "primary.wav", tmp_path / "video.wav", tmp_path, cfg)
    assert len(m.segments) == 2
    errs = []
    for p in range(10, 330, 5):
        if 170 < p < 190:
            continue
        true = 4.2 + (p if p < 180 else p + 20) * (1 + drift)
        errs.append(abs(m.map(p) - true))
    assert np.median(errs) < 0.05 and max(errs) < 0.1
    assert abs(m.segments[0].drift * 3600 - 2.0) < 0.5


def test_udemy_style_srt(tmp_path):
    srt = tmp_path / "u.srt"
    srt.write_text("0\n1\n00:00:00,610 --> 00:00:09,310\nHi. In the previous lectures\n1\n\n2\n"
                   "00:00:09,310 --> 00:00:11,480\ncorrelation coefficient.\n2\n\n3\n")
    segs = parse_subtitles(srt)
    assert [s["text"] for s in segs] == ["Hi. In the previous lectures", "correlation coefficient."]


def test_text_ingestor_and_ignore(tmp_path):
    from knowlib.runner import process
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "train.py").write_text("import pandas as pd\nprint('hi')\n")
    (inbox / "data.csv").write_text("age,income\n31,1000\n45,2000\n")
    (inbox / "junk-torrentsite.txt").write_text("ads")
    (tmp_path / "config.yaml").write_text("library: {ignore: ['*torrentsite*']}\n")
    rep = process(tmp_path)
    keys = sorted(e["key"] for e in rep["new"])
    assert keys == ["data.csv", "train.py"]
    src = next((tmp_path / "sources").glob("train-*")) / "source.md"
    assert "```python\nimport pandas as pd" in src.read_text()
    csvsrc = next((tmp_path / "sources").glob("data-*")) / "source.md"
    assert "Table with 2 rows and 2 columns" in csvsrc.read_text()


def test_handbook_note_must_match_its_passage():
    from knowlib.handbook.maker import checked_note
    passages = [("a.pdf", "The test needs 5 observations per cell.")]
    assert checked_note({"note": None}, passages) == ("", "")
    line, problem = checked_note({"note": 'Each cell needs "5 observations".', "passage": 1}, passages)
    assert "a.pdf" in line and not problem
    assert checked_note({"note": "Each cell needs 7.", "passage": 1}, passages)[1]
    assert checked_note({"note": 'It says "at least five".', "passage": 1}, passages)[1]
    assert checked_note({"note": "Fine.", "passage": 4}, passages)[1]


def test_handbook_drops_slides_with_nothing_reliable():
    from knowlib.handbook.maker import Slide, missing_table_numbers
    assert not Slide(1, "00:00", "00:02", text="00-1-100", from_ocr=True).usable()
    assert Slide(2, "00:02", "00:09", text="00-1-100", from_ocr=True, spoken="Hello").usable()
    slide = Slide(3, "00:09", "00:20", text="| df | 0.05 |\n| 2 | 5.991 |")
    assert missing_table_numbers("df 2 at 0.05", slide) == ["5.991"]


def test_handbook_tables():
    from knowlib.handbook.maker import drop_repeated_tables, ragged_tables
    good = "| a | b |\n|---|---|\n| 1 | 2 |"
    assert not ragged_tables(good)
    assert ragged_tables("| a | b | c |\n|---|---|---|\n| 1 | 2 |")
    first, seen = drop_repeated_tables(f"x\n{good}\ny", set())
    assert good in first
    again, _ = drop_repeated_tables(f"z\n{good.replace('---', '--')}\n", seen)
    assert "same table" in again and "| 1 | 2 |" not in again


def test_handbook_finish_checks_an_agents_draft(tmp_path):
    import json
    from knowlib.handbook.maker import finish
    src = tmp_path / "src"
    (src / "assets").mkdir(parents=True)
    (src / "assets" / "f.jpg").write_bytes(b"x")
    (src / "source.md").write_text("---\ntitle: T\n---\n## Slide 1 (00:00–00:10)\n"
                                   "![Slide 1](assets/f.jpg) · [full resolution](assets/f.jpg)\n\n"
                                   "**On slide:**\n\n| a | b |\n|---|---|\n| 1.5 | 2.5 |\n\n**Spoken:**\n\n[00:01] Hello\n",
                                   encoding="utf-8")
    out = tmp_path / "books" / "t"
    out.mkdir(parents=True)
    (out / ".lecture.json").write_text(json.dumps({"folder": str(src), "source_id": "s", "key": "k"}))
    (out / "brief.md").write_text("| 1.5 | 2.5 |", encoding="utf-8")
    (out / "draft.md").write_text("# T\n\nThe value is 9.9.\n", encoding="utf-8")
    (out / "corrections.md").write_text("- slide 1: fixed", encoding="utf-8")
    result = finish(tmp_path / "books")[0]
    review = (out / "review.md").read_text(encoding="utf-8")
    assert result["issues"] == 2 and "9.9" in review and "1.5, 2.5" in review and "- slide 1: fixed" in review
    (out / "draft.md").write_text("# T\n\n[[slide 1]]\n", encoding="utf-8")
    assert finish(tmp_path / "books")[0]["issues"] == 0
    assert "assets/f.jpg" in (out / "handbook.md").read_text(encoding="utf-8")


def test_session_reader_waits_for_written_answers(tmp_path):
    import pytest
    import json
    from knowlib.providers.agent import AgentJobsWaiting, AgentPending, AgentProvider, stop_if_waiting
    frame = tmp_path / "f.jpg"
    provider = AgentProvider({"agent_model": "haiku"}, tmp_path)
    with pytest.raises(AgentPending):
        provider.generate([frame], "Read this")
    with pytest.raises(AgentJobsWaiting):
        stop_if_waiting(tmp_path)
    job = json.loads((tmp_path / "jobs.json").read_text())[0]
    assert job["model"] == "haiku" and job["images"] == [str(frame)]
    Path(job["answer"]).write_text("the slide text\n")
    assert provider.generate([frame], "Read this") == "the slide text"
    stop_if_waiting(tmp_path)
