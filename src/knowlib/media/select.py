"""Frame selection, captioning and primary OCR with a vision model (one call per candidate).

The model sees the candidate's sample frames plus the transcript spoken over them, and the
previously kept slide. It decides:
  keep                 - new information (slide, chart, code, whiteboard state, demo screen)
  supersedes_previous  - same slide/whiteboard as the last kept one, but more complete
                         (a build-up step or more writing); replaces it, so the final state wins
  drop                 - speaker, transition, blur, title card, duplicate
When unsure, it keeps. Frames above vlm_max_native_px, or with dense small text, get a second
pass where the native frame is tiled into crops and read in full detail.
Results are cached per candidate, so an interrupted overnight run resumes where it stopped.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np
from PIL import Image

from .. import progress
from ..providers import get_vision_provider, release_all
from .frames import Candidate, _small, resized_copy, tiles
from .ocr import BACKENDS, crosscheck, normalize

log = logging.getLogger("knowlib")

BUILD_TYPES = {"slide", "code", "whiteboard"}

SELECT_PROMPT = """You are selecting frames from a recorded lecture to build a slide-by-slide record of it.
The images are {n} sample frame(s) from one stretch of the video ({start} to {end}), in time order (index 0 = earliest).

Transcript spoken during this stretch:
\"\"\"{transcript}\"\"\"

Previously kept frame: {previous}

Decide:
- "keep" if the frame shows information worth keeping: a new slide, chart, table, diagram, code, whiteboard or blackboard content, a demo or application screen.
- "supersedes_previous" if it shows the SAME slide or board as the previously kept frame but more complete (a slide that builds up step by step, more writing added). The final, most complete state should win.
- "drop" if it only shows the speaker or audience, a transition, a blurry or blank frame, a pure title card with no content, or an exact duplicate.
If you are unsure, choose "keep". Cues in the transcript such as "as you can see here" or "look at this chart" are strong signals to keep.

Return JSON with exactly these keys:
{{"decision": "keep" | "supersedes_previous" | "drop",
  "best_frame": <index of the clearest, most complete sample>,
  "reason": "<one short sentence>",
  "caption": "<one or two sentences in {caption_language}: what the frame shows and what it is about; for charts say what is plotted and the main takeaway>",
  "slide_text": "<all text visible in best_frame, transcribed exactly in its original language, line by line; tables as Markdown tables; empty string if none>",
  "content_type": "slide" | "chart" | "table" | "diagram" | "code" | "whiteboard" | "demo" | "photo" | "speaker" | "transition" | "other",
  "dense_text": <true if the frame has much small text that may need a closer look>}}"""

DECIDE_PROMPT = """You are selecting frames from a recorded lecture to build a slide-by-slide record of it.
The images are {n} sample frame(s) from one stretch of the video ({start} to {end}), in time order (index 0 = earliest).
The samples are expected to look alike. That they resemble each other is never a reason to drop them.

Transcript spoken during this stretch:
\"\"\"{transcript}\"\"\"

Decide:
- "keep" if any sample shows content: a slide, chart, table, diagram, code, whiteboard or blackboard, a demo or application screen. Repeats of earlier frames are removed in a later step, so keep them.
- "drop" only if no sample shows content: only the speaker or audience, a transition, a blurry or blank frame, or a pure title card.
If you are unsure, choose "keep".

Return JSON with exactly these keys:
{{"decision": "keep" | "drop",
  "best_frame": <index of the clearest, most complete sample>,
  "reason": "<one short sentence>",
  "content_type": "slide" | "chart" | "table" | "diagram" | "code" | "whiteboard" | "demo" | "photo" | "speaker" | "transition" | "other"}}"""

TRANSCRIBE_PROMPT = """Transcribe all text and numbers visible in this image, exactly as shown, line by line.
Tables as Markdown tables, including every row label, column label and total. Keep every number exactly.
Copy only what is visible. Do not explain, summarise, or add anything that is not in the image.
Output only the transcription."""

CAPTION_PROMPT = """This image is one frame of a recorded lecture.
Text on the frame:
\"\"\"{text}\"\"\"
Transcript spoken during it:
\"\"\"{transcript}\"\"\"
In one or two sentences in {caption_language} say what the frame shows and what it is about. For a table or chart, say exactly what its values represent.
Output only the sentences."""

TILE_PROMPT = """These images are overlapping crops of one lecture slide, left to right, top to bottom.
Transcribe ALL text exactly as written, in its original language, merging the crops into one
reading-order text and removing text duplicated by the overlap. Keep tables as Markdown tables and
keep every number exactly. Output only the text."""


def fmt_ts(t: float) -> str:
    t = max(0, int(round(t)))
    return f"{t // 3600:d}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60:02d}:{t % 60:02d}"


def transcript_between(segs: list[dict], start: float, end: float) -> str:
    return " ".join(s["text"] for s in segs if s["end"] > start and s["start"] < end).strip()


@dataclass
class Kept:
    start: float
    end: float
    frame: str                # native frame path
    caption: str
    slide_text: str
    content_type: str
    reason: str
    candidates: list[int] = field(default_factory=list)
    text_source: str = "vlm"
    flags: list[str] = field(default_factory=list)
    ocr: dict = field(default_factory=dict)
    samples: list[str] = field(default_factory=list)


def _as_text(value) -> str:
    """Vision models sometimes return slide_text as a list of lines instead of a string."""
    if isinstance(value, (list, tuple)):
        return "\n".join(str(v) for v in value)
    return str(value or "")


def _strip_fence(text: str) -> str:
    """Drop a ```markdown wrapper around a whole transcription."""
    m = re.fullmatch(r"```[a-z]*\n(.*?)\n?```", text.strip(), re.S)
    return (m.group(1) if m else text).strip()


def _richest_sample(samples: list, cfg: dict):
    """The sample showing the most text, latest on a tie: a slide that builds up ends complete.
    OCR counts words reliably where the model tends to name the first sample."""
    if len(samples) < 2:
        return samples[-1]
    oc = cfg["ocr_crosscheck"]
    names = oc.get("backends") or []
    try:
        words = [len(BACKENDS[names[0]](s.path, oc).split()) for s in samples]
    except Exception:  # noqa: BLE001 - OCR unavailable: the last sample is the safest guess
        return samples[-1]
    return samples[max(range(len(samples)), key=lambda i: (words[i], i))]


def _builds_on(prev: Kept, text: str, kind: str, share: float = 0.9) -> bool:
    """A slide that adds to the previous one: all its text is still there, plus more. Screens and
    charts can change under the same words, so only text slides are replaced."""
    if prev.content_type not in BUILD_TYPES or kind not in BUILD_TYPES:
        return False
    a, b = Counter(normalize(prev.slide_text).split()), Counter(normalize(text).split())
    return bool(a) and b.total() > a.total() and (a & b).total() >= share * a.total()


def _judgement_view(frame: Path, max_px: int, work: Path) -> Path:
    with Image.open(frame) as im:
        if max(im.size) <= max_px:
            return frame
    return resized_copy(frame, work / "views" / (frame.stem + ".jpg"), max_px)


def select(cands: list[Candidate], segs: list[dict], provider, cfg: dict, work: Path) -> tuple[list[Kept], list[Candidate], dict]:
    vc, fs = cfg["video"], cfg["frame_selection"]
    cache_path = work / "selection_cache.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    max_px = int(vc.get("vlm_max_native_px", 2048))
    ctx_s = float(vc.get("transcript_context_s", 10))
    lang = fs.get("caption_language", "English")
    split = bool(fs.get("split"))
    kept: list[Kept] = []
    dropped: list[Candidate] = []
    stats = {"calls": 0, "cached": 0, "keep": 0, "supersedes": 0, "drop": 0, "parse_failures": 0, "tiled": 0}
    model_tag = f"{provider.backend}:{provider.model}"

    def decide(c: Candidate) -> dict:
        key = f"{c.index}:{'|'.join(f.path.name for f in c.samples)}:{model_tag}"
        res = cache.get(key)
        if res is not None and not res.get("_failed"):
            stats["cached"] += 1
            return res
        spoken = transcript_between(segs, c.start, c.end + ctx_s)[:3000] or "(no speech)"
        if split:
            prompt = DECIDE_PROMPT.format(n=len(c.samples), start=fmt_ts(c.start), end=fmt_ts(c.end),
                                          transcript=spoken)
        else:
            prev = f'"{kept[-1].caption}"' if kept else "none yet"
            prompt = SELECT_PROMPT.format(n=len(c.samples), start=fmt_ts(c.start), end=fmt_ts(c.end),
                                          transcript=spoken, previous=prev, caption_language=lang)
        images = [_judgement_view(f.path, max_px, work) for f in c.samples]
        res = None
        for attempt in range(2):
            try:
                res = provider.json(images, prompt)
                break
            except ValueError as e:
                log.warning("candidate %d: unparseable reply (%s)", c.index, e)
        stats["calls"] += 1
        if res is None:
            stats["parse_failures"] += 1
            res = {"decision": "keep", "best_frame": len(c.samples) - 1, "reason": "model reply unparseable; kept by default",
                   "caption": "", "slide_text": "", "content_type": "other", "dense_text": True, "_failed": True}
        cache[key] = res
        cache_path.write_text(json.dumps(cache, ensure_ascii=False))
        return res

    reader, reader_tag = provider, model_tag
    if split and fs.get("transcribe_model"):
        # all decisions first, so only one model is in memory at a time
        for n, c in enumerate(cands, 1):
            progress.stage("decisions", n, len(cands))
            decide(c)
        release_all()
        reader = get_vision_provider({**fs, **fs["transcribe_model"]}, cfg)
        reader_tag = f"{reader.backend}:{reader.model}"

    for n, c in enumerate(cands, 1):
        progress.stage("transcription" if split else "frames", n, len(cands))
        res = decide(c)

        decision = str(res.get("decision", "keep")).lower()
        try:
            best = c.samples[int(res.get("best_frame", len(c.samples) - 1))]
        except (ValueError, IndexError, TypeError):
            best = c.samples[-1]
        if split:
            best = _richest_sample(c.samples, cfg)
        if decision == "drop":
            stats["drop"] += 1
            dropped.append(c)
            continue

        text, text_source = _as_text(res.get("slide_text")), "vlm"
        with Image.open(best.path) as im:
            big = max(im.size) > max_px
        if big or (res.get("dense_text") and not split):
            tkey = f"tiles:{best.path.name}:{model_tag}"
            if tkey not in cache:
                crops = tiles(best.path, work / "tiles", int(vc.get("tile_px", 1344)), float(vc.get("tile_overlap", 0.12)))
                cache[tkey] = provider.generate(crops, TILE_PROMPT).strip()
                cache_path.write_text(json.dumps(cache, ensure_ascii=False))
                stats["calls"] += 1
            if cache[tkey]:
                text, text_source = cache[tkey], "vlm-tiled-native"
                stats["tiled"] += 1
        elif split:
            tkey = f"text:{best.path.name}:{reader_tag}"
            if tkey not in cache:
                try:
                    reply = _strip_fence(reader.generate([best.path], TRANSCRIBE_PROMPT))
                    # a reply cut off at the limit is a runaway loop, not text; OCR fills it in later
                    cache[tkey] = "" if getattr(reader, "last_finish", None) == "length" else reply
                    cache_path.write_text(json.dumps(cache, ensure_ascii=False))
                except (OSError, RuntimeError) as e:   # server gone, stalled, or lms could not load it
                    log.warning("transcription of %s failed (%s)", best.path.name, e)
                stats["calls"] += 1
            text = cache.get(tkey, "")
            kind = str(res.get("content_type", "other"))
            decision = "supersedes_previous" if kept and _builds_on(kept[-1], text, kind) else "keep"

        if res.get("_failed"):
            flags = ["keep/drop reply could not be parsed; kept by default"] if split else \
                ["vision model reply could not be parsed; caption and text missing"]
        else:
            flags = []
        entry = Kept(start=c.start, end=c.end, frame=str(best.path), caption=str(res.get("caption", "")).strip(),
                     slide_text=text, content_type=str(res.get("content_type", "other")),
                     reason=str(res.get("reason", "")), candidates=[c.index], text_source=text_source, flags=flags,
                     samples=[str(f.path) for f in c.samples])
        if decision.startswith("supersede") and kept:
            prev_k = kept[-1]
            entry.start = prev_k.start
            entry.candidates = prev_k.candidates + entry.candidates
            kept[-1] = entry
            stats["supersedes"] += 1
        else:
            kept.append(entry)
            stats["keep"] += 1
    kept, stats["merged"] = _merge_repeats(kept, vc)
    return kept, dropped, stats


def wants_strong_caption(k: Kept, cfg: dict) -> bool:
    """Tables, charts and slides the OCR check flagged are worth the slower caption model."""
    types = cfg["frame_selection"].get("caption_model_types", ["table", "chart"])
    return k.content_type in types or "|---" in k.slide_text.replace(" ", "") or bool(k.ocr.get("flag"))


def caption_kept(kept: list[Kept], segs: list[dict], cfg: dict, work: Path) -> dict:
    """Step three of the split prompt: caption each kept frame from its final frame and checked text.
    The selection model does most frames, then caption_model (if set) loads and does the hard ones."""
    fs = cfg["frame_selection"]
    release_all()
    cache_path = work / "selection_cache.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    strong = fs.get("caption_model")
    hard = [k for k in kept if strong and wants_strong_caption(k, cfg)]
    easy = [k for k in kept if k not in hard]
    ctx_s = float(cfg["video"].get("transcript_context_s", 10))

    def run(frames: list[Kept], section: dict) -> list[Kept]:
        failed = []
        if not frames:
            return failed
        provider = get_vision_provider(section, cfg)
        tag = f"{provider.backend}:{provider.model}"
        for n, k in enumerate(frames, 1):
            progress.stage("captions", n, len(frames))
            key = f"caption:{Path(k.frame).name}:{tag}"
            if key not in cache:
                prompt = CAPTION_PROMPT.format(text=k.slide_text[:3000],
                                               transcript=transcript_between(segs, k.start, k.end + ctx_s)[:3000] or "(no speech)",
                                               caption_language=fs.get("caption_language", "English"))
                try:
                    cache[key] = provider.generate([Path(k.frame)], prompt).strip()
                except (OSError, RuntimeError) as e:   # server gone, or lms could not load it
                    log.warning("caption of %s failed (%s)", Path(k.frame).name, e)
                    failed.append(k)
                    continue
                cache_path.write_text(json.dumps(cache, ensure_ascii=False))
            k.caption = cache[key]
        return failed

    failed = run(easy, fs)
    retry = []
    if hard:
        release_all()
        retry = run(hard, {**fs, **strong})
        release_all()
        failed += run(retry, fs)    # a frame the slow model stalls on still gets a caption
    for k in failed:
        k.flags.append("caption missing: no model answered")
    return {"selection_model": len(easy) + len(retry), "caption_model": len(hard) - len(retry), "failed": len(failed)}


def match_frame_to_text(k: Kept, cfg: dict) -> None:
    """Keep the sample that shows the model's text: it may read one sample and name another as best."""
    if len(k.samples) < 2 or not k.slide_text.strip():
        return

    def score(path: str) -> float:
        sims = [r["similarity"] for r in crosscheck(Path(path), k.slide_text, cfg)["backends"].values()]
        return sum(sims) / len(sims) if sims else 0.0

    scores = {p: score(p) for p in k.samples}
    best = max(scores, key=scores.get)
    if scores[best] > scores.get(k.frame, 0.0) + 0.05:
        k.frame = best


def fill_text_from_ocr(k: Kept) -> None:
    """A frame the vision model gave no text for keeps what classic OCR read, labelled as such."""
    if k.slide_text.strip():
        return
    text = next((r["text"] for r in k.ocr.get("backends", {}).values() if r.get("text", "").strip()), "")
    if text:
        k.slide_text, k.text_source = text.strip(), "ocr"
        k.ocr["flag"] = False


def _merge_repeats(kept: list[Kept], vc: dict) -> tuple[list[Kept], int]:
    """Fold consecutive frames of one unchanged slide (e.g. only a laser pointer moved) into one.

    Needs both the same text and the same image, so a chart that changes under an identical
    title stays separate.
    """
    from skimage.metrics import structural_similarity as ssim

    text_thr = float(vc.get("repeat_text_similarity", 0.98))
    image_thr = float(vc.get("repeat_ssim", 0.97))
    sw = int(vc.get("ssim_width", 640))
    out: list[Kept] = []
    merged = 0
    for k in kept:
        prev = out[-1] if out else None
        same = False
        if prev and prev.slide_text.strip() and k.slide_text.strip():
            ratio = SequenceMatcher(None, normalize(prev.slide_text), normalize(k.slide_text)).ratio()
            if ratio >= text_thr:
                a = np.asarray(_small(Path(prev.frame), sw), dtype=np.float32)
                b = np.asarray(_small(Path(k.frame), sw), dtype=np.float32)
                same = a.shape == b.shape and ssim(a, b, data_range=255.0) >= image_thr
        if same:
            k.start = prev.start
            k.candidates = prev.candidates + k.candidates
            k.caption = k.caption or prev.caption
            k.flags = list(dict.fromkeys(prev.flags + k.flags))
            out[-1] = k
            merged += 1
        else:
            out.append(k)
    return out, merged


def kept_to_json(kept: list[Kept]) -> list[dict]:
    return [asdict(k) for k in kept]
