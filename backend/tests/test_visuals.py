import hashlib
import io
import json
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.asset_providers import ProviderError, fetch
from app.captions import ass_asset
from app.director import DirectorError, mock_response, validate_recommendation
from app.ffmpeg_render import ffmpeg_args
from app.main import create_app
from app.render_manifest import RenderSettings
from app.visuals import BoundVisual, VisualAsset, VisualEvent
from test_director import context
from test_render import FFMPEG, manifest_for, prepared, span
from test_transcription import studio


def test_visual_schema_and_director_word_validation():
    with pytest.raises(ValidationError):
        VisualEvent(source=span(0, 200))
    with pytest.raises(ValidationError):
        VisualEvent(source=span(0, 900), placement="overlay")
    with pytest.raises(ValidationError):
        VisualEvent(source=span(0, 900), status="accepted")
    data = context()
    draft = json.loads(mock_response(data))
    draft["visual_ideas"] = [{"first_word":0,"last_word":50,"concept":"Visual example","purpose":"example",
                              "asset_type":"stock_image","queries":["example"],"prompt":None,"rationale":"Adds context."}]
    with pytest.raises(DirectorError, match="recorded words"):
        validate_recommendation(json.dumps(draft), data)
    draft["visual_ideas"][0]["last_word"]=1
    validate_recommendation(json.dumps(draft),data)


def test_fetch_rejects_unsafe_urls_and_excessive_response(monkeypatch):
    for url in ("http://images.pexels.com/x", "https://127.0.0.1/x",
                "https://images.pexels.com.example.com/x","https://user@images.pexels.com/x"):
        with pytest.raises(ProviderError):
            fetch(url,{"images.pexels.com"})
    from email.message import Message
    class Reply(io.BytesIO):
        headers = Message()
    Reply.headers["Content-Type"] = "image/jpeg"
    Reply.headers["Content-Length"] = "100"
    class Opener:
        def open(self,*_a,**_k):
            return Reply(b"1234")
    monkeypatch.setattr("app.asset_providers.build_opener",lambda *_:Opener())
    with pytest.raises(ProviderError,match="exceeds"):
        fetch("https://images.pexels.com/x",{"images.pexels.com"},types=("image/jpeg",),maximum=3)


def make_image(path):
    subprocess.run([FFMPEG,"-v","error","-f","lavfi","-i","color=red:s=96x160",
                    "-frames:v","1","-threads","1",str(path)],check=True,capture_output=True)


def test_library_deduplication_and_visual_history(studio, monkeypatch):
    client, settings, jobs = studio
    root, worker, payload=prepared(studio,monkeypatch)
    image=settings.data_dir/"picture.png"
    make_image(image)
    files={"file":("picture.png",image.read_bytes())}
    first=client.post("/assets",files=files,data={"category":"Journaling","tags":"book, quiet"})
    assert first.status_code==201,first.text
    item=first.json()
    assert client.post("/assets",files=files).json()["id"]==item["id"]
    assert client.post("/assets",files={"file":("fake.jpg",b"not an image")}).status_code==422
    assert client.post(f"/assets/{item['id']}/favourite",json={"value":True}).status_code==200
    assert client.get("/assets?q=book&favourite=true").json()["items"][0]["id"]==item["id"]
    assert client.get(f"/assets/{item['id']}/thumbnail").status_code==200
    assert client.get("/visual-stock?provider=mock&kind=video").json()["items"][0]["id"]=="crossroads"
    graphics=client.post(root+"/visuals/card",json={"plan_id":payload["plan_id"],"template":"flow","lines":["विचार","Emotion","Action"]})
    assert graphics.status_code==201,graphics.text
    assert graphics.json()["kind"]=="video"
    outcome=client.post("/visual-stock/import",json={"provider":"mock","asset_id":"routine","kind":"image"})
    assert outcome.status_code==201,outcome.text
    assert outcome.json()["provenance"]["license"]=="Local synthetic demo"
    prior=client.post(root+"/render",json=payload)
    assert prior.status_code==202,prior.text
    worker.process(jobs.claim())
    route=f"{root}/render/{prior.json()['id']}"
    assert client.post(route+"/review",json={"decision":"approved"}).status_code==200
    event={"source":{"start_ms":1600,"end_ms":2400},"asset_id":item["id"],"status":"accepted"}
    data={"plan_id":payload["plan_id"],"speed":1.1,"events":[event]}
    response=client.post(root+"/visuals",json=data)
    assert response.status_code==201,response.text
    assert client.post(root+"/visuals",json={**data,"events":[event,event]}).status_code==422
    assert client.post(root+"/render",json=payload).status_code==409
    assert client.get(route+"/preview").status_code==200
    assert client.get(route+"/final").status_code==409
    new_render=client.post(root+"/render",json={**payload,"settings":{"playback_speed":1.1}})
    assert new_render.status_code==202,new_render.text
    (settings.data_dir/"assets"/item["id"]/"original.png").unlink()
    worker.process(jobs.claim())
    assert client.get(root+"/render").json()["job"]["status"]=="failed"
    with TestClient(create_app(settings),base_url="http://127.0.0.1") as restarted:
        assert restarted.get(root+"/visuals").json()["current"]["id"]==response.json()["id"]
        assert restarted.get("/assets?q=book").json()["items"][0]["usage"]


def test_paid_image_accounting_and_mock_job(studio,monkeypatch):
    client,settings,jobs=studio
    root,worker,payload=prepared(studio,monkeypatch)
    route=root+"/visuals/generate"
    monkeypatch.setenv("OPENAI_API_KEY","fake")
    monkeypatch.setenv("ELEVATE_IMAGE_LOW_CENTS","6")
    monkeypatch.setenv("ELEVATE_IMAGE_PROJECT_CAP_CENTS","5")
    body={"provider":"openai","prompt":"A quiet empty desk"}
    assert client.post(route,json=body).status_code==422
    assert client.post(route,json={**body,"approval_cents":6}).status_code==409
    free={"provider":"mock","prompt":"A quiet empty desk"}
    assert client.post(route,json=free).status_code==202
    worker.process(jobs.claim())
    assert client.get(root+"/visuals").json()["job"]["asset_id"]
    assert client.post(route,json=free).status_code==409


def test_image_and_video_cutaways_at_exact_frames(tmp_path):
    from numpy import frombuffer,uint8
    fixture=Path(__file__).parents[1]/"app"/"render_fonts"
    shutil.copytree(fixture,tmp_path/"fonts")
    source=tmp_path/"source.mp4"
    video=tmp_path/"cutaway.mp4"
    picture=tmp_path/"cutaway.png"
    subprocess.run([FFMPEG,"-v","error","-f","lavfi","-i","color=blue:s=96x160:r=30:d=3",
                    "-f","lavfi","-i","sine=d=3","-c:v","libx264","-c:a","aac",str(source)],check=True)
    subprocess.run([FFMPEG,"-v","error","-f","lavfi","-i","color=green:s=96x160:r=30:d=2",
                    "-c:v","libx264",str(video)],check=True)
    make_image(picture)
    def insert(path,kind,begin,end):
        asset=VisualAsset(id=uuid4(),checksum=hashlib.sha256(path.read_bytes()).hexdigest(), kind=kind,
                          extension=path.suffix,duration=2 if kind=="video" else None,provenance={})
        return BoundVisual(asset=asset,event=VisualEvent(asset_id=asset.id,source=span(begin,end),status="accepted"))
    inserts=[insert(picture,"image",400,900),insert(video,"video",1650,2400)]
    manifest=manifest_for(visuals=inserts,settings=RenderSettings(playback_speed=1.1))
    (tmp_path/"captions.ass").write_text(ass_asset(manifest.brand,[]))
    args=ffmpeg_args(FFMPEG,source,manifest,0,1,visual_paths=[picture,video])
    subprocess.run(args,cwd=tmp_path,check=True,capture_output=True)
    encoded=tmp_path/"preview.mp4"
    raw=subprocess.check_output([FFMPEG,"-v","error","-i",str(encoded),"-vf",
                                "scale=8:8,format=rgb24","-f","rawvideo","-"])
    pixels=frombuffer(raw,dtype=uint8).reshape(-1,8,8,3)[:,4,4,:]
    assert len(pixels)==manifest.output_frames
    for item,channel in zip(inserts,(0,1)):
        start,end=manifest.visual_frames(item.event.source)
        assert all(index==channel for index in pixels[start:end].argmax(axis=1))
        assert pixels[start-1].argmax()==pixels[end].argmax()==2
    with pytest.raises(ValidationError,match="overlap"):
        manifest_for(visuals=[inserts[0],insert(video,"video",500,900)])
    too_short = inserts[1].model_copy(update={"asset":inserts[1].asset.model_copy(update={"duration":.2})})
    with pytest.raises(ValidationError,match="shorter"):
        manifest_for(visuals=[too_short])
    with pytest.raises(ValidationError,match="removed interval"):
        manifest_for(visuals=[insert(video,"video",800,1800)])
