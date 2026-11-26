import hashlib
import io
import subprocess

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import create_app
from app.render_store import Renders
from app.studio_key import PRESET_ADJUSTMENTS, StudioSettings, sample_key
from test_render import FFMPEG, FFPROBE, prepared
from test_transcription import studio

GREEN="0x00BB40"


def test_background_sampler_conservative_key():
    frames=np.tile(np.array([8,168,64],dtype=np.uint8),(4,60,40,1))
    frames[:,:15]=[8,138,59]
    frames[:,18:53,10:33]=[150,100,70]
    result=sample_key(frames)
    assert result["detected"] and result["shadow_fraction"]>=0
    assert result["key"].startswith("#") and .1<=result["similarity"]<=.24
    frames[:]=[203,180,170]
    assert not sample_key(frames)["detected"]
    with pytest.raises(ValidationError):
        StudioSettings(key="green,drawbox")
    with pytest.raises(ValidationError):
        StudioSettings(feather=12)
    with pytest.raises(ValidationError):
        StudioSettings(enabled=True,mode="asset")


def check(client,path,status=200,method="GET",**kw):
    result=client.request(method,path,**kw)
    assert result.status_code==status,result.text[:500] if not result.is_success else result.status_code
    return result


def test_ffmpeg_key_matte_foreground_and_background_loop(studio,tmp_path):
    client,_,_=studio
    source=tmp_path/"green.mp4"
    background=tmp_path/"blue.mp4"
    picture=tmp_path/"blue.png"
    # Compressed low-resolution subject with uneven screen, moving hard edges and a matching green patch.
    subprocess.run([FFMPEG,"-v","error","-f","lavfi","-i",f"color={GREEN}:s=180x320:r=30:d=3",
        "-vf","drawbox=x=0:y=0:w=24:h=320:color=0x009735:t=fill,drawbox=x=49:y=97:w=85:h=175:color=0xCF755A:t=fill,"
        "drawbox=x=83:y=156:w=17:h=28:color=0x00BB40:t=fill,drawbox=x=24:y=150:w=40:h=30:color=0xB97761:t=fill:enable='between(t,1,2)'",
        "-c:v","libx264","-crf","27",str(source)],check=True)
    subprocess.run([FFMPEG,"-v","error","-f","lavfi","-i","color=blue:s=180x320:r=24:d=1",
                    "-c:v","libx264",str(background)],check=True)
    subprocess.run([FFMPEG,"-v","error","-i",str(background),"-frames:v","1","-threads","1",str(picture)],check=True)
    project=check(client,"/projects",201,"POST",json={"title":"Key fixture"}).json()
    root=f"/projects/{project['id']}"
    check(client,root+"/source",201,"POST",files={"file":(source.name,source.read_bytes())})
    report=check(client,root+"/studio/analyse",200,"POST").json()
    assert report["detected"] and check(client,root+"/studio/analyse",200,"POST").json()["cached"]
    opts={"enabled":True,"mode":"solid","background_color":"#0000FF","key":"#00bb40","similarity":.13,
          "refine":"erode","feather":.5,"spill":.45,"foreground_scale":.8}
    for mode in ("original","matte","composite","foreground","black","white"):
        row=check(client,root+"/studio/preview",200,"POST",json={"settings":opts,"mode":mode,"duration":2}).json()
        result=check(client,f"{root}/studio/preview/{row['id']}.{row['extension']}")
        if mode=="matte":
            path=tmp_path/"mask.mp4";path.write_bytes(result.content)
            rgb=subprocess.check_output([FFMPEG,"-v","error","-i",str(path),"-vf","format=gray","-frames:v","1","-f","rawvideo","-"])
            matte=np.frombuffer(rgb,dtype=np.uint8).reshape(640,360)
            assert matte[120,60]<12 and matte[245,150]>230
    for media in (picture,background):
        asset=check(client,"/assets",201,"POST",files={"file":(media.name,media.read_bytes())},
                    data={"category":"Virtual Studio","creator":"Test fixture","license":"Synthetic test asset"}).json()
        composite={**opts,"mode":"asset","background_id":asset["id"],"background_fit":"contain"}
        if media==background:
            check(client,root+"/studio/preview",422,"POST",json={"settings":composite,"duration":2})
            composite["loop"]=True
        sample=check(client,root+"/studio/preview",200,"POST",json={"settings":composite,"duration":2}).json()
        assert sample["duration"]==2
    check(client,"/assets",422,"POST",files={"file":("fake.png",b"invalid image")})
    preset = "mystical-cinematic@2"
    assert check(client,root+"/studio").json()["preset_adjustments"][preset]["foreground_scale"] == 1.05
    v2 = {**opts, **PRESET_ADJUSTMENTS[preset], "preset":preset,"foreground_sharpen":.12}
    assert check(client,root+"/studio/preview",200,"POST",json={"settings":v2,"duration":2}).json()["duration"] == 2
    sample = check(client,root+"/studio/preview",200,"POST",
                   json={"settings":v2,"mode":"matte","start_ms":1400,"duration":2}).json()
    mask = tmp_path/"v2-mask.mp4"
    mask.write_bytes(check(client,f"{root}/studio/preview/{sample['id']}.mp4").content)
    raw = subprocess.check_output([FFMPEG,"-v","error","-i",str(mask),"-vf","format=gray",
                                   "-frames:v","1","-f","rawvideo","-"])
    matte = np.frombuffer(raw,dtype=np.uint8).reshape(640,360)
    assert matte[320,82] > 230  # moving hand
    assert matte[246,150] > 230  # subject core
    assert matte[100,65] < 12   # backdrop


@pytest.mark.parametrize("options",[
    {"foreground_midtone":.2},{"foreground_sharpen":.21},{"foreground_scale":1.11},
])
def test_v2_foreground_controls_are_bounded(options):
    with pytest.raises(ValidationError):
        StudioSettings(preset="mystical-cinematic@2",**options)


def test_studio_revision_invalidates_only_current_approval(studio,monkeypatch):
    client,settings,jobs=studio
    root,worker,body=prepared(studio,monkeypatch)
    original=hashlib.sha256(client.get(root+"/source").content).hexdigest()
    job=check(client,root+"/render",202,"POST",json=body).json()
    worker.process(jobs.claim())
    review=f"{root}/render/{job['id']}"
    check(client,review+"/review",200,"POST",json={"decision":"approved"})
    draft={"plan_id":body["plan_id"],"speed":1,"settings":{"enabled":True,"mode":"gradient","camera":"static"}}
    saved=check(client,root+"/studio",201,"POST",json=draft).json()
    assert client.get(review+"/final").status_code==409
    assert client.get(review+"/preview").status_code==200
    second=check(client,root+"/render",202,"POST",json=body).json()
    worker.process(jobs.claim())
    data=client.get(root+"/render").json()
    assert data["job"]["status"]=="completed",data["job"]
    newest=next(row for row in data["history"] if row["id"]==second["id"])
    historic=next(row for row in data["history"] if row["id"]==job["id"])
    assert newest["metadata"]["approval_fingerprint"]!=historic["metadata"]["approval_fingerprint"]
    assert newest["metadata"]["duration_seconds"]==historic["metadata"]["duration_seconds"]
    assert newest["metadata"]["color_transfer"]=="bt709"
    r=Renders(jobs)
    paths=[r.files(root.rsplit("/",1)[-1],entry["id"],"preview.mp4") for entry in (historic,newest)]
    audio=[subprocess.check_output([FFMPEG,"-v","error","-i",str(item),"-map","0:a:0","-c:a","copy","-f","hash","-"])
           for item in paths]
    assert audio[0]==audio[1]
    assert hashlib.sha256(client.get(root+"/source").content).hexdigest()==original
    with TestClient(create_app(settings),base_url="http://127.0.0.1") as restarted:
        assert restarted.get(root+"/studio").json()["current"]["id"]==saved["id"]
