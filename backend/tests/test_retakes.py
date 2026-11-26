import hashlib
import json
import subprocess
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.render_manifest import RenderSettings
from uuid import UUID
from app.render_store import Renders
from app.retakes import Candidate, Decision, Options, detect, normalize, safe_cut
from app.timeline import Span
from app.worker import Worker
from test_transcription import FFMPEG, FFPROBE, FakeEngine, project, studio


def span(a,b):
    return Span(start_ms=a,end_ms=b)


def words(text):
    return [(500+i*300,700+i*300,word,0.97) for i,word in enumerate(text.split())]


def candidates(text):
    return detect(words(text),"source","transcript","edit")


@pytest.mark.parametrize("text,reason", [
    ("Begin with this example. Begin with this example.", "exact repetition"),
    ("Begin with this example um Begin with this example.", "filler-separated restart"),
    ("The first reason is pressure. The first reason is fear.", "corrected restart"),
    ("I want to show— I want to show you now.", "incomplete attempt"),
    ("Start with one start with one start with one action.", "multiple takes"),
])
def test_detect_expected(text,reason):
    found=candidates(text)
    assert reason in found[0].reasons
    assert len({item.id for item in found})==len(found)
    assert found[0].earlier.first==0 and found[0].earlier.last<found[0].later.first
    assert [item.id for item in found]==[item.id for item in candidates(text)]
    if reason=="multiple takes":
        assert len(found)==3


def test_language_and_intent():
    assert normalize("क़दम।")==normalize("क़दम")
    assert normalize("तो")!="त"
    assert not candidates("very very very very good")
    assert not candidates("keep going keep going even when in doubt")
    [refrain]=candidates("Always keep moving forward. Always keep moving forward.")
    assert refrain.confidence=="ambiguous" and refrain.recommendation=="both"
    [changed]=candidates("The first reason is 5. The first reason is 6.")
    assert changed.confidence=="ambiguous" and "possible change of meaning" in changed.reasons
    with pytest.raises(ValidationError):
        Candidate.model_validate({**refrain.model_dump(),"later":refrain.earlier})


def test_frame_and_speech_protection():
    source=words("Begin with this example. Begin with this example.")
    [candidate]=detect(source,"source","transcript","edit")
    decision=Decision(selected="second")
    cut=safe_cut(candidate,decision,source,4100,[],[],Options())
    assert cut.end_ms < candidate.later.source.start_ms
    assert safe_cut(candidate,Decision(),source,4100,[],[],Options()) is None
    with pytest.raises(ValueError,match="neighbouring"):
        safe_cut(candidate,Decision(selected="second",adjustment=span(0,1750)),source,4100,[],[],Options())
    with pytest.raises(ValueError,match="overlaps"):
        safe_cut(candidate,decision,source,4100,[],[span(650,890)],Options())
    spaced=[(a+(200 if i>0 else 0),b+(200 if i>0 else 0),text,p) for i,(a,b,text,p) in enumerate(source)]
    [match]=detect(spaced,"source","transcript","edit")
    with pytest.raises(ValueError,match="without matching words"):
        safe_cut(match,decision,spaced,4300,[span(710,970)],[],Options())


class RepeatedFixtureEngine(FakeEngine):
    def transcribe(self,_audio):
        rows=[SimpleNamespace(word=w,start=a/1000,end=b/1000,probability=p)
              for a,b,w,p in words("Opening. Begin with this example. Begin with this example. Carry on now.")]
        return iter([SimpleNamespace(start=rows[0].start,end=rows[-1].end,text=" ".join(row.word for row in rows),words=rows)]), SimpleNamespace(language="en",language_probability=1)


def test_review_join_jobs_restore_and_render_gate(studio,tmp_path,monkeypatch):
    client,settings,jobs=studio
    monkeypatch.setenv("ELEVATE_DIRECTOR_MODE","mock")
    monkeypatch.setattr("app.analysis_worker.detect_speech",lambda *_:[])
    media=tmp_path/"fixture.mp4"
    subprocess.run([FFMPEG,"-v","error","-f","lavfi","-i","color=gray:s=120x200:r=30:d=6",
                    "-f","lavfi","-i","sine=frequency=620:sample_rate=48000:duration=6",
                    "-c:v","libx264","-c:a","aac","-shortest",str(media)],check=True)
    root=project(client,media)
    sha=hashlib.sha256(client.get(root+"/source").content).hexdigest()
    worker=Worker(settings,jobs,RepeatedFixtureEngine())
    def post(endpoint, body=None, expected=202):
        response=client.post(root+endpoint,json=body)
        assert response.status_code==expected,response.text
        return response.json()
    for endpoint,body in (("/transcription",None),("/analysis",None),("/director",{"playbook":"mystical-sumeet"})):
        post(endpoint,body)
        worker.process(jobs.claim())
    director=client.get(root+"/director").json()["selected"]
    director_path=f"/director/revisions/{director}"
    draft=client.get(root+director_path).json()
    assert draft["operations"]
    reviewed=post(director_path+"/operations/0",{"status":"rejected"},201)
    historical=post("/render",{"plan_id":reviewed["id"],"settings":{}})
    worker.process(jobs.claim())
    render=f"/render/{historical['id']}"
    post(render+"/review",{"decision":"approved"},200)
    assert client.get(root+render+"/final").status_code==200
    analysis=post("/retakes",{})
    post("/retakes",{},409)
    worker.process(jobs.claim())
    state=client.get(root+"/retakes").json()
    [candidate]=state["candidates"]
    assert candidate["earlier"]["first"]==1
    endpoint=f"/retakes/{analysis['id']}/{candidate['id']}"
    preview=post(endpoint+"/preview",{"selected":"second"},200)
    output=client.get(root+f"/retakes/previews/{preview['id']}")
    assert output.status_code==200
    join=tmp_path/"join.mp4"
    join.write_bytes(output.content)
    streams=json.loads(subprocess.check_output([FFPROBE,"-v","error","-show_streams","-of","json",str(join)]))["streams"]
    assert {x["codec_name"] for x in streams}=={"h264","aac"}
    assert abs(float(streams[0]["duration"])-float(streams[1]["duration"]))<.034
    assert client.get(root+render+"/final").status_code==200
    updated=post(endpoint+"/review",{"selected":"second"},200)
    assert updated["time_saved_ms"]>1000
    assert client.get(root+render+"/final").status_code==409
    assert client.get(root+render+"/preview").status_code==200
    assert client.get(root+"/retakes").json()["candidates"][0]["status"]=="accepted"
    post(endpoint+"/review",{"selected":"first"},409)
    post("/director",{"playbook":"mystical-sumeet"})
    worker.process(jobs.claim())
    chosen=client.get(root+"/director").json()["selected"]
    generated=client.get(root+f"/director/revisions/{chosen}").json()
    assert generated["base_approved_cuts"]==updated["accepted_cuts"]
    if generated["operations"]:
        choice=post(f"/director/revisions/{chosen}/operations/0",{"status":"rejected"},201)
        plan=Renders(jobs).make_manifest(UUID(root.rsplit("/",1)[1]),UUID(choice["id"]),RenderSettings())
        assert [item.model_dump() for item in plan.cuts]==updated["accepted_cuts"]
    cleared=post("/retakes/restore",{"expected":updated["id"]},200)
    assert not cleared["accepted_cuts"]
    restored=post("/retakes/restore",{"expected":cleared["id"],"revision":updated["id"]},200)
    assert restored["accepted_cuts"]==updated["accepted_cuts"]
    assert hashlib.sha256(client.get(root+"/source").content).hexdigest()==sha
    post("/analysis")
    worker.process(jobs.claim())
    post(endpoint+"/review",{"selected":"both","expected":restored["id"]},409)
    queued=post("/retakes",{})
    assert post(f"/retakes/jobs/{queued['id']}/cancel",{},200)["status"]=="cancelled"
    interrupted=post("/retakes",{})
    assert jobs.claim()["id"]==interrupted["id"]
    jobs.recover()
    assert client.get(root+"/retakes").json()["job"]["status"]=="interrupted"
    retry=post(f"/retakes/jobs/{interrupted['id']}/retry",{})
    worker.process(jobs.claim())
    completed=client.get(root+"/retakes").json()["job"]
    assert (completed["id"],completed["status"],completed["retry_count"])==(retry["id"],"completed",1)
