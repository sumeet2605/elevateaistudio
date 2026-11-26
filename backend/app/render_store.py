import hashlib
import json
import math
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException
from pydantic import ValidationError

from .director import DirectorPlan
from .asset_library import initialize_visuals
from .sound_store import asset_row as sound_asset_row, initialize_sound, pinned_sound, sound_path
from .sound_mix import Rights
from .finish_store import initialize as initialize_finish, pinned as pinned_finish
from .director_store import DirectorStore, reviewed_plan
from .render_manifest import RenderManifest, RenderSettings, Zoom, frame_spans
from .timeline import Span
from .motion_store import MotionStore, manifest_motion, suggested_events

RENDER = "render"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class Renders:
    def __init__(self, jobs):
        self.jobs = jobs
        self.projects = jobs.projects

    def initialize(self):
        MotionStore(self.jobs).initialize()
        with self.projects.connect() as db:
            initialize_finish(db)
            initialize_visuals(db)
            initialize_sound(db)
            db.executescript("""
                CREATE TABLE IF NOT EXISTS caption_revisions (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    plan_id TEXT NOT NULL REFERENCES director_plans(id), payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS caption_plan ON caption_revisions(project_id, plan_id);
                CREATE TABLE IF NOT EXISTS render_inputs (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(id), manifest TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS renders (
                    id TEXT PRIMARY KEY REFERENCES jobs(id),
                    project_id TEXT NOT NULL REFERENCES projects(id), metadata TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS renders_project ON renders(project_id);
                CREATE TABLE IF NOT EXISTS render_reviews (
                    render_id TEXT NOT NULL REFERENCES renders(id), decision TEXT NOT NULL
                        CHECK (decision IN ('approved','rejected')),
                    plan_fingerprint TEXT NOT NULL, video_sha256 TEXT NOT NULL, created_at TEXT NOT NULL
                );
            """)

    def transcript_path(self, project_id, revision):
        directory = self.projects.media_dir(project_id).resolve()
        path = (directory / f"transcription-{UUID(str(revision))}.json").resolve()
        if path.parent != directory or not path.is_file():
            raise HTTPException(409, "The pinned transcript file is missing.")
        return path

    def selected(self, db, project_id, plan_id=None):
        row = db.execute("""
            SELECT p.payload FROM director_current c JOIN director_plans p ON p.id=c.plan_id
            WHERE c.project_id=? AND p.project_id=c.project_id
        """, (str(project_id),)).fetchone()
        if row is None:
            return None
        plan = json.loads(row[0])
        if plan_id and plan["id"] != str(plan_id):
            return None
        return row[0], plan

    def revision_issue(self, db, project_id, plan):
        if DirectorStore(self.jobs).stale(db, project_id, plan):
            return "Silence decisions changed. Generate and review a fresh director plan."
        transcript = db.execute("SELECT job_id FROM transcripts WHERE project_id=? ORDER BY rowid DESC LIMIT 1",
                                (str(project_id),)).fetchone()
        if not transcript or transcript[0] != str(plan["transcript_revision"]):
            return "The transcript revision changed. Generate and review a fresh director plan."
        brand = db.execute("SELECT id FROM brand_revisions WHERE project_id=? ORDER BY rowid DESC LIMIT 1",
                           (str(project_id),)).fetchone()
        if (brand[0] if brand else "default-v1") != plan["brand_revision"]:
            return "The brand revision changed. Generate and review a fresh director plan."
        source = db.execute("SELECT media FROM projects WHERE id=?", (str(project_id),)).fetchone()
        media = json.loads(source[0]) if source and source[0] else None
        if not media or media["sha256"] != plan["source_media_id"] or math.floor(
                media["duration_seconds"] * 1000) != plan["source_duration_ms"]:
            return "The original source no longer matches this plan."
        return None

    def make_manifest(self, project_id, plan_id, settings, *, include_captions=True, include_motion=True, include_visuals=True, include_sound=True):
        from .visual_store import latest as latest_visual, visual_manifest
        with self.projects.connect() as db:
            db.execute("BEGIN")
            selected = self.selected(db, project_id, plan_id)
            if not selected:
                raise HTTPException(409, "Select or restore the director revision before rendering it.")
            raw, plan = selected
            issue = self.revision_issue(db, project_id, plan)
            caption = self.latest_caption(db, project_id, plan_id) if include_captions else None
            motion = MotionStore.latest(db, project_id, plan["id"]) if include_motion else None
            finishing = pinned_finish(db, project_id, plan["id"])
            sound = pinned_sound(db,project_id,plan["id"]) if include_sound else {}
            if sound:
                mix = sound["mix"]
                from .sound_mix import Mix
                if Mix.model_validate(mix).active:
                    raw_sound = db.execute("SELECT payload FROM sound_revisions WHERE id=?",(sound["mix_revision"],)).fetchone()[0]
                    if json.loads(raw_sound)["speed"] != settings.playback_speed or json.loads(raw_sound)["voice_revision"] != finishing.get("voice_revision"):
                        raise HTTPException(409,"Review and save the mix for this speed and voice revision.")
            row_visual = latest_visual(db, project_id, plan["id"]) if include_visuals else None
            visual = visual_manifest(row_visual)
            if row_visual and json.loads(row_visual["payload"])["speed"] != settings.playback_speed and visual["visuals"]:
                raise HTTPException(409, "Save the B-roll revision at the selected playback speed.")
        if issue:
            raise HTTPException(409, issue)
        try:
            DirectorPlan.model_validate(plan)
            reviewed = reviewed_plan(plan)
        except ValueError as error:
            raise HTTPException(422, "The selected director plan failed render validation.") from error
        if plan["event"] == "generation" or (plan["operations"] and not any(
                op["status"] in ("accepted", "rejected") for op in plan["operations"])):
            raise HTTPException(409, "Review at least one director recommendation before rendering.")
        notices = (["The saved caption revision uses bundled Noto Sans and Noto Sans Devanagari."] if caption
                   else ["Captions use bundled Noto Sans Bold with Noto Sans Devanagari Bold for Hindi."])
        if any(op["status"] == "pending" for op in plan["operations"]):
            notices.append("Pending recommendations are outside the current edit.")
        if reviewed["brand"]["background_music"] != "none":
            notices.append("The brand’s optional music preference is deferred for this preview.")
        highlights = []
        zooms = []
        generated_motion = False
        for operation in reviewed["reviewed_events"]:
            kind = operation["kind"]
            if kind == "emphasis":
                highlights.append(Span(start_ms=operation["start_ms"], end_ms=operation["end_ms"]))
            elif kind == "zoom":
                if not motion and operation.get("zoom_motion") is None and operation["zoom_percent"] > 100:
                    zooms.append(Zoom(source=Span(start_ms=operation["start_ms"], end_ms=operation["end_ms"]),
                                      percent=operation["zoom_percent"]))
                elif not motion:
                    generated_motion = True
            elif kind == "hook":
                notices.append("The accepted hook is advisory. The source order is preserved.")
            elif kind == "b_roll":
                notices.append("The director idea is advisory. Review the cutaway in B-roll & Visuals.")
            elif kind == "audio" and operation["idea"] == "keep natural" and reviewed["brand"]["audio"] == "natural":
                continue
            else:
                raise HTTPException(422, f"The accepted {kind.replace('_', ' ')} operation is unsupported in Day 5. Review it again before rendering.")
        retained = [Span.model_validate(span) for span in reviewed["retained_segments"]]
        try:
            camera = {"zooms": zooms}
            if generated_motion:
                camera = {"camera": {"energy": "energetic"}, "motion_events": suggested_events(plan)}
            if motion:
                camera = manifest_motion(motion["id"], motion["payload"])
            return RenderManifest(
                project_id=project_id, plan_id=plan_id, plan_fingerprint=hashlib.sha256(raw.encode()).hexdigest(),
                caption_revision=caption["id"] if caption else None,
                caption_fingerprint=hashlib.sha256(caption["payload"].encode()).hexdigest() if caption else None,
                source_media_id=plan["source_media_id"], transcript_revision=plan["transcript_revision"],
                transcript_fingerprint=digest(self.transcript_path(project_id, plan["transcript_revision"])),
                brand_revision=plan["brand_revision"], playbook_revision=plan["playbook_revision"],
                brand=plan["brand"], settings=settings, source_duration_ms=plan["source_duration_ms"],
                cuts=reviewed["approved_cuts"], retained=retained, frame_ranges=frame_spans(retained),
                emphasis=highlights, **camera, **finishing, **visual, **sound, voice_leveling=plan["brand"]["audio"] == "voice-leveling",
                notices=notices,
            )
        except ValidationError as error:
            raise HTTPException(422, error.errors()[0]["msg"]) from error

    def manifest(self, job_id, db=None):
        if db is None:
            with self.projects.connect() as conn:
                return self.manifest(job_id, conn)
        row = db.execute("SELECT manifest FROM render_inputs WHERE job_id=?", (str(job_id),)).fetchone()
        if row is None:
            raise HTTPException(404, "Render manifest not found.")
        return RenderManifest.model_validate_json(row[0])

    def latest_caption(self, db, project_id, plan_id):
        return db.execute(
            "SELECT id,payload FROM caption_revisions WHERE project_id=? AND plan_id=? ORDER BY rowid DESC LIMIT 1",
            (str(project_id), str(plan_id)),
        ).fetchone()

    def pinned_caption(self, manifest):
        with self.projects.connect() as db:
            row = db.execute("SELECT payload FROM caption_revisions WHERE id=? AND project_id=? AND plan_id=?",
                             (str(manifest.caption_revision), str(manifest.project_id), str(manifest.plan_id))).fetchone()
        if row is None or hashlib.sha256(row[0].encode()).hexdigest() != manifest.caption_fingerprint:
            raise ValueError("The pinned caption revision changed or is missing.")
        return json.loads(row[0])

    def stale(self, db, manifest, *, check_captions=True, check_motion=True, check_visuals=True, check_sound=True):
        from .visual_store import latest as latest_visual, visual_manifest
        selected = self.selected(db, manifest.project_id, manifest.plan_id)
        if not selected or hashlib.sha256(selected[0].encode()).hexdigest() != manifest.plan_fingerprint:
            return "The selected editing-plan revision changed. Generate a new preview."
        if check_captions:
            caption = self.latest_caption(db, manifest.project_id, manifest.plan_id)
            observed_id = caption["id"] if caption else None
            if observed_id != (str(manifest.caption_revision) if manifest.caption_revision else None) or (
                caption and hashlib.sha256(caption["payload"].encode()).hexdigest() != manifest.caption_fingerprint
            ):
                return "Captions changed. Generate and approve a new preview."
        if check_motion:
            motion = MotionStore.latest(db, manifest.project_id, manifest.plan_id)
            if (motion["id"] if motion else None) != (str(manifest.motion_revision) if manifest.motion_revision else None) or (
                motion and hashlib.sha256(motion["payload"].encode()).hexdigest() != manifest.motion_fingerprint
            ):
                return "Camera motion changed. Generate and approve a new preview."
        finishing = pinned_finish(db, manifest.project_id, manifest.plan_id)
        for kind in ("grade", "voice"):
            revision = getattr(manifest, f"{kind}_revision")
            if (str(revision) if revision else None) != finishing.get(f"{kind}_revision") or (
                getattr(manifest, f"{kind}_fingerprint") != finishing.get(f"{kind}_fingerprint")
            ):
                return "Colour or sound changed. Generate and approve a new preview."
        if check_sound and pinned_sound(db,manifest.project_id,manifest.plan_id).get("mix_fingerprint") != manifest.mix_fingerprint:
            return "Sound mix changed. Generate and approve a new preview."
        if check_visuals:
            current = visual_manifest(latest_visual(db, manifest.project_id, manifest.plan_id))
            if current.get("visual_fingerprint") != manifest.visual_fingerprint:
                return "B-roll changed. Generate and approve a new preview."
        issue = self.revision_issue(db, manifest.project_id, selected[1])
        if issue:
            return issue
        try:
            same = digest(self.transcript_path(manifest.project_id, manifest.transcript_revision)) == manifest.transcript_fingerprint
        except (OSError, HTTPException):
            same = False
        return None if same else "The pinned transcript changed. Generate a new preview."

    def enqueue(self, project_id, plan_id=None, settings=None, retry=None):
        if retry:
            self.jobs.get(project_id, retry, RENDER)
            manifest = self.manifest(retry)
            with self.projects.connect() as db:
                issue = self.stale(db, manifest)
            if issue:
                raise HTTPException(409, issue)
        else:
            manifest = self.make_manifest(project_id, plan_id, settings or RenderSettings())

        def prepare(db, job_id):
            issue = self.stale(db, manifest)
            if issue:
                raise HTTPException(409, issue)
            db.execute("INSERT INTO render_inputs VALUES (?, ?)", (str(job_id), manifest.model_dump_json()))
        return self.jobs.enqueue(project_id, retry, kind=RENDER, prepare=prepare)

    def directory(self, project_id, render_id):
        return self.projects.media_dir(project_id) / "renders" / str(UUID(str(render_id)))

    def files(self, project_id, render_id, name):
        root = (self.projects.media_dir(project_id) / "renders").resolve()
        directory = self.directory(project_id, render_id).resolve()
        path = (directory / name).resolve()
        if directory.parent != root or path.parent != directory or not path.is_file():
            raise HTTPException(404, "The render artifact is missing.")
        return path

    def publish(self, manifest, render_id, metadata, work, final):
        def publish(db):
            work.rename(final)
            db.execute("INSERT INTO renders VALUES (?, ?, ?)",
                       (str(render_id), str(manifest.project_id), json.dumps(metadata)))
        self.jobs.complete(str(render_id), publish)

    def detail(self, project_id, render_id, db):
        row = db.execute("SELECT metadata FROM renders WHERE id=? AND project_id=?",
                         (str(render_id), str(project_id))).fetchone()
        if row is None:
            raise HTTPException(404, "Completed render not found.")
        manifest = self.manifest(render_id, db)
        metadata = json.loads(row[0])
        latest = db.execute("""
            SELECT decision, created_at, plan_fingerprint, video_sha256 FROM render_reviews
            WHERE render_id=? ORDER BY rowid DESC LIMIT 1
        """, (str(render_id),)).fetchone()
        return {"id": str(render_id), "manifest": manifest, "metadata": metadata,
                "decision": latest["decision"] if latest else None,
                "reviewed_at": latest["created_at"] if latest else None,
                "bound_approval": bool(latest and latest["decision"] == "approved"
                                       and latest["plan_fingerprint"] == manifest.approval_fingerprint
                                       and latest["video_sha256"] == metadata["sha256"]),
                "stale_reason": self.stale(db, manifest)}

    def state(self, project_id):
        self.projects.get(project_id)
        with self.projects.connect() as db:
            db.execute("BEGIN")
            row = self.selected(db, project_id)
            selected = None
            if row:
                plan = row[1]
                sound = pinned_sound(db,project_id,plan["id"])
                mix_info = (json.loads(db.execute("SELECT payload FROM sound_revisions WHERE id=?",
                                                 (sound["mix_revision"],)).fetchone()[0]) if sound else None)
                selected = {"id": plan["id"], "mix_speed":mix_info["speed"] if mix_info else None, "brand": plan["brand"], "brand_revision": plan["brand_revision"],
                            "playbook_revision": plan["playbook_revision"],
                            "issue": self.revision_issue(db, project_id, plan)}
            revisions = db.execute("SELECT id FROM renders WHERE project_id=? ORDER BY rowid DESC",
                                   (str(project_id),)).fetchall()
            job = self.jobs.public(self.jobs.latest(db, project_id, RENDER))
            return {"selected": selected, "job": job,
                    "history": [self.detail(project_id, record[0], db) for record in revisions]}

    def review(self, project_id, render_id, decision):
        with self.projects.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            item = self.detail(project_id, render_id, db)
            if decision == "approved":
                self.validate_download(item, require_approval=False)
            db.execute("INSERT INTO render_reviews VALUES (?, ?, ?, ?, ?)",
                       (str(render_id), decision, item["manifest"].approval_fingerprint, item["metadata"]["sha256"],
                        datetime.now(timezone.utc).isoformat()))
        return self.state(project_id)

    def validate_download(self, item, *, require_approval=True):
        if item["stale_reason"]:
            raise HTTPException(409, item["stale_reason"])
        if require_approval and not item["bound_approval"]:
            raise HTTPException(409, "Approve this exact preview before downloading the final MP4.")
        manifest = item["manifest"]
        if manifest.mix and any(not entry.asset.rights.permits(manifest.mix.platform,manifest.mix.commercial)
                                for entry in manifest.sound_cues):
            raise HTTPException(409,"Audio rights do not cover this export. Confirm platform and commercial rights for every audible asset.")
        if manifest.mix and manifest.sound_cues:
            with self.projects.connect() as db:
                for entry in manifest.sound_cues:
                    asset = entry.asset
                    current = sound_asset_row(db,asset.id)
                    if (not Rights.model_validate(current["rights"]).permits(manifest.mix.platform,manifest.mix.commercial)
                            or digest(sound_path(self.projects,asset)) != asset.checksum):
                        raise HTTPException(409,"The audio asset rights or checksum have changed.")
        media = self.projects.get(manifest.project_id)["media"]
        try:
            source = self.projects.source_path(manifest.project_id, media)
            unchanged = digest(source) == manifest.source_media_id
        except (OSError, TypeError, ValueError):
            unchanged = False
        if not unchanged:
            raise HTTPException(409, "The original file changed. The previous approval is invalid.")
        path = self.files(manifest.project_id, item["id"], "preview.mp4")
        if digest(path) != item["metadata"]["sha256"]:
            raise HTTPException(409, "The render bytes changed. Generate and approve a new preview.")
        return path
