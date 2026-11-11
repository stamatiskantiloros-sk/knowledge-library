# Handbook creator: design

Status: design final, 2026-11-01. Everything here is built and tested on one lecture except where a section says **Not built**.

This document is meant to be enough to rebuild the handbook creator from nothing. The code is the reference for exact details: `src/knowlib/handbook/maker.py`, `src/knowlib/media/select.py` and `src/knowlib/ingestors/media.py`.

## 1. Purpose

A handbook is one book for one lecture. It is the study material for a student who has no time to go through everything a course contains, so it records what the professor taught, because that is what was covered in class and what the professor considers important.

Requirements, in order of priority:

1. **Correct.** Accuracy comes before speed and cost.
2. **Faithful.** The book follows the lecture in order and gives each topic the space it got in class.
3. **Self-sufficient.** A student can read it instead of watching.
4. **Deeper only when critical.** Other course material enters only when the student cannot follow without it.
5. **Auditable.** Every correction, added note and open doubt is recorded.

## 2. Principles

These came from testing. Section 12 gives the evidence.

- **The lecture is the spine.** Course materials never set the structure or the content of the body.
- **Different models for different jobs.** A local model reads video. The best available model writes and checks.
- **The image outranks the text.** Machine readings of slides are drafts, and the frame decides.
- **Code checks what code can check.** Numbers, table shapes and quotes are verified mechanically. The writer's own confidence is not evidence.
- **Keep the lecturer's words, even when wrong.** An error stays in the text and gets a labelled note.
- **Anything added is labelled.** Text not from the lecture sits in a "Note (not from class)" line with its source.
- **Summaries find things, they do not replace them.** Anything used in the book is read from the original.

## 3. Pipeline

```
 lecture video + subtitles ─┐
                            ├─ 1. INGEST (local) ──> sources/<id>/source.md + frames
 PDFs, web, office, books ──┘                                  │
                                                               │
 books only: 1b. DIGEST (local 9B, once) ──> digest.md         │
                                                               ▼
                            2. PREPARE (knowlib) ──> <output>/<lecture>/brief.md
                                                               │
                            3. WRITE (Opus, in a Claude Code session)
                               reads brief, frames, materials
                               writes draft.md + corrections.md
                                                               │
                            4. FINISH (knowlib) ──> handbook.md, review.md, handbook.pdf
                                                               │
                            5. Resolve "To check", finish again
```

| Stage | Who runs it | Where | Cost |
|---|---|---|---|
| Ingest | knowlib, fast and strong models | Local | Time only |
| Digest (books) | Fast local model, or a Sonnet subagent | Local, or session | About 30 minutes per 100 pages locally |
| Prepare | knowlib | Local | Seconds, plus any new ingestion |
| Write | Opus 5.5 agent | Session | Subscription limits |
| Finish | knowlib | Local | Seconds |

No API key is needed. Writing needs someone to start it in a session, so it does not run unattended as ingestion does. An unattended version would call the Anthropic API through the same brief; estimated cost is under $1 for a five-minute lecture, $2–5 for 90 minutes, and $50–150 for a 24-hour course, at $4 / $20 per million tokens. These are estimates, not measurements.

## 4. Stage 1: ingestion

Ingestion belongs to knowlib and knows nothing about handbooks. Its only contract with the handbook is `source.md` (section 5).

**Models.** An OpenAI-compatible local server (LM Studio here, at `http://localhost:1234/v1`) serves two vision models, and knowlib loads and unloads them with the `lms` CLI (`manage_lms: true`). One model is in memory at a time, so ordinary laptops can run it.

- **Fast model:** decides keep or drop. Each dropped frame is listed at the end of `source.md` with a thumbnail and the model's reason.
- **Strong model:** reads frames and captions the hard slides. Tested with a 16K context, `parallel: 1` and a 450 s timeout.

Exact model ids live in `config.yaml`. Choose models by testing them on your own frames (section 12).

**Lecture steps:**

1. Transcript: the lecture's own `.srt`/`.vtt` if present, otherwise Whisper (`mlx-community/whisper-large-v3-turbo`).
2. Frames at 1 fps with ffmpeg at native resolution.
3. Group frames by pHash (threshold 6) with SSIM as a backup check (0.985).
4. The fast model decides keep or drop for every group, before any reading. The prompt asks only whether a frame has content, and says samples are expected to look alike.
5. Release the fast model. The strong model reads the richest sample of each kept group, chosen by OCR word count. A reply cut off by the token limit is discarded. A failed reading falls back to raw OCR, and the slide is marked `On slide (OCR)`.
6. Code decides what supersedes what: a slide, code or whiteboard frame that keeps at least 90% of the previous frame's text replaces it. Then repeats are merged.
7. OCR cross-check with Apple Vision and Tesseract. A slide is flagged only when both disagree with the model.
8. Captions: the strong model for every slide in the tested library (`caption_model_types: all`). The generic default gives it only tables, charts, Markdown tables and flagged slides, and the fast model the rest. A stalled strong model falls back to the fast model. Before the strong model loads, knowlib checks it fits in free memory and stops with the names of the largest apps if it does not.

**Session reader (optional).** `knowlib process --reader session` replaces the strong model with subagents in a Claude Code session, with no API key. Frame decisions stay local. Reading then runs in passes: knowlib lists its jobs in `.work/agent/jobs.json` and stops, subagents write one answer file per job, and the next run continues. Sonnet reads tables, charts, code, whiteboards, diagrams and demos, Haiku reads the rest, and Sonnet writes the hard captions. On 11 test frames Sonnet matched or beat the strong local model, and Haiku missed one chart axis. On a real 3:42 lecture with 26 frame groups, reading and captions took a few minutes against an expected hour or more. It has been tested on that one lecture. It uses subscription limits and sends frames to Anthropic, so `local_only` refuses it.

**Other materials** go through the pdf, office, web and text ingestors. PDF uses Docling with PyMuPDF as a fallback.

**Directories.** `knowlib process --input <folder> --layout "{course}/{date}/{name}"` ingests any folder. Fields: `course`, `folder`, `name`, `date`. The layout is a tree of symlinks in `organized/`. Real data stays in `sources/`, so changing the layout breaks no citation and needs no reprocessing. Keys from `--input` carry the folder name, so same-named files in different courses stay apart.

**Speed.** Time follows the slide count, not the video length: on the test machine, about 4–4.5 minutes per slide. A slide-based 5.5-minute lecture took 72 minutes, and a screen recording of 7.7 minutes took 4.6 hours, because it held four times the slides.

## 5. The contract: source.md

```markdown
---
id: <slug>-<8 hex>
title: ...
flags: ['slide 3 (00:05): OCR disagreement', ...]
---
## Before the first slide            (optional: speech before slide 1)

## Slide 10 (02:36–02:58)
![Slide 10](assets/view/frame_010.jpg) · [full resolution](assets/frame_010.jpg)
> **OCR check:** the vision model's text and classic OCR disagree (...).   (only if flagged)

**On slide:**                        (or **On slide (OCR):** if the reading failed)

**OCR text (apple-vision):**         (flagged slides only; also tesseract)

**Figure:** caption. *(table)*

**Spoken:**

[02:37] transcript with timestamps
```

`parse_source()` turns this into `Slide` objects with `n, start, end, text, caption, spoken, frame, flagged, from_ocr, ocr`. The handbook depends on these labels, so changing them means changing the parser.

`manifest.json` maps each input key to `source_id`, `ingestor` and `status`. A lecture is an entry with `ingestor == "media"`. Every other finished entry is course material.

Lectures sort by a date in the key if there is one (`2026-10-31` or `31-10-2026`), then in natural order, so lecture 2 comes before lecture 10. Undated keys sort after dated ones.

## 6. Stage 2: prepare

```bash
knowlib handbook -L <library> --prepare --output <target> --only "<lecture>" [--input <course folder>]
```

It ingests anything new first. Other videos are skipped unless they match `--only`, and `--only` ignores case but must match the path text, including hyphens. If nothing matches, it stops with an error. Then it writes:

```
<target>/<lecture-slug>/
  .lecture.json      {"folder": source folder, "source_id": ..., "key": ...}
  brief.md
```

`brief.md` contains, in order:

1. The writing rules (section 7).
2. Every slide: full-resolution image path, FLAGGED marker, a marker if the text is raw OCR, slide text, caption, transcript, classic OCR.
3. The six closest course passages, 1,200 characters each, scored by shared words of five or more letters; a passage needs at least five shared words.
4. The path of every ingested course material, to search with `rg`.

## 7. Stage 3: writing rules

This is the text of `BRIEF_RULES`, which heads every brief.

```text
You are writing the study book of one recorded lecture, for a student who will read it instead of watching.

1. Read every slide below in order, then write draft.md beside this brief: a # title, then ## sections in the order taught. The chapter breaks are yours.
2. Open the image of every flagged slide, every slide with a table, formula, chart or diagram, and every slide whose text looks wrong. The image decides. List each correction in corrections.md: slide, what the text said, what the image shows.
3. Stay faithful. Keep the lecturer's definitions, examples and reasoning, and give a topic as much space as it got in class. Remove filler and repetition. Each topic appears once.
4. Write only what the lecture contains: no links ahead, summaries or comments on the method. Attribute to the lecturer only what the transcript says. "What it shows" is an automatic description, never a source of claims.
5. Copy every number, formula and table exactly. Do not compute, count or total anything that was not shown or said.
6. If the lecturer says something wrong, keep it and add a note saying so, plainly.
7. Add a note only where the student cannot follow without it: a line starting "> **Note (not from class):**", one or two sentences, naming its source. The passages below are the closest matches only: search all the course materials, listed at the end, for what the lecture leaves unclear, for conflicts with it, and to verify a note.
8. Write [[slide N]] on its own line where the reader needs an image: charts, tables, diagrams, screens. Use LaTeX for formulas and Markdown tables, with the same number of cells in every row.
9. End each section with its slides and times in italics. Call the speaker "the lecturer", without he or she.

When done, run: knowlib handbook --finish --output <the folder above this one>
```

The writer also produces `corrections.md`, listing each place an image overrode the ingested text. Any recalculation is run as code, not done mentally. During testing a mental sum wrongly suggested the slide's 55.6 was an error, and running it showed the slide was right.

## 8. Stage 4: finish

```bash
knowlib handbook --finish --output <target>
```

It runs on every folder with both `.lecture.json` and `draft.md`, and can be repeated. Checks:

| Check | Rule | Reports |
|---|---|---|
| Unsupported numbers | Every number of two or more characters in the body must appear in the brief. Note lines and `[[slide N]]` are skipped. | "numbers no source supports" |
| Table shape | Every row of a Markdown table has the same number of cells. | "a table has rows of different lengths" |
| Lost tables | Numbers in a slide's Markdown table must be in the text or on a slide whose image is shown. | "table numbers not in the text" |
| Images | Each `[[slide N]]` must be a real slide. | "marker for slide N has no image" |

Number comparison uses `numbers()` from `media/ocr.py`, which normalises Unicode, strips trailing punctuation and reads a comma as a decimal point.

It then writes:

- `handbook.md`: the draft with `[[slide N]]` replaced by images copied into `assets/`.
- `review.md`: To check, Corrections made from the images, Slides the OCR check flagged.
- `handbook.pdf`: pandoc produces standalone HTML with MathML, and headless Chrome prints it. Chrome is at `/Applications/Google Chrome.app`. WeasyPrint is not used because it does not render MathML.

## 9. Output layout

```
<target>/<lecture-slug>/
  brief.md  .lecture.json        input to the writer
  draft.md  corrections.md       written by the agent
  handbook.md  handbook.pdf      the book
  review.md                      what to check, what was corrected
  assets/                        images the book uses
```

A planned standard setup is one folder per course holding materials and lectures, passed with `--input`, and a separate target folder for books. Folder names of the form course and lecture date sort correctly.

## 10. Digest for books

Whole books are too long to pass to Opus for each lecture, and search alone can miss a concept named differently from the lecture. `knowlib digest` writes `sources/<id>/digest.md` once per book; the brief lists it beside the book.

1. **Trigger:** a PDF of 100 pages or more (`digest.min_pages`) whose ingestion found an outline.
2. **From the PDF itself, exact:** the contents and the list of tables and figures, with printed page numbers, from `assets/outline.json`. Ingestion marks every page `<!-- page N (pdf M) -->` with the book's own page label.
3. **From a model, one part at a time:** key terms, formulas and worked examples, each with its page. A part is a chapter, split at its sections when longer than 120,000 characters (about 30K tokens), so the fast 9B model reads it at 64K context.
4. **Loops:** a reply that repeats the same items three times or more is a loop. The repeats are dropped. With `fallback_model` set (Qwopus in the library, at 32K context with reasoning off), that part is read again once the 9B has finished the book and been unloaded, in pieces of 60,000 characters, and the replies are merged under the three headings. A part that still loops carries a warning to search the book. If the fallback cannot load or stalls, the first reply is kept with its warning.
5. **Checks:** items citing a page outside their part are marked ⚠.
6. **Reuse:** replies are cached in `.work/digest/`, and the hash of `source.md` decides when to redo the digest.
7. **Use:** Opus finds pages in the digest, then reads them in `source.md`. A note cites the book and its printed page, never the digest.

`--reader session` hands the parts to a Sonnet subagent instead, never for `local_only` material.

**Test book:** Damodaran, *Applied Corporate Finance* (4th ed.), 654 pages.
- Ingestion with Docling took 8.4 minutes. It found 422 formulas; all were filled from the text layer, which keeps the symbols but not fractions or sub- and superscripts.
- The 9B digest took 3 h 23 min (about 30 minutes per 100 pages).
- Page accuracy, measured by finding each item's name on its cited page: in chapter 3, 26 of 30 items were on the page and 4 one page off; in chapter 8 part 1, 128 of 133 on the page and 5 one off.
- Loops: the 9B looped on 7 of the 29 parts, each time running to the 8,000-token limit, while the longest healthy reply was about 2,000 tokens. Qwopus reread the 7 in 12 pieces in 2 h 14 min, with no loops left.
- Page accuracy across the book: Qwopus's rereads put 249 of 253 items (98%) on the cited page; the 9B's other parts, 400 of 460 (87%), most of the rest one page off. Qwopus also found more per part: 51 items for chapter 10 against 13–26 for a typical 9B part.
- Qwopus reasoning: on half of chapter 10, thinking took 76% of the output (4,870 of 6,379 tokens) and 31 minutes. With `reasoning_effort: none` it took 15 minutes and gave 41 items, all on the right page, against 31.
- Qwopus on a second, Linux machine over the network (Q4_K_M at 71K context) had not finished a whole part after an hour, so it is slower than the Mac for this model and is not used.
- Every prompt names its part as "part X of N", so re-splitting the book changed every prompt and the cache missed: a rerun meant to take seconds took 2 h 56 min. A part's prompt now names only its chapter and pages, so re-splitting rereads only the parts that changed.
- Same chapter, both models (chapter 3, pp. 52–86, where the 9B did not loop): the 9B listed 31 items, 28 on the cited page; Qwopus (whole part, reasoning off) listed 22, all on the cited page. Each found real concepts the other missed (the 9B: skewness, kurtosis, utility function, default spread; Qwopus: the covariance formula, Fama-French, investment grade). So the larger model is more exact but not more complete, and a digest is a finding aid, never a complete index. Whether smaller pieces find more was not settled.
- Qwopus on the 24 GB Mac depends on free memory: with browsers and remote-desktop software open, only 5.4 of its 14 GB stayed resident, prompts that took 3 minutes took over 10, and LM Studio dropped the request after 600 s without data. A Qwopus stage needs a quiet machine.
- A Qwopus-family 9B on the Linux machine ignored `reasoning_effort: none` and spent its whole budget (24,000 tokens) thinking without answering.

## 11. Privacy

Stages 1, 2 and 4 are local. Stage 3 and the digest send lecture text, frames and materials to Anthropic through the session.

- Material ingested with `local_only` stays out unless the user agrees for that lecture.
- Handbooks for confidential lectures can use the local writer (section 13).
- Confidentiality is a per-course decision.

## 12. Evidence

**Choosing a reader.** Models were scored on 8 frames against the images. Larger reasoning models scored highest but ran slowest. Quantisations that pushed the machine into heavy swap were unusable, so test memory use as well as accuracy.

**What changed the design:**

| Failure | Evidence | Decision |
|---|---|---|
| Model merged build-up slides | Keep/drop gave 22→8 and 62→21 | Model judges content only; code decides supersede |
| The fast model invented text | "Verification of Totals", a Total row, a missing Expected table | The strong model reads every kept frame, from the richest sample |
| Repetition loop | 12,019 characters on one frame | Discard replies cut off by the token limit |
| Materials leaked into the body | v1 wrote the whole test in "Background" and invented a Kendall transition, because the course PDF held the same example | Sections written without materials; materials added afterwards |
| Table cut at a chunk edge | v1 lost the row with 5.991 | Lost-table check |
| Sections repeated each other | v2 repeated the flight table | Agent writes the whole book at once |
| Context caused padding | v3 grew 14% with unstated claims | Rule against links and summaries |
| Table misaligned | v3 put 5.991 under the wrong heading | Table-shape check |
| Local writer missed errors | The strong model never noticed the alpha error or the rounding issue | Opus writes, reading the images |

**Handbooks of one test lecture:**

| Version | Writer | Words | Result |
|---|---|---|---|
| v1 | Local | 3,339 | Leaked, invented content, lost table row |
| v2 | Local | 2,064 | Faithful, repeated tables, 1 unneeded note |
| v3 | Local | 2,355 | Padded, misaligned critical-values table |
| v4 | Local | 1,843 | Clean table, one pointer to a repeated table, 1 unneeded note, missed the errors Opus found |
| Opus | Opus 5.5 | 981 | Clean checks, 3 notes, 4 corrections logged |

**Time for a handbook.** Local model: about 65 minutes for 10 sections. Opus: minutes, in the session. Rough projections for a 90-minute lecture: 20–50 hours of ingestion.

## 13. The local writer

`knowlib handbook --output <target>` without `--prepare` still writes the whole book with the strong model. It stays for confidential lectures only, because it is weaker. It plans chapters, writes each section from its own slides, checks numbers and tables with one automatic rewrite, adds at most one checked note per section, then exports. Replies are stored in `.cache.json`, so an interrupted book resumes. Settings are in the `handbook` section of `config.yaml`.

## 14. Rebuilding

1. Install: `scripts/install-mac.sh`, or a Python 3.12 venv with `pip install -e .`, plus ffmpeg, tesseract, pandoc, Chrome, a local model server and ripgrep.
2. `knowlib init <library>`, then set `config.yaml` as in section 4.
3. Load the models in LM Studio under the exact ids in section 4.
4. Run the unit tests: `.venv/bin/python -m pytest -q tests` (currently 65 pass).
5. Run sections 6–8 on one short lecture and compare with its slides.

To rebuild without knowlib, keep these in order of importance: the separation of reading and writing, the rules in section 7, the checks in section 8, and the `source.md` structure.

The operating procedure for agents is Procedure 3 in the library's `CLAUDE.md`, whose template is `src/knowlib/templates/CLAUDE.md`.

## 15. Known limits

- Only one short slide lecture has been through the full Opus route. Screen-heavy and long lectures are untested.
- Lectures in other languages, and in mixed languages, have not been tested.
- A long lecture may take several sessions of reading frames. The brief asks for images of flagged, table and formula slides, and on screen recordings nearly every slide is flagged.
- OCR flags are noise on screen recordings: 58 of 65 slides on one test lecture, and false alarms on the two checked.
- A recurring-text filter still lets some interface text through.
- A failed frame reading leaves junk OCR in `source.md`, as on one test lecture's first slide. The handbook leaves it out or reads the image.
- Web pages with almost no content, and CSV files whose delimiter cannot be detected, fail ingestion. This is harmless for placeholder pages, but failures should be read.

## 16. Lessons from the first full handbook

A 95-minute lecture recorded as audio only, with a 65-slide deck, went through every step below, one step at a time, each inspected before the next started.

**Ingestion**
- Audio without video: `session.yaml` names the deck (`slides:`), `skip_slides` leaves out what must never be used (a class list), and the aligner matches speech to slides by their text. Its boundaries are often 10–30 s late, so every boundary is checked against the transcript and the slide text and recorded in `slide_starts`, each with a short quote, so reruns keep them.
- Transcription on the machine, full `large-v3` over turbo for names and terms; `start_s` skips the room before class; the glossary (terms taken from the slides) is kept on every Whisper window, which corrected most misheard company names.
- Figures go to a remote model only after local OCR finds no personal information on them.

**Writing and review**
- One model writes (a session model), a different family reviews, and the strongest model judges every flag against the slides and transcript before anything changes; a writer never judges its own text.
- The review is section by section, one section or note per request: batching six notes per request caught 5 of 16 planted errors, one per request caught 12.
- Names, emails and personal details are taken out of everything sent to a remote model and out of the book; the finish step checks the text and, by OCR, every image.
- Condensing is its own pass, and is reviewed again: it reintroduced two overstatements the first review had fixed.
- The flags worth acting on were nearly all overstatements: a hedge dropped, an example turned into a rule, a reason called a definition, a lecturer's addition credited to the slide. Rules 12–17 of the brief come from these and from the reader's notes: be condensed, let the layout show the source (never "on the slide"), plain words instead of shorthand labels, no stronger than the source, leave out what the recording lost rather than noting the gap, and mute the class: students' answers in a round of guesses are not the lecturer's teaching (one draft had credited a student's suggestion to the lecturer as a "top priority"; the transcript showed the lecturer's own picks later).

**Length and layout**
- About 2,700 words for 95 minutes of lecture read well: roughly 30 words per minute, a third of the transcript.
- Page count is mostly layout: the same text took 22 pages with full-width slides and 8 in two columns with slides at column width. Showing several styles side by side, then a live palette and typeface picker on the chosen layout, made the choice easy.
- The PDF is checked page by page: no slide split across pages, no text squeezed beside stacked slides, no near-empty last page, one typeface throughout. Justified columns need the page language declared, or Chrome does not hyphenate and leaves wide gaps.
- Recording times are left out of captions and section endings when the reader does not need them (`show_times: false`); a byline goes once under the title, not on every page.
