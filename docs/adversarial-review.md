# Adversarial review

knowlib does not trust any output because whatever produced it sounds confident. Every claim the pipeline makes falls into one of these kinds:
- text read off a slide
- a transcript line
- a number in a note
- a sentence in a handbook
- a label in a redrawn figure

Each is checked by something that did not produce it. Where the two disagree, the disagreement is shown to a stronger reader, who decides it against the original evidence. The producer never resolves its own disagreements, and nothing is quietly fixed.

This document gives the rules, lists every check in the system, and says what the checks cannot catch.

## 1. Rules

1. **The producer never grades itself.** The model that writes notes may not review them. `knowlib models` and `knowlib review-notes` refuse a review set-up that includes the writer's model, and warn when the reviewer comes from the same model family.
2. **A different method beats a second opinion of the same kind.** Two vision models tend to share blind spots. A vision model and classic OCR fail in different ways, so text read off an image is compared with OCR.
3. **Code checks what code can check.** Code verifies, without any model:
   - that numbers and quotations appear at the cited place;
   - that a cited page, slide or section exists;
   - that a table has the same number of cells in every row;
   - that a figure contains every label;
   - that no personal information is present.

   A model's own confidence is never evidence.
4. **The original decides.** The image, the transcript passage and the printed page outrank every derived text: a caption, a summary, a digest, a model's reading. A digest is used to find pages and is never cited.
5. **Doubt stays visible.** A disagreement becomes a flag in the source, a line under "To check" in `review.md`, or an entry in `corrections.md`. Flags are listed by `knowlib qa` and are only cleared by fixing the cause.
6. **The strongest reader judges.** A flag is settled by the strongest model available, or by the user, reading the original. The decision quotes the line that settles it. A checker's verdict is a lead to follow, not a correction to apply.
7. **One thing at a time.** A reviewer is given one note or one section per request. Packing several into one request lowers the share of errors it catches (section 4).
8. **Checks are allowed to be noisy.** A check that reports too much costs a reread. A check that reports too little lets an error through. The hedge check and the OCR check are tuned to report too much.
9. **A person approves what is a matter of taste or risk.** Redrawn figures enter a handbook only after the user approves them on a review sheet. Anything sent outside the machine follows the privacy rules first.

## 2. Every check, stage by stage

| What is produced | Produced by | Checked by | Kind of check | When they disagree |
|---|---|---|---|---|
| Text read off a slide or frame | vision model (reading role) | classic OCR, one or two engines, on the full-resolution frame | different method | The slide is flagged once a majority of OCR engines disagree, or on numbers that appear on one side only. Both OCR outputs are written beside the slide, and the agent opens the image. |
| Keeping or dropping a frame | fast model | the list of dropped frames, with thumbnails and reasons, at the end of the source | audit trail | QA looks at the thumbnails. A dropped frame that held content is reported. |
| Build-up slides and repeats | the model judges content only | code decides what replaces what (text kept, word overlap) and what merges (text and image similarity) | code | A model is never trusted to merge slides, because it merged build-up slides in testing. |
| Transcript | speech-to-text | glossary seeding, a repetition-loop guard, and QA reading three passages | code and review | Misheard terms go into the glossary and the source is reprocessed. |
| Figures in PDFs | vision model | classic OCR; a second vision model rereads flagged figures and tables, comparing tables row by row | different method, second reader | Differences are written to `work/figures/crosscheck-<id>.md` for QA. |
| Image detail | the model server | `detail_check`: prompt tokens for a crop against a half-size copy | code | A server that caps image detail stops the run with the fix. |
| Book digest | fast model, one part at a time | page check (an item citing a page outside its part is marked ⚠); loop detection, with the part read again by a fallback model | code, second reader | A part that still loops carries a warning. The digest is never cited. |
| Topic notes | writer (agent or model) | `knowlib check-notes`: the cited source, page, slide or section exists; numbers and quotations appear there; duplicates; consolidation; leads to sources that discuss the topic but are not cited | code | Errors are fixed. Warnings are fixed, or kept when checked and right. |
| Claims in notes | writer | `knowlib review-notes`: a second model, not the writer, judges each claim against only the passages it cites: supported, partly, unsupported or unclear | second model | Each flag is checked against its page before the note changes. Changed notes are reviewed again. |
| Handbook draft | writing agent | `knowlib handbook --finish`: unsupported numbers, table shape, lost table rows, slide markers, dropped hedges; the writer rereads every claim against its passage | code, self-check against the source | Listed under "To check" in `review.md`. Each item is resolved, then finish runs again. |
| Handbook draft, wording | writing agent | optional: a reviewer from another model family, section by section, with names removed; the strongest model judges each flag | second model, judge | The judge quotes the deciding line from the slide or transcript before anything changes. |
| Slide text in the handbook | ingestion | the writer opens the image of every flagged, table, formula and chart slide | the original decides | Each correction goes into `corrections.md`: what the text said, and what the image shows. |
| Arithmetic | anyone | run as code, never done in the head | code | During testing a mental sum wrongly suggested a slide was wrong. Running it showed the slide was right. |
| Redrawn figure | a cheaper model writes the drawing code | label check; privacy check; a vision model compares the figure with the slide; the agent judges; the user approves | code, second model, person | Up to the configured number of revisions. Nothing is placed without approval. |
| Personal information | everything that leaves the machine or goes into output | `privacy.py` on every prompt; local OCR on a figure before it may go to a remote model; `--finish` on the text and every image | code | The figure stays local, the prompt is not sent, or the handbook is not ready. |
| Model set-up | the config | `knowlib models --check`: the model is served, takes images, gets full image detail, and its context fits | code | The run stops with the fix before any work is done. |

## 3. Setting up independence

A library has five model roles: reading, digest, writer, review and handbook (`knowlib models`). The review only works if it is independent:

- **Reviewer ≠ writer.** This is enforced.
- **Reviewer from another model family.** A warning is given when they share one. Models of one family make similar mistakes, so they agree on the same errors.
- **The strongest available model reviews or judges.** A weaker reviewer is still useful as a source of leads, as long as a stronger reader settles each flag.
- **Fast readers, a careful writer, a different reviewer** is the pattern the roles encourage. Which models fill the roles is a per-library choice. Test candidates on your own material: score 8–11 frames against the images by hand, checking every number.

Confidential material (`local_only`) limits who may read it. It stays on the machine, so the reviewer must also be a local model. This is enforced in code.

## 4. Evidence

These findings set the rules above:

- **One note per request.** With planted errors, a reviewer caught 12 of 16 when given one note at a time, 9 when given three, and 5 when given six.
- **A capped server.** A model server that capped image detail at about 290 tokens per image read a dense diagram with ten wrong numbers. With the cap raised, all 41 values were exact. That led to `detail_check`.
- **Merged slides.** Models asked to keep, drop and merge frames in one step merged build-up slides (22 frames down to 8, 62 down to 21) and invented text. The jobs were split: the model judges content, code decides what supersedes what, and the strong model reads every kept frame.
- **Overstatements.** In handbook reviews, nearly every flag worth acting on was an overstatement:
  - a hedge dropped;
  - an example turned into a rule;
  - a remark about one setting stretched to another;
  - a number separated from what it was said about;
  - a second source left out where the lecturer contradicted a document.

  The number check cannot see any of these. That is why the writer rereads every claim, and why finish lists sentences that are absolute where their source hedges.
- **Condensing.** A pass that shortened a draft brought back two overstatements the first review had fixed, so condensing is reviewed again.
- **The writer misses its own errors.** A local writer never noticed an error in a slide's formula or a rounding issue that the stronger writer found by opening the images.

## 5. What the checks cannot catch

- **An error the producer and checker share.** Two models of one family, or OCR and a model both misreading a blurred digit. A different method and a different family make this rarer, not impossible.
- **What is missing.** A reviewer judges what was written, not what was left out. Leads, dropped-frame thumbnails and the brief's rule to follow the lecture in order reduce this, but completeness rests on the writer.
- **Subtle paraphrase.** A claim that is technically supported but misleading in context can pass. The fidelity rules in the brief and the claim-by-claim reread target this, but cannot prove it absent.
- **Noise.** On screen recordings most slides are flagged by OCR, because OCR reads interface text and misses tables. Flags are pointers to reread, and on such material most are false alarms.
