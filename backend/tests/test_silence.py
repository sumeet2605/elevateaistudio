import math
import subprocess
import wave
from datetime import datetime, timezone
from uuid import uuid4

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.analysis_store import ANALYSIS
from app.audio_analysis import detect_quiet, effective_threshold, measure_audio
from app.main import create_app
from app.silence import PRESETS, DetectionSettings, EditingPlan, suggest
from app.timeline import Span, Timeline, merge_spans
from app.transcription import Segment, Transcript, Word
from app.worker import Worker
from test_transcription import FFMPEG, FakeEngine, project, studio


def span(start, end):
    return Span(start_ms=start, end_ms=end)


def write_wav(path, samples):
    with wave.open(str(path), "wb") as output:
        output.setparams((samples.shape[1] if samples.ndim == 2 else 1, 2, 16000, 0, "NONE", "not compressed"))
        output.writeframes(samples.astype("<i2").tobytes())


def transcript(word_start, word_end):
    return Transcript(
        project_id=uuid4(), source_media_id="fixture", model="fixture", language="en",
        language_confidence=1, source_duration=10, text="quiet word", created_at=datetime.now(timezone.utc),
        segments=[Segment(start=word_start, end=word_end, text="quiet word",
                          words=[Word(start=word_start, end=word_end, text="quiet word")])],
    )


@pytest.fixture
def synthetic_audio(tmp_path):
    rng = np.random.default_rng(3)
    def tone(duration, peak):
        return np.sin(np.arange(round(duration * 16000)) * (2 * math.pi * 220 / 16000)) * peak
    def silence(duration):
        return np.zeros(round(duration * 16000))
    left = np.concatenate([
        silence(1.5), tone(1, 10000), silence(0.4), tone(0.6, 10000), silence(2),
        tone(0.5, 300), rng.integers(-1500, 1500, 32000), silence(2),
    ])
    # Opposite phase in stereo exposes destructive downmix bugs.
    stereo = np.column_stack((left, -left))
    path = tmp_path / "synthetic.wav"
    write_wav(path, stereo)
    return path


def test_audio_measurements_and_amplitude_detection(synthetic_audio):
    updates = []
    metrics = measure_audio(synthetic_audio, updates.append)
    assert metrics["channels"] == 2
    assert metrics["duration_seconds"] == pytest.approx(10)
    assert metrics["peak_dbfs"] == pytest.approx(-10.31, abs=0.02)
    assert -25 < metrics["rms_dbfs"] < -20
    assert metrics["noise_floor_dbfs"] == -120
    assert metrics["clipped_samples"] == 0
    assert len(metrics["waveform"]["peaks"]) <= 600
    assert max(metrics["waveform"]["peaks"]) > 0.3
    assert updates[-1] == 1 and updates == sorted(updates)
    quiet = detect_quiet(FFMPEG, synthetic_audio, -35, 800, 10000, lambda: None)
    assert quiet == [span(0, 1500), span(3500, 6000), span(8000, 10000)]


def test_silent_and_clipped_pcm(tmp_path):
    silent = tmp_path / "silent.wav"
    write_wav(silent, np.zeros(32000))
    assert measure_audio(silent, lambda _: None)["peak_dbfs"] == -120
    assert detect_quiet(FFMPEG, silent, -35, 800, 2000, lambda: None) == [span(0, 2000)]
    loud = tmp_path / "clip.wav"
    write_wav(loud, np.array([32767, -32768] * 800))
    metrics = measure_audio(loud, lambda _: None)
    assert metrics["clipped_samples"] == 1600 and metrics["clip_sample_fraction"] == 1
    assert metrics["peak_dbfs"] == 0


def test_presets_and_noise_floor():
    assert [item.minimum_silence_ms for item in PRESETS.values()] == [1200, 800, 500]
    assert [item.leading_padding_ms for item in PRESETS.values()] == [300, 200, 150]
    assert effective_threshold(DetectionSettings(), -34) == -35
    adaptive = DetectionSettings(noise_aware=True)
    assert effective_threshold(adaptive, -40) == -34
    assert effective_threshold(adaptive, -15) == -25
    assert effective_threshold(adaptive, -70) == -35
    with pytest.raises(ValidationError):
        DetectionSettings(threshold_dbfs=-100)


def test_hybrid_merges_intervals_and_protects_quiet_words():
    settings = PRESETS["balanced"]
    cuts = suggest(settings, 10000, [
        span(0, 1200), span(1000, 1600), span(2500, 2900), span(3500, 6000), span(8000, 10000),
    ], [span(1600, 2500), span(2900, 3500)], transcript(5.5, 6))
    assert all(item.source.end_ms <= 2500 or item.source.start_ms >= 2900 for item in cuts)
    assert cuts[0].confidence == "high"
    assert cuts[0].proposed == span(200, 1350)
    assert set(cuts[0].methods) == {"amplitude", "vad", "transcript"}
    assert any(item.confidence == "ambiguous" and item.source.start_ms == 6050 for item in cuts)
    for item in cuts:
        assert item.proposed.end_ms <= 5450 or item.proposed.start_ms >= 6050
    without_words = suggest(settings, 2000, [span(0, 2000)], [], None)
    assert without_words[0].confidence == "ambiguous"


def test_timeline_edges_adjacent_and_reverse():
    cuts = [span(0, 1000), span(3000, 4000), span(4000, 5000), span(9000, 10000)]
    assert merge_spans(cuts, 10000) == [span(0, 1000), span(3000, 5000), span(9000, 10000)]
    timeline = Timeline.from_cuts(10000, cuts)
    assert timeline.duration_ms == 6000
    assert timeline.source_to_edited(0) is None
    assert timeline.source_to_edited(4000) is None
    assert timeline.source_to_edited(9000) is None
    assert timeline.source_to_edited(10000) == 6000
    assert timeline.source_to_edited(6000) == 3000
    assert timeline.edited_to_source(0) == 1000
    assert timeline.edited_to_source(2000) == 5000
    assert timeline.edited_to_source(3000) == 6000
    assert timeline.edited_to_source(6000) == 9000
    with pytest.raises(ValueError):
        timeline.edited_to_source(6001)
    with pytest.raises(ValueError):
        Timeline(10000, [span(20, 100), span(30, 140)])
    with pytest.raises(ValidationError):
        span(100, 10)
    with pytest.raises(ValueError):
        Timeline.from_cuts(10000, [span(9000, 10001)])


def test_analysis_jobs_review_restart_export_and_history(studio, synthetic_audio, monkeypatch):
    client, settings, jobs = studio
    clip = settings.data_dir / "analysis-fixture.mp4"
    subprocess.run([
        FFMPEG, "-v", "error", "-f", "lavfi", "-i", "color=size=64x64:rate=10",
        "-i", str(synthetic_audio), "-shortest", "-c:v", "libx264", "-c:a", "aac", str(clip),
    ], check=True)
    root = project(client, clip)
    base = root + "/analysis"
    assert client.get(base).json()["settings"]["minimum_silence_ms"] == 1200
    assert client.post(base + "/settings", json=PRESETS["balanced"].model_dump()).status_code == 200
    assert client.post(root + "/transcription").status_code == 202
    worker = Worker(settings, jobs, FakeEngine())
    worker.process(jobs.claim())
    stage = client.post(base).json()
    assert stage["type"] == ANALYSIS
    assert client.post(base).status_code == 409
    assert client.get(f"{root}/transcription/jobs/{stage['id']}").status_code == 404
    assert client.post(f"{base}/jobs/{stage['id']}/cancel").json()["status"] == "cancelled"
    failed = client.post(base).json()
    assert jobs.claim()["id"] == failed["id"]
    jobs.recover()
    retry_url = f"{base}/jobs/{failed['id']}/retry"
    stage = client.post(retry_url).json()
    assert stage["retry_count"] == 1
    monkeypatch.setattr("app.analysis_worker.detect_speech", lambda *_: [span(1800, 3200), span(5500, 6000)])
    worker.process(jobs.claim())
    assert client.get(base).json()["job"]["status"] == "completed"
    stored = client.get(base + "/plan").json()
    assert stored["transcript_version"]
    assert client.get(base + "/waveform").json()["channels"] == 2
    cuts = stored["proposed_cuts"]
    assert len(cuts) >= 3
    high = next(cut for cut in cuts if cut["confidence"] == "high")
    ambiguous = next(cut for cut in cuts if cut["confidence"] == "ambiguous")
    reviews = f"{base}/plans/{stored['id']}/suggestions/"
    assert client.post(reviews + ambiguous["id"], json={"status": "rejected"}).status_code == 200
    assert client.post(reviews + high["id"], json={"status": "modified", "modification": span(0, 9900).model_dump()}).status_code == 422
    custom = span(high["proposed"]["start_ms"], high["proposed"]["end_ms"] - 50)
    assert client.post(reviews + high["id"], json={"status": "modified", "modification": custom.model_dump()}).status_code == 200
    assert client.post(f"{base}/plans/{stored['id']}/accept-high").status_code == 200
    saved = client.get(base + "/plan").json()
    assert saved["estimated_edited_duration_ms"] < saved["source_duration_ms"]
    assert any(item["status"] == "rejected" for item in saved["proposed_cuts"])

    with TestClient(create_app(settings), base_url="http://127.0.0.1") as restarted:
        export = restarted.get(base + "/export")
        assert export.headers["content-disposition"].startswith("attachment;")
        assert export.json() == saved
        assert EditingPlan.model_validate(export.json()).id
        assert restarted.get(base).json()["settings"]["minimum_silence_ms"] == 800

    monkeypatch.setattr("app.analysis_worker.measure_audio", lambda *_: pytest.fail("Unexpected cache miss"))
    settings_with_more_padding = PRESETS["balanced"].model_copy(update={"leading_padding_ms": 250})
    assert client.post(base + "/settings", json=settings_with_more_padding.model_dump()).status_code == 200
    assert client.post(base + "/regenerate").status_code == 202
    worker.process(jobs.claim())
    current = client.get(base + "/plan").json()
    assert current["id"] != saved["id"] and current["approved_cuts"] == []
    assert current["detection"]["leading_padding_ms"] == 250
    assert client.get(base + "/plan", params={"plan_id": saved["id"]}).json() == saved
    assert client.post(reviews + high["id"], json={"status": "rejected"}).status_code == 409
    assert client.get(root + "/source").content == clip.read_bytes()


def test_running_analysis_cancellation_does_not_publish(studio, monkeypatch):
    client, settings, jobs = studio
    root = project(client)
    base = root + "/analysis"
    queued = client.post(base).json()
    claimed = jobs.claim()

    def interrupted(*_):
        response = client.post(f"{base}/jobs/{queued['id']}/cancel")
        assert response.json()["cancellation_requested"]
        return []

    monkeypatch.setattr("app.analysis_worker.detect_speech", interrupted)
    Worker(settings, jobs, FakeEngine()).process(claimed)
    assert client.get(base).json()["job"]["status"] == "cancelled"
    assert client.get(base + "/waveform").status_code == 404
    assert not list(settings.data_dir.glob("projects/*/audio-analysis-*"))
