import hashlib
import json
import math
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import HTTPException

from .analysis_store import AnalysisStore
from .retakes import Candidate, Decision, Options, detect, overlap, safe_cut, timed_words
from .silence import EditingPlan
from .timeline import Span, Timeline, merge_spans
from .transcription import Transcript

RETAKE = "retake-analysis"


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def current_review(db, project_id):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='retake_reviews'").fetchone():
        return None
    row = db.execute("SELECT payload FROM retake_reviews WHERE project_id=? ORDER BY rowid DESC LIMIT 1",
                     (str(project_id),)).fetchone()
    return json.loads(row[0]) if row else None


class Retakes:
    def __init__(self, jobs):
        self.jobs = jobs
        self.projects = jobs.projects
        self.analysis = AnalysisStore(jobs)

    def initialize(self):
        with self.projects.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS retake_inputs (
                    id TEXT PRIMARY KEY REFERENCES jobs(id), payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS retake_results (
                    id TEXT PRIMARY KEY REFERENCES jobs(id), payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS retake_reviews (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS retake_project ON retake_reviews(project_id);
            """)

    def context(self, db, project_id, media):
        row = self.analysis.latest_row(db, project_id)
        transcript = db.execute("SELECT job_id FROM transcripts WHERE project_id=? ORDER BY rowid DESC LIMIT 1",
                                (str(project_id),)).fetchone()
        if not row or not transcript:
            raise HTTPException(409,"Generate the transcript and run Silence Detection first.")
        silence = EditingPlan.model_validate_json(row["payload"])
        if silence.source_media_id != media["sha256"] or str(silence.transcript_version)!=transcript[0]:
            raise HTTPException(409,"Refresh Silence Detection using the latest transcript.")
        current = db.execute("""SELECT p.payload FROM director_current c JOIN director_plans p ON p.id=c.plan_id
                             WHERE c.project_id=?""",(str(project_id),)).fetchone()
        plan = json.loads(current[0]) if current else {}
        locked = [Span.model_validate(s) for s in plan.get("locked_story_cuts", [])]
        existing = silence.approved_cuts + locked + [
            Span(start_ms=op["start_ms"],end_ms=op["end_ms"])
            for op in plan.get("operations",[]) if op["kind"]=="cut" and op["status"]=="accepted"
            and plan.get("source_media_id")==media["sha256"]
        ]
        path=self.projects.media_dir(project_id)/f"transcription-{UUID(transcript[0])}.json"
        try:
            from .render_store import digest
            transcript_hash=digest(path)
        except OSError as exc:
            raise HTTPException(409,"The word-timed transcript is missing.") from exc
        return {"source_id":media["sha256"],"transcript_revision":transcript[0],"transcript_hash":transcript_hash,
                "base_edit_revision":row["id"],"base_hash":fingerprint(row["payload"]),
                "waveform_job":row["waveform_job_id"], "duration":silence.source_duration_ms,
                "existing":[s.model_dump() for s in merge_spans(existing,silence.source_duration_ms)]}

    def transcript(self, project_id, snapshot):
        path=self.projects.media_dir(project_id)/f"transcription-{UUID(snapshot['transcript_revision'])}.json"
        try:
            raw=path.read_bytes()
        except OSError as exc:
            raise HTTPException(409,"The word-timed transcript is missing.") from exc
        if hashlib.sha256(raw).hexdigest()!=snapshot["transcript_hash"]:
            raise HTTPException(409,"The transcript contents changed. Run a fresh analysis.")
        transcript=Transcript.model_validate_json(raw)
        if transcript.project_id!=project_id or transcript.source_media_id!=snapshot["source_id"]:
            raise ValueError("Transcript does not match the source.")
        return transcript

    def verified_media(self, project_id):
        media=self.projects.get(project_id)["media"]
        if not media:
            raise HTTPException(409,"Upload an original first.")
        from .render_store import digest
        if digest(self.projects.source_path(project_id,media))!=media["sha256"]:
            raise HTTPException(409,"Original source checksum changed.")
        return media

    def enqueue(self, project_id, options, retry=None):
        media=self.verified_media(project_id)
        def prepare(db, job_id):
            snapshot=self.context(db,project_id,media)
            if retry:
                prior=self.input(db,retry)
                if snapshot!={key:prior[key] for key in snapshot}:
                    raise HTTPException(409,"Inputs changed. Run a new analysis.")
                snapshot=prior
            else:
                snapshot["settings"]=options.model_dump()
            db.execute("INSERT INTO retake_inputs VALUES (?,?)",(str(job_id),json.dumps(snapshot)))
        return self.jobs.enqueue(project_id,retry,kind=RETAKE,prepare=prepare)

    def input(self, db, job_id):
        row=db.execute("SELECT payload FROM retake_inputs WHERE id=?",(str(job_id),)).fetchone()
        if not row:
            raise HTTPException(404,"Analysis snapshot not found.")
        return json.loads(row[0])

    def process(self, job, stage):
        project_id=UUID(job["project_id"])
        with self.projects.connect() as db:
            snapshot=self.input(db,job["id"])
        stage("finding_retakes",.1)
        transcript=self.transcript(project_id,snapshot)
        words=timed_words(transcript)
        vad,untimed=self.speech_context(project_id,snapshot,transcript)
        existing=[Span.model_validate(s) for s in snapshot["existing"]]
        candidates=detect(words,snapshot["source_id"],snapshot["transcript_revision"],snapshot["base_edit_revision"],
                          existing,lambda:stage("finding_retakes",.3))
        options=Options.model_validate(snapshot["settings"])
        for i,candidate in enumerate(candidates):
            if candidate.recommendation not in ("first","second"):
                continue
            try:
                safe_cut(candidate,Decision(selected=candidate.recommendation),words,snapshot["duration"],vad,existing,options,untimed)
            except ValueError:
                candidates[i]=candidate.model_copy(update={"recommendation":"manual","confidence":"ambiguous",
                                                           "reasons":[*candidate.reasons,"proposed join needs a safe boundary"]})
        payload={**snapshot,"candidates":[item.model_dump(mode="json") for item in candidates]}
        stage("saving_retakes",.9)
        self.jobs.complete(job["id"],lambda db:db.execute("INSERT INTO retake_results VALUES (?,?)",
                                                         (job["id"],json.dumps(payload))))

    def result(self, db, project_id, result_id=None):
        row=db.execute("""SELECT r.id,r.payload FROM retake_results r JOIN jobs j ON j.id=r.id
                       WHERE j.project_id=? AND (? IS NULL OR r.id=?) ORDER BY j.rowid DESC LIMIT 1""",
                       (str(project_id),result_id,result_id)).fetchone()
        return (row["id"],json.loads(row["payload"])) if row else (None,None)

    def state(self, project_id):
        media=self.projects.get(project_id)["media"]
        with self.projects.connect() as db:
            job=self.jobs.public(self.jobs.latest(db,project_id,RETAKE))
            result_id,result=self.result(db,project_id)
            current=current_review(db,project_id)
            history=[{key:item[key] for key in ("id","created_at","accepted_cuts")}
                     for row in db.execute("SELECT payload FROM retake_reviews WHERE project_id=? ORDER BY rowid DESC",
                                           (str(project_id),)) for item in [json.loads(row[0])]]
            valid=False
            if result and media:
                try:
                    snapshot=self.context(db,project_id,media)
                    valid=snapshot=={key:result[key] for key in snapshot}
                except HTTPException:
                    pass
        previous={item["id"]:item for item in current["reviewed_candidates"]} if current else {}
        return {"job":job,"result_id":result_id,"stale":bool(result and not valid),"current":current,
                "history":history,"candidates":[previous.get(item["id"],item) for item in result["candidates"]] if result else []}

    def evidence(self, db, project_id, result_id, candidate_id, media):
        current_id,result=self.result(db,project_id)
        if current_id!=result_id:
            raise HTTPException(409,"Use the latest retake analysis.")
        live=self.context(db,project_id,media)
        if not result or live!={key:result[key] for key in live}:
            raise HTTPException(409,"The source, transcript or edits changed. Run a new analysis.")
        item=next((item for item in result["candidates"] if item["id"]==candidate_id),None)
        if not item:
            raise HTTPException(404,"Candidate not found.")
        return result,Candidate.model_validate(item)

    def speech_context(self, project_id, snapshot, transcript):
        try:
            evidence=json.loads(self.analysis.waveform_path(project_id,snapshot["waveform_job"]).read_text())
        except (OSError,ValueError) as exc:
            raise HTTPException(409,"Speech-boundary evidence is unavailable. Rerun Silence Detection.") from exc
        vad=[Span.model_validate(s) for s in evidence.get("speech_intervals",[])]
        unindexed=[Span(start_ms=math.floor(s.start*1000),end_ms=math.ceil(s.end*1000))
                   for s in transcript.segments if not any(w.end>w.start for w in s.words) and s.end>s.start]
        return vad,unindexed

    def cut_for(self, project_id, snapshot, candidate, decision, others):
        transcript=self.transcript(project_id,snapshot)
        words=timed_words(transcript)
        vad,unindexed=self.speech_context(project_id,snapshot,transcript)
        existing=[Span.model_validate(s) for s in snapshot["existing"]]+[item.cut for item in others if item.cut]
        try:
            cut=safe_cut(candidate,decision,words,snapshot["duration"],vad,existing,
                         Options.model_validate(snapshot["settings"]),unindexed)
            if cut:
                keep=candidate.earlier if decision.selected=="first" else candidate.later
                if any(overlap(keep.source,span) for span in existing):
                    raise ValueError("The chosen take overlaps an earlier accepted removal. Review the conflicting decision first.")
                for prior in others:
                    if prior.status!="accepted":
                        continue
                    protected=(prior.earlier,prior.later) if prior.selected=="both" else (
                        prior.earlier if prior.selected=="first" else prior.later,)
                    if any(overlap(cut,attempt.source) for attempt in protected):
                        raise ValueError("The proposed cut overlaps a take you explicitly kept.")
            return cut
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from exc

    def save(self, db, project_id, snapshot, candidates):
        cuts=merge_spans([item.cut for item in candidates if item.cut],snapshot["duration"])
        base=[Span.model_validate(s) for s in snapshot["existing"]]
        timeline=Timeline.from_cuts(snapshot["duration"],cuts+base)
        if not timeline.retained:
            raise HTTPException(422,"At least one source interval must be retained.")
        revision={"schema_version":1,"id":str(uuid4()),"created_at":datetime.now(timezone.utc).isoformat(),
                  **{key:snapshot[key] for key in ("source_id","transcript_revision","base_edit_revision","base_hash","settings")},
                  "reviewed_candidates":[item.model_dump(mode="json") for item in candidates],
                  "accepted_cuts":[item.model_dump() for item in cuts],
                  "retained_source_ranges":[item.model_dump() for item in timeline.retained],
                  "time_saved_ms":snapshot["duration"]-timeline.duration_ms-sum(item.duration for item in base)}
        db.execute("INSERT INTO retake_reviews VALUES (?,?,?)",(revision["id"],str(project_id),json.dumps(revision)))
        return revision

    def review(self, project_id, result_id, candidate_id, decision, expected):
        media=self.verified_media(project_id)
        with self.projects.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            snapshot,candidate=self.evidence(db,project_id,result_id,candidate_id,media)
            prior=current_review(db,project_id)
            if (prior["id"] if prior else None)!=expected:
                raise HTTPException(409,"Decisions changed. Reload before reviewing.")
            others=[Candidate.model_validate(item) for item in prior["reviewed_candidates"] if item["id"]!=candidate_id] if prior else []
            if prior and prior["transcript_revision"]!=snapshot["transcript_revision"]:
                raise HTTPException(409,"Restore the empty revision before reviewing a new transcript.")
            cut=self.cut_for(project_id,snapshot,candidate,decision,others)
            updated=candidate.model_copy(update={"status":decision.status,"selected":decision.selected,
                                                  "adjustment":decision.adjustment,"cut":cut})
            return self.save(db,project_id,snapshot,[*others,updated])

    def restore(self, project_id, revision_id, expected):
        media=self.verified_media(project_id)
        with self.projects.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current=current_review(db,project_id)
            if (current["id"] if current else None)!=expected:
                raise HTTPException(409,"Decisions changed. Reload before restoring.")
            snapshot=self.context(db,project_id,media)
            snapshot["settings"]=Options().model_dump()
            candidates=[]
            if revision_id:
                row=db.execute("SELECT payload FROM retake_reviews WHERE id=? AND project_id=?",
                               (revision_id,str(project_id))).fetchone()
                if not row:
                    raise HTTPException(404,"Retake review revision not found.")
                previous=json.loads(row[0])
                if previous["transcript_revision"]!=snapshot["transcript_revision"]:
                    raise HTTPException(409,"This revision belongs to an older transcript.")
                snapshot["settings"]=previous["settings"]
                for item in previous["reviewed_candidates"]:
                    candidate=Candidate.model_validate(item)
                    restored=self.cut_for(project_id,snapshot,candidate,
                                          Decision(status=candidate.status,selected=candidate.selected,adjustment=candidate.adjustment),candidates)
                    candidates.append(candidate.model_copy(update={"cut":restored}))
            return self.save(db,project_id,snapshot,candidates)
