import hashlib
import json
import math
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import HTTPException

from .analysis_store import AnalysisStore
from .brand import Brand
from .director import PLAYBOOKS, PROMPT_VERSION, DirectorError, DirectorPlan, DirectorSettings, request_body, usage_record
from .render_manifest import FPS
from .silence import EditingPlan
from .timeline import Span, Timeline, merge_spans
from .transcription import Transcript

DIRECTOR = "director"
MAX_RETRIES = 2


def now():
    return datetime.now(timezone.utc).isoformat()


class DirectorStore:
    def __init__(self, jobs):
        self.jobs = jobs
        self.projects = jobs.projects

    def initialize(self):
        with self.projects.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS brand_revisions (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    created_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS director_inputs (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(id), snapshot TEXT NOT NULL,
                    context TEXT NOT NULL, refs TEXT NOT NULL, estimated INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS director_usage (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(id), payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS director_plans (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS director_current (
                    project_id TEXT PRIMARY KEY REFERENCES projects(id),
                    plan_id TEXT NOT NULL REFERENCES director_plans(id)
                );
            """)

    def brands(self, project_id):
        self.projects.get(project_id)
        with self.projects.connect() as db:
            rows = db.execute("SELECT * FROM brand_revisions WHERE project_id=? ORDER BY rowid DESC",
                              (str(project_id),)).fetchall()
        return [{"id": row["id"], "created_at": row["created_at"], "settings": json.loads(row["payload"])}
                for row in rows] + [{"id": "default-v1", "created_at": None, "settings": Brand().model_dump()}]

    def save_brand(self, project_id, brand):
        self.projects.get(project_id)
        revision, created = str(uuid4()), now()
        with self.projects.connect() as db:
            db.execute("INSERT INTO brand_revisions VALUES (?, ?, ?, ?)",
                       (revision, str(project_id), created, brand.model_dump_json()))
        return {"id": revision, "created_at": created, "settings": brand.model_dump()}

    def brand_revision(self, project_id, revision):
        return next((entry for entry in self.brands(project_id) if entry["id"] == revision), None)

    def inputs(self, project_id, playbook_id):
        from .visual_store import GUIDANCE, latest as latest_visual
        from .sound_store import PLAYBOOK_SOUND
        from .motion_store import MotionStore, rules, suggested_events
        cfg = DirectorSettings.from_env()
        if cfg.mode not in ("openai", "mock"):
            raise HTTPException(503, "Set ELEVATE_DIRECTOR_MODE to mock or openai.")
        playbook = PLAYBOOKS.get(playbook_id)
        if playbook is None:
            raise HTTPException(422, "Unknown editing playbook.")
        media = self.projects.get(project_id)["media"]
        if not media:
            raise HTTPException(409, "Upload original footage first.")
        transcript_id = self.jobs.state(project_id)["transcript_job_id"]
        if not transcript_id:
            raise HTTPException(409, "Generate a transcript before using the director.")
        try:
            row = AnalysisStore(self.jobs).plan_row(project_id)
        except HTTPException as error:
            raise HTTPException(409, "Analyse the audio and review its silence plan first.") from error
        silence = EditingPlan.model_validate_json(row["payload"])
        transcript_file = self.projects.media_dir(project_id) / f"transcription-{UUID(transcript_id)}.json"
        transcript = Transcript.model_validate_json(transcript_file.read_text(encoding="utf-8"))
        duration = math.floor(media["duration_seconds"] * 1000)
        if (transcript.project_id != project_id or silence.project_id != project_id
                or transcript.source_media_id != media["sha256"] or silence.source_media_id != media["sha256"]
                or silence.source_duration_ms != duration or abs(transcript.source_duration * 1000 - duration) >= 1):
            raise HTTPException(409, "Transcript, silence plan, and source metadata do not match.")
        if silence.transcript_version and str(silence.transcript_version) != transcript_id:
            raise HTTPException(409, "Regenerate the silence plan using the latest transcript before directing.")
        brand = self.brands(project_id)[0]
        words = []
        for segment in transcript.segments:
            for word in segment.words:
                begin, end = max(0, round(word.start * 1000)), min(duration, round(word.end * 1000))
                if end > begin and word.text.strip():
                    words.append({"start_ms": begin, "end_ms": end, "text": word.text.strip()})
        # Keep eligible AI cuts inside Day 3's guarded, high-confidence candidates.
        eligible = [cut.proposed.model_dump() for cut in silence.proposed_cuts
                    if cut.status == "pending" and cut.confidence == "high"]
        with self.projects.connect() as db:
            has_finish = db.execute("SELECT 1 FROM sqlite_master WHERE name='finish_analyses'").fetchone()
            finish_row = db.execute("SELECT payload FROM finish_analyses WHERE project_id=? ORDER BY rowid DESC LIMIT 1",
                                    (str(project_id),)).fetchone() if has_finish else None
            has_motion = db.execute("SELECT 1 FROM sqlite_master WHERE name='motion_revisions'").fetchone()
            current = db.execute("""SELECT p.payload FROM director_current c JOIN director_plans p ON p.id=c.plan_id
                                   WHERE c.project_id=?""", (str(project_id),)).fetchone()
            current_plan = json.loads(current[0]) if current else None
            has_sound = db.execute("SELECT 1 FROM sqlite_master WHERE name='sound_assets'").fetchone()
            sound_assets = [json.loads(row[0]) for row in db.execute(
                "SELECT payload FROM sound_assets ORDER BY rowid DESC LIMIT 30")] if has_sound else []
            from .studio_store import pinned_studio
            has_studio = db.execute("SELECT 1 FROM sqlite_master WHERE name='studio_revisions'").fetchone()
            studio_revision = pinned_studio(db,project_id,current_plan["id"]) if has_studio and current_plan else {}
            has_visual = db.execute("SELECT 1 FROM sqlite_master WHERE name='visual_assets'").fetchone()
            visual = latest_visual(db, project_id, current_plan["id"]) if has_visual and current_plan else None
            local = [json.loads(row[0]) for row in db.execute("SELECT payload FROM visual_assets ORDER BY rowid DESC LIMIT 30")] if has_visual else []
            public_render = (db.execute("""SELECT i.manifest FROM render_inputs i JOIN renders r ON r.id=i.job_id
                                        WHERE r.project_id=? ORDER BY r.rowid DESC LIMIT 1""", (str(project_id),)).fetchone()
                             if has_visual else None)
            face = MotionStore.face(db, project_id) if has_motion else None
            motion = MotionStore.latest(db, project_id, current_plan["id"]) if has_motion and current_plan else None
        saved = json.loads(motion["payload"]) if motion else {}
        current_cuts = ([{"start_ms": op["start_ms"], "end_ms": op["end_ms"]}
                         for op in current_plan["operations"] if op["kind"] == "cut" and op["status"] == "accepted"]
                        if current_plan and current_plan["source_media_id"] == media["sha256"] else [])
        energetic = saved.get("settings", {}).get("energy") == "energetic"
        maximum, transition, style = rules(playbook_id, energetic)
        measured = json.loads(finish_row[0]) if finish_row else {}
        audio = measured.get("audio") or {}
        render_context = json.loads(public_render[0]) if public_render else {}
        sound_speed = render_context.get("settings",{}).get("playback_speed",1)
        sound_duration = (round(sum(r["end_frame"]-r["start_frame"] for r in render_context["frame_ranges"])*1000/30/sound_speed)
                          if render_context and current_plan and render_context["plan_id"]==current_plan["id"] else
                          round(sum(span.duration for span in silence.retained_segments)/sound_speed))
        studio_key = hashlib.sha256(f"studio-analysis-v1:{media['sha256']}".encode()).hexdigest()
        studio_report=self.projects.media_dir(project_id)/"studio"/f"{studio_key}.json"
        detected=json.loads(studio_report.read_text()) if studio_report.is_file() else {}
        context = {
            "context_id": str(uuid4()), "duration_ms": duration,
            "media": {key: media.get(key) for key in ("width", "height", "video_codec", "audio_codec")},
            "language": transcript.language, "words": words,
            "transcript_segments": [{"start_ms": round(item.start * 1000), "end_ms": min(duration, round(item.end * 1000)),
                                     "text": item.text} for item in transcript.segments if not item.words],
            "approved_cuts": [span.model_dump() for span in silence.approved_cuts],
            "rejected_ranges": [cut.source.model_dump() for cut in silence.proposed_cuts if cut.status == "rejected"],
            "retained_segments": [span.model_dump() for span in silence.retained_segments],
            "eligible_cuts": eligible, "minimum_cut_ms": silence.detection.minimum_removable_ms,
            "brand": brand["settings"], "playbook": playbook.model_dump(),
            "brand_revision": brand["id"], "playbook_revision": f"{playbook.id}@{playbook.revision}",
            "studio":{"detected":detected.get("detected",False),"confidence":detected.get("confidence"),
                      "preference":studio_revision.get("studio",{}).get("preset"),
                      "assets":[{key:row[key] for key in ("id","kind","category")} for row in local if row["category"]=="Virtual Studio"]},
            "sound":{"guidance":PLAYBOOK_SOUND[playbook_id],"duration_ms":min(sound_duration,1200000),
                     "assets":[{key:asset[key] for key in ("id","mood","kind","duration","genre")} for asset in sound_assets],
                     "voice_metrics":{key:audio.get("original",{}).get(key) for key in ("input_i","input_tp")},
                     "broll":[{"source":row["source"],"purpose":row["purpose"]} for row in
                              json.loads(visual[0])["events"] if row["status"]=="accepted"] if visual else []},
            "visuals": {"guidance": GUIDANCE[playbook_id],
                        "speed": json.loads(public_render[0])["settings"]["playback_speed"] if public_render else 1,
                        "approved_motion": [{"source":e["source"],"end_zoom":e["end_zoom"]} for e in saved.get("events", [])
                                            if e["status"]=="accepted"],
                        "local_assets": [{"category":row["category"],"kind":row["kind"]} for row in local
                                         if row["provenance"]["provider"]=="personal"],
                        "prior_decisions": [{"source":event["source"],"purpose":event["purpose"],"status":event["status"]}
                                            for event in json.loads(visual[0])["events"]] if visual else []},
            "finishing": {"evidence": "Sampled metadata only" if measured else "No visual or audio sample measurements",
                          "video": {key: measured.get("video", {}).get(key) for key in
                                    ("interpretation", "color_primaries", "color_transfer")},
                          "near_black": measured.get("dark_fraction"), "near_white": measured.get("bright_fraction"),
                          "audio": {key: audio.get("original", {}).get(key) for key in ("input_i", "input_tp", "input_lra")}},
            "camera_motion": {"maximum": maximum, "transition": transition, "guidance": style,
                              "target": min(maximum, brand["settings"]["max_zoom_percent"], playbook.motion_target),
                              "face_summary": face["summary"] if face else None, "other_approved_cuts": current_cuts,
                              "existing": saved.get("events", suggested_events(current_plan) if current_plan else [])},
        }
        refs = {"source_media_id": media["sha256"], "transcript_revision": transcript_id,
                "silence_plan_revision": str(silence.id),
                "silence_snapshot_sha256": hashlib.sha256(row["payload"].encode()).hexdigest(),
                "brand_revision": brand["id"], "playbook_revision": f"{playbook.id}@{playbook.revision}",
                "prompt_version": PROMPT_VERSION}
        snapshot = cfg.snapshot()
        size = len(json.dumps(request_body(snapshot, context), ensure_ascii=False).encode())
        if size > 100_000:
            raise HTTPException(422, "This transcript is too large for a single director job.")
        return snapshot, context, refs, math.ceil(size / 3)

    def record(self, job_id):
        with self.projects.connect() as db:
            row = db.execute("SELECT * FROM director_inputs WHERE job_id=?", (str(job_id),)).fetchone()
        return tuple(json.loads(row[name]) for name in ("snapshot", "context", "refs")) + (row["estimated"],)

    def enqueue(self, project_id, playbook_id, paid, retry=None):
        if retry:
            job = self.jobs.get(project_id, retry, DIRECTOR)
            if job["retry_count"] >= MAX_RETRIES:
                raise HTTPException(409, "This generation has reached its two-retry limit.")
            record = self.record(retry)
        else:
            record = self.inputs(project_id, playbook_id)
        snapshot, context, refs, estimate = record
        if snapshot["mode"] == "openai":
            if not paid:
                raise HTTPException(422, "Confirm the paid API request before queuing.")
            if not DirectorSettings.from_env().key:
                raise HTTPException(503, "OpenAI credentials are missing from the server configuration.")

        def prepare(db, job_id):
            db.execute("INSERT INTO director_inputs VALUES (?, ?, ?, ?, ?)",
                       (str(job_id), json.dumps(snapshot), json.dumps(context), json.dumps(refs), estimate))
        return self.jobs.enqueue(project_id, retry, kind=DIRECTOR, prepare=prepare)

    def state(self, project_id):
        self.projects.get(project_id)
        cfg = DirectorSettings.from_env()
        with self.projects.connect() as db:
            db.execute("BEGIN")
            selected = db.execute("SELECT plan_id FROM director_current WHERE project_id=?", (str(project_id),)).fetchone()
            jobs = db.execute("""
                SELECT j.id, j.status, i.snapshot, i.estimated, u.payload
                FROM jobs j JOIN director_inputs i ON i.job_id=j.id
                LEFT JOIN director_usage u ON u.job_id=j.id WHERE j.project_id=? ORDER BY j.rowid DESC
            """, (str(project_id),)).fetchall()
            latest = self.jobs.public(self.jobs.latest(db, project_id, DIRECTOR))
        return {"job": latest, "selected": selected[0] if selected else None, "mode": cfg.mode,
                "model": cfg.snapshot()["model"], "credentials_ready": bool(cfg.key),
                "playbooks": list(PLAYBOOKS.values()),
                "usage": [{"id": row["id"], "status": row["status"], "model": json.loads(row["snapshot"])["model"],
                           "mode": json.loads(row["snapshot"])["mode"],
                           **(json.loads(row["payload"]) if row["payload"] else
                              usage_record(json.loads(row["snapshot"]), row["estimated"], None))}
                          for row in jobs]}

    def usage(self, job_id, data):
        with self.projects.connect() as db:
            db.execute("INSERT INTO director_usage VALUES (?, ?)", (str(job_id), json.dumps(data)))

    def write(self, db, plan):
        plan = DirectorPlan.model_validate(plan).model_dump(mode="json")
        db.execute("INSERT INTO director_plans VALUES (?, ?, ?)",
                   (plan["id"], plan["project_id"], json.dumps(plan)))
        db.execute("""
            INSERT INTO director_current VALUES (?, ?) ON CONFLICT(project_id)
            DO UPDATE SET plan_id=excluded.plan_id
        """, (plan["project_id"], plan["id"]))

    def get(self, project_id, plan_id):
        with self.projects.connect() as db:
            row = db.execute("SELECT payload FROM director_plans WHERE id=? AND project_id=?",
                             (str(plan_id), str(project_id))).fetchone()
        if row is None:
            raise HTTPException(404, "Director revision not found.")
        plan = json.loads(row[0])
        with self.projects.connect() as db:
            plan["stale_silence"] = self.stale(db, project_id, plan)
        return plan

    def stale(self, db, project_id, plan):
        latest = AnalysisStore(self.jobs).latest_row(db, project_id)
        return latest is None or hashlib.sha256(latest["payload"].encode()).hexdigest() != plan["silence_snapshot_sha256"]

    def history(self, project_id):
        self.projects.get(project_id)
        with self.projects.connect() as db:
            rows = db.execute("SELECT payload FROM director_plans WHERE project_id=? ORDER BY rowid DESC",
                              (str(project_id),)).fetchall()
        keys = ("id", "parent_id", "event", "created_at", "model", "playbook_revision")
        return [{key: plan[key] for key in keys} for row in rows for plan in [json.loads(row[0])]]

    def revise(self, project_id, plan_id, index=None, status=None, *, manual_cut: Span | None = None):
        with self.projects.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM director_plans WHERE id=? AND project_id=?",
                             (str(plan_id), str(project_id))).fetchone()
            if not row:
                raise HTTPException(404, "Director revision not found.")
            plan = json.loads(row[0])
            reviewing = index is not None or manual_cut is not None
            if reviewing:
                if self.stale(db, project_id, plan):
                    raise HTTPException(409, "Silence decisions changed. Generate a new director draft.")
                current = db.execute("SELECT plan_id FROM director_current WHERE project_id=?",
                                     (str(project_id),)).fetchone()
                if current[0] != str(plan_id):
                    raise HTTPException(409, "Select or restore this revision before reviewing it.")
            if index is not None:
                if not 0 <= index < len(plan["operations"]):
                    raise HTTPException(404, "Recommendation not found.")
                plan["operations"][index]["status"] = status
            if manual_cut is not None:
                self.add_manual_cut(project_id, plan, manual_cut)
            try:
                updated = reviewed_plan(plan)
            except (DirectorError, ValueError) as error:
                raise HTTPException(422, str(error)) from error
            if manual_cut is not None:
                if not updated["retained_segments"]:
                    raise HTTPException(422, "The story cut would remove the entire video.")
                plan["proposed_retained_segments"] = updated["retained_segments"]
            plan.update(id=str(uuid4()), parent_id=str(plan_id), created_at=now(),
                        event="review" if reviewing else "restore")
            self.write(db, plan)
            return {**plan, "stale_silence": self.stale(db, project_id, plan)}


    def add_manual_cut(self, project_id, plan, cut: Span):
        if cut.duration < 100 or cut.end_ms > plan["source_duration_ms"]:
            raise HTTPException(422, "Choose a source range of at least 0.1 seconds within the video.")
        previously_approved = reviewed_plan(plan)["approved_cuts"]
        if any(item["start_ms"] <= cut.start_ms and cut.end_ms <= item["end_ms"] for item in previously_approved):
            raise HTTPException(422, "This range is already cut.")
        filename = self.projects.media_dir(project_id) / f"transcription-{UUID(plan['transcript_revision'])}.json"
        try:
            transcript = Transcript.model_validate_json(filename.read_bytes())
        except (OSError, ValueError) as error:
            raise HTTPException(409, "The selected transcript is unavailable.") from error
        if transcript.project_id != project_id or transcript.source_media_id != plan["source_media_id"]:
            raise HTTPException(409, "The transcript does not match this source.")
        # The renderer rounds retained ranges inward on the output frame grid.
        start = math.floor(cut.start_ms * FPS / 1000) / FPS
        end = math.ceil(cut.end_ms * FPS / 1000) / FPS
        for segment in transcript.segments:
            for word in segment.words or [segment]:
                if word.start < start < word.end or word.start < end < word.end:
                    raise HTTPException(422, "A cut edge falls inside timed speech. Move it between words.")
        plan["operations"].append({
            "kind": "cut", **cut.model_dump(), "reason": "Manual story cut.",
            "first_word": None, "last_word": None, "zoom_percent": None,
            "idea": None, "quote": None, "status": "accepted",
        })


def reviewed_plan(plan):
    duration = plan["source_duration_ms"]
    approved = [Span.model_validate(item) for item in plan["base_approved_cuts"]]
    approved += [Span(start_ms=op["start_ms"], end_ms=op["end_ms"]) for op in plan["operations"]
                 if op["kind"] == "cut" and op["status"] == "accepted"]
    approved = merge_spans(approved, duration)
    timeline = Timeline.from_cuts(duration, approved)
    edited = []
    for op in plan["operations"]:
        if op["kind"] == "cut" or op["status"] != "accepted":
            continue
        a = timeline.source_to_edited(op["start_ms"])
        # The end is exclusive. Map its previous millisecond and then advance.
        b = timeline.source_to_edited(op["end_ms"] - 1)
        if a is None or b is None or b - a != op["end_ms"] - 1 - op["start_ms"]:
            raise DirectorError("An accepted event overlaps this cut. Reject the event in AI Director before cutting this range.")
        edited.append({**op, "edited_start_ms": a, "edited_end_ms": b + 1})
    return {**plan, "approved_cuts": [item.model_dump() for item in approved],
            "retained_segments": [item.model_dump() for item in timeline.retained],
            "edited_duration_ms": timeline.duration_ms, "reviewed_events": edited}
