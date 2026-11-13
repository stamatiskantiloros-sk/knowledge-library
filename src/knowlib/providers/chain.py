"""A role served by several API models in order: the first with quota left answers.

A role's section names the chain, for example

    notes_review:
      backend: chain
      chain: [google/gemini-3.5-flash-lite, google/gemini-3.1-flash-lite, strata/qwen3.8-flash]

Each entry is <provider>/<model>, looked up in the config's `providers:` section, which holds the
provider's URL, its key (environment variable or file), the day's reset time zone and each model's
limits (rpm, tpm, rpd) and settings. The ledger in quota.py keeps the counts across runs and
libraries. A model whose day is spent, or whose quota a 429 reply reports as spent, is skipped until
its reset; a per-minute 429 waits and asks again; a server failure moves on to the next model. The
reply's model is in `answered_by`, so caches store it under the model that wrote it and look a
prompt up under every model in the chain.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
from pathlib import Path

from . import quota
from .base import VisionProvider

log = logging.getLogger(__name__)

LIMIT_KEYS = ("rpm", "tpm", "rpd", "context", "max_output")
MEMBER_KEYS = ("backend", "chain", "base_url", "openai_model", "api_key_env", "api_key_file", "manage_lms",
               "model", "ollama_model", "api_model", "agent_model")


def key_slots(p: dict) -> list[dict]:
    """The provider's keys that are present, as {api_key_env, api_key_file} each: its main key
    (api_key_env / api_key_file), then each entry of `keys:` ({env: ..., file: ...}). An extra slot whose
    key is neither in the environment nor in its file is left out, so a config can list slots to fill."""
    import os
    main = {"api_key_env": p.get("api_key_env"), "api_key_file": p.get("api_key_file")}
    slots = [main]
    for k in p.get("keys") or []:
        k = {"env": k} if isinstance(k, str) else dict(k)
        slot = {"api_key_env": k.get("env"), "api_key_file": k.get("file")}
        has_env = bool(slot["api_key_env"] and os.environ.get(slot["api_key_env"]))
        has_file = bool(slot["api_key_file"] and Path(slot["api_key_file"]).expanduser().exists())
        if has_env or has_file:
            slots.append(slot)
    return slots


def member_sections(section: dict, cfg: dict) -> list[dict]:
    """Each chain entry as {key, section, limits, tz, trains}: the provider's settings, then the
    model's own, on top of the role's (max_tokens, reasoning_effort, ...). A provider with several keys
    gives one member per key, each with its own quota (<provider>/<model>#2, #3, ...), tried in order
    before the next model."""
    providers = cfg.get("providers") or {}
    role = {k: v for k, v in section.items() if k not in MEMBER_KEYS}
    out = []
    for spec in section.get("chain") or []:
        name, _, model = str(spec).partition("/")
        if name not in providers or not model:
            raise ValueError(f"chain entry {spec!r}: use <provider>/<model> with a provider from `providers:`")
        p = providers[name]
        mconf = dict((p.get("models") or {}).get(model) or {})
        limits = {k: mconf.pop(k) for k in LIMIT_KEYS if k in mconf}
        fixed = bool(mconf.pop("fixed_image_tokens", False))
        base = {k: v for k, v in p.items() if k not in ("models", "reset_tz", "trains_on_data", "keys")}
        for n, slot in enumerate(key_slots(p), 1):
            # each slot uses only its own key: no falling back to the main key's variable or file
            own = slot if n == 1 else {"api_key_env": slot["api_key_env"] or "KNOWLIB_NO_KEY",
                                       "api_key_file": slot["api_key_file"]}
            sec = {**role, **base, **own, **mconf,
                   "backend": "openai", "openai_model": model, "manage_lms": False}
            out.append({"key": f"{name}/{model}" + (f"#{n}" if n > 1 else ""), "section": sec, "limits": limits,
                        "tz": p.get("reset_tz"), "trains": bool(p.get("trains_on_data")),
                        "fixed_image_tokens": fixed})
    return out


def estimate_tokens(text: str, images: int = 0) -> int:
    return len(text) // 4 + 1100 * images


class ChainProvider(VisionProvider):
    backend = "chain"

    def __init__(self, section: dict, cfg: dict):
        from .openai_compat import OpenAICompatProvider, with_data_policy
        self.members = [{**m, "provider": OpenAICompatProvider(with_data_policy(m["section"], cfg))}
                        for m in member_sections(section, cfg)]
        if not self.members:
            raise ValueError("backend: chain needs a `chain:` list of <provider>/<model> entries")
        self.model = self.members[0]["provider"].model
        self.is_local = all(m["provider"].is_local for m in self.members)
        self.answered_by = None
        self.last_finish = None
        self.minute_retries = int(section.get("minute_retries", 4))

    @property
    def cache_models(self) -> list[str]:
        return [m["provider"].model for m in self.members]

    def budget(self) -> int:
        """Input tokens one request may carry: the first model with quota left, by its tpm and context."""
        for m in self.members:
            if quota.available(m["key"], m["limits"], m["tz"]):
                caps = [int(v) for v in (m["limits"].get("tpm"), m["limits"].get("context")) if v]
                return int(min(caps) * 0.8) if caps else 0
        return 0

    def generate(self, images: list[Path], prompt: str, *, json_mode: bool = False,
                 max_tokens: int | None = None) -> str:
        tokens = estimate_tokens(prompt, len(images))
        failures = []
        for m in self.members:
            limits = m["limits"]
            if limits.get("tpm") and tokens > int(limits["tpm"]) or \
               limits.get("context") and tokens > int(limits["context"]):
                failures.append(f"{m['key']}: request too large ({tokens} tokens)")
                continue
            for attempt in range(1 + self.minute_retries):
                if not quota.reserve(m["key"], limits, m["tz"], tokens):
                    failures.append(f"{m['key']}: daily quota spent")
                    break
                try:
                    reply = m["provider"].generate(images, prompt, json_mode=json_mode, max_tokens=max_tokens)
                except urllib.error.HTTPError as e:
                    kind, wait, value = _quota_error(e)
                    if kind == "day":
                        quota.exhaust(m["key"], m["tz"], time.time() + wait if wait else None, value)
                        failures.append(f"{m['key']}: daily quota spent")
                        log.warning("%s: daily quota spent; moving on", m["key"])
                        break
                    if kind == "minute" and attempt < self.minute_retries:
                        log.info("%s: per-minute limit; waiting %.0f s", m["key"], wait)
                        time.sleep(min(max(wait, 5), 120))
                        continue
                    if e.code >= 500 and attempt < 1:
                        time.sleep(10)
                        continue
                    failures.append(f"{m['key']}: HTTP {e.code}")
                    break
                except (OSError, RuntimeError) as e:
                    if type(e).__name__ == "NotEnoughMemory":
                        raise
                    failures.append(f"{m['key']}: {e}")
                    break
                self.answered_by = m["provider"].model
                self.last_finish = m["provider"].last_finish
                return reply
        raise RuntimeError("no model in the chain could answer: " + "; ".join(failures))

    def image_tokens(self, image: Path) -> int | None:
        """The first available model's tokens for an image; None (no check) for a model that gives
        every image the same budget by design, as Gemini does: its detail is not capped by a server."""
        for m in self.members:
            if quota.available(m["key"], m["limits"], m["tz"]):
                return None if m.get("fixed_image_tokens") else m["provider"].image_tokens(image)
        return None

    def usage(self) -> list[str]:
        return quota.report([(m["key"], m["limits"], m["tz"]) for m in self.members])

    def close(self) -> None:
        for m in self.members:
            m["provider"].close()

    def provenance(self) -> dict:
        return {"backend": "chain", "model": self.answered_by or self.model,
                "chain": [m["key"] for m in self.members]}


def _quota_error(e: urllib.error.HTTPError) -> tuple[str | None, float, int | None]:
    """('day' | 'minute' | None, seconds to wait, the limit's value) from a 429 reply."""
    if e.code != 429:
        return None, 0, None
    try:
        body = json.loads(e.read() or b"{}")
    except (ValueError, OSError):
        body = {}
    err = (body[0] if isinstance(body, list) and body else body).get("error", {}) if body else {}
    details = err.get("details") or []
    wait, quota_id, value = 0.0, "", None
    for d in details:
        if "retryDelay" in d:
            wait = float(re.sub(r"[^0-9.]", "", d["retryDelay"]) or 0)
        for v in d.get("violations") or []:
            quota_id = v.get("quotaId", quota_id)
            value = int(v["quotaValue"]) if str(v.get("quotaValue", "")).isdigit() else value
    text = f"{quota_id} {err.get('message', '')}".lower()
    if "perday" in text or "per day" in text or wait > 600:
        return "day", wait, value if "request" in text else None
    return "minute", wait or 30, None
