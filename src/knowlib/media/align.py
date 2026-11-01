"""Audio alignment: map the primary (best) recording's timeline onto the video timeline.

Method
  1. Both signals -> high-passed log-loudness envelopes at 100 Hz. Envelopes, not raw
     waveforms, so a louder/cleaner recorder still matches a distant camera mic.
  2. Coarse offset: FFT cross-correlation over the full length.
  3. Local points: every `step_s`, a `window_s` slice of the primary is matched against the
     video envelope within +/- `search_s` of the coarse offset, by normalized cross-correlation.
  4. Points are split into segments where the offset jumps (recorder paused / restarted), and
     each segment is fitted with offset + clock drift (robust linear fit).
Result: a piecewise-linear map t_video = t_primary + b + drift * t_primary.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy.signal import correlate

from .av import extract_wav, load_wav, require, run


def loudness_envelope(x: np.ndarray, sr: int, hz: int = 100, highpass_s: float = 0.5) -> np.ndarray:
    """Log loudness with its slow trend removed. The log turns a gain difference between
    devices into a constant, and the high-pass removes it along with noise-floor drift, so a
    quiet camera mic and a close recorder produce comparable curves."""
    from scipy.ndimage import uniform_filter1d
    hop = sr // hz
    n = len(x) // hop
    if n < 2:
        return np.zeros(max(n, 1), dtype=np.float32)
    frames = x[: n * hop].reshape(n, hop).astype(np.float64)
    loud = 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-4)
    loud = loud - uniform_filter1d(loud, max(3, int(highpass_s * hz)))
    return ((loud - loud.mean()) / (loud.std() + 1e-9)).astype(np.float32)


def ncc(signal: np.ndarray, template: np.ndarray) -> np.ndarray:
    """Normalized cross-correlation of template at every offset in signal (valid mode)."""
    w = len(template)
    t = template - template.mean()
    tn = np.linalg.norm(t)
    if tn < 1e-9 or len(signal) < w:
        return np.zeros(max(len(signal) - w + 1, 1))
    num = correlate(signal, t, mode="valid", method="fft")
    cs = np.cumsum(np.concatenate([[0.0], signal.astype(np.float64)]))
    cs2 = np.cumsum(np.concatenate([[0.0], signal.astype(np.float64) ** 2]))
    s = cs[w:] - cs[:-w]
    s2 = cs2[w:] - cs2[:-w]
    var = np.maximum(s2 - s * s / w, 1e-12)
    return num / (np.sqrt(var) * tn)


def _parabolic(y: np.ndarray, i: int) -> float:
    if 0 < i < len(y) - 1:
        a, b, c = y[i - 1], y[i], y[i + 1]
        d = a - 2 * b + c
        if abs(d) > 1e-12:
            return i + 0.5 * (a - c) / d
    return float(i)


def coarse_offset(env_p: np.ndarray, env_v: np.ndarray, hz: int) -> tuple[float, float]:
    c = correlate(env_v, env_p, mode="full", method="fft")
    k = int(np.argmax(c))
    lag = k - (len(env_p) - 1)
    peak = c[k] / (np.linalg.norm(env_p) * np.linalg.norm(env_v) + 1e-9)
    return lag / hz, float(peak)


@dataclass
class Segment:
    start: float          # primary-time range this segment covers
    end: float
    offset: float         # b
    drift: float          # seconds per second
    points: int

    def map(self, t: float) -> float:
        return t + self.offset + self.drift * t


@dataclass
class AlignmentMap:
    segments: list[Segment] = field(default_factory=list)
    identity: bool = False
    quality: dict = field(default_factory=dict)

    def map(self, t: float) -> float:
        if self.identity or not self.segments:
            return t
        for s in self.segments:
            if t < s.end:
                return s.map(t)
        return self.segments[-1].map(t)

    def to_dict(self) -> dict:
        return {"identity": self.identity, "quality": self.quality,
                "segments": [s.__dict__ for s in self.segments]}

    @classmethod
    def from_dict(cls, d: dict) -> "AlignmentMap":
        return cls([Segment(**s) for s in d.get("segments", [])], d.get("identity", False),
                   d.get("quality", {}))


def _fit(ts: np.ndarray, offs: np.ndarray) -> tuple[float, float, np.ndarray]:
    keep = np.ones(len(ts), bool)
    if len(ts) == 1:
        return float(offs[0]), 0.0, keep
    for _ in range(3):
        drift, b = np.polyfit(ts[keep], offs[keep], 1)
        resid = np.abs(offs - (b + drift * ts))
        new_keep = resid < max(0.08, 3 * np.median(resid[keep]))
        if new_keep.sum() < 2 or (new_keep == keep).all():
            break
        keep = new_keep
    drift, b = np.polyfit(ts[keep], offs[keep], 1)
    return float(b), float(drift), keep


def align(primary: Path, video_audio: Path, work: Path, cfg: dict) -> AlignmentMap:
    ac = cfg["alignment"]
    hz = int(ac.get("envelope_hz", 100))
    xp, sr = load_wav(extract_wav(primary, work / "primary.wav"))
    xv, srv = load_wav(extract_wav(video_audio, work / "video_audio.wav"))
    ep, ev = loudness_envelope(xp, sr, hz), loudness_envelope(xv, srv, hz)
    coarse, coarse_peak = coarse_offset(ep, ev, hz)

    win, step, search = int(ac["window_s"] * hz), int(ac["step_s"] * hz), int(ac["search_s"] * hz)
    min_conf, jump = float(ac["min_confidence"]), float(ac["jump_threshold_s"])
    pts: list[tuple[float, float, float]] = []   # (t_primary_center, offset, confidence)
    total = 0
    track = coarse                                   # follows the last accepted offset
    for start in range(0, max(len(ep) - win, 1), step):
        tmpl = ep[start:start + win]
        if len(tmpl) < win or tmpl.std() < 0.2:
            continue
        total += 1
        best = None
        # local search around the tracked offset first; on a miss (e.g. after a recorder
        # pause) search the whole video, so a new segment can be picked up
        centre = start + int(round(track * hz))
        for lo, hi in ((max(0, centre - search), min(len(ev), centre + win + search)), (0, len(ev))):
            if hi - lo < win:
                continue
            cc = ncc(ev[lo:hi], tmpl)
            k = int(np.argmax(cc))
            if cc[k] >= min_conf:
                best = (lo + _parabolic(cc, k), float(cc[k]))
                break
        if best is None:
            continue
        off = (best[0] - start) / hz
        pts.append(((start + win / 2) / hz, off, best[1]))
        track = off

    dur_p = len(xp) / sr
    if not pts:
        amap = AlignmentMap([Segment(0.0, dur_p, coarse, 0.0, 0)],
                            quality={"coarse_offset_s": round(coarse, 3), "coarse_peak": round(coarse_peak, 3),
                                     "points": 0, "windows": total, "warning": "no confident local points; coarse offset only"})
        return amap

    # split into segments at offset jumps confirmed by the next point (isolated outliers are dropped)
    groups: list[list[tuple]] = [[pts[0]]]
    i = 1
    while i < len(pts):
        p = pts[i]
        cur = groups[-1]
        if abs(p[1] - cur[-1][1]) <= jump:
            cur.append(p)
        elif i + 1 < len(pts) and abs(pts[i + 1][1] - p[1]) <= jump / 2:
            groups.append([p])
        i += 1

    segments = []
    for g in groups:
        ts = np.array([p[0] for p in g])
        offs = np.array([p[1] for p in g])
        if ts[-1] - ts[0] >= float(ac.get("min_drift_span_s", 300)):
            b, drift, _ = _fit(ts, offs)
        else:
            b, drift = float(np.median(offs)), 0.0
        segments.append(Segment(float(ts[0]), float(ts[-1]), b, drift, len(g)))
    for a, b_ in zip(segments, segments[1:]):     # boundaries halfway between point groups
        mid = (a.end + b_.start) / 2
        a.end, b_.start = mid, mid
    segments[0].start, segments[-1].end = 0.0, dur_p

    confs = [p[2] for p in pts]
    q = {"coarse_offset_s": round(coarse, 3), "points": len(pts), "windows": total,
         "mean_confidence": round(float(np.mean(confs)), 3),
         "drift_ms_per_hour": [round(s.drift * 3600 * 1000, 1) for s in segments],
         "segments": len(segments)}
    if total and len(pts) / total < 0.5:
        q["warning"] = "fewer than half of the windows aligned confidently"
    return AlignmentMap(segments, quality=q)


def mux_primary_audio(video: Path, primary: Path, amap: AlignmentMap, dest: Path) -> Path | None:
    """Archive copy of the video carrying the better audio. Single-segment maps only: with a
    paused recorder there is no single continuous track to lay under the video."""
    if amap.identity or len(amap.segments) != 1:
        return None
    s = amap.segments[0]
    tempo = 1.0 / (1.0 + s.drift)          # stretch primary to video clock
    filt = f"atempo={tempo:.8f}"
    if s.offset >= 0:
        filt += f",adelay={int(s.offset * 1000)}:all=1"
    else:
        filt = f"atrim=start={-s.offset:.3f},asetpts=PTS-STARTPTS," + filt
    run([require("ffmpeg"), "-nostdin", "-y", "-v", "error", "-i", str(video), "-i", str(primary),
         "-map", "0:v:0", "-map", "1:a:0", "-af", filt, "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
         "-shortest", str(dest)])
    return dest


def save(amap: AlignmentMap, path: Path) -> None:
    path.write_text(json.dumps(amap.to_dict(), indent=2))
