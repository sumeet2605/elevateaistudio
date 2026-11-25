import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, StringConstraints
from typing import Annotated


class ProjectInput(BaseModel):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class ProjectNotFound(Exception):
    pass


class SourceExists(Exception):
    pass


class Projects:
    def __init__(self, root: Path):
        self.root = root

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.root / "studio.sqlite3")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    brand_id TEXT NOT NULL DEFAULT 'elevate-with-sumeet',
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    media TEXT
                )
            """)

    @staticmethod
    def serialize(row):
        if row is None:
            raise ProjectNotFound
        project = dict(row)
        project["media"] = json.loads(project["media"]) if project["media"] else None
        return project

    def list(self):
        with self.connect() as db:
            return [self.serialize(row) for row in db.execute(
                "SELECT * FROM projects ORDER BY created_at DESC, id DESC"
            )]

    def get(self, project_id: UUID):
        with self.connect() as db:
            return self.serialize(db.execute(
                "SELECT * FROM projects WHERE id = ?", (str(project_id),)
            ).fetchone())

    def create(self, title: str):
        project_id = uuid4()
        with self.connect() as db:
            db.execute("INSERT INTO projects (id, title) VALUES (?, ?)", (str(project_id), title))
        return self.get(project_id)

    def media_dir(self, project_id: UUID):
        return self.root / "projects" / str(project_id)

    def attach(self, project_id: UUID, metadata: dict):
        with self.connect() as db:
            updated = db.execute(
                "UPDATE projects SET media = ? WHERE id = ? AND media IS NULL",
                (json.dumps(metadata), str(project_id)),
            )
            if updated.rowcount != 1:
                raise SourceExists

    def source_path(self, project_id: UUID, media: dict):
        directory = self.media_dir(project_id).resolve()
        path = (directory / media["stored_name"]).resolve()
        if path.parent != directory or not path.is_file():
            raise FileNotFoundError("The original file is missing from local storage.")
        return path
