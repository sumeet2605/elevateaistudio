import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID, uuid4, uuid5

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .director import PLAYBOOKS
from .face_detection import detect_faces, detector_key
from .motion_models import CameraSettings, FaceSettings, MotionEvent, Sample, track_faces

FACE_ANALYSIS = "face_analysis"


def rules(revision, energetic=False):
    if energetic:
        return 125, "punch", "Emphasize strong hooks and contrasts without repeated motion."
    playbook = PLAYBOOKS.get(revision.split("@")[0], PLAYBOOKS["mystical-sumeet"])
    return playbook.motion_max, playbook.motion_transition, playbook.motion_guidance


class MotionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: UUID
    settings: CameraSettings
    events: list[MotionEvent] = Field(max_length=40)
    face_version: UUID | None = None


class MotionStore:
    def __init__(self, jobs):
        self.jobs = jobs
        self.projects = jobs.projects

    def initialize(self):
        with self.projects.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS face_inputs (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(id), source_id TEXT NOT NULL, options TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS face_results (
                    id TEXT PRIMARY KEY REFERENCES jobs(id), project_id TEXT NOT NULL REFERENCES projects(id),
                    cache_key TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(project_id, cache_key)
                );
                CREATE TABLE IF NOT EXISTS motion_revisions (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    plan_id TEXT NOT NULL REFERENCES director_plans(id), payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS motion_plan ON motion_revisions(project_id, plan_id);
            """)

    @staticmethod
    def latest(db, project_id, plan_id):
        return db.execute("""SELECT id,payload FROM motion_revisions WHERE project_id=? AND plan_id=?
                          ORDER BY rowid DESC LIMIT 1""", (str(project_id), str(plan_id))).fetchone()

    @staticmethod
    def face(db, project_id, version=None):
        query = "SELECT payload FROM face_results WHERE project_id=?"
        args = [str(project_id)]
        if version:
            query += " AND id=?"
            args.append(str(version))
        row = db.execute(query + " ORDER BY rowid DESC LIMIT 1", args).fetchone()
        return json.loads(row[0]) if row else None

    def enqueue(self, project_id, options):
        media = self.projects.get(project_id)["media"]
        if not media:
            raise HTTPException(409, "Upload an original first.")
        key = detector_key(media["sha256"], options)
        with self.projects.connect() as db:
            cached = db.execute("SELECT payload FROM face_results WHERE project_id=? AND cache_key=?",
                                (str(project_id), key)).fetchone()
        if cached:
            return {"analysis": json.loads(cached[0]), "cached": True}

        def prepare(db, job_id):
            db.execute("INSERT INTO face_inputs VALUES (?,?,?)", (str(job_id), media["sha256"], options.model_dump_json()))
        return {"job": self.jobs.enqueue(project_id, kind=FACE_ANALYSIS, prepare=prepare), "cached": False}

    def analyse(self, job, stage, settings):
        project_id = UUID(job["project_id"])
        with self.projects.connect() as db:
            operation = db.execute("SELECT source_id,options FROM face_inputs WHERE job_id=?", (job["id"],)).fetchone()
        media = self.projects.get(project_id)["media"]
        source = self.projects.source_path(project_id, media)
        if media["sha256"] != operation["source_id"]:
            raise ValueError("Face analysis no longer matches the original.")
        from .render_store import digest
        if digest(source) != operation["source_id"]:
            raise ValueError("The original bytes no longer match this face-analysis job.")
        options = FaceSettings.model_validate_json(operation["options"])
        key = detector_key(media["sha256"], options)
        stage("detecting_faces", 0)
        result = detect_faces(self.projects.root, source, int(media["duration_seconds"] * 1000), options,
                              lambda progress: stage("detecting_faces", progress),
                              ffmpeg=settings.ffmpeg, ffprobe=settings.ffprobe, media=media)
        counts = [sum(box["confidence"] >= options.min_confidence for box in sample["faces"])
                  for sample in result["samples"]]
        result.update(id=job["id"], source_id=media["sha256"], options=options.model_dump(),
                      summary={"samples": len(counts), "single": counts.count(1), "missing": counts.count(0),
                               "multiple": sum(count > 1 for count in counts)})

        def publish(db):
            db.execute("INSERT OR IGNORE INTO face_results VALUES (?,?,?,?)",
                       (job["id"], str(project_id), key, json.dumps(result)))
        self.jobs.complete(job["id"], publish)


def suggested_events(plan):
    _, transition, _ = rules(plan["playbook_revision"])
    plan_id = UUID(plan["id"])
    return [MotionEvent(id=uuid5(plan_id, f"zoom-{index}"),
                        source={"start_ms": op["start_ms"], "end_ms": op["end_ms"]},
                        end_zoom=op["zoom_percent"],
                        **({"transition": transition} if not op.get("zoom_motion") else op["zoom_motion"]),
                        reason=op["reason"], status=op["status"]).model_dump(mode="json")
            for index, op in enumerate(plan["operations"])
            if op["kind"] == "zoom" and op["end_ms"] - op["start_ms"] >= 100]


def motion_routes(renders):
    router = APIRouter(prefix="/projects/{project_id}")
    store = MotionStore(renders.jobs)

    @router.get("/motion")
    def state(project_id: UUID):
        store.projects.get(project_id)
        with store.projects.connect() as db:
            db.execute("BEGIN")
            selected = renders.selected(db, project_id)
            plan = selected[1] if selected else None
            history = [public_revision(json.loads(row[0])) for row in db.execute("""
                SELECT payload FROM motion_revisions WHERE project_id=? ORDER BY rowid DESC
            """, (str(project_id),))]
            current = next((item for item in history if plan and item["plan_id"] == plan["id"]), None)
            active_face = current["face_version"] if current else None
            analysis = store.face(db, project_id, active_face)
            playbook = PLAYBOOKS[plan["playbook_revision"].split("@")[0]] if plan else None
            target = min(playbook.motion_target, playbook.motion_max, plan["brand"]["max_zoom_percent"]) if playbook else 105
            return {"plan_id": plan["id"] if plan else None, "default_zoom": target, "current": current, "history": history,
                    "suggestions": suggested_events(plan) if plan else [],
                    "analysis": analysis, "latest_analysis": store.face(db, project_id),
                    "job": store.jobs.public(store.jobs.latest(db, project_id, FACE_ANALYSIS))}

    @router.post("/motion/analysis", status_code=202, max_body_size=8192)
    def analyse(project_id: UUID, options: FaceSettings):
        return store.enqueue(project_id, options)

    @router.get("/motion/analysis/{version}")
    def read_analysis(project_id: UUID, version: UUID):
        with store.projects.connect() as db:
            result = store.face(db, project_id, version)
        if not result:
            raise HTTPException(404, "Face analysis revision not found.")
        return result

    @router.post("/motion/analysis/{job_id}/cancel")
    def cancel(project_id: UUID, job_id: UUID):
        return store.jobs.cancel(project_id, job_id, FACE_ANALYSIS)

    @router.post("/motion", status_code=201, max_body_size=65536)
    def save(project_id: UUID, request: MotionInput):
        from .render_manifest import RenderSettings
        manifest = renders.make_manifest(project_id, request.plan_id, RenderSettings(), include_motion=False)
        with store.projects.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if issue := renders.stale(db, manifest, check_motion=False):
                raise HTTPException(409, issue)
            face = store.face(db, project_id, request.face_version) if request.face_version else None
            if request.face_version and (not face or face["source_id"] != manifest.source_media_id):
                raise HTTPException(422, "The face analysis must match this original.")
            history = store.latest(db, project_id, request.plan_id)
            revision = str(uuid4())
            events = sorted(request.events, key=lambda event: event.source.start_ms)
            track = []
            if face:
                track = track_faces([Sample.model_validate(item) for item in face["samples"]],
                                    face["options"]["min_confidence"], request.settings.dead_zone, request.settings.smoothing)
            payload = {**request.model_dump(mode="json"), "id": revision,
                       "events": [event.model_dump(mode="json") for event in events],
                       "source_id": manifest.source_media_id, "brand_revision": manifest.brand_revision,
                       "playbook_revision": manifest.playbook_revision, "parent_id": history["id"] if history else None,
                       "created_at": datetime.now(timezone.utc).isoformat(),
                       "track": [[time, point.model_dump()] for time, point in track],
                       "geometry": [face["width"], face["height"]] if face else None}
            encoded = json.dumps(payload)
            try:
                manifest.__class__.model_validate({**manifest.model_dump(), **manifest_motion(revision, encoded)})
            except ValidationError as error:
                raise HTTPException(422, error.errors()[0]["msg"]) from error
            db.execute("INSERT INTO motion_revisions VALUES (?,?,?,?)",
                       (revision, str(project_id), str(request.plan_id), encoded))
        return public_revision(payload)

    return router


def public_revision(revision):
    return {key: value for key, value in revision.items() if key not in ("track", "geometry")}


def manifest_motion(revision, encoded):
    motion = json.loads(encoded)
    return {"zooms": [], "motion_revision": revision,
            "motion_fingerprint": hashlib.sha256(encoded.encode()).hexdigest(),
            "camera": motion["settings"], "motion_events": motion["events"],
            "face_track": motion["track"], "display_geometry": motion["geometry"],
            "face_version": motion["face_version"]}
