import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    frontend_origin: str = "http://127.0.0.1:3000"
    max_upload_bytes: int = 512 * 1024 * 1024
    ffprobe: str = "ffprobe"
    ffmpeg: str = "ffmpeg"

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env")
        return cls(
            data_dir=(ROOT / os.environ.get("ELEVATE_DATA_DIR", "data")).resolve(),
            frontend_origin=os.environ.get("ELEVATE_FRONTEND_ORIGIN", cls.frontend_origin),
            max_upload_bytes=int(os.environ.get("ELEVATE_MAX_UPLOAD_BYTES", cls.max_upload_bytes)),
            ffprobe=os.environ.get("ELEVATE_FFPROBE", cls.ffprobe),
            ffmpeg=os.environ.get("ELEVATE_FFMPEG", cls.ffmpeg),
        )

    def dependencies(self):
        return {name: shutil.which(getattr(self, name)) is not None for name in ("ffprobe", "ffmpeg")}
