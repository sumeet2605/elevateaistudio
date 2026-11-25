import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.brand import Brand, Playbook
from app.director import (PLAYBOOKS, DirectorError, mock_response, provider, request_body,
                          usage_record, validate_recommendation)
from app.main import create_app
from app.worker import Worker
from test_transcription import FakeEngine, project, studio


def context():
    return {
        "context_id": "run", "duration_ms": 10000, "approved_cuts": [{"start_ms": 0, "end_ms": 1000}],
        "rejected_ranges": [{"start_ms": 7000, "end_ms": 8000}],
        "eligible_cuts": [{"start_ms": 4500, "end_ms": 5400}],
        "minimum_cut_ms": 200,
        "words": [{"start_ms": 1400, "end_ms": 1700, "text": "Take"},
                  {"start_ms": 1700, "end_ms": 2200, "text": "a breath"}],
        "brand": Brand().model_dump(), "playbook": PLAYBOOKS["mystical-sumeet"].model_dump(),
    }


def test_brand_and_playbook_schemas():
    with pytest.raises(ValidationError):
        Brand(primary="gold")
    with pytest.raises(ValidationError):
        Brand(width=1081)
    assert len(PLAYBOOKS) == 4
    with pytest.raises(ValidationError):
        Playbook.model_validate({**PLAYBOOKS["educational"].model_dump(), "revision": 99})


@pytest.mark.parametrize("problem", ["bounds", "cut", "word", "overlap", "unknown", "identity", "zoom", "command"])
def test_director_rejects_unsafe_recommendations(problem):
    data = context()
    value = json.loads(mock_response(data))
    first = value["operations"][0]
    if problem == "bounds":
        first["start_ms"] = -1
    elif problem == "cut":
        first.update(kind="cut", start_ms=2, end_ms=300, first_word=None, last_word=None)
    elif problem == "word":
        first["first_word"] = 22
    elif problem == "overlap":
        zoom = next(item for item in value["operations"] if item["kind"] == "zoom")
        value["operations"].insert(0, dict(zoom))
    elif problem == "unknown":
        first["command"] = "command"
    elif problem == "identity":
        value["context_id"] = "other"
    elif problem == "zoom":
        next(item for item in value["operations"] if item["kind"] == "zoom")["zoom_percent"] = 120
    else:
        first["reason"] = "Use /tmp/out.mp4"
    with pytest.raises(DirectorError):
        validate_recommendation(json.dumps(value), data)


def test_mock_reuses_recorded_words_and_timeline():
    data = context()
    _, operations, timeline = validate_recommendation(mock_response(data), data)
    assert operations[0]["quote"] == "Take a breath"
    assert timeline.source_to_edited(1400) == 400
    assert timeline.source_to_edited(5000) is None
    assert timeline.edited_to_source(3500) == 5400
    assert timeline.duration_ms == 8100


def test_structured_request_and_pricing(monkeypatch):
    snapshot = {"mode": "openai", "model": "gpt-5-mini", "max_output": 3000,
                "rates": {"input": 0.25, "cached": 0.025, "output": 2}}
    body = request_body(snapshot, context())
    assert body["store"] is False
    assert body["text"]["format"]["strict"] is True
    schema = body["text"]["format"]["schema"]
    assert set(schema["required"]) == schema["properties"].keys()
    usage = usage_record(snapshot, 1000, {"input_tokens": 900, "output_tokens": 200,
                                         "input_tokens_details": {"cached_tokens": 100}})
    assert usage["cost_usd"] == pytest.approx(0.0006025)
    from urllib.error import HTTPError
    def error(*_args, **_kwargs):
        raise HTTPError("", 429, "", {}, None)
    monkeypatch.setattr("app.director.urlopen", error)
    with pytest.raises(DirectorError, match="HTTP 429"):
        provider(snapshot, context(), "test-key")


def ready_project(studio, monkeypatch):
    client, settings, jobs = studio
    root = project(client)
    worker = Worker(settings, jobs, FakeEngine())
    monkeypatch.setattr("app.analysis_worker.detect_speech", lambda *_: [])
    assert client.post(root + "/transcription").status_code == 202
    worker.process(jobs.claim())
    assert client.post(root + "/analysis").status_code == 202
    worker.process(jobs.claim())
    assert client.get(root + "/analysis").json()["job"]["status"] == "completed"
    return root, worker


def test_brand_history_and_director_review_restart(studio, monkeypatch):
    monkeypatch.setenv("ELEVATE_DIRECTOR_MODE", "mock")
    client, settings, jobs = studio
    root, worker = ready_project(studio, monkeypatch)
    brand_url = root + "/brand"
    first = client.get(brand_url).json()[0]
    revision = client.post(brand_url, json={**first["settings"], "highlight": "#FFFF00"}).json()
    base = root + "/director"
    assert client.get(base + "/estimate", params={"playbook": "educational"}).json()["estimated_input_tokens"] > 0
    body = {"playbook": "educational"}
    job = client.post(base, json=body).json()
    assert client.post(base, json=body).status_code == 409
    worker.process(jobs.claim())
    state = client.get(base).json()
    assert state["job"]["status"] == "completed", state["job"]
    assert state["usage"][0]["input_tokens"] is None
    assert state["usage"][0]["cost_usd"] == 0
    draft = base + "/revisions/" + job["id"]
    assert client.get(draft).json()["brand_revision"] == revision["id"]
    restored = client.post(f'{brand_url}/{first["id"]}/restore').json()
    assert restored["id"] not in (first["id"], revision["id"])
    assert client.get(brand_url).json()[1]["settings"]["highlight"] == "#FFFF00"
    reviewed = client.post(draft + "/operations/0", json={"status": "accepted"}).json()
    assert reviewed["reviewed_events"][0]["edited_start_ms"] == 200
    assert client.get(draft).json()["operations"][0]["status"] == "pending"
    assert client.post(draft + "/operations/0", json={"status": "rejected"}).status_code == 409
    assert client.post(draft + "/restore").status_code == 201
    other = project(client, None)
    for url in [f"{other}/director/revisions/{reviewed['id']}", f"{other}/brand/{revision['id']}/restore"]:
        response = client.post(url) if url.endswith("/restore") else client.get(url)
        assert response.status_code == 404
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as restarted:
        export = restarted.get(f"{base}/revisions/{reviewed['id']}/export")
        assert export.status_code == 200 and export.json()["operations"][0]["status"] == "accepted"
        assert len(restarted.get(base + "/revisions").json()) == 3


def test_paid_consent_failure_retry_limit_and_cancellation(studio, monkeypatch):
    client, _, jobs = studio
    root, worker = ready_project(studio, monkeypatch)
    base = root + "/director"
    monkeypatch.setenv("ELEVATE_DIRECTOR_MODE", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    assert client.post(base, json={"playbook": "authority"}).status_code == 422
    response = client.post(base, json={"playbook": "authority", "confirm_paid": True})
    assert response.status_code == 202
    queued = response.json()
    assert client.post(f'{base}/jobs/{queued["id"]}/cancel').json()["status"] == "cancelled"

    def invalid(*_):
        return "not-json", {"input_tokens": 200, "output_tokens": 20}
    monkeypatch.setattr("app.director_worker.provider", invalid)
    response = client.post(base, json={"playbook": "authority", "confirm_paid": True})
    assert response.status_code == 202
    for retry_count in range(3):
        worker.process(jobs.claim())
        state = client.get(base).json()
        assert state["job"]["status"] == "failed"
        assert state["job"]["retry_count"] == retry_count
        assert "strict editing schema" in state["job"]["error"]
        assert state["usage"][0]["input_tokens"] == 200
        retry = f'{base}/jobs/{state["job"]["id"]}/retry'
        if retry_count < 2:
            assert client.post(retry, json={}).status_code == 422
            assert client.post(retry, json={"confirm_paid": True}).status_code == 202
        else:
            assert client.post(retry, json={"confirm_paid": True}).status_code == 409


def test_silence_changes_make_old_plan_read_only(studio, monkeypatch):
    monkeypatch.setenv("ELEVATE_DIRECTOR_MODE", "mock")
    client, _, jobs = studio
    root, worker = ready_project(studio, monkeypatch)
    base = root + "/director"
    job = client.post(base, json={"playbook": "storytelling"}).json()
    worker.process(jobs.claim())
    draft = f"{base}/revisions/{job['id']}"
    assert client.get(draft).json()["stale_silence"] is False
    assert client.post(root + "/analysis/regenerate").status_code == 202
    worker.process(jobs.claim())
    assert client.get(draft).json()["stale_silence"] is True
    assert client.post(draft + "/operations/0", json={"status": "accepted"}).status_code == 409
    assert client.get(draft + "/export").status_code == 409
    assert client.get(base + "/revisions").status_code == 200


def test_mock_hook_preserves_sentence_boundary():
    data = context()
    data["words"][1]["text"] = "a breath."
    data["words"].append({"start_ms": 2500, "end_ms": 2700, "text": "Take"})
    _, operations, _ = validate_recommendation(mock_response(data), data)
    assert operations[0]["quote"] == "Take a breath."
    assert operations[0]["end_ms"] == 2200


def test_mock_emphasizes_key_words_instead_of_entire_hook():
    data = context()
    words = ["What", "if", "the", "same", "problem", "is", "repeating?"]
    data["words"] = [{"text": word, "start_ms": 1400 + 200*i, "end_ms": 1570 + 200*i}
                     for i, word in enumerate(words)]
    _, decisions, _ = validate_recommendation(mock_response(data), data)
    assert next(op["quote"] for op in decisions if op["kind"] == "hook") == "What if the same problem is repeating?"
    assert next(op["quote"] for op in decisions if op["kind"] == "emphasis") == "same problem"
