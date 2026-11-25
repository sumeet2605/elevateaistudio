from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from .timeline import Span


class Focus(BaseModel, frozen=True):
    x: float = Field(default=0.5, ge=0, le=1)
    y: float = Field(default=0.5, ge=0, le=1)


class Face(BaseModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def within_frame(self):
        if self.x + self.width > 1.00001 or self.y + self.height > 1.00001:
            raise ValueError("Face box exceeds the source frame.")
        return self

    @computed_field
    @property
    def centre(self) -> Focus:
        return Focus(x=self.x + self.width/2, y=self.y + self.height/2)


class Sample(BaseModel):
    source_ms: int = Field(ge=0)
    faces: list[Face] = Field(max_length=30)


class FaceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    frequency: Literal[1, 2, 3] = 2
    min_confidence: float = Field(default=0.65, ge=0.3, le=0.95)


class CameraSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    framing: Literal["original", "center", "face", "manual", "fit"] = "original"
    focus: Focus = Field(default_factory=Focus)
    energy: Literal["playbook", "energetic"] = "playbook"
    dead_zone: float = Field(default=0.025, ge=0.005, le=0.12)
    smoothing: float = Field(default=0.32, ge=0.1, le=0.7)


class MotionEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID = Field(default_factory=uuid4)
    source: Span
    start_zoom: int = Field(default=100, ge=100, le=125)
    end_zoom: int = Field(default=105, ge=100, le=125)
    strategy: Literal["center", "face", "manual"] = "center"
    focus: Focus = Field(default_factory=Focus)
    transition: Literal["punch", "smooth", "hold"] = "smooth"
    easing: Literal["linear", "ease-in", "ease-out", "ease-in-out"] = "ease-in-out"
    reason: str = Field(default="Manual camera emphasis.", max_length=320)
    status: Literal["pending", "accepted", "rejected"] = "pending"

    @model_validator(mode="after")
    def duration_ok(self):
        if self.source.duration < 100:
            raise ValueError("Camera events need at least 100 milliseconds.")
        return self


def track_faces(samples, minimum, dead_zone, smoothing):
    """Single plausible subject only. Ambiguous frames hold the last reliable centre."""
    current = None
    anchor = None
    track = []
    for sample in samples:
        eligible = [f for f in sample.faces if f.confidence >= minimum]
        if len(eligible) == 1:
            face = eligible[0]
            aim = face.centre
            if anchor is None or max(abs(aim.x-anchor.x), abs(aim.y-anchor.y)) <= 0.22:
                anchor = aim
                if current is None:
                    current = aim
                else:
                    def approach(value, target):
                        error = target-value
                        if abs(error) <= dead_zone:
                            return value
                        return value+max(-0.045, min(0.045, smoothing*error))
                    current = Focus(x=approach(current.x,aim.x),y=approach(current.y,aim.y))
        if current is not None:
            track.append((sample.source_ms, current))
    return track


def clamp_crop(center: Focus, width: float, height: float, *, headroom=False):
    return (max(0,min(1-width,center.x-width/2)),
            max(0,min(1-height,center.y-height*(0.40 if headroom else 0.5))))
