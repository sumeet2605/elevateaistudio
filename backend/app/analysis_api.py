from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

from .analysis_store import ANALYSIS
from .silence import DetectionSettings, EditingPlan, PRESETS, Review


def analysis_routes(store):
    router = APIRouter(prefix="/projects/{project_id}/analysis", tags=["audio analysis"])

    @router.get("")
    def state(project_id: UUID):
        return {**store.state(project_id), "presets": PRESETS}

    @router.post("", status_code=202)
    @router.post("/regenerate", status_code=202)
    def start(project_id: UUID):
        return store.enqueue(project_id)

    @router.post("/settings", max_body_size=8192)
    def settings(project_id: UUID, configuration: DetectionSettings):
        return store.save_configuration(project_id, configuration)

    @router.get("/jobs/{job_id}")
    def job(project_id: UUID, job_id: UUID):
        return store.jobs.get(project_id, job_id, ANALYSIS)

    @router.post("/jobs/{job_id}/cancel")
    def cancel(project_id: UUID, job_id: UUID):
        return store.jobs.cancel(project_id, job_id, ANALYSIS)

    @router.post("/jobs/{job_id}/retry", status_code=202)
    def retry(project_id: UUID, job_id: UUID):
        store.jobs.get(project_id, job_id, ANALYSIS)
        return store.enqueue(project_id, retry=job_id)

    @router.get("/plans")
    def history(project_id: UUID):
        return store.history(project_id)

    @router.get("/silence")
    @router.get("/plan")
    def plan(project_id: UUID, plan_id: UUID | None = None):
        return EditingPlan.model_validate_json(store.plan_row(project_id, plan_id)["payload"])

    @router.get("/waveform")
    def waveform(project_id: UUID, plan_id: UUID | None = None):
        row = store.plan_row(project_id, plan_id)
        path = store.waveform_path(project_id, row["waveform_job_id"])
        if not path.is_file():
            raise HTTPException(404, "The waveform artifact is missing.")
        return FileResponse(path, media_type="application/json")

    @router.post("/plans/{plan_id}/suggestions/{suggestion_id}", max_body_size=8192)
    def review(project_id: UUID, plan_id: UUID, suggestion_id: UUID, decision: Review):
        return store.review(project_id, plan_id, suggestion_id, decision)

    @router.post("/plans/{plan_id}/accept-high")
    def accept_high(project_id: UUID, plan_id: UUID):
        return store.review(project_id, plan_id)

    @router.get("/export")
    def export(project_id: UUID, plan_id: UUID | None = None):
        row = store.plan_row(project_id, plan_id)
        return Response(row["payload"], media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="editing-plan-{row["id"]}.json"'})

    return router
