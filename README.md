# knowlib — Knowledge Library pipeline

Turns a folder of mixed materials (PDFs, Office files, web pages, audio, video) into a Markdown knowledge library that a frontier model can use for demanding academic and industry work. Implements the *Knowledge Library — System Design* doc.

```
inbox/  ──►  router + manifest  ──►  ingestors (pdf · office · web · media)  ──►  sources/<id>/source.md + assets/
                                                                                         │
                                                       agent (CLAUDE.md): QA ──► synthesis ──► notes/, index.md, glossary.md
```

Scripts do the mechanics (conversion, extraction, alignment, dedup); models make the judgment calls (frame selection, captions, synthesis). Everything runs locally by default; API models are opt-in per stage or session.

---

## 1. Install on macOS (Apple Silicon)

```bash
# system tools
brew install ffmpeg pandoc tesseract tesseract-lang
brew install --cask libreoffice          # optional: slide images for .pptx

# python (3.11–3.12 recommended for the ML packages)
cd knowledge-library
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[mac,docs]"             # add ,api for Claude/GPT/Gemini via LiteLLM

knowlib init ~/KnowledgeLibrary          # creates inbox/, sources/, notes/, config.yaml, CLAUDE.md ...
knowlib doctor -L ~/KnowledgeLibrary     # shows what is installed for the configured backends
```

First run downloads the models (once): Whisper large-v3-turbo (~1.6 GB) and Qwen3-VL-8B 4-bit (~6 GB) from Hugging Face.

## 2. Add material

Drop files into `inbox/` (never edited or moved by the pipeline). Subfolders are fine.

| You drop | It becomes |
| --- | --- |
| `.pdf` | PDF source; pages with figures rendered to images; scanned pages OCR'd and flagged |
| `.docx .pptx .xlsx .odt .rtf` | Office source; `.pptx` slide by slide (image + text + speaker notes) |
| `.html` / `urls.txt` / `.url` / `.webloc` | web source with URL and fetch date |
| a lone audio or video file | its own session (a `.srt`/`.vtt` with the same name is attached) |
| a folder with **one video + audio files** | one session: video for slides, best audio for the transcript |
| a folder with a **`session.yaml`** | one session, configured by the sidecar |

`session.yaml` (all keys optional):

```yaml
title: Corporate Finance - Lecture 4
date: 2026-10-28
language: el                  # el | en | mixed  (set explicitly; no auto-detect)
glossary: [WACC, CAPM, ΔΕΣΦΑ] # or a path to a text file; seeds Whisper's spelling
primary_audio: zoom_h1.wav    # the best recording; otherwise chosen by estimated SNR
video: lecture.mp4            # if the folder holds several videos
slides: slides.pdf            # the deck: ingested too and linked from the lecture
local_only: true              # block every API call for this session
fps: 2                        # fast screen recordings / live coding
tags: [mba, finance]
```

Two audio-only recordings of the same lecture need a `session.yaml`; otherwise they are treated as two lectures.

## 3. Run

```bash
knowlib process -L ~/KnowledgeLibrary            # ingest new/changed material (resumable)
knowlib qa -L ~/KnowledgeLibrary                 # every flag, for the QA pass
knowlib pending -L ~/KnowledgeLibrary            # sources not yet synthesized into notes
```

Then, in Claude with the library folder connected: **"process inbox"**. The agent follows `CLAUDE.md`: runs `knowlib process`, does QA, writes topic notes with citations (`[@source-id, slide 12, 14:32]`), updates `index.md` and `glossary.md`, and marks sources synthesized. For questions and work, it follows the **research** procedure: index → notes → sources.

## 4. What a lecture looks like

```markdown
## Slide 12 (14:32–16:05)
![Slide 12](assets/view/frame_012.jpg) · [full resolution](assets/frame_012.jpg)
> **OCR check:** the vision model's text and classic OCR disagree (...). Verify against the image.

**On slide:**
WACC = E/V × Re + D/V × Rd × (1 − Tc) ...

**Figure:** Formula slide defining WACC, with a worked example giving 8.5%.

**Spoken:**
[14:32] So the weighted average cost of capital ...
```

Frames are kept at native resolution; the `view/` copy (≤ 1568 px) is what the frontier model opens first. `assets/transcript.json` keeps every segment and word timestamp on the video timeline.

## 5. Swapping models and backends

Everything is in `config.yaml` (full list with defaults: `src/knowlib/config.py`). Examples:

```yaml
frame_selection: {backend: ollama, ollama_model: "qwen3-vl:8b"}          # local via Ollama
frame_selection: {backend: api, api_model: "anthropic/claude-sonnet-4-5"} # API, non-confidential only
transcription:   {backend: subtitles}                                      # use provided .srt/.vtt
ocr_crosscheck:  {backends: [apple-vision, tesseract]}                     # Tesseract (Greek) as third opinion
pdf:             {backend: marker}                                         # or pymupdf; scans: scan_backend: vision_llm
```

When a better model arrives, reprocess only what the old one produced:

```bash
knowlib reprocess -L ~/KnowledgeLibrary --where transcription.model=whisper-small
knowlib reprocess -L ~/KnowledgeLibrary --ids corporate-finance-lecture-4-a5a09eab
```

## 6. How the media pipeline works

| Stage | Module | Notes |
| --- | --- | --- |
| Session assembly | `media/session.py` | grouping rules above; primary audio from sidecar or estimated SNR |
| Alignment | `media/align.py` | high-passed log-loudness envelopes; coarse FFT correlation, then 30 s windows every 30 s; offset + drift fitted per segment; recorder pauses become new segments. Tested: 2 s/h drift and a 20 s pause recovered within ~20 ms |
| Transcription | `media/transcribe.py` | mlx-whisper large-v3-turbo; explicit language; glossary prompt; repetition-loop guard; mapped to the video timeline |
| Extraction | `media/frames.py` | ffmpeg, 1 fps, native resolution |
| Dedup | `media/frames.py` | 256-bit pHash vs the group's first frame + SSIM backup at 640 px; short groups folded in as transitions; talking-head fallback to interval frames |
| Selection, caption, OCR | `media/select.py` | one VLM call per candidate with the transcript of that stretch; keep / supersedes_previous (build-ups and whiteboards keep their final state) / drop; when unsure, keep. Cached per candidate, so interrupted runs resume |
| Dense text / 4K | `media/select.py` | the native frame is tiled into overlapping crops and read in full detail |
| Cross-check | `media/ocr.py` | Apple Vision (and optionally Tesseract) on every kept native frame; disagreements, especially on numbers, are flagged on the slide |
| Output | `ingestors/media.py` | slide-by-slide `source.md`, native frames + view copies, low-res archive of candidates in `.work/` |

Frames up to 2048 px (1080p, 1440p) go to the vision model untouched. Larger frames (4K) are judged from a 2048 px view, while their text is always read from native-resolution tiles. `video.vlm_max_native_px` controls the cut-off.

## 7. Code map

```
src/knowlib/
  config.py        defaults, config.yaml, per-session overrides
  contract.py      SourceRecord (frontmatter), SourceWriter (atomic writes), ids, hashing, provenance
  manifest.py      hashes, status, provenance queries, synthesis tracking
  router.py        inbox walk, session rules, URL lists
  runner.py        process inbox, incremental runs, last_run.json report
  cli.py           knowlib command
  ingestors/       base (interface + registry), pdf, office, web, media
  media/           av, session, align, transcribe, frames, select, ocr
  providers/       vision model layer: mlx-vlm, ollama, api (LiteLLM), mock
  templates/       config.yaml, CLAUDE.md, index.md, glossary.md
```

Adding a source type: write `ingestors/<name>.py` with an `@register`'d `Ingestor` (`can_handle`, `ingest`) that writes through `SourceWriter`, and import it in `ingestors/__init__.py`.

## 8. Tests

```bash
pip install -e ".[dev]" python-pptx && pytest -q
```

The suite generates its own material (a 2-minute lecture with slides, a separate better recording with a 2.3 s offset, a scanned slide deck, PDFs, Office files, a web page) and runs the whole pipeline with mock models, so it needs only ffmpeg.

## 9. To verify on the Mac (first test batch)

- mlx-whisper, mlx-vlm and Apple Vision paths run only on Apple Silicon; they were written against current APIs but not executed in the build environment. `knowlib doctor` then a single short lecture is the first check.
- Greek: Whisper quality, Qwen-VL slide reading, and whether Apple Vision supports Greek on your macOS version (`knowlib doctor` reports it; if not, add `tesseract` to `ocr_crosscheck.backends`).
- Dedup thresholds (`phash_threshold`, `ssim_threshold`, `camera_change_ratio`) on real footage, especially camera-filmed projectors.
- Alignment on a real camera + recorder pair.
