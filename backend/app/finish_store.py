import hashlib
import json
import math
import re
import shutil
import subprocess
from datetime import datetime, timezone
from time import perf_counter
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from .finishing import GRADES, VOICES, Grade, Voice, grade_filter, loudnorm, measure_audio, speech_filter

FINISH = "finish_analysis"


class FinishInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: UUID
    preset: str
    grade: Grade | None = None
    voice: Voice | None = None
    analysis_id: UUID | None = None


class AnalysisInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: float = Field(default=0, ge=0, le=1800)
    grade: Grade = Field(default_factory=lambda: GRADES["mystical-warmth@1"][1])
    voice: Voice = Field(default_factory=Voice)


def revisions(db, project, plan, kind):
    return db.execute("""SELECT payload FROM finish_revisions WHERE project_id=? AND plan_id=? AND kind=?
                     ORDER BY rowid DESC""", (str(project), str(plan), kind))


def pinned(db, project, plan):
    result = {}
    for kind in ("grade", "voice"):
        row = revisions(db, project, plan, kind).fetchone()
        if row:
            raw = row[0]
            item = json.loads(raw)
            result.update({kind: item["settings"], f"{kind}_revision": item["id"],
                           f"{kind}_fingerprint": hashlib.sha256(raw.encode()).hexdigest()})
    return result


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS finish_revisions (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
            plan_id TEXT NOT NULL REFERENCES director_plans(id), kind TEXT NOT NULL, payload TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS finish_plan ON finish_revisions(project_id,plan_id,kind);
        CREATE TABLE IF NOT EXISTS finish_inputs (
            id TEXT PRIMARY KEY REFERENCES jobs(id), source_id TEXT NOT NULL, cache_key TEXT NOT NULL, payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS finish_analyses (
            id TEXT PRIMARY KEY REFERENCES jobs(id), project_id TEXT NOT NULL REFERENCES projects(id),
            cache_key TEXT NOT NULL, payload TEXT NOT NULL
        );
    """)


def cache_key(source, options):
    return hashlib.sha256(f"finish-v2:{source}:{options.model_dump_json()}".encode()).hexdigest()


def analyse(settings, jobs, job, stage):
    import numpy as np
    from .ffmpeg_render import HDR_TO_SDR, HDR_TRANSFERS, run_ffmpeg, streams
    from .render_store import digest
    project = UUID(job["project_id"])
    job_id = job["id"]
    with jobs.projects.connect() as db:
        entry = db.execute("SELECT * FROM finish_inputs WHERE id=?", (job_id,)).fetchone()
    options = AnalysisInput.model_validate_json(entry["payload"])
    media = jobs.projects.get(project)["media"]
    source = jobs.projects.source_path(project, media)
    if digest(source) != entry["source_id"]:
        raise ValueError("The original no longer matches this analysis.")
    video, audio, _ = streams(settings.ffprobe, source, allow_missing_audio=True)
    duration = min(10, media["duration_seconds"])
    start = min(options.start, max(0, media["duration_seconds"] - duration))
    folder = jobs.projects.media_dir(project) / f"finish-{job_id}"
    folder.mkdir()
    published = False
    began = perf_counter()
    base = [settings.ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-protocol_whitelist", "file",
            "-f", "mov", "-enable_drefs", "0", "-ss", str(start), "-i", str(source), "-t", str(duration)]
    metadata = {field: video.get(field) for field in
                ("color_space", "color_range", "color_transfer", "color_primaries", "pix_fmt", "bits_per_raw_sample")}
    depth = video.get("bits_per_raw_sample")
    pixel_format = video.get("pix_fmt") or ""
    suffix = re.search(r"(?:p|gray)(9|10|12|14|16)(?:le|be)$", pixel_format)
    metadata["bit_depth"] = (int(depth) if str(depth).isdigit() and int(depth) else
                             int(suffix[1]) if suffix else 8 if pixel_format in
                             {"yuv420p", "yuvj420p", "yuv422p", "yuv444p", "nv12", "rgb24"} else None)
    metadata["hdr_metadata"] = video.get("side_data_list", [])
    metadata["interpretation"] = ("HDR tone map" if video.get("color_transfer") in HDR_TRANSFERS else
                                  "SDR transfer" if video.get("color_transfer") not in (None, "unknown", "unspecified")
                                  else "Transfer unknown. No HDR conversion without an HDR transfer tag.")
    try:
        stage("sampling_picture", .05)
        hdr = video.get("color_transfer") in HDR_TRANSFERS
        prep = ("scale=360:640:force_original_aspect_ratio=decrease:force_divisible_by=2" +
                ("," + HDR_TO_SDR if hdr else "") + ",setsar=1")
        time_picture = perf_counter()
        for index, (name, grade) in enumerate((("reference", None), ("graded", options.grade))):
            run_ffmpeg(base + ["-progress", "pipe:1", "-map", f"0:{video['index']}", "-an",
                               "-vf", f"{prep},{grade_filter(grade)}format=yuv420p", "-c:v", "libx264",
                               "-threads", "2", "-preset", "veryfast", "-movflags", "+faststart", name+".mp4"],
                       folder, round(duration*1000), lambda n: stage("sampling_picture", .08+.23*(index+n)))
        picture_seconds = perf_counter()-time_picture
        rgb = subprocess.check_output([settings.ffmpeg, "-nostdin", "-v", "error", "-i", str(folder/"reference.mp4"),
                                       "-vf", "fps=2,scale=96:54,format=gray", "-f", "rawvideo", "-"], timeout=30)
        pixels = np.frombuffer(rgb, dtype=np.uint8)
        histogram = np.bincount(pixels//16, minlength=16).tolist()
        result = {"id": job_id, "source_id": media["sha256"], "start": start, "duration": duration,
                  "video": metadata, "histogram": histogram,
                  "dark_fraction": float((pixels <= 3).mean()) if len(pixels) else None,
                  "bright_fraction": float((pixels >= 252).mean()) if len(pixels) else None,
                  "configuration": options.model_dump(), "picture_seconds": round(picture_seconds, 3), "audio_seconds": None, "audio": None, "available": ["reference.mp4", "graded.mp4"]}
        if audio:
            time_sound = perf_counter()
            stage("sampling_sound", .58)
            wave = ["-acodec", "pcm_s16le", "-ar", "48000"]
            subprocess.run(base + ["-map", f"0:{audio['index']}", "-vn", *wave, str(folder/"original.wav")],
                           capture_output=True, check=True, timeout=45)
            original = folder/"original.wav"
            enhanced = folder/"enhanced.wav"
            stage("sampling_sound", .7)
            before = measure_audio(settings.ffmpeg, original, lambda: stage("sampling_sound", .7))
            if options.voice.enabled and before.get("input_i") is not None:
                voice = options.voice
                measured = measure_audio(settings.ffmpeg, original, lambda: stage("sampling_sound", .75),
                                         chain=speech_filter(voice), voice=voice)
                subprocess.run([settings.ffmpeg, "-nostdin", "-v", "error", "-i", str(original),
                                "-af", f"{speech_filter(voice)}{loudnorm(voice, measured)},aresample=48000,apad",
                                "-t", str(duration), *wave, str(enhanced)], capture_output=True, check=True, timeout=45)
            else:
                shutil.copyfile(original, enhanced)
            after = measure_audio(settings.ffmpeg, enhanced, lambda: stage("sampling_sound", .82))
            import wave as wave_file
            with wave_file.open(str(original)) as samples:
                raw = np.frombuffer(samples.readframes(samples.getnframes()), dtype="<i2").astype(float)/32768
            blocks = [x for x in np.array_split(raw, min(96, max(1, len(raw)//512))) if len(x)]
            rms = lambda values: float(20*math.log10(max(1e-6, np.sqrt(np.mean(values**2))))) if len(values) else None
            levels = [rms(block) for block in blocks]
            result["audio"] = {"original": before, "enhanced": after, "sample_rate": audio.get("sample_rate"),
                               "channels": audio.get("channels"), "layout": audio.get("channel_layout"),
                               "waveform": [float(np.abs(block).max()) for block in blocks], "rms_dbfs": rms(raw),
                               "quiet_window_rms_estimate": float(np.percentile(levels, 20)) if levels else None,
                               "near_full_scale_fraction": float((np.abs(raw)>=.999).mean()) if len(raw) else None,
                               "quiet_window_fraction_estimate": sum(level < -50 for level in levels)/len(levels) if levels else None}
            result["available"] += ["original.wav", "enhanced.wav"]
            if before.get("input_i") is not None and after.get("input_i") is not None:
                for name, level in (("original", before["input_i"]), ("enhanced", after["input_i"])):
                    attenuation = min(before["input_i"], after["input_i"])-level
                    target = f"{name}-matched.wav"
                    subprocess.run([settings.ffmpeg, "-nostdin", "-v", "error", "-i", str(folder/f"{name}.wav"),
                                    "-af", f"volume={attenuation:.2f}dB", *wave, str(folder/target)],
                                   capture_output=True, check=True, timeout=30)
                    result["available"].append(target)
            result["audio_seconds"] = round(perf_counter()-time_sound, 3)
        result["seconds"] = round(perf_counter()-began, 3)
        jobs.complete(job_id, lambda db: db.execute("INSERT INTO finish_analyses VALUES (?,?,?,?)",
                      (job_id, str(project), entry["cache_key"], json.dumps(result))))
        published = True
    finally:
        if not published:
            shutil.rmtree(folder)


def finish_routes(renders):
    router = APIRouter(prefix="/projects/{project_id}/finish")
    projects = renders.projects
    jobs = renders.jobs

    @router.get("")
    def state(project_id: UUID):
        projects.get(project_id)
        with projects.connect() as db:
            current = renders.selected(db, project_id)
            plan = current[1]["id"] if current else None
            rows = db.execute("SELECT kind,payload FROM finish_revisions WHERE project_id=? ORDER BY rowid DESC",
                              (str(project_id),))
            items = {"grade": [], "voice": []}
            for row in rows:
                items[row["kind"]].append(json.loads(row["payload"]))
            active = {kind: next((item for item in history if item["plan_id"] == plan), None)
                      for kind, history in items.items()}
            latest = db.execute("SELECT payload FROM finish_analyses WHERE project_id=? ORDER BY rowid DESC LIMIT 1",
                                (str(project_id),)).fetchone()
            job = jobs.public(jobs.latest(db, project_id, FINISH))
            suggestion = current[1].get("finishing") if current else None
        return {"plan_id": plan, "history": items, "current": active, "grade_presets": GRADES, "voice_presets": VOICES,
                "analysis": json.loads(latest[0]) if latest else None, "job": job,
                "advice": suggestion, "recommendation": {"grade": "mystical-warmth@1", "voice": "elevate-voice@1",
                                   "rationale": "Playbook starting point. Review the image and listen before applying."}}

    @router.post("/analysis", status_code=202, max_body_size=8192)
    def request_analysis(project_id: UUID, options: AnalysisInput):
        media = projects.get(project_id)["media"]
        if not media:
            raise HTTPException(409, "Upload a source first.")
        key = cache_key(media["sha256"], options)
        with projects.connect() as db:
            row = db.execute("SELECT payload FROM finish_analyses WHERE project_id=? AND cache_key=?",
                             (str(project_id), key)).fetchone()
        if row:
            return {"analysis": json.loads(row[0])}
        def prepare(db, job):
            db.execute("INSERT INTO finish_inputs VALUES (?,?,?,?)",
                       (str(job), media["sha256"], key, options.model_dump_json()))
        return {"job": jobs.enqueue(project_id, kind=FINISH, prepare=prepare, require_audio=False)}

    @router.post("/analysis/{job_id}/cancel")
    def cancel(project_id: UUID, job_id: UUID):
        return jobs.cancel(project_id, job_id, FINISH)

    @router.get("/analysis/{job_id}/{name}")
    def preview(project_id: UUID, job_id: UUID, name: str):
        with projects.connect() as db:
            row = db.execute("SELECT payload FROM finish_analyses WHERE project_id=? AND id=?",
                             (str(project_id), str(job_id))).fetchone()
        if not row or name not in json.loads(row[0])["available"]:
            raise HTTPException(404, "Preview not found.")
        root = projects.media_dir(project_id).resolve()
        folder = (root / f"finish-{job_id}").resolve()
        path = (folder / name).resolve()
        if folder.parent != root or path.parent != folder or not path.is_file():
            raise HTTPException(404, "Preview not found.")
        return FileResponse(path, media_type="video/mp4" if name.endswith(".mp4") else "audio/wav",
                            headers={"X-Content-Type-Options": "nosniff"})

    @router.post("/{kind}", status_code=201, max_body_size=8192)
    def save(project_id: UUID, kind: str, body: FinishInput):
        presets, values = (GRADES, body.grade) if kind == "grade" else (VOICES, body.voice)
        if kind not in {"grade", "voice"} or body.preset not in presets or (kind == "grade" and body.voice is not None) or (kind == "voice" and body.grade is not None):
            raise HTTPException(422, "Choose a valid finishing preset.")
        values = values if values is not None else presets[body.preset][1]
        projects.get(project_id)
        with projects.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            selected = renders.selected(db, project_id, body.plan_id)
            if not selected:
                raise HTTPException(409, "Select the director revision before saving.")
            plan = selected[1]
            if issue := renders.revision_issue(db, project_id, plan):
                raise HTTPException(409, issue)
            if body.analysis_id:
                row = db.execute("SELECT payload FROM finish_analyses WHERE project_id=? AND id=?",
                                 (str(project_id), str(body.analysis_id))).fetchone()
                if not row or json.loads(row[0])["source_id"] != plan["source_media_id"]:
                    raise HTTPException(422, "Analysis must match this original.")
            revision = {"id": str(uuid4()), "plan_id": str(body.plan_id), "preset": body.preset,
                        "settings": values.model_dump(), "source_id": plan["source_media_id"],
                        "analysis_id": str(body.analysis_id) if body.analysis_id else None,
                        "decision": "saved-for-preview", "created_at": datetime.now(timezone.utc).isoformat()}
            db.execute("INSERT INTO finish_revisions VALUES (?,?,?,?,?)",
                       (revision["id"], str(project_id), str(body.plan_id), kind, json.dumps(revision)))
        return revision

    return router
