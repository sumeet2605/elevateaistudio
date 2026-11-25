from typing import Literal
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from .brand import Brand
from .timeline import Span, Timeline

FPS = 30
SAMPLE_RATE = 48000


class RenderSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    framing: Literal["fit", "crop"] = "fit"
    quality: Literal["standard", "high"] = "standard"
    caption_words: int = Field(default=7, ge=3, le=14)
    caption_gap_ms: int = Field(default=380, ge=120, le=1000)
    caption_min_ms: int = Field(default=650, ge=300, le=1500)


class FrameRange(BaseModel, frozen=True):
    start_frame: int = Field(ge=0, strict=True, validation_alias=AliasChoices("start_frame", "start_ms"))
    end_frame: int = Field(gt=0, strict=True, validation_alias=AliasChoices("end_frame", "end_ms"))

    @model_validator(mode="after")
    def ordered(self):
        if self.end_frame <= self.start_frame:
            raise ValueError("A retained frame range must have positive duration.")
        return self

    @property
    def duration(self):
        return self.end_frame - self.start_frame


def frame_spans(retained: list[Span]):
    spans = []
    for span in retained:
        start = (span.start_ms * FPS + 999) // 1000
        end = span.end_ms * FPS // 1000
        if start < end:
            spans.append(FrameRange(start_frame=start, end_frame=end))
    return spans


def milliseconds(frames: int):
    return (frames * 1000 + FPS // 2) // FPS


class RenderManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    project_id: UUID
    plan_id: UUID
    plan_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_media_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    transcript_revision: UUID
    transcript_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    brand_revision: str
    playbook_revision: str
    brand: Brand
    settings: RenderSettings
    source_duration_ms: int = Field(gt=0)
    cuts: list[Span]
    retained: list[Span]
    frame_ranges: list[FrameRange]
    emphasis: list[Span]
    voice_leveling: bool
    notices: list[str]

    @model_validator(mode="after")
    def validated(self):
        if (self.brand.width, self.brand.height) != (1080, 1920):
            raise ValueError("Day 5 rendering requires a 1080 × 1920 brand revision.")
        retained = Timeline.from_cuts(self.source_duration_ms, self.cuts).retained
        if tuple(self.retained) != retained or self.frame_ranges != frame_spans(list(retained)):
            raise ValueError("Render ranges do not match the approved cuts.")
        if not self.frame_ranges or len(self.frame_ranges) > 80:
            raise ValueError("The render needs between 1 and 80 frame-aligned source ranges.")
        if any(not any(span.start_ms <= item.start_ms < item.end_ms <= span.end_ms for span in self.retained)
               for item in self.emphasis):
            raise ValueError("Caption emphasis crosses a removed range.")
        return self

    @property
    def total_frames(self):
        return sum(span.duration for span in self.frame_ranges)

    @property
    def encoded_ms(self):
        return milliseconds(self.total_frames)
