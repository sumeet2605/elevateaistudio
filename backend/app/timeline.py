from pydantic import BaseModel, Field, model_validator


class Span(BaseModel, frozen=True):
    start_ms: int = Field(ge=0, strict=True)
    end_ms: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def ordered(self):
        if self.end_ms <= self.start_ms:
            raise ValueError("A range must end after it starts.")
        return self

    @property
    def duration(self):
        return self.end_ms - self.start_ms


def merge_spans(spans: list[Span], duration: int) -> list[Span]:
    merged: list[Span] = []
    for span in sorted(spans, key=lambda item: item.start_ms):
        if span.end_ms > duration:
            raise ValueError("Range exceeds the source duration.")
        if merged and span.start_ms <= merged[-1].end_ms:
            last = merged.pop()
            span = Span(start_ms=last.start_ms, end_ms=max(last.end_ms, span.end_ms))
        merged.append(span)
    return merged


def subtract_spans(span: Span, excluded: list[Span]) -> list[Span]:
    remaining = []
    start = span.start_ms
    for cut in excluded:
        if cut.end_ms <= start:
            continue
        if cut.start_ms >= span.end_ms:
            break
        if cut.start_ms > start:
            remaining.append(Span(start_ms=start, end_ms=cut.start_ms))
        start = max(start, cut.end_ms)
    if start < span.end_ms:
        remaining.append(Span(start_ms=start, end_ms=span.end_ms))
    return remaining


class Timeline:
    def __init__(self, source_duration_ms: int, retained: list[Span]):
        if source_duration_ms <= 0:
            raise ValueError("Source duration must be positive.")
        previous = 0
        for span in retained:
            if span.start_ms < previous or span.end_ms > source_duration_ms:
                raise ValueError("Retained ranges must be ordered, disjoint and within the source.")
            previous = span.end_ms
        self.source_duration_ms = source_duration_ms
        self.retained = tuple(retained)
        self.duration_ms = sum(span.duration for span in retained)

    @classmethod
    def from_cuts(cls, duration: int, cuts: list[Span]):
        return cls(duration, subtract_spans(Span(start_ms=0, end_ms=duration), merge_spans(cuts, duration)))

    def source_to_edited(self, time_ms: int) -> int | None:
        if not 0 <= time_ms <= self.source_duration_ms:
            raise ValueError("Source timestamp is out of range.")
        if time_ms == self.source_duration_ms:
            return self.duration_ms
        offset = 0
        for span in self.retained:
            if span.start_ms <= time_ms < span.end_ms:
                return offset + time_ms - span.start_ms
            offset += span.duration
        return None

    def edited_to_source(self, time_ms: int) -> int:
        if not 0 <= time_ms <= self.duration_ms:
            raise ValueError("Edited timestamp is out of range.")
        if time_ms == self.duration_ms:
            return self.retained[-1].end_ms if self.retained else self.source_duration_ms
        for span in self.retained:
            if time_ms < span.duration:
                return span.start_ms + time_ms
            time_ms -= span.duration
        raise AssertionError("Edited time fell outside validated retained ranges.")
