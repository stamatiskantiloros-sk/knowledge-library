# knowlib: system design

This document explains how knowlib works and why, in enough detail to rebuild it from nothing. It describes the code as built. Where the code and this document differ, the code is right and this document needs fixing.

The handbook creator has its own document, [handbook-design.md](handbook-design.md). This one covers everything underneath it.

## 1. What it is for

knowlib turns a folder of mixed material into a Markdown library that a frontier model can use for demanding academic and industry work. The inputs are PDFs, Office files, web pages, code, data, audio and recorded lectures. The output is plain files that any tool can read.

It exists because frontier models are excellent at reasoning but cannot directly use a three-hour lecture video or a scanned PDF, and sending everything to them is slow and costly. So knowlib separates the work:

- **Local tools do the bulk extraction**, which is high in volume and can run unattended.
- **A frontier model does the judgement**: quality checks, synthesis, research and writing.

## 2. Design principles

These rules explain most of the decisions below.

1. **Markdown is the source of truth.** Everything durable is a readable file. A tool can be replaced without losing the library.
2. **One output contract.** Every ingestor writes the same `source.md` shape, so QA, search and writing never depend on what produced a source.
3. **Originals are never touched.** `inbox/` is read-only by rule.
4. **Provenance on everything.** Each source records the tool and model behind each stage, so it can be found and redone when something better arrives.
5. **Flag doubt, do not hide it.** A disagreement becomes a visible note and a flag for a stronger reader.
6. **Everything resumes.** Each expensive stage caches its result, so a crash or a power cut costs little.
7. **One bad file never stops a batch.** Failures are recorded and the run continues.
8. **Keep native resolution.** Frames are never downscaled before reading. Any smaller copy is an additional view.
9. **No backend is hard-coded.** Tools and models are chosen in config, per stage and even per lecture.
10. **Models judge, code decides what code can decide.** Deterministic rules handle anything that does not need a model.
11. **Local by default, and privacy is enforced in code.** `local_only` blocks any route that would send material out.

## 3. Architecture

```
 inbox/ ──> router ──> runner ──> ingestor ──> sources/<id>/source.md + assets/
             │          │  │                         │
             │          │  └─ manifest.json          ├─ QA and synthesis ──> notes/, index.md, glossary.md
             │          └──── .work/ caches          ├─ research
             └─ session.yaml                         └─ handbook creator ──> books
```

| Layer | Modules | Job |
|---|---|---|
| Command line | `cli.py` | Commands and flags |
| Configuration | `config.py` | Defaults, layered overrides |
| Discovery | `router.py` | Turn folders into units of work |
| Orchestration | `runner.py`, `manifest.py`, `progress.py` | What to run, what happened, how far along |
| Ingestion | `ingestors/` | One per input family |
| Contract | `contract.py` | IDs, hashing, atomic writes, provenance |
| Media | `media/` | Sessions, alignment, transcription, frames, selection, OCR |
| Models | `providers/` | One interface over all vision backends |
| Handbooks | `handbook/` | Books per lecture |
| Agent procedures | `templates/CLAUDE.md` | What the agent does after ingestion |

## 4. The library on disk

```
<library>/
  inbox/          originals; never edited, can hold symlinks to folders elsewhere
  sources/<id>/   source.md and assets/
  notes/          synthesized topic notes, written by the agent
  organized/      optional tree of symlinks into sources/
  archive/        optional videos with the better audio muxed in
  index.md        map of the library, kept by the agent
  glossary.md     terms and names, kept by the agent
  manifest.json   what was processed, how and whether it was synthesized
  config.yaml     backends and models
  CLAUDE.md       procedures for the agent
  .work/          caches and reports; safe to delete, at the cost of redoing the work
    hash_cache.json, last_run.json, progress.json
    <id>/         per-lecture caches
    agent/        session reader jobs and answers
```

`knowlib init <dir>` creates this from `templates/`. Its `.gitignore` excludes `.work/`, `archive/` and `inbox/`, so a library can be its own repository of text.

## 5. Identity and change detection

**Source id:** `make_id(title, hash)` gives `<slug>-<first 8 hex of hash>`. `slugify` lowercases, removes accents, transliterates Greek to Latin, replaces anything else with `-`, and cuts at 48 characters. Ids are ASCII so they work in any tool or URL.

**Content hash:**
- A file: SHA-256 of its bytes.
- A session: SHA-256 over each member's name and hash, plus the sidecar.
- A URL: the hash of the URL for change detection. The stored source hash is the extracted text's, so a refetch only makes a new version if the page changed.

`HashCache` keeps `size + mtime → hash` in `.work/hash_cache.json`, because hashing large videos is slow.

**Atomic writes:** `SourceWriter` builds `sources/.<id>.tmp/`, writes `source.md` last, removes any old folder, then renames. A crash cannot leave a half-written source. Ingestors call `abort()` on any exception.

## 6. Discovery: the router

`discover()` walks `inbox/` depth first, skipping hidden names and `library.ignore` patterns. It returns `InputItem`s in the order **files, then URLs, then sessions**, so a lecture's slide PDF exists before the session that links to it.

**A session is one lecture, possibly several files:**
- A folder with `session.yaml` is one session. `files:` can list members explicitly.
- A folder with exactly one video and one or more audio files is one session, with no sidecar needed.
- Any other media file is its own session. A subtitle file with the same stem attaches to it.
- Two audio-only recordings of one lecture need a sidecar, because nothing else tells them from two lectures.

**URLs** come from `.url`, `.webloc`, `urls.txt` and `*.urls.txt`, one per line, with `#` lines ignored.

**`session.yaml`** (all keys optional):

```yaml
title: Corporate Finance - Lecture 4
date: 2026-10-28
language: el                       # el | en | mixed
glossary: [WACC, ΔΕΣΦΑ, CAPM]      # or a path to a text file, one term per line
primary_audio: zoom_h1.wav         # the best recording; otherwise picked by SNR
video: lecture.mp4                 # if the folder has several
slides: slides.pdf                 # ingested alongside and linked
local_only: true                   # block all API calls for this session
fps: 2                             # for fast screen recordings
tags: [finance]
files: [lecture.mp4, zoom_h1.wav]  # explicit membership
frame_selection: {...}             # any full config section is accepted too
```

## 7. The runner

`process()` runs this loop for each item:

1. Filter by `--only`. This match is case-sensitive on the key.
2. Hash the item. Skip it if unchanged, unless forced. Failed and stale entries always rerun.
3. Find the first registered ingestor that accepts it, or report it as unsupported.
4. Ingest it. `AgentJobsWaiting` means the item pauses for the session, and is not a failure. Any other exception is recorded in the manifest, and the run moves on.
5. On success, record it in the manifest. If the id changed, delete the old folder and report the rename, so citations can be updated.
6. Add the `organized/` symlink if a layout is set.

It ends by writing `.work/last_run.json` with `new`, `updated`, `unchanged`, `failed`, `unsupported`, `renamed`, `waiting`, `flags`, `orphans` and `pending_synthesis`. This report is how the agent learns what to do next. Orphans are manifest keys no longer found in the inbox.

**`--input <folder>`** replaces the inbox for a run and prefixes keys with the folder's name, so identical file names in different courses stay apart.

**`--layout "{course}/{date}/{name}"`** builds `organized/`. Fields: `course` (first path part), `folder` (all but the file), `name` (stem), `date` (found in the key, or `undated`). Sources stay flat, so changing the layout changes no id or citation.

**Manifest:** `manifest.json` is written atomically after every item. Each entry holds `hash`, `status` (`done`, `failed`, `stale`), `source_id`, `ingestor`, `pipeline`, `flags`, timestamps and `synthesized`. `match()` compares dotted provenance paths by substring, which powers:

```bash
knowlib reprocess --where transcription.model=small
```

**Progress:** `progress.py` keeps `.work/progress.json` and logs at most one line every 30 seconds, with a time estimate for the current stage. `knowlib status` reads it.

## 8. Configuration

Precedence, later winning: built-in defaults, library `config.yaml`, `session.yaml`, CLI overrides (`--set a.b=c`, parsed as YAML).

`session_config()` applies shortcut keys (`language`, `fps`, `local_only`) and any whole section. Appendix A lists every default.

**Recommended setup for lectures,** which the code defaults do not assume:

```yaml
transcription: {prefer_subtitles: true}     # when lectures have their own subtitles
frame_selection:
  backend: openai
  base_url: http://localhost:1234/v1
  openai_model: <fast vision model>
  manage_lms: true
  max_tokens: 6000
  split: true
  transcribe_model:
    openai_model: <strong vision model>
    context_length: 16384
    parallel: 1
    timeout_s: 450
  caption_model:
    openai_model: <strong vision model>
    context_length: 16384
    parallel: 1
    timeout_s: 450
ocr_crosscheck:
  backends: [apple-vision, tesseract]
```

## 9. The contract: source.md

Every ingestor writes YAML frontmatter and a Markdown body. Frontmatter fields, in order: `id`, `title`, `source_type`, `origin`, `original_date`, `ingested_at`, `fetch_date`, `language`, `pipeline`, `hash`, `flags`, `tags`, `reliability`, `related`, `stats`. Empty fields are omitted.

`source_type` is one of `pdf`, `office`, `web`, `audio`, `video`, `session`, `text`.

`pipeline` maps each stage to its tool, backend, model and version, through `stage()`.

`flags` lists everything a reader should doubt. They are the input to QA: `knowlib qa` prints all of them.

The session body is specified in section 11.8.

## 10. Document ingestors

All use `first_working()`, which tries backends in order and turns an import error or tool failure into a fallback. A fallback is itself recorded as a flag.

**PDF.** Docling, then Marker or PyMuPDF. Then `_page_survey` checks every page: under 25 characters means no text layer; an image over 4% of the page or more than 40 drawings means a figure. Figure pages are rendered at 200 dpi into `assets/`, so charts remain available as images. Scanned pages are rendered too. If the backend's text is well above the text layer (more than 40 characters per scanned page), the backend's OCR is trusted and each page is flagged for checking. Otherwise knowlib OCRs them with Tesseract or a vision model.

**Office.** Documents use Docling, then Pandoc, which also extracts embedded images. Presentations are written slide by slide with `python-pptx`, in the same shape as video slides: image (LibreOffice to PDF to PNG, if installed), slide text, tables and speaker notes.

**Web.** trafilatura extracts main content as Markdown, including tables and links. URL and fetch date are always stored, because pages change. A result under 400 characters is flagged. The Jina fallback is a third-party service, so it is removed under `local_only`, and for local files.

**Text.** `.md`, `.txt`, code, notebooks and tables, so a lecture's exact code can be cited. Notebooks keep code and Markdown cells and drop outputs. CSV and TSV become a summary of shape, columns and the first 15 rows, not the whole file. Bodies are cut at 200,000 characters with a flag. Decoding tries UTF-8, then Windows Greek, then Latin-1.

## 11. The media pipeline

This is the core of knowlib. A lecture has two streams, what was said and what was shown, and they are captured separately and then joined on one timeline. Every stage caches in `.work/<id>/`.

```
assemble ─ align ─ transcribe ─ extract frames ─ group ─ decide ─ read ─ merge ─ OCR check ─ caption ─ render
```

### 11.1 Session assembly

`assemble()` probes each file with ffprobe, chooses the video, and chooses the primary audio: the one named in the sidecar, the only one, or the best estimated signal-to-noise ratio. SNR is the 95th minus the 10th percentile of frame loudness in dB, with level as the tiebreak.

### 11.2 Alignment

This applies when the best audio is a separate recorder. The result maps primary time onto video time.

1. Both signals become log-loudness envelopes at 100 Hz, with a 0.5 s high-pass. Envelopes, not waveforms, so a close recorder still matches a distant camera microphone. The log turns a gain difference into a constant, and the high-pass removes it.
2. Coarse offset from FFT cross-correlation over the full length.
3. Every 30 s, a 30 s window is matched within ±8 s of the tracked offset by normalized cross-correlation, accepting peaks of 0.2 or more. On a miss it searches the whole video, so a paused recorder can be recovered.
4. Points are split into segments where the offset jumps by more than 0.5 s and the next point confirms it. Isolated outliers are dropped.
5. Segments over 300 s get a robust linear fit for clock drift. Shorter ones use the median offset.

The result is a piecewise-linear map, `t_video = t_primary + b + drift × t_primary`. It warns if under half the windows aligned, and flags multiple segments. `mux_better_audio` can write an archive video with the better audio, for single-segment maps.

### 11.3 Transcription

Backends: `mlx-whisper`, `faster-whisper`, `whisper-cpp`, `subtitles`, `mock`. Default model: `whisper-large-v3-turbo`.

- Language is set explicitly, because auto-detection fails on mixed lectures. `mixed` maps to `mixed_language_as`.
- The session glossary seeds Whisper's initial prompt, up to 800 characters, so names and terms come out right.
- `condition_on_previous_text` is off, because it risks repetition loops.
- Three or more identical consecutive segments collapse, with a flag.
- With `prefer_subtitles`, the lecture's own `.srt` or `.vtt` is used instead. The parser strips markup and the trailing cue numbers some exporters add.

Timestamps then pass through the alignment map. The cache fingerprint covers audio, backend, model, language, glossary and subtitles.

### 11.4 Frames and grouping

ffmpeg extracts frames at `fps` (default 1) at native resolution, JPEG quality 2.

`group_frames` walks them in order. A frame starts a new group if its 256-bit pHash differs from the group's **first** frame by more than 6 bits, or, as a backup that catches a single changed number, if SSIM at 640 px width falls below 0.985. Comparing with the first frame, not the previous one, stops slow changes from drifting through.

Groups shorter than `min_group_s` (2 s) are treated as transitions and folded into the next group.

If over half the frames start a group, in a video of at least 30 frames, it is camera footage. The system then falls back to one frame every 60 s, with a flag. Screen recordings do not trigger this, because they hold still most of the time.

Each group keeps up to 3 samples: first, sharpest (Laplacian variance) and last.

### 11.5 Deciding and reading

There are two modes. The **single-prompt** mode asks one model for everything at once. The **split** mode (`split: true`) is the recommended one, because combining the jobs made models both merge slides and invent text.

**Decide.** The fast model sees the samples and transcript and answers `keep` or `drop`, plus `content_type`. The prompt states that samples are expected to look alike, because the model otherwise dropped them as duplicates. If unsure, it keeps. An unparseable reply retries once, then keeps by default with a flag. All decisions run first, then the model is released, so one model is in memory at a time.

**Read.** The strong model transcribes the richest sample, chosen as the one with the most OCR words. Models tend to name the first sample as best, and on a build-up slide that loses content. A reply cut off at the token limit is treated as a runaway loop and discarded. A failed read is logged and falls back to OCR later.

Frames over `vlm_max_native_px` (2048) are cut into overlapping 1344 px tiles, read together, and merged by the model, so small text keeps full detail.

**Supersede, by code.** `_builds_on()` replaces the previous frame if both are `slide`, `code` or `whiteboard`, the new text is longer, and it holds at least 90% of the previous words. This handles slides that build step by step. Charts and screens are excluded, because they can change under identical words.

**Merge repeats.** Two consecutive frames merge if text similarity is at least 0.98 **and** image SSIM at least 0.97. Requiring both keeps a chart that changes under the same title.

### 11.6 OCR cross-check

Apple Vision and Tesseract read every kept frame at native resolution, and each is compared with the model's text.

- Text is normalized: Unicode folded, accents removed, Markdown removed.
- Similarity is **word overlap** (a Dice score on word counts), not word order, because OCR reads tables cell by cell.
- A backend disagrees if similarity is under 0.75 with more than 20 OCR characters, or if numbers appear on one side only.
- A slide is flagged only when a majority of backends disagree. With two, both must.

**Recurring text** is removed before comparing. Lines that OCR finds on at least 30% of a session's frames, and at least 3, are treated as interface chrome or footers. Lines without letters are never removed, so recurring numbers stay checked.

`match_frame_to_text` swaps to another sample if it matches the text better by over 0.05. `fill_text_from_ocr` keeps OCR text, labelled as such, when the model gave none.

A flag adds both OCR outputs to `source.md`, so a stronger reader can compare them with the image.

### 11.7 Captions

Captions run last, from the final frame and checked text, with the same frame's transcript. The fast model captions ordinary slides. The strong model captions tables, charts, anything with a Markdown table, and flagged slides. A stalled strong model falls back to the fast one, and a slide no model captions gets a flag.

Captions are descriptions for search, and may state inferences. They are never treated as evidence.

### 11.8 Output

```markdown
# Title

*Duration 05:34 · 16 slides/frames kept from 26 candidates*

## Before the first slide (to 00:10)

## Slide 3 (00:11–00:53)
![Slide 3](assets/view/frame_003.jpg) · [full resolution](assets/frame_003.jpg)
> **OCR check:** ...                    (flagged only)

**On slide:**                           (**On slide (OCR):** if the model gave no text)

**OCR text (apple-vision):**            (flagged only)

**Figure:** caption *(table)*

**Spoken:**

[00:10] transcript paragraphs of about 60 seconds
```

A slide's speech runs from its start to the next slide's start. Audio-only sessions become timestamped transcript sections of 5 minutes.

`assets/` holds `frame_NNN` at native resolution, `view/frame_NNN.jpg` at up to 1568 px for model reading, and `transcript.json` with all segments.

**Cleanup:** after a successful write, decoded audio and full frames are deleted from `.work/`. Low-resolution copies of the samples stay in `archive/` there, along with the cached selection, so selection can be redone without the video.

## 12. Model providers

`VisionProvider` has `generate(images, prompt)`, `json()` (with a tolerant parser that handles fences and chatter), `close()` and `provenance()`. `get_vision_provider(section, cfg)` builds one from a config section. `release_all()` closes them all between stages.

| Backend | Use | Local |
|---|---|---|
| `openai` | Any OpenAI-compatible server, such as LM Studio or llama.cpp | If the URL is this machine |
| `mlx-vlm` | Apple Silicon in-process | Yes |
| `ollama` | Ollama server | Yes |
| `api` | Any model through LiteLLM | No |
| `agent` | Subagents in a Claude Code session | No |
| `mock` | Tests | Yes |

With `manage_lms`, the `openai` provider loads and unloads models through the `lms` CLI, passing context length and parallel slots. Parallel slots share memory, and requests are sequential, so use one. It strips reasoning up to `</think>` and records `finish_reason`. A local timeout is required, because a stalled local server otherwise blocks a lecture for its full timeout.

`reasoning_effort: none` is available on this provider. In testing it kept the numbers on 10 of 11 frames and saved about a quarter of the reading time, with small losses of on-screen text. It is off by default.

### Session reader

`knowlib process --reader session` sets the reader to the `agent` backend, with no API key.

- `AgentProvider.generate` hashes the model, the full image paths and the prompt into a job id. If `.work/agent/answers/<id>.md` exists, it returns it. Otherwise it records the job and raises `AgentPending`.
- Callers skip that frame without caching anything.
- After a stage, `stop_if_waiting` writes `.work/agent/jobs.json` and raises `AgentJobsWaiting`. The runner reports the item as waiting, not failed.
- A subagent per model reads each job's images, follows its prompt, and writes the answer file.
- The same command then continues. Decisions and readings are cached, so it stops once more for hard captions and then finishes.

Files suit this because answers can arrive out of order, a run resumes at any point, and there is no JSON escaping to get wrong.

Haiku reads ordinary frames. Sonnet reads `table`, `chart`, `code`, `whiteboard`, `diagram` and `demo`, and writes hard captions. The fast local model still decides keep or drop, and writes easy captions. Provenance is recorded under `pipeline.reader`. `local_only` refuses this backend. The `knowlib handbook` command does not take `--reader`, so ingest with `process` first.

## 13. After ingestion: the agent

knowlib stops at clean sources. `templates/CLAUDE.md` tells the agent what comes next.

1. **QA.** Read `last_run.json`, the flags, three transcript passages, and every image a flag names. Fix through config and reprocessing, never by editing `source.md`.
2. **Synthesize.** Write topic notes in `notes/` with a citation on every claim, in the form `[@<source-id>, slide 12, 14:32]`. Record disagreements instead of picking a side. Update `index.md` and `glossary.md`, then `knowlib mark-synthesized`.
3. **Research.** Answer from notes, drilling into sources with `rg`, and keep sources separate from reasoning.
4. **Handbook.** See [handbook-design.md](handbook-design.md).

## 14. Privacy

`local_only` can be set for the library, a session or a run. It blocks the `api` and `agent` backends, a non-local `openai` URL, and the Jina web fallback. The default backends are all local, so nothing leaves the machine unless a config selects otherwise. An agent reading sources in a session is a separate matter: its procedures keep `local_only` sources away from external services.

## 15. Commands

| Command | Purpose |
|---|---|
| `knowlib init [DIR]` | Create a library |
| `knowlib doctor` | Check tools, packages and backends |
| `knowlib process` | Ingest new or changed material. Flags: `--input`, `--layout`, `--only`, `--force`, `--dry-run`, `--limit`, `--local-only`, `--set`, `--reader` |
| `knowlib status` | Counts, failures and live progress |
| `knowlib qa` | All flags across sources |
| `knowlib reprocess` | Mark matching sources stale and re-ingest, with `--where` or `--ids` |
| `knowlib pending` | Sources not yet synthesized |
| `knowlib mark-synthesized ID...` | Record synthesis |
| `knowlib handbook` | Handbooks, with `--prepare`, `--finish`, `--input`, `--only`, `--output` |
| `knowlib transcribe FILE` | Transcribe one file, for testing |

## 16. Tests

`tests/test_pipeline.py` runs end to end on generated fixtures with the mock provider. `tests/test_units.py` covers individual rules: IDs, grouping, alignment, OCR comparison, selection, handbook checks and the session reader.

```bash
.venv/bin/python -m pytest -q tests
```

Any change to selection rules should also be tested on real frames. Unit tests cannot show that a model invented text.

**Evaluating models.** Pick about 8–11 frames covering a clean slide, a table, a build-up slide, a chart, and busy screens. Run each model with the reading prompt, then compare against the images by hand, checking every number. Record time per frame and peak memory.

## 17. Rebuilding from nothing

1. Install Python 3.11 or newer, ffmpeg, Pandoc, Tesseract with language data, and a local vision model server. `scripts/install-mac.sh` does this on Apple Silicon.
2. Install the package with `pip install -e ".[mac,docs,dev]"`.
3. Build in this order, testing each with the mock provider: contract, config, router, manifest, runner, the text ingestor, then PDF, Office and web.
4. Then build media in pipeline order: session, transcription, frames, single-prompt selection, OCR, rendering. Add alignment once one-file lectures work.
5. Add the split mode and test it on real lectures. Most quality depends on it.
6. Add `templates/CLAUDE.md`, then the handbook creator.

Keep the decisions in section 2, the thresholds in section 11, and the prompts in Appendix B. The thresholds came from testing, and small changes have large effects.

## 18. Known limits

- OCR flags are noisy on screen recordings, where OCR often misses tables and reads interface text. On one such lecture 58 of 65 slides were flagged, and the two checked were false alarms.
- The recurring-text filter still lets some interface text through.
- A failed reading leaves junk OCR in `source.md` until someone reprocesses it.
- Models can silently correct on-screen spelling.
- Video ingestion is slow on a laptop: time follows slide count, at minutes per slide with a strong local model.
- `--only` is case-sensitive in `process` and case-insensitive in `handbook`.
- The session reader needs a person to start it, and has been tested on one short lecture.
- Web pages with almost no content, and CSV files with an undetectable delimiter, fail ingestion.
- Lectures in other languages and mixed languages have not been tested end to end.

## Appendix A: default configuration

```yaml
library:
  inbox: inbox
  sources: sources
  notes: notes
  work: .work
  ignore: []
  layout: null
local_only: false
transcription:
  backend: mlx-whisper
  model: mlx-community/whisper-large-v3-turbo
  whisper_cpp_model: ~/models/ggml-large-v3-turbo.bin
  language: el
  mixed_language_as: el
  word_timestamps: true
  glossary_prompt_chars: 800
  prefer_subtitles: false
alignment:
  envelope_hz: 100
  window_s: 30
  step_s: 30
  search_s: 8
  min_confidence: 0.2
  jump_threshold_s: 0.5
  min_drift_span_s: 300
  mux_better_audio: false
video:
  fps: 1
  extract_resolution: native
  frame_format: jpg
  dedup: phash
  phash_size: 16
  phash_threshold: 6
  ssim_check: true
  ssim_width: 640
  ssim_threshold: 0.985
  min_group_s: 2
  camera_change_ratio: 0.5
  talking_head_interval_s: 60
  frame_store: native
  selection_input: native
  vlm_max_native_px: 2048
  tile_px: 1344
  tile_overlap: 0.12
  ocr_input: native
  model_view_max_px: 1568
  keep_lowres_archive: true
  archive_px: 480
  samples_per_candidate: 3
  transcript_context_s: 10
frame_selection:
  backend: mlx-vlm
  model: mlx-community/Qwen3-VL-8B-Instruct-4bit
  base_url: http://localhost:8080/v1
  openai_model: local
  ollama_model: qwen3-vl:8b
  ollama_url: http://localhost:11434
  api_model: anthropic/claude-sonnet-4-5
  max_tokens: 1200
  temperature: 0.0
  split: false
  transcribe_model: null
  caption_model: null
  caption_model_types:
  - table
  - chart
handbook:
  language: English
  max_tokens: 6000
ocr_crosscheck:
  backends:
  - apple-vision
  languages:
  - el-GR
  - en-US
  tesseract_langs: ell+eng
  similarity_threshold: 0.75
pdf:
  backend: docling
  fallbacks:
  - pymupdf
  scan_backend: tesseract
  render_figure_pages: true
  render_dpi: 200
  min_text_chars: 25
office:
  backend: docling
  fallbacks:
  - pandoc
  - python-pptx
  render_slides: true
web:
  backend: trafilatura
  fallbacks: []
  timeout_s: 30
provider:
  vision: null
```

## Appendix B: model prompts

### Decide (split mode)

```text
You are selecting frames from a recorded lecture to build a slide-by-slide record of it.
The images are {n} sample frame(s) from one stretch of the video ({start} to {end}), in time order (index 0 = earliest).
The samples are expected to look alike. That they resemble each other is never a reason to drop them.

Transcript spoken during this stretch:
"""{transcript}"""

Decide:
- "keep" if any sample shows content: a slide, chart, table, diagram, code, whiteboard or blackboard, a demo or application screen. Repeats of earlier frames are removed in a later step, so keep them.
- "drop" only if no sample shows content: only the speaker or audience, a transition, a blurry or blank frame, or a pure title card.
If you are unsure, choose "keep".

Return JSON with exactly these keys:
{{"decision": "keep" | "drop",
  "best_frame": <index of the clearest, most complete sample>,
  "reason": "<one short sentence>",
  "content_type": "slide" | "chart" | "table" | "diagram" | "code" | "whiteboard" | "demo" | "photo" | "speaker" | "transition" | "other"}}
```

### Read

```text
Transcribe all text and numbers visible in this image, exactly as shown, line by line.
Tables as Markdown tables, including every row label, column label and total. Keep every number exactly.
Copy only what is visible. Do not explain, summarise, or add anything that is not in the image.
Output only the transcription.
```

### Caption

```text
This image is one frame of a recorded lecture.
Text on the frame:
"""{text}"""
Transcript spoken during it:
"""{transcript}"""
In one or two sentences in {caption_language} say what the frame shows and what it is about. For a table or chart, say exactly what its values represent.
Output only the sentences.
```

### Single-prompt mode

```text
You are selecting frames from a recorded lecture to build a slide-by-slide record of it.
The images are {n} sample frame(s) from one stretch of the video ({start} to {end}), in time order (index 0 = earliest).

Transcript spoken during this stretch:
"""{transcript}"""

Previously kept frame: {previous}

Decide:
- "keep" if the frame shows information worth keeping: a new slide, chart, table, diagram, code, whiteboard or blackboard content, a demo or application screen.
- "supersedes_previous" if it shows the SAME slide or board as the previously kept frame but more complete (a slide that builds up step by step, more writing added). The final, most complete state should win.
- "drop" if it only shows the speaker or audience, a transition, a blurry or blank frame, a pure title card with no content, or an exact duplicate.
If you are unsure, choose "keep". Cues in the transcript such as "as you can see here" or "look at this chart" are strong signals to keep.

Return JSON with exactly these keys:
{{"decision": "keep" | "supersedes_previous" | "drop",
  "best_frame": <index of the clearest, most complete sample>,
  "reason": "<one short sentence>",
  "caption": "<one or two sentences in {caption_language}: what the frame shows and what it is about; for charts say what is plotted and the main takeaway>",
  "slide_text": "<all text visible in best_frame, transcribed exactly in its original language, line by line; tables as Markdown tables; empty string if none>",
  "content_type": "slide" | "chart" | "table" | "diagram" | "code" | "whiteboard" | "demo" | "photo" | "speaker" | "transition" | "other",
  "dense_text": <true if the frame has much small text that may need a closer look>}}
```

### Tiles

```text
These images are overlapping crops of one lecture slide, left to right, top to bottom.
Transcribe ALL text exactly as written, in its original language, merging the crops into one
reading-order text and removing text duplicated by the overlap. Keep tables as Markdown tables and
keep every number exactly. Output only the text.
```

### Handbook plan, local writer

```text
Below are the slides of one recorded lecture, in the order they were taught.
Divide the lecture into consecutive sections a student can read as chapters of a book. Keep the
lecture's own order and cover every slide exactly once. Leave out nothing; a section may be one slide.

{slides}

Return JSON only: {{"title": "<title of the lecture>",
 "sections": [{{"title": "<short heading>", "first_slide": <number>, "last_slide": <number>}}]}}
```

### Handbook section, local writer

```text
You are writing one section of a study book that records a lecture.
A student who missed the class will read it instead of watching the recording.

Section: {title}

What was taught, slide by slide:
{material}

Rules:
- Write flowing prose in {language}, following the order of the lecture. Do not reorganise it.
- Stay faithful to what was taught. Keep the lecturer's definitions, examples and reasoning, and give a topic as much space as it got in class. Remove filler and repetition.
- Write only what this section's slides and the lecturer's words contain. Do not continue into later steps, and do not add explanations, consequences or conclusions the lecturer did not state.
- "What it shows" is an automatic description of the image. Use it to understand the picture, never as a source of claims about the subject.
- Do not count rows or items, or state a total, unless the slide or the lecturer does.
- Report only what this section contains. Do not link back to earlier steps or ahead to later ones, sum up the procedure, or comment on the method. Say "the lecturer said" only for what the lecturer's words here contain.
- Do not describe decorative images. Call the speaker "the lecturer", without he or she.
- Copy every number, formula and table exactly from the slide text. Never compute a value that was not shown or said. Do not add a Total or any label the slide lacks.
- Use Markdown tables for tables and LaTeX for formulas.
- To place a slide's image, write [[slide N]] on its own line where the reader needs it. Use it for charts, tables, diagrams and screens, not for plain text slides.
- Do not write the section heading, an introduction to the book, or closing remarks.
Output only the section text.
```

### Number fix, local writer

```text
{text}

The section above contains these numbers, which appear in no slide or transcript of this section: {bad}.
Rewrite it, removing or correcting each one using only this material. Keep everything else unchanged.

{material}

Output only the section text.
```

### Table fix, local writer

```text
{text}

A table above has rows with a different number of cells from its header, so values sit under the wrong headings.
Rewrite the section with every row matching its header, copying each table from this material. Keep everything else unchanged.

{material}

Output only the section text.
```

### Note, local writer

```text
Below is one section of a study book that records a lecture, then passages from the course materials.

{text}

Passages:
{passages}

Almost every section needs nothing. Add a note only if the student cannot follow this section or the next one without something missing here that a passage states. Say nothing the next slide of the lecture will give anyway. Use only the passages and never mention them; quote only their exact words.
Return JSON only: {{"note": null}} or {{"note": "<one or two sentences>", "passage": <number>}}
```
