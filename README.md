# Elevate AI Studio

A local video workspace for Elevate with Sumeet. Create a project, upload original footage, and run local speech transcription with word and segment timestamps. Review silence suggestions, generate a portrait preview with captions, and download the approved MP4.

## Local setup

Install Node.js 24, Python 3.12 or later, and FFmpeg with FFprobe. On macOS with Homebrew, the media tools are available through `brew install ffmpeg`. On Ubuntu, use `sudo apt-get install ffmpeg`. Confirm your tools from a terminal.

```sh
node --version
npm --version
python3 --version
ffmpeg -version
ffprobe -version
```

From the repository root, install dependencies.

```sh
cp .env.example .env
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r backend/requirements-dev.txt -r backend/requirements-transcription.txt
npm ci --prefix frontend
```

Start the backend from the repository root.

```sh
.venv/bin/python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

In a second terminal, start the frontend.

```sh
npm run dev --prefix frontend -- --hostname 127.0.0.1
```

In a third terminal, start the transcription worker from the repository root. It must use the same `ELEVATE_DATA_DIR` as the API.

```sh
PYTHONPATH=backend .venv/bin/python -m app.worker
```

The API and existing upload features also run with the smaller base requirements. The transcription package is needed in the worker's environment.

Open [the studio](http://127.0.0.1:3000). The [health endpoint](http://127.0.0.1:8000/health) reports media-tool availability. The [API explorer](http://127.0.0.1:8000/docs) documents the endpoints.

Use the exact loopback addresses above. To change the frontend address, set `ELEVATE_FRONTEND_ORIGIN` in the root `.env`. To change the API address, set `NEXT_PUBLIC_ELEVATE_API_URL` in `frontend/.env.local` and restart Next.js. The API URL is public configuration. The backend reads secrets separately from the root `.env`.

## Try an upload

Enter a project title, choose **Create project**, then choose **Browse files** and upload `backend/tests/fixtures/studio-synthetic.mp4`. The metadata should show 160 × 90, one second, and H.264. Play the original and refresh the page to see that the project persists. A project accepts one original. Create another project for another take.

Some MOV codecs require a compatible player. The player serves the original bytes directly, so browser decoding depends on the source codec. The API validates that FFprobe can read the container and metadata. It does not decode every frame.

The default upload limit is 512 MiB. The backend stores metadata in `data/studio.sqlite3` and originals under `data/projects/<project UUID>/`. This application runs on loopback and uses the local OS account as its trust boundary.

## Generate a transcript

Upload a video with speech, such as `backend/tests/fixtures/studio-spoken.mp4`. Choose **Transcription**, then **Generate transcript**. When the transcript is ready, click a segment timestamp or expand **Word timestamps** and click a word to seek the original. You can copy the transcript or download TXT and JSON.

While queued, a job waits for the worker. During audio extraction and model loading, the status shows the stage with indeterminate progress. During inference, the percentage reflects the end of the most recently emitted segment relative to the source duration. It can jump if the model emits several segments together. A job is complete after its transcript is saved. Cancellation is immediate for queued jobs, interrupts FFmpeg extraction, and is cooperative during native model loading and inference. It may take until control returns to the worker. Restarting the worker marks unfinished running jobs as interrupted. Use **Retry** on a failed or interrupted job to create a fresh attempt.

The first use downloads the selected multilingual model into `data/models` by default. The small model used here occupied about 464 MiB on disk. With the cache populated, speech processing can run offline and the model stays in memory for reuse. The benchmark below measured about 883 MiB of peak resident memory for the test process. Memory use rises with clip length, model size, and decoding settings. Derived mono PCM needs about 110 MiB of disk per hour. The first audio stream is used.

The defaults are `small`, `cpu`, `int8`, four CPU threads, beam size five, and voice activity detection. CTranslate2 confirmed CPU INT8 support on the Apple M3 Pro used for testing. See `.env.example` for configurable settings. CUDA needs a compatible NVIDIA environment. No network service receives the uploaded audio. The worker can use an existing local converted model path via `ELEVATE_WHISPER_MODEL`. Keep the model multilingual for Hindi and mixed speech.

## Review silence

1. Upload footage with audio. Use **Transcription** first to include word boundaries in the analysis.
2. Open **Silence Detection** and choose **Analyse Audio**. Click the waveform or a suggestion timestamp to seek the original.
3. Accept, reject, or adjust each cut. **Accept high confidence** approves pending suggestions where low amplitude, VAD, and the transcript agree. Listen to ambiguous audio before reviewing it.
4. Export the current editing plan as JSON. The estimated duration subtracts only accepted or modified cuts. No media is edited during this workflow.

Gentle is the default for coaching footage. It uses a minimum pause of 1.2 seconds and 300 ms of padding on each side. Balanced uses 0.8 seconds and 200 ms, and Fast-paced uses 0.5 seconds and 150 ms. All presets start at −35 dBFS and require at least 200 ms of removable audio. You can save custom settings and regenerate a new plan. Prior plans and their decisions are available through **Plan version** and can still be exported.

Peak and RMS are reported in dBFS relative to full-scale digital amplitude. The estimated noise floor is the 20th percentile of 50 ms RMS windows, using the loudest channel in each window. The measurement floor is −120 dBFS. The clipping count measures samples at either full-scale PCM value after extraction and resampling. The optional noise-floor adjustment can raise the amplitude threshold by up to 10 dB, with an adaptation ceiling of −25 dBFS. Known speech remains protected even when the threshold rises. Waveform bars show peak magnitude in each time bucket.

The worker analyses the first audio stream, preserves channel count for measurements, and checks VAD on each channel. This protects speech that appears on only one side of a stereo recording. It stops evaluating candidates at the end of the available audio, even when the video continues. VAD and transcription can miss speech or mistake other sounds for speech. Listen before approving cuts, especially with music, crosstalk, or soft voices.

## Brand Kit and AI Director

Open **Brand Kit** on any project to configure its colours, typography, caption placement, zooms, transitions, audio, export size, and intro or outro conventions. Each save makes a revision. Restoring a revision saves a new copy. The default closing phrase is a style convention. The director can quote only timed recorded words.

On a project with a transcript and silence plan, open **AI Director**, choose a playbook, and use **Estimate input** or **Generate edit plan**. Four versioned playbooks are available. Generated decisions can be accepted or rejected individually. Open **Story cuts** to remove a spoken section using source times or the original video’s playhead. Each cut saves a new revision. The studio checks cut edges against the transcript and keeps earlier previews in history. The review timeline highlights previous silence approvals and proposed operations. Compare or restore earlier revisions and export the reviewed plan as JSON. The export contains both historical suggestions and explicit approved cuts and reviewed events.

The default `ELEVATE_DIRECTOR_MODE=mock` uses deterministic local recommendations and incurs no API cost. To use OpenAI, set `ELEVATE_DIRECTOR_MODE=openai` and `OPENAI_API_KEY` in the root `.env`, then restart the API and worker. Choose a model compatible with strict Responses API structured output using `ELEVATE_DIRECTOR_MODEL`. Generations and explicit retries require confirmation in OpenAI mode. The worker sends the transcript and limited editing context to OpenAI. It omits video, audio, project names, filenames, and local paths. Cancellation is cooperative while an OpenAI request is in flight and the request can still incur a charge.

The input estimate is approximate and includes the instructions, request metadata, and schema. API input, cached, and output tokens are recorded when returned, including invalid plans and refusals that report usage. Configure the matching model's per-million-token rates to calculate cost. The app reports cost as unavailable if relevant rates or API usage are missing. See the [OpenAI model page](https://developers.openai.com/api/docs/models/gpt-5-mini).

## Edit dynamic captions

Open **Captions** on a reviewed director plan. Choose a versioned preset, use the phone preview to check the style and safe margins, and choose a cue to seek the original. Text edits and manual keywords stay in the draft until **Save caption revision**. Split after a word or merge adjacent cues within the same retained section. If a correction adds words absent from the transcript, the studio requires explicit confirmation. When the number of words changes, it estimates timings for that cue, so check the preview against the audio.

Caption times in the editor are edited-video times at 1×. Rendering applies the chosen output speed and the current saved caption revision. Preview styling in the browser is approximate. Generate an MP4 to inspect actual text shaping, line breaks, and motion. The safe area is a configurable starting point, so verify placement on your publishing surface.

Mystical Gold is the default draft in Captions. A render with no saved caption revision uses the earlier static style, keeping existing projects and previews compatible. Saving or restoring captions creates a new revision, so render and approve a new MP4. Older previews remain playable.

## Render and approve a portrait MP4

1. Review the current AI Director plan, including caption emphasis and any additional cuts. The studio uses the approved silence cuts and accepted director cuts. Accepted hooks and B-roll ideas stay advisory. Accepted zooms apply a brief centered push-in on the edited video. Unsupported range-specific audio operations must be reviewed again before rendering.
2. Open **Render**, check the selected plan and choose **Generate Preview**. Standard uses x264 veryfast with CRF 22 and AAC at 160 kbps. High uses x264 medium with CRF 17 and AAC at 192 kbps. Playback speed can stay at 1× or render at 1.1× with pitch-preserving audio and retimed captions. Fit preserves the entire source frame on the brand background. Crop fills the portrait frame with a centred crop.
3. Play the preview and inspect the cuts and caption style. Choose **Approve preview** to enable **Download Final MP4**, or **Reject preview** and refine the plan. A later revision of the plan, brand, transcript, or source makes the prior approval stale. Older previews remain in render history.

The worker requires FFmpeg with the `ass` filter (libass), `libx264`, and AAC, plus FFprobe. Run `ffmpeg -filters` to check for `ass` and `ffmpeg -encoders` to check for `libx264`. Tagged HLG or PQ footage requires the FFmpeg `zscale` filter for tone mapping to BT.709 SDR before captions. Output is 1080 × 1920 at 30 fps, H.264 with yuv420p, and AAC at 48 kHz. The caption engine uses bundled Noto Sans Bold and Noto Sans Devanagari Bold, including when the brand requests a different font. It uses the pinned brand's colours, alignment, and caption style. The lower placement includes room for social-video interface controls.

Captions are grouped from recorded words, recalculated on the frame-aligned edited timeline, and burned in. The settings let you change maximum words, pause separation, and preferred minimum cue duration. Words intersecting a cut seam and untimed segments crossing a seam are omitted. A complete segment with no word timings can appear as one cue. Retained boundaries round inward to the 30 fps frame grid. ASS times have 10 ms precision. A source frame can contribute pixels to a nearby grid frame when the source has a different frame rate.

Day 5 applies brief audio fades at cut seams and a peak limiter. The voice-leveling brand option adds light compression. The renderer supports up to 80 retained ranges and uses a first audio stream. It previews and exports the same immutable file, with no automatic music or B-roll insertion. Use an uncaptioned source when possible. Existing captions burned into the source pixels remain visible and can overlap the new captions.

The 20.734-second coaching fixture produced an 18.634-second approved sample with two cuts and four caption cues. The Standard preview took 20.281 s in the Chrome workflow and 1.597 s in an isolated worker run on the Apple M3 Pro. A separate local 74.3-second portrait recording took 475.556 s with the High preset. Its stream durations matched, and its measured decoded audio peak was −3.8 dBFS. These are observed wall-clock measurements for two different clips and presets.

## Quality pilot

Use the [coaching reel quality pilot](docs/quality-pilot.md) to record revision IDs, measured processing time, manual correction time, and the human quality scorecard.

## Checks

```sh
.venv/bin/python -m pytest -q backend
npm run build --prefix frontend
npm run typecheck --prefix frontend
```

The backend tests require FFmpeg with libass, H.264 and AAC support, and FFprobe. Set `ELEVATE_FFMPEG` and `ELEVATE_FFPROBE` to executable paths if they are outside `PATH`. Normal CI mocks inference. It still exercises real FFmpeg extraction and checks that delayed audio preserves its source offset.

Run the optional real inference test with the transcription dependencies installed.

```sh
ELEVATE_REAL_TRANSCRIPTION=1 .venv/bin/python -m pytest -q -s backend/tests/test_real_transcription.py
```

With Python 3.14.5 and Faster-Whisper 1.2.1 on an Apple M3 Pro with 18 GiB RAM, the 13.33-second spoken fixture produced two segments and 31 timestamped words. A fresh model load from the local cache, WAV extraction, inference, and persistence took 4.35 s. A second job reusing the loaded model took 2.72 s. These wall times exclude the initial model download. The fixture uses synthesized English speech. Separate Hindi and Hinglish smoke samples were detected as Hindi and returned Devanagari, with spelling errors and English terms sometimes rendered phonetically. The language confidence is for the detected language, independent of word accuracy. Review transcripts before using them to drive edits.

The Day 3 end-to-end test uses a 20.734-second synthesized speech fixture and real FFmpeg, Faster-Whisper, and Silero VAD. On the Apple M3 Pro, a fresh analysis took 0.097 s and cached regeneration took 0.003 s after transcription. Gentle found four candidates totaling 5.404 s. Bulk approval of those four produced a 15.330 s plan. The original's SHA-256 matched before and after analysis and review.

The GitHub Actions workflow runs the backend and frontend checks once the repository is pushed to GitHub.

See [Architecture](ARCHITECTURE.md) for the system design and [Roadmap](ROADMAP.md) for milestone boundaries.

The separate paid integration test sends the synthesized coaching sample's transcript to OpenAI. Run it only when you intend to authorize that request.

```sh
ELEVATE_REAL_DIRECTOR=1 .venv/bin/python -m pytest -q -s backend/tests/test_real_director.py
```

## Camera & Motion

Open **Camera & Motion** after reviewing the director plan. Choose original render framing, centre crop, face-aware crop, a manual focus point, or fit-to-frame. **Analyse faces** samples the upright source locally at 1, 2, or 3 frames per second. On Apple Silicon, install the macOS Command Line Tools with `xcode-select --install` if needed, and run the worker from a normal terminal so Vision can allocate its native image buffers. The first analysis compiles a small Swift helper. Face-aware analysis requires FFmpeg and FFprobe, plus the `zscale` filter for HDR. The other framing modes and reviewed zooms work on hosts without Vision.

Scrub or click an event to seek the original. Review the director suggestions, or add an event and set the source interval, magnification, focus, transition and easing. A smooth event can zoom in or out by choosing different start and end levels. A hold eases briefly to a fixed level. A punch changes framing immediately. Each ends at the base framing. Brand Kit and the selected playbook cap magnification. Mystical Sumeet defaults to restrained, centred pushes toward 106% with ease-in-out. The high-energy option permits stronger punches within the brand limit.

Save a motion revision, then render and review the full MP4. Pending and rejected events stay in history. An accepted event must fit in one retained shot and cannot overlap another accepted zoom. Restore an earlier revision into the draft and save it to apply it. Saving motion requires fresh approval for final export, and earlier MP4s remain available in Render history. Browser framing is approximate. The exported render uses a smoothed track, exact cuts, and the existing caption timing.

## Colour & Sound

Open **Colour & Sound** to compare a ten-second window starting at the original playhead. The previews are FFmpeg outputs of that source window. A full reel also applies the existing cuts, speed, framing and captions. **Sound** offers original and enhanced WAVs and a loudness-matched toggle that attenuates the louder comparison. The displayed LUFS and true peak readings refer to the original-level samples. Render history shows the measured encoded audio.

Mystical Warmth and Elevate Voice are the starting presets in the workspace. Save each revision explicitly to use it on the next render. Choose Ungraded Reference or Raw Audio for a control, and restore settings from revision history into a new draft. A new save requires a fresh preview approval. Older projects keep their previous processing until a finishing revision is saved.

Colour controls use exposure in stops, a brightness offset, contrast and saturation multipliers relative to neutral, gamma, midtone white-balance offsets, an RGB tone curve for highlight, shadow, black and white adjustments, vibrance, unsharp strength and vignette angle in radians. Intensity scales these values. Sound uses input and makeup gain in dB, high-pass frequency in Hz, a 2.8 kHz presence EQ, denoising strength in dB, compressor threshold in dBFS, attack and release in milliseconds and unitless compression ratio and de-essing. Intensity scales the effect of gain, filtering and compression. The LUFS and true-peak output targets stay explicit while enhancement is enabled.

Denoising begins at zero in the default preset. Raise it only if a recording has steady audible noise. The quietest-window RMS and luma clipping indicators are estimates from the chosen source window. Quiet audio can include speech, breaths, room tone or recording noise. The browser workflow preserves the original video and creates no new cuts.

## B-roll & Visuals

Open **B-roll & Visuals** to review the Director's transcript-linked suggestions, add an insert manually, or restore an earlier visual draft. Click an event to seek its source interval. Choose a library asset before accepting an insert, check the crop, and save the plan with the intended playback speed. Render and review the MP4 to check the composition. Accepted inserts support full-screen hard cuts, fit or fill, and optional gentle movement on stills. Captions and the edited speech continue across each cutaway.

**Mystical Storytelling** is the default B-roll direction for the Mystical Sumeet playbook. Start with two or three meaningful inserts per edited minute, typically two to four seconds each. Aim to keep the speaker visible for about 75–85% when inserts are useful. Prefer relevant personal footage, then stock, then illustrative AI imagery. Use graphics when they clarify an idea. Keep speaker-only for healing and personal reflections, and use Mixed Storytelling as an option for educational and concept-heavy reels. The current renderer uses hard cuts. These are editorial guidelines, and each insert remains subject to review.

The reusable **Asset Library** accepts MP4, MOV, JPEG, PNG and WebP. It validates and decodes uploads, deduplicates by checksum, and shows locally generated thumbnails, provenance and prior plan uses. Personal files stay local. Mock stock and mock image generation are available without credentials. Graphic Cards renders Statement, Concept Flow and Contrast templates locally with bundled Latin and Devanagari fonts.

Configure optional `PEXELS_API_KEY` for interactive stock search and reviewed imports. Store only assets needed for a planned production, preserve attribution, and review restrictions and any third-party rights. Studio disables standalone Pexels file delivery. Use the credited provider page for stock previews. There is no bulk import, public catalogue, model training collection, or stock redistribution workflow. See the [Pexels API terms guidance](https://help.pexels.com/hc/en-us/articles/900005880463-What-are-the-Terms-and-Conditions/).

Configure an OpenAI image model, server API key, estimated cents per quality and a per-project cap in `.env`. Paid image requests require the displayed estimate to be approved. Reservations are conservative and include any failed or cancelled paid attempts because in-flight calls can still incur charges. Retry is explicit. The provider receives the approved prompt and basic Brand Kit creative context. No personal assets or source footage are sent. Images are labelled illustrative. See the [OpenAI Images API](https://developers.openai.com/api/reference/resources/images/methods/generate).

## Music & Sound Design

Open **Music & Sound Design** and choose **Audio Library** to import music or effects. The library accepts WAV, MP3, M4A, AAC, FLAC and OGG when supported by FFmpeg. It measures loudness and true peak and records creator, licence, platform and commercial-use declarations. Local studio chimes, swells, whooshes, soft impacts and ambient transitions can be synthesized explicitly. Preview-only fixtures cannot be approved for final export.

The six versioned music presets select matching tracks already in the library. Mystical Ambient is the starting suggestion for regular coaching. Start around −24 dB with gentle fades and roughly 7 dB of ducking. Choose No Music for voice-led cuts or silence. Healing Frequency describes an aesthetic. Play the samples at matched loudness and review the voice against each bed. Add effects sparingly. The mix uses the edited clock after cuts and playback speed. Save a new revision after changing speed or voice enhancement.

Render a compatible voice-only preview once before generating short mix samples. The app reuses the exact verified picture stream when the visual and voice settings match. Otherwise a full render builds the carrier first. Mix revisions and asset identities are included in approval. Keep authorized originals to support rerendering. A missing or changed asset blocks rendering, and the app checks current rights again at export.

## Virtual Studio

Upload a green-screen recording, open **Virtual Studio**, and run **Analyse background**. Apply the sampled key or use the colour picker. To sample a pixel, generate an original-source FFmpeg proxy, enable **Pick from original sample**, and click the screen colour. The indicator estimates consistency in likely background areas. It does not identify the speaker or assess whether every green pixel belongs to the backdrop.

Review the matte, transparent foreground, black and white checks, and the final composite. Generate a ten-second proxy at the current original playhead. Use restrained spill suppression and narrow feathering to protect hair and hands. Green clothing that matches the screen needs a different recording setup or a manual key that preserves enough separation. No semantic background removal runs in this workspace.

Choose a library plate, solid colour, or gradient placeholder. Virtual Studio uses the shared asset library for JPEG, PNG, WebP, MP4 and MOV. Six versioned studio styles and Original Background are available. Presets use matching named plates already in the library and can fall back to visibly labelled gradients. Video plates require enough footage or explicit looping. Loop cuts use the source boundary. Save a studio revision, then render and review the actual full MP4 before approval.

**Mystical Cinematic V2** starts at 105% foreground scale and uses mild shadow-preserving exposure, midtone and warmth adjustments. Compare 100%, 105% and 110% at a point where the speaker gestures. At scales above 100%, the position controls choose which part of the enlarged foreground to keep. Review the fingertips and the caption area together. The optional luma sharpening control starts at zero.
