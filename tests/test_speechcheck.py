"""The speech cross-check: comparing Whisper's segments with a second model's words."""
from knowlib.ingestors.media import _paragraphs
from knowlib.media import speechcheck as sc
from knowlib import privacy


def _words(text: str, start: float, step: float = 0.3) -> list[dict]:
    return [{"start": start + i * step, "end": start + (i + 1) * step, "word": w} for i, w in enumerate(text.split())]


def test_normalise_numbers_and_negations():
    assert sc.normalise("It isn't $1,300 on the 21st.") == ["it", "is", "not", "1300", "on", "the", "21"]
    assert sc.normalise("Nvidia's 50%") == ["nvidia", "50", "percent"]
    assert [w["n"] for w in sc._words("a shortfall of three hundred and fifteen billion", set())] == \
        ["a", "shortfall", "of", "315", "billion"]
    assert [w["n"] for w in sc._words("in two thousand thirteen.", set())] == ["in", "2013"]
    assert [w["n"] for w in sc._words("the twenty-first of October", set())] == ["the", "21", "of", "october"]


def test_compare_flags_what_matters():
    seg = {"start": 0.0, "end": 3.0, "text": "it drops close to 50% today"}
    words = _words("it drops close to 30% today", 0.0, 0.5)
    out = sc.compare([seg], words)
    assert len(out[0]) == 1 and "50%" in out[0][0]["whisper"] and "30%" in out[0][0]["other"]


def test_compare_spelled_number_equals_digits():
    seg = {"start": 0.0, "end": 3.0, "text": "a shortfall of 300 billion for 2030"}
    assert sc.compare([seg], _words("a shortfall of three hundred billion for 2030", 0.0, 0.3)) == {}
    other = sc.compare([seg], _words("a shortfall of three hundred million for 2030", 0.0, 0.3))
    assert other and "million" in other[0][0]["other"]


def test_compare_names_but_not_spelling_variants_or_fillers():
    seg = {"start": 0.0, "end": 4.0, "text": "blah blah. The classic example is Steve Jobs, he said."}
    assert sc.compare([seg], _words("blah blah. The classic example is Steve Johnson, he said.", 0.0, 0.4))
    assert sc.compare([seg], _words("blah blah. uh The classic example is Steve Jobs, uh he said.", 0.0, 0.3)) == {}
    seg2 = {"start": 0.0, "end": 3.0, "text": "the opinion of Manos is that"}
    assert sc.compare([seg2], _words("the opinion of Manus is that", 0.0, 0.5)) == {}
    seg3 = {"start": 0.0, "end": 3.0, "text": "we did something great now"}
    assert sc.compare([seg3], _words("we didn't get something great now", 0.0, 0.5))


def test_compare_ignores_words_cut_at_the_edge():
    seg = {"start": 1.0, "end": 3.0, "text": "This is very important."}
    words = _words("Nvidia said. This is very important.", 0.0, 0.5)
    assert sc.compare([seg], words) == {}


def test_check_lines_under_their_paragraph_and_treated_as_speech():
    segs = [{"start": 0.0, "end": 5.0, "text": "Ask Maria about the 50% drop."}]
    checks = {0: [{"start": 0.0, "whisper": "…the 50% drop.", "other": "…the 30% drop."}]}
    out = _paragraphs(segs, 0, 10, checks=checks)
    assert out[0].startswith("[00:00]") and out[1].startswith("> [00:00] **Speech check:**")
    assert privacy.redact_speech(out[1] + " Maria", ["Maria"]).endswith("[a person]")
    spoken, _ = privacy._split_session("\n".join(out))
    assert "30% drop" in spoken
