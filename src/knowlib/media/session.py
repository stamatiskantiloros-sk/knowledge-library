"""Session assembly: one lecture = one session, possibly several files.

session.yaml (all keys optional):

    title: Corporate Finance - Lecture 4
    date: 2026-10-28
    language: el            # el | en | mixed
    glossary: [WACC, ΔΕΣΦΑ, CAPM]      # or a path to a text file, one term per line
    primary_audio: zoom_h1.wav         # the best recording; otherwise picked by SNR
    video: lecture.mp4                 # if the folder has several videos
    slides: slides.pdf                 # the deck, ingested alongside and linked
    local_only: true                   # block all API calls for this session
    fps: 2                             # e.g. fast screen recording / live coding
    tags: [mba, finance]
    files: [lecture.mp4, zoom_h1.wav]  # explicit membership (otherwise: all media in the folder)
"""
from __future__ import annotations

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

    @property
    def is_video(self) -> bool:
        return self.video is not None


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
