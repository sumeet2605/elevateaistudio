import json
from datetime import datetime, timezone
from pathlib import Path

from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .brand import Brand
from .captions import captions_for
from .dynamic_captions import CaptionStyle, Cue, normalized
from .render_manifest import RenderSettings, milliseconds
from .transcription import Transcript

PRESETS = {
    key: {"name": entry["name"], "style": CaptionStyle.model_validate(entry["style"])}
    for key, entry in json.loads(Path(__file__).with_name("caption_presets.json").read_text()).items()
}
DEFAULT = "mystical-gold@1"


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: UUID
    preset: str
    style: CaptionStyle
    cues: list[Cue] = Field(default_factory=list, max_length=1000)
    confirm_new_words: bool = False


def preset_styles(brand: Brand):
    return {
        key: {"name": entry["name"], "style": entry["style"].model_copy(
            update={"primary": brand.primary, "highlight": brand.highlight})}
        for key, entry in PRESETS.items()
    }


def plan_cues(manifest, transcript, style):
    cues, _ = captions_for(manifest, transcript, grouping=style)
    result = []
    for cue in cues:
        words = [{"text": word.text, "start_ms": max(cue.start_ms, word.start_ms),
                  "end_ms": min(cue.end_ms, word.end_ms), "highlighted": word.highlighted}
                 for line in cue.lines for word in line]
        words = [word for word in words if word["start_ms"] < word["end_ms"]]
        if words and cue.end_ms - cue.start_ms >= 40:
            result.append(Cue(start_ms=cue.start_ms, end_ms=cue.end_ms, words=words))
    return result


def mapping(manifest):
    result = []
    offset = 0
    for span in manifest.frame_ranges:
        result.append({"source_ms": milliseconds(span.start_frame), "start_ms": milliseconds(offset),
                       "end_ms": milliseconds(offset + span.duration)})
        offset += span.duration
    return result


def caption_routes(renders):
    router = APIRouter(prefix="/projects/{project_id}/captions", tags=["captions"])

    def inputs(project_id, plan_id):
        manifest = renders.make_manifest(project_id, plan_id, RenderSettings(), include_captions=False)
        transcript = Transcript.model_validate_json(renders.transcript_path(project_id, manifest.transcript_revision).read_bytes())
        return manifest, transcript

    def checked_preset(preset):
        if preset not in PRESETS:
            raise HTTPException(422, "Unknown caption preset version.")

    def current(db, project_id, plan_id):
        row = db.execute("SELECT payload FROM caption_revisions WHERE project_id=? AND plan_id=? ORDER BY rowid DESC LIMIT 1",
                         (str(project_id), str(plan_id))).fetchone()
        return json.loads(row[0]) if row else None

    @router.get("")
    def state(project_id: UUID):
        selected = renders.state(project_id)["selected"]
        if not selected:
            return {"selected": None}
        plan_id = UUID(selected["id"])
        manifest, transcript = inputs(project_id, plan_id)
        presets = preset_styles(manifest.brand)
        with renders.projects.connect() as db:
            revisions = [json.loads(row[0]) for row in db.execute(
                "SELECT payload FROM caption_revisions WHERE project_id=? AND plan_id=? ORDER BY rowid DESC",
                (str(project_id), str(plan_id)))]
        return {
            "selected": str(plan_id), "presets": presets, "duration_ms": manifest.encoded_ms,
            "mapping": mapping(manifest), "transcript": transcript.text,
            "draft": revisions[0] if revisions else {"preset": DEFAULT, "style": presets[DEFAULT]["style"],
                "cues": plan_cues(manifest, transcript, presets[DEFAULT]["style"]), "introduced": []},
            "history": revisions,
        }

    @router.post("/group", max_body_size=1024*1024)
    def group(project_id: UUID, draft: Draft):
        checked_preset(draft.preset)
        manifest, transcript = inputs(project_id, draft.plan_id)
        return plan_cues(manifest, transcript, draft.style)

    @router.post("", status_code=201, max_body_size=1024*1024)
    def save(project_id: UUID, draft: Draft):
        checked_preset(draft.preset)
        manifest, transcript = inputs(project_id, draft.plan_id)
        ranges = mapping(manifest)
        if not draft.cues:
            raise HTTPException(422, "At least one caption cue is required.")
        end = 0
        for cue in draft.cues:
            if cue.start_ms < end or not any(r["start_ms"] <= cue.start_ms < cue.end_ms <= r["end_ms"] for r in ranges):
                raise HTTPException(422, "Cues must be ordered, non-overlapping, and stay inside one retained segment.")
            end = cue.end_ms
        known = {normalized(t) for t in transcript.text.split()}
        introduced = sorted({word.text for cue in draft.cues for word in cue.words
                             if normalized(word.text) and normalized(word.text) not in known}, key=str.casefold)
        if introduced and not draft.confirm_new_words:
            raise HTTPException(422, "Some words are absent from the transcript. Review and confirm the correction.")
        cue_json = [cue.model_dump() for cue in draft.cues]
        with renders.projects.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not renders.selected(db, project_id, draft.plan_id) or renders.stale(db, manifest, check_captions=False):
                raise HTTPException(409, "The edit changed while these captions were open. Reload the workspace.")
            parent = current(db, project_id, draft.plan_id)
            reference = parent["cues"] if parent else [
                cue.model_dump() for cue in plan_cues(manifest, transcript, draft.style)]
            def phrases(cues):
                return {f"{cue['start_ms']}–{cue['end_ms']}": " ".join(w["text"] for w in cue["words"]) for cue in cues}
            old, new = phrases(reference), phrases(cue_json)
            corrections = [{"cue": key, "before": old.get(key), "after": new.get(key)}
                           for key in sorted(old.keys() | new.keys()) if old.get(key) != new.get(key)]
            revision = {
                "id": str(uuid4()), "project_id": str(project_id), "plan_id": str(draft.plan_id),
                "parent_id": parent["id"] if parent else None, "source_media_id": manifest.source_media_id,
                "transcript_revision": str(manifest.transcript_revision),
                "created_at": datetime.now(timezone.utc).isoformat(), "preset": draft.preset,
                "style": draft.style.model_dump(), "cues": cue_json, "introduced": introduced,
                "confirmed_new_words": bool(introduced and draft.confirm_new_words), "corrections": corrections,
            }
            db.execute("INSERT INTO caption_revisions VALUES (?, ?, ?, ?)",
                       (revision["id"], str(project_id), str(draft.plan_id), json.dumps(revision, ensure_ascii=False)))
        return revision

    return router
