from uuid import UUID, uuid4

from fastapi import HTTPException

from .projects import Projects

NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"


class Jobs:
    def __init__(self, projects: Projects):
        self.projects = projects

    def initialize(self):
        with self.projects.connect() as db:
            db.executescript(f"""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    type TEXT NOT NULL DEFAULT 'transcription',
                    status TEXT NOT NULL CHECK (status IN
                        ('queued','running','completed','failed','cancelled','interrupted')),
                    progress REAL CHECK (progress BETWEEN 0 AND 1),
                    stage TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT ({NOW}),
                    started_at TEXT, completed_at TEXT, error TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    cancellation_requested INTEGER NOT NULL DEFAULT 0
                );
                CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_active ON jobs(project_id, type)
                    WHERE status IN ('queued','running');
                CREATE INDEX IF NOT EXISTS jobs_project_history ON jobs(project_id);
                CREATE TABLE IF NOT EXISTS transcripts (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(id),
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    source_id TEXT NOT NULL, model TEXT NOT NULL, language TEXT,
                    source_duration REAL NOT NULL, schema_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)

    @staticmethod
    def public(row):
        if row is None:
            return None
        job = dict(row)
        job["cancellation_requested"] = bool(job["cancellation_requested"])
        return job

    def latest(self, db, project_id: UUID):
        return db.execute(
            "SELECT * FROM jobs WHERE project_id=? ORDER BY rowid DESC LIMIT 1",
            (str(project_id),),
        ).fetchone()

    def get(self, project_id: UUID, job_id: UUID):
        with self.projects.connect() as db:
            job = db.execute("SELECT * FROM jobs WHERE project_id=? AND id=?",
                             (str(project_id), str(job_id))).fetchone()
        if job is None:
            raise HTTPException(404, "Transcription job not found.")
        return self.public(job)

    def state(self, project_id: UUID):
        self.projects.get(project_id)
        with self.projects.connect() as db:
            db.execute("BEGIN")
            artifact = db.execute(
                "SELECT job_id FROM transcripts WHERE project_id=? ORDER BY rowid DESC LIMIT 1",
                (str(project_id),),
            ).fetchone()
            return {
                "job": self.public(self.latest(db, project_id)),
                "transcript_job_id": artifact["job_id"] if artifact else None,
            }

    def enqueue(self, project_id: UUID, retry: UUID | None = None):
        media = self.projects.get(project_id)["media"]
        if not media:
            raise HTTPException(409, "Upload an original before generating a transcript.")
        if not media["audio_codec"]:
            raise HTTPException(422, "The original video has no audio track.")
        job_id = uuid4()
        with self.projects.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            latest = self.latest(db, project_id)
            if latest and latest["status"] in ("queued", "running"):
                raise HTTPException(409, "A transcription job is already active for this project.")
            retries = 0
            if retry:
                if not latest or latest["id"] != str(retry) or latest["status"] not in ("failed", "interrupted"):
                    raise HTTPException(409, "Only the latest failed or interrupted job can be retried.")
                retries = latest["retry_count"] + 1
            db.execute(
                "INSERT INTO jobs (id, project_id, status, stage, retry_count) VALUES (?, ?, 'queued', 'queued', ?)",
                (str(job_id), str(project_id), retries),
            )
        return self.get(project_id, job_id)

    def cancel(self, project_id: UUID, job_id: UUID):
        self.get(project_id, job_id)
        with self.projects.connect() as db:
            changed = db.execute(f"""
                UPDATE jobs SET cancellation_requested=1,
                    stage=CASE WHEN status='queued' THEN 'cancelled' ELSE 'cancelling' END,
                    completed_at=CASE WHEN status='queued' THEN {NOW} ELSE NULL END,
                    status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END
                WHERE id=? AND status IN ('queued','running')
            """, (str(job_id),))
            if changed.rowcount != 1:
                raise HTTPException(409, "Only an active transcription can be cancelled.")
        return self.get(project_id, job_id)

    def recover(self):
        with self.projects.connect() as db:
            db.execute(f"""
                UPDATE jobs SET
                    status=CASE WHEN cancellation_requested THEN 'cancelled' ELSE 'interrupted' END,
                    stage=CASE WHEN cancellation_requested THEN 'cancelled' ELSE 'interrupted' END,
                    error=CASE WHEN cancellation_requested THEN NULL ELSE 'The transcription worker stopped before finishing.' END,
                    completed_at={NOW}
                WHERE status='running'
            """)

    def claim(self):
        with self.projects.connect() as db:
            return self.public(db.execute(f"""
                UPDATE jobs SET status='running', stage='extracting_audio', started_at={NOW}
                WHERE id=(SELECT id FROM jobs WHERE status='queued' ORDER BY rowid LIMIT 1)
                RETURNING *
            """).fetchone())

    def stage(self, job_id: str, stage: str, progress: float | None = None):
        with self.projects.connect() as db:
            updated = db.execute("""
                UPDATE jobs SET stage=?, progress=? WHERE id=? AND status='running'
                    AND cancellation_requested=0
            """, (stage, progress, job_id))
            if updated.rowcount != 1:
                raise Cancelled

    def finish(self, job_id: str, status: str, error: str | None = None):
        with self.projects.connect() as db:
            db.execute(f"""
                UPDATE jobs SET
                    status=CASE WHEN cancellation_requested THEN 'cancelled' ELSE ? END,
                    stage=CASE WHEN cancellation_requested THEN 'cancelled' ELSE ? END,
                    error=CASE WHEN cancellation_requested THEN NULL ELSE ? END, completed_at={NOW}
                WHERE id=? AND status='running'
            """, (status, status, error, job_id))

    def publish(self, job_id: str, transcript, commit_artifact):
        with self.projects.connect() as db:
            updated = db.execute(f"""
                UPDATE jobs SET status='completed', stage='completed', progress=1, completed_at={NOW}
                WHERE id=? AND status='running' AND cancellation_requested=0
            """, (job_id,))
            if updated.rowcount != 1:
                raise Cancelled
            db.execute("INSERT INTO transcripts VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
                job_id, str(transcript.project_id), transcript.source_media_id, transcript.model,
                transcript.language, transcript.source_duration, transcript.schema_version,
                transcript.created_at.isoformat(),
            ))
            commit_artifact()


class Cancelled(Exception):
    pass
