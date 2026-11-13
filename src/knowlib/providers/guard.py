"""The privacy check in front of every model off this machine.

Before a prompt leaves, emails, phone numbers and the names in `privacy.names` are replaced with neutral
labels. Before an image leaves, local OCR reads it; one that shows personal information is not sent:
the call raises PersonalInformation (a RuntimeError, so callers fall back as they do when a model
fails: OCR text, or the agent reads the image). Without a local OCR engine to check it, an image is
not sent either.
"""
from __future__ import annotations

import logging
from pathlib import Path

from .. import privacy
from .base import VisionProvider

log = logging.getLogger(__name__)


class PersonalInformation(RuntimeError):
    pass


def local_text(image: Path, cfg: dict) -> str:
    """The image's text read on this machine, by the first OCR engine that works."""
    from ..media.ocr import BACKENDS
    oc = cfg.get("ocr_crosscheck") or {}
    errors = []
    for name in list(oc.get("backends") or []) + ["apple-vision", "tesseract"]:
        if name not in BACKENDS:
            continue
        try:
            return BACKENDS[name](Path(image), oc)
        except Exception as e:  # noqa: BLE001 - try the next engine
            errors.append(f"{name}: {e}")
    raise PersonalInformation(f"no local OCR could check {Path(image).name} before sending it ({'; '.join(errors)})")


class PrivacyGuard(VisionProvider):
    """Wraps a remote provider; everything else is passed through to it."""

    def __init__(self, inner: VisionProvider, cfg: dict):
        self.inner, self.cfg = inner, cfg
        self.names = privacy.names(cfg)
        self._seen: dict[str, list[str]] = {}

    def __getattr__(self, name):
        return getattr(self.__dict__["inner"], name)

    # attributes VisionProvider defines on the class, so __getattr__ would not reach them
    backend = property(lambda self: self.inner.backend)
    model = property(lambda self: self.inner.model)
    is_local = property(lambda self: self.inner.is_local)

    def check_image(self, image: Path) -> list[str]:
        key = str(image)
        if key not in self._seen:
            self._seen[key] = privacy.reasons(local_text(image, self.cfg), self.names)
        return self._seen[key]

    def generate(self, images: list[Path], prompt: str, **kw) -> str:
        for im in images:
            if why := self.check_image(im):
                log.warning("%s stays on this machine: it shows %s", Path(im).name, ", ".join(why))
                raise PersonalInformation(f"{Path(im).name} shows {', '.join(why)}; not sent to {self.inner.model}")
        return self.inner.generate(images, privacy.redact(prompt, self.names), **kw)

    def close(self) -> None:
        self.inner.close()
