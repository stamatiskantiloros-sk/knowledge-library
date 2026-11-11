"""Media session ingestor: audio-only and video lectures.

Stages (each cached in .work/<id>/ so an interrupted run resumes):
  session assembly -> alignment (multi-source only) -> transcription (primary audio, mapped to
  the video timeline) -> 1 fps native extraction -> dedup -> VLM selection/caption/OCR ->
  OCR cross-check -> frame handling -> slide-by-slide source.md
Audio-only sessions skip the frame stages and become a timestamped transcript.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path

from .. import progress
from ..config import session_config
from ..contract import SourceRecord, SourceWriter, hash_many, make_id, stage
from ..media import align as align_mod
from ..media.frames import extract_frames, group_frames, resized_copy, save_candidates
from ..media.ocr import crosscheck, recheck_without_recurring
from ..media.select import caption_kept, fill_text_from_ocr, fmt_ts, kept_to_json, match_frame_to_text, select
from ..media.session import assemble
from ..media.transcribe import map_segments, transcribe
from ..providers import get_vision_provider, release_all
from ..providers.agent import stop_if_waiting
from .base import Context, Ingestor, IngestResult, InputItem, register

log = logging.getLogger("knowlib")


def _paragraphs(segs: list[dict], start: float, end: float, para_s: float = 60) -> list[str]:
    """Transcript between start and end as paragraphs of ~para_s seconds, each with a timestamp."""
    out, cur, cur_t = [], [], None
    for s in segs:
        mid = (s["start"] + s["end"]) / 2
        if not (start <= mid < end):
            continue
        if cur_t is None:
            cur_t = s["start"]
        cur.append(s["text"])
        if s["end"] - cur_t >= para_s:
            out.append(f"[{fmt_ts(cur_t)}] " + " ".join(cur))
            cur, cur_t = [], None
    if cur:
        out.append(f"[{fmt_ts(cur_t)}] " + " ".join(cur))
    return out


def _seconds(t) -> float:
    """'1:02:41', '62:41' or a number of seconds."""
    if isinstance(t, (int, float)):
        return float(t)
    p = [float(x) for x in str(t).split(":")]
    return p[0] * 3600 + p[1] * 60 + p[2] if len(p) == 3 else p[0] * 60 + p[1] if len(p) == 2 else p[0]


def _reuse_earlier(path: Path, fingerprint: dict) -> None:
    """A transcript cached under the session's earlier id (a session.yaml edit changes the id): copy it
    over when its fingerprint matches, so the recording is not transcribed again."""
    if path.exists():
        return
    for other in path.parent.parent.glob(f"*/{path.name}"):
        try:
            if json.loads(other.read_text()).get("fingerprint") == fingerprint:
                shutil.copy2(other, path)
                return
        except (OSError, ValueError):
            continue


def _cached(path: Path, fingerprint: dict, compute):
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("fingerprint") == fingerprint:
            return data["value"]
    value = compute()
    path.write_text(json.dumps({"fingerprint": fingerprint, "value": value}, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return value


@register
class MediaSessionIngestor(Ingestor):
    name = "media"

    def can_handle(self, item: InputItem) -> bool:
        return item.kind == "session"

    def ingest(self, item: InputItem, ctx: Context) -> IngestResult:
        cfg = session_config(ctx.config, item.sidecar)
        content_hash = item.content_hash or hash_many(item.paths)
        tmp_work = ctx.work_for(f"_session_{content_hash[:12]}")
        sess = assemble(item.paths, item.folder or item.path.parent, item.sidecar, tmp_work)
        sid = make_id(sess.title, content_hash)
        work = ctx.work_dir / sid
        if work.exists() and tmp_work != work:
            shutil.rmtree(tmp_work)
        elif tmp_work != work:
            tmp_work.rename(work)
        work.mkdir(parents=True, exist_ok=True)
        if ctx.force:
            for f in ("alignment.json", "transcript.json", "candidates.json", "selection_cache.json"):
                (work / f).unlink(missing_ok=True)

        flags: list[str] = []
        pipeline: dict = {"session": {"tool": "knowlib", "primary_audio": sess.primary_audio.name if sess.primary_audio else None,
                                      "primary_reason": sess.primary_reason}}
        language = cfg["transcription"].get("language")
        stats: dict = {"files": [p.name for p in item.paths]}
        if sess.video:
            stats["duration_s"] = round(sess.info[sess.video.name]["duration"], 1)
            stats["video_resolution"] = f"{sess.info[sess.video.name].get('width')}x{sess.info[sess.video.name].get('height')}"
        elif sess.primary_audio:
            stats["duration_s"] = round(sess.info[sess.primary_audio.name]["duration"], 1)

        # ---- alignment ---------------------------------------------------------------
        mapping = align_mod.AlignmentMap(identity=True)
        if sess.needs_alignment:
            if not sess.info[sess.video.name]["has_audio"]:
                flags.append("video has no audio track; cannot align - transcript timestamps assume both start together")
            else:
                fp = {"primary": sess.primary_audio.name, "video": sess.video.name, "cfg": cfg["alignment"]}
                progress.stage("alignment")
                d = _cached(work / "alignment.json", fp,
                            lambda: align_mod.align(sess.primary_audio, sess.video, work, cfg).to_dict())
                mapping = align_mod.AlignmentMap.from_dict(d)
                q = mapping.quality
                pipeline["alignment"] = {"tool": "knowlib-envelope-xcorr", "segments": len(mapping.segments),
                                         "offset_s": round(mapping.segments[0].offset, 3) if mapping.segments else None,
                                         "drift_ms_per_hour": q.get("drift_ms_per_hour"),
                                         "mean_confidence": q.get("mean_confidence")}
                if q.get("warning"):
                    flags.append(f"alignment: {q['warning']}")
                if len(mapping.segments) > 1:
                    flags.append(f"alignment: {len(mapping.segments)} segments (recorder paused or restarted)")
                if cfg["alignment"].get("mux_better_audio"):
                    dest = ctx.root / "archive" / f"{sid}.mp4"
                    dest.parent.mkdir(exist_ok=True)
                    if align_mod.mux_primary_audio(sess.video, sess.primary_audio, mapping, dest):
                        stats["archive_video"] = f"archive/{dest.name}"

        # ---- transcription -------------------------------------------------------------
        segs: list[dict] = []
        if sess.primary_audio or sess.subtitles:
            tc = cfg["transcription"]
            fp = {"audio": sess.primary_audio.name if sess.primary_audio else None, "backend": tc["backend"],
                  "model": tc.get("model"), "language": language, "glossary": sess.glossary,
                  "subs": [s.name for s in sess.subtitles]}

            def run_transcription():
                s, prov, fl = transcribe(sess.primary_audio, work, cfg, language=language,
                                         glossary=sess.glossary, subtitles=sess.subtitles)
                return {"segments": s, "provenance": prov, "flags": fl}
            progress.stage("speech to text")
            _reuse_earlier(work / "transcript.json", fp)
            tr = _cached(work / "transcript.json", fp, run_transcription)
            segs = map_segments(tr["segments"], mapping.map)
            pipeline["transcription"] = tr["provenance"]
            flags += tr["flags"]
            stats["transcript_segments"] = len(segs)
        else:
            flags.append("no audio: transcript missing")

        # ---- frames -------------------------------------------------------------------
        kept, dropped, sel_stats, dedup_stats = [], [], {}, {}
        if sess.video:
            vc = cfg["video"]
            progress.stage("frame extraction")
            frames = extract_frames(sess.video, work / "frames", float(vc["fps"]), vc.get("frame_format", "jpg"))
            cands, dedup_stats = group_frames(frames, vc)
            save_candidates(cands, dedup_stats, work / "candidates.json")
            pipeline["frames"] = stage("ffmpeg", fps=vc["fps"], resolution="native")
            pipeline["dedup"] = {"tool": "phash+ssim", "phash_threshold": vc["phash_threshold"],
                                 "ssim_threshold": vc["ssim_threshold"] if vc.get("ssim_check") else None,
                                 "mode": dedup_stats["mode"]}
            if dedup_stats["mode"] == "interval":
                flags.append("no stable slides detected (talking head or constant motion): sparse interval frames used")
            provider = get_vision_provider(cfg["frame_selection"], cfg)
            kept, dropped, sel_stats = select(cands, segs, provider, cfg, work)
            stop_if_waiting(Path(cfg["agent_dir"]))
            pipeline["frame_selection"] = provider.provenance()
            fs = cfg["frame_selection"]
            if fs.get("transcribe_model"):
                pipeline["reader"] = {k: {**fs["transcribe_model"], **s}.get("agent_model") or {**fs, **fs["transcribe_model"], **s}.get("openai_model")
                                      for k, s in (("ordinary", {}), ("hard", fs.get("transcribe_hard") or {}))}
            split = bool(cfg["frame_selection"].get("split"))
            if not split:
                release_all()   # free the VLM before the next session/stage loads a model

            # OCR cross-check on every kept frame, at native resolution
            ocr_errors: set[str] = set()
            for n, k in enumerate(kept, 1):
                progress.stage("OCR check", n, len(kept))
                match_frame_to_text(k, cfg)
                k.ocr = crosscheck(Path(k.frame), k.slide_text, cfg)
                ocr_errors.update(k.ocr.get("errors", []))
                fill_text_from_ocr(k)
            recheck_without_recurring([(None if k.text_source == "ocr" else k.slide_text, k.ocr) for k in kept],
                                      cfg["ocr_crosscheck"])
            if split:
                stats["captions"] = caption_kept(kept, segs, cfg, work)
                stop_if_waiting(Path(cfg["agent_dir"]))
                if strong := cfg["frame_selection"].get("caption_model"):
                    pipeline["caption"] = strong.get("agent_model") or strong.get("openai_model")
                release_all()
            if any(k.ocr.get("backends") for k in kept):
                pipeline["ocr_crosscheck"] = {"tool": ",".join(cfg["ocr_crosscheck"]["backends"]),
                                              "languages": cfg["ocr_crosscheck"].get("languages")}
            for e in sorted(ocr_errors):
                flags.append(f"ocr cross-check unavailable: {e}")
            (work / "kept.json").write_text(json.dumps(kept_to_json(kept), ensure_ascii=False, indent=1), encoding="utf-8")
            stats.update(frames=dedup_stats["frames"], candidates=dedup_stats["groups"], kept=len(kept),
                         selection=sel_stats)

        # ---- related slide deck --------------------------------------------------------
        related = []
        if item.sidecar.get("slides") and ctx.manifest is not None:
            folder_key = item.key.rstrip("/")
            key = (f"{folder_key}/{item.sidecar['slides']}" if item.key.endswith("/") and folder_key != "."
                   else item.sidecar["slides"])
            e = ctx.manifest.entries.get(key)
            if e and e.get("source_id"):
                related.append(e["source_id"])
                if ctx.layout is not None:                 # audio without video: align speech to the deck
                    self._deck = (sess.folder / item.sidecar["slides"], ctx.layout.source_md(e["source_id"], e),
                                  {int(n) for n in item.sidecar.get("skip_slides") or []})
                    self._starts = item.sidecar.get("slide_starts") or {}
            else:
                flags.append(f"slides file {item.sidecar['slides']} named in session.yaml was not ingested")

        # ---- write the source ----------------------------------------------------------
        w = ctx.writer(sid)
        try:
            body = self._render(sess, segs, kept, w, cfg, flags, stats) + self._dropped(dropped, w)
            (w.asset_path("transcript.json")).write_text(
                json.dumps(segs, ensure_ascii=False, indent=0), encoding="utf-8")
            if len(item.paths) > 1:
                stype = "session"
            else:
                stype = "video" if sess.video else "audio"
            rec = SourceRecord(id=sid, title=sess.title, source_type=stype,
                               origin=[f"inbox/{(Path(item.key) / p.name).as_posix()}" if item.key.endswith("/")
                                       else f"inbox/{(Path(item.key).parent / p.name).as_posix()}" for p in item.paths],
                               hash=content_hash, language=language, pipeline=pipeline, flags=flags,
                               original_date=str(item.sidecar.get("date")) if item.sidecar.get("date") else
                               self._creation(sess), tags=list(item.sidecar.get("tags", [])),
                               related=related, stats=stats)
            md = w.commit(rec, body)
        except Exception:
            w.abort()
            raise

        self._cleanup(work, kept, cfg)
        return IngestResult(sid, md, pipeline, flags)

    @staticmethod
    def _creation(sess) -> str | None:
        for p in [sess.video, sess.primary_audio]:
            if p and sess.info.get(p.name, {}).get("creation_time"):
                return sess.info[p.name]["creation_time"][:10]
        return None

    _deck = None
    _starts = None

    def _render_aligned(self, sess, segs, w: SourceWriter, flags, stats, pipeline: dict | None = None) -> str:
        """An audio-only lecture with its slide deck: each slide with what was said while it was up,
        matched by the slides' text (media/slidealign.py)."""
        import pymupdf
        from ..media.slidealign import align
        pdf, deck_md, skip = self._deck
        raw = deck_md.read_text(encoding="utf-8")
        parts = re.split(r"<!-- page (\d+) \(pdf \d+\) -->", raw)[1:]
        pages = {int(parts[i]): parts[i + 1] for i in range(0, len(parts), 2)}
        slides = [(n, t) for n, t in sorted(pages.items()) if n not in skip]
        places = align(segs, slides)
        manual = self._starts or {}
        if manual:                                         # session.yaml slide_starts: checked by hand
            by_n = {p.slide: p for p in places}
            for n, t in manual.items():
                if int(n) in by_n:
                    by_n[int(n)].start = None if t in (None, "", "none") else _seconds(t)
                    by_n[int(n)].score = 9.9 if t not in (None, "", "none") else 0.0
            shown = sorted((p for p in places if p.start is not None), key=lambda p: p.start)
            for a, b in zip(shown, shown[1:]):
                a.end = b.start
            if shown:
                shown[-1].end = max([s["end"] for s in segs], default=shown[-1].start)
        doc = pymupdf.open(pdf)
        lines = [f"# {sess.title}", "",
                 f"*Duration {fmt_ts(stats.get('duration_s') or 0)} · speech matched to the {len(slides)} slides of "
                 f"{pdf.name} by their text" + (f"; slides {', '.join(map(str, sorted(skip)))} left out" if skip else "")
                 + "*"]
        shown = [p for p in places if p.start is not None]
        if shown and shown[0].start > 0:
            pre = _paragraphs(segs, float("-inf"), shown[0].start)
            if pre:
                lines += ["", f"## Before the first slide (to {fmt_ts(shown[0].start)})", "", *[p + "\n" for p in pre]]
        last_end = shown[0].start if shown else 0.0
        end_all = max([s["end"] for s in segs], default=0) + 1
        weak = []
        for p in places:
            text = pages[p.slide]
            figures = re.findall(r"> \*\*Figure, read by the vision model:\*\*\n((?:>.*\n?)*)", text)
            body = re.sub(r"> \*\*Figure, read by the vision model:\*\*\n(?:>.*\n?)*", "", text)
            body = re.sub(r"!\[[^\]]*\]\([^)]*\)|<!--.*?-->", "", body).strip()
            reading = "\n".join(re.sub(r"^> ?", "", ln) for f in figures for ln in f.splitlines()).strip()
            img = w.asset_path(f"slide_{p.slide:03d}.png")
            if p.slide - 1 < doc.page_count:
                doc[p.slide - 1].get_pixmap(dpi=110).save(str(img))
            start = p.start if p.start is not None else last_end
            end = p.end if p.end is not None else start
            if p.end is None and p.start is not None:
                end = end_all
            lines += ["", f"## Slide {p.slide} ({fmt_ts(start)}–{fmt_ts(end)})", f"![Slide {p.slide}](assets/{img.name})"]
            if p.start is None:
                lines += ["", "*Not discussed in the recording.*"]
            elif p.score < 1.0:
                weak.append(p.slide)
            if body:
                lines += ["", "**On slide:**", "", body]
            if reading:
                lines += ["", f"**Figure:** {reading}"]
            if p.start is not None:
                spoken = _paragraphs(segs, start, end)
                if spoken:
                    lines += ["", "**Spoken:**", "", *[s + "\n" for s in spoken]]
                last_end = end
        missed = [p.slide for p in places if p.start is None]
        stats["slides"] = len(slides)
        stats["slides_discussed"] = len(slides) - len(missed)
        if missed:
            flags.append(f"slides not matched to any speech: {', '.join(map(str, missed))} (not discussed, "
                         "or discussed with few of their words: check)")
        if weak:
            flags.append(f"slides matched weakly, check where they start and end: {', '.join(map(str, weak))}")
        return "\n".join(lines)

    def _render(self, sess, segs, kept, w: SourceWriter, cfg, flags, stats) -> str:
        if not kept and self._deck and segs:
            try:
                return self._render_aligned(sess, segs, w, flags, stats)
            finally:
                self._deck = None
        self._deck = None
        vc = cfg["video"]
        lines = [f"# {sess.title}", ""]
        dur = stats.get("duration_s")
        meta = []
        if dur:
            meta.append(f"Duration {fmt_ts(dur)}")
        if kept:
            meta.append(f"{len(kept)} slides/frames kept from {stats.get('candidates')} candidates")
        if sess.needs_alignment:
            meta.append(f"transcript from {sess.primary_audio.name} aligned to {sess.video.name}")
        if meta:
            lines.append("*" + " · ".join(meta) + "*")
        if not kept:
            lines += ["", "## Transcript", ""]
            end = max([s["end"] for s in segs], default=0) + 1
            chunk = 300.0
            t = 0.0
            while t < end:
                paras = _paragraphs(segs, t, t + chunk)
                if paras:
                    lines += [f"### {fmt_ts(t)}–{fmt_ts(min(t + chunk, end))}", "", *[p + "\n" for p in paras]]
                t += chunk
            return "\n".join(lines)

        starts = [k.start for k in kept] + [float("inf")]
        if starts[0] > 0:
            pre = _paragraphs(segs, float("-inf"), starts[0])
            if pre:
                lines += ["", f"## Before the first slide (to {fmt_ts(starts[0])})", "", *[p + "\n" for p in pre]]
        ext = Path(kept[0].frame).suffix
        for i, k in enumerate(kept, 1):
            name = f"frame_{i:03d}{ext}"
            full = w.asset_path(name)
            shutil.copy2(k.frame, full)
            view = resized_copy(full, w.asset_path(f"view/frame_{i:03d}.jpg"), int(vc["model_view_max_px"]))
            with_view = view.stat().st_size != full.stat().st_size or view.suffix != full.suffix
            if not with_view:
                view.unlink()
            end = starts[i] if starts[i] != float("inf") else max([s["end"] for s in segs], default=k.end)
            lines += ["", f"## Slide {i} ({fmt_ts(k.start)}–{fmt_ts(end)})"]
            img = f"assets/view/frame_{i:03d}.jpg" if with_view else f"assets/{name}"
            lines.append(f"![Slide {i}]({img})" + (f" · [full resolution](assets/{name})" if with_view else ""))
            if k.ocr.get("flag"):
                det = []
                for b, r in k.ocr["backends"].items():
                    if r.get("numbers_only_vlm") or r.get("numbers_only_ocr"):
                        det.append(f"{b}: numbers VLM {r.get('numbers_only_vlm')} vs OCR {r.get('numbers_only_ocr')}")
                    else:
                        det.append(f"{b}: text similarity {r.get('similarity')}")
                lines.append(f"> **OCR check:** the vision model's text and classic OCR disagree ({'; '.join(det)}). "
                             "Verify against the image before relying on the text.")
                flags.append(f"slide {i} ({fmt_ts(k.start)}): OCR disagreement")
            for fl in k.flags:
                flags.append(f"slide {i}: {fl}")
            if k.slide_text.strip():
                label = "On slide (OCR)" if k.text_source == "ocr" else "On slide"
                lines += ["", f"**{label}:**", "", k.slide_text.strip()]
            if k.ocr.get("flag"):
                for b, r in k.ocr["backends"].items():
                    if r.get("text", "").strip():
                        lines += ["", f"**OCR text ({b}):**", "", r["text"].strip()]
            if k.caption:
                lines += ["", f"**Figure:** {k.caption}" + (f" *({k.content_type})*" if k.content_type not in ("slide", "other") else "")]
            spoken = _paragraphs(segs, k.start, end)
            if spoken:
                lines += ["", "**Spoken:**", "", *[p + "\n" for p in spoken]]
        return "\n".join(lines)

    @staticmethod
    def _dropped(dropped, w: SourceWriter) -> str:
        """The frames the model judged empty, with a thumbnail and its reason, so a wrong drop can be seen."""
        if not dropped:
            return ""
        lines = ["", "", "## Dropped frames", "",
                 "The fast model judged these frames to have no content, so they are not slides above. "
                 "If something seems missing from the lecture, check them.", ""]
        for c in dropped:
            thumb = resized_copy(c.samples[-1].path, w.asset_path(f"dropped/drop_{c.index:03d}.jpg"), 480)
            reason = f": {c.drop_reason}" if c.drop_reason else ""
            lines.append(f"- {fmt_ts(c.start)}–{fmt_ts(c.end)}{reason} ![dropped](assets/dropped/{thumb.name})")
        return "\n".join(lines)

    @staticmethod
    def _cleanup(work: Path, kept, cfg) -> None:
        vc = cfg["video"]
        for wav in work.glob("*.wav"):        # decoded audio; re-created from the original if needed
            wav.unlink()
        frames_dir = work / "frames"
        if not frames_dir.exists():
            return
        if vc.get("keep_lowres_archive", True) and (work / "candidates.json").exists():
            # low-res copies of every candidate's sample frames: enough to re-run selection
            # with a better model later without re-extracting the video
            arch = work / "archive"
            arch.mkdir(exist_ok=True)
            px = int(vc.get("archive_px", 480))
            cands = json.loads((work / "candidates.json").read_text())["candidates"]
            for name in sorted({n for c in cands for n in c["samples"]}):
                if (frames_dir / name).exists():
                    resized_copy(frames_dir / name, arch / (Path(name).stem + ".jpg"), px)
        shutil.rmtree(frames_dir)
        for d in ("tiles", "views"):
            shutil.rmtree(work / d, ignore_errors=True)
        for wav in work.glob("*.wav"):
            wav.unlink()
