"""Local vision model served by Ollama (http://localhost:11434)."""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from .base import VisionProvider, image_b64


class OllamaProvider(VisionProvider):
    backend = "ollama"

    def __init__(self, section: dict):
        self.model = section.get("ollama_model", "qwen2.5vl:7b")
        self.url = section.get("ollama_url", "http://localhost:11434").rstrip("/")
        self.max_tokens = int(section.get("max_tokens", 1200))
        self.temperature = float(section.get("temperature", 0.0))

    def generate(self, images: list[Path], prompt: str, *, json_mode: bool = False,
                 max_tokens: int | None = None) -> str:
        body = {
            "model": self.model, "stream": False,
            "messages": [{"role": "user", "content": prompt,
                          "images": [image_b64(p) for p in images]}],
            "options": {"temperature": self.temperature, "num_predict": max_tokens or self.max_tokens},
        }
        if json_mode:
            body["format"] = "json"
        req = urllib.request.Request(f"{self.url}/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read())["message"]["content"]
