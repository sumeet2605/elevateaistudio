# Roadmap

| Milestone | Deliverable | Verification |
| --- | --- | --- |
| Day 1 | Local project dashboard, project creation, MP4/MOV upload, original preview, and FFprobe metadata | API tests with real media, frontend build, and browser upload |
| Day 2 (implemented) | Persisted transcription jobs and Faster-Whisper segment and word timestamps | Audio offset, bounds, cancellation, retry, restart, and real speech benchmark |
| Day 3 | Audio metrics, silence suggestions with pause padding, and transcript/cut review | Synthetic silence fixtures and retention of natural pauses |
| Day 4 | Versioned brand settings, modular playbooks, and one validated AI director | Invalid-plan fixtures, source-to-edit mapping, and revision history |
| Day 5 | Styled subtitles, vertical FFmpeg render, approval, preview, and MP4 export | Subtitle styling, argument construction, render dimensions/codecs, and approval gate |

Scheduling depends on local model setup, test footage, and review feedback. Rubrics for cut quality and caption legibility should be exercised with real brand footage as each feature lands.
