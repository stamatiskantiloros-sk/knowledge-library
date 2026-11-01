"""Model provider layer: ingestors ask for a capability (vision -> text/JSON), not a vendor.

Backends: mlx-vlm and ollama (local), api (any model LiteLLM supports: Claude, GPT, Gemini...),
mock (deterministic, for tests). A config- or session-level local_only blocks every API call.
"""
from __future__ import annotations

import base64
import io
import json
import re
from abc import ABC, abstractmethod
from pathlib import Path

from PIL import Image


class LocalOnlyViolation(RuntimeError):
    pass


class VisionProvider(ABC):
    backend = "base"
    model: str | None = None
    is_local = True

    @abstractmethod
    def generate(self, images: list[Path], prompt: str, *, json_mode: bool = False,
                 max_tokens: int | None = None) -> str: ...

    def json(self, images: list[Path], prompt: str, **kw) -> dict:
        return parse_json(self.generate(images, prompt, json_mode=True, **kw))

    def provenance(self) -> dict:
        return {"backend": self.backend, "model": self.model}

    def close(self) -> None:
        """Free model memory before the next stage loads its own model."""


def parse_json(text: str) -> dict:
    """Pull the first JSON object out of a model reply (handles ```json fences and chatter)."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [fenced.group(1)] if fenced else []
    start = text.find("{")
    if start >= 0:
        depth, in_str, esc = 0, False, False
        for i, ch in enumerate(text[start:], start):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start:i + 1])
                    break
    for c in candidates:
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            continue
    raise ValueError(f"no JSON object in model reply ({len(text)} chars): {text[:300]!r} ... {text[-300:]!r}")


def image_b64(path: Path, max_px: int | None = None, fmt: str = "JPEG") -> str:
    img = Image.open(path)
    img.load()
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    if max_px and max(img.size) > max_px:
        img.thumbnail((max_px, max_px), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, fmt, quality=92)
    return base64.b64encode(buf.getvalue()).decode()


_CACHE: dict[tuple, VisionProvider] = {}


def get_vision_provider(section: dict, cfg: dict) -> VisionProvider:
    """Build (or reuse) the provider described by a config section such as frame_selection."""
    backend = section.get("backend", "mlx-vlm")
    if backend == "api" and cfg.get("local_only"):
        raise LocalOnlyViolation(
            "local_only is set for this session/library; refusing to call an API model. "
            "Use backend mlx-vlm or ollama, or drop local_only for non-confidential material.")
    key = (backend, section.get("model"), section.get("ollama_model"), section.get("api_model"),
           section.get("base_url"), section.get("openai_model"))
    if key in _CACHE:
        return _CACHE[key]
    if backend == "openai":
        from .openai_compat import OpenAICompatProvider
        prov: VisionProvider = OpenAICompatProvider(section)
        if not prov.is_local and cfg.get("local_only"):
            raise LocalOnlyViolation(f"local_only is set and {prov.base_url} is not on this machine")
    elif backend == "mlx-vlm":
        from .mlx_vlm import MLXVLMProvider
        prov = MLXVLMProvider(section)
    elif backend == "ollama":
        from .ollama import OllamaProvider
        prov = OllamaProvider(section)
    elif backend == "api":
        from .api import APIProvider
        prov = APIProvider(section)
    elif backend == "mock":
        from .mock import MockProvider
        prov = MockProvider(section)
    else:
        raise ValueError(f"unknown vision backend: {backend}")
    _CACHE[key] = prov
    return prov


def release_all() -> None:
    for p in _CACHE.values():
        p.close()
    _CACHE.clear()
