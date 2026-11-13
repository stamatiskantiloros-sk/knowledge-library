"""The privacy check in front of every model off this machine.

Before a prompt leaves, emails, phone numbers and the names in `privacy.names` are replaced with neutral
labels. Before an image leaves, local OCR reads it line by line; each line that shows personal
information is blacked out on a copy, the copy is read again to make sure nothing is left, and the copy
is sent. An image that still shows personal information after masking, or that no local OCR engine
could check, is not sent: the call raises PersonalInformation (a RuntimeError, so callers fall back as
they do when a model fails: OCR text, or the agent reads the image).
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from .. import privacy
from .base import VisionProvider

log = logging.getLogger(__name__)
PAD = 4                                   # pixels of margin around a masked line


class PersonalInformation(RuntimeError):
    pass


def _apple_lines(image: Path, oc: dict) -> list[tuple[str, tuple[int, int, int, int]]]:
    from ocrmac import ocrmac
    from PIL import Image
    with Image.open(image) as im:
        w, h = im.size
    try:
        res = ocrmac.OCR(str(image), recognition_level="accurate",
                         language_preference=oc.get("languages") or None).recognize()
    except Exception:  # noqa: BLE001 - e.g. a language this macOS version's Vision lacks
        res = ocrmac.OCR(str(image), recognition_level="accurate").recognize()
    out = []
    for text, _conf, (x, y, bw, bh) in res:          # normalized, y from the bottom
        out.append((text, (int(x * w), int((1 - y - bh) * h), int((x + bw) * w) + 1, int((1 - y) * h) + 1)))
    return out


def _tesseract_lines(image: Path, oc: dict) -> list[tuple[str, tuple[int, int, int, int]]]:
    if not shutil.which("tesseract"):
        raise RuntimeError("tesseract not installed")
    r = subprocess.run(["tesseract", str(image), "stdout", "-l", oc.get("tesseract_langs", "ell+eng"), "tsv"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-300:])
    lines: dict[tuple, list] = {}
    for row in r.stdout.splitlines()[1:]:
        f = row.split("\t")
        if len(f) < 12 or not f[11].strip():
            continue
        left, top, bw, bh = map(int, f[6:10])
        lines.setdefault((f[2], f[3], f[4]), []).append((f[11], left, top, left + bw, top + bh))
    return [(" ".join(t for t, *_ in ws), (min(w[1] for w in ws), min(w[2] for w in ws),
                                           max(w[3] for w in ws), max(w[4] for w in ws))) for ws in lines.values()]


LINE_READERS = {"apple-vision": _apple_lines, "tesseract": _tesseract_lines}


def local_lines(image: Path, cfg: dict) -> list[tuple[str, tuple[int, int, int, int]]]:
    """The image's lines of text with their boxes in pixels, read on this machine."""
    oc = cfg.get("ocr_crosscheck") or {}
    errors = []
    for name in list(oc.get("backends") or []) + ["apple-vision", "tesseract"]:
        if name not in LINE_READERS:
            continue
        try:
            return LINE_READERS[name](Path(image), oc)
        except Exception as e:  # noqa: BLE001 - try the next engine
            errors.append(f"{name}: {e}")
    raise PersonalInformation(f"no local OCR could check {Path(image).name} before sending it ({'; '.join(errors)})")


def local_text(image: Path, cfg: dict) -> str:
    return "\n".join(t for t, _ in local_lines(image, cfg))


class PrivacyGuard(VisionProvider):
    """Wraps a remote provider; everything else is passed through to it."""

    def __init__(self, inner: VisionProvider, cfg: dict):
        self.inner, self.cfg = inner, cfg
        self.names = privacy.names(cfg)                # masked in images and replaced in text
        self.text_names = privacy.text_names(cfg)      # replaced in text only
        self._sendable: dict[str, Path] = {}
        self._tmp = Path(tempfile.mkdtemp(prefix="knowlib-masked-"))

    def __getattr__(self, name):
        return getattr(self.__dict__["inner"], name)

    # attributes VisionProvider defines on the class, so __getattr__ would not reach them
    backend = property(lambda self: self.inner.backend)
    model = property(lambda self: self.inner.model)
    is_local = property(lambda self: self.inner.is_local)

    def sendable(self, image: Path) -> Path:
        """The image itself when it shows no personal information, else a copy with those lines masked."""
        key = str(image)
        if key in self._sendable:
            return self._sendable[key]
        hits = [(t, box) for t, box in local_lines(image, self.cfg) if privacy.reasons(t, self.names)]
        if not hits:
            self._sendable[key] = Path(image)
            return Path(image)
        from PIL import Image, ImageDraw
        out = self._tmp / f"{len(self._sendable):04d}-{Path(image).stem}.png"
        with Image.open(image) as im:
            im = im.convert("RGB")
            draw = ImageDraw.Draw(im)
            for _, (x0, y0, x1, y1) in hits:
                draw.rectangle((x0 - PAD, y0 - PAD, x1 + PAD, y1 + PAD), fill="black")
            im.save(out)
        if left := privacy.reasons(local_text(out, self.cfg), self.names):
            raise PersonalInformation(f"{Path(image).name} still shows {', '.join(left)} after masking; "
                                      f"not sent to {self.inner.model}")
        log.info("%s: %d line(s) with personal information masked before sending", Path(image).name, len(hits))
        self._sendable[key] = out
        return out

    def generate(self, images: list[Path], prompt: str, **kw) -> str:
        send = [self.sendable(im) for im in images]
        return self.inner.generate(send, privacy.redact(prompt, self.text_names), **kw)

    def close(self) -> None:
        self.inner.close()
        shutil.rmtree(self._tmp, ignore_errors=True)
