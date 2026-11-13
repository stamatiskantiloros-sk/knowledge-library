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
import threading
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from .base import VisionProvider, image_b64

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _key_file(section: dict) -> str | None:
    """The key from api_key_file (e.g. ~/.google_key) when its environment variable is not set."""
    path = section.get("api_key_file")
    if path and Path(path).expanduser().exists():
        return Path(path).expanduser().read_text().strip() or None
    return None


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
        self.api_key = os.environ.get(section.get("api_key_env") or "OPENAI_API_KEY") or _key_file(section) or "none"
        self.is_local = (urlparse(self.base_url).hostname or "") in LOCAL_HOSTS
        self.timeout = float(section.get("timeout_s", 900))
        self.manage_lms = bool(section.get("manage_lms"))      # load and unload through LM Studio's lms CLI
        self.context_length = section.get("context_length")   # with manage_lms
        self.parallel = section.get("parallel")                # slots share the memory; we send one at a time
        self.reasoning_effort = section.get("reasoning_effort")   # "none" skips the model's reasoning
        self.memory_check = section.get("memory_check", True)     # with manage_lms: refuse a model that will not fit
        self.routing = section.get("provider_routing")            # OpenRouter's `provider` preferences
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

    def _already_loaded(self) -> dict | None:
        """LM Studio's entry for this model if it is in memory already (loaded by hand or by an earlier run)."""
        lms = shutil.which("lms") or str(Path.home() / ".lmstudio/bin/lms")
        try:
            r = subprocess.run([lms, "ps", "--json"], capture_output=True, text=True, timeout=30)
            return next((m for m in json.loads(r.stdout or "[]") if m.get("identifier") == self.model), None)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    def close(self) -> None:
        if self._loaded:
            self._lms("unload", self.model)
            self._loaded = False

    def generate(self, images: list[Path], prompt: str, *, json_mode: bool = False,
                 max_tokens: int | None = None) -> str:
        self._ensure_loaded()
        content = [{"type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{image_b64(p, self.max_px)}"}}
                   for p in images]
        content.append({"type": "text", "text": prompt + ("\n\nReply with a single JSON object only." if json_mode else "")})
        body = {"model": self.model, "messages": [{"role": "user", "content": content}],
                "max_tokens": max_tokens or self.max_tokens, "temperature": self.temperature}
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        choice = self._post(body)["choices"][0]
        self.last_finish = choice.get("finish_reason")
        # Gemini leaves "content" out when a reply has no text (thinking spent the tokens, or it was
        # blocked): treat that as an empty reply so the caller retries or flags it, not a crash.
        reply = (choice.get("message") or {}).get("content") or ""
        reply = re.sub(r"<thought>.*?</thought>", "", reply, flags=re.S)   # Gemma's reasoning
        return reply.split("</think>")[-1].strip()           # reasoning models

    def image_tokens(self, image: Path) -> int | None:
        """The prompt tokens the server spends on one image (None if it does not report usage):
        a server that caps them shows every image to the model at the same low detail."""
        self._ensure_loaded()
        def tokens(content):
            body = {"model": self.model, "max_tokens": 1, "temperature": 0,
                    "messages": [{"role": "user", "content": content}]}
            return (self._post(body).get("usage") or {}).get("prompt_tokens")
        with_image = tokens([{"type": "image_url", "image_url": {
            "url": f"data:image/jpeg;base64,{image_b64(image, self.max_px)}"}}, {"type": "text", "text": "Hi"}])
        text_only = tokens([{"type": "text", "text": "Hi"}])
        return None if with_image is None or text_only is None else with_image - text_only

    def _post(self, body: dict) -> dict:
        """One request with a hard deadline of timeout_s for the whole reply. urlopen's own timeout
        only bounds each socket read, so a server that keeps the connection open without finishing
        could hold the run for hours. A request past the deadline raises TimeoutError (an OSError,
        which callers treat as a server failure); its thread is left to finish on its own."""
        if self.routing:
            body = {**body, "provider": self.routing}
        req = urllib.request.Request(f"{self.base_url}/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {self.api_key}"})
        result: dict = {}

        def send() -> None:
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    result["reply"] = json.loads(r.read())
            except BaseException as e:  # noqa: BLE001 - handed to the caller's thread below
                result["error"] = e

        worker = threading.Thread(target=send, daemon=True)
        worker.start()
        worker.join(self.timeout)
        if worker.is_alive():
            raise TimeoutError(f"{self.base_url} gave no complete reply within {self.timeout:.0f} s")
        if "error" in result:
            raise result["error"]
        return result["reply"]

    def _ensure_loaded(self) -> None:
        if self.manage_lms and not self._loaded:
            if self.memory_check:
                self._check_memory()
            ctx =["-c", str(self.context_length)] if self.context_length else []
            ctx += ["--parallel", str(self.parallel)] if self.parallel else []
            held = self._already_loaded()
            if held and self.context_length and held.get("contextLength") not in (None, int(self.context_length)):
                self._lms("unload", self.model)          # loaded earlier with another context: reload it
                held = None
            if not held:
                self._lms("load", self.model, "-y", "--identifier", self.model, *ctx)
            self._loaded = True

    def provenance(self) -> dict:
        return {"backend": self.backend, "model": self.model, "base_url": self.base_url}
