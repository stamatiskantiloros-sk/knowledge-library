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
    cfg["frame_selection"]["caption_model_types"] = "all"
    assert wants_strong_caption(k("slide"), cfg)


def test_a_model_that_does_not_fit_stops_before_loading(monkeypatch):
    import subprocess
    from knowlib.providers import openai_compat as oc

    def fake_run(args, **kw):
        out = {"ps": "[]", "ls": '[{"modelKey": "big", "sizeBytes": 14000000000}]'}.get(args[1], "")
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

    monkeypatch.setattr(oc.subprocess, "run", fake_run)
    monkeypatch.setattr(oc, "available_bytes", lambda: 11_000_000_000)
    monkeypatch.setattr(oc, "largest_processes", lambda n=3: "Browser 4.0 GB")
    p = oc.OpenAICompatProvider({"openai_model": "big", "manage_lms": True})
    with pytest.raises(oc.NotEnoughMemory, match="14.0 GB but only 11.0 GB.*Browser"):
        p.generate([], "hi")
    oc.OpenAICompatProvider({"openai_model": "small", "manage_lms": True})._check_memory()   # unknown size: no check
    monkeypatch.setattr(oc, "available_bytes", lambda: 20_000_000_000)
    p._check_memory()                                                                       # fits


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
    from PIL import Image
    Image.new("RGB", (40, 30), "white").save(src / "assets" / "f.jpg")   # a real image: finish reads it for privacy
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
    with pytest.raises(AgentPending):             # the same frame name in another lecture is another job
        provider.generate([tmp_path / "other" / "f.jpg"], "Read this")


def test_the_glossary_prompt_is_kept_on_every_whisper_window():
    """Whisper drops initial_prompt after the first window when it does not condition on its own
    text; knowlib puts it back in front of each window's prompt, once."""
    from mlx_whisper import whisper as W
    from mlx_whisper.decoding import DecodingOptions
    from knowlib.media.transcribe import _GlossaryEveryWindow
    seen = []
    real = W.Whisper.decode
    W.Whisper.decode = lambda model, mel, options, *a, **k: seen.append(options.prompt)
    try:
        model = type("M", (), {"is_multilingual": True, "num_languages": 100})()
        with _GlossaryEveryWindow("Glossary: Little's law, takt time.", "en") as g:
            W.Whisper.decode(model, None, DecodingOptions(prompt=[]))           # a later window: reset
            W.Whisper.decode(model, None, DecodingOptions(prompt=g.tokens + [1, 2]))   # the first window
        W.Whisper.decode(model, None, DecodingOptions(prompt=[]))               # restored afterwards
    finally:
        W.Whisper.decode = real
    assert seen[0] == g.tokens and seen[1] == g.tokens + [1, 2] and seen[2] == []


def test_personal_information_is_found_and_redacted():
    from knowlib import privacy
    ns = ["Maria", "Papadopoulos"]
    assert privacy.reasons("Throughput = 42 units per hour", ns) == []
    assert privacy.reasons("1 | Doe | Jane | jane.doe@uni.gr", ns) == ["an email address"]
    assert privacy.reasons("Ast. Prof. A. Papadopoulos", ns) == ["a name"]
    assert privacy.reasons("call +30 210 1234567", []) == ["a phone number"]
    assert privacy.reasons("pages 120-135, 2026", []) == []           # numbers alone are not a phone
    assert privacy.redact("What happens in your jobs, Maria? Mail m@example.org", ns) == \
        "What happens in your jobs, [a person]? Mail [an email address]"


def test_the_brief_marks_undiscussed_slides_redacts_names_and_lists_the_notes(tmp_path):
    from knowlib.handbook.maker import Lecture, Slide, brief
    lec = Lecture("lec", "L/", "Lecture 1", tmp_path, [
        Slide(1, "0:00", "1:00", text="Title", spoken="What happens in your jobs, Maria? Write to a@example.org"),
        Slide(2, "1:00", "1:00", text="Team exercise", discussed=False)])
    (tmp_path / "notes").mkdir()
    out = brief(lec, [], notes_dir=tmp_path / "notes", names=["Maria"])
    assert "Maria" not in out and "a@example.org" not in out and "[a person]" in out
    assert out.split("### Slide 2", 1)[1].count("Not discussed in the recording") == 1
    assert "Topic notes built from these materials" in out and "18. Name no one in the room." in out


def test_finish_reports_personal_information_in_the_text_and_images(tmp_path, monkeypatch):
    from knowlib.handbook import maker
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "s1.png").write_bytes(b"png")
    (tmp_path / "handbook.md").write_text("# H\n\nAsk Maria.\n\n![Slide 1](assets/s1.png)\n")
    monkeypatch.setattr("knowlib.media.ocr.apple_vision", lambda path, oc: "Prof. A. B., ab@example.org")
    got = maker.personal_information(tmp_path, {"privacy": {"names": ["Maria"]}})
    assert any("in the text (a name)" in g for g in got) and any("s1.png shows personal information" in g for g in got)


def test_captions_and_section_endings_can_leave_out_recording_times():
    from knowlib.handbook.maker import without_times
    text = ("![Slide 2, 08:16](assets/s.png)\n\nSlide 2, 08:16\n\n*Slides 1–2, 07:24–11:05*\n"
            "*Slides 54–55, 1:30:30–1:35:05 (recording ends at the break)*")
    assert without_times(text) == ("![Slide 2](assets/s.png)\n\nSlide 2\n\n*Slides 1–2*\n"
                                   "*Slides 54–55 (recording ends at the break)*")
    assert without_times("*Slides 13, 16–17*") == "*Slides 13, 16–17*"        # a slide range is not a time
    assert without_times("*Slides 13, 16–17, 07:24–11:05*") == "*Slides 13, 16–17*"


def test_the_brief_asks_for_fidelity_and_a_claim_by_claim_check(tmp_path):
    from knowlib.handbook.maker import Lecture, Slide, brief
    rules = brief(Lecture("lec", "L/", "Lecture 1", tmp_path, [Slide(1, "0:00", "1:00", text="Title")]), []).split("## Slides")[0]
    assert "Keep the lecturer's scope and qualifiers" in rules and "never \"not X\"" in rules
    assert "give both, each with its source, and point the student to the document" in rules
    assert "Check the draft claim by claim" in rules and rules.index("21. Check") < rules.index("When done")


def test_finish_lists_absolute_claims_whose_source_hedges():
    from knowlib.handbook.maker import Slide, dropped_hedges
    slides = [Slide(3, "10:00", "12:00", spoken="[10:05] Markets are in general efficient, but not when people act irrationally."),
              Slide(4, "12:00", "13:00", spoken="[12:10] Out of 10 decisions in your life, 8, 7 have to do with money.")]
    draft = ("# T\n\n## Markets\n\n- The market is not efficient because people are not rational.\n"
             "- Markets are efficient in general, unless people act irrationally.\n\n*Slides 3–4*\n")
    got = dropped_hedges(draft, slides)
    assert len(got) == 1 and "The market is not efficient" in got[0] and "slide 3, 10:05" in got[0]
    assert dropped_hedges("# T\n\n## A\n\nThe market is never wrong.\n\n*Slides 9*\n", slides) == []   # no matching passage


def test_the_brief_calls_the_speaker_what_the_library_asks(tmp_path):
    from knowlib.handbook.maker import Lecture, Slide, brief
    lec = Lecture("lec", "L/", "Lecture 1", tmp_path, [Slide(1, "0:00", "1:00", text="Title")])
    out = brief(lec, [], speaker="the professor")
    assert 'Call the speaker "the professor"' in out and "the lecturer" not in out.split("## Slides")[0]


def test_docx_highlights_are_marked():
    from knowlib.ingestors.office import _mark_highlights
    hl = '<w:rPr><w:highlight w:val="yellow"/></w:rPr>'
    xml = ('<w:p><w:r><w:t>A. </w:t></w:r><w:r>' + hl + '<w:t>C</w:t></w:r>'
           '<w:r>' + hl + '<w:t xml:space="preserve">.</w:t></w:r>'
           '<w:r><w:rPr><w:highlight w:val="none"/></w:rPr><w:t>D.</w:t></w:r></w:p>')
    out, n = _mark_highlights(xml)
    assert n == 2
    text = "".join(__import__("re").findall(r"<w:t\b[^>]*>([^<]*)</w:t>", out))
    assert text == "A. ==C.==D."          # adjacent highlighted runs merge; val="none" is not a highlight


def test_docx_bold_underline_is_marked():
    from knowlib.ingestors.office import _mark_highlights
    bu = '<w:rPr><w:b/><w:u w:val="single"/></w:rPr>'
    xml = ('<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>A.</w:t></w:r>'
           '<w:r>' + bu + '<w:t>D.</w:t></w:r></w:p>')
    out, n = _mark_highlights(xml)
    assert n == 1 and "==D.==" in out and "==A." not in out   # bold alone is not a mark


def test_citation_parts_inherit_the_source():
    from knowlib.notecheck import citations
    assert citations("x [@a, p. 4; p. 6] y [@b, slide 2; @c, 1:00]") == [
        ("a", "p. 4"), ("a", "p. 6"), ("b", "slide 2"), ("c", "1:00")]


def test_write_notes_plan_and_pages(tmp_path):
    from knowlib.notecheck import Source
    from knowlib.writenotes import chapter_contents, chapter_starts, page_text, parse_plan
    plan = ("- BR = brealey-1a (Brealey)\n- RO = ross-2b (Ross)\n\n## A. Valuation\n"
            "Chapters: BR 5 (119), RO 8\n- npv-rule — the NPV rule\n- irr — IRR and payback\n\n"
            "## B. Risk\nChapters: BR 7 (184)\n- capm-and-beta — beta and CAPM\n")
    keys, themes = parse_plan(plan)
    assert keys == {"BR": "brealey-1a", "RO": "ross-2b"}
    assert [t["letter"] for t in themes] == ["A", "B"] and themes[0]["notes"][1] == ("irr", "IRR and payback")
    digest = "# D\n\n## Contents\n\n- 8 NPV and Other Criteria, p. 628\n  - 8.1 NPV, p. 630\n- 9 Next, p. 706\n\n## Body\n"
    starts = chapter_starts(themes, {"RO": digest})
    assert starts["RO"] == [(628, 8)] and starts["BR"] == [(119, 5), (184, 7)]
    assert chapter_contents(digest, 628, 705).splitlines() == ["- 8 NPV and Other Criteria, p. 628", "  - 8.1 NPV, p. 630"]
    md = tmp_path / "b.pdf.md"
    md.write_text("---\nid: b\n---\n<!-- page 1 (pdf 1) -->\nOne\n<!-- page 2 (pdf 2) -->\nTwo\n<!-- page 3 (pdf 3) -->\nThree\n")
    assert page_text(Source(md, "b"), "1-2", 10_000) == "[@b, p. 1]\nOne\n\n[@b, p. 2]\nTwo"


def test_write_notes_quotes_a_title_with_a_colon():
    import yaml
    from knowlib.writenotes import strip_fences
    note = strip_fences("```markdown\n---\ntitle: Investment Criteria: IRR\nupdated: 2026-11-12\n---\n# X\n```")
    fm = yaml.safe_load(note.split("---\n")[1])
    assert fm["title"] == "Investment Criteria: IRR" and note.endswith("# X\n")


def test_lecture_in_parts_joins_audio_and_labels_slides(tmp_path):
    import subprocess
    from knowlib.ingestors.media import mark_parts
    from knowlib.media.session import Session, join_parts
    for name, secs in (("a.m4a", 2), ("b.m4a", 3)):
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=440:duration={secs}",
                        str(tmp_path / name)], check=True)
    sess = Session(title="L1", folder=tmp_path, video=tmp_path / "screen.mov", audio_sources=[],
                   primary_audio=None, primary_reason="")
    parts = join_parts(sess, ["a.m4a", {"name": "After the break", "audio": "b.m4a"}], tmp_path)
    assert [p["name"] for p in parts] == ["Part A", "After the break"]
    assert sess.video is None and sess.primary_audio.suffix == ".flac"
    assert abs(sess.info[sess.primary_audio.name]["duration"] - 5) < 0.2
    assert abs(parts[1]["start_s"] - 2) < 0.1
    body = "# L1\n\n*Duration 00:05*\n\n## Slide 1 (00:00–00:02)\ntext\n\n## Slide 2 (00:02–00:05)\nmore"
    out = mark_parts(body, parts)
    assert "## Slide 1 (00:00–00:02) · Part A" in out
    assert "## Slide 2 (00:02–00:05) · After the break" in out
    assert out.index("*Recorded in parts:") < out.index("*Duration")


def test_handbook_pdf_is_named_and_dated_from_the_lecture():
    from knowlib.handbook.maker import long_date, pdf_name
    assert long_date("2026-11-10") == "10 November 2026" and long_date("") == "" and long_date("autumn") == "autumn"
    assert pdf_name("Lecture 1: Introduction to Operations Management", "2026-11-10", "Operations Management") == \
        "Lecture 1 - Introduction to Operations Management - 2026-11-10.pdf"
    assert pdf_name("Session 3: WACC", "2026-11-12", "Corporate Finance") == "Corporate Finance - Session 3 - WACC - 2026-11-12.pdf"
    assert pdf_name('A/B "test"?', "", "") == "AB test.pdf"


def test_the_signature_goes_once_in_the_last_pages_corner(tmp_path):
    import pymupdf
    from knowlib.handbook.maker import _sign_last_page
    doc = pymupdf.open()
    for n in (1, 2):
        page = doc.new_page(width=595, height=842)
        page.insert_text((540, 820), str(n), fontsize=8)          # the page number in the footer
    path = tmp_path / "book.pdf"
    doc.save(path)
    _sign_last_page(path, "made by Maria")
    with pymupdf.open(path) as out:
        assert "Maria" not in out[0].get_text() and "made by Maria" in out[1].get_text().replace("\xa0", " ")
        sign = [w for w in out[1].get_text("words") if "Maria" in w[4]][-1]
        num = [w for w in out[1].get_text("words") if w[4] == "2"][0]
        assert sign[2] < num[0] and abs(sign[3] - num[3]) < 3 and sign[1] > 790   # left of the number, same line
def test_blank_last_pages_with_only_a_page_number_are_dropped(tmp_path):
    import pymupdf
    from knowlib.handbook.maker import drop_blank_last_pages
    doc = pymupdf.open()
    for n, body in ((1, "Text"), (2, "More text"), (3, ""), (4, "")):
        page = doc.new_page()
        if body:
            page.insert_text((72, 72), body)
        page.insert_text((500, 800), str(n))
    doc.save(tmp_path / "h.pdf")
    assert drop_blank_last_pages(tmp_path / "h.pdf") == 2
    with pymupdf.open(tmp_path / "h.pdf") as d:
        assert len(d) == 2 and "More text" in d[-1].get_text()
    assert drop_blank_last_pages(tmp_path / "h.pdf") == 0                     # nothing to drop: left as it is


def test_last_page_signature_takes_several_lines_stacked_beside_the_page_number(tmp_path):
    import pymupdf
    from knowlib.handbook.maker import _sign_last_page
    doc = pymupdf.open()
    for n in (1, 2):
        page = doc.new_page()
        page.insert_text((72, 72), "Body")
        page.insert_text((540, 815), str(n))
    doc.save(tmp_path / "h.pdf")
    _sign_last_page(tmp_path / "h.pdf", ['"cooked" by me', "with care"], size=6.5)
    with pymupdf.open(tmp_path / "h.pdf") as d:
        first, second = d[-1].search_for("cooked")[0], d[-1].search_for("with care")[0]
        number = d[-1].search_for("2")[-1]
        assert first.y1 < second.y1 and second.x1 < number.x0 and abs(second.y1 - number.y1) < 4
        assert not d[0].search_for("cooked")
