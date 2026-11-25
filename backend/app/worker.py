import fcntl
import logging
import os
import signal
import threading
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException

from .analysis_store import ANALYSIS, AnalysisStore
from .analysis_worker import analyse
from .audio import AudioError, extract_audio
from .config import Settings
from .director_store import DIRECTOR, DirectorStore
from .director_worker import direct
from .jobs import Cancelled, Jobs
from .projects import Projects
from .render_store import RENDER, Renders
from .render_worker import render_video
from .transcription import FasterWhisper, Transcript, TranscriptionSettings, segment_from_model

logger = logging.getLogger(__name__)


class WorkerStopped(Exception):
    pass


class Worker:
    def __init__(self, settings, jobs, engine):
        self.settings = settings
        self.jobs = jobs
        self.engine = engine
        self.stop = threading.Event()

    def process(self, job):
        job_id = job["id"]
        project_id = UUID(job["project_id"])
        projects = self.jobs.projects
        directory = projects.media_dir(project_id)
        audio = directory / f"transcription-{job_id}.wav"
        artifact = directory / f"transcription-{job_id}.json"
        staged = artifact.with_suffix(".tmp")

        def stage(label="extracting_audio", progress=None):
            if self.stop.is_set():
                raise WorkerStopped
            self.jobs.stage(job_id, label, progress)

        published = False
        try:
            if job["type"] == RENDER:
                render_video(self.settings, self.jobs, job, stage)
                return
            if job["type"] == DIRECTOR:
                direct(self.jobs, job, stage)
                return
            if job["type"] == ANALYSIS:
                analyse(self.settings, self.jobs, job, stage)
                return
            project = projects.get(project_id)
            media = project["media"]
            source = projects.source_path(project_id, media)
            duration = media["duration_seconds"]
            extract_audio(self.settings.ffmpeg, source, audio, stage)
            stage("loading_model")
            self.engine.load()
            stage("transcribing", 0)
            candidates, info = self.engine.transcribe(audio)
            segments = []
            for candidate in candidates:
                segment = segment_from_model(candidate, duration, segments[-1].end if segments else 0)
                segments.append(segment)
                stage("transcribing", min(segment.end / duration, 1))
            stage("saving_transcript", segments[-1].end / duration if segments else None)
            transcript = Transcript(
                project_id=project_id, source_media_id=media["sha256"], source_duration=duration,
                model=self.engine.settings.model,
                language=info.language if segments else None,
                language_confidence=info.language_probability if segments else None,
                text=" ".join(segment.text for segment in segments), segments=segments,
                created_at=datetime.now(timezone.utc),
            )
            with staged.open("x", encoding="utf-8") as file:
                file.write(transcript.model_dump_json(indent=2))
                file.flush()
                os.fsync(file.fileno())
            stage("saving_transcript", segments[-1].end / duration if segments else None)
            self.jobs.publish(job_id, transcript, lambda: staged.replace(artifact))
            published = True
        except Cancelled:
            self.jobs.finish(job_id, "cancelled")
        except WorkerStopped:
            self.jobs.finish(job_id, "interrupted", "The local worker stopped before finishing.")
        except Exception as exc:
            logger.exception("%s job %s failed", job["type"], job_id)
            if isinstance(exc, HTTPException):
                detail = str(exc.detail)
            elif isinstance(exc, (AudioError, FileNotFoundError, ImportError, ValueError)):
                detail = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            else:
                detail = "Check the worker log, model installation, and network access, then retry."
            self.jobs.finish(job_id, "failed", detail)
        finally:
            staged.unlink(missing_ok=True)
            if not published:
                audio.unlink(missing_ok=True)
                artifact.unlink(missing_ok=True)

    def run(self):
        lock_path = self.settings.data_dir / ".transcription-worker.lock"
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SystemExit("A transcription worker is already running for this data directory.") from exc
            self.jobs.recover()
            while not self.stop.is_set():
                job = self.jobs.claim()
                if job:
                    self.process(job)
                else:
                    self.stop.wait(0.35)


def main():
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    projects = Projects(settings.data_dir)
    projects.initialize()
    jobs = Jobs(projects)
    jobs.initialize()
    AnalysisStore(jobs).initialize()
    DirectorStore(jobs).initialize()
    Renders(jobs).initialize()
    engine = FasterWhisper(TranscriptionSettings.from_env(settings.data_dir))
    worker = Worker(settings, jobs, engine)
    for event in (signal.SIGTERM, signal.SIGINT):
        signal.signal(event, lambda *_: worker.stop.set())
    logger.info("Local worker ready for %s (%s, %s, %s)", settings.data_dir,
                engine.settings.model, engine.settings.device, engine.settings.compute_type)
    worker.run()


if __name__ == "__main__":
    main()
