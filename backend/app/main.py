import logging
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Settings
from .jobs import Jobs
from .media import ingest
from .projects import ProjectInput, ProjectNotFound, Projects, SourceExists
from .transcription_api import transcription_routes

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None):
    settings = settings or Settings.from_env()
    projects = Projects(settings.data_dir)
    jobs = Jobs(projects)

    @asynccontextmanager
    async def lifespan(_app):
        projects.initialize()
        jobs.initialize()
        yield

    app = FastAPI(title="Elevate AI Studio", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware, allow_origins=[settings.frontend_origin],
        allow_methods=["GET", "POST"], allow_headers=["Content-Type", "Range"],
    )

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.middleware("http")
    async def restrict_browser_writes(request: Request, call_next):
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin and origin != settings.frontend_origin:
                return JSONResponse({"detail": "This browser origin is not allowed."}, 403)
        return await call_next(request)

    @app.exception_handler(ProjectNotFound)
    async def project_not_found(_request, _error):
        return JSONResponse({"detail": "Project not found."}, 404)

    @app.exception_handler(SourceExists)
    async def source_exists(_request, _error):
        return JSONResponse({"detail": "This project already has an original. Create a new project for another video."}, 409)

    @app.exception_handler(OSError)
    async def storage_error(_request, error):
        logger.error("Media storage or subprocess I/O failed", exc_info=error)
        return JSONResponse({"detail": "Local storage or media inspection failed. Check the server logs."}, 500)

    def public(project):
        if project["media"]:
            project["media"].pop("stored_name")
        return project

    @app.get("/health")
    def health():
        return {"status": "ok", "tools": settings.dependencies(), "max_upload_bytes": settings.max_upload_bytes}

    @app.get("/projects")
    def list_projects():
        return [public(project) for project in projects.list()]

    @app.post("/projects", status_code=201, max_body_size=8192)
    def create_project(payload: ProjectInput):
        return public(projects.create(payload.title))

    @app.get("/projects/{project_id}")
    def get_project(project_id: UUID):
        return public(projects.get(project_id))

    @app.post(
        "/projects/{project_id}/source", status_code=201,
        max_body_size=settings.max_upload_bytes + 64 * 1024,
        form_limits={"max_files": 1, "max_fields": 0},
    )
    def upload_source(project_id: UUID, file: UploadFile = File(...)):
        return public(ingest(projects, settings, project_id, file))

    @app.get("/projects/{project_id}/source")
    def original(project_id: UUID):
        media = projects.get(project_id)["media"]
        if not media:
            raise HTTPException(404, "This project does not have an original yet.")
        try:
            path = projects.source_path(project_id, media)
        except FileNotFoundError as error:
            raise HTTPException(404, str(error)) from error
        return FileResponse(
            path, media_type=media["content_type"],
            headers={"X-Content-Type-Options": "nosniff"},
        )

    app.include_router(transcription_routes(projects, jobs))
    return app


app = create_app()
