"""Personal information blacked out of video frames before any model sees them.

Each frame image that frame selection will look at is read by Apple Vision on this Mac. A line of
text with a name from `privacy.names`, an email address or a phone number is covered with a black
box in the image file itself, so the frame that is sent to the reading model, kept as an asset,
captioned and shown in a handbook carries no such information. Names that are not in the list
cannot be found this way: list everyone who may appear (class lists, participants) in the config.
"""
from __future__ import annotations

from pathlib import Path

from .. import privacy

PAD = 0.004                     # box padding, as a share of the image's width and height


def lines(path: Path, languages: list[str] | None = None) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Apple Vision's text lines with their boxes (x, y, w, h; normalised, y from the bottom)."""
    from ocrmac import ocrmac
    try:
        res = ocrmac.OCR(str(path), recognition_level="accurate", language_preference=languages or None).recognize()
    except Exception:  # noqa: BLE001 - e.g. a language this macOS version's Vision lacks
        res = ocrmac.OCR(str(path), recognition_level="accurate").recognize()
    return [(text, box) for text, _conf, box in res]


def redact_image(path: Path, names: list[str], languages: list[str] | None = None,
                 found: list | None = None) -> list[str]:
    """Black out every text line in the image that carries personal information. Returns the reasons
    (e.g. "a name", "an email address"), one per line covered; the file is rewritten only if any.
    `found` lets a test pass the OCR result instead of running Apple Vision."""
    from PIL import Image, ImageDraw
    hits = [(text, box, why) for text, box in (found if found is not None else lines(path, languages))
            if (why := privacy.reasons(text, names))]
    if not hits:
        return []
    with Image.open(path) as im:
        im = im.convert("RGB")
        w, h = im.size
        draw = ImageDraw.Draw(im)
        for _text, (x, y, bw, bh), _why in hits:
            draw.rectangle([(x - PAD) * w, (1 - y - bh - PAD) * h, (x + bw + PAD) * w, (1 - y + PAD) * h], fill="black")
        fmt = "JPEG" if path.suffix.lower() in (".jpg", ".jpeg") else "PNG"
        im.save(path, fmt, **({"quality": 95} if fmt == "JPEG" else {}))
    return [", ".join(why) for _t, _b, why in hits]


def redact_candidates(cands: list, cfg: dict, progress=None) -> dict:
    """Redact the sample frames of every candidate group (what selection sends to the model and picks
    the kept frame from). Returns {"frames": checked, "redacted": frames changed, "lines": lines covered}."""
    names = privacy.names(cfg)
    languages = (cfg.get("ocr_crosscheck") or {}).get("languages") or ["el-GR", "en-US"]   # Greek read as Greek
    seen, changed, covered = set(), 0, 0
    samples = [f for c in cands for f in (c.samples or c.frames[-1:])]
    for n, f in enumerate(samples, 1):
        if f.path in seen:
            continue
        seen.add(f.path)
        if progress:
            progress.stage("privacy redaction", n, len(samples))
        reasons = redact_image(Path(f.path), names, languages)
        if reasons:
            changed += 1
            covered += len(reasons)
    return {"frames": len(seen), "redacted": changed, "lines": covered}
