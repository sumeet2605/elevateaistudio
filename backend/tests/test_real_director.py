import os

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.jobs import Jobs
from app.main import create_app
from app.projects import Projects
from app.transcription import FasterWhisper, TranscriptionSettings
from app.worker import Worker
from test_transcription import FIXTURES, project


@pytest.mark.skipif(os.getenv("ELEVATE_REAL_DIRECTOR") != "1", reason="Live OpenAI test incurs charges")
def test_live_coaching_sample(tmp_path, monkeypatch):
    env = Settings.from_env()
    if not os.getenv("OPENAI_API_KEY"):
        pytest.skip("No OpenAI API key is configured")
    monkeypatch.setenv("ELEVATE_DIRECTOR_MODE", "openai")
    settings = Settings(data_dir=tmp_path, ffmpeg=env.ffmpeg, ffprobe=env.ffprobe)
    jobs = Jobs(Projects(tmp_path))
    worker = Worker(settings, jobs, FasterWhisper(TranscriptionSettings.from_env(env.data_dir)))
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        root = project(client, FIXTURES / "studio-paused.mp4")
        for path in ("/transcription", "/analysis"):
            assert client.post(root + path).status_code == 202
            worker.process(jobs.claim())
        base = root + "/director"
        job = client.post(base, json={"playbook": "mystical-sumeet", "confirm_paid": True})
        assert job.status_code == 202
        worker.process(jobs.claim())
        state = client.get(base).json()
        assert state["job"]["status"] == "completed", state["job"]["error"]
        plan = client.get(f"{base}/revisions/{state['selected']}").json()
        assert plan["validation"]["status"] == "passed"
        assert plan["mode"] == "openai"
        usage = state["usage"][0]
        print(f"Live model={usage['model']}; input={usage['input_tokens']}; "
              f"output={usage['output_tokens']}; cost_usd={usage['cost_usd']}; "
              f"validated_operations={len(plan['operations'])}")
