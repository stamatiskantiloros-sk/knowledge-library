"""Transcription, always local by default: mlx-whisper (Apple GPU) with large-v3-turbo.

Language is set explicitly per session (no auto-detect on mixed Greek/English lectures), and
Whisper's initial prompt is seeded with the session glossary so domain terms and names are
spelled correctly. Timestamps are in the primary audio's timeline; the caller maps them onto
the video timeline.
"""
from __future__ import annotations

import gc
import json
import re
import shutil
import subprocess
from pathlib import Path

from ..contract import stage
from .av import extract_wav


def glossary_prompt(terms: list[str], max_chars: int) -> str | None:
    if not terms:
        return None
    out = "Glossary: "
    for t in terms:
        if len(out) + len(t) + 2 > max_chars:
            break
        out += t + ", "
    return out.rstrip(", ") + "."


def whisper_language(lang: str | None, tc: dict) -> str | None:
    if lang in (None, "", "auto"):
        return None
    return tc.get("mixed_language_as", "el") if lang == "mixed" else lang


# ---- backends -----------------------------------------------------------------------------

def _mlx_whisper(wav: Path, lang, prompt, tc: dict) -> list[dict]:
    import mlx_whisper
    r = mlx_whisper.transcribe(str(wav), path_or_hf_repo=tc["model"], language=lang,
                               initial_prompt=prompt, word_timestamps=tc.get("word_timestamps", True),
                               condition_on_previous_text=tc.get("condition_on_previous_text", False),
                               verbose=None)
    return [_seg(s) for s in r["segments"]]


def _faster_whisper(wav: Path, lang, prompt, tc: dict) -> list[dict]:
    from faster_whisper import WhisperModel
    model = WhisperModel(tc.get("faster_whisper_model", "large-v3-turbo"), device="auto", compute_type="auto")
    segs, _ = model.transcribe(str(wav), language=lang, initial_prompt=prompt, vad_filter=True,
                               word_timestamps=tc.get("word_timestamps", True),
                               condition_on_previous_text=tc.get("condition_on_previous_text", False))
    out = []
    for s in segs:
        out.append({"start": s.start, "end": s.end, "text": s.text.strip(),
                    "words": [{"start": w.start, "end": w.end, "word": w.word} for w in (s.words or [])]})
    return out


def _whisper_cpp(wav: Path, lang, prompt, tc: dict) -> list[dict]:
    exe = shutil.which("whisper-cli") or shutil.which("whisper-cpp")
    if not exe:
        raise RuntimeError("whisper-cli not found (brew install whisper-cpp)")
    model = str(Path(tc["whisper_cpp_model"]).expanduser())
    base = wav.with_suffix("")
    cmd = [exe, "-m", model, "-f", str(wav), "-ojf", "-of", str(base), "-l", lang or "auto"]
    if prompt:
        cmd += ["--prompt", prompt]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-800:])
    data = json.loads(Path(f"{base}.json").read_text(encoding="utf-8"))
    return [{"start": t["offsets"]["from"] / 1000, "end": t["offsets"]["to"] / 1000,
             "text": t["text"].strip()} for t in data.get("transcription", [])]


_TS = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")


def _ts(s: str) -> float:
    m = _TS.search(s)
    h, mi, se, ms = m.groups()
    return int(h or 0) * 3600 + int(mi) * 60 + int(se) + int(ms.ljust(3, "0")) / 1000


def parse_subtitles(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    out = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        lines = [ln for ln in block.strip().splitlines() if ln.strip()]
        idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if idx is None:
            continue
        a, b = lines[idx].split("-->")
        body_lines = lines[idx + 1:]
        # some exporters (e.g. Udemy downloads) put the cue number after the text as well
        while body_lines and body_lines[-1].strip().isdigit():
            body_lines.pop()
        body = " ".join(re.sub(r"<[^>]+>", "", ln) for ln in body_lines).strip()
        if body:
            out.append({"start": _ts(a), "end": _ts(b), "text": body})
    return out


def _mock(wav: Path, lang, prompt, tc: dict) -> list[dict]:
    from .av import load_wav
    x, sr = load_wav(wav)
    dur = len(x) / sr
    return [{"start": float(t), "end": float(min(t + 10, dur)), "text": f"[mock speech {int(t)}s]"}
            for t in range(0, int(dur), 10)]


def _seg(s: dict) -> dict:
    out = {"start": float(s["start"]), "end": float(s["end"]), "text": s["text"].strip()}
    if s.get("words"):
        out["words"] = [{"start": w["start"], "end": w["end"], "word": w["word"]} for w in s["words"]]
    return out


BACKENDS = {"mlx-whisper": _mlx_whisper, "faster-whisper": _faster_whisper,
            "whisper-cpp": _whisper_cpp, "mock": _mock}
PACKAGES = {"mlx-whisper": "mlx-whisper", "faster-whisper": "faster-whisper"}


def clean_repetitions(segs: list[dict]) -> tuple[list[dict], int]:
    """Whisper sometimes loops on silence; collapse 3+ identical consecutive segments."""
    out, dropped, run = [], 0, 0
    for s in segs:
        if out and s["text"] and s["text"] == out[-1]["text"]:
            run += 1
            if run >= 2:
                out[-1]["end"] = s["end"]
                dropped += 1
                continue
        else:
            run = 0
        out.append(s)
    return [s for s in out if s["text"]], dropped


def transcribe(audio: Path, work: Path, cfg: dict, *, language: str | None, glossary: list[str],
               subtitles: list[Path] | None = None) -> tuple[list[dict], dict, list[str]]:
    tc = cfg["transcription"]
    backend = tc["backend"]
    flags: list[str] = []
    if subtitles and backend == "subtitles" or (subtitles and tc.get("prefer_subtitles")):
        segs = parse_subtitles(subtitles[0])
        return segs, {"tool": "subtitles", "file": subtitles[0].name}, flags
    if backend == "subtitles":
        raise RuntimeError("transcription.backend is 'subtitles' but the session has no .srt/.vtt")
    wav = extract_wav(audio, work / "transcribe.wav")
    lang = whisper_language(language, tc)
    prompt = glossary_prompt(glossary, int(tc.get("glossary_prompt_chars", 800)))
    segs = BACKENDS[backend](wav, lang, prompt, tc)
    gc.collect()
    segs, dropped = clean_repetitions(segs)
    if dropped:
        flags.append(f"transcription: collapsed {dropped} repeated segments (possible hallucination loop)")
    prov = stage(backend, model=tc.get("model") if backend == "mlx-whisper" else
                 tc.get("whisper_cpp_model") if backend == "whisper-cpp" else
                 tc.get("faster_whisper_model") if backend == "faster-whisper" else backend,
                 package=PACKAGES.get(backend), language=lang, glossary_terms=len(glossary) or None)
    return segs, prov, flags


def map_segments(segs: list[dict], fn) -> list[dict]:
    out = []
    for s in segs:
        m = dict(s, start=fn(s["start"]), end=fn(s["end"]))
        if "words" in s:
            m["words"] = [dict(w, start=fn(w["start"]), end=fn(w["end"])) for w in s["words"]]
        out.append(m)
    return out
