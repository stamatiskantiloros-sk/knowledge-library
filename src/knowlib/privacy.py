"""Personal information that must not leave the machine.

Before an image goes to a model off this machine, classic OCR (local) reads it; an email address, a
phone number or a name from `privacy.names` keeps it here, and the agent reads it instead. The same
patterns let a step that sends text (a review, a reading) replace names with neutral labels first.
"""
from __future__ import annotations

import re

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"(?<![\d.])[+(]?\d[\d ()-]{8,}\d(?![\d.])")


def _is_phone(m: re.Match) -> bool:
    """A run of digits that is not a row of a table or chart axis: years (1988 1989 1990) or groups of
    one length (100 200 300) separated by spaces only."""
    run = m.group(0)
    if re.fullmatch(r"\(?\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{4}|\(?\d{4} ?- ?\d{4}\)?", run.strip()):
        return False                                   # a date or a range of years, not a phone number
    if re.search(r"[+()-]", run):
        return True
    groups = run.split()
    if len(groups) >= 3 and (all(re.fullmatch(r"(19|20)\d\d", g) for g in groups)
                             or len({len(g) for g in groups}) == 1):
        return False
    return True


def phones(text: str) -> list[str]:
    return [m.group(0) for m in PHONE_RE.finditer(text) if _is_phone(m)]


def names(cfg: dict) -> list[str]:
    """Names to protect: `privacy.names` in the library config or a session's sidecar."""
    return [str(n) for n in ((cfg.get("privacy") or {}).get("names") or []) if str(n).strip()]


def _name_re(ns: list[str]) -> re.Pattern | None:
    ns = sorted({n.strip() for n in ns if len(n.strip()) >= 3}, key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(map(re.escape, ns)) + r")\b", re.I) if ns else None


def reasons(text: str, ns: list[str]) -> list[str]:
    """Why a text counts as personal: 'an email address', 'a phone number', 'a name (...)'; empty if not."""
    out = []
    if EMAIL_RE.search(text):
        out.append("an email address")
    if phones(text):
        out.append("a phone number")
    if (rx := _name_re(ns)) and (m := rx.search(text)):
        out.append("a name")
    return out


def redact(text: str, ns: list[str], label: str = "[a person]") -> str:
    """The text with emails, phone numbers and the listed names replaced by neutral labels."""
    text = EMAIL_RE.sub("[an email address]", text)
    text = PHONE_RE.sub(lambda m: "[a phone number]" if _is_phone(m) else m.group(0), text)
    if rx := _name_re(ns):
        text = rx.sub(label, text)
    return text


# ---- names spoken in class ---------------------------------------------------------------------

_SPOKEN = re.compile(r"^\[\d+:\d\d(?::\d\d)?\]\s*(.*)$", re.M)
# a capitalised word (or two) in mid-sentence: after a lower-case letter or a comma, or after "name is"
_CANDIDATE = re.compile(r"(?:(?<=[a-z,;] )|(?<=name is ))([A-Z][a-z]{2,}(?: [A-Z][a-z]{2,})?)\b")
_NOT_NAMES = set("""I'm I'll I've I'd Okay Yes Yeah No The What This That These Those There Then Do Does You And But So Why
How When Where Who Which Can Because If Now Let We He She It They My Our Your His Her Mr Mrs Ms Dr Sir Professor
Monday Tuesday Wednesday Thursday Friday Saturday Sunday Mondays Tuesdays Wednesdays Thursdays Fridays January
February March April May June July August September October November December""".split())


def _split_session(text: str) -> tuple[str, str]:
    """(what was said, everything else) of a lecture's Markdown: slide text belongs to the materials."""
    spoken = "\n".join(_SPOKEN.findall(text))
    return spoken, _SPOKEN.sub("", text)


def spoken_names(texts: dict[str, tuple[str, bool]]) -> list[dict]:
    """Names said in class: [{name, count, example, materials}], most frequent first.

    texts maps a source id to (its Markdown, whether it is a lecture). A capitalised word said in
    mid-sentence is a candidate, unless it is also used in lower case (an ordinary word) or is a day, a
    month or the like. `materials` lists the sources outside speech (documents, books, slide text) that
    contain it: a name there may be what the class was discussing rather than someone in the room. The
    result is a proposal, read and cut by a person or the agent before it is saved."""
    spoken, material = [], {}
    for sid, (text, is_lecture) in texts.items():
        if is_lecture:
            said, rest = _split_session(text)
            spoken.append(said)
            material[sid] = rest
        else:
            material[sid] = text
    said = "\n".join(spoken)
    lower_words = set(re.findall(r"\b[a-z]{3,}\b", said)) | {w for t in material.values()
                                                           for w in re.findall(r"\b[a-z]{3,}\b", t)}
    lowered = {sid: t.lower() for sid, t in material.items()}
    found: dict[str, dict] = {}
    for m in _CANDIDATE.finditer(said):
        words = [w for w in m.group(1).split() if w not in _NOT_NAMES and w.lower() not in lower_words]
        if not words or (len(words) < len(m.group(1).split()) and len(words) != 1):
            continue
        name = " ".join(words)
        e = found.get(name)
        if e is None:
            rx = re.compile(r"\b" + re.escape(name.lower()) + r"\b")
            e = found[name] = {"name": name, "count": 0,
                               "example": said[max(0, m.start() - 60): m.end() + 40].replace("\n", " "),
                               "materials": [sid for sid, t in lowered.items() if rx.search(t)]}
        e["count"] += 1
    return sorted(found.values(), key=lambda e: (-e["count"], e["name"]))


def spoken_only(cfg: dict) -> list[str]:
    return [str(n) for n in ((cfg.get("privacy") or {}).get("spoken_only") or []) if str(n).strip()]


_TIMESTAMPED = re.compile(r"^\s*\[\d+:\d\d(?::\d\d)?\].*$", re.M)
_TRANSCRIPT_BLOCK = re.compile(r'(Transcript[^\n]*:\s*\n?""")(.*?)(""")', re.S)


def redact_speech(text: str, ns: list[str], label: str = "[a person]") -> str:
    """The text with the names replaced only where people are speaking: timestamped transcript lines
    ("[48:38] ...") and a prompt's transcript block. A book passage keeps its names, so a student
    who shares a first name with Paul Volcker does not cost the book its Volcker."""
    if not (rx := _name_re(ns)):
        return text
    text = _TIMESTAMPED.sub(lambda m: rx.sub(label, m.group(0)), text)
    return _TRANSCRIPT_BLOCK.sub(lambda m: m.group(1) + rx.sub(label, m.group(2)) + m.group(3), text)


def text_names(cfg: dict) -> list[str]:
    """Every name the library protects: privacy.names (replaced in all text sent off the machine and
    blacked out in images) and privacy.spoken_only (names said in class that the materials also use,
    e.g. a student who shares a first name with a director on a slide: replaced only where people
    speak, see redact_speech)."""
    extra = [str(n) for n in ((cfg.get("privacy") or {}).get("spoken_only") or []) if str(n).strip()]
    return names(cfg) + extra
