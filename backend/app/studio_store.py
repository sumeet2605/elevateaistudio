import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from .asset_library import asset_model, asset_path, asset_row
from .studio_key import PRESETS, PRESET_ADJUSTMENTS, StudioSettings, sample_key, studio_graph

KINDS={"original","matte","foreground","black","white","composite"}
MP4_INPUT=["-protocol_whitelist","file","-f","mov","-enable_drefs","0"]


def init_studio(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS studio_revisions (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
            plan_id TEXT NOT NULL REFERENCES director_plans(id), payload TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS studio_history ON studio_revisions(project_id,plan_id);
    """)


def pinned_studio(db, project, plan):
    row=db.execute("SELECT payload FROM studio_revisions WHERE project_id=? AND plan_id=? ORDER BY rowid DESC LIMIT 1",
                   (str(project),str(plan))).fetchone()
    if not row:
        return {}
    raw=row[0]
    data=json.loads(raw)
    return {"studio":data["settings"],"studio_revision":data["id"],
            "studio_fingerprint":hashlib.sha256(raw.encode()).hexdigest(),"studio_asset":data["asset"]}


class StudioDraft(BaseModel):
    model_config=ConfigDict(extra="forbid")
    plan_id: UUID
    speed: Literal[1,1.1] = 1
    settings: StudioSettings


class Preview(BaseModel):
    model_config=ConfigDict(extra="forbid")
    settings: StudioSettings
    mode: Literal["original","matte","foreground","black","white","composite"] = "composite"
    start_ms: int = Field(default=0,ge=0,le=3600000)
    duration: int = Field(default=10,ge=2,le=12)


def background(renders,project,options,seconds,db):
    from .render_store import digest
    asset=None
    path=None
    if options.enabled and options.mode=="asset":
        row=asset_row(db,options.background_id)
        asset=asset_model(row)
        path=asset_path(renders.projects,asset)
        if digest(path)!=asset.checksum:
            raise HTTPException(409,"The background file checksum changed.")
        if asset.kind=="video":
            offset=options.offset_ms/1000
            if offset >= asset.duration or (not options.loop and asset.duration-offset+.02<seconds):
                raise HTTPException(422,"The video background is too short. Enable looping, change the offset, or choose a longer clip.")
    return asset,path


def background_input(settings,asset,path):
    if not asset:
        return []
    if asset.kind=="video":
        return [*(["-stream_loop","-1"] if settings.loop else []),*MP4_INPUT,"-i",str(path)]
    return ["-protocol_whitelist","file","-i",str(path)]


def clip_source(renders,project):
    from .render_store import digest
    media=renders.projects.get(project)["media"]
    if not media:
        raise HTTPException(409,"Upload your original green-screen recording first.")
    source=renders.projects.source_path(project,media)
    if digest(source)!=media["sha256"]:
        raise HTTPException(409,"The original source checksum changed.")
    return media,source


def studio_routes(renders,settings):
    from .ffmpeg_render import HDR_TO_SDR, HDR_TRANSFERS, streams
    from .render_manifest import RenderSettings
    router=APIRouter(prefix="/projects/{project_id}/studio")
    projects=renders.projects

    @router.get("")
    def state(project_id:UUID):
        projects.get(project_id)
        with projects.connect() as db:
            row=renders.selected(db,project_id)
            plan=row[1] if row else None
            revisions=[json.loads(row[0]) for row in db.execute(
                "SELECT payload FROM studio_revisions WHERE project_id=? ORDER BY rowid DESC",(str(project_id),))]
            current=next((item for item in revisions if plan and item["plan_id"]==plan["id"]),None)
        return {"plan_id":plan["id"] if plan else None,"history":revisions,"current":current,"presets":PRESETS,
                "preset_adjustments":PRESET_ADJUSTMENTS,
                "suggestion":plan.get("studio_idea") if plan else None}

    @router.post("",status_code=201,max_body_size=8192)
    def save(project_id:UUID,body:StudioDraft):
        manifest=renders.make_manifest(project_id,body.plan_id,RenderSettings(playback_speed=body.speed),include_studio=False)
        with projects.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if issue:=renders.stale(db,manifest,check_studio=False):
                raise HTTPException(409,issue)
            asset,_=background(renders,project_id,body.settings,manifest.total_frames/30,db)
            revised={"id":str(uuid4()),"plan_id":str(body.plan_id),"created_at":datetime.now(timezone.utc).isoformat(),
                     "source_id":manifest.source_media_id,"edit_fingerprint":manifest.plan_fingerprint,
                     "brand_revision":manifest.brand_revision,"speed":body.speed,"decision":"saved-for-preview",
                     "settings":body.settings.model_dump(mode="json"),"asset":asset.model_dump(mode="json") if asset else None}
            db.execute("INSERT INTO studio_revisions VALUES (?,?,?,?)",
                       (revised["id"],str(project_id),str(body.plan_id),json.dumps(revised)))
        return revised

    @router.post("/analyse")
    def analyse(project_id:UUID):
        import numpy as np
        media,source=clip_source(renders,project_id)
        key=hashlib.sha256(f"studio-analysis-v1:{media['sha256']}".encode()).hexdigest()
        folder=projects.media_dir(project_id)/"studio";folder.mkdir(exist_ok=True)
        report=folder/f"{key}.json"
        if report.is_file():
            return {**json.loads(report.read_text()),"cached":True}
        start=perf_counter()
        duration=media["duration_seconds"]
        chunks=[]
        for ratio in (.08,.22,.43,.66,.88):
            args=[settings.ffmpeg,"-nostdin","-v","error",*MP4_INPUT,"-ss",f"{duration*ratio:.4f}","-i",str(source),
                  "-map","0:v:0","-an","-vf","scale=160:284,format=rgb24","-frames:v","1","-f","rawvideo","-"]
            data=subprocess.check_output(args,stderr=subprocess.PIPE,timeout=25)
            if len(data)==160*284*3:
                chunks.append(np.frombuffer(data,dtype=np.uint8).reshape(284,160,3))
        if not chunks:
            raise HTTPException(422,"No sampled picture could be decoded.")
        result=sample_key(np.stack(chunks))
        result.update(id=key,sample_frames=len(chunks),analysis_seconds=round(perf_counter()-start,3),
                      source_id=media["sha256"])
        with TemporaryDirectory(dir=folder) as temp:
            scratch=Path(temp)/report.name
            scratch.write_text(json.dumps(result))
            scratch.replace(report)
        return {**result,"cached":False}

    @router.post("/preview",max_body_size=8192)
    def preview(project_id:UUID,body:Preview):
        media,source=clip_source(renders,project_id)
        mode=body.mode
        opts=body.settings
        if mode!="original" and not opts.enabled:
            raise HTTPException(422,"Enable keying before generating a keyed sample.")
        start_ms=min(body.start_ms,max(0,int(media["duration_seconds"]*1000)-100))
        duration=min(body.duration,media["duration_seconds"]-start_ms/1000)
        with projects.connect() as db:
            asset,path=background(renders,project_id,opts,duration,db) if mode=="composite" else (None,None)
        key=hashlib.sha256(json.dumps({"engine":2,"source":media["sha256"],"draft":body.model_dump(mode="json"),
                                       "asset":asset.checksum if asset else None},sort_keys=True).encode()).hexdigest()
        folder=projects.media_dir(project_id)/"studio";folder.mkdir(exist_ok=True)
        extension="webm" if mode=="foreground" else "mp4"
        target=folder/f"{key}.{extension}"
        if target.is_file():
            return {"id":key,"extension":extension,"cached":True,"duration":duration,"seconds":0}
        began=perf_counter()
        # The diagnostic proxy deliberately uses a small picture for fast iterative checks.
        fps=24
        frames=max(1,int(duration*fps))
        video,_,_=streams(settings.ffprobe,source,allow_missing_audio=True)
        tone=","+HDR_TO_SDR if video.get("color_transfer") in HDR_TRANSFERS else ""
        args=[settings.ffmpeg,"-nostdin","-v","error",*MP4_INPUT,"-ss",f"{start_ms/1000:.4f}","-i",str(source),
              *background_input(opts,asset,path),"-filter_complex_threads","2"]
        if mode=="original":
            graph=f"[0:v:0]fps={fps},scale=360:640:force_original_aspect_ratio=decrease:force_divisible_by=2{tone},setsar=1[v]"
        else:
            nodes=[f"[0:v:0]fps={fps},scale=360:640:force_original_aspect_ratio=decrease:force_divisible_by=2{tone},"
                   f"setsar=1,trim=end_frame={frames},setpts=PTS-STARTPTS[raw]"]
            label=studio_graph(nodes,"[raw]",opts,asset,1,frames,width=360,height=640,fps=fps,
                               tone=HDR_TO_SDR,diagnostic=mode)
            graph=";".join(nodes)+f";{label}format="+("yuva420p" if mode=="foreground" else "yuv420p")+"[v]"
        encoder=(["-c:v","libvpx-vp9","-deadline","realtime","-cpu-used","6","-auto-alt-ref","0","-pix_fmt","yuva420p"]
                 if mode=="foreground" else ["-c:v","libx264","-preset","veryfast","-pix_fmt","yuv420p","-movflags","+faststart"])
        with TemporaryDirectory(dir=folder,prefix=".preview-") as temp:
            out=Path(temp)/target.name
            result=subprocess.run(args+["-filter_complex",graph,"-map","[v]","-frames:v",str(frames),"-an",
                                        "-threads","2",*encoder,str(out)],capture_output=True,timeout=120)
            if result.returncode:
                raise HTTPException(422,"The studio preview could not render: "+result.stderr.decode(errors="replace")[-280:])
            out.replace(target)
        return {"id":key,"extension":extension,"cached":False,"duration":frames/fps,"seconds":round(perf_counter()-began,3)}

    @router.get("/preview/{name}")
    def preview_file(project_id:UUID,name:str):
        import re
        if not re.fullmatch(r"[a-f0-9]{64}\.(mp4|webm)",name):
            raise HTTPException(404,"Studio sample not found.")
        root=(projects.media_dir(project_id)/"studio").resolve()
        path=(root/name).resolve()
        if path.parent!=root or not path.is_file():
            raise HTTPException(404,"Studio sample not found.")
        return FileResponse(path,media_type="video/webm" if path.suffix==".webm" else "video/mp4",
                            headers={"X-Content-Type-Options":"nosniff"})
    return router
