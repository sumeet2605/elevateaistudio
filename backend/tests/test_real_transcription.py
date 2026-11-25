import os
from time import perf_counter

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.jobs import Jobs
from app.main import create_app
from app.projects import Projects
from app.transcription import FasterWhisper, Transcript, TranscriptionSettings
from app.worker import Worker

from test_transcription import FIXTURES, enqueue, project


@pytest.mark.skipif(os.getenv("ELEVATE_REAL_TRANSCRIPTION") != "1", reason="Opt in to real model inference")
def test_real_spoken_fixture(tmp_path):
    env = Settings.from_env()
    settings = Settings(data_dir=tmp_path, ffmpeg=env.ffmpeg, ffprobe=env.ffprobe)
    model_settings = TranscriptionSettings.from_env(env.data_dir)
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        root = project(client, FIXTURES / "studio-spoken.mp4")
        jobs = Jobs(Projects(tmp_path))
        engine = FasterWhisper(model_settings)
        worker = Worker(settings, jobs, engine)

        for label in ("cold model load + inference", "warm model reuse"):
            enqueue(client, root)
            started = perf_counter()
            worker.process(jobs.claim())
            elapsed = perf_counter() - started
            job = client.get(root + "/transcription").json()["job"]
            assert job["status"] == "completed", job["error"]
            transcript = Transcript.model_validate(client.get(root + "/transcript").json())
            assert transcript.language == "en"
            assert "growth starts with one small decision" in transcript.text.lower()
            assert len(transcript.segments) >= 2
            assert all(segment.words for segment in transcript.segments)
            print(f"{label}: {elapsed:.2f} s for {transcript.source_duration:.2f} s source; "
                  f"model={model_settings.model}, device={model_settings.device}, "
                  f"compute={model_settings.compute_type}, threads={model_settings.threads}, "
                  f"beam={model_settings.beam_size}, VAD={model_settings.vad}; "
                  f"language={transcript.language}, segments={len(transcript.segments)}, "
                  f"words={sum(len(segment.words) for segment in transcript.segments)}")
