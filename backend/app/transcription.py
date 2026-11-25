import os
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

Seconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Probability = Annotated[float, Field(ge=0, le=1)]


class Word(BaseModel):
    start: Seconds
    end: Seconds
    text: str
    probability: Probability | None = None


class Segment(BaseModel):
    start: Seconds
    end: Seconds
    text: str
    words: list[Word]


class Transcript(BaseModel):
    schema_version: Literal[1] = 1
    project_id: UUID
    source_media_id: str
    model: str
    language: str | None
    language_confidence: Probability | None
    source_duration: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    text: str
    segments: list[Segment]
    created_at: datetime

    @model_validator(mode="after")
    def check_timeline(self):
        previous = 0.0
        for segment in self.segments:
            if not previous <= segment.start <= segment.end <= self.source_duration:
                raise ValueError("Segment timestamps must be ordered within the original video.")
            previous = segment.end
            word_end = segment.start
            for word in segment.words:
                if not word_end <= word.start <= word.end <= segment.end:
                    raise ValueError("Word timestamps must be ordered within their segment.")
                word_end = word.end
        return self


@dataclass(frozen=True)
class TranscriptionSettings:
    model: str = "small"
    device: str = "cpu"
    compute_type: str = "int8"
    threads: int = 4
    beam_size: int = 5
    vad: bool = True
    vad_min_silence_ms: int = 500
    model_cache: Path | None = None

    @classmethod
    def from_env(cls, data_dir: Path):
        cache = os.getenv("ELEVATE_WHISPER_CACHE")
        return cls(
            model=os.getenv("ELEVATE_WHISPER_MODEL", cls.model),
            device=os.getenv("ELEVATE_WHISPER_DEVICE", cls.device),
            compute_type=os.getenv("ELEVATE_WHISPER_COMPUTE_TYPE", cls.compute_type),
            threads=int(os.getenv("ELEVATE_WHISPER_THREADS", cls.threads)),
            beam_size=int(os.getenv("ELEVATE_WHISPER_BEAM_SIZE", cls.beam_size)),
            vad=os.getenv("ELEVATE_WHISPER_VAD", "true").lower() in ("1", "true"),
            vad_min_silence_ms=int(os.getenv("ELEVATE_WHISPER_VAD_MIN_SILENCE_MS", cls.vad_min_silence_ms)),
            model_cache=Path(cache).resolve() if cache else data_dir / "models",
        )

    def __post_init__(self):
        if self.model.endswith(".en"):
            raise ValueError("Use a multilingual Whisper model for English and Hindi.")
        if min(self.threads, self.beam_size, self.vad_min_silence_ms) < 1:
            raise ValueError("Whisper threads, beam size, and minimum VAD silence must be positive.")


class FasterWhisper:
    def __init__(self, settings: TranscriptionSettings):
        self.settings = settings
        self.model = None

    def load(self):
        if self.model is None:
            from faster_whisper import WhisperModel

            settings = self.settings
            self.model = WhisperModel(
                settings.model, device=settings.device, compute_type=settings.compute_type,
                cpu_threads=settings.threads,
                download_root=str(settings.model_cache) if settings.model_cache else None,
            )

    def transcribe(self, audio: Path):
        import numpy as np

        # FFmpeg already produced Whisper's sample format. Feed the samples
        # directly so a second decoder cannot change alignment.
        with wave.open(str(audio)) as wav:
            if (wav.getnchannels(), wav.getframerate(), wav.getsampwidth()) != (1, 16000, 2):
                raise ValueError("Expected mono, 16 kHz, 16-bit PCM audio.")
            samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
        return self.model.transcribe(
            samples.astype(np.float32) / 32768.0, task="transcribe", language=None, word_timestamps=True,
            beam_size=self.settings.beam_size, vad_filter=self.settings.vad,
            vad_parameters={"min_silence_duration_ms": self.settings.vad_min_silence_ms},
        )


def segment_from_model(candidate, source_duration: float, previous_end: float):
    # Whisper occasionally emits small overlaps across words or adjacent segments.
    # Reconcile those at the boundary while preserving the source clock.
    def bounds(start, end, floor):
        if not 0 <= start <= end <= source_duration + 0.5 or start < floor - 0.5:
            raise ValueError("The speech model returned invalid source timestamps.")
        start = min(source_duration, max(floor, start))
        return start, min(source_duration, max(start, end))

    words = []
    for item in candidate.words or []:
        start, end = bounds(item.start, item.end, words[-1].end if words else previous_end)
        words.append(Word(start=start, end=end, text=item.word, probability=item.probability))
    start, end = bounds(candidate.start, candidate.end, previous_end)
    if words:
        start, end = min(start, words[0].start), max(end, words[-1].end)
    return Segment(start=start, end=end, text=candidate.text.strip(), words=words)
