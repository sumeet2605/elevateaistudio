import hashlib
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

FIXTURES = Path(__file__).parent / "fixtures"
CLIP = (FIXTURES / "studio-synthetic.mp4").read_bytes()
FFPROBE = os.environ.get("ELEVATE_FFPROBE", "ffprobe")


@pytest.fixture
def studio(tmp_path):
    settings = Settings(data_dir=tmp_path, ffprobe=FFPROBE, max_upload_bytes=len(CLIP) + 1024)
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        yield client, settings


def new_project(client):
    response = client.post("/projects", json={"title": "  Make space for growth  "})
    assert response.status_code == 201
    assert response.json()["title"] == "Make space for growth"
    return response.json()


def upload(client, url, name="source.mp4", content=CLIP):
    return client.post(url, files={"file": (name, content, "application/octet-stream")})


def require_ffprobe():
    if not shutil.which(FFPROBE):
        pytest.fail("FFprobe is required for media integration tests. Set ELEVATE_FFPROBE or install it.")


def test_create_validate_and_persist(studio):
    client, settings = studio
    assert client.post("/projects", json={"title": "  "}).status_code == 422
    assert client.post("/projects", json={"title": "blocked"}, headers={
        "origin": "https://other.example",
    }).status_code == 403
    project = new_project(client)
    assert project["brand_id"] == "elevate-with-sumeet"
    assert project["media"] is None
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as restarted:
        assert restarted.get("/projects").json() == [project]
    assert client.get("/projects/../../").status_code == 404
    assert client.post("/projects/not-a-uuid/source", files={"file": ("file.mp4", CLIP)}).status_code == 422
    assert client.get("/projects", headers={"host": "other.example"}).status_code == 400
    assert client.get("/health").json()["max_upload_bytes"] == settings.max_upload_bytes


@pytest.mark.parametrize(("name", "content", "status"), [
    ("video.avi", CLIP, 415),
    ("video.mp4", b"", 422),
    ("big.mp4", CLIP * 2, 413),
    ("huge.mp4", b"x" * 100_000, 413),
])
def test_rejected_upload_leaves_project_empty(studio, name, content, status):
    client, settings = studio
    project = new_project(client)
    url = f"/projects/{project['id']}/source"
    assert upload(client, url, name, content).status_code == status
    assert client.get(url).status_code == 404
    assert not list((settings.data_dir / "projects").glob("*/*"))


@pytest.mark.parametrize("extension", ["mp4", "mov"])
def test_probe_failed_upload_then_retry_and_preview(studio, extension):
    require_ffprobe()
    client, settings = studio
    project = new_project(client)
    url = f"/projects/{project['id']}/source"
    assert upload(client, url, content=b"not a movie").status_code == 415
    assert client.get(url).status_code == 404
    assert list((settings.data_dir / "projects" / project["id"]).iterdir()) == []

    name = f"../test.{extension}"
    footage = (FIXTURES / f"studio-synthetic.{extension}").read_bytes()
    response = upload(client, url, name, footage)
    assert response.status_code == 201, response.text
    media = response.json()["media"]
    assert media["filename"] == f"test.{extension}"
    assert (media["width"], media["height"]) == (160, 90)
    assert media["duration_seconds"] == pytest.approx(1, abs=0.04)
    assert (media["video_codec"], media["audio_codec"]) == ("h264", "aac")
    assert media["sha256"] == hashlib.sha256(footage).hexdigest()
    assert "stored_name" not in media
    assert upload(client, url).status_code == 409
    partial = client.get(url, headers={"Range": "bytes=0-31"})
    assert partial.status_code == 206
    assert partial.content == footage[:32]
    assert client.get(url).content == footage
    assert len(list((settings.data_dir / "projects" / project["id"]).iterdir())) == 1


def test_parallel_uploads_have_one_winner(studio):
    require_ffprobe()
    client, settings = studio
    project = new_project(client)
    url = f"/projects/{project['id']}/source"
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda n: upload(client, url, f"take-{n}.mp4").status_code, range(2)))
    assert sorted(statuses) == [201, 409]
    assert len(list((settings.data_dir / "projects" / project["id"]).iterdir())) == 1


def test_missing_ffprobe_leaves_no_partial_file(tmp_path):
    settings = Settings(data_dir=tmp_path, ffprobe=str(tmp_path / "missing"))
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        url = f"/projects/{new_project(client)['id']}/source"
        assert client.get("/health").json()["tools"]["ffprobe"] is False
        assert upload(client, url).status_code == 503
        assert client.get(url).status_code == 404
        assert not list((tmp_path / "projects").glob("*/*"))

def test_upload_accepts_only_one_file(studio):
    client, _settings = studio
    url = f"/projects/{new_project(client)['id']}/source"
    response = client.post(url, files=[
        ("file", ("one.mp4", b"x")),
        ("file", ("two.mp4", b"x")),
    ])
    assert response.status_code == 400
    assert client.get(url).status_code == 404
