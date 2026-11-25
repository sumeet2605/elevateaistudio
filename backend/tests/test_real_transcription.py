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


@pytest.mark.skipif(os.getenv("ELEVATE_REAL_TRANSCRIPTION") != "1", reason="Opt in to real model inference")
def test_real_silence_analysis(tmp_path):
    import hashlib

    from app.silence import EditingPlan

    env = Settings.from_env()
    settings = Settings(data_dir=tmp_path, ffmpeg=env.ffmpeg, ffprobe=env.ffprobe)
    clip = FIXTURES / "studio-paused.mp4"
    source_id = hashlib.sha256(clip.read_bytes()).hexdigest()
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        root = project(client, clip)
        jobs = Jobs(Projects(tmp_path))
        worker = Worker(settings, jobs, FasterWhisper(TranscriptionSettings.from_env(env.data_dir)))
        enqueue(client, root)
        worker.process(jobs.claim())
        transcript = Transcript.model_validate(client.get(root + "/transcript").json())
        assert "growth starts with one small decision" in transcript.text.lower()

        base = root + "/analysis"
        assert client.post(base).status_code == 202
        started = perf_counter()
        worker.process(jobs.claim())
        elapsed = perf_counter() - started
        state = client.get(base).json()
        assert state["job"]["status"] == "completed", state["job"]["error"]
        plan = EditingPlan.model_validate(client.get(base + "/export").json())
        assert plan.source_media_id == source_id
        assert len(plan.proposed_cuts) >= 3
        for cut in plan.proposed_cuts:
            for segment in transcript.segments:
                for word in segment.words:
                    assert cut.proposed.start_ms / 1000 >= word.end or cut.proposed.end_ms / 1000 <= word.start
        approved = client.post(f"{base}/plans/{plan.id}/accept-high")
        assert approved.status_code == 200
        revised = EditingPlan.model_validate(approved.json())
        assert revised.approved_cuts
        assert hashlib.sha256(client.get(root + "/source").content).hexdigest() == source_id

        assert client.post(base + "/regenerate").status_code == 202
        started = perf_counter()
        worker.process(jobs.claim())
        cached_elapsed = perf_counter() - started
        assert client.get(base + "/plan").json()["approved_cuts"] == []
        assert client.get(base + "/plan", params={"plan_id": str(plan.id)}).json() == approved.json()
        print(f"Silence analysis: {elapsed:.3f} s fresh, {cached_elapsed:.3f} s cached; "
              f"source={plan.source_duration_ms / 1000:.3f} s; "
              f"candidates={len(plan.proposed_cuts)}; "
              f"suggested={sum(c.removable_duration_ms for c in plan.proposed_cuts) / 1000:.3f} s; "
              f"approved={(plan.source_duration_ms - revised.estimated_edited_duration_ms) / 1000:.3f} s; "
              f"edited={revised.estimated_edited_duration_ms / 1000:.3f} s; checksum={source_id}")
