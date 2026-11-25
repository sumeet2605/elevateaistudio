import json
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

from .brand import Brand
from .director_store import DIRECTOR, reviewed_plan
from .timeline import Span


class Generation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    playbook: str
    confirm_paid: bool = False


class Consent(BaseModel):
    confirm_paid: bool = False


class Decision(BaseModel):
    status: Literal["pending", "accepted", "rejected"]


def director_routes(store):
    router = APIRouter(prefix="/projects/{project_id}", tags=["brand and director"])

    @router.get("/brand")
    def brands(project_id: UUID):
        return store.brands(project_id)

    @router.post("/brand", status_code=201, max_body_size=8192)
    def save_brand(project_id: UUID, settings: Brand):
        return store.save_brand(project_id, settings)

    @router.post("/brand/{revision}/restore", status_code=201)
    def restore_brand(project_id: UUID, revision: str):
        entry = store.brand_revision(project_id, revision)
        if entry is None:
            raise HTTPException(404, "Brand revision not found.")
        return store.save_brand(project_id, Brand.model_validate(entry["settings"]))

    @router.get("/director")
    def state(project_id: UUID):
        return store.state(project_id)

    @router.get("/director/estimate")
    def estimate(project_id: UUID, playbook: str):
        snapshot, _, _, estimate = store.inputs(project_id, playbook)
        return {"estimated_input_tokens": estimate, "mode": snapshot["mode"], "model": snapshot["model"]}

    @router.post("/director", status_code=202, max_body_size=8192)
    def generate(project_id: UUID, request: Generation):
        return store.enqueue(project_id, request.playbook, request.confirm_paid)

    @router.post("/director/jobs/{job_id}/cancel")
    def cancel(project_id: UUID, job_id: UUID):
        return store.jobs.cancel(project_id, job_id, DIRECTOR)

    @router.post("/director/jobs/{job_id}/retry", status_code=202, max_body_size=8192)
    def retry(project_id: UUID, job_id: UUID, consent: Consent):
        return store.enqueue(project_id, None, consent.confirm_paid, retry=job_id)

    @router.get("/director/revisions")
    def history(project_id: UUID):
        return store.history(project_id)

    @router.get("/director/revisions/{revision}")
    def plan(project_id: UUID, revision: UUID):
        return reviewed_plan(store.get(project_id, revision))

    @router.post("/director/revisions/{revision}/restore", status_code=201)
    def restore(project_id: UUID, revision: UUID):
        return reviewed_plan(store.revise(project_id, revision))

    @router.post("/director/revisions/{revision}/operations/{index}", status_code=201, max_body_size=8192)
    def review(project_id: UUID, revision: UUID, index: int, decision: Decision):
        return reviewed_plan(store.revise(project_id, revision, index, decision.status))

    @router.post("/director/revisions/{revision}/cuts", status_code=201, max_body_size=8192)
    def story_cut(project_id: UUID, revision: UUID, cut: Span):
        return reviewed_plan(store.revise(project_id, revision, manual_cut=cut))

    @router.get("/director/revisions/{revision}/export")
    def export(project_id: UUID, revision: UUID):
        data = plan(project_id, revision)
        if data["stale_silence"]:
            raise HTTPException(409, "Silence decisions changed. Generate a new director draft before exporting.")
        # Exported decisions include review status; only reviewed_events and approved_cuts are actionable.
        return Response(json.dumps(data, indent=2), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="director-{revision}.json"'})

    return router
