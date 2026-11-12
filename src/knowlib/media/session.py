"""Session assembly: one lecture = one session, possibly several files.

session.yaml (all keys optional):

    title: Corporate Finance - Lecture 4
    date: 2026-10-28
    language: el            # el | en | mixed
    glossary: [WACC, ΔΕΣΦΑ, CAPM]      # or a path to a text file, one term per line
    primary_audio: recorder.wav         # the best recording; otherwise picked by SNR
    video: lecture.mp4                 # if the folder has several videos
    slides: slides.pdf                 # the deck, ingested alongside and linked
    local_only: true                   # block all API calls for this session
    fps: 2                             # e.g. fast screen recording / live coding
    tags: [mba, finance]
    files: [lecture.mp4, recorder.wav]  # explicit membership (otherwise: all media in the folder)
    parts: [{name: Part A, audio: a.m4a}, {name: Part B, audio: b.m4a}]  # one lecture recorded in parts: audio joined in order
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..ingestors.base import AUDIO_EXT, SUBTITLE_EXT, VIDEO_EXT
from .av import extract_wav, load_wav, probe


@dataclass
class Session:
    title: str
    folder: Path
    video: Path | None
    audio_sources: list[Path]          # every file carrying audio (videos included)
    primary_audio: Path | None
    primary_reason: str
    subtitles: list[Path] = field(default_factory=list)
    sidecar: dict = field(default_factory=dict)
    info: dict = field(default_factory=dict)   # ffprobe results per file name
    glossary: list[str] = field(default_factory=list)

    @property
    def needs_alignment(self) -> bool:
        return (self.video is not None and self.primary_audio is not None
                and self.primary_audio != self.video)


def estimate_quality(path: Path, work: Path) -> dict:
    """Rough signal-to-noise from loudness percentiles: speech peaks vs. the noise floor."""
    wav = extract_wav(path, work / f"q_{path.stem}.wav")
    x, sr = load_wav(wav)
    hop = sr // 20
    n = len(x) // hop
    if n < 20:
        return {"snr_db": 0.0, "level_db": -99.0}
    rms = np.sqrt((x[: n * hop].reshape(n, hop).astype(np.float64) ** 2).mean(axis=1)) + 1e-9
    db = 20 * np.log10(rms)
    return {"snr_db": float(np.percentile(db, 95) - np.percentile(db, 10)),
            "level_db": float(np.percentile(db, 95))}


def _glossary(sidecar: dict, folder: Path) -> list[str]:
    g = sidecar.get("glossary")
    if isinstance(g, str):
        p = (folder / g)
        if p.exists():
            return [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()
                    if ln.strip() and not ln.startswith("#")]
        return [t.strip() for t in g.split(",") if t.strip()]
    return [str(t) for t in (g or [])]


def assemble(paths: list[Path], folder: Path, sidecar: dict, work: Path) -> Session:
    media = [p for p in paths if p.suffix.lower() in AUDIO_EXT | VIDEO_EXT]
    subs = [p for p in paths if p.suffix.lower() in SUBTITLE_EXT]
    info = {p.name: probe(p) for p in media}
    videos = [p for p in media if info[p.name]["has_video"]]
    if sidecar.get("video"):
        video = folder / sidecar["video"]
    else:
        video = max(videos, key=lambda p: info[p.name].get("width", 0) * info[p.name]["duration"]) if videos else None
    audio_sources = [p for p in media if info[p.name]["has_audio"]]

    if sidecar.get("primary_audio"):
        primary, reason = folder / sidecar["primary_audio"], "marked in session.yaml"
    elif len(audio_sources) == 1:
        primary, reason = audio_sources[0], "only audio source"
    elif audio_sources:
        q = {p: estimate_quality(p, work) for p in audio_sources}
        primary = max(q, key=lambda p: (q[p]["snr_db"], q[p]["level_db"]))
        reason = "highest estimated SNR: " + ", ".join(f"{p.name} {q[p]['snr_db']:.1f} dB" for p in q)
    else:
        primary, reason = None, "no audio"

    if sidecar.get("title"):
        title = str(sidecar["title"])
    elif len(media) > 1 or sidecar:
        title = folder.name.replace("_", " ").replace("-", " ").strip()
    else:
        title = media[0].stem.replace("_", " ")
    return Session(title=title, folder=folder, video=video, audio_sources=audio_sources,
                   primary_audio=primary, primary_reason=reason, subtitles=subs, sidecar=sidecar,
                   info=info, glossary=_glossary(sidecar, folder))


def join_parts(sess: Session, parts: list, work: Path) -> list[dict]:
    """A lecture recorded in parts (session.yaml `parts:`): join their audio, in order, into one
    recording that becomes the session's primary audio. Each part is a file name or
    {name: Part A, audio: file}. Returns [{name, start_s, end_s}] on the joined timeline.
    The video, if any, is left out: a lecture in parts is read from its audio and its deck."""
    import hashlib
    from .av import probe, require, run
    items = []
    for i, p in enumerate(parts):
        d = {"audio": p} if isinstance(p, str) else dict(p)
        f = sess.folder / d["audio"]
        if not f.exists():
            raise FileNotFoundError(f"session.yaml part {d['audio']} not found in {sess.folder}")
        items.append((str(d.get("name") or f"Part {chr(65 + i)}"), f))
    fp = hashlib.sha256(json.dumps([(n, f.name, f.stat().st_size) for n, f in items]).encode()).hexdigest()[:10]
    out = work / f"parts-{fp}.flac"
    if not out.exists():
        cmd = [require("ffmpeg"), "-v", "error", "-y"]
        for _, f in items:
            cmd += ["-i", str(f)]
        chain = "".join(f"[{i}:a]" for i in range(len(items)))
        cmd += ["-filter_complex", f"{chain}concat=n={len(items)}:v=0:a=1,aformat=sample_rates=16000:channel_layouts=mono",
                "-c:a", "flac", str(out.with_suffix(".tmp.flac"))]
        run(cmd)
        out.with_suffix(".tmp.flac").rename(out)
    bounds, t = [], 0.0
    for name, f in items:
        d = sess.info.get(f.name, {}).get("duration") or probe(f)["duration"]
        bounds.append({"name": name, "file": f.name, "start_s": round(t, 2), "end_s": round(t + d, 2)})
        t += d
    sess.info[out.name] = probe(out)
    sess.video = None
    sess.primary_audio = out
    sess.audio_sources = [out]
    sess.primary_reason = "parts joined in order: " + ", ".join(f"{b['name']} ({b['file']})" for b in bounds)
    return bounds
