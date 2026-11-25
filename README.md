# Elevate AI Studio

A local video workspace for Elevate with Sumeet. Create a project, upload original footage, and run local speech transcription with word and segment timestamps.

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

## Checks

```sh
.venv/bin/python -m pytest -q backend
npm run build --prefix frontend
npm run typecheck --prefix frontend
```

The backend tests require FFmpeg and FFprobe. Set `ELEVATE_FFMPEG` and `ELEVATE_FFPROBE` to executable paths if they are outside `PATH`. Normal CI mocks inference. It still exercises real FFmpeg extraction and checks that delayed audio preserves its source offset.

Run the optional real inference test with the transcription dependencies installed.

```sh
ELEVATE_REAL_TRANSCRIPTION=1 .venv/bin/python -m pytest -q -s backend/tests/test_real_transcription.py
```

With Python 3.14.5 and Faster-Whisper 1.2.1 on an Apple M3 Pro with 18 GiB RAM, the 13.33-second spoken fixture produced two segments and 31 timestamped words. A fresh model load from the local cache, WAV extraction, inference, and persistence took 4.35 s. A second job reusing the loaded model took 2.72 s. These wall times exclude the initial model download. The fixture uses synthesized English speech. Separate Hindi and Hinglish smoke samples were detected as Hindi and returned Devanagari, with spelling errors and English terms sometimes rendered phonetically. The language confidence is for the detected language, independent of word accuracy. Review transcripts before using them to drive edits.

The GitHub Actions workflow runs the backend and frontend checks once the repository is pushed to GitHub.

See [Architecture](ARCHITECTURE.md) for the system design and [Roadmap](ROADMAP.md) for milestone boundaries.
