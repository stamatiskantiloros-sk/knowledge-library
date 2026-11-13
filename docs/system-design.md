# knowlib: system design

This document explains how knowlib works and why, in enough detail to rebuild it from nothing. It describes the code as built. Where the code and this document differ, the code is right and this document needs fixing.

The handbook creator has its own document, [handbook-design.md](handbook-design.md), and the checks that run through every stage are collected in [adversarial-review.md](adversarial-review.md). This one covers everything underneath.

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
12. **Nothing grades itself.** Every output is checked by something that did not produce it: a different method, code, a second model of another family, or the user. Disagreements become flags for a stronger reader, never quiet fixes. See [adversarial-review.md](adversarial-review.md).

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

There are two layouts, chosen at setup and recorded as `library.mode`. Everything that needs a source's files asks `layout.py`, so ingestion, checks, digests and handbooks work the same in both, and notes cite the same stable ids.

**In place** (`mode: inplace`, the default for a folder that already holds material). The library is the user's own folder, used as it is. knowlib crawls every subfolder and writes each source's Markdown beside its original, named after the whole file name so it never collides with the user's own `.md` files:

```
<your folder>/
  Lecturer/inventory.pdf            the user's originals, never edited, moved or deleted
  Lecturer/inventory.pdf.md         its ingested text, frontmatter `generated_by: knowlib`
  Lecture 3/Lecture 3.lecture.md  a lecture folder (video and audio): the lecture, slide by slide
  textbook.pdf.digest.md          a long book's finding aid
  notes/                          synthesized topic notes, written by the agent
  INDEX.md                        topics (the agent) and the sources table (knowlib, between markers)
  glossary.md                     terms and names, kept by the agent
  AGENTS.md                       procedures for any agent; CLAUDE.md imports it
  .ignore                         tells rg to skip _knowlib's images and caches
  _knowlib/                       knowlib's own files, visible on purpose
    config.yaml                   the setup answers
    manifest.json                 what was processed, how, where its Markdown is, whether synthesized
    sources.jsonl                 one line per source: id, title, type, size, Markdown path, digest, flags
    assets/<id>/                  frames, page renders and outline.json; image links point here
    work/                         caches and reports; safe to delete
```

The crawler skips `_knowlib/`, `notes/`, the agent files at the top and any Markdown carrying `generated_by: knowlib`, so a second run never ingests its own output. Image links are written relative to wherever the Markdown sits (`../../_knowlib/assets/<id>/page_001.png` two folders down). The manifest stores each source's output path, so finding a source from its id never involves guessing. A scanned page with no text layer and no OCR text keeps its image in the Markdown, so it is never silently lost.

**Separate** (`mode: separate`, the original layout). Originals go in `inbox/` and each source gets its own folder:

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

Its `.gitignore` excludes `.work/`, `archive/` and `inbox/`, so a library can be its own repository of text.

**Setup** (`setup.py`). `knowlib init <dir>` asks a few questions, each with a default: in place or separate (in place when the folder already holds material); which folders or name patterns to leave out, after showing how many files per top-level folder knowlib can read; whether the material is confidential (`local_only`); the language; and, from the models the local model server offers, a fast model, a strong model and a note reviewer, ideally of another family. Every answer has a flag (`--mode`, `--skip`, `--local-only`, `--language`, `--fast-model`, `--strong-model`, `--reviewer-model`) and `--yes` accepts the defaults, so an agent can run setup without questions. Only the answers go into the config, under a header naming the date; everything else keeps the built-in default (Appendix A), and `config.example.yaml` is copied beside it for reference. A previous config is kept as `config.yaml.bak`. `knowlib process` on a folder that was never set up offers setup at a terminal and otherwise says to run `knowlib init`, rather than creating `inbox/` there.

**Files for agents.** Setup writes the procedures from one template, filled in for the layout (`AGENTS.md` and a `CLAUDE.md` that imports it in place; `CLAUDE.md` in a separate library), plus the index and glossary; it never overwrites them once written. After every `process` and `mark-synthesized`, knowlib rewrites the sources table between `<!-- knowlib:sources -->` markers in the index (id, title, type, size, a link to the Markdown, flags, synthesized) and `sources.jsonl`, so an agent goes from a cited id to its file in one lookup. Each generated Markdown starts with a contents list of its headings, each with its page when the text has page markers, when there are between 3 and 80.

## 5. Identity and change detection

**Source id:** `make_id(title, hash)` gives `<slug>-<first 8 hex of hash>`. `slugify` lowercases, removes accents, transliterates Greek to Latin, replaces anything else with `-`, and cuts at 48 characters. Ids are ASCII so they work in any tool or URL.

**Content hash:**
- A file: SHA-256 of its bytes.
- A session: SHA-256 over each member's name and hash, plus the sidecar.
- A URL: the hash of the URL for change detection. The stored source hash is the extracted text's, so a refetch only makes a new version if the page changed.

`HashCache` keeps `size + mtime → hash` in `.work/hash_cache.json`, because hashing large videos is slow.

**Atomic writes:** `SourceWriter` builds `sources/.<id>.tmp/`, writes `source.md` last, removes any old folder, then renames. A crash cannot leave a half-written source. Ingestors call `abort()` on any exception.

## 6. Discovery: the router

`discover()` walks `inbox/` depth first, skipping hidden names and `library.ignore` patterns. It returns `InputItem`s in the order **files, then image folders, then URLs, then sessions**, so a lecture's slide PDF exists before the session that links to it.

**A session is one lecture, possibly several files:**
- A folder with `session.yaml` is one session. `files:` can list members explicitly.
- A folder with exactly one video and one or more audio files is one session, with no sidecar needed.
- Any other media file is its own session. A subtitle file with the same stem attaches to it.
- Two audio-only recordings of one lecture need a sidecar, because nothing else tells them from two lectures.

**Loose images** (`.png .jpg .jpeg .webp .tif .tiff .bmp .gif`) directly in one folder become one item of kind `images`, keyed `<folder>/*images`, so 48 quiz screenshots are one source, not 48. Adding or changing a screenshot changes the folder's hash and updates that one source. HEIC photos are not read yet (convert them to JPEG). For a quiz screenshot the model lists every option on its own line and adds, in brackets, only what is shown beside it (`[selected]`, `[green check]`, `[red X]`), never what it thinks the marks mean; the OCR comparison leaves those brackets, "Figure:" lines and list numbering out.

**URLs** come from `.url`, `.webloc`, `urls.txt` and `*.urls.txt`, one per line, with `#` lines ignored.

**`session.yaml`** (all keys optional):

```yaml
title: Corporate Finance - Lecture 4
date: 2026-10-28
language: el                       # el | en | mixed
glossary: [WACC, ΔΕΣΦΑ, CAPM]      # or a path to a text file, one term per line
primary_audio: recorder.wav         # the best recording; otherwise picked by SNR
video: lecture.mp4                 # if the folder has several
slides: slides.pdf                 # ingested alongside and linked
local_only: true                   # block all API calls for this session
fps: 2                             # for fast screen recordings
tags: [finance]
files: [lecture.mp4, recorder.wav]  # explicit membership
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

**Progress:** `progress.py` keeps `.work/progress.json` and a status line with a bar for each stage that can count its work (pages, frames, OCR checks, captions, digest parts), the time left in the stage and, after the first item, overall. Stages that cannot count (Docling conversion, speech to text, alignment, frame extraction) show how long they have run. In a terminal the line redraws in place; in a log file it is written every 30 seconds. `process`, `handbook` and `digest` report this way, and `knowlib status` reads the file from another shell.

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

**PDF.** Docling, then Marker or PyMuPDF. Docling and PyMuPDF write the text page by page, each page starting with `<!-- page N (pdf M) -->`, where N is the number printed on the page (the PDF's page label) and M its place in the file, so notes can cite printed pages. Marker has no page markers. Docling leaves `<!-- formula-not-decoded -->` where it cannot read an equation; knowlib fills each gap with the PDF text layer inside the formula's box, in a ```` ```formula ```` block. The symbols survive but the layout does not, so a flag counts these formulas, and any gap the text layer cannot fill. Tables are written compact. `outline()` saves the PDF's bookmarks and its table and figure captions, with printed pages, to `assets/outline.json`. A caption is a line starting "Table 3.1" followed by nothing or a title in capitals. "Table 3.7 summarizes" and a sentence ending "Table 3.7." are mentions. A number that is only mentioned keeps its first mention's page, with no title. Then `_page_survey` checks every page: under 25 characters means no text layer; an image over 4% of the page or more than 40 drawings means a figure. Figure pages are rendered at 200 dpi into `assets/`, so charts remain available as images. Scanned pages are rendered too. If the backend's text is well above the text layer (more than 40 characters per scanned page), the backend's OCR is trusted and each page is flagged for checking. Otherwise knowlib OCRs them with Tesseract or a vision model.

**Figures in PDFs** (`ingestors/figures.py`, Docling only). Docling marks each picture and its box; knowlib writes `<!-- figure P-K box -->` there, crops the box at 200 dpi, and skips, without a model, boxes under 60 pt or 2% of the page (icons, bullets), a picture that recurs on three or more pages (logos, running heads) and photos with no text in them (OCR finds fewer than three words in a picture that is not flat-coloured). The vision model (`pdf.figures.model`, then `images.model`, then the strong local model, `frame_selection` with its `caption_model`) reads the rest: every label and number exactly, tables, journal entries and T-accounts as Markdown tables with the account names in their cells, no headings or descriptions of colours and arrows, no estimate of a value that is not printed (a bar's length), and a "Figure:" line saying what a chart or diagram shows; it may answer DECORATIVE, which counts even after an explanation. The fast model was tried first on a 881-page textbook and flagged 176 of 255 readings: it dropped text beside tables, moved account names out of journal entries into its description and estimated unlabelled bars, so figures, being few, go to the strong model. Its text goes where the picture was, in a quote block under the cropped image, on its page, so a note cites it as `p. N` and check-notes can find its numbers. OCR checks each reading as for slides, without the reading's headings, code fences and dotted leaders, which OCR never sees; a sign that OCR spaced apart (`+ 3,000`) or dropped still matches, opposite signs do not. Skipped pictures stay as `<!-- image: small | repeated | photo without text | decorative -->`. An empty or cut-off reply is asked again, `pdf.figures.retries` (2) more times, with twice the tokens each time: it comes from the model spending `pdf.figures.max_tokens` (8000) thinking, from a big table that needs more room (a page of twelve MRP records stopped after seven at 4000), or from a thinking loop that the server stops early, and a loop is random (on the textbook, the model server looped on 12 of 246 plain journal entries and read each one at once when asked again). An empty reply after that is never taken as decorative: the figure is flagged as not read, its crop is kept beside `<!-- image: not read -->`, and nothing is cached, so the next run asks again. A reading that still stops at the limit ends `[CUT OFF ...]` and is flagged, and the next run asks again too. Before reading, `pdf.figures.detail_check` (`stop`, or `warn`, `off`) asks the server how many prompt tokens the largest crop still to be read gets, and a copy at half its width and height: a server that keeps the detail gives the full crop about four times as many, so under twice as many means it caps image tokens and the run stops with the fix. A capped server gave every figure about 290 tokens: a dense ROIC tree came back with ten wrong numbers, and with the cap raised to about 2,000 tokens (2 megapixels, enough for nearly every crop at 200 dpi) all 41 values were exact. Readings are cached by crop and prompt, and Docling's own output is cached by file hash in `work/docling/`, so reprocessing a book does not convert it again.

**Office.** Documents use Docling, then Pandoc, which also extracts embedded images. Presentations are written slide by slide with `python-pptx`, in the same shape as video slides: image (LibreOffice to PDF to PNG, if installed), slide text, tables and speaker notes.

**Web.** trafilatura extracts main content as Markdown, including tables and links. URL and fetch date are always stored, because pages change. A result under 400 characters is flagged. The Jina fallback is a third-party service, so it is removed under `local_only`, and for local files.

**Text.** `.md`, `.txt`, code, notebooks and tables, so a lecture's exact code can be cited. Notebooks keep code and Markdown cells and drop outputs. CSV and TSV become a summary of shape, columns and the first 15 rows, not the whole file. Bodies are cut at 200,000 characters with a flag. Decoding tries UTF-8, then Windows Greek, then Latin-1.

**Images.** `ingestors/images.py` writes `# <folder> (images)` and one `## Image N: <file name>` section per image, with a copy at `images.max_px` (1568) in `assets/`. The vision model (`images.model`, default `frame_selection`) transcribes each image with every number exact, lists every option of a multiple-choice question and says which is marked and whether it is shown correct, and adds a "Figure:" line for a chart. Classic OCR (`ocr_crosscheck`) reads the same copy; where the two disagree, above all on numbers, the image is flagged. Replies are cached in `work/images/read_cache.json` by file hash and model, so a rerun reads only new images. If the model fails, the OCR text is used and flagged. Notes cite `[@id, image N]`.

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

Backends: `mlx-whisper`, `faster-whisper`, `whisper-cpp`, `subtitles`, `mock`. The model is set in `transcription.model`; a larger model is worth its time for names and terms.

- Language is set explicitly, because auto-detection fails on mixed lectures. `mixed` maps to `mixed_language_as`.
- The session glossary seeds the speech model's initial prompt, up to 800 characters, so names and terms come out right.
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

Captions run last, from the final frame and checked text, with the same frame's transcript. The fast model captions ordinary slides, unless `caption_model_types: all` sends every slide to the strong model, as the tested library does so that every caption comes from one model. The strong model captions tables, charts, anything with a Markdown table, and flagged slides. A stalled strong model falls back to the fast one, and a slide no model captions gets a flag.

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

**Dropped frames** come last, under `## Dropped frames`: each group the model dropped, with its time range, the model's reason and a 480 px thumbnail in `assets/dropped/`. A wrong drop is the one model error that otherwise leaves no trace. The handbook parser stops before this section.

`assets/` holds `frame_NNN` at native resolution, `view/frame_NNN.jpg` at up to 1568 px for model reading, and `transcript.json` with all segments.

**Cleanup:** after a successful write, decoded audio and full frames are deleted from `.work/`. Low-resolution copies of the samples stay in `archive/` there, along with the cached selection, so selection can be redone without the video.

## 12. Model providers

`VisionProvider` has `generate(images, prompt)`, `json()` (with a tolerant parser that handles fences and chatter), `close()` and `provenance()`. `get_vision_provider(section, cfg)` builds one from a config section. `release_all()` closes them all between stages.

| Backend | Use | Local |
|---|---|---|
| `openai` | Any OpenAI-compatible server, local or remote | If the URL is this machine |
| `mlx-vlm` | In-process, on macOS | Yes |
| `ollama` | Ollama server | Yes |
| `api` | Any model through LiteLLM | No |
| `agent` | Subagents of an agent session | No |
| `mock` | Tests | Yes |

With `manage_lms`, the `openai` provider loads and unloads models through the `lms` CLI, passing context length and parallel slots. Parallel slots share memory, and requests are sequential, so use one. It strips reasoning blocks (`</think>`, `<thought>`) and records `finish_reason`. Before loading, it compares the model's size with the memory the system can hand out without swapping. A model that will not fit stops the run at once with `NotEnoughMemory`, naming the largest apps, instead of loading into swap: in testing, a model that only partly fitted ran several times slower and the server dropped requests after 600 s without output. Callers that fall back to another model on a load error let this one through. `memory_check: false` turns it off, and a model already loaded is not checked. A local timeout is required, because a stalled local server otherwise blocks a lecture for its full timeout.

**API chains** (`providers/chain.py`, `providers/quota.py`). A role can name a chain instead of one model: `backend: chain` with `chain: [<provider>/<model>, ...]`. The `providers:` section holds each provider's URL, its key (an environment variable, or `api_key_file` such as `~/.google_key`), the time zone its day resets in, whether its free tier may train on what it is sent, and each model's limits (`rpm`, `tpm`, `rpd`, `context`) and settings (some models take no `reasoning_effort`). The defaults include one provider's free-tier models as an example. Free-tier limits are per account or project, not per library, so a ledger in `~/.config/knowlib/usage.json` counts each model's requests and tokens across runs and processes. A request goes to the first model with quota left: it waits when the minute's budget is spent, skips a model whose day is spent, and on a 429 reply reads the provider's quota details (a daily quota spent marks the model until its reset and records the real limit; a per-minute one waits and asks again). A server error moves on to the next model; a request no model can take fails like a server failure. Caches look a prompt up under every model in the chain and store a reply under the model that wrote it, so changing a chain never redoes finished work. The review asks about one note per request by default. It can pack several (`notes_review.batch_notes`) up to the first model's budget (80% of the smaller of `tpm` and `context`), but accuracy falls with each note added: with planted errors, the reviewer caught 12 of 16 one note at a time, 9 three at a time and 5 six at a time. Batch only to stretch a small daily quota. A batch reply whose JSON is broken keeps every complete verdict (`salvage`); each note's answer is cached under its own prompt, and notes a batch misses are asked one by one. The digest packs up to four book parts per request (`digest.batch_parts`), each part's digest after its own marker line. `knowlib models` shows each chain and the requests left today.

**Privacy.** Personal information never leaves the machine and never reaches written output. `privacy.py` finds emails, phone numbers and the names listed in `privacy.names`; before a figure goes to a remote model, classic OCR on this machine reads it and a figure showing any of these stays local (flagged for the agent to read); the handbook brief is redacted ("[a person]") and its rules forbid naming anyone; `handbook --finish` checks the finished text and, by OCR, every image it shows, and lists each finding under "To check". Transcription runs on this machine.

**Independence.** The writer's model may not review its own notes: `knowlib models` and `review-notes` refuse a review chain that contains the model in `notes_writer`, and warn when a reviewer is of the same model family. `knowlib check-figures` has a second vision model, set in `pdf.figures.second_model` and best of another family than the reader, reread the figures classic OCR flagged and the tables; it compares tables row by row (OCR compares numbers as a bag, so a right number in the wrong row passes) and other figures by their numbers, and writes the disagreements to `work/figures/crosscheck-<id>.md` for QA. Second readings are cached beside the first, one at a time by default.

**Roles** (`models.py`). A library has five model roles, each resolved from the config the way the code that runs it builds its provider: reading (`frame_selection` with its `caption_model`, which also reads figures and images), digest (`frame_selection` plus `digest`), writer (`notes_writer`, recorded for the agent, which writes the notes), review (`frame_selection`, `caption_model`, then `notes_review`) and handbook (`frame_selection` plus `handbook`). `knowlib models` writes a role's provider into only the keys that role reads, keeping the other settings and a backup of the config. A check lists the server's models and confirms the one chosen is served, that a reading model takes images, and, in full, that the server does not cap image tokens (a 1600x1200 test page against a copy at half size) and that the digest's parts fit the context. Before a run, the roles that command uses are checked quickly; a failure stops it, and a choice changed since the last confirmed run is asked about at a terminal (fingerprints in `work/models_confirmed.json`). The pattern the roles encourage: a fast server reads, a session model writes, and a stronger one reviews.

`reasoning_effort: none` is available on this provider. In testing it kept the numbers on 10 of 11 frames and saved about a quarter of the reading time, with small losses of on-screen text. It is off by default.

### Session reader

`knowlib process --reader session` sets the reader to the `agent` backend, with no API key.

- `AgentProvider.generate` hashes the model, the full image paths and the prompt into a job id. If `.work/agent/answers/<id>.md` exists, it returns it. Otherwise it records the job and raises `AgentPending`.
- Callers skip that frame without caching anything.
- After a stage, `stop_if_waiting` writes `.work/agent/jobs.json` and raises `AgentJobsWaiting`. The runner reports the item as waiting, not failed.
- A subagent per model reads each job's images, follows its prompt, and writes the answer file.
- The same command then continues. Decisions and readings are cached, so it stops once more for hard captions and then finishes.

Files suit this because answers can arrive out of order, a run resumes at any point, and there is no JSON escaping to get wrong.

A smaller session model reads ordinary frames. A stronger one reads `table`, `chart`, `code`, `whiteboard`, `diagram` and `demo`, and writes hard captions. The fast local model still decides keep or drop, and writes easy captions. Provenance is recorded under `pipeline.reader`. `local_only` refuses this backend. The `knowlib handbook` command does not take `--reader`, so ingest with `process` first.

## 13. After ingestion: the agent

knowlib stops at clean sources. `templates/CLAUDE.md` tells the agent what comes next.

1. **QA.** Read `last_run.json`, the flags, three transcript passages, and every image a flag names. Fix through config and reprocessing, never by editing `source.md`.
2. **Synthesize.** Write topic notes in `notes/` with a citation on every claim, in the form `[@<source-id>, slide 12, 14:32]`. Record disagreements instead of picking a side. A book is synthesized from its digest, a chapter at a time, reading only the pages the digest points to. Update `index.md` and `glossary.md`, run `knowlib check-notes` and `knowlib review-notes`, then `knowlib mark-synthesized`.
   **Checks on notes** (`notecheck.py`). Notes are where sources merge, one note per concept, and nothing generates them by code, so three checks keep them honest. *Citations:* the source exists; the page (from the page markers), slide (with its time range), passage time or section exists; and the claim's numbers (compared as values, so 8.5% matches 8.50 and 1,000 matches 1000; whole numbers under 10 are skipped) and quotations of four words or more appear there. A miss names the neighbouring page if the text is there. Claims of four words or more with no citation are listed; the summary before the first `##` heading is not checked. A claim citing several sources is checked for its places only. *Duplicates:* notes sharing a title or alias after normalisation, notes sharing half their citations, notes with 60% of their wording in common, and claims written in two notes. *Consolidation:* every source marked synthesized is cited; `sources:` matches the citations; every note is in `index.md`; `[[links]]` resolve; a note older than a reprocessed source it cites; and sources that mention the note's title or aliases three times or more without being cited, the leads where another book or deck has not been merged in. HTML comments are ignored. The command exits with 1 on errors and writes `.work/notes_check.json`.
   **Review by a second model** (`notereview.py`). The checks above match numbers and quotations but cannot tell a faithful paraphrase from a wrong one. `knowlib review-notes` sends each note, with the passages its claims cite (each passage once, cut at 8,000 characters), to a model other than the writer, which judges every claim against those passages only: supported, partly (overstated, too general or too exact), unsupported, or unclear. Unsupported is an error, partly and unclear are warnings. The reviewer is configured like any model stage: `notes_review` overrides `frame_selection` and its `caption_model`, so by default the strong local model reviews, and setting `openai_model` picks another. Pick a reviewer of another family than the readers and the writer, so it does not share their blind spots. `backend: agent` with `agent_model: <model>` sends the review to a session subagent through the same jobs file as the session reader. The manifest records `local_only` for every source ingested under it, and keeps it on re-ingestion; a claim citing such a source is never sent to the session and is reported as not reviewed. Replies are cached in `.work/notes_review_cache.json`, so only changed notes are reviewed again, and the findings go to `.work/notes_review.json`.
3. **Research.** Answer from notes, drilling into sources with `rg`, and keep sources separate from reasoning.
4. **Handbook.** See [handbook-design.md](handbook-design.md).

## 14. Privacy

`local_only` can be set for the library, a session or a run. It blocks the `api` and `agent` backends, a non-local `openai` URL, and the Jina web fallback. The default backends are all local, so nothing leaves the machine unless a config selects otherwise. An agent reading sources in a session is a separate matter: its procedures keep `local_only` sources away from external services.

## 15. Commands

| Command | Purpose |
|---|---|
| `knowlib init [DIR]` | Create a library |
| `knowlib doctor` | Check tools, packages and backends |
| `knowlib models` | Show, change (`--use role=spec`) and check (`--check`) the model for each role: reading, digest, writer, review, handbook |
| `knowlib process` | Ingest new or changed material. Flags: `--input`, `--layout`, `--only`, `--force`, `--dry-run`, `--limit`, `--local-only`, `--set`, `--reader` |
| `knowlib status` | Counts, failures and live progress |
| `knowlib qa` | All flags across sources |
| `knowlib check-notes` | Citations, duplicates and consolidation of the notes, with `--only` and `--notes` |
| `knowlib review-notes` | A second model, the review role, judges each claim against its cited passages, with `--notes` and `--set` |
| `knowlib reprocess` | Mark matching sources stale and re-ingest, with `--where` or `--ids` |
| `knowlib pending` | Sources not yet synthesized |
| `knowlib mark-synthesized ID...` | Record synthesis |
| `knowlib handbook` | Handbooks, with `--prepare`, `--finish`, `--input`, `--only`, `--output` |
| `knowlib digest` | A finding aid per long PDF, with `--ids`, `--force`, `--reader session`, `--agent-model`, `--local-only`, `--set` |
| `knowlib transcribe FILE` | Transcribe one file, for testing |

## 16. Tests

`tests/test_pipeline.py` runs end to end on generated fixtures with the mock provider. `tests/test_units.py` covers individual rules: IDs, grouping, alignment, OCR comparison, selection, handbook checks and the session reader. `tests/test_books.py` covers long PDFs on a generated six-page book: printed page markers from both backends, formula text from its box, the outline and captions, chapter splitting, loop detection, a digest written through the session reader, and a looped part read again in pieces by the fallback model. `tests/test_notes.py` builds a three-source library by hand and plants each problem the note checks look for, and checks that correct notes pass, then runs a review through the session reader and the `local_only` guard. `tests/test_inplace.py` covers the in-place layout, setup, and a folder of screenshots read as one source and cited by image.

```bash
.venv/bin/python -m pytest -q tests
```

Any change to selection rules should also be tested on real frames. Unit tests cannot show that a model invented text.

**Evaluating models.** Pick about 8–11 frames covering a clean slide, a table, a build-up slide, a chart, and busy screens. Run each model with the reading prompt, then compare against the images by hand, checking every number. Record time per frame and peak memory.

## 17. Rebuilding from nothing

1. Install Python 3.11 or newer, ffmpeg, Pandoc, Tesseract with language data, and a local vision model server. `scripts/install-mac.sh` does this on macOS.
2. Install the package with `pip install -e ".[docs,dev]"`, plus the extra for your platform (`mac` or `linux`).
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
- Video ingestion with local models is slow on consumer hardware: time follows slide count, at minutes per slide with a strong model.
- `--only` is case-sensitive in `process` and case-insensitive in `handbook`.
- The session reader needs a person to start it, and has been tested on one short lecture.
- Web pages with almost no content, and CSV files with an undetectable delimiter, fail ingestion.
- Lectures in other languages and mixed languages have not been tested end to end.
- Book digests have been tested on one 654-page textbook. A small fast model looped on about a quarter of the parts, so a digest needs the fallback model and can take hours locally.
- Formulas in PDFs keep their symbols but not their layout (fractions, sub- and superscripts).

## Appendix A: default configuration

Every key and its default is in `src/knowlib/config.py` (`DEFAULTS`), with a comment on each. `knowlib init` copies `config.example.yaml` beside the library's `config.yaml` for reference, and the config holds only what setup asked or the user changed. Model ids are never assumed: setup and `knowlib models` fill them in per library.

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

### Book digest, one part

```text
Below is {where} of the textbook "{title}". Each page begins with a line such as "=== Page 52 ===", the page number printed in the book; everything up to the next such line is on that page.

Write a digest that a writer will use to find the right pages, not to replace them. Use only what the text says. End every item with the page it is on, read from the nearest "=== Page" line above it.

Use exactly these headings:

### Key terms
- **Term** (p. N): what the book says it is, in one sentence close to its wording.

### Formulas
- **Name** (p. N): the formula as the book writes it, then what its symbols mean.

### Worked examples
- **Company or setting** (pp. N–M): what it shows and the main numbers it reaches.

Leave out tables, figures and section headings: they are listed separately. Write "(none)" under a heading with nothing in it.

Text:

{text}
```
