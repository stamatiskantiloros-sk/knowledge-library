"""Frame extraction (1 fps at native resolution) and deterministic deduplication.

Dedup collapses runs of near-identical frames into candidate groups, each with a time range:
a frame starts a new group when its perceptual hash differs from the group's first frame by
more than `phash_threshold` bits (256-bit hash), or - as a backup that catches a single
changed number - when SSIM against that frame at `ssim_width` drops below `ssim_threshold`.
The comparison images are small internal copies; the frames themselves stay at native size.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from .av import require, run


@dataclass
class Frame:
    path: Path
    t: float
    sharpness: float = 0.0


@dataclass
class Candidate:
    index: int
    start: float
    end: float
    frames: list[Frame] = field(default_factory=list)
    samples: list[Frame] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"index": self.index, "start": self.start, "end": self.end,
                "frames": [str(f.path.name) for f in self.frames],
                "samples": [str(f.path.name) for f in self.samples]}


def extract_frames(video: Path, out: Path, fps: float, fmt: str = "jpg") -> list[Frame]:
    out.mkdir(parents=True, exist_ok=True)
    marker = out / "_done.json"
    if marker.exists() and json.loads(marker.read_text()).get("fps") == fps:
        files = sorted(out.glob(f"f_*.{fmt}"))
    else:
        for f in out.glob("f_*"):
            f.unlink()
        args = ["-q:v", "2"] if fmt == "jpg" else []
        run([require("ffmpeg"), "-nostdin", "-y", "-v", "error", "-i", str(video),
             "-vf", f"fps={fps}", *args, "-start_number", "0", str(out / f"f_%06d.{fmt}")])
        files = sorted(out.glob(f"f_*.{fmt}"))
        marker.write_text(json.dumps({"fps": fps, "count": len(files)}))
    return [Frame(p, i / fps) for i, p in enumerate(files)]


def _small(path: Path, width: int) -> Image.Image:
    img = Image.open(path)
    if img.format == "JPEG":
        img.draft("L", (width, max(1, int(width * img.height / img.width))))
    img = img.convert("L")
    if img.width != width:
        img = img.resize((width, max(1, int(width * img.height / img.width))), Image.BILINEAR)
    return img


def _sharpness(a: np.ndarray) -> float:
    lap = a[1:-1, 1:-1] * 4 - a[:-2, 1:-1] - a[2:, 1:-1] - a[1:-1, :-2] - a[1:-1, 2:]
    return float(lap.var())


def group_frames(frames: list[Frame], vc: dict) -> tuple[list[Candidate], dict]:
    import imagehash
    from skimage.metrics import structural_similarity as ssim

    thr = int(vc.get("phash_threshold", 6))
    size = int(vc.get("phash_size", 16))
    sw = int(vc.get("ssim_width", 640))
    sthr = float(vc.get("ssim_threshold", 0.985))
    use_ssim = bool(vc.get("ssim_check", True))
    fps = float(vc.get("fps", 1))

    groups: list[list[Frame]] = []
    anchor_hash = anchor_arr = None
    for f in frames:
        small = _small(f.path, sw)
        arr = np.asarray(small, dtype=np.float32)
        f.sharpness = _sharpness(arr)
        h = imagehash.phash(small, hash_size=size)
        new = anchor_hash is None or (h - anchor_hash) > thr
        if not new and use_ssim and arr.shape == anchor_arr.shape:
            new = ssim(anchor_arr, arr, data_range=255.0) < sthr
        if new:
            groups.append([f])
            anchor_hash, anchor_arr = h, arr
        else:
            groups[-1].append(f)

    # very short groups are transitions/fades: fold them into the following group
    min_frames = max(1, int(round(float(vc.get("min_group_s", 2)) * fps)))
    merged: list[list[Frame]] = []
    carry: list[Frame] = []
    for g in groups:
        if len(g) < min_frames:
            carry += g
            continue
        merged.append(carry + g)
        carry = []
    if carry:
        (merged[-1].extend(carry) if merged else merged.append(carry))

    duration_min = max((frames[-1].t + 1 / fps) / 60, 1e-6) if frames else 1
    change_ratio = len(groups) / max(len(frames), 1)
    stats = {"frames": len(frames), "raw_groups": len(groups), "groups": len(merged),
             "groups_per_min": round(len(merged) / duration_min, 2),
             "change_ratio": round(change_ratio, 3), "mode": "slides"}

    # Camera footage changes almost every frame; slides and screen recordings (even busy demos
    # with typing and scrolling) hold still most of the time. Only the former falls back.
    if frames and len(frames) >= 30 and change_ratio > float(vc.get("camera_change_ratio", 0.5)):
        # constant motion and no stable slides: fall back to sparse interval frames
        step = max(1, int(float(vc.get("talking_head_interval_s", 60)) * fps))
        merged = [frames[i:i + step] for i in range(0, len(frames), step)]
        stats.update(mode="interval", groups=len(merged))

    cands = []
    n_samples = int(vc.get("samples_per_candidate", 3))
    for i, g in enumerate(merged):
        picks = {0: g[0], len(g) - 1: g[-1]}
        sharp = int(np.argmax([f.sharpness for f in g]))
        picks[sharp] = g[sharp]
        samples = [picks[k] for k in sorted(picks)][-n_samples:] if n_samples >= 1 else [g[-1]]
        cands.append(Candidate(i, g[0].t, g[-1].t + 1 / fps, g, samples))
    return cands, stats


def tiles(img_path: Path, out_dir: Path, tile_px: int, overlap: float) -> list[Path]:
    """Cut a native-resolution frame into overlapping crops no larger than tile_px, so small
    text is read at full detail instead of being downscaled."""
    img = Image.open(img_path)
    w, h = img.size
    nx = max(1, int(np.ceil((w - tile_px * overlap) / (tile_px * (1 - overlap)))))
    ny = max(1, int(np.ceil((h - tile_px * overlap) / (tile_px * (1 - overlap)))))
    tw, th = int(np.ceil(w / (nx - (nx - 1) * overlap))), int(np.ceil(h / (ny - (ny - 1) * overlap)))
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for j in range(ny):
        for i in range(nx):
            x0 = int(i * tw * (1 - overlap)) if nx > 1 else 0
            y0 = int(j * th * (1 - overlap)) if ny > 1 else 0
            box = (x0, y0, min(w, x0 + tw), min(h, y0 + th))
            p = out_dir / f"{img_path.stem}_t{j}{i}.png"
            img.crop(box).save(p)
            paths.append(p)
    return paths


def resized_copy(src: Path, dest: Path, max_px: int) -> Path:
    img = Image.open(src)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if max(img.size) <= max_px:
        img.save(dest, quality=92) if dest.suffix.lower() in (".jpg", ".jpeg") else img.save(dest)
        return dest
    img = img.convert("RGB")
    img.thumbnail((max_px, max_px), Image.LANCZOS)
    img.save(dest, quality=90)
    return dest


def save_candidates(cands: list[Candidate], stats: dict, path: Path) -> None:
    path.write_text(json.dumps({"stats": stats, "candidates": [c.to_dict() for c in cands]}, indent=1))
