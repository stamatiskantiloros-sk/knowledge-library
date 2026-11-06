"""Any OpenAI-compatible chat server with vision: llama.cpp llama-server, LM Studio, vLLM,
mlx_lm/mlx_vlm servers. Counts as local when base_url points at this machine, so it is allowed
under local_only; a remote base_url is treated like an API.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from .base import VisionProvider, image_b64

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


class NotEnoughMemory(RuntimeError):
    """A model does not fit in the memory the system can hand out. Callers that fall back to another
    model on a RuntimeError let this one through, so the run stops and says why."""


def available_bytes() -> int | None:
    """Memory macOS can hand out without swapping: free, inactive, speculative and purgeable pages.
    None where vm_stat is missing (not a Mac)."""
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
    except OSError:
        return None
    page = re.search(r"page size of (\d+)", out)
    counts = [re.search(rf"Pages {k}:\s+(\d+)", out) for k in ("free", "inactive", "speculative", "purgeable")]
    if not page or not all(counts):
        return None
    return int(page[1]) * sum(int(m[1]) for m in counts)


def largest_processes(n: int = 3) -> str:
    """'Brave Browser 2.1 GB, ...': the biggest memory users, for the message."""
    out = subprocess.run(["ps", "-axo", "rss=,comm="], capture_output=True, text=True).stdout
    by_app: dict[str, int] = {}
    for line in out.splitlines():
        rss, _, cmd = line.strip().partition(" ")
        app = re.sub(r"\.app/.*", "", cmd).rsplit("/", 1)[-1]
        by_app[app] = by_app.get(app, 0) + int(rss or 0) * 1024
    top = sorted(by_app.items(), key=lambda kv: -kv[1])[:n]
    return ", ".join(f"{app} {size / 1e9:.1f} GB" for app, size in top)


class OpenAICompatProvider(VisionProvider):
    backend = "openai"

    def __init__(self, section: dict):
        self.base_url = section.get("base_url", "http://localhost:8080/v1").rstrip("/")
        self.model = section.get("openai_model", "local")
        self.max_tokens = int(section.get("max_tokens", 1200))
        self.temperature = float(section.get("temperature", 0.0))
        self.max_px = section.get("openai_image_max_px")       # None = send native frames
        self.api_key = os.environ.get(section.get("api_key_env", "OPENAI_API_KEY"), "none")
        self.is_local = (urlparse(self.base_url).hostname or "") in LOCAL_HOSTS
        self.timeout = float(section.get("timeout_s", 900))
        self.manage_lms = bool(section.get("manage_lms"))      # load and unload through LM Studio's lms CLI
        self.context_length = section.get("context_length")   # with manage_lms
        self.parallel = section.get("parallel")                # slots share the memory; we send one at a time
        self.reasoning_effort = section.get("reasoning_effort")   # "none" skips the model's reasoning
        self.memory_check = section.get("memory_check", True)     # with manage_lms: refuse a model that will not fit
        self._loaded = False
        self.last_finish = None

    def _lms(self, *args: str) -> None:
        lms = shutil.which("lms") or str(Path.home() / ".lmstudio/bin/lms")
        r = subprocess.run([lms, *args], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"lms {args[0]} failed: {(r.stderr or r.stdout).strip()[-300:]}")

    def _check_memory(self) -> None:
        """Stop before loading a model that macOS would have to swap: it would run many times slower
        and LM Studio drops a request after ten minutes without output."""
        free = available_bytes()
        if free is None:
            return
        lms = shutil.which("lms") or str(Path.home() / ".lmstudio/bin/lms")
        loaded = subprocess.run([lms, "ps", "--json"], capture_output=True, text=True)
        r = subprocess.run([lms, "ls", "--json"], capture_output=True, text=True)
        try:
            if any(self.model in (m.get("identifier"), m.get("modelKey")) for m in json.loads(loaded.stdout or "[]")):
                return                                         # already in memory
            size = next((m.get("sizeBytes") for m in json.loads(r.stdout) if m.get("modelKey") == self.model), None)
        except ValueError:
            return
        if size and free < size:
            raise NotEnoughMemory(
                f"{self.model} needs about {size / 1e9:.1f} GB but only {free / 1e9:.1f} GB is free. "
                f"Largest users: {largest_processes()}. Close what you can and run again, "
                "or set memory_check: false to load it anyway (slowly, through swap).")

    def close(self) -> None:
        if self._loaded:
            self._lms("unload", self.model)
            self._loaded = False

    def generate(self, images: list[Path], prompt: str, *, json_mode: bool = False,
                 max_tokens: int | None = None) -> str:
        if self.manage_lms and not self._loaded:
            if self.memory_check:
                self._check_memory()
            ctx =["-c", str(self.context_length)] if self.context_length else []
            ctx += ["--parallel", str(self.parallel)] if self.parallel else []
            self._lms("load", self.model, "-y", "--identifier", self.model, *ctx)
            self._loaded = True
        content = [{"type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{image_b64(p, self.max_px)}"}}
                   for p in images]
        content.append({"type": "text", "text": prompt + ("\n\nReply with a single JSON object only." if json_mode else "")})
        body = {"model": self.model, "messages": [{"role": "user", "content": content}],
                "max_tokens": max_tokens or self.max_tokens, "temperature": self.temperature}
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        req = urllib.request.Request(f"{self.base_url}/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {self.api_key}"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            choice = json.loads(r.read())["choices"][0]
        self.last_finish = choice.get("finish_reason")
        reply = choice["message"]["content"] or ""
        reply = re.sub(r"<thought>.*?</thought>", "", reply, flags=re.S)   # Gemma's reasoning
        return reply.split("</think>")[-1].strip()           # reasoning models

    def provenance(self) -> dict:
        return {"backend": self.backend, "model": self.model, "base_url": self.base_url}
