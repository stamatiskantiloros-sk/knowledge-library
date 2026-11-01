"""Local vision model on the Apple GPU via mlx-vlm (default: Qwen-VL class, 4-bit)."""
from __future__ import annotations

import gc
from pathlib import Path

from .base import VisionProvider


class MLXVLMProvider(VisionProvider):
    backend = "mlx-vlm"

    def __init__(self, section: dict):
        self.model = section.get("model")
        self.max_tokens = int(section.get("max_tokens", 1200))
        self.temperature = float(section.get("temperature", 0.0))
        self._model = self._processor = self._config = None

    def _load(self):
        if self._model is None:
            from mlx_vlm import load
            from mlx_vlm.utils import load_config
            self._model, self._processor = load(self.model)
            self._config = load_config(self.model)

    def generate(self, images: list[Path], prompt: str, *, json_mode: bool = False,
                 max_tokens: int | None = None) -> str:
        self._load()
        from mlx_vlm import generate
        from mlx_vlm.prompt_utils import apply_chat_template
        if json_mode:
            prompt += "\n\nReply with a single JSON object only."
        formatted = apply_chat_template(self._processor, self._config, prompt, num_images=len(images))
        kw = dict(image=[str(p) for p in images], max_tokens=max_tokens or self.max_tokens, verbose=False)
        try:
            out = generate(self._model, self._processor, formatted, temperature=self.temperature, **kw)
        except TypeError:  # older mlx-vlm releases call it temp
            out = generate(self._model, self._processor, formatted, temp=self.temperature, **kw)
        return getattr(out, "text", out)

    def provenance(self) -> dict:
        return {"backend": self.backend, "model": self.model}

    def close(self) -> None:
        self._model = self._processor = self._config = None
        gc.collect()
        try:
            import mlx.core as mx
            mx.clear_cache() if hasattr(mx, "clear_cache") else mx.metal.clear_cache()
        except Exception:
            pass
