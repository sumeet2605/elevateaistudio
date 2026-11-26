# Day 12 plan for reviewable retake detection

A retake replaces an earlier attempt at the same line. A pause alone does not identify a retake, and deliberate repetition is common in coaching speech. Treat both attempts as review material.

## Inputs and algorithm

1. Read the existing Faster-Whisper transcript ID, timed word spans, media duration, existing VAD/silence data and current retained timeline. Freeze those inputs for reproducibility.
2. Form a canonical token key with Unicode NFC and case folding while retaining vowel marks. Keep original captions and transcript bytes intact. Compare only nearby chunks within the same speaker/continuity window and cap the length and number of comparisons. Retain explicit word indices.
3. Start with exact adjacent runs. Show a separate, lower-confidence class for a short filler gap or corrected phrase with substantial shared context. Add a local sequence-alignment score with small word and time bounds only after fixture calibration. Reject lone words and repetition runs with one distinct token. Lower confidence for intentional refrain, useful context, lower ASR certainty and differing meaning. Never remove fillers by default.
4. Rank comparisons using phrase completeness, continuity and corroborating boundaries. The later attempt is a weak tie-breaker. Let the reviewer select either attempt, keep both or adjust boundaries. Include source intervals, evidence codes, both snippets, a transcript revision and a stable candidate ID. Seed selection with a deterministic rule and avoid a second model.
5. Guard neighbouring words and nearby VAD. Clamp source endpoints to the media. Review the join with short handles. When a retake candidate overlaps a silence cut, use the same authoritative source-clock plan and resolve conflicts before merging. Never cut a selected retained word due to frame rounding or remove an entire segment collaterally.
6. Store proposals, decisions and parent revision in SQLite through existing jobs and plan snapshots. Invalidate stale suggestions when transcript, source or base edit changes. Existing approvals stay pinned. The existing Director can consume reviewed hints, and FFmpeg still compiles the final retained ranges and caption timeline.

## Suggested proposal fields

`candidate_id`, `schema_version`, `transcript_revision_id`, `base_edit_revision_id`, `detector_version`, `earlier_word_range`, `later_word_range`, `earlier_source_span_ms`, `later_source_span_ms`, `reason_codes`, `confidence`, `preferred_take`, and `status`. Use half-open source intervals, explicit uncertain status, server-owned media paths and bounded integers. Do not trust proposed times until the server validates them against the pinned words.

## Review surface

Add a section beside Suggested Cuts with two listen buttons, readable reasons and an A/B join preview. Keep both by default. Only a human-confirmed decision can join a new edit revision. Preserve the ability to undo by restoring a revision. Approval of an edit plan is distinct from approval of the rendered MP4.

## Benchmarks and exit gates

- Record a new, consented 60–120-second fixture containing an exact restart, corrected restart, isolated false start, filler gap, three takes, deliberate refrain, single-word emphasis, a soft-spoken segment and an intentional pause. Hand-mark kept/alternate words, syllable-safe boundaries, and an explanation for each ambiguity. Keep raw media local. Add at least three varied consented recordings for tuning and reserve one unseen recording for final scoring.
- Compare existing silence suggestions, the inspected exact-phrase rule and the proposed detector on the same transcript and audio. Score one-to-one candidate precision/recall and false positives by class, median/p95 ideal cut endpoint drift, clipped protected words, rendered caption timing and matched actual join PTS. Track ASR, detection and preview times separately. Record reviewer correction time with an ordered protocol.
- Proposed initial acceptance gates are zero silently applied cuts, zero protected neighbour words or revisions lost, 100% candidate provenance, at least 90% precision on the high-confidence class in the held-out review set, and audible review of every accepted join in the end-to-end fixture. Treat recall as exploratory until gold coverage expands. Report raw counts and avoid claims from the synthetic eight-case fixture.
- Test reordered/replaced transcripts, multilingual words, punctuation/marks, exact emphasis, corrected line, open-ended false start, overlap, clip boundary, word indexing, frame quantization, cancel/retry, and stale render approval. Test manually in the existing UI and compare original/preview hashes. Keep Phase 12 execution separate from this audit.

## Controlled Hinglish follow-on

Use an owned code-switch recording with manually verified spoken words and source timestamps. Label Devanagari Hindi, Roman Hindi as a separately reviewed display option, and existing English segments. Keep translation a separate cue-level mapping. Record Hindi/English WER and Devanagari CER after declared canonicalization, per-word start/end absolute error, mixed-script cue line fit and reading rate, missing glyphs and false highlights. Present the same media, original-timed ASS and one script-aware layout variant to a bilingual reviewer. Keep sentence danda, vowel signs, nukta, punctuation, code switches, and any word merges explicitly tested. Record read-through and manual correction time. Local models, bundled OFL fonts and deterministic renderer are enough for the initial benchmark.

## Day 12 implementation and synthetic baseline

The local worker now stores pinned retake analyses and the workspace provides side-by-side playback, a short FFmpeg join preview, an explicit keep decision and immutable restore history. The Director pins accepted retake cuts alongside the existing reviewed base. A new decision makes final export stale while historical previews remain readable.

The synthetic benchmark used eleven labelled scenarios with eight earlier-take starts. It compared a speech-protecting silence-only workflow, the audited adjacent exact rule and the hybrid detector. The exact rule found five starts with one deliberate-refrain flag. The hybrid found eight starts with the same refrain flagged as ambiguous. A separate locally synthesized MP4 demonstrated a real short A/V join and retained the surrounding words. The synthetic holdout contains one high-confidence match. Natural-speaker precision, actual ASR boundary error, and human correction and review times require the consented benchmark described above.
