"""A PDF page's text with subscripts kept: "Q_BEP", not "QBEP".

A subscript in a PDF is only formatting, a smaller span set lower on the line, so plain text
extraction glues it to the letter before it. Here such a span is written after an underscore, the
way it reads in a formula. Only a short run of letters or digits straight after a letter counts:
footnote marks, ordinals ("4th") and trademarks are raised, not lowered, and stay as they are.
"""
from __future__ import annotations

import re

SMALLER = 0.85      # a subscript's size, at most, against the letter before it
LOWER = 0.15        # how far below that letter's baseline it sits, at least, in its sizes
SUBSCRIPT = re.compile(r"[A-Za-z0-9]{1,6}")


def _subscript(prev: dict, span: dict) -> bool:
    text = span["text"].strip()
    return (bool(SUBSCRIPT.fullmatch(text)) and prev["text"][-1:].isalpha()
            and span["size"] <= prev["size"] * SMALLER
            and span["origin"][1] - prev["origin"][1] >= prev["size"] * LOWER)


def page_text(page, clip=None) -> str:
    """`page.get_text()`, with each subscript written as "_BEP" after its letter. Only the lines
    that hold a subscript change; the rest of the text is MuPDF's own, line breaks included."""
    kw = {"clip": clip} if clip is not None else {}
    text = page.get_text(**kw)
    for block in page.get_text("dict", **kw)["blocks"]:
        for line in block.get("lines", []):
            plain, marked, prev = [], [], None
            for span in line["spans"]:
                plain.append(span["text"])
                if prev is not None and _subscript(prev, span):
                    marked.append("_" + span["text"].lstrip())
                    continue                                  # one level: the letter stays the reference
                marked.append(span["text"])
                if span["text"].strip():
                    prev = span
            plain, marked = "".join(plain).strip(), "".join(marked).strip()
            if marked != plain:
                text = text.replace(plain, marked, 1)
    return text
