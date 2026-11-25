# Roadmap

| Milestone | Deliverable | Verification |
| --- | --- | --- |
| Day 1 | Local project dashboard, project creation, MP4/MOV upload, original preview, and FFprobe metadata | API tests with real media, frontend build, and browser upload |
| Day 2 (implemented) | Persisted transcription jobs and Faster-Whisper segment and word timestamps | Audio offset, bounds, cancellation, retry, restart, and real speech benchmark |
| Day 3 (implemented) | Audio metrics, hybrid silence suggestions, waveform review, and versioned edit plans | Speech protection, synthetic fixtures, timeline mapping, and real audio benchmark |
| Day 4 (implemented, live inference opt-in) | Versioned brand settings, modular playbooks, and one validated AI director | Invalid-plan fixtures, source-to-edit mapping, and revision history |
| Day 5 (implemented) | Styled ASS captions, vertical FFmpeg previews, approval, and final MP4 download | Spoken-fixture render, source checksums, frame/audio markers, Chrome playback, and stale approval gate |

Scheduling depends on local model setup, test footage, and review feedback. Rubrics for cut quality and caption legibility should be exercised with real brand footage as each feature lands.

Day 6 uses the [coaching reel quality pilot rubric](docs/quality-pilot.md) to validate uncaptioned recordings and compare render performance.

Day 7 adds versioned caption presets, correction history, and ASS word effects in a mobile-first Captions workspace.

Day 8 adds the Camera & Motion workspace, local face-analysis caching, reviewed camera moves and revision-bound export approval. The real-footage style default remains pending brand review.

Day 9 adds local Colour & Sound samples, adjustable finishing presets, pinned finishing revisions and encoded loudness verification. The four pilot treatments await visual and listening review.
