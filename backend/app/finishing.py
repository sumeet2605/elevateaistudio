import json
import math
import re
import subprocess
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Grade(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    intensity: float = Field(default=1, ge=0, le=1)
    exposure: float = Field(default=0, ge=-0.5, le=0.5)
    brightness: float = Field(default=0, ge=-0.12, le=0.12)
    contrast: float = Field(default=0, ge=-0.3, le=0.3)
    saturation: float = Field(default=0, ge=-0.5, le=0.35)
    temperature: float = Field(default=0, ge=-0.08, le=0.08)
    tint: float = Field(default=0, ge=-0.08, le=0.08)
    gamma: float = Field(default=0, ge=-0.25, le=0.25)
    highlights: float = Field(default=0, ge=-0.12, le=0.12)
    shadows: float = Field(default=0, ge=-0.12, le=0.12)
    black: float = Field(default=0, ge=-0.08, le=0.08)
    white: float = Field(default=0, ge=-0.08, le=0.08)
    vibrance: float = Field(default=0, ge=-0.3, le=0.3)
    sharpen: float = Field(default=0, ge=0, le=0.7)
    vignette: float = Field(default=0, ge=0, le=0.5)


class Voice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    bypass: bool = False
    intensity: float = Field(default=1, ge=0, le=1)
    gain: float = Field(default=0, ge=-12, le=12)
    highpass: int = Field(default=65, ge=0, le=180)
    denoise: float = Field(default=0, ge=0, le=12)
    presence: float = Field(default=0.8, ge=-4, le=4)
    threshold: float = Field(default=-19, ge=-45, le=-6)
    ratio: float = Field(default=1.6, ge=1, le=4)
    attack: float = Field(default=15, ge=2, le=100)
    release: float = Field(default=130, ge=30, le=650)
    makeup: float = Field(default=0, ge=0, le=6)
    deess: float = Field(default=0.12, ge=0, le=0.55)
    target: float = Field(default=-14, ge=-28, le=-10)
    peak: float = Field(default=-1.5, ge=-6, le=-1)

    @property
    def enabled(self):
        return not self.bypass and self.intensity > 0


GRADES = {
    "ungraded@1": ("Ungraded reference", Grade(intensity=0)),
    "mystical-warmth@1": ("Mystical Warmth", Grade(temperature=.025, contrast=.035, saturation=-.045,
                                                 highlights=-.025, shadows=.012)),
    "clean-professional@1": ("Clean Professional", Grade(contrast=.065, sharpen=.14)),
    "deep-cinematic@1": ("Deep Cinematic", Grade(contrast=.13, saturation=-.12, shadows=-.016, highlights=-.035)),
    "healing-soft@1": ("Healing Soft", Grade(contrast=-.07, saturation=-.13, shadows=.025, highlights=-.04)),
}
VOICES = {
    "elevate-voice@1": ("Elevate Voice", Voice()),
    "podcast-clarity@1": ("Podcast Clarity", Voice(highpass=85, presence=1.9, ratio=2.3, threshold=-22, deess=.19)),
    "healing-voice@1": ("Healing Voice", Voice(highpass=45, presence=0, ratio=1.2, target=-19, deess=.06)),
    "raw-audio@1": ("Raw Audio", Voice(bypass=True)),
}


def grade_filter(grade):
    if not grade or grade.intensity == 0:
        return ""
    p = grade.intensity
    filters = []
    if any((grade.exposure, grade.brightness, grade.contrast, grade.saturation, grade.gamma)):
        filters.append(f"eq=brightness={p*grade.brightness:.5f}:contrast={1+p*grade.contrast:.5f}:"
                       f"saturation={1+p*grade.saturation:.5f}:gamma={1+p*grade.gamma:.5f}")
    if grade.exposure:
        filters.append(f"exposure=exposure={p*grade.exposure:.5f}")
    if grade.temperature or grade.tint:
        t, g = p*grade.temperature, p*grade.tint
        filters.append(f"colorbalance=rm={t:.5f}:gm={-g:.5f}:bm={-t:.5f}:pl=1")
    if any((grade.shadows, grade.highlights, grade.black, grade.white)):
        black, white = p*grade.black, 1+p*grade.white
        curve = f"0/{max(0,black):.5f} 0.25/{.25+p*grade.shadows+black*.5:.5f} 0.75/{.75+p*grade.highlights+(white-1)*.5:.5f} 1/{min(1,white):.5f}"
        filters.append(f"curves=all='{curve}'")
    if grade.vibrance:
        filters.append(f"vibrance=intensity={p*grade.vibrance:.5f}")
    if grade.sharpen:
        filters.append(f"unsharp=5:5:{p*grade.sharpen:.5f}")
    if grade.vignette:
        filters.append(f"vignette=angle={p*grade.vignette:.5f}")
    return ",".join(filters) + "," if filters else ""


def speech_filter(voice):
    p = voice.intensity
    chain = [f"volume={voice.gain*p:.4f}dB"]
    if voice.highpass:
        chain.append(f"highpass=f={max(1,voice.highpass*p):.4f}")
    if voice.denoise*p >= .01:
        chain.append(f"afftdn=nr={max(.01,voice.denoise*p):.4f}:nf=-45")
    if voice.presence:
        chain.append(f"equalizer=f=2800:t=q:w=1:g={voice.presence*p:.4f}")
    chain.append(f"acompressor=threshold={10**(voice.threshold/20):.6f}:ratio={1+(voice.ratio-1)*p:.5f}:"
                 f"attack={voice.attack:.4f}:release={voice.release:.4f}:makeup={10**(voice.makeup*p/20):.5f}")
    if voice.deess:
        chain.append(f"deesser=i={voice.deess*p:.4f}:m=0.3")
    return ",".join(chain) + ","


def loudnorm(voice, measured=None, *, stats=False):
    # Leave headroom for overshoot from AAC encoding.
    result = f"loudnorm=I={voice.target:g}:TP={voice.peak-.6:g}:LRA=11"
    fields = {"input_i": "measured_I", "input_tp": "measured_TP", "input_lra": "measured_LRA",
              "input_thresh": "measured_thresh", "target_offset": "offset"}
    if measured and all(measured.get(name) is not None for name in fields):
        result += ":linear=true" + "".join(f":{arg}={measured[name]:.4f}" for name, arg in fields.items())
    return result + (":print_format=json" if stats else "")


def parse_loudness(log):
    matches = re.findall(r'\{\s*"input_i".*?\}', log, re.DOTALL)
    if not matches:
        return {}
    result = {}
    for key, value in json.loads(matches[-1]).items():
        if key != "normalization_type":
            number = float(value)
            result[key] = number if math.isfinite(number) else None
    return result


def measure_audio(executable, source, stage=lambda: None, *, audio_index=None, chain="", voice=None):
    """Measure decoded audio, including intersample peaks, with FFmpeg's BS.1770 meter."""
    args = [executable, "-nostdin", "-hide_banner", "-v", "info", "-protocol_whitelist", "file",
            "-i", str(source), "-map", f"0:{audio_index}" if audio_index is not None else "0:a:0",
            "-af", f"{chain}{loudnorm(voice or Voice(), stats=True)}", "-f", "null", "-"]
    return loudness_pass(args, stage)


def loudness_pass(args, stage=lambda: None):
    import tempfile

    with tempfile.TemporaryFile() as log, subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=log) as process:
        try:
            while process.poll() is None:
                stage()
                try:
                    process.wait(timeout=.25)
                except subprocess.TimeoutExpired:
                    continue
            log.seek(0)
            trace = log.read().decode(errors="replace")
            if process.returncode:
                raise ValueError("FFmpeg could not measure the audio. " + trace[-400:])
            return parse_loudness(trace)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
