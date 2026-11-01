"""Deterministic stand-in for a vision model, used by the test suite and dry runs.

Keeps any frame with visible structure, drops near-uniform frames (blank/transition), and
reads text with Tesseract when it is installed so the cross-check path is exercised.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image

from .base import VisionProvider


class MockProvider(VisionProvider):
    backend = "mock"

    def __init__(self, section: dict):
        self.model = "mock"

    def _text(self, p: Path) -> str:
        if not shutil.which("tesseract"):
            return ""
        r = subprocess.run(["tesseract", str(p), "stdout", "-l", "eng"], capture_output=True, text=True)
        return r.stdout.strip()

    def generate(self, images: list[Path], prompt: str, *, json_mode: bool = False,
                 max_tokens: int | None = None) -> str:
        if not json_mode:
            return "\n".join(self._text(p) for p in images)
        stds = [float(np.asarray(Image.open(p).convert("L")).std()) for p in images]
        best = int(np.argmax(stds))
        keep = stds[best] > 5
        text = self._text(images[best]) if keep else ""
        return json.dumps({
            "decision": "keep" if keep else "drop", "best_frame": best,
            "reason": "structured content" if keep else "near-uniform frame",
            "caption": "Slide with text" if text else "Visual frame",
            "slide_text": text, "content_type": "slide" if keep else "transition",
            "dense_text": False,
        })
