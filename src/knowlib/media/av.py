"""ffmpeg/ffprobe helpers shared by the media stages."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np


def require(tool: str) -> str:
    p = shutil.which(tool)
    if not p:
        raise RuntimeError(f"{tool} not found; on the Mac: brew install ffmpeg")
    return p


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {r.stderr.strip()[-800:]}")
    return r


def probe(path: Path) -> dict:
    r = run([require("ffprobe"), "-v", "error", "-print_format", "json", "-show_format",
             "-show_streams", str(path)])
    data = json.loads(r.stdout)
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"
              and s.get("disposition", {}).get("attached_pic") != 1), None)
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    fmt = data.get("format", {})
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    out = {"duration": float(fmt.get("duration") or 0), "has_video": v is not None,
           "has_audio": a is not None, "creation_time": tags.get("creation_time")}
    if v:
        w, h = int(v.get("width", 0)), int(v.get("height", 0))
        rot = 0
        for sd in v.get("side_data_list", []) or []:
            rot = abs(int(sd.get("rotation", 0) or 0))
        if rot in (90, 270):
            w, h = h, w
        out.update(width=w, height=h)
    return out


def extract_wav(src: Path, dest: Path, sr: int = 16000) -> Path:
    """Mono 16 kHz PCM: what Whisper wants, and plenty for alignment envelopes."""
    if dest.exists() and dest.stat().st_mtime >= src.stat().st_mtime:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp.wav")
    run([require("ffmpeg"), "-nostdin", "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1",
         "-ar", str(sr), "-c:a", "pcm_s16le", str(tmp)])
    tmp.rename(dest)
    return dest


def load_wav(path: Path) -> tuple[np.ndarray, int]:
    import wave
    with wave.open(str(path), "rb") as wf:
        sr, ch = wf.getframerate(), wf.getnchannels()
        data = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    return data, sr
