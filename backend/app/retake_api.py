import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .render_manifest import frame_spans
from .render_store import digest
from .retake_store import RETAKE, fingerprint, current_review
from .retakes import Candidate, Decision, Options
from .timeline import Span, subtract_spans, merge_spans


class ReviewRequest(Decision):
    expected: UUID | None = None


class RestoreRequest(BaseModel):
    expected: UUID | None = None
    revision: UUID | None = None


def retake_routes(store, settings):
    router=APIRouter(prefix="/projects/{project_id}/retakes")

    @router.get("")
    def state(project_id: UUID):
        return store.state(project_id)

    @router.post("",status_code=202)
    def enqueue(project_id: UUID, options: Options):
        return store.enqueue(project_id,options)

    @router.post("/jobs/{job_id}/cancel")
    def cancel(project_id: UUID, job_id: UUID):
        return store.jobs.cancel(project_id,job_id,RETAKE)

    @router.post("/jobs/{job_id}/retry",status_code=202)
    def retry(project_id: UUID, job_id: UUID):
        store.jobs.get(project_id,job_id,RETAKE)
        return store.enqueue(project_id,Options(),retry=job_id)

    @router.post("/restore")
    def restore(project_id: UUID, body: RestoreRequest):
        return store.restore(project_id,str(body.revision) if body.revision else None,
                             str(body.expected) if body.expected else None)

    @router.post("/{analysis_id}/{candidate_id}/review")
    def review(project_id: UUID, analysis_id: UUID, candidate_id: str, body: ReviewRequest):
        return store.review(project_id,str(analysis_id),candidate_id,body,str(body.expected) if body.expected else None)

    @router.post("/{analysis_id}/{candidate_id}/preview")
    def preview(project_id: UUID, analysis_id: UUID, candidate_id: str, decision: Decision):
        media=store.projects.get(project_id)["media"]
        with store.projects.connect() as db:
            snapshot,candidate=store.evidence(db,project_id,str(analysis_id),candidate_id,media)
            prior=current_review(db,project_id)
            if prior and prior["transcript_revision"]!=snapshot["transcript_revision"]:
                raise HTTPException(409,"Clear the review for the previous transcript before previewing a new join.")
            others=[Candidate.model_validate(item) for item in prior["reviewed_candidates"] if item["id"]!=candidate_id] if prior else []
            cut=store.cut_for(project_id,snapshot,candidate,decision,others)
        if not cut:
            raise HTTPException(422,"Choose a take before previewing the join.")
        source=store.projects.source_path(project_id,media)
        if digest(source)!=snapshot["source_id"]:
            raise HTTPException(409,"Original source checksum changed.")
        context=Span(start_ms=max(0,cut.start_ms-2400),end_ms=min(snapshot["duration"],cut.end_ms+2400))
        excluded=merge_spans([cut,*[Span.model_validate(s) for s in snapshot["existing"]],
                              *[item.cut for item in others if item.cut]],snapshot["duration"])
        ranges=frame_spans(subtract_spans(context,excluded))
        if not ranges:
            raise HTTPException(422,"No retained context is available for this preview.")
        key=fingerprint(["retake-join-v1",snapshot["source_id"],[span.model_dump() for span in ranges]])
        folder=store.projects.media_dir(project_id)/"retake-joins"
        folder.mkdir(exist_ok=True)
        target=folder/f"{key}.mp4"
        if target.is_file():
            return {"id":key,"cached":True,"seconds":0,"cut":cut}
        start=monotonic()
        # Seek once, then split a short source window. Each branch gets the same A/V clock.
        offset=ranges[0].start_frame
        end=ranges[-1].end_frame
        count=len(ranges)
        nodes=[]
        for media_type,program in (("v","fps=30"),("a","aresample=48000:async=1:first_pts=0,apad")):
            split=f",{'a' if media_type=='a' else ''}split={count}" if count>1 else ""
            nodes.append(f"[0:{media_type}:0]{program}{split}"+"".join(f"[{media_type}{i}]" for i in range(count)))
        for i,span in enumerate(ranges):
            begin,finish=span.start_frame-offset,span.end_frame-offset
            nodes.append(f"[v{i}]trim=start_frame={begin}:end_frame={finish},setpts=PTS-STARTPTS,"
                         f"scale=360:640:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                         f"pad=360:640:(ow-iw)/2:(oh-ih)/2,setsar=1[vout{i}]")
            tail=max(0,(finish-begin)*1600-384)
            fades=(",afade=t=in:ss=0:ns=384" if i else "")+(f",afade=t=out:ss={tail}:ns=384" if i<count-1 else "")
            nodes.append(f"[a{i}]atrim=start_sample={begin*1600}:end_sample={finish*1600},asetpts=N/SR/TB{fades}[aout{i}]")
        if count>1:
            labels="".join(f"[vout{i}][aout{i}]" for i in range(count))
            nodes.append(f"{labels}concat=n={count}:v=1:a=1[v][a]")
        maps=["-map","[v]","-map","[a]"] if count>1 else ["-map","[vout0]","-map","[aout0]"]
        with TemporaryDirectory(dir=folder) as tmp:
            output=Path(tmp)/"join.mp4"
            command=[settings.ffmpeg,"-nostdin","-v","error","-protocol_whitelist","file","-f","mov","-enable_drefs","0",
                     "-ss",f"{offset/30:.9f}","-t",f"{(end-offset)/30+.2:.9f}","-i",str(source),
                     "-filter_complex_threads","2","-filter_complex",";".join(nodes),*maps,"-threads:v","2",
                     "-c:v","libx264","-preset","veryfast","-pix_fmt","yuv420p","-c:a","aac","-movflags","+faststart",str(output)]
            try:
                result=subprocess.run(command,capture_output=True,timeout=45)
                if result.returncode or not output.is_file():
                    raise HTTPException(422,"FFmpeg could not render this join. Try another safe boundary.")
                output.replace(target)
            except subprocess.TimeoutExpired as exc:
                raise HTTPException(504,"Join preview timed out.") from exc
        return {"id":key,"cached":False,"seconds":round(monotonic()-start,3),"cut":cut}

    @router.get("/previews/{preview_id}")
    def preview_file(project_id: UUID, preview_id: str):
        store.projects.get(project_id)
        if len(preview_id)!=64 or any(c not in "0123456789abcdef" for c in preview_id):
            raise HTTPException(404,"Preview not found.")
        folder=(store.projects.media_dir(project_id)/"retake-joins").resolve()
        path=(folder/f"{preview_id}.mp4").resolve()
        if path.parent!=folder or not path.is_file():
            raise HTTPException(404,"Preview not found.")
        return FileResponse(path,media_type="video/mp4")

    return router
