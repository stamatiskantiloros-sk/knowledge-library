"""Session assembly: one lecture = one session, possibly several files.

session.yaml (all keys optional):

    title: Corporate Finance - Lecture 4
    date: 2026-10-28
    language: el            # el | en | mixed
    glossary: [WACC, ΔΕΣΦΑ, CAPM]      # or a path to a text file, one term per line
    primary_audio: recorder.wav         # the best recording; otherwise picked by SNR
    video: lecture.mp4                 # if the folder has several videos
    slides: slides.pdf                 # the deck, ingested alongside and linked
    labels: {Assignment-2026.pdf: Productivity assignment}  # how handbooks name the folder's documents
    local_only: true                   # block all API calls for this session
    fps: 2                             # e.g. fast screen recording / live coding
    tags: [mba, finance]
    files: [lecture.mp4, recorder.wav]  # explicit membership (otherwise: all media in the folder)
    parts: [{name: Part A, audio: a.m4a}, {name: Part B, audio: b.m4a}]  # one lecture recorded in parts: audio joined in order
           # a part may name its screen recording too: {name: Part A, audio: a.m4a, video: screen-a.mov}
"""
from __future__ import annotations

import json
import re
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
    part_videos: list[dict] = field(default_factory=list)   # [{video, shift_s, offset_s, match}] for parts with a video

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
        video = find_file(folder, sidecar["video"]) or folder / sidecar["video"]
    else:
        video = max(videos, key=lambda p: info[p.name].get("width", 0) * info[p.name]["duration"]) if videos else None
    audio_sources = [p for p in media if info[p.name]["has_audio"]]

    if sidecar.get("primary_audio"):
        primary, reason = find_file(folder, sidecar["primary_audio"]) or folder / sidecar["primary_audio"], "marked in session.yaml"
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


def video_offset(audio: Path, video: Path, work: Path, max_lag_s: float | None = None, rate: int = 10) -> tuple[float, float]:
    """How far the video's timeline is behind the audio's: audio time = video time + offset. Found by
    matching the loudness envelopes of the audio and the video's own sound track (a screen recording
    picks up the room faintly). Returns (offset_s, match), match being the correlation at the best lag
    (around 0.3 or more is a clear match; below 0.15, treat the offset as unknown). Every lag that
    leaves a minute of overlap is tried unless max_lag_s narrows it: a phone may record half an hour
    before the screen recording starts."""
    def envelope(path: Path, name: str) -> np.ndarray:
        x, sr = load_wav(extract_wav(path, work / name))
        hop = sr // rate
        n = len(x) // hop
        rms = np.sqrt((x[: n * hop].reshape(n, hop).astype(np.float64) ** 2).mean(axis=1)) + 1e-6
        e = 20 * np.log10(rms)
        return (e - e.mean()) / (e.std() + 1e-9)
    a = envelope(audio, f"off_a_{audio.stem}.wav")
    v = envelope(video, f"off_v_{video.stem}.wav")
    best, best_r = 0, -1.0
    lo, hi = -len(v), len(a)
    if max_lag_s is not None:
        lo, hi = max(lo, -int(max_lag_s * rate)), min(hi, int(max_lag_s * rate))
    for lag in range(lo, hi + 1):
        va, aa = (v[-lag:], a) if lag < 0 else (v, a[lag:])
        n = min(len(va), len(aa))
        if n < 60 * rate:
            continue
        r = float(np.dot(va[:n] - va[:n].mean(), aa[:n] - aa[:n].mean()) / (n * va[:n].std() * aa[:n].std() + 1e-9))
        if r > best_r:
            best, best_r = lag, r
    return best / rate, round(best_r, 3)


def _name_key(name: str) -> str:
    import unicodedata
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", name)).strip().casefold()


def find_file(folder: Path, name: str) -> Path | None:
    """A file named in session.yaml: the exact name, else the one whose name differs only in spaces,
    Unicode form or case. macOS names screen recordings "... at 6.06.38 PM.mov" with a narrow
    no-break space that a name typed by hand never has."""
    f = folder / name
    if f.exists():
        return f
    key = _name_key(name)
    hits = [p for p in folder.iterdir() if _name_key(p.name) == key]
    return hits[0] if len(hits) == 1 else None


def join_parts(sess: Session, parts: list, work: Path) -> list[dict]:
    """A lecture recorded in parts (session.yaml `parts:`): join their audio, in order, into one
    recording that becomes the session's primary audio. Each part is a file name or
    {name: Part A, audio: file, video: file}. Returns [{name, start_s, end_s}] on the joined timeline.
    A part's video (e.g. a screen recording of the same hour) is placed on the joined timeline by
    matching its sound to the part's audio (video_offset); its frames are then read as the session's
    video. A video not named in a part is left out."""
    import hashlib
    from .av import probe, require, run
    items = []
    for i, p in enumerate(parts):
        d = {"audio": p} if isinstance(p, str) else dict(p)
        f = find_file(sess.folder, d["audio"])
        if f is None:
            raise FileNotFoundError(f"session.yaml part {d['audio']} not found in {sess.folder}")
        v = find_file(sess.folder, d["video"]) if d.get("video") else None
        if d.get("video") and v is None:
            raise FileNotFoundError(f"session.yaml part video {d['video']} not found in {sess.folder}")
        items.append((str(d.get("name") or f"Part {chr(65 + i)}"), f, v))
    fp = hashlib.sha256(json.dumps([(n, f.name, f.stat().st_size) for n, f, _ in items]).encode()).hexdigest()[:10]
    out = work / f"parts-{fp}.flac"
    if not out.exists():
        cmd = [require("ffmpeg"), "-v", "error", "-y"]
        for _, f, _ in items:
            cmd += ["-i", str(f)]
        chain = "".join(f"[{i}:a]" for i in range(len(items)))
        # Written on this Mac's disk, then copied: ffmpeg rewrites a FLAC's header when it closes the file, and
        # on an SMB share that close can fail ("Bad file descriptor") after every byte was written.
        import shutil
        import tempfile
        with tempfile.TemporaryDirectory(prefix="knowlib-parts-") as tmp:
            local = Path(tmp) / out.name
            cmd += ["-filter_complex", f"{chain}concat=n={len(items)}:v=0:a=1,aformat=sample_rates=16000:channel_layouts=mono",
                    "-c:a", "flac", str(local)]
            run(cmd)
            shutil.copyfile(local, out.with_suffix(".tmp.flac"))
        out.with_suffix(".tmp.flac").rename(out)
    bounds, t = [], 0.0
    sess.part_videos = []
    for name, f, v in items:
        d = sess.info.get(f.name, {}).get("duration") or probe(f)["duration"]
        bounds.append({"name": name, "file": f.name, "start_s": round(t, 2), "end_s": round(t + d, 2)})
        if v is not None:
            cache = work / f"offset2-{hashlib.sha256(f'{f.name}|{f.stat().st_size}|{v.name}|{v.stat().st_size}'.encode()).hexdigest()[:10]}.json"
            if cache.exists():
                off, match = json.loads(cache.read_text())
            else:
                off, match = video_offset(f, v, work)
                cache.write_text(json.dumps([off, match]))
                for tmp in work.glob("off_*.wav"):
                    tmp.unlink()
            if match < 0.15:
                off = 0.0                                       # no clear match: assume both started together
            sess.part_videos.append({"video": v, "shift_s": round(t + off, 2), "offset_s": off, "match": match,
                                     "part": name, "end_s": round(t + d, 2), "start_s": round(t, 2)})
        t += d
    sess.info[out.name] = probe(out)
    sess.video = None
    sess.primary_audio = out
    sess.audio_sources = [out]
    sess.primary_reason = "parts joined in order: " + ", ".join(f"{b['name']} ({b['file']})" for b in bounds)
    return bounds
