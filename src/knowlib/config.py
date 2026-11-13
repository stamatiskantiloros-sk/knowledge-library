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
        "mode": "separate",       # separate: inbox/ and sources/<id>/; inplace: the folder as it is (see layout.py)
        "data": "_knowlib",       # inplace: images, caches, manifest and this config, in one visible folder
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
        "glossary_every_window": True,       # mlx-whisper: keep the glossary prompt on every 30 s window
        "hallucination_silence_s": 2.0,      # mlx-whisper: skip silences this long, where loops start
        "start_s": None,                     # per session: transcribe from here (skip the room before class)
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
        "caption_model_types": ["table", "chart"],   # it captions these, Markdown tables and flagged slides; or all
        "memory_check": True,                # with manage_lms: stop if a model will not fit in free memory
    },
    "handbook": {                        # overrides of frame_selection for the writing model
        "language": "English",
        "style_file": None,                  # a CSS file (relative to the library) for the PDF; None: the plain default
        "speaker": "the lecturer",           # what the book calls the person teaching ("the professor", ...)
        "byline": None,                      # one line under the title, e.g. who made it
        "course": None,                      # course name under the title and in the PDF's name (default: the library folder)
        "subtitle": "Lecture notes",         # the line under the title: "<subtitle> · <course> · <date>"; false for none
        "last_page_signature": None,         # once, small, in the bottom right corner of the last page;
                                             # a list for several lines (the last beside the page number)
        "last_page_signature_size": 7.2,     # its size in pt
        "signature": None,                   # a line at the foot of every page and at the end
        "tagline": None,                     # a line printed at the end
        "show_times": True,                  # recording times in slide captions and section endings
        "theme": None,                       # the look, set by `knowlib style`: palette, main, accent, tint,
                                             # body_font, heading_font, headings, table_header, boxes, box_fill,
                                             # line_weight. It wins over style_file and drives the figures.
        "max_tokens": 6000,
        "figures": {                         # `knowlib figures`: slides redrawn in the handbook's style
            "writer": {"backend": "chain", "max_tokens": 8000, "reasoning_effort": "low",     # first drawing
                       "chain": ["google/gemini-3.5-flash-lite", "google/gemini-3.1-flash-lite"]},
            "reviser": {"backend": "chain", "max_tokens": 8000, "reasoning_effort": "low",    # revisions
                        "chain": ["google/gemini-3.5-flash", "google/gemini-3.8-flash", "google/gemini-3.5-flash-lite"]},
            "checker": {"backend": "chain", "max_tokens": 4000, "reasoning_effort": "low",    # compares with the slide
                        "chain": ["google/gemini-3.5-flash", "google/gemini-3.8-flash", "google/gemini-3.5-flash-lite"]},
            "revisions": 2,                      # rounds after the label check and the comparison
            "compile_retries": 3,                # a render error goes back to the model this often
            "font": "Georgia", "main": "#5a0f1b", "accent": "#3a3a3a", "tint": "#f3eff0", "text": "#1a1a1a",
            "column_mm": 85,                     # printed width of one column
        },
    },
    "digest": {                          # book finding aids; overrides of frame_selection for the reading model
        "min_pages": 100,                    # PDFs this long get a digest
        "max_chars": 120000,                 # one request per chapter, split when longer (~30K tokens)
        "max_tokens": 8000,
        "retries": 1,                        # a part cut off at the token limit is asked again with twice the tokens
        "fallback_model": None,              # overrides for a stronger model that rereads a part the first one looped on
        "fallback_max_chars": 60000,         # it reads such a part in pieces this long
    },
    "notes_review": {                    # a second model checks each note's claims against the cited passages.
        # Overrides on top of frame_selection and its caption_model, so by default the strong local
        # model reviews. Set openai_model (and context_length, reasoning_effort) for another local
        # model, or backend: agent with agent_model: sonnet for a session subagent. Pick a model
        # unlike the writer's, so they do not share blind spots.
        "max_tokens": 4000,
        "max_passage_chars": 8000,           # each cited passage is cut to this length
        "retries": 1,                        # a reply cut off at the token limit is asked again with twice the tokens
        # Notes per request for a chain. 1 (the default) asks about each note on its own: in a test with
        # planted errors, Gemini 3.5 Flash-Lite caught 12 of 16 one note at a time, 9 three at a time
        # and 5 six at a time. Raise it only to stretch a small daily quota.
        "batch_notes": 1,
    },
    "providers": {
        # API providers that a role's chain can use (backend: chain, chain: [<provider>/<model>, ...]).
        # A provider may hold more keys than its main one: keys: [{env: VAR, file: ~/.file}, "VAR2", ...].
        # Each key present becomes its own chain member (<provider>/<model>#2, ...) with its own quota,
        # tried before the next model; a slot whose key is missing is skipped. Quotas usually belong to an
        # account or project, not a key: a second key of the same account adds nothing.
        # Limits are per model: rpm requests and tpm input tokens per minute, rpd requests per day,
        # context the model's window. Free-tier numbers change; a 429 reply teaches the real daily
        # limit, and `knowlib models` shows what is left today.
        "google": {
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "api_key_env": "GOOGLE_API_KEY",
            "api_key_file": "~/.google_key",
            "reset_tz": "America/Los_Angeles",   # free-tier days end at midnight Pacific
            "trains_on_data": True,              # the free tier may use what it is sent to improve Google's products
            "timeout_s": 600,
            # Gemini gives every image about 1,100 tokens whatever its size (fixed_image_tokens), so the
            # image-detail check, meant for servers that cap tokens, is skipped for it.
            "models": {
                "gemini-3.8-flash": {"rpm": 5, "tpm": 250000, "rpd": 20, "context": 1000000, "fixed_image_tokens": True},
                "gemini-3.5-flash": {"rpm": 5, "tpm": 250000, "rpd": 20, "context": 1000000, "fixed_image_tokens": True},
                "gemini-3.5-flash-lite": {"rpm": 15, "tpm": 250000, "rpd": 500, "context": 1000000, "fixed_image_tokens": True},
                "gemini-3.1-flash-lite": {"rpm": 15, "tpm": 250000, "rpd": 500, "context": 1000000, "fixed_image_tokens": True},
                # Gemma thinks on its own (no reasoning_effort) and sees an image at ~260 tokens, a
                # quarter of Gemini's detail: fine for text, weak for dense figures.
                "gemma-4-31b-it": {"rpm": 30, "tpm": 15000, "rpd": 1500, "context": 128000,
                                   "reasoning_effort": None},
                "gemma-4-26b-a4b-it": {"rpm": 30, "tpm": 15000, "rpd": 1500, "context": 128000,
                                       "reasoning_effort": None},
            },
        },
        "openrouter": {
            # openrouter.ai: one key for models of many vendors, paid per token (a few are free). Name a
            # model by OpenRouter's id after the provider: chain: [openrouter/<vendor>/<model>, ...].
            # data_collection: deny routes only to hosts that neither store nor train on prompts.
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_env": "OPENROUTER_API_KEY",
            "api_key_file": "~/.openrouter_key",
            # With deny, a model whose only host trains on prompts fails with 404 ("No endpoints found
            # matching your data policy") instead of being sent anyway; in November 2026 that ruled out
            # NVIDIA's free endpoints. Free (":free") models share 20 requests a minute and 50 a day per
            # account (1,000 a day once 10 credits have been bought): the rpd below is the account's.
            "provider_routing": {"data_collection": "deny"},
            "timeout_s": 600,
            "models": {
                # Read 8 dense lecture slides (charts, a 12-person board, a bond chart) with no wrong value.
                "dots-studio/dots-3-note-preview:free": {"rpm": 20, "rpd": 50, "context": 512000},
                # Fast, 1M context, text only. As a reviewer it caught a dropped hedge but missed a dropped
                # scope ("in general efficient"): use it for leads, not verdicts.
                "baidu/ernie-5.5-flash-preview:free": {"rpm": 20, "rpd": 50, "context": 1024000},
                # These two are hosted only where prompts may be kept: they need privacy.models_may_keep_data.
                # The VL model read the same 8 slides with every number right, 3x faster than dots, but
                # slipped on two words (a name, a logo). Ultra, as a reviewer, caught the dropped hedge only.
                "nvidia/nemotron-3.5-super-vl:free": {"rpm": 20, "rpd": 50, "context": 1000000},
                "nvidia/nemotron-3-ultra-550b-a55b:free": {"rpm": 20, "rpd": 50, "context": 1000000},
            },
        },
        "nvidia": {
            # build.nvidia.com: free for development, rate-limited (about 40 requests a minute), no daily
            # quota published. Requests queue: a minute or more is normal, and a request still running
            # after about 300 s is cut off by NVIDIA's gateway, so keep requests to one figure or note.
            "base_url": "https://integrate.api.nvidia.com/v1",
            "api_key_env": "NVIDIA_API_KEY",
            "api_key_file": "~/.nvidia_key",
            "timeout_s": 320,
            "models": {
                "nvidia/nemotron-3.5-super-vl-120b-a12b": {"rpm": 40, "context": 128000},
                "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning": {"rpm": 40, "context": 128000},
            },
        },
    },
    "privacy": {
        # Names to keep off every model outside this machine (also settable per lecture in session.yaml).
        "names": [],
        # Every prompt to a remote model has emails, phone numbers and these names replaced, and every
        # image is read by local OCR first; one showing personal information is not sent.
        "check_remote": True,
        # With the check above, a library may allow hosts that store or train on what they are sent
        # (OpenRouter's data_collection: allow). local_only material never leaves the machine either way.
        "models_may_keep_data": False,
    },
    "ocr_crosscheck": {
        "backends": ["apple-vision"],        # apple-vision | tesseract ; add tesseract as a third opinion
        "languages": ["el-GR", "en-US"],
        "tesseract_langs": "ell+eng",
        "similarity_threshold": 0.75,
    },
    "images": {
        # loose images (screenshots, photos): one source per folder, one section per image
        "describe": True,                    # the vision model reads each image; false = classic OCR only
        "model": None,                       # a provider section; defaults to frame_selection
        "max_px": 1568,                      # the copy kept in assets and read by the model
    },
    "pdf": {
        "backend": "docling",                # docling | marker | pymupdf
        "fallbacks": ["pymupdf"],
        "scan_backend": "tesseract",         # tesseract | vision_llm | none  (pages without a text layer)
        "render_figure_pages": True,
        "render_dpi": 200,
        "min_text_chars": 25,
        "figures": {                         # pictures inside PDFs (Docling only), read by the vision model
            "read": True,
            "model": None,                   # a provider section; defaults to images.model, then the strong model (caption_model)
            "second_model": None,            # a provider section of another family: rereads OCR-flagged figures and
                                             # tables and reports where the two readings differ (knowlib check-figures)
            "max_tokens": 8000,              # a big table, plus the strong model's thinking; doubled on each retry
            "retries": 2,                    # ask again after an empty or cut-off reply, with twice the tokens each time
            "detail_check": "stop",          # stop | warn | off: before reading, check the server does not cap image tokens
            "dpi": 200,
            "min_side_pt": 60,               # smaller boxes are icons and bullets
            "min_page_share": 0.02,          # so are boxes under 2% of the page
            "repeat_pages": 3,               # the same picture on this many pages is a logo or header
        },
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


def config_path(library_root: Path) -> Path:
    """_knowlib/config.yaml for a library in place, config.yaml at the top otherwise."""
    inplace = Path(library_root) / "_knowlib" / "config.yaml"
    return inplace if inplace.exists() else Path(library_root) / "config.yaml"


def load_config(library_root: Path, overrides: dict | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    path = config_path(library_root)
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
