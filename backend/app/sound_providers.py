"""Providers yield original audio and ownership metadata for the same import pipeline."""
import wave
from pathlib import Path
from typing import Protocol, Literal

from .sound_mix import RATE

Effect = Literal["chime", "swell", "whoosh", "soft-impact", "ambient-transition"]


class SoundProvider(Protocol):
    name: str

    def prepare(self, effect: Effect, destination: Path) -> None: ...


class LocalEffects:
    name = "studio-original-effects-v1"

    def prepare(self, effect: Effect, destination: Path) -> None:
        import numpy as np

        t = np.arange(2*RATE)/RATE
        if effect == "chime":
            tone = (.16*np.sin(2*np.pi*523.25*t)+.055*np.sin(2*np.pi*784*t))*np.exp(-2*t)
        elif effect in ("swell", "ambient-transition"):
            tone = (.08*np.sin(2*np.pi*196*t)+.04*np.sin(2*np.pi*293.66*t))*np.sin(np.pi*t/2)**2
            if effect == "ambient-transition":
                tone *= .65+.35*np.cos(2*np.pi*1.6*t)
        elif effect == "whoosh":
            random=np.random.default_rng(425).normal(size=len(t))
            noise=np.convolve(random,np.ones(24)/24,mode="same")
            tone=noise*.16*np.sin(np.pi*t/2)**4
        else:
            tone=.22*np.sin(2*np.pi*(75*t-16*t**2))*np.exp(-6*t)
        tone *= np.minimum(1,t/.03)*np.minimum(1,(2-t)/.08)
        with wave.open(str(destination),"wb") as out:
            out.setparams((1,2,RATE,0,"NONE","not compressed"))
            out.writeframes((tone*32767).astype("<i2").tobytes())
