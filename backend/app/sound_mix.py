import math
import subprocess
import wave
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

RATE = 48000
PRESETS = {
    "none@1": "No Music", "mystical-ambient@1": "Mystical Ambient", "healing-frequency@1": "Healing Frequency",
    "emotional-journey@1": "Emotional Journey", "inspired-action@1": "Inspired Action",
    "deep-reflection@1": "Deep Reflection", "professional-focus@1": "Professional Focus",
}
MOODS = ("ambient", "healing", "emotional", "inspired", "reflective", "professional")


class Rights(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid")
    origin: Literal["owned", "licensed", "generated", "fixture"]
    license: str = Field(min_length=3, max_length=500)
    creator: str = Field(min_length=1, max_length=150)
    commercial: bool = False
    confirmed: bool = False
    platforms: list[Literal["instagram", "youtube", "tiktok", "other"]] = Field(default_factory=list, max_length=4)
    source_url: str = Field(default="", max_length=500)
    restrictions: str = Field(default="", max_length=500)
    prohibited: bool = False

    @model_validator(mode="after")
    def valid(self):
        from urllib.parse import urlsplit
        if self.source_url:
            url = urlsplit(self.source_url)
            if url.scheme != "https" or not url.hostname or url.username or url.password:
                raise ValueError("Provide an HTTPS source URL without credentials.")
        if not self.license.strip() or not self.creator.strip():
            raise ValueError("Creator and licence details are required.")
        return self

    def permits(self, platform, commercial):
        return (self.confirmed and not self.prohibited and self.origin != "fixture" and
                platform in self.platforms and (not commercial or self.commercial))


class AudioAsset(BaseModel, frozen=True):
    id: UUID
    checksum: str = Field(pattern=r"^[a-f0-9]{64}$")
    extension: Literal[".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"]
    duration: float = Field(gt=0, le=3600)
    kind: Literal["music", "effect"]
    rights: Rights
    true_peak: float | None = None
    lufs: float | None = None


class Duck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    reduction: float = Field(default=7, ge=0, le=18)
    threshold: float = Field(default=-34, ge=-65, le=-10)
    attack_ms: int = Field(default=100, ge=20, le=1000)
    release_ms: int = Field(default=650, ge=80, le=4000)
    hold_ms: int = Field(default=330, ge=0, le=2000)
    floor_db: float = Field(default=-44, ge=-70, le=-18)


class Point(BaseModel):
    ms: int = Field(ge=0, le=1200000)
    gain_db: float = Field(ge=-60, le=0)


class Cue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID = Field(default_factory=uuid4)
    asset_id: UUID | None = None
    search: str = Field(default="", max_length=140)
    rationale: str = Field(default="", max_length=300)
    start_ms: int = Field(ge=0, le=1200000)
    end_ms: int = Field(gt=0, le=1200000)
    offset_ms: int = Field(default=0, ge=0, le=3600000)
    gain_db: float = Field(default=-24, ge=-60, le=-12)
    fade_in_ms: int = Field(default=1100, ge=0, le=6000)
    fade_out_ms: int = Field(default=1400, ge=0, le=6000)
    loop: bool = False
    crossfade_ms: int = Field(default=180, ge=50, le=2000)
    keyframes: list[Point] = Field(default_factory=list, max_length=20)
    mute: bool = False
    status: Literal["pending", "accepted", "rejected"] = "pending"

    @model_validator(mode="after")
    def valid(self):
        duration = self.end_ms-self.start_ms
        if duration < 40 or self.fade_in_ms+self.fade_out_ms > duration:
            raise ValueError("Cue duration must fit both fades and be at least 40 ms.")
        if self.status == "accepted" and self.asset_id is None:
            raise ValueError("Choose a licensed audio asset or a test fixture first.")
        times = [point.ms for point in self.keyframes]
        if times != sorted(set(times)) or any(ms >= duration for ms in times):
            raise ValueError("Gain keyframes must be unique, ordered and inside the cue.")
        return self


class BoundCue(BaseModel):
    cue: Cue
    asset: AudioAsset


class Mix(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preset: str = "none@1"
    platform: Literal["instagram", "youtube", "tiktok", "other"] = "instagram"
    commercial: bool = True
    voice_db: float = Field(default=0, ge=-9, le=0)
    music_db: float = Field(default=0, ge=-24, le=0)
    effects_db: float = Field(default=0, ge=-24, le=0)
    mute_music: bool = False
    mute_effects: bool = False
    bypass: bool = False
    duck: Duck = Field(default_factory=Duck)
    music: Cue | None = None
    effects: list[Cue] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def valid(self):
        if self.preset not in PRESETS:
            raise ValueError("Unknown versioned music preset.")
        if any(cue.loop for cue in self.effects):
            raise ValueError("Looping is available for music only.")
        return self

    def cues(self):
        if self.bypass:
            return []
        music = [self.music] if self.preset != "none@1" and self.music and not self.mute_music else []
        return [cue for cue in music + ([] if self.mute_effects else self.effects)
                if cue.status == "accepted" and not cue.mute]

    @property
    def active(self):
        return bool(self.cues() or (not self.bypass and self.voice_db))


def samples(ms):
    return ms * (RATE // 1000)


def gain(db):
    return 10 ** (db/20)


def duck_curve(voice, settings, bed_gain):
    import numpy as np
    if not settings.enabled or not len(voice):
        return 1
    chunk = RATE//50
    padded = np.pad(voice, ((0, (-len(voice)) % chunk), (0, 0)))
    levels = np.sqrt(np.mean(padded.reshape(-1, chunk, 2)**2, axis=(1,2)))
    threshold = gain(settings.threshold)
    target_reduction = min(settings.reduction,max(0,bed_gain-settings.floor_db))
    level = hold = 0
    envelope = []
    for rms in levels:
        if rms >= threshold:
            hold = math.ceil(settings.hold_ms/20)+1
        desired = target_reduction if hold else 0
        hold = max(0,hold-1)
        period = settings.attack_ms if desired > level else settings.release_ms
        level += (desired-level)*(1-math.exp(-20/period))
        envelope.append(-level)
    return 10 ** (np.interp(np.arange(len(voice)),np.arange(len(envelope))*chunk,envelope)/20)


def decode(executable, path, seconds, offset=0):
    import numpy as np
    from pathlib import Path
    extension=Path(path).suffix.lower()
    formats={".wav":"wav",".mp3":"mp3",".m4a":"mov",".aac":"aac",".flac":"flac",".ogg":"ogg",".mp4":"mov"}
    fmt=formats.get(extension)
    demux=["-f",fmt,*(["-enable_drefs","0"] if fmt=="mov" else [])] if fmt else []
    data = subprocess.check_output([executable,"-nostdin","-v","error","-protocol_whitelist","file",*demux,
        "-ss",f"{offset:.6f}","-i",str(path),"-map","0:a:0","-vn","-t",f"{seconds:.6f}",
        "-ar",str(RATE),"-ac","2","-f","f32le","-"],stderr=subprocess.PIPE,timeout=60)
    return np.frombuffer(data,dtype="<f4").reshape((-1,2))


def fit_clip(track, count, crossfade=0):
    import numpy as np
    if len(track)>=count:
        return track[:count].copy()
    if not crossfade or len(track)<=2*crossfade:
        raise ValueError("The audio asset is shorter than the cue or cannot support its loop crossfade.")
    # Allocate once, blending the tail of one repeat into the head of the next.
    out = np.zeros((count,2),dtype=np.float32)
    out[:len(track)] = track
    edge = len(track)
    while edge < count:
        begin = edge-crossfade
        stop = min(count,begin+len(track))
        blend = np.linspace(0,1,crossfade,dtype=np.float32)[:,None]
        out[begin:edge] = out[begin:edge]*(1-blend)+track[:crossfade]*blend
        out[edge:stop] = track[crossfade:stop-begin]
        edge = stop
    return out


def apply_envelope(clip, cue, db):
    import numpy as np
    clip *= gain(db)
    rise,fall = samples(cue.fade_in_ms),samples(cue.fade_out_ms)
    if rise:
        clip[:rise] *= np.linspace(0,1,rise,dtype=np.float32)[:,None]
    if fall:
        clip[-fall:] *= np.linspace(1,0,fall,dtype=np.float32)[:,None]
    if cue.keyframes:
        points = {0:0, **{samples(p.ms):p.gain_db for p in cue.keyframes}}
        envelope = np.interp(np.arange(len(clip)),list(points),list(points.values()))
        clip *= (10 ** (envelope/20))[:,None]


def program_audio(executable, carrier, bound, mix, milliseconds, stage, *, analysis=None, window=None):
    import numpy as np
    from time import perf_counter
    length=samples(milliseconds)
    voice=decode(executable,carrier,milliseconds/1000)
    voice=np.pad(voice[:length],((0,max(0,length-len(voice))),(0,0)))
    base=voice*gain(mix.voice_db if not mix.bypass else 0)
    music=np.zeros_like(base)
    effects=np.zeros_like(base)
    duck_seconds=0.0
    maximum = gain(-1.5)
    for item,path in bound:
        cue,asset=item.cue,item.asset
        stage()
        start,end=samples(cue.start_ms),min(length,samples(cue.end_ms))
        duration=end-start
        if duration <= 0:
            continue
        available=asset.duration-cue.offset_ms/1000
        track=decode(executable,path,min(available,duration/RATE+0.025),cue.offset_ms/1000)
        if not cue.loop and len(track) < duration and duration-len(track) <= RATE//100:
            track=np.pad(track,((0,duration-len(track)),(0,0)))
        clip=fit_clip(track,duration,samples(cue.crossfade_ms) if cue.loop else 0)
        bus=music if asset.kind=="music" else effects
        target=-16 if asset.kind=="music" else -18
        calibration=max(-12,min(16,target-asset.lufs)) if asset.lufs is not None else 0
        db=cue.gain_db+(mix.music_db if asset.kind=="music" else mix.effects_db)+calibration
        apply_envelope(clip,cue,db)
        if asset.kind=="music":
            began=perf_counter()
            curve=duck_curve(base[start:end],mix.duck,cue.gain_db+mix.music_db)
            clip *= curve[:,None] if hasattr(curve,"ndim") else curve
            duck_seconds+=perf_counter()-began
        bus[start:end]+=clip
    combined=base+music+effects
    peak=float(np.max(np.abs(combined),initial=0))
    if peak > maximum:
        combined *= maximum/peak
    if analysis is not None:
        def bars(track):
            return [round(float(np.max(np.abs(part),initial=0)),3) for part in np.array_split(track,72)]
        def rms(track):
            return round(20*math.log10(max(1e-6, float(np.sqrt(np.mean(track**2))))),2)
        analysis.update(duck_seconds=round(duck_seconds,4),
                        rms_dbfs={"voice":rms(base),"music":rms(music),"effects":rms(effects)},
                        waveforms={"voice":bars(base),"music":bars(music),"effects":bars(effects)})
    if window:
        a,b=map(samples,window)
        return base[a:b],(base+music)[a:b],combined[a:b],music[a:b],effects[a:b]
    return combined


def write_pcm(path, audio):
    import numpy as np
    with wave.open(str(path),"wb") as stream:
        stream.setparams((2,2,RATE,0,"NONE","not compressed"))
        stream.writeframes((np.clip(audio,-1,32767/32768)*32768).astype("<i2").tobytes())


class SoundIdea(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["music","effect"]
    preset: Literal["none@1","mystical-ambient@1","healing-frequency@1","emotional-journey@1",
                    "inspired-action@1","deep-reflection@1","professional-focus@1"]
    asset_id: str | None
    search: str
    start_ms: int
    end_ms: int
    gain_db: float
    fade_in_ms: int
    fade_out_ms: int
    duck: bool
    reduction_db: float
    rationale: str



def sound_meter(executable, path, stage=lambda:None):
    """Read a BS.1770/EBU integrated meter without running a normalization pass."""
    import re
    import tempfile
    with tempfile.TemporaryFile() as log, subprocess.Popen(
          [executable,"-nostdin","-hide_banner","-v","info","-protocol_whitelist","file","-i",str(path),
           "-map","0:a:0","-vn","-af","ebur128=peak=true:framelog=verbose","-f","null","-"],
          stdout=subprocess.DEVNULL,stderr=log) as process:
        try:
            while process.poll() is None:
                stage()
                try: process.wait(timeout=.2)
                except subprocess.TimeoutExpired: continue
            log.seek(0)
            summary=log.read().decode(errors="replace").rsplit("Summary:",1)[-1]
            if process.returncode:
                raise ValueError("Unable to measure the soundtrack.")
            def read(label):
                match=re.search(rf"^\s*{label}:\s*(-?[\d.]+)",summary,re.MULTILINE)
                return float(match[1]) if match else None
            level=read("I")
            if level is not None and level<=-70:
                level=None
            return {"input_i":level,"input_tp":read("Peak"),"input_lra":read("LRA")}
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
