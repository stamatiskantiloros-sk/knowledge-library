"""Personal information that must not leave the machine.

Before an image goes to a model off this machine, classic OCR (local) reads it; an email address, a
phone number or a name from `privacy.names` keeps it here, and the agent reads it instead. The same
patterns let a step that sends text (a review, a reading) replace names with neutral labels first.
"""
from __future__ import annotations

import re

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"(?<![\d.])\+?\d[\d ()-]{8,}\d(?![\d.])")


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
    if PHONE_RE.search(text):
        out.append("a phone number")
    if (rx := _name_re(ns)) and (m := rx.search(text)):
        out.append("a name")
    return out


def redact(text: str, ns: list[str], label: str = "[a person]") -> str:
    """The text with emails, phone numbers and the listed names replaced by neutral labels."""
    text = EMAIL_RE.sub("[an email address]", text)
    text = PHONE_RE.sub("[a phone number]", text)
    if rx := _name_re(ns):
        text = rx.sub(label, text)
    return text
