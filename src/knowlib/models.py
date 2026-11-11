"""Which model does what: one choice per role, checked before long runs.

A library has five model roles. knowlib runs three of them (reading, digest, review) and records
the other two for the agent that follows the procedures (writing notes, handbooks):

  reading   slides, video frames, figures in PDFs and loose images (needs vision)
  digest    book finding aids, a chapter at a time
  writer    topic notes (an agent writes them; this records which model)
  review    checks every claim of a note against the passages it cites
  handbook  lecture handbooks

Each role is a provider: a remote OpenAI-compatible server (llama.cpp, vLLM, a GPU box on the
network), a model in LM Studio on this machine, a Claude model in the session (an agent), or the
built-in default. `knowlib models` shows the choices and changes them; before a run, knowlib checks
that the servers answer and the models are there, and asks again only when a choice changed or a
check failed. The full check (`knowlib models --check`) also looks for a vision server that caps
image tokens and for a context window too small for a book chapter.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

import yaml

ROLES = {
    "reading": "slides, frames, figures and images",
    "digest": "book digests",
    "writer": "topic notes",
    "review": "note review",
    "handbook": "lecture handbooks",
}
SESSION_MODELS = ("haiku", "sonnet", "opus")
LMSTUDIO_URL = "http://localhost:1234/v1"
PROVIDER_KEYS = ("backend", "base_url", "openai_model", "api_key_env", "manage_lms", "agent_model", "model",
                 "ollama_model", "api_model", "context_length", "parallel", "chain", "api_key_file")
COMMANDS = {"process": ["reading"], "reprocess": ["reading"], "digest": ["digest"], "review-notes": ["review"],
            "handbook": ["reading", "handbook"]}


# ---- what each role uses ----------------------------------------------------------------------

def section(cfg: dict, role: str) -> dict:
    """The provider section a role resolves to, the same way the code that runs it builds it."""
    fs = cfg["frame_selection"]
    if role == "reading":
        return {**fs, **(fs.get("caption_model") or {})}
    if role == "digest":
        return {**fs, **cfg["digest"]}
    if role == "review":
        return {**fs, **(fs.get("caption_model") or {}), **cfg["notes_review"]}
    if role == "handbook":
        return {**fs, **cfg["handbook"]}
    if role == "writer":
        return cfg.get("notes_writer") or {"backend": "agent", "agent_model": "the session"}
    raise KeyError(role)


def describe(sec: dict) -> str:
    """'server gpu-box.example: qwen3-vl', 'LM Studio gemma-3-27b', 'session: sonnet', ..."""
    b = sec.get("backend", "mlx-vlm")
    if b == "agent":
        return f"session: {sec.get('agent_model', 'sonnet')}"
    if b == "chain":
        return "chain: " + " → ".join(map(str, sec.get("chain") or []))
    if b == "openai":
        if sec.get("manage_lms"):
            return f"LM Studio: {sec.get('openai_model')}"
        from urllib.parse import urlparse
        host = urlparse(sec.get("base_url", "")).netloc or sec.get("base_url", "")
        return f"server {host}: {sec.get('openai_model')}"
    if b == "ollama":
        return f"Ollama: {sec.get('ollama_model')}"
    if b == "api":
        return f"API: {sec.get('api_model')}"
    if b == "mlx-vlm":
        return f"MLX on this Mac: {sec.get('model')}"
    return b


def summary(cfg: dict, roles=ROLES) -> dict[str, str]:
    return {r: describe(section(cfg, r)) for r in roles}


# ---- choosing: specs and the config they write ------------------------------------------------

def parse_spec(spec: str) -> dict | None:
    """'session:sonnet' | 'lmstudio:<model>' | 'server:<base_url>|<model>[|<key env var>]' |
    'chain:<provider>/<model>,<provider>/<model>,...' | 'default'."""
    kind, _, rest = spec.partition(":")
    if kind == "default":
        return None
    if kind == "chain":
        entries = [e.strip() for e in rest.split(",") if e.strip()]
        if not entries or any("/" not in e for e in entries):
            raise ValueError("chain:<provider>/<model>,<provider>/<model>,... (providers from `providers:` in the config)")
        return {"backend": "chain", "chain": entries}
    if kind == "session":
        if rest not in SESSION_MODELS:
            raise ValueError(f"session model must be one of {', '.join(SESSION_MODELS)}")
        return {"backend": "agent", "agent_model": rest}
    if kind == "lmstudio":
        if not rest:
            raise ValueError("lmstudio:<model key from `lms ls`>")
        return {"backend": "openai", "base_url": LMSTUDIO_URL, "openai_model": rest, "manage_lms": True}
    if kind == "server":
        parts = rest.split("|")
        if len(parts) < 2 or not parts[0].startswith("http"):
            raise ValueError("server:<base_url>|<model>[|<environment variable holding the key>]")
        sec = {"backend": "openai", "base_url": parts[0].rstrip("/"), "openai_model": parts[1], "manage_lms": False}
        if len(parts) > 2 and parts[2]:
            sec["api_key_env"] = parts[2]
        return sec
    raise ValueError(f"unknown provider {spec!r}: use session:, lmstudio:, server:, chain: or default")


def _clear(d: dict) -> dict:
    return {k: v for k, v in (d or {}).items() if k not in PROVIDER_KEYS}


def apply(answers: dict, role: str, sec: dict | None) -> dict:
    """Write a role's provider into the library's config answers (only the keys that role reads)."""
    a = answers
    if role == "reading":
        fs = _clear(a.get("frame_selection", {}))
        for k in ("caption_model", "transcribe_model"):          # one model reads everything
            fs.pop(k, None)
        if sec:
            fs.update(sec)
            if sec.get("backend") == "openai":
                fs.setdefault("split", True)
                fs["caption_model_types"] = "all"
                fs.setdefault("timeout_s", 900)
        a["frame_selection"] = fs
        a.setdefault("pdf", {}).setdefault("figures", {})["model"] = None
        a.setdefault("images", {})["model"] = None
    elif role == "writer":
        if sec:
            a["notes_writer"] = sec
        else:
            a.pop("notes_writer", None)
    else:
        key = {"digest": "digest", "review": "notes_review", "handbook": "handbook"}[role]
        cur = _clear(a.get(key, {}))
        if role == "digest" and "fallback_model" in cur:
            cur["fallback_model"] = _clear(cur["fallback_model"])
        if sec:
            cur.update(sec)
        a[key] = cur
    return a


def config_path(root: Path) -> Path:
    from .config import config_path as find
    return find(Path(root).expanduser().resolve())


def save(root: Path, choices: dict[str, dict | None]) -> Path:
    """Apply the choices to the library's config.yaml, keeping a backup of the old one."""
    path = config_path(root)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    header = "".join(ln + "\n" for ln in text.splitlines() if ln.startswith("#"))
    answers = yaml.safe_load(text) or {}
    for role, sec in choices.items():
        apply(answers, role, sec)
    if path.exists():
        shutil.copy2(path, path.with_suffix(".yaml.bak"))
    path.write_text(header + ("\n" if header else "") + yaml.safe_dump(answers, sort_keys=False, allow_unicode=True),
                    encoding="utf-8")
    return path


# ---- checking ---------------------------------------------------------------------------------

def _get(url: str, key: str, timeout: float = 20) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 - the configured server
        return json.loads(r.read())


def _test_image(path: Path) -> Path:
    """A 1600x1200 page of small numbers: enough detail that a server capping image tokens shows it."""
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (1600, 1200), "white")
    d = ImageDraw.Draw(im)
    for row in range(40):
        d.text((20, 10 + row * 29), "  ".join(f"{(row * 37 + c * 11) % 997:4d}" for c in range(24)), fill="black")
    im.save(path)
    return path


def _bare(model_id) -> str:
    """A model id without the "models/" prefix Google's endpoint lists its models under."""
    return str(model_id or "").removeprefix("models/")


def check(cfg: dict, role: str, *, full: bool = False) -> tuple[bool, str]:
    """(ok, message) for one role: the server answers and has the model; with full, also image detail
    for reading and the context window for the digest."""
    sec = section(cfg, role)
    b = sec.get("backend", "mlx-vlm")
    if b == "agent":
        if cfg.get("local_only"):
            return False, "local_only is set, so nothing may go to a session model"
        return True, "session model (no check needed)"
    if b == "chain":
        from .providers.chain import member_sections
        try:
            members = member_sections(sec, cfg)
        except ValueError as e:
            return False, str(e)
        if cfg.get("local_only") and any(urlparse_host(m["section"]) not in LOCAL_HOSTS for m in members):
            return False, "local_only is set, and this chain reaches models off this machine"
        results = [(m["key"], *_check_server(m["section"], role, cfg, full=False)) for m in members]
        good = [k for k, ok, _ in results if ok]
        msg = "; ".join(f"{k}: {'ok' if ok else m}" for k, ok, m in results)
        return bool(good), msg
    if b != "openai":
        return True, f"{b}: not checked"
    return _check_server(sec, role, cfg, full=full)


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def urlparse_host(sec: dict) -> str:
    from urllib.parse import urlparse
    return urlparse(sec.get("base_url", "")).hostname or ""


def _check_server(sec: dict, role: str, cfg: dict, *, full: bool) -> tuple[bool, str]:
    if sec.get("manage_lms"):
        from .setup import lmstudio_models
        keys = [m.get("modelKey") for m in lmstudio_models()]
        if not keys:
            return False, "LM Studio is not installed or lists no models (`lms ls`)"
        if sec.get("openai_model") not in keys:
            return False, f"{sec.get('openai_model')} is not downloaded in LM Studio"
        return True, "in LM Studio (loaded when needed)"
    from .providers.openai_compat import _key_file
    env = sec.get("api_key_env") or "OPENAI_API_KEY"
    key = os.environ.get(env) or _key_file(sec) or "none"
    base = sec.get("base_url", "").rstrip("/")
    try:
        listed = _get(f"{base}/models", key).get("data", [])
    except Exception as e:  # noqa: BLE001 - any failure to reach the server is the answer
        hint = f" (is {env} set?)" if key == "none" and "401" in str(e) else ""
        return False, f"{base} does not answer: {e}{hint}"
    model = next((m for m in listed if _bare(m.get("id")) == _bare(sec.get("openai_model"))), None)
    if model is None:
        return False, f"{base} does not serve {sec.get('openai_model')} (it has: " \
                      f"{', '.join(m.get('id', '?') for m in listed) or 'nothing'})"
    notes = []
    mods = (model.get("architecture") or {}).get("input_modalities")
    if role == "reading" and mods and "image" not in mods:
        return False, f"{sec.get('openai_model')} does not take images"
    n_ctx = (model.get("meta") or {}).get("n_ctx")
    if full and role == "digest" and n_ctx:
        need = int(int(cfg["digest"].get("max_chars", 120000)) / 3.5 + int(cfg["digest"].get("max_tokens", 8000)))
        if n_ctx < need:
            return False, f"context {n_ctx} tokens is too small for a digest part (about {need}); " \
                          "lower digest.max_chars or raise the server's context"
    if full and role == "reading":
        from .providers import get_vision_provider
        with tempfile.TemporaryDirectory() as tmp:
            full_img = _test_image(Path(tmp) / "full.png")
            from PIL import Image
            half = Path(tmp) / "half.png"
            Image.open(full_img).resize((800, 600)).save(half)
            prov = get_vision_provider(sec, cfg)
            try:
                a, b2 = prov.image_tokens(full_img), prov.image_tokens(half)
            finally:
                prov.close() if hasattr(prov, "close") else None
        if a and b2 and a < 2 * b2:
            return False, f"the server caps image tokens ({a} for the full image, {b2} for a quarter of it); " \
                          "raise its image-token limit (llama.cpp: --image-max-tokens about 2048)"
        if a:
            notes.append(f"{a} image tokens for a 1600x1200 page")
    if n_ctx:
        notes.append(f"context {n_ctx}")
    return True, "answers" + (f" ({', '.join(notes)})" if notes else "")


# ---- before a run -----------------------------------------------------------------------------

def _fingerprint(cfg: dict, roles: list[str]) -> str:
    return hashlib.sha256(json.dumps(summary(cfg, roles), sort_keys=True).encode()).hexdigest()[:16]


def preflight(cfg: dict, work_dir: Path, command: str, *, interactive: bool | None = None) -> bool:
    """Before a run: one line with the models it uses, a quick check of each, and a question only when
    a choice changed since the last confirmed run (interactive) or a check failed. False stops the run."""
    roles = COMMANDS.get(command, [])
    if not roles:
        return True
    interactive = sys.stdin.isatty() if interactive is None else interactive
    results = {r: check(cfg, r) for r in roles}
    line = " · ".join(f"{r}: {describe(section(cfg, r))}" for r in roles)
    failed = {r: m for r, (ok, m) in results.items() if not ok}
    if failed:
        print(f"models  {line}")
        for r, m in failed.items():
            print(f"  {r}: {m}")
        print("Fix it, choose another model with `knowlib models`, or run with --no-model-check.")
        return False
    stamp = Path(work_dir) / "models_confirmed.json"
    seen = json.loads(stamp.read_text()) if stamp.exists() else {}
    fp = _fingerprint(cfg, roles)
    print(f"models  {line}  · checks OK")
    if seen.get(command) != fp and interactive:
        reply = input("These models are new for this command. Run with them? [Y/n] ").strip().lower()
        if reply.startswith("n"):
            print("Change them with `knowlib models`.")
            return False
    seen[command] = fp
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(json.dumps(seen, indent=2))
    return True


# ---- interactive setup ------------------------------------------------------------------------

def _ask(q: str, default: str = "") -> str:
    reply = input(f"{q}" + (f" [{default}]" if default else "") + ": ").strip()
    return reply or default


def choose(cfg: dict) -> dict[str, dict | None]:
    """Ask role by role; Enter keeps the current choice. Returns only the roles that changed."""
    from .setup import lmstudio_models
    changes: dict[str, dict | None] = {}
    last_server = next((section(cfg, r) for r in ROLES if section(cfg, r).get("backend") == "openai"
                        and not section(cfg, r).get("manage_lms")), {})
    for role, what in ROLES.items():
        print(f"\n{role} ({what}): now {describe(section(cfg, role))}")
        print("  Enter keeps it; or 1 a model server, 2 LM Studio on this machine, 3 Claude in the session, 4 default")
        pick = _ask("  choice")
        if pick == "1":
            url = _ask("  server URL (OpenAI-compatible, ending in /v1)", last_server.get("base_url", ""))
            env = _ask("  environment variable with its key", last_server.get("api_key_env", "OPENAI_API_KEY"))
            try:
                ids = [m["id"] for m in _get(f"{url.rstrip('/')}/models", os.environ.get(env, "none")).get("data", [])]
            except Exception as e:  # noqa: BLE001
                print(f"  could not list its models: {e}")
                ids = []
            for i, m in enumerate(ids, 1):
                print(f"   {i}. {m}")
            m = _ask("  model (number or name)", ids[0] if len(ids) == 1 else last_server.get("openai_model", ""))
            m = ids[int(m) - 1] if m.isdigit() and 1 <= int(m) <= len(ids) else m
            changes[role] = parse_spec(f"server:{url}|{m}|{env}")
            last_server = changes[role]
        elif pick == "2":
            models = lmstudio_models()
            for i, m in enumerate(models, 1):
                print(f"   {i}. {m['modelKey']}  ({m.get('sizeBytes', 0) / 1e9:.1f} GB)")
            m = _ask("  model (number or key)")
            m = models[int(m) - 1]["modelKey"] if m.isdigit() and 1 <= int(m) <= len(models) else m
            changes[role] = parse_spec(f"lmstudio:{m}") if m else None
        elif pick == "3":
            if role == "reading":
                print("  Session reading runs with `knowlib process --reader session`; pick a model here only "
                      "for the other roles.")
                continue
            m = _ask(f"  {' / '.join(SESSION_MODELS)}", "opus" if role == "review" else "sonnet")
            changes[role] = parse_spec(f"session:{m}")
        elif pick == "4":
            changes[role] = None
    return changes


def usage(cfg: dict, role: str) -> list[str]:
    """For a chain: each model's requests today against its daily limit."""
    sec = section(cfg, role)
    if sec.get("backend") != "chain":
        return []
    from .providers import quota
    from .providers.chain import member_sections
    return quota.report([(m["key"], m["limits"], m["tz"]) for m in member_sections(sec, cfg)])
