from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .timeline import Span

AssetType = Literal["stock_video", "stock_image", "personal_video", "personal_image",
                    "generated_image", "typography", "graphic"]


class VisualIdea(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    first_word: int
    last_word: int
    concept: str
    purpose: Literal["example", "contrast", "transition", "explanation", "takeaway"]
    asset_type: AssetType
    queries: list[str]
    prompt: str | None
    rationale: str


class VisualEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID = Field(default_factory=uuid4)
    source: Span
    phrase: str = Field(default="", max_length=350)
    words: tuple[int, int] | None = None
    concept: str = Field(default="Supporting visual", min_length=1, max_length=180)
    purpose: Literal["example", "contrast", "transition", "explanation", "takeaway"] = "example"
    asset_type: AssetType = "personal_image"
    queries: list[str] = Field(default_factory=list, max_length=3)
    prompt: str | None = Field(default=None, max_length=450)
    rationale: str = Field(default="", max_length=320)
    placement: Literal["cutaway"] = "cutaway"
    transition: Literal["cut"] = "cut"
    crop: Literal["fit", "fill"] = "fill"
    movement: bool = False
    asset_id: UUID | None = None
    status: Literal["pending", "accepted", "rejected"] = "pending"

    @model_validator(mode="after")
    def validate_visual(self):
        if not 300 <= self.source.duration <= 8000:
            raise ValueError("Visual inserts must be 300–8000 source milliseconds.")
        if self.status == "accepted" and not self.asset_id:
            raise ValueError("Choose an asset before accepting an insert.")
        if any(not 1 <= len(query.strip()) <= 100 for query in self.queries):
            raise ValueError("Each stock query needs 1–100 characters.")
        return self


class VisualAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    checksum: str = Field(pattern=r"^[a-f0-9]{64}$")
    kind: Literal["image", "video"]
    extension: Literal[".mp4", ".mov", ".jpg", ".jpeg", ".png", ".webp"]
    duration: float | None
    hdr: bool = False
    provenance: dict


class BoundVisual(BaseModel):
    event: VisualEvent
    asset: VisualAsset
