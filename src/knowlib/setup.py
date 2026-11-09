"""knowlib init: set a library up, asking a few questions, and keep its agent files current.

Setup decides the layout (a separate library, or the user's own folder in place), which folders
to skip, whether the material is confidential, the transcription language, and which local
models to use. Every answer has a default, so `--yes` (or no terminal) runs without questions, and
each answer has a flag, so an agent can run setup non-interactively. Only the answers go into the
config; everything else keeps its built-in default (docs/system-design.md, Appendix A).

It also writes the files agents work from: AGENTS.md (the procedures, filled in for the layout),
CLAUDE.md (in place, a pointer to AGENTS.md), INDEX.md or index.md, glossary.md, notes/, and in
place an .ignore file so `rg` searches Markdown but skips images and caches. After every
`knowlib process`, refresh() rewrites the sources table in the index and _knowlib/sources.jsonl.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from datetime import date
from importlib import resources
from pathlib import Path

import yaml

from .layout import Layout

TEMPLATES = resources.files("knowlib") / "templates"

FILLS = {
    "separate": {
        "LAYOUT": """```
inbox/          raw originals. NEVER edit, move or delete anything here.
sources/<id>/   one ingested source: source.md (frontmatter + text), digest.md for a long book, and assets/ (frames, page renders)
notes/          synthesized topic notes with citations (you write these)
index.md        map of the library: topics (you maintain them) and a sources table (knowlib keeps it current)
glossary.md     terms and names, incl. per-course / per-project sections (you maintain it)
manifest.json   what was processed, how, and whether it has been synthesized (written by knowlib)
config.yaml     backends and models per stage
.work/          caches and run reports; safe to delete, but deleting it forces full reprocessing
```""",
        "SOURCE_MD": "`sources/<id>/source.md`",
        "DIGEST_MD": "`sources/<id>/digest.md`",
        "CONFIG": "config.yaml",
        "INDEX": "index.md",
        "RG": '`rg -i "<term>" sources/`',
        "RULES": "- Never modify `inbox/`.\n- Never hand-edit `sources/*/source.md`; reprocess instead, and put corrections in notes.",
    },
    "inplace": {
        "LAYOUT": """```
<your folders>/        the originals, arranged by the user. NEVER edit, move or delete them.
<file>.<ext>.md        beside each original (inventory.pdf -> inventory.pdf.md): its ingested text with frontmatter, written by knowlib
<folder>.lecture.md    in a lecture folder: the lecture, slide by slide
<file>.pdf.digest.md   beside a long book: its finding aid
notes/                 synthesized topic notes with citations (you write these)
INDEX.md               map of the library: topics (you maintain them) and a sources table (knowlib keeps it current)
glossary.md            terms and names, incl. per-course / per-project sections (you maintain it)
_knowlib/              knowlib's own files: config.yaml, manifest.json, sources.jsonl (one line per source),
                       assets/<id>/ (frames, page renders), work/ (caches, safe to delete)
```

Every source has a stable id; notes cite it, never a path. To go from an id to its Markdown, look it up in the sources table at the end of `INDEX.md` or in `_knowlib/sources.jsonl`.""",
        "SOURCE_MD": "the `<file>.md` beside the original, listed in `INDEX.md`",
        "DIGEST_MD": "the `<file>.pdf.digest.md` beside the book",
        "CONFIG": "_knowlib/config.yaml",
        "INDEX": "INDEX.md",
        "RG": '`rg -i "<term>" -g "*.md" -g "!notes/**"`',
        "RULES": "- Never modify, move or delete the user's original files.\n- Never hand-edit a generated `<file>.md` (its frontmatter says `generated_by: knowlib`); reprocess instead, and put corrections in notes.",
    },
}

SOURCES_START, SOURCES_END = "<!-- knowlib:sources -->", "<!-- /knowlib:sources -->"
IGNORE = "# rg and other tools: search the Markdown, not knowlib's images and caches\n_knowlib/assets/\n_knowlib/work/\n_knowlib/staging/\n"


def procedures(mode: str) -> str:
    text = (TEMPLATES / "CLAUDE.md").read_text(encoding="utf-8")
    for key, value in FILLS[mode].items():
        text = text.replace("{{" + key + "}}", value)
    return text


# ---- questions -------------------------------------------------------------------------------

def ask(question: str, default: str, interactive: bool) -> str:
    if not interactive:
        return default
    reply = input(f"{question} [{default}]: ").strip()
    return reply or default


def yes(question: str, default: bool, interactive: bool) -> bool:
    return ask(question + " (y/n)", "y" if default else "n", interactive).lower().startswith("y")


def survey(folder: Path) -> list[tuple[str, int, int]]:
    """(top-level folder, files knowlib can read, files it cannot) for the folder's contents."""
    from . import ingestors  # noqa: F401  - registers all ingestors
    from .ingestors.base import find_ingestor
    from .router import discover
    readable: Counter = Counter()
    other: Counter = Counter()
    for item in discover(folder, skip=lambda p: p.name in ("_knowlib", "notes")):
        top = item.key.split("/", 1)[0] if "/" in item.key.rstrip("/") else "(top level)"
        (readable if find_ingestor(item) else other)[top] += 1
    return [(t, readable[t], other[t]) for t in sorted(set(readable) | set(other))]


def lmstudio_models() -> list[dict]:
    """The LLMs LM Studio has downloaded, largest first; [] without LM Studio."""
    lms = shutil.which("lms") or str(Path.home() / ".lmstudio/bin/lms")
    try:
        out = subprocess.run([lms, "ls", "--json"], capture_output=True, text=True, timeout=20).stdout
        models = [m for m in json.loads(out or "[]") if m.get("type") == "llm"]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    return sorted(models, key=lambda m: -m.get("sizeBytes", 0))


def model_config(fast: str | None, strong: str | None, reviewer: str | None) -> dict:
    """Config for models served by LM Studio, which knowlib loads and unloads itself."""
    if not (fast or strong or reviewer):
        return {}
    fast = fast or strong
    strong = strong or fast
    cfg: dict = {"frame_selection": {"backend": "openai", "base_url": "http://localhost:1234/v1", "openai_model": fast,
                                     "manage_lms": True, "split": True,
                                     "transcribe_model": {"openai_model": strong, "context_length": 16384, "parallel": 1},
                                     "caption_model": {"openai_model": strong, "context_length": 16384, "parallel": 1},
                                     "caption_model_types": "all"},
                 "digest": {"context_length": 65536, "parallel": 1,
                            "fallback_model": {"openai_model": strong, "context_length": 32768, "reasoning_effort": "none"}},
                 "handbook": {"openai_model": strong, "context_length": 16384, "parallel": 1}}
    if reviewer:
        cfg["notes_review"] = {"openai_model": reviewer, "context_length": 32768}
    return cfg


# ---- init -------------------------------------------------------------------------------------

def init(root: Path, *, mode: str | None = None, skip: list[str] | None = None, local_only: bool | None = None,
         language: str | None = None, fast: str | None = None, strong: str | None = None, reviewer: str | None = None,
         interactive: bool | None = None) -> Path:
    """Ask (or take from the arguments) the setup answers, write the config and the agent files.
    Returns the config path."""
    root = Path(root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    interactive = sys.stdin.isatty() if interactive is None else interactive
    has_material = any(p for p in root.iterdir() if not p.name.startswith(".") and p.name not in ("_knowlib", "notes"))
    if mode is None:
        print(f"Setting up a knowledge library in {root}") if interactive else None
        mode = "inplace" if yes("Use this folder as it is, with each file's Markdown beside it?" if has_material
                                else "This folder is empty. Use it in place anyway (no inbox/ and sources/)?",
                                has_material, interactive) else "separate"
    if skip is None:
        skip = []
        if mode == "inplace" and interactive:
            print("\nWhat knowlib can read here (files it can read / files it will skip):")
            for top, n_ok, n_other in survey(root):
                print(f"  {top:40s} {n_ok:5d} / {n_other}")
            reply = ask("Folders or name patterns to leave out, comma separated", "none", True)
            skip = [] if reply.lower() == "none" else [s.strip() for s in reply.split(",") if s.strip()]
    if local_only is None:
        local_only = yes("Is this material confidential, never to leave this machine?", False, interactive)
    if language is None:
        language = ask("Language of lectures and documents: en, el or mixed", "en", interactive)

    lay = Layout(root, {"library": {"mode": mode, "data": "_knowlib", "notes": "notes", "inbox": "inbox",
                                    "sources": "sources", "work": ".work"}})
    answers: dict = {"library": {"mode": mode}, "local_only": bool(local_only), "transcription": {"language": language}}
    if skip:
        answers["library"]["ignore"] = skip
    answers.update(model_config(fast, strong, reviewer))
    cfg_path = (lay.data if mode == "inplace" else root) / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    header = (f"# knowlib settings, written by `knowlib init` on {date.today()}. Only these answers are set here;\n"
              "# everything else uses the built-in default (docs/system-design.md, Appendix A, and\n"
              "# config.example.yaml beside this file). Edit by hand or run `knowlib init` again.\n\n")
    if cfg_path.exists():
        shutil.copy2(cfg_path, cfg_path.with_suffix(".yaml.bak"))
    cfg_path.write_text(header + yaml.safe_dump(answers, sort_keys=False, allow_unicode=True), encoding="utf-8")
    shutil.copy2(TEMPLATES / "config.yaml", cfg_path.with_name("config.example.yaml"))
    if interactive and not (fast or strong or reviewer):
        from . import models
        from .config import load_config
        print("\nWhich model does what. Enter keeps the built-in default; `knowlib models` changes it later. "
              "Ideally the model that reviews notes is a stronger one than the model that writes them.")
        changes = models.choose(load_config(root))
        if changes:
            models.save(root, changes)

    lay.notes.mkdir(exist_ok=True)
    if mode == "separate":
        lay.inbox.mkdir(exist_ok=True)
        lay.sources.mkdir(exist_ok=True)
        _write_new(root / "CLAUDE.md", procedures("separate"))
        _write_new(root / ".gitignore", (TEMPLATES / "gitignore").read_text(encoding="utf-8"))
    else:
        _write_new(root / "AGENTS.md", procedures("inplace"))
        _write_new(root / "CLAUDE.md", "# Agent instructions\n\nThe procedures for this library are in AGENTS.md.\n\n@AGENTS.md\n")
        _write_new(root / ".ignore", IGNORE)
    _write_new(lay.index, (TEMPLATES / "index.md").read_text(encoding="utf-8"))
    _write_new(root / "glossary.md", (TEMPLATES / "glossary.md").read_text(encoding="utf-8"))
    return cfg_path


def _write_new(path: Path, text: str) -> None:
    """Agent files are the user's once written: never overwrite them."""
    if not path.exists():
        path.write_text(text, encoding="utf-8")
        print(f"created {path.name}")


# ---- after each run ---------------------------------------------------------------------------

def refresh(lay: Layout, entries: dict) -> None:
    """Rewrite the sources table in the index and the machine-readable sources list."""
    rows = []
    for key, e in sorted(entries.items()):
        sid = e.get("source_id")
        if e.get("status") != "done" or not sid:
            continue
        md = lay.source_md(sid, e)
        if not md.exists():
            continue
        fm = {}
        text = md.read_text(encoding="utf-8")
        if text.startswith("---\n"):
            fm = yaml.safe_load(text[4:text.find("\n---\n", 4)]) or {}
        stats = fm.get("stats") or {}
        size = (f"{stats['pages']} pages" if stats.get("pages") else
                f"{stats['kept']} slides" if stats.get("kept") else
                f"{round(stats['duration_s'] / 60)} min" if stats.get("duration_s") else "")
        rows.append({"id": sid, "title": fm.get("title", sid), "type": fm.get("source_type", ""), "original": key,
                     "markdown": md.relative_to(lay.root).as_posix(), "size": size, "flags": len(e.get("flags") or []),
                     "synthesized": bool(e.get("synthesized")), "local_only": bool(e.get("local_only")),
                     "digest": lay.digest_md(sid, e).relative_to(lay.root).as_posix() if lay.digest_md(sid, e).exists() else None})
    out = lay.data if lay.inplace else lay.work
    out.mkdir(parents=True, exist_ok=True)
    (out / "sources.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    if not lay.index.exists():
        return
    table = ["## Sources", "", "Written by knowlib after each run; do not edit between the markers.", "",
             "| id | title | type | size | Markdown | flags | synthesized |", "| - | - | - | - | - | - | - |"]
    for r in rows:
        link = r["markdown"].replace(" ", "%20")
        table.append(f"| `{r['id']}` | {r['title']} | {r['type']} | {r['size']} | [{Path(r['markdown']).name}]({link}) "
                     f"| {r['flags']} | {'yes' if r['synthesized'] else 'no'} |")
    block = f"{SOURCES_START}\n" + "\n".join(table) + f"\n{SOURCES_END}"
    index = lay.index.read_text(encoding="utf-8")
    if SOURCES_START in index:
        index = re.sub(re.escape(SOURCES_START) + r".*?" + re.escape(SOURCES_END), lambda _: block, index, flags=re.S)
    else:
        index = index.rstrip() + "\n\n" + block + "\n"
    lay.index.write_text(index, encoding="utf-8")
