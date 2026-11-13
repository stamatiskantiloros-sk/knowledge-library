# knowlib

knowlib turns a folder of mixed material into a Markdown knowledge library that an agent can use for demanding academic and professional work, citing every claim. The material can be PDFs and books, Office files, web pages, screenshots, audio and recorded lectures. From that library it also writes study handbooks: one checked, illustrated book per lecture.

What it is built around is not extraction but **adversarial review**. Every output is checked by something that did not produce it:
- a slide's text, read by a vision model, is compared with classic OCR;
- a note's numbers are checked by code against the cited page;
- a note's claims are judged by a second model from another family, which is never the writer;
- a handbook's sentences are reread against the transcript passage they come from;
- a redrawn figure is compared with its slide, then approved by you.

Disagreements are never resolved by the producer. They become visible flags, and the strongest reader available settles each one against the original. See [docs/adversarial-review.md](docs/adversarial-review.md).

```
your folder ──> knowlib: ingest ──> <file>.md beside each original, frames, page images, flags
                                         │
                       agent: QA ──> notes with citations ──> check-notes ──> review-notes (another model)
                                         │
                       agent: handbook ──> knowlib finish: numbers, tables, hedges, privacy ──> PDF
```

Scripts do the mechanical work: conversion, extraction, alignment, deduplication and every check code can make. Models make the judgement calls. Every model is chosen per role in the config, and none is hard-coded. Confidential material can be kept entirely on the machine.

## Principles

1. **Markdown is the source of truth.** Everything durable is a readable file, so any tool can be replaced without losing the library.
2. **Your originals are never edited, moved or deleted.**
3. **Every claim cites its source**: a page, a slide and time, a section, or an image (`[@source-id, slide 12, 14:32]`).
4. **The producer never grades itself.** The writer and the reviewer are different models, and code checks what code can check.
5. **The original decides.** Images, transcripts and printed pages outrank captions, summaries and digests.
6. **Doubt stays visible.** Flags, "To check" lists and correction logs, never a quiet fix.
7. **Provenance on everything.** Each source records the tool and model behind each stage, so it can be redone when something better arrives.
8. **Everything resumes.** Expensive stages are cached, so an interrupted run continues where it stopped.
9. **Privacy is enforced in code.** `local_only` blocks every route off the machine. Before any prompt goes to a model off the machine, emails, phone numbers and listed names are replaced; before any image goes, local OCR reads it, and one showing personal information stays local. Personal information is also removed from everything written. Only with that check in place may a library allow hosts that keep or train on data (`privacy.models_may_keep_data`).

## Install

Requirements: Python 3.11 or newer, `ffmpeg`, `pandoc`, `tesseract` with the language data you need, Chrome or Chromium for PDF export, and `ripgrep` for search. LibreOffice (slide images from `.pptx`) and Typst or Graphviz (redrawn figures) are optional.

```bash
cd knowledge-library
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[docs]"            # core and document conversion
pip install -e ".[docs,mac]"        # or: with the in-process speech-to-text, vision and OCR backends for macOS
pip install -e ".[docs,linux]"      # or: with the speech-to-text backend for Linux
pip install -e ".[api]"             # optional: API models through LiteLLM
pip install -e ".[figures]"         # optional: charts for redrawn figures

knowlib init ~/Documents/MyCourse   # asks a few questions, or takes them as flags (--yes for defaults)
knowlib doctor -L ~/Documents/MyCourse
knowlib models -L ~/Documents/MyCourse --check
```

`scripts/install-mac.sh` does all of this on macOS.

**Models.** knowlib needs a vision model for reading and a text model for digests. Any of these will do:
- an OpenAI-compatible server, local or remote;
- an in-process backend;
- an API model through LiteLLM, or any model on OpenRouter (`openrouter:<vendor>/<model>`);
- subagents of an agent session.

Speech-to-text runs locally. Choose models by testing them on your own material (see [Model roles](#model-roles)).

**Two layouts.**
- **In place**, the default for a folder that already holds material, uses your folder as it is. knowlib writes each file's Markdown beside it (`inventory.pdf` → `inventory.pdf.md`), keeps its own files in `_knowlib/`, and puts `notes/`, `INDEX.md`, `glossary.md` and `AGENTS.md` at the top.
- **Separate** keeps originals in `inbox/` and each source in `sources/<id>/`.

## Add material

| You add | It becomes |
| --- | --- |
| `.pdf` | text with printed page numbers. Each chart, diagram or table-as-picture is cropped, read by the vision model and checked by OCR. Scanned pages are OCR'd and flagged. |
| `.docx .pptx .xlsx .odt .rtf` | an Office source; `.pptx` slide by slide (image, text, speaker notes) |
| a folder of images (`.png .jpg .webp .tif`) | one source, one section per image, read by the vision model and checked by OCR |
| `.html`, `urls.txt`, `.url`, `.webloc` | a web source with its URL and fetch date |
| an audio or video file | a lecture: transcript, and the slides it shows, slide by slide |
| a folder with one video and audio files | one lecture: the video for the slides, the best audio for the transcript, aligned |
| a folder with a `session.yaml` | one lecture, configured by the sidecar |

`session.yaml` (all keys optional):

```yaml
title: Corporate Finance - Lecture 4
date: 2026-10-28
language: en                   # set explicitly; mixed-language lectures need it
glossary: [WACC, CAPM]         # seeds the transcription's spelling of terms and names
primary_audio: recorder.wav    # the best recording; otherwise chosen by estimated signal-to-noise
slides: slides.pdf             # the deck, ingested too and linked from the lecture
parts: [part1.m4a, part2.m4a]  # one lecture recorded in parts, joined in order
local_only: true               # nothing from this lecture leaves the machine
tags: [finance]
```

## Use

```bash
knowlib process -L DIR              # ingest new or changed material (resumable)
knowlib status -L DIR               # live progress, from another shell
knowlib qa -L DIR                   # every flag, for the QA pass
knowlib pending -L DIR              # sources not yet synthesized into notes
knowlib check-notes -L DIR          # citations, numbers, quotations, duplicates, consolidation
knowlib review-notes -L DIR         # a second model judges every claim against its cited passages
knowlib check-figures -L DIR        # a second vision model rereads flagged figures and tables
knowlib digest -L DIR               # a finding aid per book, never cited itself
knowlib handbook -L DIR --prepare --output OUT --only "<lecture>"   # the brief for one lecture's handbook
knowlib handbook -L DIR --finish --output OUT                       # check the draft, write the book and the PDF
knowlib figures make OUT/<lecture>/figures   # redraw slides in the handbook's style, for your approval
knowlib style -L DIR                # pick the handbook's look in the browser
knowlib reprocess -L DIR --where transcription.model=<old>   # redo what an older model produced
```

After ingestion an agent takes over, following `AGENTS.md` in the library. It has three procedures:
- **process inbox:** ingest, QA every flag, write topic notes with citations, then check and review them;
- **research:** answer from the notes, drilling into sources;
- **handbook:** write one lecture's book from its brief, then check it.

## Model roles

Each library has five roles: **reading** (slides, frames, figures, images), **digest** (books), **writer** (topic notes), **review** (checks every claim) and **handbook**. `knowlib models` shows and changes them:

```bash
knowlib models -L DIR                                    # show; at a terminal, asks role by role
knowlib models -L DIR --use reading='server:https://host/v1|<model>|<KEY_ENV>'
knowlib models -L DIR --use review=chain:<provider>/<model>,<provider>/<model>
knowlib models -L DIR --use writer=session:<model>
knowlib models -L DIR --check                            # served, takes images, full image detail, context fits
```

A provider spec is one of:
- `server:<url>|<model>[|<env var with the key>]`, any OpenAI-compatible server;
- `lmstudio:<model>`, a local server that knowlib loads and unloads;
- `openrouter:<vendor>/<model>`, a model on OpenRouter (key in `OPENROUTER_API_KEY` or `~/.openrouter_key`). Requests go only to hosts that neither store nor train on prompts, and fail when the model has none, unless the library sets `privacy.models_may_keep_data: true`;
- `chain:<provider>/<model>,...`, API models tried in order within their quotas (`openrouter/<vendor>/<model>` works here too);
- `session:<model>`, a subagent of the agent session;
- `default`.

The review role must not be the writer. knowlib refuses that set-up, and warns when the two are the same model family. The pattern the roles encourage: fast models read, a careful model writes, and a different and preferably stronger model reviews. Before each run, knowlib checks that the models it needs answer, and stops with the fix if one does not.

Everything else is in the library's `config.yaml`; all keys and defaults are in `src/knowlib/config.py`. Speech-to-text, PDF conversion, OCR engines and web extraction each have more than one backend. Any stage can be overridden per lecture in its `session.yaml`.

## What a lecture looks like

```markdown
## Slide 12 (14:32–16:05)
![Slide 12](assets/view/frame_012.jpg) · [full resolution](assets/frame_012.jpg)
> **OCR check:** the vision model's text and classic OCR disagree on numbers (8.5 / 8.3). Verify against the image.

**On slide:**
WACC = E/V × Re + D/V × Rd × (1 − Tc) ...

**Figure:** formula slide defining WACC, with a worked example.

**Spoken:**
[14:32] So the weighted average cost of capital ...
```

Frames are kept at native resolution. Dropped frames are listed at the end with thumbnails and the model's reason, so a wrong drop leaves a trace.

## Handbooks

A handbook is one book per lecture, for a student who reads it instead of attending:
- **Preparing:** knowlib writes a brief: every slide, everything said, the closest course passages and the writing rules.
- **Writing:** an agent writes the draft from the brief. It opens the image of every flagged, table, formula or chart slide, and logs each correction.
- **Finishing:** knowlib checks the draft and writes the PDF. It checks for numbers no source supports, uneven tables, lost table rows, sentences more absolute than their source, and personal information in the text and images.

  The PDF has two columns in the library's theme. Its figures are redrawn in vector form and searchable, and it is named after the lecture and its date.

The writing rules keep the lecturer's hedges, scope and qualifiers, give both sides where the lecturer and a document disagree, and leave out the class's chatter and anyone's name. See [docs/handbook-design.md](docs/handbook-design.md).

## Documentation

- [docs/adversarial-review.md](docs/adversarial-review.md): the checks, what produces and what checks each output, the evidence behind them, and what they cannot catch.
- [docs/system-design.md](docs/system-design.md): how ingestion works and why, in enough detail to rebuild it.
- [docs/handbook-design.md](docs/handbook-design.md): the handbook creator.
- `src/knowlib/templates/CLAUDE.md`: the agent procedures written into each library as `AGENTS.md`.

## Code map

```
src/knowlib/
  cli.py           the knowlib command
  config.py        defaults, config.yaml, per-lecture overrides
  contract.py      source ids, hashing, atomic writes, provenance
  manifest.py      what was processed, how, and whether it was synthesized
  router.py        turns folders into units of work
  runner.py        process: incremental runs and the last_run.json report
  layout.py        where sources, assets and caches live (in place or separate)
  setup.py         knowlib init
  models.py        the model per role, the checks before each run
  privacy.py       personal information: found, removed, reported
  digest.py        book digests
  notecheck.py     checks on topic notes
  notereview.py    the second-model review of every claim
  ingestors/       pdf, office, web, text, images, media
  media/           sessions, alignment, transcription, frames, selection, OCR
  providers/       one interface over every model backend
  handbook/        briefs, checks, figures, themes and the PDF
  templates/       config, agent procedures, index, glossary, style picker, figure style
```

To add a source type, write `ingestors/<name>.py` with an `@register`'d `Ingestor` that writes through `SourceWriter`.

## Tests

```bash
pip install -e ".[dev,docs]" && python -m pytest -q tests
```

The suite generates its own material: a short lecture with slides, a separate recording with an offset, a scanned deck, PDFs, Office files, a web page and a short book with printed page labels. It runs the whole pipeline with mock models, so it needs only `ffmpeg`. The planted-error tests check that each note check and the review catch what they should, and that correct notes pass.

## Status and limits

Tested end to end on English slide lectures through the handbook route, and on a 650-page textbook through ingestion and the digest. Not yet tested: other and mixed languages, filmed projectors, and alignment of a real camera and recorder pair. On screen recordings, OCR flags most slides, mostly false alarms. Known limits are listed in [docs/system-design.md](docs/system-design.md) and [docs/handbook-design.md](docs/handbook-design.md).
