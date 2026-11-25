import hashlib
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.finishing import GRADES, VOICES, Grade, Voice, grade_filter, loudnorm, measure_audio, speech_filter
from app.ffmpeg_render import ffmpeg_args
from test_render import FFMPEG, manifest_for, prepared
from test_transcription import FIXTURES, project, studio
from app.worker import Worker
from app.main import create_app
from fastapi.testclient import TestClient


def test_preset_validation_and_filter_order():
    assert len(GRADES) == 5 and len(VOICES) == 4
    assert grade_filter(GRADES["ungraded@1"][1]) == ""
    with pytest.raises(ValidationError):
        Grade(temperature=.8)
    with pytest.raises(ValidationError):
        Voice(target=-2)
    with pytest.raises(ValidationError):
        Voice(presence="4:arbitrary")
    grade = GRADES["mystical-warmth@1"][1]
    manifest = manifest_for(grade=grade, voice=Voice())
    hdr = ffmpeg_args("ffmpeg", Path("source.mov"), manifest, 0, 1, color_transfer="arib-std-b67")
    graph = hdr[hdr.index("-filter_complex") + 1]
    assert graph.index("tonemap") < graph.index("colorbalance") < graph.index("ass=filename")
    assert "bt709" in hdr
    sdr = ffmpeg_args("ffmpeg", Path("source.mov"), manifest, 0, 1, color_transfer="bt709")
    assert "tonemap" not in sdr[sdr.index("-filter_complex") + 1]
    assert manifest_for(grade_fingerprint="a"*64).approval_fingerprint == manifest.plan_fingerprint


@pytest.mark.parametrize("source", [
    "0.16*sin(2*PI*170*t)+0.04*sin(2*PI*570*t)",
    "0.16*sin(2*PI*170*t)+0.015*sin(2*PI*60*t)+0.008*(2*random(4)-1)",
    "0.022*sin(2*PI*180*t)",
    "0.55*sin(2*PI*175*t)+0.12*sin(2*PI*550*t)",
    "if(between(t,1.2,2.2),0.007*sin(2*PI*600*t),0.09*sin(2*PI*170*t))",
    "0",
], ids=["clean-proxy","noise-proxy","quiet-proxy","loud-proxy","reflective-proxy","silence"])
def test_real_audio_filters_preserve_sample_clock_and_measure_peaks(tmp_path, source):
    original = tmp_path/"original.wav"
    processed = tmp_path/"processed.wav"
    subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i",
                    f"aevalsrc='{source}':s=48000:d=4", str(original)], check=True, capture_output=True)
    voice = Voice(denoise=3 if "random" in source else 0)
    processed_input = measure_audio(FFMPEG, original, chain=speech_filter(voice))
    first_pass = measure_audio(FFMPEG, original)
    if source == "0":
        assert first_pass["input_i"] is None and first_pass["input_tp"] is None
        return
    subprocess.run([FFMPEG, "-v", "error", "-i", str(original), "-af",
                    f"{speech_filter(voice)}{loudnorm(voice, processed_input)},aresample=48000,apad,atrim=end_sample=192000",
                    str(processed)], check=True, capture_output=True)
    final = measure_audio(FFMPEG, processed)
    assert final["input_i"] is not None and final["input_tp"] <= voice.peak
    import wave
    with wave.open(str(processed)) as reader:
        assert reader.getnframes() == 192000 and reader.getframerate() == 48000


def test_finishing_revisions_are_immutable_and_invalidate_approval(studio, monkeypatch):
    client, settings, jobs = studio
    root, worker, payload = prepared(studio, monkeypatch)
    entry = client.post(root + "/render", json=payload).json()
    worker.process(jobs.claim())
    route = f"{root}/render/{entry['id']}"
    assert client.post(route + "/review", json={"decision":"approved"}).status_code == 200
    source = client.get(root + "/source").content
    finish = root + "/finish"
    first = client.post(finish+"/grade", json={"plan_id": payload["plan_id"], "preset":"ungraded@1"})
    second = client.post(finish+"/grade", json={"plan_id": payload["plan_id"], "preset":"mystical-warmth@1"})
    sound = client.post(finish+"/voice", json={"plan_id": payload["plan_id"], "preset":"elevate-voice@1"})
    assert first.status_code == second.status_code == sound.status_code == 201
    assert [row["id"] for row in client.get(finish).json()["history"]["grade"]] == [second.json()["id"], first.json()["id"]]
    assert client.get(route + "/final").status_code == 409
    assert client.get(route + "/preview").status_code == 200
    new = client.post(root + "/render", json=payload)
    assert new.status_code == 202, new.text
    worker.process(jobs.claim())
    result = client.get(root+"/render").json()
    assert result["job"]["status"] == "completed", result["job"]["error"]
    item = result["history"][0]
    assert item["metadata"]["color_transfer"] == "bt709"
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as restarted:
        assert restarted.get(finish).json()["history"]["grade"][0]["id"] == second.json()["id"]
        assert restarted.get(finish).json()["history"]["voice"][0]["id"] == sound.json()["id"]
    assert item["manifest"]["voice_revision"] == sound.json()["id"]
    assert item["metadata"]["output_audio"]["input_tp"] is None or item["metadata"]["output_audio"]["input_tp"] <= -1.45
    assert abs(item["metadata"]["video_seconds"] - item["metadata"]["audio_seconds"]) <= .04
    assert hashlib.sha256(client.get(root+"/source").content).digest() == hashlib.sha256(source).digest()


def test_short_picture_only_analysis_is_cached(studio):
    client, settings, jobs = studio
    clip = settings.data_dir/"silent-picture.mp4"
    subprocess.run([FFMPEG, "-v", "error", "-i", str(FIXTURES/"studio-synthetic.mp4"),
                    "-an", "-t", "0.4", "-c:v", "copy", str(clip)], check=True)
    root = project(client, clip) + "/finish"
    first = client.post(root+"/analysis", json={"grade":{"intensity":0}})
    assert first.status_code == 202, first.text
    Worker(settings, jobs, None).process(jobs.claim())
    state = client.get(root).json()
    assert state["job"]["status"] == "completed", state["job"]["error"]
    assert state["analysis"]["audio"] is None
    assert "graded.mp4" in state["analysis"]["available"]
    assert client.post(root+"/analysis", json={"grade":{"intensity":0}}).json()["analysis"]["id"] == first.json()["job"]["id"]
