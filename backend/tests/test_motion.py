import hashlib
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.captions import ass_asset
from app.face_detection import detector_key
from app.ffmpeg_render import ffmpeg_args, streams
from app.main import create_app
from app.motion_models import CameraSettings, Face, FaceSettings, Focus, MotionEvent, Sample, clamp_crop, track_faces
from app.motion_render import smooth_delta
from app.render_manifest import RenderSettings
from app.render_worker import camera_notice
from test_render import FFMPEG, FFPROBE, manifest_for, prepared, span
from test_transcription import studio


def event(start=1630, end=2430, **options):
    return MotionEvent(source=span(start, end), **{"status": "accepted", **options})


def test_tracking_keeps_last_subject_and_respects_crop_edges():
    subject = Face(x=.25, y=.1, width=.2, height=.3, confidence=.92)
    other = Face(x=.7, y=.2, width=.24, height=.2, confidence=.9)
    near = subject.model_copy(update={"x": .26})
    later = subject.model_copy(update={"x": .38})
    samples = [Sample(source_ms=i*500, faces=faces) for i, faces in enumerate(
        [[], [subject], [subject, other], [], [other], [near], [later]])]
    track = track_faces(samples, .65, .025, .32)
    assert track[0] == (500, subject.centre)
    assert all(point == subject.centre for _, point in track[:-1])
    assert track[-1][1].x - subject.centre.x == pytest.approx(.0416)
    assert clamp_crop(Focus(x=0, y=0), .4, .6, headroom=True) == (0, 0)
    assert clamp_crop(Focus(x=1, y=1), .4, .6, headroom=True) == (.6, .4)
    with pytest.raises(ValidationError):
        Focus(x=1.2)
    with pytest.raises(ValidationError):
        Face(x=.9, y=0, width=.2, height=.3, confidence=.95)


def test_easing_limits_seams_and_fingerprint():
    easing = [smooth_delta(event(easing=mode), 31, 54)
              for mode in ("linear", "ease-in", "ease-out", "ease-in-out")]
    assert len(set(easing)) == 4
    assert all("*clip(min(on-31,54-1-on)/6,0,1)" in expression for expression in easing)
    plain = manifest_for()
    moved = manifest_for(camera=CameraSettings(), motion_events=[event()],
                         motion_revision=uuid4(), motion_fingerprint="a"*64)
    assert moved.map_motion_span(span(1630, 2430)) == (31, 54)
    assert moved.encoded_ms == plain.encoded_ms
    assert moved.approval_fingerprint != moved.plan_fingerprint
    assert camera_notice(moved, {"width": 1920, "height": 1080, "tags": {"rotate": "90"}})
    assert camera_notice(moved, {"width": 3840, "height": 2160, "side_data_list": [{"rotation": -90}]}) is None
    for events, message in (
        ([event(800, 1800)], "edit seam"),
        ([event(), event(1800, 2200)], "overlap"),
        ([event(end=3100)], "duration"),
        ([event(end_zoom=110)], "108%"),
    ):
        with pytest.raises(ValidationError, match=message):
            manifest_for(camera=CameraSettings(), motion_events=events)
    with pytest.raises(ValidationError, match="100 milliseconds"):
        event(1700, 1750)


def test_face_cache_motion_history_and_export_approval(studio, monkeypatch):
    client, settings, jobs = studio
    root, worker, render_payload = prepared(studio, monkeypatch)
    source = client.get(root + "/source").content
    checksum = hashlib.sha256(source).hexdigest()
    assert detector_key(checksum, FaceSettings()) != detector_key(checksum, FaceSettings(frequency=3))
    calls = []

    def fake_detector(*_args, **_options):
        calls.append(1)
        sample = Sample(source_ms=1500, faces=[Face(x=.2, y=.2, width=.2, height=.3, confidence=.9)])
        return {"width": 640, "height": 360, "samples": [sample.model_dump()]}

    monkeypatch.setattr("app.motion_store.detect_faces", fake_detector)
    route = root + "/motion"
    assert client.post(route + "/analysis", json={"frequency": 2}).status_code == 202
    worker.process(jobs.claim())
    state = client.get(route).json()
    face = state["analysis"]
    assert face["summary"]["single"] == 1
    assert client.post(route + "/analysis", json={"frequency": 2}).json() == {"analysis": face, "cached": True}
    assert len(calls) == 1
    motion = {"plan_id": render_payload["plan_id"], "settings": {"framing": "face"},
              "events": [], "face_version": face["id"]}
    first = client.post(route, json=motion)
    assert first.status_code == 201, first.text
    render_route = root + "/render"
    trial = client.post(render_route, json={**render_payload, "settings": {"playback_speed": 1.1}})
    assert trial.status_code == 202, trial.text
    worker.process(jobs.claim())
    assert client.get(render_route).json()["job"]["status"] == "completed"
    preview = f"{render_route}/{trial.json()['id']}"
    assert client.post(preview + "/review", json={"decision": "approved"}).status_code == 200
    assert client.get(preview + "/final").status_code == 200
    restored = client.post(route, json={**motion, "settings": {"framing": "manual", "focus": {"x": .2, "y": .1}}})
    assert restored.status_code == 201, restored.text
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as restarted:
        assert [item["id"] for item in restarted.get(route).json()["history"]] == [
            restored.json()["id"], first.json()["id"]]
        assert restarted.get(preview + "/preview").status_code == 200
        assert restarted.get(preview + "/final").status_code == 409
        assert restarted.get(root + "/source").content == source


def test_mp4_has_visible_animated_zoom_and_unchanged_audio_clock(tmp_path):
    source = tmp_path / "grid.mp4"
    subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i",
                    "color=black:s=180x320:r=30:d=3,drawbox=x=36:y=70:w=108:h=180:color=white:t=fill",
                    "-f", "lavfi", "-i", "sine=d=3", "-c:v", "libx264", "-c:a", "aac", str(source)], check=True)
    folder = Path(__file__).parents[1] / "app" / "render_fonts"
    shutil.copytree(folder, tmp_path / "fonts")
    manifests = (
        manifest_for(cuts=[span(1000, 1400)], emphasis=[], settings=RenderSettings(playback_speed=1.1)),
        manifest_for(cuts=[span(1000, 1400)], emphasis=[], settings=RenderSettings(playback_speed=1.1),
                     camera=CameraSettings(framing="face"), face_track=[(0, Focus(x=.6, y=.3)), (2400, Focus(x=.58, y=.4))],
                     motion_events=[event(1400, 2900, start_zoom=102, end_zoom=108)]),
    )
    previews = []
    for index, manifest in enumerate(manifests):
        (tmp_path / "captions.ass").write_text(ass_asset(manifest.brand, []))
        args = ffmpeg_args(FFMPEG, source, manifest, 0, 1)
        graph = args[args.index("-filter_complex") + 1]
        if index:
            assert graph.index("zoompan") < graph.index("ass=filename")
        subprocess.run(args, cwd=tmp_path, capture_output=True, check=True)
        target = tmp_path / f"motion-{index}.mp4"
        (tmp_path / "preview.mp4").rename(target)
        previews.append(target)
    lengths = [(float(v["duration"]), float(a["duration"])) for v, a, _ in
               (streams(FFPROBE, item) for item in previews)]
    assert lengths[0] == lengths[1] and abs(lengths[1][0] - lengths[1][1]) < .04

    def white_width(path, timestamp):
        frame = subprocess.run([FFMPEG, "-v", "error", "-ss", timestamp, "-i", str(path),
                                "-vf", "scale=180:320,format=gray", "-frames:v", "1", "-f", "rawvideo", "-"],
                               check=True, capture_output=True).stdout
        return np.count_nonzero(np.frombuffer(frame, dtype=np.uint8).reshape(320, 180)[145] > 190)

    assert abs(white_width(previews[0], "0.5") - white_width(previews[1], "0.5")) <= 2
    assert white_width(previews[1], "1.95") > white_width(previews[0], "1.95") + 4
