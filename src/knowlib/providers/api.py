"""API vision models (Claude, GPT, Gemini, ...) through LiteLLM. Never used when local_only is set.

Keys come from the usual environment variables (ANTHROPIC_API_KEY, OPENAI_API_KEY, ...).
Images are sent at <= model_view_max_px: the APIs downscale larger images anyway.
"""
from __future__ import annotations

from pathlib import Path

from .base import VisionProvider, image_b64


class APIProvider(VisionProvider):
    backend = "api"
    is_local = False

    def __init__(self, section: dict):
        self.model = section.get("api_model")
        if not self.model:
            raise ValueError("backend: api needs api_model (a LiteLLM model string)")
        self.max_tokens = int(section.get("max_tokens", 1200))
        self.temperature = float(section.get("temperature", 0.0))
        self.max_px = int(section.get("api_image_max_px", 1568))

    def generate(self, images: list[Path], prompt: str, *, json_mode: bool = False,
                 max_tokens: int | None = None) -> str:
        import litellm
        content = [{"type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{image_b64(p, self.max_px)}"}}
                   for p in images]
        content.append({"type": "text", "text": prompt + ("\n\nReply with a single JSON object only." if json_mode else "")})
        resp = litellm.completion(model=self.model, messages=[{"role": "user", "content": content}],
                                  max_tokens=max_tokens or self.max_tokens, temperature=self.temperature)
        return resp.choices[0].message.content
