from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from .jobs import Jobs
from .projects import Projects


def transcription_routes(projects: Projects, jobs: Jobs):
    router = APIRouter(prefix="/projects/{project_id}", tags=["transcription"])

    @router.get("/transcription")
    def state(project_id: UUID):
        return jobs.state(project_id)

    @router.post("/transcription", status_code=202)
    def start(project_id: UUID):
        return jobs.enqueue(project_id)

    @router.get("/transcription/jobs/{job_id}")
    def get_job(project_id: UUID, job_id: UUID):
        return jobs.get(project_id, job_id)

    @router.post("/transcription/jobs/{job_id}/cancel")
    def cancel(project_id: UUID, job_id: UUID):
        return jobs.cancel(project_id, job_id)

    @router.post("/transcription/jobs/{job_id}/retry", status_code=202)
    def retry(project_id: UUID, job_id: UUID):
        jobs.get(project_id, job_id)
        return jobs.enqueue(project_id, retry=job_id)

    @router.get("/transcript")
    def completed_transcript(project_id: UUID, job_id: UUID | None = None):
        if job_id is None:
            latest = jobs.state(project_id)["transcript_job_id"]
            if not latest:
                raise HTTPException(404, "No completed transcript is available.")
            job_id = UUID(latest)

        with projects.connect() as db:
            record = db.execute(
                "SELECT job_id FROM transcripts WHERE project_id=? AND job_id=?",
                (str(project_id), str(job_id)),
            ).fetchone()
        if record is None:
            raise HTTPException(404, "Transcript not found.")

        directory = projects.media_dir(project_id).resolve()
        path = (directory / f"transcription-{job_id}.json").resolve()
        if path.parent != directory or not path.is_file():
            raise HTTPException(404, "The transcript file is missing from local storage.")
        return FileResponse(path, media_type="application/json",
                            headers={"X-Content-Type-Options": "nosniff"})

    return router
