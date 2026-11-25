from typing import Literal
from uuid import UUID

from fastapi import APIRouter
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict

from .render_manifest import RenderSettings
from .render_store import RENDER


class Preview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: UUID
    settings: RenderSettings


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approved", "rejected"]


def render_routes(store):
    router = APIRouter(prefix="/projects/{project_id}/render", tags=["rendering"])

    @router.get("")
    def state(project_id: UUID):
        return store.state(project_id)

    @router.post("", status_code=202, max_body_size=8192)
    def enqueue(project_id: UUID, request: Preview):
        return store.enqueue(project_id, request.plan_id, request.settings)

    @router.get("/jobs/{job_id}")
    def job(project_id: UUID, job_id: UUID):
        return store.jobs.get(project_id, job_id, RENDER)

    @router.post("/jobs/{job_id}/cancel")
    def cancel(project_id: UUID, job_id: UUID):
        return store.jobs.cancel(project_id, job_id, RENDER)

    @router.post("/jobs/{job_id}/retry", status_code=202)
    def retry(project_id: UUID, job_id: UUID):
        return store.enqueue(project_id, retry=job_id)

    @router.post("/{render_id}/review", max_body_size=8192)
    def review(project_id: UUID, render_id: UUID, request: Review):
        return store.review(project_id, render_id, request.decision)

    @router.get("/{render_id}/preview")
    def preview(project_id: UUID, render_id: UUID):
        store.jobs.get(project_id, render_id, RENDER)
        with store.projects.connect() as db:
            store.detail(project_id, render_id, db)
        return FileResponse(store.files(project_id, render_id, "preview.mp4"), media_type="video/mp4",
                            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})

    @router.get("/{render_id}/final")
    def final(project_id: UUID, render_id: UUID):
        with store.projects.connect() as db:
            db.execute("BEGIN")
            item = store.detail(project_id, render_id, db)
            path = store.validate_download(item)
        return FileResponse(path, filename=f"elevate-{render_id}.mp4", media_type="video/mp4",
                            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})

    return router
