import hashlib
from typing import Literal
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from .brand import Brand
from .sound_mix import Mix,BoundCue
from .visuals import BoundVisual, VisualAsset
from .studio_key import StudioSettings
from .finishing import Grade, Voice
from .motion_models import CameraSettings, Focus, MotionEvent
from .motion_store import rules
from .timeline import Span, Timeline

FPS = 30
SAMPLE_RATE = 48000


class RenderSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    framing: Literal["fit", "crop"] = "fit"
    quality: Literal["standard", "high"] = "standard"
    playback_speed: Literal[1.0, 1.1] = 1.0
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


class Zoom(BaseModel, frozen=True):
    source: Span
    percent: int = Field(gt=100, le=125, strict=True)


class RenderManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    project_id: UUID
    plan_id: UUID
    caption_revision: UUID | None = None
    caption_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
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
    zooms: list[Zoom] = Field(default_factory=list)
    motion_revision: UUID | None = None
    motion_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    camera: CameraSettings | None = None
    motion_events: list[MotionEvent] = Field(default_factory=list, max_length=40)
    face_track: list[tuple[int, Focus]] = Field(default_factory=list)
    display_geometry: tuple[int, int] | None = None
    face_version: UUID | None = None
    grade: Grade | None = None
    voice: Voice | None = None
    grade_revision: UUID | None = None
    voice_revision: UUID | None = None
    grade_fingerprint: str | None = None
    voice_fingerprint: str | None = None
    visual_revision: UUID | None = None
    visual_fingerprint: str | None = None
    visuals: list[BoundVisual] = Field(default_factory=list, max_length=18)
    mix: Mix | None = None
    mix_revision: UUID | None = None
    mix_fingerprint: str | None = None
    sound_cues: list[BoundCue] = Field(default_factory=list, max_length=9)
    studio: StudioSettings | None = None
    studio_revision: UUID | None = None
    studio_fingerprint: str | None = Field(default=None,pattern=r"^[a-f0-9]{64}$")
    studio_asset: VisualAsset | None = None
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
        previous = 0
        for zoom in self.zooms:
            if zoom.percent > self.brand.max_zoom_percent or zoom.source.start_ms < previous:
                raise ValueError("Zooms must be ordered, disjoint, and within the brand magnification limit.")
            if not any(item.start_ms <= zoom.source.start_ms < zoom.source.end_ms <= item.end_ms
                       for item in self.retained):
                raise ValueError("A zoom crosses a removed range.")
            previous = zoom.source.end_ms
        if len(self.edited_zooms()) != len(self.zooms):
            raise ValueError("An accepted zoom does not cover a complete retained frame.")
        if self.camera:
            cap = min(self.brand.max_zoom_percent,
                      rules(self.playbook_revision, self.camera.energy == "energetic")[0])
            if len({event.id for event in self.motion_events}) != len(self.motion_events):
                raise ValueError("Camera event IDs must be unique.")
            previous = 0
            for event in sorted(self.motion_events, key=lambda item: item.source.start_ms):
                source = event.source
                if source.end_ms > self.source_duration_ms:
                    raise ValueError("A camera event exceeds the original duration.")
                if event.status == "rejected":
                    continue
                if max(event.start_zoom, event.end_zoom) > cap:
                    raise ValueError(f"Camera events must respect the {cap}% brand and playbook limit.")
                if event.status == "accepted":
                    if source.start_ms < previous:
                        raise ValueError("Accepted camera events cannot overlap.")
                    if self.map_motion_span(source) is None:
                        raise ValueError("A camera event crosses an edit seam or has no retained frames. Adjust the event to fit within one kept shot.")
                    previous = source.end_ms
            timestamps = [time for time, _ in self.face_track]
            if timestamps != sorted(set(timestamps)) or any(not 0 <= time <= self.source_duration_ms for time in timestamps):
                raise ValueError("Face-tracking timestamps must be ordered and within the original.")
            if self.display_geometry and min(self.display_geometry) < 2:
                raise ValueError("Invalid display dimensions.")
        if self.mix:
            active = {cue.id:cue for cue in self.mix.cues()}
            if len(active)!=len(self.sound_cues) or any(
                item.cue != active.get(item.cue.id) or item.cue.asset_id != item.asset.id
                or item.cue.end_ms > self.encoded_ms for item in self.sound_cues
            ):
                raise ValueError("The pinned mix cues do not match the edited timeline.")
        if self.studio and self.studio.enabled and self.studio.mode=="asset":
            if not self.studio_asset or self.studio_asset.id != self.studio.background_id:
                raise ValueError("The studio background is not pinned.")
            asset=self.studio_asset
            if asset.kind=="video" and (not asset.duration or self.studio.offset_ms/1000>=asset.duration or
                (not self.studio.loop and asset.duration-self.studio.offset_ms/1000+.02<self.total_frames/FPS)):
                raise ValueError("The pinned background is too short for the kept frames.")
        last = 0
        for visual in self.visuals:
            event, asset = visual.event, visual.asset
            if event.status != "accepted" or event.asset_id != asset.id:
                raise ValueError("Only accepted inserts with their pinned assets may render.")
            mapped = self.visual_frames(event.source)
            if not mapped or mapped[0] < last:
                raise ValueError("Visual inserts overlap or cross a removed interval.")
            last = mapped[1]
            if asset.kind == "video" and (asset.duration is None or asset.duration + 1e-6 < (mapped[1]-mapped[0])/FPS):
                raise ValueError("A video cutaway is shorter than the insert. Shorten the event.")
        return self

    def map_motion_span(self, source):
        if not any(span.start_ms <= source.start_ms < source.end_ms <= span.end_ms for span in self.retained):
            return None
        offset = 0
        for keep in self.frame_ranges:
            start = max(keep.start_frame, (source.start_ms * FPS + 999) // 1000)
            end = min(keep.end_frame, source.end_ms * FPS // 1000)
            if start < end:
                return start - keep.start_frame + offset, end - keep.start_frame + offset
            offset += keep.duration
        return None

    def visual_frames(self, source):
        mapped = self.map_motion_span(source)
        if mapped is None:
            return None
        speed = self.settings.playback_speed
        start, end = (round(frame / speed) for frame in mapped)
        return (start, min(end, self.output_frames)) if end > start else None

    def edited_zooms(self):
        mapped = []
        for zoom in self.zooms:
            offset = 0
            for keep in self.frame_ranges:
                start = max(keep.start_frame, (zoom.source.start_ms * FPS + 999) // 1000)
                end = min(keep.end_frame, zoom.source.end_ms * FPS // 1000)
                if start < end:
                    mapped.append((start - keep.start_frame + offset, end - keep.start_frame + offset, zoom.percent))
                    break
                offset += keep.duration
        return mapped

    @property
    def approval_fingerprint(self):
        identity = self.plan_fingerprint
        if self.caption_revision:
            identity = hashlib.sha256(f"{identity}:{self.caption_revision}:{self.caption_fingerprint}".encode()).hexdigest()
        if self.motion_revision:
            identity = hashlib.sha256(f"{identity}:{self.motion_revision}:{self.motion_fingerprint}".encode()).hexdigest()
        for revision, fingerprint in ((self.grade_revision, self.grade_fingerprint),
                                      (self.voice_revision, self.voice_fingerprint),
                                      (self.visual_revision, self.visual_fingerprint),
                                      (self.mix_revision, self.mix_fingerprint),
                                      (self.studio_revision,self.studio_fingerprint)):
            if revision:
                identity = hashlib.sha256(f"{identity}:{revision}:{fingerprint}".encode()).hexdigest()
        return identity

    @property
    def total_frames(self):
        return sum(span.duration for span in self.frame_ranges)

    @property
    def output_frames(self):
        return max(1, round(self.total_frames / self.settings.playback_speed))

    @property
    def encoded_ms(self):
        return milliseconds(self.output_frames)
