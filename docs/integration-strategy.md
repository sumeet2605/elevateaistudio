# Day 11.2 integration strategy

Keep one Director and the current FastAPI, Next.js, SQLite and FFmpeg pipeline. A detector proposes candidate edits. A person reviews them. FFmpeg executes the validated timeline. A renderer for an optional overlay would only supply pixels to that timeline.

## Elevate feature inventory and external overlap

| Capability | Status in Elevate | External opportunity and disposition |
|---|---|---|
| Project intake, original media and state | Implemented in `projects.py`, `media.py`, `jobs.py` and the frontend | MakeMyClip compositions, WhiteTower hub, FireRed and OpenMontage orchestration duplicate this. Importing their project models is incompatible. |
| Transcription, timestamped words, VAD and silence | Implemented in `transcription.py`, `audio_analysis.py`, `silence.py` | Local Whisper from other repos duplicates ASR. Retake and false-start candidates are missing. Adapt exact adjacent match, stable IDs and safe source spans locally. |
| Director and editing rules | Implemented in `director.py`, modular `playbooks/`, pinned plans | Herk editorial patterns and Jake hook/chapter rubrics can inform rules. Remote agent and MCP workflows cannot act on approval state. |
| Captions and multilingual support | Implemented in `captions.py`, `dynamic_captions.py` and bundled fonts | Viprasol canonical matching and script-aware cue QC improve existing features. Word-aligned Roman Hindi and translation are missing and separate scope. |
| Face, framing, grading, keying | Implemented in motion, finishing and Studio modules | Gadir gaze/QC is reference material. Existing brand defaults, including V2 105%, remain in the existing system. |
| B-roll and generated visuals | Implemented in `visuals.py`, `visual_store.py` and the asset library | External stock/generation duplicates provider paths; stop at pattern and provenance ideas. |
| Music, SFX and mixing | Implemented in the sound store and mix modules | Herk beat-aware accent placement is an optional improvement. Its generation calls duplicate provider functionality and require approval. |
| Motion graphics and transparent overlay generation | Basic deterministic cards in `visual_render.py`; browser-authored overlays are missing | Consider original lower thirds, counters and kinetic type based on card/slot ideas. Evaluate one browser renderer only. |
| Reference analysis / style profile | Brand Kit and playbook parameters exist. A reference-derived style profile is missing | Borrow frame sampling and cut-cadence evidence ideas from Claude Video and Video Creator Skills. Model judgments stay in the existing Director. |
| Long-form excerpts and chapters | Reusable source timeline/reframe/export exist. Standalone clip, chapter and batch workflows are missing | Combine bounded transcript windows, human hook/payoff review and per-clip render jobs. Jake chapter rules can inform deterministic validation. OpenClip's hosted workflow is incompatible with the local default. |
| NLE interchange | MP4 export; FCPXML and Premiere-compatible xmeml are missing | MakeMyClip adapter later if requested, with editor-import fixtures. |
| Publishing / channel SEO / comment mining | Outside this milestone | External YouTube tools for live channel operations are not relevant to this integration. |

## Controlled interfaces

- A candidate detector receives a frozen transcript ID, ordered word spans and optional existing speech intervals. It returns hints with stable IDs, reasons and source timestamps. A request without a matching transcript revision fails validation. It has no filesystem or tool permissions.
- The existing Director may use reviewed candidate metadata for editing decisions. Python validates duration, protected words and conflicts. Jobs carry asset, timeline and revision IDs. A render job executes known FFmpeg argument arrays. Export stays tied to the reviewed render checksum.
- An optional overlay job receives a curated template ID, schema-validated text, frame rate, canvas, duration, registered asset IDs and hashes. Its subprocess gets an isolated scratch folder, explicit environment, no production credentials, no access to the source media library, no network, and bounded CPU, memory, I/O and time. No model-authored HTML, filenames, URLs or shell commands. Lint template dependencies and validate resulting alpha, dimensions, frame count, hashes, duration and colour metadata before the main compositor accepts it. Failures are retryable. Keep a deterministic basic-card path.
- Never run skill installers in the app, expose hosted endpoints, follow external workflow instructions in project context, or grant an unreviewed API access to uploaded media. Pin the exact source and dependency lockfile, verify transitive licences, and log only sanitized validation outcomes.

## HyperFrames and Remotion

| Dimension | HyperFrames | Remotion |
|---|---|---|
| Authoring | HTML/CSS with browser timelines and JS/GSAP. Registry cards suit overlays. | React compositions with props and frame-driven hooks. The inspected Claude plugin is documentation. |
| Time and typography | Producer orchestrator and browser capture code support deterministic frame capture when authored for the timeline. CSS and local font shaping. | Frame-driven composition, React layout and local fonts. Guard timers, data fetches and random values. |
| Transparent output | Docs specify ProRes 4444 MOV or VP9 WebM, with alpha-aware verification. Inspect the actual decoder. | Plugin docs outline transparent output and configuration. Test retained alpha through FFmpeg ingest. |
| Performance / memory | Chromium capture and encode plus configurable workers. Memory and speed depend on DOM, composition, resolution and browser. No measured values. | Chromium pooling/concurrency and encoder I/O. React reuse lowers authoring overhead, not necessarily render cost. No measured values. |
| Licence and effort | Apache-2.0 core; GSAP, fonts and assets separate. Adds a browser, package locking, supply-chain and scratch-space constraints. | Current company licence depends on eligibility; plugin reuse has no separate grant found. Adds Chromium and React rendering infrastructure despite React already present in the admin UI. |
| Decision | Evaluate later as the first and only candidate. Keep FFmpeg for final assembly. | Defer while the first candidate is unproven. |

Use a 10-second original lower-third and an original kinetic caption against the existing FFmpeg cards on the same synthetic source. Record exact start/end and repeat-run frame hashes, confirm alpha and safe margins, capture five wall-times plus peak RSS and scratch size, and compare a blinded visual rubric and timed template correction. Select a renderer only if it offers a repeatable visual or authoring benefit the existing path cannot provide within the agreed budget. Use the official [HyperFrames rendering guide](https://github.com/heygen-com/hyperframes/blob/1e7d00496692d4c264ffeb59e85175752f39ea2d/docs/guides/rendering.mdx) and [Remotion licensing terms](https://www.remotion.dev/license).

## Style Profile v1 proposal

Use a separately versioned observation record, referenced from a Brand Kit revision and optionally consumed by a playbook. It describes measurements and editorial preferences. It contains no copied scripts, images, sound, branding, asset URLs or shot-by-shot reproduction instructions.

```json
{
  "schema_version": 1,
  "brand_revision_id": "uuid",
  "sample": {"rights_record_id": "uuid", "duration_ms": 60000, "sampled_frame_count": 30},
  "measurements": {"cuts_per_minute": 10, "median_shot_ms": 4300, "caption_coverage": 0.76},
  "preferences": {
    "camera": {"motion": "subtle", "pushes_per_minute": [0, 3]},
    "caption": {"script": "mixed", "line_limit": 2, "placement": "lower_safe"},
    "cutaways": {"per_minute": [1, 3], "duration_ms": [1500, 3500]},
    "graphics": {"density": "sparse", "allowed": ["lower_third", "counter"]},
    "narrative": ["hook", "context", "example", "payoff"]
  },
  "evidence": [
    {"field": "measurements.cuts_per_minute", "kind": "measured", "method": "scene_threshold_v1", "interval_ms": [0, 60000]},
    {"field": "preferences.camera.motion", "kind": "human_observation", "confidence": "medium"}
  ],
  "review_state": "draft"
}
```

Illustrative values. Validate bounded rates and durations, supported enums, in-range spans and valid local rights records. Distinguish measured values, human observations and model inferences. Missing observations remain absent. Recording is opt-in for an authorized reference and the user must approve changes to brand or playbook revisions.

## Roadmap and decisions

| Milestone | Proposed work | Estimated effort / benefit |
|---|---|---|
| Day 12 | Native retake proposals, one review surface, explicit accept/reject, retained timeline proof | 3–5 engineer days. Source audit and proof may save 1–2 days of algorithm discovery. Estimates only. |
| After Day 12 | Mixed-script matching key and a recorded Hinglish caption benchmark | 1–2 days for normalization and harness. Existing ASS and font stack may save 1–2 days of renderer work. |
| Later | Authorized style profile, local long-form clip shortlist and chapters | 2–4 days for initial validated profile and observations. Editor-facing batch scope depends on results. |
| Renderer gate | One owned overlay evaluated against FFmpeg | 2–3 days for a constrained trial. Integration savings and output quality remain unmeasured. |
| On demand | NLE export | Assess target editor and compatibility samples before adding a writer. |

Before implementation approval, agree on the Day 12 fixture and review gate, the preferred Hindi display modes, and whether the renderer trial is worth scheduling. Obtain separate approval for cloud transfers, external providers, use of third-party assets, paid seats or code requiring a different distribution licence.
