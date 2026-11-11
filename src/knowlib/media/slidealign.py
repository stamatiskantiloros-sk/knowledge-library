"""Which slide was on screen when: an audio recording matched to its slide deck, without video.

A lecture recorded as audio has no frames to show when the slide changed, so the speech is matched
to the slides' own text. The transcript is cut into windows of about 30 seconds; each window is
scored against every slide by the distinctive words they share (tf-idf over the slides, so a word on
every slide counts for little), and the scores are standardised per window. A path through the
windows then picks one slide per window, moving only forward, with a cost for each move and a larger
one for each slide skipped; the path may start late and stop early (a recording that ends at the
break). Slides the path never visits were not discussed. A slide whose windows match it weakly is
reported, so its boundaries can be checked by hand.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

STOP = set("""a about above after again against all also am an and any are as at be because been before being below
between both but by can could did do does doing down during each few for from further had has have having he her
here hers him his how i if in into is it its itself just let like me more most my no nor not now of off on once only
or other our ours out over own same she should so some such than that the their theirs them then there these they
this those through to too under until up very was we were what when where which while who whom why will with would
you your yours yeah okay ok alright right so um uh know think going get got say said thing things really kind sort
mean well basically actually maybe one two first also see look want need make way lot much many example""".split())


def words(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-zA-Z][a-zA-Z\-]{2,}", text.lower()):
        if w in STOP:
            continue
        for suf in ("ing", "ies", "es", "ed", "s"):          # crude stemming: plural and tense
            if w.endswith(suf) and len(w) - len(suf) >= 3:
                w = w[: -len(suf)] + ("y" if suf == "ies" else "")
                break
        out.append(w)
    return out


@dataclass
class Window:
    start: float
    end: float
    text: str


@dataclass
class Placement:
    slide: int                     # the slide's number in the deck
    start: float | None = None     # None: not discussed
    end: float | None = None
    score: float = 0.0             # mean standardised match of its windows
    windows: list[int] = field(default_factory=list)


def windows(segs: list[dict], size: float = 30.0) -> list[Window]:
    out, cur, t0 = [], [], None
    for s in segs:
        if t0 is None:
            t0 = s["start"]
        cur.append(s["text"])
        if s["end"] - t0 >= size:
            out.append(Window(t0, s["end"], " ".join(cur)))
            cur, t0 = [], None
    if cur:
        out.append(Window(t0, segs[-1]["end"], " ".join(cur)))
    return out


def similarity(wins: list[Window], slides: list[tuple[int, str]]) -> list[list[float]]:
    """Each window's cosine similarity to each slide, standardised across the slides."""
    docs = [Counter(words(t)) for _, t in slides]
    df = Counter(w for d in docs for w in d)
    n = len(docs)
    idf = {w: math.log((1 + n) / (1 + c)) + 1 for w, c in df.items()}

    def vec(c: Counter) -> dict:
        v = {w: (1 + math.log(f)) * idf[w] for w, f in c.items() if w in idf}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {w: x / norm for w, x in v.items()}

    svecs = [vec(d) for d in docs]
    out = []
    for win in wins:
        wv = vec(Counter(words(win.text)))
        raw = [sum(x * sv.get(w, 0.0) for w, x in wv.items()) for sv in svecs]
        mean = sum(raw) / len(raw)
        sd = math.sqrt(sum((r - mean) ** 2 for r in raw) / len(raw)) or 1.0
        out.append([(r - mean) / sd if any(raw) else 0.0 for r in raw])
    return out


def path(sim: list[list[float]], move: float = 1.0, skip: float = 0.6, start_skip: float = 0.3) -> list[int]:
    """The best slide index for each window, moving only forward. move: the cost of a change of
    slide; skip: the cost of each slide passed over; start_skip: per slide not shown at the start."""
    W, S = len(sim), len(sim[0]) if sim else 0
    if not W or not S:
        return []
    neg = float("-inf")
    best = [[neg] * S for _ in range(W)]
    back = [[0] * S for _ in range(W)]
    for j in range(S):
        best[0][j] = sim[0][j] - start_skip * j
    for w in range(1, W):
        run_best, run_arg = neg, 0            # best of best[w-1][k] + skip*k for k < j
        for j in range(S):
            stay = best[w - 1][j]
            moved = run_best - move - skip * (j - 1) if j else neg
            if stay >= moved:
                best[w][j], back[w][j] = stay + sim[w][j], j
            else:
                best[w][j], back[w][j] = moved + sim[w][j], run_arg
            cand = best[w - 1][j] + skip * j
            if cand > run_best:
                run_best, run_arg = cand, j
    j = max(range(S), key=lambda k: best[W - 1][k])       # may stop early: no cost for slides left
    out = [0] * W
    for w in range(W - 1, -1, -1):
        out[w] = j
        j = back[w][j]
    return out


def align(segs: list[dict], slides: list[tuple[int, str]], *, window_s: float = 30.0,
          move: float = 1.0, skip: float = 0.6) -> list[Placement]:
    """slides: (number, text) in deck order. One Placement per slide, in order."""
    wins = windows(segs, window_s)
    sim = similarity(wins, slides)
    idx = path(sim, move=move, skip=skip)
    places = [Placement(n) for n, _ in slides]
    for w, j in enumerate(idx):
        p = places[j]
        p.windows.append(w)
        p.start = wins[w].start if p.start is None else p.start
        p.end = wins[w].end
    for j, p in enumerate(places):
        if p.windows:
            p.score = sum(sim[w][j] for w in p.windows) / len(p.windows)
    shown = [p for p in places if p.start is not None]
    for a, b in zip(shown, shown[1:]):                     # a slide lasts until the next one starts
        a.end = b.start
    return places
