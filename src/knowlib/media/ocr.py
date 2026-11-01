"""OCR cross-check. The VLM is the primary reader; classic OCR runs on every kept frame and the
slide is flagged where they disagree, especially on numbers - so the frontier model checks the
image instead of trusting plausible-looking hallucinated text.

Backends: apple-vision (macOS Vision framework via ocrmac; fast, local) and tesseract
(with the Greek model `ell` as a third opinion).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import unicodedata
from collections import Counter
from pathlib import Path

_NUM = re.compile(r"(?<![\w.])[-+]?\d[\d.,]*%?")


def apple_vision(path: Path, oc: dict) -> str:
    from ocrmac import ocrmac
    try:
        res = ocrmac.OCR(str(path), recognition_level="accurate",
                         language_preference=oc.get("languages") or None).recognize()
    except Exception:  # noqa: BLE001 - e.g. a language this macOS version's Vision lacks
        res = ocrmac.OCR(str(path), recognition_level="accurate").recognize()
    # results: (text, confidence, (x, y, w, h)) with y from the bottom; sort top-down, left-right
    res.sort(key=lambda r: (-round(r[2][1] + r[2][3], 2), r[2][0]))
    return "\n".join(r[0] for r in res)


def tesseract(path: Path, oc: dict) -> str:
    if not shutil.which("tesseract"):
        raise RuntimeError("tesseract not installed (brew install tesseract tesseract-lang)")
    r = subprocess.run(["tesseract", str(path), "stdout", "-l", oc.get("tesseract_langs", "ell+eng")],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-300:])
    return r.stdout.strip()


BACKENDS = {"apple-vision": apple_vision, "tesseract": tesseract}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")
    text = re.sub(r"[*_`#>|\-]+", " ", text)          # markdown the VLM may add
    return re.sub(r"\s+", " ", text).strip()


def numbers(text: str) -> list[str]:
    return [n.strip(".,").replace(",", ".") for n in _NUM.findall(unicodedata.normalize("NFKC", text))]


def compare(vlm_text: str, ocr_text: str, threshold: float) -> dict:
    a, b = normalize(vlm_text), normalize(ocr_text)
    if not a and not b:
        return {"similarity": 1.0, "flag": False}
    # word overlap, not word order: OCR reads a table cell by cell where the VLM keeps its rows
    ca, cb = Counter(a.split()), Counter(b.split())
    sim = 2 * sum((ca & cb).values()) / (sum(ca.values()) + sum(cb.values())) if a and b else 0.0
    na, nb = numbers(vlm_text), numbers(ocr_text)
    only_vlm = sorted(set(na) - set(nb))
    only_ocr = sorted(set(nb) - set(na))
    flag = (sim < threshold and len(b) > 20) or bool(only_vlm and nb) or bool(only_ocr and na)
    return {"similarity": round(sim, 3), "numbers_only_vlm": only_vlm[:10],
            "numbers_only_ocr": only_ocr[:10], "flag": bool(flag)}


def crosscheck(frame: Path, vlm_text: str, cfg: dict) -> dict:
    oc = cfg["ocr_crosscheck"]
    out = {"backends": {}, "flag": False, "errors": []}
    for name in oc.get("backends", []):
        try:
            text = BACKENDS[name](frame, oc)
        except Exception as e:   # noqa: BLE001 - a missing OCR backend must not stop ingestion
            out["errors"].append(f"{name}: {e}")
            continue
        cmp_ = compare(vlm_text, text, float(oc.get("similarity_threshold", 0.75)))
        out["backends"][name] = {"text": text, **cmp_}
    _tally(out)
    return out


def _tally(out: dict) -> None:
    flagged = [n for n, r in out["backends"].items() if r["flag"]]
    # majority of the classic OCRs must disagree with the VLM (with two, both must)
    n = len(out["backends"])
    out["flag"] = n > 0 and len(flagged) >= n // 2 + 1


def recurring_lines(texts: list[str], min_share: float = 0.3) -> set[str]:
    """Lines OCR finds on many frames of a session: app menus, headers, footers. Lines without
    letters are never included, so recurring numbers stay checked."""
    seen = Counter(line for t in texts for line in {normalize(l) for l in t.splitlines()})
    return {line for line, n in seen.items()
            if n >= max(3, min_share * len(texts)) and any(c.isalpha() for c in line)}


def strip_lines(text: str, ignore: set[str]) -> str:
    return "\n".join(l for l in text.splitlines() if normalize(l) not in ignore)


def recheck_without_recurring(items: list[tuple[str | None, dict]], oc: dict) -> None:
    """Compare again without a session's recurring screen text. items: (VLM text or None, crosscheck)."""
    threshold = float(oc.get("similarity_threshold", 0.75))
    for name in {n for _, c in items for n in c["backends"]}:
        results = [(vlm, c["backends"][name]) for vlm, c in items if name in c["backends"]]
        ignore = recurring_lines([r["text"] for _, r in results], float(oc.get("recurring_line_share", 0.3)))
        for vlm, r in results:
            if vlm is not None:
                r.update(compare(strip_lines(vlm, ignore), strip_lines(r["text"], ignore), threshold))
    for vlm, c in items:
        if vlm is not None:
            _tally(c)
