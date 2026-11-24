"""Speech cross-check: a second speech model, from another family, on the same audio.

Whisper stays the transcript: citations, timestamps and slide alignment hang off it. A second model
(NVIDIA Parakeet, through mlx-audio, on this Mac) transcribes the recording again, and every Whisper
segment is compared with what Parakeet heard in the same seconds. Where the two differ on a name, a
number or a negation, the lecture file gets a "Speech check" line under that passage, so the writer
rereads it (and confirms it from a slide or document) and the reviewer is warned.

The two models mostly fail in different places (tested on a Lecture 2 of Financial Management,
2026-11-24: each of Whisper turbo, large-v3 and Parakeet got passages the others missed). Any two
disagree on 20-30% of words in classroom audio, mostly fillers and small words, so only the words
that cost a note are compared.
"""
from __future__ import annotations

import difflib
import gc
import re
from pathlib import Path

from ..contract import stage

PACKAGES = {"parakeet": "mlx-audio"}

# ---- the second transcription ------------------------------------------------------------------


def _parakeet(wav: Path, lang: str | None, cc: dict) -> list[dict]:
    from mlx_audio.stt.utils import load_model
    model_id = cc["model"] if lang == "en" else cc.get("model_other_languages") or cc["model"]
    model = load_model(model_id)
    r = model.generate(str(wav), chunk_duration=float(cc.get("chunk_s", 120)),
                       overlap_duration=float(cc.get("overlap_s", 15)))
    words: list[dict] = []
    for s in r.sentences:
        for k, t in enumerate(s.tokens):
            text = t.text
            if words and k and not text.startswith(" "):      # a subword or punctuation: same word
                words[-1]["word"] += text
                words[-1]["end"] = float(t.end)
            else:
                words.append({"start": float(t.start), "end": float(t.end), "word": text.strip()})
    del model
    gc.collect()
    return [w for w in words if w["word"]]


def _mock(wav: Path, lang, cc: dict) -> list[dict]:
    return []


BACKENDS = {"parakeet": _parakeet, "mock": _mock}


def second_transcript(wav: Path, lang: str | None, cc: dict) -> tuple[list[dict], dict]:
    words = BACKENDS[cc["backend"]](wav, lang, cc)
    model = cc["model"] if lang == "en" else cc.get("model_other_languages") or cc["model"]
    return words, stage(cc["backend"], model=model, package=PACKAGES.get(cc["backend"]), words=len(words))


# ---- comparison ------------------------------------------------------------------------------

FILLERS = {"uh", "um", "erm", "ah", "eh", "hmm", "mm"}
NEGATIONS = {"not", "no", "never", "without", "nobody", "nothing", "none", "neither", "nor", "cannot"}
SCALES = {"thousand", "million", "billion", "trillion", "percent", "%"}
_UNITS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                     "fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "_"}
_ORD = {"first": "one", "second": "two", "third": "three", "fifth": "five", "eighth": "eight", "ninth": "nine",
        "twelfth": "twelve"}
# capitalised words that are not names (sentence starts are skipped anyway)
_COMMON = set("""I I'm I'll I've I'd Okay OK Yes Yeah No The What This That These Those There Then Do Does You And But
So Why How When Where Who Which Can Because If Now Let We He She It They My Our Your His Her Mr Mrs Ms Dr Sir
Professor Monday Tuesday Wednesday Thursday Friday Saturday Sunday January February March April May June July
August September October November December English Greek""".split())


_NUMBER = set(_UNITS) | set(_TENS) | set(_ORD) | {"hundred", "thousand"} | \
    {w + "th" for w in _UNITS} | {w[:-1] + "ieth" for w in _TENS}


def _num_word(w: str) -> bool:
    parts = re.sub(r"[^a-z-]", "", w.lower()).split("-")
    return bool(parts[0]) and all(p in _NUMBER for p in parts)


def _number(ws: list[str]) -> str:
    """'three hundred and fifteen' -> '315', 'twenty-first' -> '21', 'two thousand thirteen' -> '2013'."""
    total = cur = 0
    for w in (p for x in ws for p in re.sub(r"[^a-z-]", "", x.lower()).split("-")):
        w = _ORD.get(w, w)
        if w.endswith("ieth"):
            w = w[:-4] + "y"
        elif w.endswith("th") and w[:-2] in _UNITS:
            w = w[:-2]
        if w in _UNITS:
            cur += _UNITS[w]
        elif w in _TENS:
            cur += _TENS[w]
        elif w == "hundred":
            cur = (cur or 1) * 100
        elif w == "thousand":
            total, cur = total + (cur or 1) * 1000, 0
    return str(total + cur)


def normalise(text: str) -> list[str]:
    """Words for comparing: lower case, no punctuation or fillers, digits for numbers ('$215' -> '215',
    '21st' -> '21', '1,300' -> '1300'), "isn't" -> 'is not', '%' -> 'percent'."""
    t = text.lower().replace("n't", " not").replace("%", " percent")
    t = re.sub(r"(?<=\d),(?=\d{3})", "", t)
    t = re.sub(r"'s\b", "", t)
    ws = [w for w in re.findall(r"[a-z]+(?:'[a-z]+)?|\d+(?:\.\d+)?(?:st|nd|rd|th)?", t) if w not in FILLERS]
    return [re.sub(r"^(\d+)(st|nd|rd|th)$", r"\1", w) for w in ws]


def significant(word: str, display: str, at_start: bool, glossary: set[str]) -> bool:
    """A number, a negation, a scale word, a glossary term or a capitalised word mid-sentence."""
    if re.fullmatch(r"\d+(\.\d+)?", word) or word in NEGATIONS or word in SCALES or word in glossary:
        return True
    core = re.sub(r"^\W+|\W+$", "", display)
    return bool(core[:1].isupper() and not at_start and core not in _COMMON and len(core) > 1)


def _words(text: str, glossary: set[str]) -> list[dict]:
    """The text as words: {raw (as written), n (normalised), sig (a word that matters)}. A spelled
    number ('three hundred and fifteen') is one word, so it compares equal to '315'."""
    raws, out, start, i, k = text.split(), [], True, 0, 0
    while i < len(raws):
        j = i
        while j < len(raws) and (_num_word(raws[j]) or (j > i and re.sub(r"\W", "", raws[j].lower()) == "and"
                                                         and re.sub(r"\W", "", raws[j - 1].lower()) in ("hundred", "thousand"))):
            j += 1
            if re.search(r"[.,;:?!]$", raws[j - 1]):
                break
        while j > i and re.sub(r"\W", "", raws[j - 1].lower()) == "and":
            j -= 1
        if j > i:
            raw, ns = " ".join(raws[i:j]), [_number(raws[i:j])]
        else:
            raw, ns, j = raws[i], normalise(raws[i]), i + 1
        for n in ns:
            out.append({"raw": raw, "k": k, "n": n, "sig": significant(n, raw, start, glossary)})
        k += 1
        start, i = bool(re.search(r"[.?!][\"')]*$", raw)), j
    return out


def _window(words: list[dict], start: float, end: float) -> str:
    return " ".join(w["word"] for w in words if start <= (w["start"] + w["end"]) / 2 < end)


def _context(ws: list[dict], i: int, j: int, pad: int = 3) -> str:
    """The words i..j with `pad` written words either side; a word that normalised into two
    ('don't' -> 'do not') is shown once."""
    a, b = max(0, i - pad), min(len(ws), j + pad)
    shown, last = [], None
    for w in ws[a:b]:
        if w["k"] != last:
            shown.append(w["raw"])
            last = w["k"]
    return ("…" if a > 0 else "") + " ".join(shown).strip() + ("…" if b < len(ws) else "")


def _same(sa: list[str], sb: list[str]) -> bool:
    """The words that matter agree, allowing spelling variants of a name ('Manos'/'Manus');
    numbers must match exactly."""
    if sorted(sa) == sorted(sb):
        return True
    if len(sa) != len(sb):
        return False
    for x, y in zip(sa, sb):
        if x == y:
            continue
        if re.search(r"\d", x + y) or x in NEGATIONS or y in NEGATIONS or x in SCALES or y in SCALES:
            return False
        if difflib.SequenceMatcher(None, x, y).ratio() < 0.8:
            return False
    return True


def _merge(ops: list[tuple], gap: int = 3) -> list[tuple]:
    """Differences a few words apart become one passage."""
    out: list[list] = []
    for tag, i1, i2, j1, j2 in ops:
        if out and i1 - out[-1][2] <= gap and j1 - out[-1][4] <= gap:
            out[-1][2], out[-1][4], out[-1][0] = i2, j2, "replace"
        else:
            out.append([tag, i1, i2, j1, j2])
    return [tuple(o) for o in out]


def compare(segs: list[dict], words: list[dict], glossary: list[str] | None = None,
            max_per_segment: int = 2) -> dict[int, list[dict]]:
    """{index of a Whisper segment: [{start, whisper, other}]} where the second model heard a
    different name, number or negation in the same seconds."""
    gl = {n for t in (glossary or []) for n in normalise(t)}
    out: dict[int, list[dict]] = {}
    for idx, s in enumerate(segs):
        other = _window(words, s["start"], s["end"])
        a, b = _words(s["text"], gl), _words(other, gl)
        if not a or not b:
            continue
        sm = difflib.SequenceMatcher(None, [w["n"] for w in a], [w["n"] for w in b], autojunk=False)
        found = []
        ops = [op for op in sm.get_opcodes() if op[0] != "equal" and not (    # words cut at the window's edge
            op[0] != "replace" and (op[1] == 0 or op[2] == len(a)) and (op[3] == 0 or op[4] == len(b)))]
        for tag, i1, i2, j1, j2 in _merge(ops):
            sa = [w["n"] for w in a[i1:i2] if w["sig"]]
            sb = [w["n"] for w in b[j1:j2] if w["sig"]]
            if not sa and not sb or _same(sa, sb):
                continue
            found.append({"start": s["start"], "whisper": _context(a, i1, i2), "other": _context(b, j1, j2)})
        if found:
            out[idx] = found[:max_per_segment]
    return out


def check_line(ts: str, c: dict, other_name: str = "Parakeet") -> str:
    return (f'> [{ts}] **Speech check:** Whisper heard "{c["whisper"]}"; {other_name} heard "{c["other"]}". '
            "Confirm names, numbers and negations here from a slide or document.")
