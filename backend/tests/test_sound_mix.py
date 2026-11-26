import hashlib
import json
import subprocess
from uuid import uuid4

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import create_app
from app.render_store import Renders
from app.sound_mix import Cue, Duck, Mix, Rights, apply_envelope, duck_curve, fit_clip
from test_render import FFMPEG, prepared
from test_transcription import studio


def test_presets_fade_loop_duck_and_automation():
    with pytest.raises(ValidationError):
        Mix(preset="unapproved")
    with pytest.raises(ValidationError):
        Cue(start_ms=0,end_ms=100,gain_db=8,fade_in_ms=0,fade_out_ms=0)
    with pytest.raises(ValidationError):
        Cue(start_ms=0,end_ms=200,fade_in_ms=140,fade_out_ms=140)
    cue=Cue(start_ms=0,end_ms=500,fade_in_ms=100,fade_out_ms=100,
            keyframes=[{"ms":0,"gain_db":0},{"ms":250,"gain_db":-6}])
    clip=np.ones((24000,2),dtype=np.float32)
    apply_envelope(clip,cue,-20)
    assert clip[0,0]==clip[-1,0]==0
    assert clip[12000,0]==pytest.approx(.0501187)
    track=np.array([[x,x] for x in (.6,.4,.3,0,.1,.2,.8)],dtype=np.float32)
    with pytest.raises(ValueError):
        fit_clip(track,12)
    fitted=fit_clip(track,12,2)
    assert len(fitted)==12
    assert fitted[5,0]==pytest.approx(.2) and fitted[6,0]==pytest.approx(.4)
    voice=np.zeros((4800+48000+48000,2),dtype=np.float32)
    voice[4800:52800]=.1
    env=duck_curve(voice,Duck(hold_ms=320),-24)
    assert env[0]==pytest.approx(1) and env[25000]<.5
    assert env[57600]<.52
    assert env[-1]>env[57600]
    assert np.all(duck_curve(voice*0,Duck(),-24)==1)
    rights=Rights(origin="owned",license="Original work",creator="Creator",confirmed=True,platforms=["instagram"])
    assert rights.permits("instagram",False) and not rights.permits("instagram",True) and not rights.permits("youtube",False)


def stream_hash(path):
    return subprocess.check_output([FFMPEG,"-v","error","-i",str(path),"-map","0:v:0","-c:v","copy","-f","hash","-"])


def test_audio_library_mix_render_and_stale_approval(studio,monkeypatch):
    client,settings,jobs=studio
    root,worker,base=prepared(studio,monkeypatch)
    original=hashlib.sha256(client.get(root+"/source").content).hexdigest()
    assert client.post(root+"/finish/grade",json={"plan_id":base["plan_id"],"preset":"ungraded@1"}).status_code==201
    old=client.post(root+"/render",json=base)
    assert old.status_code==202
    worker.process(jobs.claim())
    cache=old.json()["id"]
    route=f"{root}/render/{cache}"
    assert client.post(route+"/review",json={"decision":"approved"}).status_code==200
    a=Renders(jobs).files(root.rsplit("/",1)[1],cache,"preview.mp4")
    sha_video=stream_hash(a)
    fixture=settings.data_dir/"sound.wav"
    subprocess.run([FFMPEG,"-v","error","-f","lavfi","-i","sine=f=330:d=4","-ar","48000",str(fixture)],check=True)
    tags={"title":"Original test tone","kind":"music","mood":"ambient","tempo":90,
          "rights":{"origin":"fixture","license":"Test only","creator":"Tests"}}
    payload={"metadata":json.dumps(tags)}
    first=client.post("/sound-assets",data=payload,files={"file":("sound.wav",fixture.read_bytes())})
    assert first.status_code==201,first.text
    asset=first.json()
    assert asset["sample_rate"]==48000 and asset["channels"]==1
    assert asset["tempo_source"]=="user provided" and asset["lufs"] is not None
    assert client.post("/sound-assets",data=payload,files={"file":("sound.wav",fixture.read_bytes())}).json()["id"]==asset["id"]
    assert client.post("/sound-assets",data=payload,files={"file":("invalid.wav",b"bad")}).status_code==422
    cue={"asset_id":asset["id"],"start_ms":200,"end_ms":4200,"offset_ms":100,"loop":True,
         "fade_in_ms":300,"fade_out_ms":500,"status":"accepted"}
    body={"plan_id":base["plan_id"],"speed":1,"mix":{"preset":"mystical-ambient@1","music":cue}}
    sample=client.post(root+"/sound-mix/sample",json=body)
    assert sample.status_code==200,sample.text
    assert client.post(root+"/sound-mix/sample",json=body).json()["cached"]
    assert client.get(f"{root}/sound-mix/sample/{sample.json()['id']}/full-matched.wav").status_code==200
    saved=client.post(root+"/sound-mix",json=body)
    assert saved.status_code==201,saved.text
    assert client.get(route+"/final").status_code==409
    new=client.post(root+"/render",json=base)
    assert new.status_code==202,new.text
    worker.process(jobs.claim())
    state=client.get(root+"/render").json()
    assert state["job"]["status"]=="completed",state["job"]
    mixed=state["history"][0]
    assert mixed["metadata"]["video_stream_reused"]
    assert mixed["metadata"]["output_audio"]["input_tp"]<=-1.5
    assert abs(mixed["metadata"]["audio_seconds"]-mixed["metadata"]["video_seconds"])<.05
    assert mixed["manifest"]["mix_revision"]==saved.json()["id"]
    assert mixed["metadata"]["approval_fingerprint"] != state["history"][1]["metadata"]["approval_fingerprint"]
    b=Renders(jobs).files(root.rsplit("/",1)[1],mixed["id"],"preview.mp4")
    assert stream_hash(b)==sha_video
    response=client.post(f"{root}/render/{mixed['id']}/review",json={"decision":"approved"})
    assert response.status_code==409 and "rights" in response.text.lower()
    assert hashlib.sha256(client.get(root+"/source").content).hexdigest()==original
    with TestClient(create_app(settings),base_url="http://127.0.0.1") as restored:
        assert restored.get(root+"/sound-mix").json()["current"]["id"]==saved.json()["id"]
        assert asset["id"] in [i["id"] for i in restored.get("/sound-assets").json()["items"]]
    pinned=settings.data_dir/"sound"/asset["id"]/"original.wav"
    pinned.unlink()
    assert client.post(root+"/sound-mix",json=body).status_code==409
