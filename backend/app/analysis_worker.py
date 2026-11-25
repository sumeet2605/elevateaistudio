import hashlib
import json
import math
import os
from datetime import datetime, timezone
from uuid import UUID

from .analysis_store import AnalysisStore
from .audio import extract_audio
from .audio_analysis import detect_quiet, detect_speech, effective_threshold, measure_audio
from .silence import EditingPlan, suggest
from .timeline import Span
from .transcription import Transcript


def analyse(settings, jobs, job, stage):
    job_id = UUID(job["id"])
    project_id = UUID(job["project_id"])
    store = AnalysisStore(jobs)
    projects = jobs.projects
    project = projects.get(project_id)
    media = project["media"]
    source = projects.source_path(project_id, media)
    duration = math.floor(media["duration_seconds"] * 1000)
    config, version = store.inputs(job["id"])
    directory = projects.media_dir(project_id)
    transcript = None
    if version:
        path = directory / f"transcription-{version}.json"
        transcript = Transcript.model_validate_json(path.read_text(encoding="utf-8"))
        if transcript.source_media_id != media["sha256"] or transcript.project_id != project_id:
            raise ValueError("The transcript does not match the original source.")

    evidence_inputs = f"1:{media['sha256']}:{config.threshold_dbfs}:{config.minimum_silence_ms}:{config.noise_aware}"
    key = hashlib.sha256(evidence_inputs.encode()).hexdigest()
    audio = directory / f"audio-analysis-{job_id}.wav"
    artifact = store.waveform_path(project_id, job_id)
    staged = artifact.with_suffix(".tmp")
    cached = store.cached(project_id, key)
    waveform_job, evidence = cached or (job_id, None)
    published = False
    try:
        if evidence is None:
            extract_audio(settings.ffmpeg, source, audio, stage, mono=False)
            stage("measuring_audio", 0)
            metrics = measure_audio(audio, lambda progress: stage("measuring_audio", progress))
            threshold = effective_threshold(config, metrics["noise_floor_dbfs"])
            audio_duration = min(duration, math.floor(metrics["duration_seconds"] * 1000))
            stage("detecting_quiet")
            quiet = detect_quiet(settings.ffmpeg, audio, threshold,
                                 config.minimum_silence_ms, audio_duration,
                                 lambda: stage("detecting_quiet"))
            stage("detecting_speech")
            speech = detect_speech(audio, audio_duration, lambda: stage("detecting_speech"))
            evidence = {
                **metrics, "audio_duration_ms": audio_duration, "threshold_dbfs": threshold,
                "silence_intervals": [item.model_dump() for item in quiet],
                "speech_intervals": [item.model_dump() for item in speech],
            }

        stage("building_plan")
        suggestions = suggest(
            config, evidence["audio_duration_ms"],
            [Span.model_validate(item) for item in evidence["silence_intervals"]],
            [Span.model_validate(item) for item in evidence["speech_intervals"]],
            transcript,
        )
        now = datetime.now(timezone.utc)
        plan = EditingPlan(
            id=job_id, project_id=project_id, source_media_id=media["sha256"], source_duration_ms=duration,
            transcript_version=version, detection=config, effective_threshold_dbfs=evidence["threshold_dbfs"],
            proposed_cuts=suggestions, approved_cuts=[], retained_segments=[Span(start_ms=0, end_ms=duration)],
            estimated_edited_duration_ms=duration, created_at=now, updated_at=now,
        )
        commit_artifact = None
        if not cached:
            with staged.open("x", encoding="utf-8") as file:
                json.dump(evidence, file, allow_nan=False, separators=(",", ":"))
                file.flush()
                os.fsync(file.fileno())
            commit_artifact = lambda: staged.replace(artifact)
        stage("saving_plan")
        store.publish(plan, key, waveform_job, commit_artifact)
        published = True
    finally:
        audio.unlink(missing_ok=True)
        staged.unlink(missing_ok=True)
        if not cached and not published:
            artifact.unlink(missing_ok=True)
