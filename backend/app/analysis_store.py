import json
from uuid import UUID

from fastapi import HTTPException
from pydantic import ValidationError

from .silence import DetectionSettings, EditingPlan, Review, Suggestion, update_plan

ANALYSIS = "audio-analysis"


class AnalysisStore:
    def __init__(self, jobs):
        self.jobs = jobs
        self.projects = jobs.projects

    def initialize(self):
        with self.projects.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS detection_settings (
                    project_id TEXT PRIMARY KEY REFERENCES projects(id),
                    configuration TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS analysis_inputs (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(id),
                    configuration TEXT NOT NULL,
                    transcript_version TEXT REFERENCES transcripts(job_id)
                );
                CREATE TABLE IF NOT EXISTS audio_cache (
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    cache_key TEXT NOT NULL,
                    artifact_job_id TEXT NOT NULL REFERENCES jobs(id),
                    PRIMARY KEY (project_id, cache_key)
                );
                CREATE TABLE IF NOT EXISTS edit_plans (
                    id TEXT PRIMARY KEY REFERENCES jobs(id),
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    waveform_job_id TEXT NOT NULL REFERENCES jobs(id),
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS edit_plans_project ON edit_plans(project_id);
            """)

    def configuration(self, db, project_id):
        row = db.execute("SELECT configuration FROM detection_settings WHERE project_id=?",
                         (str(project_id),)).fetchone()
        return DetectionSettings.model_validate_json(row[0]) if row else DetectionSettings()

    def save_configuration(self, project_id, settings):
        self.projects.get(project_id)
        with self.projects.connect() as db:
            db.execute("""
                INSERT INTO detection_settings VALUES (?, ?) ON CONFLICT(project_id)
                DO UPDATE SET configuration=excluded.configuration
            """, (str(project_id), settings.model_dump_json()))
        return settings

    def enqueue(self, project_id, retry=None):
        def prepare(db, job_id):
            if retry:
                config, version = db.execute(
                    "SELECT configuration, transcript_version FROM analysis_inputs WHERE job_id=?",
                    (str(retry),),
                ).fetchone()
            else:
                config = self.configuration(db, project_id).model_dump_json()
                transcript = db.execute(
                    "SELECT job_id FROM transcripts WHERE project_id=? ORDER BY rowid DESC LIMIT 1",
                    (str(project_id),),
                ).fetchone()
                version = transcript[0] if transcript else None
            db.execute("INSERT INTO analysis_inputs VALUES (?, ?, ?)", (str(job_id), config, version))
        return self.jobs.enqueue(project_id, retry, kind=ANALYSIS, prepare=prepare)

    def inputs(self, job_id):
        with self.projects.connect() as db:
            config, transcript = db.execute(
                "SELECT configuration, transcript_version FROM analysis_inputs WHERE job_id=?",
                (job_id,),
            ).fetchone()
        return DetectionSettings.model_validate_json(config), UUID(transcript) if transcript else None

    def latest_row(self, db, project_id, plan_id=None):
        return db.execute("""
            SELECT * FROM edit_plans WHERE project_id=? AND (? IS NULL OR id=?)
            ORDER BY rowid DESC LIMIT 1
        """, (str(project_id), str(plan_id) if plan_id else None, str(plan_id))).fetchone()

    def state(self, project_id):
        self.projects.get(project_id)
        with self.projects.connect() as db:
            db.execute("BEGIN")
            plan = self.latest_row(db, project_id)
            return {
                "job": self.jobs.public(self.jobs.latest(db, project_id, ANALYSIS)),
                "plan_id": plan["id"] if plan else None,
                "settings": self.configuration(db, project_id),
            }

    def plan_row(self, project_id, plan_id=None):
        self.projects.get(project_id)
        with self.projects.connect() as db:
            row = self.latest_row(db, project_id, plan_id)
        if row is None:
            raise HTTPException(404, "No matching editing plan is available.")
        return row

    def history(self, project_id):
        self.projects.get(project_id)
        with self.projects.connect() as db:
            rows = db.execute("SELECT payload FROM edit_plans WHERE project_id=? ORDER BY rowid DESC",
                              (str(project_id),)).fetchall()
        return [
            {"id": plan["id"], "created_at": plan["created_at"],
             "approved_count": len(plan["approved_cuts"])}
            for row in rows for plan in [json.loads(row[0])]
        ]

    def waveform_path(self, project_id, job_id):
        return self.projects.media_dir(project_id) / f"audio-analysis-{UUID(str(job_id))}.json"

    def cached(self, project_id, key):
        with self.projects.connect() as db:
            row = db.execute("SELECT artifact_job_id FROM audio_cache WHERE project_id=? AND cache_key=?",
                             (str(project_id), key)).fetchone()
        if row:
            path = self.waveform_path(project_id, row[0])
            if path.is_file():
                return row[0], json.loads(path.read_text(encoding="utf-8"))
        return None

    def publish(self, plan, cache_key, waveform_job, commit_artifact):
        def publish(db):
            if commit_artifact:
                commit_artifact()
                db.execute("""
                    INSERT INTO audio_cache VALUES (?, ?, ?) ON CONFLICT(project_id, cache_key)
                    DO UPDATE SET artifact_job_id=excluded.artifact_job_id
                """, (str(plan.project_id), cache_key, str(waveform_job)))
            db.execute("INSERT INTO edit_plans VALUES (?, ?, ?, ?)",
                       (str(plan.id), str(plan.project_id), str(waveform_job), plan.model_dump_json()))
        self.jobs.complete(str(plan.id), publish)

    def review(self, project_id, plan_id, suggestion_id=None, decision: Review | None = None):
        with self.projects.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.latest_row(db, project_id)
            if row is None or row["id"] != str(plan_id):
                raise HTTPException(409, "This plan is historical or unavailable. Review the current plan.")
            plan = EditingPlan.model_validate_json(row["payload"])
            if suggestion_id and all(item.id != suggestion_id for item in plan.proposed_cuts):
                raise HTTPException(404, "Cut suggestion not found.")
            try:
                suggestions = []
                for item in plan.proposed_cuts:
                    if decision and item.id == suggestion_id:
                        item = Suggestion.model_validate({**item.model_dump(), **decision.model_dump()})
                    elif decision is None and item.confidence == "high" and item.status == "pending":
                        item = item.model_copy(update={"status": "accepted"})
                    suggestions.append(item)
                plan = update_plan(plan, suggestions)
            except ValidationError as error:
                raise HTTPException(422, error.errors()[0]["msg"]) from error
            db.execute("UPDATE edit_plans SET payload=? WHERE id=?", (plan.model_dump_json(), str(plan.id)))
        return plan
