# Day 12.1 real-speaker validation protocol

**Status:** Awaiting designated, consented recordings and a human reviewer. The Day 12 source baseline is `b02c9ca22c857ee82274eb01f34f1aa834e9ed61`. No detector tuning or scores are recorded here.

## Intake and separation

Use three varied development clips and one held-out clip. Include at least one natural English/Hinglish coaching delivery. The held-out clip should be a separate recording session or delivery, rather than an excerpt or alternate encode of a development clip. Register local file paths privately with clip aliases, consent confirmation, duration, source SHA-256, language and role. Keep originals read-only and recheck their hashes after preview creation. Keep footage, transcripts and identifiable annotations outside Git. A designated reviewer or custodian should hold back the evaluation clip and its annotations until the detector configuration and scoring criteria are frozen.

## Independent annotation

A human annotator listens to the full source before opening any method's suggestions. Record the source-clock half-open millisecond interval of each earlier attempt, the corrected or final attempt, the protected neighbouring words, explicit may-remove spans and acceptable boundary windows. Mark repetition, fillers, emphasis, quiet speech and intentional pauses by observed role. For a three-take group, record each earlier start exactly once and record all acceptable keepers. Include ambiguous cases and document adjudications. Ambiguous ground truth is reported separately from primary precision and recall. Record the annotation version and source hash before running comparisons.

## Controlled methods

Use the same source, pinned local Faster-Whisper transcript, silence/VAD evidence and source clock for all methods. Method A uses the current silence workflow. Method B uses the Day 11.2 audited adjacent exact matcher with its reference behaviour. Method C uses the current hybrid. Wall time for detection excludes shared decoding, inference and review, which get separate timings. Record software SHAs, model settings, thresholds and runtime. Inspect development errors before proposing any change and freeze the final implementation before opening the holdout. Keep confidence bands as heuristic labels.

Count unique earlier starts rather than counting multiple keeper options for one start. A proposed start matches one gold start when its lead timestamp is within 300 ms and its earlier attempt overlaps the corresponding gold attempt. Use one-to-one matching, maximizing matches and resolving ties by smallest absolute start error. Report duplicate flags separately. A silence suggestion is only a retake hit if it meets the same match rule. Count unmatched method candidates as false positives and unmatched determinate gold starts as false negatives. For categories, report recalls by gold case type and list false positives by the closest adjudicated source event. Report high-confidence subsets and ambiguous flags separately. Precision is TP/(TP+FP), recall is TP/(TP+FN), and a zero denominator is undefined. Include raw counts and treat tiny holdout samples as descriptive.

For a matched accepted join, report deviation from the closest boundary inside each gold window, zero when inside. Use the actual frame-rounded removal to count intersections with protected word intervals. Confirm the two-sided preview from the reviewed choice, its FFprobe stream durations and the unchanged source checksum. Report skipped or blocked joins with the safety reason. Choosing a take remains a review action and final MP4 approval is a separate step.

## Human review timing

Assign one reviewer to both workflows for each paired timing trial. Use a fresh copy of the edit state, the same source and an identical quality rubric. For manual review, hide retake suggestions. For assisted review, show them, but require the same auditory join check. Start at first playback and stop when the reviewer explicitly approves the edit plan, excluding upload and transcription. Record review, take listening, choice, boundary correction, preview generation, preview listening, and edit-plan approval as separate stages. Log pauses outside the task and exclude them from elapsed time. Alternate order across comparable clips when possible, and report same-clip learning effects. Compare both plans against the annotations and report actual paired minutes saved or lost alongside quality differences. No automation timing counts as human timing.
