"""knowlib command line.

  knowlib init [DIR]                 set a library up (in place or separate): asks, or takes flags
  knowlib doctor [-L DIR]            which tools/models are installed for the configured backends
  knowlib process [-L DIR]           process inbox: ingest new or changed material
  knowlib status [-L DIR]            counts, failures, sources waiting for synthesis
  knowlib handbook --output DIR      one book per lecture, from --input or the inbox
  knowlib digest [-L DIR]            a finding aid per long PDF (book), for handbooks that cite it
  knowlib qa [-L DIR]                every flag across sources, for the QA pass
  knowlib check-notes [-L DIR]       citations, duplicates and consolidation of the topic notes
  knowlib review-notes [-L DIR]      a second model judges every claim against its cited passages
  knowlib reprocess --where K=V      mark matching sources stale and re-ingest them
  knowlib pending [-L DIR]           source ids not yet synthesized into notes
  knowlib mark-synthesized ID...     record that sources were synthesized
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import platform
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

from .config import load_config
from .layout import open_layout


def _lib(args) -> Path:
    return Path(args.library).expanduser().resolve()


def cmd_init(args) -> int:
    from .setup import init
    root = Path(args.dir).expanduser().resolve()
    interactive = False if args.yes else None
    cfg = init(root, mode=args.mode, skip=args.skip, local_only=True if args.local_only else None,
               language=args.language, fast=args.fast_model, strong=args.strong_model, reviewer=args.reviewer_model,
               interactive=interactive)
    print(f"settings in {cfg}")
    where = "the folders here" if "_knowlib" in cfg.parts else f"{root / 'inbox'}"
    print(f"next: `knowlib process -L {root}` ingests {where}")
    return 0


def _ensure_setup(root: Path) -> bool:
    """A folder knowlib has never set up: offer setup at a terminal, otherwise say how."""
    from .config import config_path
    if config_path(root).exists() or (root / "manifest.json").exists():
        return True
    if sys.stdin.isatty():
        from .setup import init
        if input(f"{root} is not set up as a library yet. Set it up now? (y/n) [y]: ").strip().lower() in ("", "y", "yes"):
            init(root)
            return True
    print(f"{root} is not a library yet: run `knowlib init {root}` (add --yes to accept the defaults)", file=sys.stderr)
    return False


def _models_ok(args, command: str, overrides: dict | None = None) -> bool:
    """The pre-run model check (knowlib/models.py), unless --no-model-check."""
    if getattr(args, "no_model_check", False):
        return True
    from . import models
    from .config import load_config
    root = _lib(args)
    lay, _ = open_layout(root, overrides)
    return models.preflight(load_config(root, overrides), lay.work, command)


def cmd_models(args) -> int:
    from . import models
    from .config import load_config
    root = _lib(args)
    if not _ensure_setup(root):
        return 2
    changes = {}
    for pair in args.use:
        role, _, spec = pair.partition("=")
        if role not in models.ROLES:
            print(f"unknown role {role!r}: {', '.join(models.ROLES)}", file=sys.stderr)
            return 2
        changes[role] = models.parse_spec(spec)
    if not changes and sys.stdin.isatty() and not args.check:
        changes = models.choose(load_config(root))
    if changes:
        print(f"saved to {models.save(root, changes)}")
    cfg = load_config(root)
    bad = 0
    for role, what in models.ROLES.items():
        line = f"{role:9s} {models.describe(models.section(cfg, role))}"
        if args.check or changes:
            ok, msg = models.check(cfg, role, full=True)
            bad += not ok
            line += f"\n          {'ok' if ok else 'PROBLEM'}: {msg}"
        for u in models.usage(cfg, role):
            line += f"\n          {u}"
        print(line)
    return 1 if bad else 0


def _has(mod: str) -> bool:
    return importlib.util.find_spec(mod) is not None


def cmd_doctor(args) -> int:
    root = _lib(args)
    cfg = load_config(root)
    ok = lambda b: "ok " if b else "-- "  # noqa: E731
    mac = sys.platform == "darwin"
    arm = platform.machine() == "arm64"
    print(f"platform: {platform.system()} {platform.machine()}  python {platform.python_version()}")
    if not (mac and arm):
        print("  note: the default backends (mlx-whisper, mlx-vlm, Apple Vision) need an Apple Silicon Mac")
    print("\ntools")
    for t in ("ffmpeg", "ffprobe", "pandoc", "tesseract", "soffice", "whisper-cli"):
        p = shutil.which(t) or (t == "soffice" and Path("/Applications/LibreOffice.app/Contents/MacOS/soffice").exists())
        print(f"  {ok(p)}{t}")
    if shutil.which("tesseract"):
        langs = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True).stdout.split()
        print(f"  {ok('ell' in langs)}tesseract Greek model (ell)" + ("" if "ell" in langs else "  -> brew install tesseract-lang"))
    print("\npython packages")
    for m, why in [("mlx_whisper", "transcription: mlx-whisper"), ("mlx_vlm", "frame selection: mlx-vlm"),
                   ("ocrmac", "OCR cross-check: apple-vision"), ("docling", "PDF/Office: docling"),
                   ("fitz", "PDF renders: pymupdf"), ("trafilatura", "web pages"), ("pptx", "slides: python-pptx"),
                   ("litellm", "API models (optional)"), ("faster_whisper", "transcription: faster-whisper (optional)")]:
        print(f"  {ok(_has(m))}{m:16s} {why}")
    if mac and _has("Vision"):
        try:
            import Vision
            req = Vision.VNRecognizeTextRequest.alloc().init()
            req.setRecognitionLevel_(0)
            langs, _ = req.supportedRecognitionLanguagesAndReturnError_(None)
            el = any(str(l).startswith("el") for l in langs)
            print(f"  {ok(el)}Apple Vision Greek support" + ("" if el else
                  "  -> Greek cross-check falls back to Tesseract: add tesseract to ocr_crosscheck.backends"))
        except Exception as e:  # noqa: BLE001
            print(f"  -- Apple Vision language query failed: {e}")
    fs = cfg["frame_selection"]
    if fs["backend"] == "ollama":
        try:
            urllib.request.urlopen(fs["ollama_url"] + "/api/tags", timeout=3)
            print("  ok ollama server reachable")
        except Exception:
            print("  -- ollama server not reachable (ollama serve)")
    print("\nconfigured backends")
    for sec, key in [("transcription", "backend"), ("frame_selection", "backend"), ("pdf", "backend"),
                     ("office", "backend"), ("web", "backend")]:
        print(f"  {sec:16s} {cfg[sec][key]}  {cfg[sec].get('model', '') or ''}")
    print(f"  ocr_crosscheck   {', '.join(cfg['ocr_crosscheck']['backends'])}")
    print(f"  local_only       {cfg['local_only']}")
    return 0


def parse_sets(pairs: list[str]) -> dict:
    """--set transcription.prefer_subtitles=false  ->  nested override dict (values parsed as YAML)."""
    import yaml
    out: dict = {}
    for pair in pairs or []:
        path, _, raw = pair.partition("=")
        node = out
        parts = path.strip().split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = yaml.safe_load(raw)
    return out


SESSION_READER = {"transcribe_model": {"backend": "agent", "agent_model": "haiku"},
                  "transcribe_hard": {"agent_model": "sonnet"},
                  "caption_model": {"backend": "agent", "agent_model": "sonnet"}}


def cmd_process(args) -> int:
    from .config import deep_merge
    from .runner import process
    overrides = deep_merge(parse_sets(args.set), {"local_only": True} if args.local_only else {})
    if args.input:
        overrides = deep_merge(overrides, {"library": {"inbox": str(Path(args.input).expanduser().resolve())}})
    if args.layout:
        overrides = deep_merge(overrides, {"library": {"layout": args.layout}})
    if args.reader == "session":
        overrides = deep_merge(overrides, {"frame_selection": SESSION_READER})
    if not args.input and not _ensure_setup(_lib(args)):
        return 2
    if args.reader != "session" and not args.dry_run and not _models_ok(args, "process", overrides):
        return 2
    rep = process(_lib(args), force=args.force, only=args.only, dry_run=args.dry_run, overrides=overrides,
                  limit=args.limit)
    print(json.dumps({k: rep[k] for k in ("new", "updated", "unchanged", "failed", "unsupported", "renamed")},
                     indent=2, ensure_ascii=False))
    if rep["waiting"]:
        print(f"\n{len(rep['waiting'])} sources wait for readings: answer .work/agent/jobs.json, then run this again")
    if rep["flags"]:
        print(f"\n{sum(len(v) for v in rep['flags'].values())} flags on {len(rep['flags'])} sources - run `knowlib qa`")
    if rep["pending_synthesis"]:
        print(f"{len(rep['pending_synthesis'])} sources waiting for synthesis")
    return 1 if rep["failed"] else 0


def cmd_transcribe(args) -> int:
    """Transcribe one file with the configured backend: a quick check of Whisper speed and quality."""
    import tempfile
    import time
    from .config import deep_merge
    from .media.transcribe import transcribe
    cfg = deep_merge(load_config(_lib(args)), parse_sets(args.set))
    cfg["transcription"]["prefer_subtitles"] = False
    src = Path(args.file).expanduser()
    with tempfile.TemporaryDirectory() as td:
        t0 = time.time()
        segs, prov, flags = transcribe(src, Path(td), cfg, language=args.language or cfg["transcription"]["language"],
                                       glossary=args.glossary.split(",") if args.glossary else [])
        secs = time.time() - t0
    dur = segs[-1]["end"] if segs else 0
    out = Path(args.output) if args.output else src.with_suffix(".knowlib-transcript.md")
    lines = [f"# Transcript test: {src.name}", "", f"- backend: {prov}", f"- audio: {dur:.0f}s, took {secs:.0f}s "
             f"({dur / max(secs, 1e-6):.1f}x real time)", *[f"- flag: {f}" for f in flags], ""]
    lines += [f"[{int(s['start']) // 60:02d}:{int(s['start']) % 60:02d}] {s['text']}" for s in segs]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{len(segs)} segments, {dur:.0f}s of audio in {secs:.0f}s -> {out}")
    return 0


def cmd_status(args) -> int:
    m = open_layout(_lib(args))[1]
    by = {}
    for e in m.entries.values():
        by[e.get("status")] = by.get(e.get("status"), 0) + 1
    print(json.dumps({"entries": len(m.entries), "by_status": by,
                      "pending_synthesis": len(m.pending_synthesis()),
                      "failed": [k for k, e in m.entries.items() if e.get("status") == "failed"]}, indent=2))
    from . import progress
    run = progress.read(_lib(args) / ".work")
    if run:
        print(("running: " if run.get("running") else "last run finished: ") + progress.summary(run))
        if run.get("running") and run.get("item"):
            print(f"current: {run['item']}")
    return 0


def cmd_qa(args) -> int:
    m = open_layout(_lib(args))[1]
    out = {e["source_id"]: e["flags"] for _, e in m.done() if e.get("flags")}
    out.update({k: [e.get("error", "")[-500:]] for k, e in m.entries.items() if e.get("status") == "failed"})
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def cmd_check_notes(args) -> int:
    from . import notecheck
    root = _lib(args)
    findings = notecheck.run(root, only=args.only or None, note_slugs=args.notes or None)
    print(notecheck.report(findings, len(notecheck.load_notes(open_layout(root)[0].notes))))
    return 1 if any(f.level == "error" for f in findings) else 0


def cmd_review_notes(args) -> int:
    from . import notecheck, notereview
    from .config import deep_merge
    from .providers.agent import AgentJobsWaiting
    root = _lib(args)
    overrides = deep_merge(parse_sets(args.set), {"local_only": True} if args.local_only else {})
    if not _models_ok(args, "review-notes", overrides):
        return 2
    try:
        findings = notereview.run(root, note_slugs=args.notes or None, overrides=overrides)
    except AgentJobsWaiting as e:
        print(f"waiting for {e}: answer .work/agent/jobs.json, then run this again")
        return 0
    print(notecheck.report(findings, len(notecheck.load_notes(open_layout(root)[0].notes))))
    return 1 if any(f.level == "error" for f in findings) else 0


def cmd_reprocess(args) -> int:
    from .runner import process
    root = _lib(args)
    m = open_layout(root)[1]
    where = dict(w.split("=", 1) for w in args.where)
    keys = m.match(where) if where else []
    if args.ids:
        keys += [k for k, e in m.entries.items() if e.get("source_id") in args.ids and k not in keys]
    print(f"{len(keys)} sources match: " + ", ".join(m.entries[k].get("source_id", k) for k in keys))
    if not keys or args.dry_run:
        return 0
    if not _models_ok(args, "reprocess"):
        return 2
    m.mark_stale(keys)
    # rerun the stages under the new config; caches keyed by config fingerprint are invalidated
    rep = process(root, only=keys, force=True)
    if rep["failed"]:
        print(json.dumps(rep["failed"], indent=2))
    return 0


def cmd_handbook(args) -> int:
    from .handbook import finish, make, prepare
    output = Path(args.output).expanduser()
    if args.finish:
        print(json.dumps(finish(output), indent=2, ensure_ascii=False))
        return 0
    results = (prepare if args.prepare else make)(_lib(args), output, only=args.only,
                                                  input_dir=Path(args.input).expanduser().resolve() if args.input else None)
    print(json.dumps(results, indent=2, ensure_ascii=False))
    return 0


def cmd_digest(args) -> int:
    from .config import deep_merge
    from .digest import run
    from .providers.agent import AgentJobsWaiting
    overrides = deep_merge(parse_sets(args.set), {"local_only": True} if args.local_only else {})
    if args.reader == "session":
        overrides = deep_merge(overrides, {"digest": {"backend": "agent", "agent_model": args.agent_model}})
    if not _models_ok(args, "digest", overrides):
        return 2
    try:
        results = run(_lib(args), ids=args.ids or None, force=args.force, overrides=overrides)
    except AgentJobsWaiting as e:
        print(f"waiting for {e}: answer .work/agent/jobs.json, then run this again")
        return 0
    print(json.dumps(results, indent=2, ensure_ascii=False) if results else "no PDFs long enough for a digest")
    return 0


def cmd_check_figures(args) -> int:
    from .config import load_config
    from .figcheck import check_book, report
    from .providers import get_vision_provider
    root = _lib(args)
    cfg = load_config(root, parse_sets(args.set))
    sec = (cfg["pdf"].get("figures") or {}).get("second_model")
    if not sec:
        print("set pdf.figures.second_model in the config (a provider section, e.g. a chain of another family "
              "than the model that read the figures)", file=sys.stderr)
        return 2
    lay, manifest = open_layout(root)
    provider = get_vision_provider(sec, cfg)
    sources = lay.all_sources(manifest.entries)
    out_dir = lay.work / "figures"
    out_dir.mkdir(parents=True, exist_ok=True)
    code = 0
    for sid, md in sorted(sources.items()):
        if args.ids and sid not in args.ids or not (lay.assets_dir(sid) / "figures").exists():
            continue
        results = check_book(sid, md, lay.assets_dir(sid), out_dir / "figures_cache.json", provider,
                             scope=args.scope, parallel=args.parallel, limit=args.limit or None)
        text = report(sid, results)
        (out_dir / f"crosscheck-{sid}.md").write_text(text, encoding="utf-8")
        print(text)
        code = code or int(any(r.get("disagreements") for r in results))
    provider.close()
    return code


def cmd_pending(args) -> int:
    print("\n".join(open_layout(_lib(args))[1].pending_synthesis()))
    return 0


def cmd_mark(args) -> int:
    from .setup import refresh
    lay, m = open_layout(_lib(args))
    n = m.mark_synthesized(args.ids)
    refresh(lay, m.entries)
    print(f"marked {n} sources as synthesized")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="knowlib", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def lib(sp):
        sp.add_argument("-L", "--library", default=".", help="library root (default: current folder)")
        return sp

    s = sub.add_parser("init", help="set a library up: asks a few questions, or takes them as flags")
    s.add_argument("dir", nargs="?", default=".")
    s.add_argument("--mode", choices=["inplace", "separate"], help="inplace: use the folder as it is")
    s.add_argument("--skip", nargs="*", help="folders or name patterns to leave out")
    s.add_argument("--local-only", action="store_true", help="the material is confidential: nothing leaves this machine")
    s.add_argument("--language", help="en, el or mixed")
    s.add_argument("--fast-model", help="LM Studio model for frame decisions and digests")
    s.add_argument("--strong-model", help="LM Studio model for reading slides, captions and digest rereads")
    s.add_argument("--reviewer-model", help="LM Studio model that reviews notes; ideally another family")
    s.add_argument("--yes", action="store_true", help="ask nothing: the defaults for anything not given")
    s.set_defaults(fn=cmd_init)
    lib(sub.add_parser("doctor")).set_defaults(fn=cmd_doctor)
    s = lib(sub.add_parser("process"))
    s.add_argument("--input", help="folder to ingest instead of the inbox")
    s.add_argument("--layout", help='links in organized/, e.g. "{course}/{date}/{name}"; also {folder}')
    s.add_argument("--force", action="store_true", help="re-ingest even if unchanged")
    s.add_argument("--reader", choices=["local", "session"], default="local",
                   help="session: Haiku and Sonnet read the frames through Claude Code, without an API key")
    s.add_argument("--only", action="append", help="only inputs whose inbox path contains this text (repeatable)")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--local-only", action="store_true", help="block all API calls for this run")
    s.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                   help="override a config value for this run, e.g. --set video.fps=2")
    s.add_argument("--limit", type=int, help="process at most N inputs (for test runs)")
    s.add_argument("--no-model-check", action="store_true", help="skip the check that the models answer")
    s.set_defaults(fn=cmd_process)
    s = lib(sub.add_parser("transcribe", help="transcribe one file to test the Whisper setup"))
    s.add_argument("file")
    s.add_argument("--language")
    s.add_argument("--glossary", help="comma-separated terms")
    s.add_argument("-o", "--output")
    s.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    s.set_defaults(fn=cmd_transcribe)
    s = lib(sub.add_parser("handbook", help="write one handbook per lecture into --output"))
    s.add_argument("--output", required=True, help="target folder")
    s.add_argument("--prepare", action="store_true", help="write a brief per lecture for an agent to write from")
    s.add_argument("--finish", action="store_true", help="check the agent's draft.md files and export them")
    s.add_argument("--input", help="folder of lectures and course materials (default: the inbox)")
    s.add_argument("--only", action="append", help="only lectures whose path contains this text")
    s.set_defaults(fn=cmd_handbook)
    s = lib(sub.add_parser("digest", help="a finding aid per long PDF, for handbooks that cite it"))
    s.add_argument("--ids", nargs="*", default=[], help="only these source ids")
    s.add_argument("--force", action="store_true", help="rewrite digests that are already current")
    s.add_argument("--reader", choices=["local", "session"], default="local",
                   help="session: a Claude model reads the chapters through Claude Code")
    s.add_argument("--agent-model", default="sonnet", help="with --reader session: sonnet or haiku")
    s.add_argument("--local-only", action="store_true", help="block all API calls for this run")
    s.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    s.add_argument("--no-model-check", action="store_true", help="skip the check that the models answer")
    s.set_defaults(fn=cmd_digest)
    s = lib(sub.add_parser("check-figures", help="a second vision model rereads flagged figures and tables"))
    s.add_argument("--ids", nargs="*", default=[], help="only these source ids")
    s.add_argument("--scope", default="flagged+tables", choices=["flagged+tables", "flagged", "tables", "all"])
    s.add_argument("--parallel", type=int, default=1, help="requests at a time (free APIs queue the rest)")
    s.add_argument("--limit", type=int, default=0, help="only the first N figures, to try a model")
    s.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    s.set_defaults(fn=cmd_check_figures)
    lib(sub.add_parser("status")).set_defaults(fn=cmd_status)
    lib(sub.add_parser("qa")).set_defaults(fn=cmd_qa)
    s = lib(sub.add_parser("check-notes", help="citations, duplicates and consolidation of the topic notes"))
    s.add_argument("--only", nargs="*", choices=["citations", "duplicates", "consolidation"], default=[])
    s.add_argument("--notes", nargs="*", default=[], help="only these note slugs")
    s.set_defaults(fn=cmd_check_notes)
    s = lib(sub.add_parser("review-notes", help="a second model judges every claim against its cited passages"))
    s.add_argument("--notes", nargs="*", default=[], help="only these note slugs")
    s.add_argument("--local-only", action="store_true", help="refuse to send anything off this machine")
    s.add_argument("--set", action="append", default=[], help="config override, e.g. notes_review.openai_model=google/gemma-4-12b")
    s.add_argument("--no-model-check", action="store_true", help="skip the check that the models answer")
    s.set_defaults(fn=cmd_review_notes)
    s = lib(sub.add_parser("reprocess"))
    s.add_argument("--where", action="append", default=[], help="provenance filter, e.g. transcription.model=small")
    s.add_argument("--ids", nargs="*", default=[])
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--no-model-check", action="store_true", help="skip the check that the models answer")
    s.set_defaults(fn=cmd_reprocess)
    s = lib(sub.add_parser("models", help="which model does what: show, change (--use role=spec) and check them"))
    s.add_argument("--use", action="append", default=[], metavar="ROLE=SPEC",
                   help="e.g. review=session:opus, reading=server:https://host/v1|model|KEY_ENV, digest=lmstudio:<key>")
    s.add_argument("--check", action="store_true", help="check every role, including image detail and context")
    s.set_defaults(fn=cmd_models)
    lib(sub.add_parser("pending")).set_defaults(fn=cmd_pending)
    s = lib(sub.add_parser("mark-synthesized")); s.add_argument("ids", nargs="+"); s.set_defaults(fn=cmd_mark)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
