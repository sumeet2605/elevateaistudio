import hashlib
import json
import math
import subprocess
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import UUID, uuid4

from fastapi import HTTPException, UploadFile

from .config import Settings
from .projects import Projects, SourceExists

CONTENT_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime"}


def probe(executable: str, path: Path):
    args = [
        executable, "-v", "error", "-protocol_whitelist", "file", "-f", "mov",
        "-enable_drefs", "0",
        "-show_entries", "format=format_name,duration:stream=codec_type,codec_name,width,height:stream_disposition=attached_pic",
        "-of", "json", str(path),
    ]
    try:
        result = subprocess.run(args, capture_output=True, timeout=30, check=True)
    except FileNotFoundError as exc:
        raise HTTPException(503, "FFprobe is unavailable. Install it before uploading.") from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(422, "Video inspection timed out.") from exc
    except subprocess.CalledProcessError as exc:
        raise HTTPException(415, "The file is not a readable MP4 or MOV video.") from exc

    try:
        result_json = json.loads(result.stdout)
        streams = result_json["streams"]
        video = next(stream for stream in streams if stream["codec_type"] == "video"
                     and not stream.get("disposition", {}).get("attached_pic"))
        audio = next((stream for stream in streams if stream["codec_type"] == "audio"), {})
        duration = float(result_json["format"]["duration"])
        if not math.isfinite(duration) or duration <= 0 or min(video["width"], video["height"]) <= 0:
            raise ValueError
    except (KeyError, TypeError, ValueError, StopIteration) as exc:
        raise HTTPException(422, "The file needs a video stream with a valid duration and dimensions.") from exc
    return {
        "duration_seconds": duration, "width": video["width"], "height": video["height"],
        "video_codec": video["codec_name"], "audio_codec": audio.get("codec_name"),
    }


def ingest(projects: Projects, settings: Settings, project_id: UUID, upload: UploadFile):
    if projects.get(project_id)["media"]:
        raise SourceExists

    filename = (upload.filename or "").replace("\\", "/").split("/")[-1]
    suffix = Path(filename).suffix.lower()
    if suffix not in CONTENT_TYPES:
        raise HTTPException(415, "Choose an MP4 or MOV video.")
    if upload.size is not None and upload.size > settings.max_upload_bytes:
        raise HTTPException(413, "Video exceeds the upload limit.")

    directory = projects.media_dir(project_id)
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    total = 0
    with NamedTemporaryFile(dir=directory, prefix=".upload-", delete=False) as staged:
        temporary = Path(staged.name)
        try:
            while chunk := upload.file.read(1024 * 1024):
                total += len(chunk)
                if total > settings.max_upload_bytes:
                    raise HTTPException(413, "Video exceeds the upload limit.")
                digest.update(chunk)
                staged.write(chunk)
            if total == 0:
                raise HTTPException(422, "The video file is empty.")
            staged.close()
            if temporary.stat().st_size != total:
                raise OSError("Incomplete staged upload")
            metadata = probe(settings.ffprobe, temporary)
            source = directory / f"source-{uuid4().hex}{suffix}"
            temporary.chmod(0o444)
            temporary.rename(source)
            try:
                projects.attach(project_id, {
                    **metadata, "filename": filename, "stored_name": source.name,
                    "size_bytes": total, "sha256": digest.hexdigest(), "content_type": CONTENT_TYPES[suffix],
                })
            except Exception:
                source.unlink()
                raise
        finally:
            temporary.unlink(missing_ok=True)
    return projects.get(project_id)
