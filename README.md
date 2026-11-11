# knowlib — Knowledge Library pipeline

Turns a folder of mixed materials (PDFs, Office files, web pages, audio, video) into a Markdown knowledge library that a frontier model can use for demanding academic and industry work. See [docs/system-design.md](docs/system-design.md) for the full design.

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

knowlib init ~/Documents/MyCourse      # asks a few questions; use the folder in place, or make a separate library
knowlib doctor -L ~/Documents/MyCourse # shows what is installed for the configured backends
```

**Two layouts.** *In place* (the default for a folder that already holds material) uses your folder as it is: knowlib crawls every subfolder and writes each file's Markdown beside it (`inventory.pdf` -> `inventory.pdf.md`), keeps images, caches and its config in a visible `_knowlib/` folder, and puts `notes/`, `INDEX.md`, `glossary.md` and `AGENTS.md` at the top. *Separate* keeps originals in `inbox/` and each source in `sources/<id>/`. Setup asks which, which folders to skip, whether the material is confidential, the language, and which model does each job (see section 5); every question also has a flag (`knowlib init --help`), and `--yes` takes the defaults.

First run downloads Whisper large-v3-turbo (~1.6 GB). Vision and text models run in LM Studio (`frame_selection.backend: openai`, `manage_lms: true`): knowlib loads and unloads them by id, so load each one once in LM Studio under the id in `config.yaml`. The tested setup is a fast 9B model for decisions and digests and Qwopus 27B for reading frames, captions and rereading looped digest parts.

## 2. Add material

In place, the material is already where it belongs: knowlib reads every subfolder and never edits, moves or deletes an original. In a separate library, drop files into `inbox/` (never edited or moved by the pipeline). Subfolders are fine either way.

| You drop | It becomes |
| --- | --- |
| `.pdf` | PDF source with printed page numbers; each chart, diagram or table-as-picture cropped and read by the vision model, its text placed on its page (logos, icons and photos skipped); scanned pages OCR'd and flagged |
| `.docx .pptx .xlsx .odt .rtf` | Office source; `.pptx` slide by slide (image + text + speaker notes) |
| loose images (`.png .jpg .webp .tif`) | one source per folder (`Ch12 Qs/Ch12 Qs.images.md`), a section per image, in file-name order: the vision model's transcription (tables, every option of a quiz question and which one is marked) checked against classic OCR; `images.describe: false` uses OCR only |
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
primary_audio: recorder.wav    # the best recording; otherwise chosen by estimated SNR
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
knowlib status -L ~/KnowledgeLibrary             # live progress of a run, from another shell
knowlib qa -L ~/KnowledgeLibrary                 # every flag, for the QA pass
knowlib check-notes -L ~/KnowledgeLibrary        # citations, duplicates and consolidation of notes
knowlib review-notes -L ~/KnowledgeLibrary       # a second model checks each claim against its pages
knowlib pending -L ~/KnowledgeLibrary            # sources not yet synthesized into notes
knowlib digest -L ~/KnowledgeLibrary             # finding aid for each book (PDF of 100+ pages)
knowlib handbook --prepare --output ~/Handbooks --only "<lecture>"   # brief for a study handbook
knowlib handbook --finish --output ~/Handbooks   # check the draft, write handbook.md and .pdf
```

Long runs show a progress bar per stage with the time left; in a log file the same line is written every 30 seconds.

Then, in Claude with the library folder connected: **"process inbox"**. The agent follows `CLAUDE.md`: runs `knowlib process`, does QA, writes topic notes with citations (`[@source-id, slide 12, 14:32]`), updates `index.md` and `glossary.md`, and marks sources synthesized. For questions and work, it follows the **research** procedure: index → notes → sources. For a **handbook**, the agent writes the book from the brief in the session; see [docs/handbook-design.md](docs/handbook-design.md). `--reader session` hands frame reading or digest parts to Claude subagents instead of local models (never for `local_only` material).

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

Each library has five model roles: **reading** (slides, frames, figures, images), **digest** (books), **writer** (topic notes, written by an agent), **review** (a second model checks every claim) and **handbook**. `knowlib models` shows and changes them, and setup asks for them:

```bash
knowlib models -L ~/Documents/MyCourse                     # show; at a terminal, asks role by role
knowlib models -L ~/Documents/MyCourse --use writer=session:sonnet --use review=session:opus
knowlib models -L ~/Documents/MyCourse --use reading='server:https://gpu-box.example/v1|qwen3-vl|GPU_KEY'
knowlib models -L ~/Documents/MyCourse --use review=chain:google/gemini-3.5-flash-lite,google/gemini-3.1-flash-lite
knowlib models -L ~/Documents/MyCourse --check             # servers answer, vision without an image-token cap, context fits
```

A provider is `server:<url>|<model>[|<env var with the key>]` (any OpenAI-compatible server), `lmstudio:<model>` (LM Studio on this machine, loaded and unloaded by knowlib), `session:haiku|sonnet|opus` (a Claude model through the Claude Code session) or `default`. The reviewer should not be the writer, and is best the strongest model available. Before `process`, `reprocess`, `digest` and `review-notes`, knowlib prints the models the run uses, checks they answer, stops with the fix if one does not, and asks again only when a choice changed (`--no-model-check` skips this).

Everything else is in `config.yaml` (full list with defaults: `src/knowlib/config.py`). Examples:

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
  layout.py        where sources, assets and caches live: in place or separate
  setup.py         knowlib init: the questions, the config, AGENTS.md and the index's sources table
  models.py        knowlib models: the provider per role, the checks, the check before each run
  progress.py      progress bars, .work/progress.json for `knowlib status`
  digest.py        book digests: chapter parts, loop check, fallback reread
  notecheck.py     checks on topic notes: citations, duplicates, consolidation
  notereview.py    a second model reviews each claim against its cited passages
  cli.py           knowlib command
  ingestors/       base (interface + registry), pdf, office, web, text, images, media
  media/           av, session, align, transcribe, frames, select, ocr
  providers/       model layer: openai-compatible (LM Studio, llama.cpp, vLLM), mlx-vlm, ollama,
                   api (LiteLLM), agent (Claude Code session reader), mock
  handbook/        handbook briefs, checks and export; the local writer
  templates/       config.yaml, CLAUDE.md, index.md, glossary.md
```

Adding a source type: write `ingestors/<name>.py` with an `@register`'d `Ingestor` (`can_handle`, `ingest`) that writes through `SourceWriter`, and import it in `ingestors/__init__.py`.

## 8. Tests

```bash
pip install -e ".[dev,docs]" && .venv/bin/python -m pytest -q tests
```

The suite generates its own material (a 2-minute lecture with slides, a separate better recording with a 2.3 s offset, a scanned slide deck, PDFs, Office files, a web page, a six-page book with printed page labels) and runs the whole pipeline with mock models and the session reader, so it needs only ffmpeg. 83 tests pass.

## 9. Status

Tested on the Mac (M4, 24 GB): English slide lectures through the full handbook route, and one 654-page textbook through ingestion and the digest. Not yet tested: Greek and mixed-language lectures, camera-filmed projectors, and alignment on a real camera and recorder pair. Known limits and test results are in [docs/system-design.md](docs/system-design.md) (section 18) and [docs/handbook-design.md](docs/handbook-design.md) (sections 10, 12 and 15).
