from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Brand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="Elevate with Sumeet", min_length=1, max_length=80)
    primary: str = Field(default="#FFFFFF", pattern=r"^#[0-9A-Fa-f]{6}$")
    highlight: str = Field(default="#EFC77F", pattern=r"^#[0-9A-Fa-f]{6}$")
    background: str = Field(default="#191B1E", pattern=r"^#[0-9A-Fa-f]{6}$")
    typography: str = Field(default="Inter Bold", min_length=1, max_length=80)
    subtitle_placement: Literal["lower-third", "center", "upper-third"] = "lower-third"
    caption_style: Literal["clean-bold", "minimal", "keyword-highlight"] = "clean-bold"
    max_zoom_percent: int = Field(default=108, ge=100, le=125)
    transitions: Literal["minimal", "hard-cuts"] = "minimal"
    background_music: Literal["none", "optional-low"] = "none"
    audio: Literal["natural", "voice-leveling"] = "natural"
    width: int = Field(default=1080, ge=240, le=4096, multiple_of=2)
    height: int = Field(default=1920, ge=240, le=4096, multiple_of=2)
    video_codec: Literal["h264"] = "h264"
    audio_codec: Literal["aac"] = "aac"
    intro: str = Field(default="Start with recorded speech.", max_length=200)
    outro: str = Field(default="Let's Elevate!", max_length=120)


class Playbook(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Literal["mystical-sumeet", "educational", "storytelling", "authority"]
    revision: Literal[1]
    name: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    pacing: str = Field(min_length=1)
    pause_preservation: str = Field(min_length=1)
    hook_handling: str = Field(min_length=1)
    caption_emphasis: str = Field(min_length=1)
    max_zooms_per_minute: int = Field(ge=0, le=12)
    b_roll: str = Field(min_length=1)
    transitions: Literal["minimal", "hard-cuts"]
    target_style: str = Field(min_length=1)
