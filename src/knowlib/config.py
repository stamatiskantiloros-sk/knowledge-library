"""Configuration: defaults per stage, the library's config.yaml, and per-session overrides.

Precedence (later wins): built-in defaults -> library config.yaml -> session.yaml (sidecar)
-> CLI overrides. Ingestors never hard-code a backend or model; they read it from here.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "library": {
        "inbox": "inbox",
        "sources": "sources",
        "notes": "notes",
        "work": ".work",          # stage caches, extracted frames; safe to delete
        "ignore": [],             # glob patterns (file or folder names) the router skips
        "layout": None,           # e.g. "{course}/{date}/{name}": links in organized/ to each source
    },
    # Global privacy switch. A session can also set local_only: true in its session.yaml.
    "local_only": False,
    "transcription": {
        "backend": "mlx-whisper",            # mlx-whisper | whisper-cpp | faster-whisper | subtitles | mock
        "model": "mlx-community/whisper-large-v3-turbo",
        "whisper_cpp_model": "~/models/ggml-large-v3-turbo.bin",
        "language": "el",                    # default when a session does not say: el | en | mixed
        "mixed_language_as": "el",           # Whisper needs one language; English terms go in the glossary prompt
        "word_timestamps": True,
        "glossary_prompt_chars": 800,        # Whisper's initial prompt is ~224 tokens max
        "prefer_subtitles": False,           # use a provided .srt/.vtt instead of transcribing
    },
    "alignment": {
        "envelope_hz": 100,                  # feature rate for loudness/onset envelopes
        "window_s": 30,                      # local correlation window
        "step_s": 30,                        # one alignment point every step_s seconds
        "search_s": 8,                       # +/- search around the coarse offset
        "min_confidence": 0.2,               # normalized correlation peak to accept a point
        "jump_threshold_s": 0.5,             # offset change that marks a pause / new segment
        "min_drift_span_s": 300,             # fit clock drift only over segments at least this long
        "mux_better_audio": False,           # write an archive copy of the video with the primary audio
    },
    "video": {
        "fps": 1,
        "extract_resolution": "native",      # never downscaled
        "frame_format": "jpg",               # jpg (quality 2) or png (lossless, much larger)
        "dedup": "phash",
        "phash_size": 16,                    # 256-bit hash: sensitive to small changes
        "phash_threshold": 6,                # hamming distance that starts a new group
        "ssim_check": True,                  # backup check at higher resolution
        "ssim_width": 640,
        "ssim_threshold": 0.985,
        "min_group_s": 2,                    # shorter groups are transitions, merged into neighbours
        "camera_change_ratio": 0.5,          # share of frames that differ from the last: above it = camera footage
        "talking_head_interval_s": 60,       # sparse interval frames in that case
        "frame_store": "native",
        "selection_input": "native",
        "vlm_max_native_px": 2048,           # frames up to this long edge go to the VLM untouched; larger ones are tiled
        "tile_px": 1344,
        "tile_overlap": 0.12,
        "ocr_input": "native",
        "model_view_max_px": 1568,           # copy the frontier model reads first
        "keep_lowres_archive": True,
        "archive_px": 480,
        "samples_per_candidate": 3,
        "transcript_context_s": 10,          # extra transcript after a candidate, for "as you can see..." cues
    },
    "frame_selection": {
        "backend": "mlx-vlm",                # mlx-vlm | openai (llama-server, LM Studio) | ollama | api | mock
        "model": "mlx-community/Qwen3-VL-8B-Instruct-4bit",
        "base_url": "http://localhost:8080/v1",   # backend: openai
        "openai_model": "local",
        "ollama_model": "qwen3-vl:8b",
        "ollama_url": "http://localhost:11434",
        "api_model": "anthropic/claude-sonnet-4-5",   # any LiteLLM model string; used only when backend: api
        "max_tokens": 1200,
        "temperature": 0.0,
        "split": False,                      # separate requests to decide, transcribe and caption
        "transcribe_model": None,            # with split: overrides for the model that reads the frames
        "caption_model": None,               # with split: overrides for a slower model, e.g. {openai_model: ...}
        "caption_model_types": ["table", "chart"],   # it captions these, Markdown tables and flagged slides
    },
    "handbook": {                        # overrides of frame_selection for the writing model
        "language": "English",
        "max_tokens": 6000,
    },
    "ocr_crosscheck": {
        "backends": ["apple-vision"],        # apple-vision | tesseract ; add tesseract as a third opinion
        "languages": ["el-GR", "en-US"],
        "tesseract_langs": "ell+eng",
        "similarity_threshold": 0.75,
    },
    "pdf": {
        "backend": "docling",                # docling | marker | pymupdf
        "fallbacks": ["pymupdf"],
        "scan_backend": "tesseract",         # tesseract | vision_llm | none  (pages without a text layer)
        "render_figure_pages": True,
        "render_dpi": 200,
        "min_text_chars": 25,
    },
    "office": {
        "backend": "docling",                # docling | pandoc
        "fallbacks": ["pandoc", "python-pptx"],
        "render_slides": True,               # .pptx -> PDF -> slide images, needs LibreOffice
    },
    "web": {
        "backend": "trafilatura",            # trafilatura | jina
        "fallbacks": [],                     # e.g. [jina]; jina sends the URL to a third-party service
        "timeout_s": 30,
    },
    "provider": {
        "vision": None,                      # used by pdf scan_backend: vision_llm; defaults to frame_selection
    },
}


def deep_merge(base: dict, override: dict | None) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(library_root: Path, overrides: dict | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    path = Path(library_root) / "config.yaml"
    if path.exists():
        cfg = deep_merge(cfg, yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    return deep_merge(cfg, overrides)


# Keys a session.yaml may set directly, mapped onto config sections.
SESSION_KEYS = {
    "language": ("transcription", "language"),
    "fps": ("video", "fps"),
    "local_only": (None, "local_only"),
}


def session_config(cfg: dict, sidecar: dict | None) -> dict:
    """Apply a session sidecar on top of the library config.

    A sidecar may use the shortcut keys above or full config sections (e.g. a
    `frame_selection:` block) for anything else.
    """
    sidecar = sidecar or {}
    out = copy.deepcopy(cfg)
    for key, (section, field) in SESSION_KEYS.items():
        if key in sidecar:
            if section is None:
                out[field] = sidecar[key]
            else:
                out[section][field] = sidecar[key]
    for section in DEFAULTS:
        if isinstance(sidecar.get(section), dict):
            out[section] = deep_merge(out[section], sidecar[section])
    return out
