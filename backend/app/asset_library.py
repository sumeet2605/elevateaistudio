import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

from fastapi import HTTPException

from .visuals import VisualAsset

CATEGORIES = ("Meditation", "Journaling", "Fitness", "Nature", "Travel", "Family", "Coaching",
              "Office and career", "Money and finance", "Abstract visuals", "Virtual Studio")
IMAGE_FORMAT = {".jpg": "mjpeg", ".jpeg": "mjpeg", ".png": "png", ".webp": "webp"}
EXTENSIONS = {".mp4", ".mov", *IMAGE_FORMAT}


def utc():
    return datetime.now(timezone.utc).isoformat()


def library_root(projects):
    root = projects.root.resolve() / "assets"
    root.mkdir(exist_ok=True)
    if root.is_symlink():
        raise ValueError("The asset library must be a local directory.")
    return root


def initialize_visuals(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS visual_assets (
            id TEXT PRIMARY KEY, checksum TEXT NOT NULL UNIQUE, payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS visual_revisions (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
            plan_id TEXT NOT NULL REFERENCES director_plans(id), payload TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS visuals_by_plan ON visual_revisions(project_id,plan_id);
        CREATE TABLE IF NOT EXISTS image_requests (
            id TEXT PRIMARY KEY REFERENCES jobs(id), project_id TEXT NOT NULL REFERENCES projects(id),
            request_key TEXT NOT NULL, reserved_cents INTEGER NOT NULL, payload TEXT NOT NULL, asset_id TEXT
        );
    """)


def asset_row(db, uid):
    row = db.execute("SELECT payload FROM visual_assets WHERE id=?", (str(uid),)).fetchone()
    if not row:
        raise HTTPException(404, "Visual asset not found.")
    return json.loads(row[0])


def asset_model(row):
    return VisualAsset.model_validate({key: row[key] for key in VisualAsset.model_fields})


def asset_path(projects, asset, *, thumbnail=False):
    root = library_root(projects)
    directory = root / str(asset.id)
    result = directory / ("thumbnail.jpg" if thumbnail else "original"+asset.extension)
    if directory.is_symlink() or result.is_symlink() or not result.is_file():
        raise HTTPException(409, "The pinned visual file is missing.")
    return result


def store_asset(projects, settings, stream, filename, description, category, tags, provenance):
    extension = Path(filename).suffix.lower()
    if extension not in EXTENSIONS:
        raise HTTPException(415, "Upload MP4, MOV, JPEG, PNG or WebP.")
    limit = min(settings.max_upload_bytes, int(os.getenv("ELEVATE_ASSET_MAX_BYTES", "100000000")))
    root = library_root(projects)
    with TemporaryDirectory(prefix=".asset-", dir=root) as staging:
        working = Path(staging)
        original = working / ("original"+extension)
        length = 0
        sha = hashlib.sha256()
        with original.open("xb") as target:
            while block := stream.read(1024*1024):
                length += len(block)
                if length > limit:
                    raise HTTPException(413, "Visual asset exceeds the configured limit.")
                sha.update(block)
                target.write(block)
        checksum = sha.hexdigest()
        with projects.connect() as db:
            prior = db.execute("SELECT payload FROM visual_assets WHERE checksum=?", (checksum,)).fetchone()
        if prior:
            return json.loads(prior[0])
        kind = "image" if extension in IMAGE_FORMAT else "video"
        demuxer = "mov" if kind == "video" else {"mjpeg": "jpeg_pipe", "png": "png_pipe", "webp": "webp_pipe"}[IMAGE_FORMAT[extension]]
        demux = ["-protocol_whitelist", "file", "-f", demuxer, *(["-enable_drefs", "0"] if kind == "video" else [])]
        try:
            raw = subprocess.check_output([settings.ffprobe, "-v", "error", *demux, "-show_streams",
                                           "-show_format", "-of", "json", str(original)],
                                          stderr=subprocess.DEVNULL, timeout=15)
            details = json.loads(raw)
            video = next(item for item in details["streams"]
                         if item["codec_type"] == "video" and not item.get("disposition", {}).get("attached_pic"))
            width, height = video["width"], video["height"]
            if not 16 <= min(width, height) or max(width, height) > 8192:
                raise ValueError("Unsupported dimensions.")
            duration = float(video.get("duration", details["format"].get("duration", 0))) if kind == "video" else None
            if kind == "image" and video["codec_name"] != IMAGE_FORMAT[extension]:
                raise ValueError("Image does not match its extension.")
            if kind == "video" and not 0 < duration <= 3600:
                raise ValueError("Invalid asset duration.")
            from .ffmpeg_render import HDR_TO_SDR, HDR_TRANSFERS
            hdr = video.get("color_transfer") in HDR_TRANSFERS
            filters = "scale=320:320:force_original_aspect_ratio=decrease:force_divisible_by=2"
            if hdr:
                filters += ","+HDR_TO_SDR
            subprocess.run([settings.ffmpeg, "-nostdin", "-v", "error", *demux, "-i", str(original),
                            "-map", "0:v:0", "-frames:v", "1", "-vf", filters, "-threads", "1",
                            str(working/"thumbnail.jpg")], capture_output=True, check=True, timeout=25)
            if not (working/"thumbnail.jpg").is_file():
                raise ValueError("No picture could be decoded.")
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, StopIteration) as error:
            raise HTTPException(422, "Visual asset could not be validated and decoded.") from error
        uid = str(uuid4())
        row = {"id": uid, "checksum": checksum, "kind": kind, "extension": extension, "duration": duration,
               "hdr": hdr, "provenance": {**provenance, "retrieved_at": utc()}, "name": Path(filename).name[:150],
               "description": description[:400], "category": category, "tags": tags[:12],
               "width": width, "height": height, "aspect_ratio": round(width/height, 4),
               "bytes": length, "favourite": False}
        destination = root / uid
        with projects.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT payload FROM visual_assets WHERE checksum=?", (checksum,)).fetchone()
            if prior:
                return json.loads(prior[0])
            destination.mkdir()
            try:
                for item in working.iterdir():
                    item.chmod(0o444)
                    item.rename(destination/item.name)
                db.execute("INSERT INTO visual_assets VALUES (?,?,?)", (uid, checksum, json.dumps(row)))
            except BaseException:
                import shutil
                shutil.rmtree(destination)
                raise
        return row
