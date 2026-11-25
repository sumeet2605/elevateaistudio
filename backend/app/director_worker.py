from .director import (PROMPT_VERSION, DirectorError, DirectorSettings, Recommendation, mock_response,
                       provider, usage_record, validate_recommendation)
from .director_store import DirectorStore, now


def direct(jobs, job, stage):
    store = DirectorStore(jobs)
    job_id = job["id"]
    snapshot, context, refs, estimate = store.record(job_id)
    if refs["prompt_version"] != PROMPT_VERSION:
        raise DirectorError("This queued job uses an older prompt. Generate a fresh draft.")
    stage("requesting_director")
    if snapshot["mode"] == "mock":
        raw, usage = mock_response(context), None
    else:
        raw, usage = provider(snapshot, context, DirectorSettings.from_env().key)
    cost = usage_record(snapshot, estimate, usage)
    store.usage(job_id, cost)
    stage("validating_director")
    if raw is None:
        raise DirectorError("The model declined or did not finish the structured response.")
    rationale, operations, proposed = validate_recommendation(raw, context)
    plan = {
        **refs, "id": job_id, "project_id": job["project_id"], "created_at": now(),
        "schema_version": 1, "parent_id": None, "event": "generation",
        "model": snapshot["model"], "mode": snapshot["mode"], "rationale": rationale,
        "source_duration_ms": context["duration_ms"], "base_approved_cuts": context["approved_cuts"],
        "brand": context["brand"], "playbook": context["playbook"],
        "proposed_retained_segments": [span.model_dump() for span in proposed.retained],
        "validation": {"status": "passed", "source_clock": "integer-ms-half-open",
                       "crossing_events": "rejected", "base_approvals_protected": True},
        "operations": [{**operation, "status": "pending"} for operation in operations],
        "usage": cost,
        "finishing": Recommendation.model_validate_json(raw).finishing,
    }
    stage("saving_director")
    jobs.complete(job_id, lambda db: store.write(db, plan))
