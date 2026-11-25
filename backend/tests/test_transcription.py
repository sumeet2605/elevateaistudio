import os
import sqlite3
import subprocess
import wave
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.audio import extract_audio
from app.config import Settings
from app.jobs import Jobs
from app.main import create_app
from app.projects import Projects
from app.transcription import Segment, Transcript, Word, segment_from_model
from app.worker import Worker

FIXTURES = Path(__file__).parent / "fixtures"
CLIP = FIXTURES / "studio-synthetic.mp4"
FFMPEG = os.getenv("ELEVATE_FFMPEG", "ffmpeg")
FFPROBE = os.getenv("ELEVATE_FFPROBE", "ffprobe")


class FakeEngine:
    settings = SimpleNamespace(model="mock")

    def load(self):
        pass

    def transcribe(self, _audio):
        word = SimpleNamespace(start=0.2, end=0.4, word=" Hello", probability=0.9)
        segment = SimpleNamespace(start=0.1, end=0.8, text=" Hello", words=[word])
        return iter([segment]), SimpleNamespace(language="en", language_probability=0.98)


@pytest.fixture
def studio(tmp_path):
    settings = Settings(data_dir=tmp_path, ffmpeg=FFMPEG, ffprobe=FFPROBE)
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        yield client, settings, Jobs(Projects(tmp_path))


def project(client, clip=CLIP):
    project_id = client.post("/projects", json={"title": "Speech fixture"}).json()["id"]
    root = f"/projects/{project_id}"
    if clip:
        assert client.post(root + "/source", files={"file": (clip.name, clip.read_bytes())}).status_code == 201
    return root


def enqueue(client, root):
    response = client.post(root + "/transcription")
    assert response.status_code == 202
    return response.json()


def test_audio_extraction_preserves_delayed_audio(studio):
    _, settings, _ = studio
    delayed = settings.data_dir / "delayed.mp4"
    subprocess.run([
        FFMPEG, "-v", "error", "-i", str(CLIP), "-itsoffset", "0.45", "-i", str(CLIP),
        "-map", "0:v", "-map", "1:a", "-c", "copy", str(delayed),
    ], check=True)
    output = settings.data_dir / "audio.wav"
    extract_audio(FFMPEG, delayed, output, lambda: None)
    with wave.open(str(output)) as wav:
        assert (wav.getnchannels(), wav.getframerate(), wav.getsampwidth()) == (1, 16000, 2)
        assert wav.readframes(5600) == bytes(11200)
        assert wav.getnframes() > 16000
        assert any(wav.readframes(16000))


def test_missing_audio_and_invalid_project(studio):
    client, settings, _ = studio
    video = settings.data_dir / "silent.mp4"
    subprocess.run([FFMPEG, "-v", "error", "-i", str(CLIP), "-an", "-c:v", "copy", str(video)], check=True)
    assert client.post(project(client, video) + "/transcription").status_code == 422
    assert client.post(project(client, None) + "/transcription").status_code == 409
    assert client.post("/projects/invalid/transcription").status_code == 422
    assert client.post(f"/projects/{uuid4()}/transcription").status_code == 404


@pytest.mark.parametrize("segments", [
    [Segment(start=0.7, end=1.1, text="", words=[])],
    [Segment(start=0.7, end=0.6, text="", words=[])],
    [Segment(start=0.1, end=0.4, text="", words=[]), Segment(start=0.3, end=0.5, text="", words=[])],
    [Segment(start=0.1, end=0.4, text="", words=[Word(start=0.2, end=0.5, text="word")])],
    [Segment(start=0.1, end=0.8, text="", words=[
        Word(start=0.2, end=0.5, text="word"), Word(start=0.4, end=0.7, text="word")
    ])],
])
def test_transcript_rejects_bad_timeline(segments):
    with pytest.raises(ValidationError):
        Transcript(project_id=uuid4(), source_media_id="source", model="small",
                   language="hi", language_confidence=0.95, source_duration=1,
                   text="", created_at=datetime.now(timezone.utc), segments=segments)


def test_model_boundary_rounding_preserves_source_clock():
    model_word = SimpleNamespace(start=0.15, end=0.25, word=" नमस्ते", probability=0.9)
    candidate = SimpleNamespace(start=0.18, end=0.26, text="नमस्ते", words=[model_word])
    segment = segment_from_model(candidate, 1, 0.16)
    assert segment.start == segment.words[0].start == 0.16
    assert segment.words[0].end == 0.25
    assert segment.text == "नमस्ते"


def test_job_completion_survives_backend_restart(studio):
    client, settings, jobs = studio
    root = project(client)
    job = enqueue(client, root)
    assert job["status"] == "queued"
    assert job["progress"] is None
    assert client.post(root + "/transcription").status_code == 409
    assert client.get(root + "/transcript").status_code == 404
    claimed = jobs.claim()
    assert claimed["status"] == "running" and claimed["started_at"]
    assert jobs.claim() is None
    Worker(settings, jobs, FakeEngine()).process(claimed)
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as restarted:
        state = restarted.get(root + "/transcription").json()
        assert state["job"]["status"] == "completed"
        assert state["job"]["progress"] == 1
        assert state["transcript_job_id"] == job["id"]
        transcript = Transcript.model_validate(restarted.get(root + "/transcript").json())
        assert transcript.source_media_id == restarted.get(root).json()["media"]["sha256"]
        assert transcript.text == "Hello"
        assert transcript.language_confidence == 0.98
        assert transcript.segments[0].words[0].start == 0.2
        assert restarted.get(root + "/source").content == CLIP.read_bytes()
    with sqlite3.connect(settings.data_dir / "studio.sqlite3") as db:
        assert db.execute("SELECT model, language FROM transcripts").fetchone() == ("mock", "en")


def test_queued_and_running_cancellation(studio):
    client, settings, jobs = studio
    root = project(client)
    queued = enqueue(client, root)
    cancel = lambda job: client.post(f"{root}/transcription/jobs/{job['id']}/cancel")
    assert cancel(queued).json()["status"] == "cancelled"
    assert jobs.claim() is None
    running = enqueue(client, root)
    claimed = jobs.claim()

    class CancellingEngine(FakeEngine):
        def transcribe(self, audio):
            assert cancel(running).json()["cancellation_requested"]
            return super().transcribe(audio)

    Worker(settings, jobs, CancellingEngine()).process(claimed)
    state = client.get(root + "/transcription").json()
    assert state["job"]["status"] == "cancelled"
    assert state["transcript_job_id"] is None


def test_failed_and_interrupted_job_retry(studio):
    client, settings, jobs = studio
    root = project(client)
    first = enqueue(client, root)

    class BrokenEngine(FakeEngine):
        def load(self):
            raise ValueError("Model configuration is invalid.")

    Worker(settings, jobs, BrokenEngine()).process(jobs.claim())
    first_url = f"{root}/transcription/jobs/{first['id']}"
    assert client.get(first_url).json()["error"] == "Model configuration is invalid."
    second = client.post(first_url + "/retry").json()
    assert second["retry_count"] == 1 and second["status"] == "queued"
    jobs.claim()
    jobs.recover()
    second_url = f"{root}/transcription/jobs/{second['id']}"
    assert client.get(second_url).json()["status"] == "interrupted"
    assert client.post(first_url + "/retry").status_code == 409
    third = client.post(second_url + "/retry").json()
    assert third["retry_count"] == 2
    Worker(settings, jobs, FakeEngine()).process(jobs.claim())
    assert client.get(root + "/transcript").status_code == 200
    other = project(client, None)
    assert client.get(f"{other}/transcription/jobs/{third['id']}").status_code == 404
    assert client.get(f"{other}/transcript", params={"job_id": third["id"]}).status_code == 404
