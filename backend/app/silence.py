import math
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .timeline import Span, Timeline, merge_spans, subtract_spans
from .transcription import Transcript


class DetectionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    threshold_dbfs: float = Field(default=-35, ge=-90, le=-5)
    minimum_silence_ms: int = Field(default=1200, ge=200, le=30000)
    leading_padding_ms: int = Field(default=300, ge=0, le=5000)
    trailing_padding_ms: int = Field(default=300, ge=0, le=5000)
    minimum_removable_ms: int = Field(default=200, ge=50, le=10000)
    noise_aware: bool = False


PRESETS = {
    "gentle": DetectionSettings(),
    "balanced": DetectionSettings(minimum_silence_ms=800, leading_padding_ms=200, trailing_padding_ms=200),
    "fast-paced": DetectionSettings(minimum_silence_ms=500, leading_padding_ms=150, trailing_padding_ms=150),
}


class Suggestion(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    source: Span
    proposed: Span
    silence_duration_ms: int
    removable_duration_ms: int
    methods: list[Literal["amplitude", "vad", "transcript"]]
    confidence: Literal["high", "ambiguous"]
    reason: str
    status: Literal["pending", "accepted", "rejected", "modified"] = "pending"
    modification: Span | None = None

    @model_validator(mode="after")
    def validate_ranges(self):
        for span in [self.proposed, self.modification]:
            if span and not self.source.start_ms <= span.start_ms < span.end_ms <= self.source.end_ms:
                raise ValueError("Cut must stay inside the detected candidate.")
        if self.silence_duration_ms != self.source.duration or self.removable_duration_ms != self.proposed.duration:
            raise ValueError("Suggestion durations must match their ranges.")
        if (self.status == "modified") != (self.modification is not None):
            raise ValueError("Only a modified cut may contain custom boundaries.")
        return self


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["pending", "accepted", "rejected", "modified"]
    modification: Span | None = None

    @model_validator(mode="after")
    def requires_boundaries(self):
        if (self.status == "modified") != (self.modification is not None):
            raise ValueError("Provide boundaries only when modifying a cut.")
        return self


class EditingPlan(BaseModel):
    schema_version: Literal[1] = 1
    id: UUID
    project_id: UUID
    source_media_id: str
    source_duration_ms: int = Field(gt=0)
    transcript_version: UUID | None
    detection: DetectionSettings
    effective_threshold_dbfs: float
    proposed_cuts: list[Suggestion]
    approved_cuts: list[Span]
    retained_segments: list[Span]
    estimated_edited_duration_ms: int
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_timeline(self):
        previous = 0
        for suggestion in self.proposed_cuts:
            if not previous <= suggestion.source.start_ms < suggestion.source.end_ms <= self.source_duration_ms:
                raise ValueError("Candidates must be ordered and within the source.")
            if suggestion.modification and suggestion.modification.duration < self.detection.minimum_removable_ms:
                raise ValueError("The adjusted cut is shorter than the minimum removable duration.")
            previous = suggestion.source.end_ms
        approved, timeline = approved_timeline(self.source_duration_ms, self.proposed_cuts)
        if self.approved_cuts != approved or self.retained_segments != list(timeline.retained):
            raise ValueError("The plan does not match its reviewed suggestions.")
        if self.estimated_edited_duration_ms != timeline.duration_ms:
            raise ValueError("The edited duration does not match the retained ranges.")
        return self


def approved_timeline(duration, suggestions):
    cuts = [item.modification or item.proposed for item in suggestions if item.status in ("accepted", "modified")]
    approved = merge_spans(cuts, duration)
    return approved, Timeline.from_cuts(duration, approved)


def update_plan(plan: EditingPlan, suggestions: list[Suggestion]):
    approved, timeline = approved_timeline(plan.source_duration_ms, suggestions)
    return EditingPlan.model_validate({
        **plan.model_dump(), "proposed_cuts": suggestions, "approved_cuts": approved,
        "retained_segments": list(timeline.retained), "estimated_edited_duration_ms": timeline.duration_ms,
        "updated_at": datetime.now(timezone.utc),
    })


def speech_guard(transcript: Transcript | None, vad: list[Span], duration: int):
    speech = [(item.start_ms / 1000, item.end_ms / 1000) for item in vad]
    if transcript:
        for segment in transcript.segments:
            speech.extend((item.start, item.end) for item in segment.words or [segment])
    spans = []
    for start, end in speech:
        low = max(0, math.floor(start * 1000) - 50)
        high = min(duration, math.ceil(end * 1000) + 50)
        if low < high:
            spans.append(Span(start_ms=low, end_ms=high))
    return merge_spans(spans, duration)


def suggest(settings: DetectionSettings, duration: int, quiet: list[Span], vad: list[Span],
            transcript: Transcript | None):
    if duration <= 0:
        return []
    protected = speech_guard(transcript, vad, duration)
    quiet = merge_spans(quiet, duration)
    suggestions = []
    for gap in subtract_spans(Span(start_ms=0, end_ms=duration), protected):
        quiet_parts = []
        for interval in quiet:
            if interval.end_ms <= gap.start_ms:
                continue
            if interval.start_ms >= gap.end_ms:
                break
            quiet_parts.append(Span(start_ms=max(gap.start_ms, interval.start_ms),
                                    end_ms=min(gap.end_ms, interval.end_ms)))
        for area, low_level in [(part, True) for part in quiet_parts] + [
            (part, False) for part in subtract_spans(gap, quiet_parts)
        ]:
            length = area.duration
            start = area.start_ms + settings.leading_padding_ms
            end = area.end_ms - settings.trailing_padding_ms
            if length < settings.minimum_silence_ms or end - start < settings.minimum_removable_ms:
                continue
            methods = ["vad"]
            if low_level:
                methods.append("amplitude")
            if transcript is not None:
                methods.append("transcript")
            high = low_level and transcript is not None
            reason = ("Below the amplitude threshold and outside detected speech and transcript words." if high else
                      "Low level with no VAD speech. Generate a transcript for an additional check." if low_level else
                      "No speech was detected, but the audio is above the silence threshold. Listen before approving.")
            suggestions.append(Suggestion(
                source=area, proposed=Span(start_ms=start, end_ms=end),
                silence_duration_ms=length, removable_duration_ms=end - start, methods=methods,
                confidence="high" if high else "ambiguous", reason=reason,
            ))
    return sorted(suggestions, key=lambda item: item.source.start_ms)
