import hashlib
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient

from app.audio import AudioError
from app.brand import Brand
from app.captions import ass_asset, captions_for, escape_ass
from app.ffmpeg_render import ffmpeg_args, streams
from app.main import create_app
from app.render_manifest import RenderManifest, RenderSettings, frame_spans
from app.render_store import Renders, digest
from app.timeline import Span, Timeline
from app.transcription import Segment, Transcript, Word
from app.worker import Worker
from test_transcription import FIXTURES, FFMPEG, FFPROBE, FakeEngine, project, studio


def span(start, end):
    return Span(start_ms=start, end_ms=end)


def manifest_for(duration=3000, cuts=None, **options):
    cuts = cuts or [span(1001, 1580)]
    retained = list(Timeline.from_cuts(duration, cuts).retained)
    return RenderManifest(**{
        "project_id": uuid4(), "plan_id": uuid4(), "plan_fingerprint": "a" * 64, "source_media_id": "b" * 64,
        "transcript_revision": uuid4(), "transcript_fingerprint": "c" * 64, "brand_revision": "default-v1",
        "playbook_revision": "mystical-sumeet@1", "brand": Brand(), "settings": RenderSettings(),
        "source_duration_ms": duration, "cuts": cuts, "retained": retained,
        "frame_ranges": frame_spans(retained), "emphasis": [span(1650, 2000)], "voice_leveling": False,
        "notices": [], **options,
    })


def test_safe_compiler_uses_frames_and_samples():
    manifest = manifest_for()
    source = Path("weird;echo command.mp4").resolve()
    args = ffmpeg_args("ffmpeg", source, manifest, 2, 4)
    assert args[args.index("-i") + 1] == str(source)
    assert all("echo" not in item for item in args if item != str(source))
    graph = args[args.index("-filter_complex") + 1]
    assert "trim=start_frame=48:end_frame=90" in graph
    assert "atrim=start_sample=76800:end_sample=144000" in graph
    assert "concat=n=2:v=1:a=1" in graph
    assert "afade=" in graph
    assert "pad=1080:1920" in graph
    assert "captions.ass" in graph and "fontsdir=fonts" in graph
    assert "-n" in args and args[-1] == "preview.mp4"
    crop = ffmpeg_args("ffmpeg", source, manifest_for(settings=RenderSettings(framing="crop", quality="high"), voice_leveling=True), 0, 1)
    assert "crop=1080:1920" in crop[crop.index("-filter_complex") + 1]
    assert "acompressor" in crop[crop.index("-filter_complex") + 1]
    assert crop[crop.index("-crf") + 1] == "17"
    with pytest.raises(ValidationError):
        RenderSettings.model_validate({"framing": "fit;command"})
    with pytest.raises(ValidationError):
        manifest_for(brand=Brand(width=720))


def test_caption_mapping_seam_fallback_and_styling():
    manifest = manifest_for()
    transcript = Transcript(
        project_id=manifest.project_id, source_media_id=manifest.source_media_id, model="test", language="hi",
        language_confidence=1, source_duration=3, text="", created_at=datetime.now(timezone.utc),
        segments=[
            Segment(start=0.05, end=0.93, text="", words=[
                Word(start=0.05, end=0.18, text=r"A{\N}"), Word(start=0.24, end=0.36, text="pause"),
                Word(start=0.91, end=0.93, text="near")]),
            Segment(start=0.96, end=1.20, text="seam", words=[]),
            Segment(start=1.71, end=2.2, text="", words=[
                Word(start=1.71, end=1.93, text="नमस्ते"), Word(start=1.97, end=2.2, text="world")]),
            Segment(start=2.62, end=2.85, text="आज", words=[]),
        ],
    )
    cues, omitted = captions_for(manifest, transcript)
    assert omitted == 1
    assert all(len(cue.lines) <= 2 for cue in cues)
    assert all(a.end_ms <= b.start_ms for a, b in zip(cues, cues[1:]))
    word = next(word for cue in cues for line in cue.lines for word in line if word.text == "नमस्ते")
    assert word.start_ms == 1110
    assert word.highlighted
    ass = ass_asset(manifest.brand, cues)
    assert "Noto Sans Devanagari" in ass
    assert "&H007FC7EF" in ass and "&H00FFFFFF" in ass
    assert "320,1" in ass
    assert "seam" not in ass
    assert r"\{" in escape_ass(r"{\N}")
    assert r"\N" not in escape_ass(r"{\N}")


def test_devanagari_glyphs_drawn_by_libass(tmp_path):
    fixture = Path(__file__).parents[1] / "app" / "render_fonts"
    shutil.copytree(fixture, tmp_path / "fonts")
    manifest = manifest_for()
    transcript = Transcript(
        project_id=manifest.project_id, source_media_id=manifest.source_media_id, model="test", language="hi",
        language_confidence=1, source_duration=3, text="नमस्ते World", created_at=datetime.now(timezone.utc),
        segments=[Segment(start=0.0, end=0.6, text="नमस्ते World", words=[
            Word(start=0, end=0.2, text="नमस्ते"), Word(start=0.25, end=0.60, text="World")])],
    )
    (tmp_path / "captions.ass").write_text(ass_asset(manifest.brand, captions_for(manifest, transcript)[0]), encoding="utf-8")
    rendered = subprocess.run([
        FFMPEG, "-hide_banner", "-v", "verbose", "-f", "lavfi", "-i", "color=black:s=270x480",
        "-vf", "ass=filename=captions.ass:fontsdir=fonts,format=gray", "-frames:v", "1",
        "-f", "rawvideo", "-",
    ], cwd=tmp_path, capture_output=True, check=True)
    assert b"Noto Sans Devanagari" in rendered.stderr
    assert b"failed to find any fallback" not in rendered.stderr
    assert max(rendered.stdout) > 70


class SpokenFixtureEngine(FakeEngine):
    def transcribe(self, _audio):
        words = [SimpleNamespace(word=text, start=begin, end=end, probability=1) for text, begin, end in [
            ("Growth", 1.49, 1.86), ("starts", 1.87, 2.13), ("with", 2.14, 2.39), ("one", 2.40, 2.67),
            ("small", 2.68, 2.96), ("decision.", 2.97, 3.75),
        ]]
        more = [SimpleNamespace(word="Take", start=6.22, end=6.37, probability=1),
                SimpleNamespace(word="a", start=6.38, end=6.48, probability=1),
                SimpleNamespace(word="breath.", start=6.49, end=7.0, probability=1)]
        segments = [SimpleNamespace(start=items[0].start, end=items[-1].end,
                                    text=" ".join(w.word for w in items), words=items) for items in (words, more)]
        return iter(segments), SimpleNamespace(language="en", language_probability=1)


def prepared(studio, monkeypatch):
    client, settings, jobs = studio
    monkeypatch.setenv("ELEVATE_DIRECTOR_MODE", "mock")
    # Keep run-of-the-mill CI independent of model packages. The fixture itself has recorded speech.
    monkeypatch.setattr("app.analysis_worker.detect_speech", lambda *_: [span(1400, 4000), span(6000, 15000)])
    source = FIXTURES / "studio-paused.mp4"
    root = project(client, source)
    worker = Worker(settings, jobs, SpokenFixtureEngine())
    for suffix in ("/transcription", "/analysis"):
        assert client.post(root + suffix).status_code == 202
        worker.process(jobs.claim())
    analysis = client.get(root + "/analysis/plan").json()
    cut = analysis["proposed_cuts"][0]
    assert client.post(f"{root}/analysis/plans/{analysis['id']}/suggestions/{cut['id']}",
                       json={"status": "accepted"}).status_code == 200
    assert client.post(root + "/director", json={"playbook": "mystical-sumeet"}).status_code == 202
    worker.process(jobs.claim())
    draft = client.get(root + "/director").json()["selected"]
    draft_path = f"{root}/director/revisions/{draft}"
    plan = client.get(draft_path).json()
    emphasis = next(i for i, op in enumerate(plan["operations"]) if op["kind"] == "emphasis")
    review = client.post(f"{draft_path}/operations/{emphasis}", json={"status": "accepted"})
    assert review.status_code == 201
    return root, worker, {"plan_id": review.json()["id"], "settings": {}}


def test_real_render_retry_approval_history_and_stale_gate(studio, monkeypatch):
    client, settings, jobs = studio
    root, worker, payload = prepared(studio, monkeypatch)
    path = root + "/render"
    original = client.get(root + "/source").content
    source_hash = hashlib.sha256(original).hexdigest()
    response = client.post(path, json=payload)
    assert response.status_code == 202, response.text
    failed = response.json()["id"]
    assert client.post(path, json=payload).status_code == 409
    with monkeypatch.context() as m:
        def fail(*_):
            raise AudioError("Encoder unavailable for test.")
        m.setattr("app.render_worker.run_ffmpeg", fail)
        worker.process(jobs.claim())
    assert client.get(f"{path}/jobs/{failed}").json()["status"] == "failed"
    retry = client.post(f"{path}/jobs/{failed}/retry")
    assert retry.status_code == 202 and retry.json()["retry_count"] == 1
    attempt = retry.json()["id"]
    worker.process(jobs.claim())
    state = client.get(path).json()
    assert state["job"]["status"] == "completed", state["job"]["error"]
    assert len(state["history"]) == 1
    complete = state["history"][0]
    assert complete["id"] == attempt != failed
    assert complete["manifest"]["source_media_id"] == source_hash
    assert complete["metadata"]["caption_sample"][0]["time_ms"] < 1490
    assert complete["metadata"]["caption_sample"][0]["lines"][0][0]["highlighted"]
    media_path = f"{path}/{attempt}"
    preview = client.get(media_path + "/preview")
    final = client.get(media_path + "/final")
    assert preview.status_code == 200 and final.status_code == 409
    assert client.get(media_path + "/preview", headers={"range": "bytes=0-21"}).status_code == 206
    assert client.post(media_path + "/review", json={"decision": "approved"}).status_code == 200
    exported = client.get(media_path + "/final")
    assert exported.status_code == 200
    assert exported.content == preview.content
    copy = settings.data_dir / "export.mp4"
    copy.write_bytes(exported.content)
    video, audio, container = streams(FFPROBE, copy)
    assert (video["codec_name"], audio["codec_name"], video["width"], video["height"], video["pix_fmt"]) == (
        "h264", "aac", 1080, 1920, "yuv420p")
    assert abs(float(video["duration"]) - float(audio["duration"])) < 0.04
    expected = sum(s["end_ms"] - s["start_ms"] for s in complete["manifest"]["retained"]) / 1000
    assert abs(float(container["duration"]) - expected) <= 2 / 30 + 0.001
    assert client.post(media_path + "/review", json={"decision": "rejected"}).status_code == 200
    assert client.get(media_path + "/final").status_code == 409
    assert client.post(media_path + "/review", json={"decision": "approved"}).status_code == 200
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as restarted:
        assert restarted.get(media_path + "/final").content == preview.content
        brand = restarted.get(root + "/brand").json()[0]["settings"]
        assert restarted.post(root + "/brand", json={**brand, "highlight": "#F0CC80"}).status_code == 201
        assert restarted.get(media_path + "/final").status_code == 409
        assert restarted.get(media_path + "/preview").content == preview.content
        assert restarted.post(media_path + "/review", json={"decision": "approved"}).status_code == 409
        assert restarted.get(media_path + "/render").status_code == 404
    assert client.get(root + "/source").content == original
    rendered_file = Renders(jobs).directory(UUID(root.split("/")[-1]), attempt) / "preview.mp4"
    assert digest(rendered_file) == complete["metadata"]["sha256"]
    with sqlite3.connect(settings.data_dir / "studio.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM render_reviews WHERE render_id=?", (attempt,)).fetchone()[0] == 3


def test_running_render_cancellation_leaves_no_output(studio, monkeypatch):
    client, settings, jobs = studio
    root, worker, payload = prepared(studio, monkeypatch)
    path = root + "/render"
    queued = client.post(path, json=payload)
    assert queued.status_code == 202
    attempt = queued.json()["id"]

    def cancel(_args, directory, _length, callback):
        (directory / "preview.mp4").write_bytes(b"partial")
        assert client.post(f"{path}/jobs/{attempt}/cancel").json()["cancellation_requested"]
        callback(0.3)
    monkeypatch.setattr("app.render_worker.run_ffmpeg", cancel)
    worker.process(jobs.claim())
    assert client.get(path).json()["job"]["status"] == "cancelled"
    assert client.get(f"{path}/{attempt}/preview").status_code == 404
    assert not list(settings.data_dir.glob("projects/*/renders/*"))


@pytest.mark.parametrize("speed", [1.0, 1.1])
def test_accepted_zoom_renders_on_the_edited_clock(studio, monkeypatch, speed):
    client, _, jobs = studio
    root, worker, payload = prepared(studio, monkeypatch)
    current = f"{root}/director/revisions/{payload['plan_id']}"
    plan = client.get(current).json()
    zoom = next(i for i, op in enumerate(plan["operations"]) if op["kind"] == "zoom")
    new = client.post(f"{current}/operations/{zoom}", json={"status": "accepted"}).json()
    response = client.post(root + "/render", json={**payload, "plan_id": new["id"], "settings": {"playback_speed": speed}})
    assert response.status_code == 202, response.text
    manifest = Renders(jobs).manifest(response.json()["id"])
    (start, end, percent), = manifest.edited_zooms()
    assert start < round(plan["operations"][zoom]["start_ms"] * 30 / 1000)
    assert end > start and percent == plan["operations"][zoom]["zoom_percent"]
    graph = ffmpeg_args(FFMPEG, Path("source.mp4"), manifest, 0, 1)
    graph = graph[graph.index("-filter_complex") + 1]
    assert f"between(on,{start},{end - 1})" in graph
    assert graph.index("zoompan") < graph.index("ass=filename")
    worker.process(jobs.claim())
    result = client.get(root + "/render").json()
    assert result["job"]["status"] == "completed", result["job"]["error"]
    assert result["history"][0]["manifest"]["zooms"][0]["percent"] == percent
    metadata = result["history"][0]["metadata"]
    assert abs(metadata["duration_seconds"] - manifest.total_frames / 30 / speed) < 0.05
    assert abs(metadata["video_seconds"] - metadata["audio_seconds"]) < 0.05
    if speed != 1:
        assert "atempo=1.1" in graph and graph.index("setpts=PTS/1.1") < graph.index("ass=filename")
        first_retained = manifest.frame_ranges[0]
        second_retained = manifest.frame_ranges[1]
        unscaled_ms = 1490 + round(first_retained.duration * 1000 / 30) - round(second_retained.start_frame * 1000 / 30)
        assert metadata["caption_sample"][0]["time_ms"] == 10 * int(unscaled_ms / 1.1 / 10)


def test_audio_and_video_markers_together_after_cut(tmp_path):
    import numpy as np

    source = tmp_path / "marker.mp4"
    subprocess.run([
        FFMPEG, "-v", "error", "-f", "lavfi", "-i",
        "color=black:size=96x64:rate=30:duration=3,drawbox=color=white:t=fill:enable='between(t,2,2.3)'",
        "-f", "lavfi", "-i",
        "aevalsrc='if(between(t,2,2.3),0.4*sin(2*PI*440*t),0)':s=48000:d=3",
        "-c:v", "libx264", "-c:a", "aac", str(source),
    ], check=True)
    fixture = Path(__file__).parents[1] / "app" / "render_fonts"
    shutil.copytree(fixture, tmp_path / "fonts")
    manifest = manifest_for(cuts=[span(1000, 1400)], emphasis=[])
    (tmp_path / "captions.ass").write_text(ass_asset(manifest.brand, []), encoding="utf-8")
    subprocess.run(ffmpeg_args(FFMPEG, source, manifest, 0, 1), cwd=tmp_path, check=True, capture_output=True)
    preview = tmp_path / "preview.mp4"
    sound = subprocess.run([FFMPEG, "-v", "error", "-i", str(preview),
                            "-map", "0:a", "-ac", "1", "-f", "f32le", "-"], capture_output=True, check=True)
    samples = np.frombuffer(sound.stdout, dtype="<f4")
    before = samples[int(1.50 * 48000):int(1.57 * 48000)]
    after = samples[int(1.64 * 48000):int(1.71 * 48000)]
    assert np.max(np.abs(before)) < 0.02 < np.max(np.abs(after))
    assert np.max(np.abs(samples)) < 0.98
    def centre_at(seconds):
        result = subprocess.run([
            FFMPEG, "-v", "error", "-ss", str(seconds), "-i", str(preview),
            "-vf", "crop=64:64,format=gray", "-frames:v", "1", "-f", "rawvideo", "-",
        ], capture_output=True, check=True)
        return np.frombuffer(result.stdout, dtype="u1").mean()
    assert centre_at(1.55) < 20 and centre_at(1.65) > 220


def test_hdr_tone_map_is_applied_before_captions_and_tagged_sdr():
    manifest = manifest_for()
    source = Path("source.mov")
    for transfer in ("arib-std-b67", "smpte2084"):
        command = ffmpeg_args("ffmpeg", source, manifest, 0, 1, color_transfer=transfer)
        graph = command[command.index("-filter_complex") + 1]
        assert graph.index("tonemap=hable") < graph.index("ass=filename")
        assert command[command.index("-color_trc") + 1] == "bt709"
        assert command[command.index("-color_primaries") + 1] == "bt709"
    normal = ffmpeg_args("ffmpeg", source, manifest, 0, 1, color_transfer="bt709")
    assert all("tonemap" not in arg for arg in normal)
