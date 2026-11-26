import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from uuid import UUID, uuid4, uuid5

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .sound_providers import Effect, LocalEffects
from .sound_mix import AudioAsset, BoundCue, Mix, MOODS, PRESETS, Rights, gain, program_audio, sound_meter, write_pcm

DEMUX = {".wav":"wav",".mp3":"mp3",".m4a":"mov",".aac":"aac",".flac":"flac",".ogg":"ogg"}
MIME = {".wav":"audio/wav",".mp3":"audio/mpeg",".m4a":"audio/mp4",".aac":"audio/aac",".flac":"audio/flac",".ogg":"audio/ogg"}
MIX_KEYS = {"mix","mix_revision","mix_fingerprint","sound_cues","notices"}
PLAYBOOK_SOUND = {
    "mystical-sumeet":"Prefer very quiet instrumental ambient pads, gentle fades and few effects. For guided healing, allow silence or a softer setting.",
    "educational":"Consider Professional Focus or no music. Favor clarity and avoid decorative effects.",
    "storytelling":"Consider Emotional Journey at low level. Preserve reflective pauses.",
    "authority":"Choose restrained instrumental music or silence. Avoid dramatic effects.",
}


def initialize_sound(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS sound_assets (id TEXT PRIMARY KEY, sha TEXT UNIQUE NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sound_revisions (id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                 plan_id TEXT NOT NULL REFERENCES director_plans(id), payload TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS sound_plans ON sound_revisions(project_id,plan_id);
    """)


def pinned_sound(db, project_id, plan_id):
    row=db.execute("SELECT payload FROM sound_revisions WHERE project_id=? AND plan_id=? ORDER BY rowid DESC LIMIT 1",
                   (str(project_id),str(plan_id))).fetchone()
    if row is None:
        return {}
    raw=row[0]
    data=json.loads(raw)
    return {"mix":data["mix"],"mix_revision":data["id"],"mix_fingerprint":hashlib.sha256(raw.encode()).hexdigest(),
            "sound_cues":data["cues"]}


def sound_root(projects):
    path=projects.root.resolve()/"sound"
    path.mkdir(exist_ok=True)
    if path.is_symlink():
        raise ValueError("Sound library directory cannot be a symlink.")
    return path


def sound_path(projects,asset):
    folder=sound_root(projects)/str(asset.id)
    file=folder/("original"+asset.extension)
    if folder.is_symlink() or file.is_symlink() or not file.is_file():
        raise HTTPException(409,"A pinned sound asset is unavailable.")
    return file


def import_sound(projects, settings, stream, filename, tags):
    asset_id=uuid4()
    root=sound_root(projects)
    ext=Path(filename).suffix.lower()
    if ext not in DEMUX:
        raise HTTPException(415,"Upload WAV, MP3, M4A, AAC, FLAC or OGG.")
    with TemporaryDirectory(prefix=".sound-",dir=root) as temp:
        file=Path(temp)/("original"+ext)
        sha=hashlib.sha256()
        size=0
        with file.open("wb") as writer:
            while chunk:=stream.read(1024*1024):
                size+=len(chunk)
                if size > min(settings.max_upload_bytes,100*1024*1024):
                    raise HTTPException(413,"Audio asset exceeds the upload limit.")
                sha.update(chunk)
                writer.write(chunk)
        fingerprint=sha.hexdigest()
        with projects.connect() as db:
            prior=db.execute("SELECT payload FROM sound_assets WHERE sha=?",(fingerprint,)).fetchone()
        if prior:
            return json.loads(prior[0])
        demux=["-protocol_whitelist","file","-f",DEMUX[ext],*(["-enable_drefs","0"] if ext==".m4a" else [])]
        try:
            started=perf_counter()
            info=json.loads(subprocess.check_output([settings.ffprobe,"-v","error",*demux,"-show_streams",
                        "-show_format","-of","json",str(file)],stderr=subprocess.PIPE,timeout=20))
            streamdata=next(s for s in info["streams"] if s["codec_type"]=="audio")
            duration=float(streamdata.get("duration") or info["format"]["duration"])
            if not 0 < duration <= 3600 or not 1<=streamdata["channels"]<=8:
                raise ValueError("Unsupported duration or channel count.")
            stats=sound_meter(settings.ffmpeg,file)
            if not stats:
                raise ValueError("No decodable audio.")
        except (OSError,ValueError,KeyError,StopIteration,subprocess.SubprocessError) as error:
            raise HTTPException(422,"Audio could not be validated and measured.") from error
        model=AudioAsset(id=asset_id,checksum=fingerprint,extension=ext,duration=duration,
                         kind=tags.kind,rights=tags.rights,true_peak=stats.get("input_tp"),lufs=stats.get("input_i"))
        row={**model.model_dump(mode="json"),"title":tags.title,"description":tags.description,
             "mood":tags.mood,"genre":tags.genre,"instrumentation":tags.instrumentation,
             "tempo":tags.tempo,"tempo_source":"user provided" if tags.tempo else None,"favourite":False,
             "sample_rate":int(streamdata["sample_rate"]),"channels":streamdata["channels"],
             "channel_layout":streamdata.get("channel_layout"),"lufs":stats.get("input_i"),
             "analysis_seconds":round(perf_counter()-started,3),"imported_at":now()}
        with projects.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior=db.execute("SELECT payload FROM sound_assets WHERE sha=?",(fingerprint,)).fetchone()
            if prior:
                return json.loads(prior[0])
            folder=root/str(asset_id)
            folder.mkdir()
            try:
                file.chmod(0o444)
                file.rename(folder/file.name)
                db.execute("INSERT INTO sound_assets VALUES (?,?,?)",(str(asset_id),fingerprint,json.dumps(row)))
            except BaseException:
                shutil.rmtree(folder)
                raise
        return row


def now():
    return datetime.now(timezone.utc).isoformat()


class Tags(BaseModel):
    model_config=ConfigDict(extra="forbid")
    title: str = Field(min_length=1,max_length=150)
    kind: str = Field(pattern="^(music|effect)$")
    rights: Rights
    description: str = Field(default="",max_length=400)
    mood: str = Field(default="ambient",max_length=40)
    genre: str = Field(default="",max_length=80)
    instrumentation: str = Field(default="",max_length=160)
    tempo: float | None = Field(default=None,ge=25,le=300)


class MixDraft(BaseModel):
    model_config=ConfigDict(extra="forbid")
    plan_id: UUID
    speed: float = Field(default=1.1)
    mix: Mix


def asset_row(db,uid):
    record=db.execute("SELECT payload FROM sound_assets WHERE id=?",(str(uid),)).fetchone()
    if record is None:
        raise HTTPException(404,"Sound asset not found.")
    return json.loads(record[0])


def bind(db,projects,mix,duration):
    from .render_store import digest
    bound=[]
    for cue in mix.cues():
        row=asset_row(db,cue.asset_id)
        asset=AudioAsset.model_validate({key:row[key] for key in AudioAsset.model_fields})
        expected="music" if mix.music and cue.id==mix.music.id else "effect"
        if expected!=asset.kind or cue.end_ms>duration:
            raise HTTPException(422,"Check the audio track type and edited cue boundaries.")
        free=asset.duration*1000-cue.offset_ms
        if free<40 or (not cue.loop and free+1<cue.end_ms-cue.start_ms) or (cue.loop and free<=2*cue.crossfade_ms):
            raise HTTPException(422,"The selected sound cannot cover the cue with these trim or loop settings.")
        path=sound_path(projects,asset)
        if digest(path)!=asset.checksum:
            raise HTTPException(409,"Audio source checksum changed.")
        bound.append(BoundCue(cue=cue,asset=asset))
    if len({cue.cue.id for cue in bound}) != len(bound):
        raise HTTPException(422,"Duplicate sound cue IDs.")
    return bound


def carrier_for(store,manifest,*,caption_asset=None):
    """Reuse only a verified voice-only render of the same full visual and voice manifest."""
    from .render_store import digest
    target=manifest.model_dump(exclude=MIX_KEYS)
    with store.projects.connect() as db:
        rows=db.execute("SELECT id,metadata FROM renders WHERE project_id=? ORDER BY rowid DESC",
                        (str(manifest.project_id),)).fetchall()
    for row in rows:
        candidate=store.manifest(row["id"])
        if candidate.mix and candidate.mix.active:
            continue
        if candidate.model_dump(exclude=MIX_KEYS)!=target:
            continue
        try:
            path=store.files(manifest.project_id,row["id"],"preview.mp4")
            if caption_asset is not None:
                captions=store.files(manifest.project_id,row["id"],"captions.ass")
                if captions.read_text(encoding="utf-8") != caption_asset:
                    continue
        except (HTTPException,OSError):
            continue
        metadata=json.loads(row["metadata"])
        if metadata.get("color_transfer")=="bt709" and digest(path)==metadata["sha256"]:
            return path
    return None


def master_blob(executable,carrier,assets,mix,duration,folder,stage):
    started=perf_counter()
    details={}
    audio=program_audio(executable,carrier,assets,mix,duration,stage,analysis=details)
    write_pcm(folder/"mix.wav",audio)
    details["mix_seconds"]=round(perf_counter()-started,3)
    start_master=perf_counter()
    stats=sound_meter(executable,folder/"mix.wav",stage)
    level,peak=stats.get("input_i"),stats.get("input_tp")
    adjustment=min(2,max(-18,-14-level),-2.15-peak) if level is not None and peak is not None else 0
    details["master_seconds"]=round(perf_counter()-start_master,3)
    details["master_gain_db"]=round(adjustment,3)
    voice,music,effects=(details["rms_dbfs"][key] for key in ("voice","music","effects"))
    warnings = ["Music or effects may compete with speech. Listen to the mix."] if max(music,effects)>voice-13 else []
    if any(item.asset.lufs is None and item.asset.true_peak is None for item,_ in assets):
        warnings.append("An active sound asset measured silent or below the meter threshold.")
    if any(item.asset.true_peak is not None and item.asset.true_peak >= 0 for item,_ in assets):
        warnings.append("An imported track may already contain clipping.")
    details["warnings"] = warnings
    return adjustment,details


def sound_routes(renders,settings):
    from .render_manifest import RenderSettings
    router=APIRouter()
    projects=renders.projects

    @router.get("/sound-assets")
    def assets(q:str="",mood:str="",minimum:float=0,maximum:float=3600,favourite:bool=False):
        with projects.connect() as db:
            entries=[json.loads(row[0]) for row in db.execute("SELECT payload FROM sound_assets ORDER BY rowid DESC")]
            usage={}
            for row in db.execute("SELECT payload FROM sound_revisions"):
                revision=json.loads(row[0])
                for item in revision["cues"]:
                    uid=item["asset"]["id"]
                    usage.setdefault(uid,[]).append(revision["id"])
        return {"moods":MOODS,"items":[{**item,"usage":usage.get(item["id"],[])} for item in entries
                 if minimum<=item["duration"]<=maximum and (not mood or item["mood"]==mood)
                 and (not favourite or item["favourite"]) and q.casefold() in
                 " ".join((item["title"],item["description"],item["genre"],item["instrumentation"],item["rights"]["creator"])).casefold()]}

    @router.post("/sound-assets",status_code=201,max_body_size=settings.max_upload_bytes+65536)
    def upload(file:UploadFile=File(...),metadata:str=Form(...)):
        try:
            tags=Tags.model_validate_json(metadata)
        except ValidationError as error:
            raise HTTPException(422,"Check the name, track type and usage-rights metadata.") from error
        return import_sound(projects,settings,file.file,file.filename or "",tags)

    @router.post("/sound-assets/local-effect/{effect}",status_code=201)
    def local_effect(effect:Effect):
        provider=LocalEffects()
        with TemporaryDirectory(dir=sound_root(projects)) as folder:
            output=Path(folder)/f"{effect}.wav"
            provider.prepare(effect,output)
            tags=Tags(title=effect.replace("-"," ").title(),kind="effect",mood="reflective",
                      description=f"Original local synthesis · {provider.name}",
                      rights=Rights(origin="owned",license="Original locally synthesized sound",creator="Elevate AI Studio",
                                    confirmed=True,commercial=True,platforms=["instagram","youtube","tiktok","other"]))
            with output.open("rb") as stream:
                return import_sound(projects,settings,stream,output.name,tags)

    @router.post("/sound-assets/{asset_id}/rights",max_body_size=8192)
    def update_rights(asset_id:UUID, rights:Rights):
        with projects.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row=asset_row(db,asset_id)
            if row["rights"]["origin"]=="fixture" and rights.origin!="fixture":
                raise HTTPException(422,"Studio preview fixtures cannot be relicensed.")
            row["rights"]=rights.model_dump(mode="json")
            db.execute("UPDATE sound_assets SET payload=? WHERE id=?",(json.dumps(row),str(asset_id)))
        return row

    @router.get("/sound-assets/{asset_id}/preview")
    def asset_preview(asset_id:UUID):
        with projects.connect() as db: row=asset_row(db,asset_id)
        asset=AudioAsset.model_validate({key:row[key] for key in AudioAsset.model_fields})
        return FileResponse(sound_path(projects,asset),media_type=MIME[asset.extension],
                            headers={"X-Content-Type-Options":"nosniff"})

    @router.post("/sound-assets/{asset_id}/tags")
    def edit(asset_id:UUID, data:dict):
        with projects.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old=asset_row(db,asset_id)
            clean={k:data[k] for k in ("title","mood","description","genre","instrumentation") if k in data}
            if any(not isinstance(v,str) or len(v)>150 for v in clean.values()):
                raise HTTPException(422,"Check tag lengths.")
            old.update(clean)
            if isinstance(data.get("favourite"),bool):
                old["favourite"]=data["favourite"]
            db.execute("UPDATE sound_assets SET payload=? WHERE id=?",(json.dumps(old),str(asset_id)))
        return old

    @router.get("/projects/{project_id}/sound-mix")
    def state(project_id:UUID):
        projects.get(project_id)
        with projects.connect() as db:
            row=renders.selected(db,project_id)
            history=[json.loads(x[0]) for x in db.execute("SELECT payload FROM sound_revisions WHERE project_id=? ORDER BY rowid DESC",(str(project_id),))]
            plan=row[1] if row else None
            last=db.execute("SELECT id FROM renders WHERE project_id=? ORDER BY rowid DESC LIMIT 1",(str(project_id),)).fetchone()
        previous=renders.manifest(last[0]) if last else None
        plan_id=plan["id"] if plan else None
        return {"plan_id":plan_id,"history":history,"current":next((r for r in history if r["plan_id"]==plan_id),None),
                "presets":PRESETS,"speed":previous.settings.playback_speed if previous else 1.1,
                "duration_ms":previous.encoded_ms if previous else None,
                "suggestions":[{**item,"id":str(uuid5(UUID(plan_id),f"sound-{index}")),"status":"pending"}
                               for index,item in enumerate(plan.get("sound_ideas") or [])] if plan else []}

    def snapshot(project_id,body):
        try:
            render_settings=RenderSettings(playback_speed=body.speed)
            with projects.connect() as db:
                prior=db.execute("""SELECT i.manifest FROM render_inputs i JOIN renders r ON r.id=i.job_id
                                    WHERE r.project_id=? ORDER BY r.rowid DESC LIMIT 20""",(str(project_id),)).fetchall()
            for row in prior:
                manifest=json.loads(row[0])
                if manifest["plan_id"]==str(body.plan_id) and manifest["settings"]["playback_speed"]==body.speed:
                    render_settings=RenderSettings.model_validate(manifest["settings"])
                    break
        except ValidationError as error:
            raise HTTPException(422,"Choose a supported playback speed.") from error
        manifest=renders.make_manifest(project_id,body.plan_id,render_settings,include_sound=False)
        if manifest.encoded_ms>1200000:
            raise HTTPException(422,"Mix previews are limited to twenty minutes.")
        return manifest

    @router.post("/projects/{project_id}/sound-mix",status_code=201,max_body_size=65536)
    def save(project_id:UUID,body:MixDraft):
        manifest=snapshot(project_id,body)
        with projects.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if issue:=renders.stale(db,manifest,check_sound=False):
                raise HTTPException(409,issue)
            cues=bind(db,projects,body.mix,manifest.encoded_ms)
            row={"id":str(uuid4()),"plan_id":str(body.plan_id),"source_id":manifest.source_media_id,
                 "timeline":manifest.plan_fingerprint,"transcript_revision":str(manifest.transcript_revision),
                 "voice_revision":str(manifest.voice_revision) if manifest.voice_revision else None,
                 "brand_revision":manifest.brand_revision,"playbook_revision":manifest.playbook_revision,
                 "speed":body.speed,"created_at":now(),"mix":body.mix.model_dump(mode="json"),
                 "cues":[cue.model_dump(mode="json") for cue in cues]}
            db.execute("INSERT INTO sound_revisions VALUES (?,?,?,?)",(row["id"],str(project_id),str(body.plan_id),json.dumps(row)))
        return row

    @router.post("/projects/{project_id}/sound-mix/sample",max_body_size=65536)
    def sample(project_id:UUID,body:MixDraft,start_ms:int=0):
        manifest=snapshot(project_id,body)
        carrier=carrier_for(renders,manifest)
        if carrier is None:
            raise HTTPException(409,"Render a compatible voice-only preview once before generating sound samples.")
        start_ms=max(0,min(start_ms,manifest.encoded_ms-1))
        finish=min(start_ms+10000,manifest.encoded_ms)
        with projects.connect() as db:
            bound=bind(db,projects,body.mix,manifest.encoded_ms)
        cache_input=body.model_dump(mode="json")
        for cue in [cache_input["mix"]["music"],*cache_input["mix"]["effects"]]:
            if cue:
                cue.pop("id")
        key=hashlib.sha256(json.dumps({"version":3,"mix":cache_input,"start":start_ms,
             "carrier":carrier.parent.name,"assets":[item.asset.checksum for item in bound]},sort_keys=True).encode()).hexdigest()
        cache=projects.media_dir(project_id)/"sound-samples"
        cache.mkdir(exist_ok=True)
        folder=cache/key
        if (folder/"report.json").is_file():
            return {**json.loads((folder/"report.json").read_text()),"cached":True}
        began=perf_counter()
        details={}
        assets=[(item,sound_path(projects,item.asset)) for item in bound]
        clips=program_audio(settings.ffmpeg,carrier,assets,body.mix,manifest.encoded_ms,lambda:None,
                           window=(start_ms,finish),analysis=details)
        with TemporaryDirectory(dir=cache,prefix=".sample-") as scratch:
            work=Path(scratch)
            labels=("voice","music","full","bed","effects")
            for label,clip in zip(labels,clips,strict=True):
                write_pcm(work/f"{label}.wav",clip)
            metrics={label:sound_meter(settings.ffmpeg,work/f"{label}.wav") for label in labels}
            measured=[metrics[key]["input_i"] for key in ("voice","music","full") if metrics[key].get("input_i") is not None]
            target=min(measured) if measured else -14
            for label,clip in zip(labels,clips,strict=True):
                level=metrics[label].get("input_i")
                write_pcm(work/f"{label}-matched.wav",clip*gain(min(0,target-level)) if level is not None else clip)
            details.update(id=key,seconds=round(perf_counter()-began,3),metrics=metrics,start_ms=start_ms,end_ms=finish)
            (work/"report.json").write_text(json.dumps(details))
            try:
                work.rename(folder)
            except OSError:
                if not (folder/"report.json").is_file():
                    raise
        return {**details,"cached":False}

    @router.get("/projects/{project_id}/sound-mix/sample/{key}/{name}")
    def sample_download(project_id:UUID,key:str,name:str):
        import re
        if not re.fullmatch(r"[a-f0-9]{64}",key) or not re.fullmatch(r"(voice|music|full|bed|effects)(-matched)?\.wav",name):
            raise HTTPException(404,"Sample not found.")
        root=(projects.media_dir(project_id)/"sound-samples").resolve()
        file=(root/key/name).resolve()
        if file.parent.parent!=root or not file.is_file():
            raise HTTPException(404,"Sample not found.")
        return FileResponse(file,media_type="audio/wav")

    return router
