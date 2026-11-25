import math
import re
import wave
from pathlib import Path

from .audio import AudioError, run_audio
from .timeline import Span, merge_spans

SAMPLE_RATE = 16000
WAVEFORM_BUCKETS = 600
WINDOW_FRAMES = 800


def dbfs(amplitude: float):
    return round(max(-120, 20 * math.log10(amplitude)), 2) if amplitude > 0 else -120.0


def effective_threshold(settings, noise_floor_dbfs: float):
    if not settings.noise_aware:
        return settings.threshold_dbfs
    # Permit up to 10 dB of upward adaptation, with a ceiling of -25 dBFS.
    return max(settings.threshold_dbfs, min(-25, settings.threshold_dbfs + 10, noise_floor_dbfs + 6))


def measure_audio(path: Path, progress):
    import numpy as np

    with wave.open(str(path)) as audio:
        channels = audio.getnchannels()
        frames = audio.getnframes()
        if not frames or (audio.getframerate(), audio.getsampwidth()) != (SAMPLE_RATE, 2):
            raise AudioError("Expected a nonempty 16 kHz PCM audio track.")
        bucket = max(1, math.ceil(frames / WAVEFORM_BUCKETS))
        waveform = np.zeros(math.ceil(frames / bucket), dtype=np.float64)
        windows = []
        clipped = 0
        energy = 0.0
        peak = 0
        read = 0
        while read < frames:
            block = np.frombuffer(audio.readframes(WINDOW_FRAMES), dtype="<i2").astype(np.int32)
            if block.size == 0:
                raise AudioError("The extracted audio ended unexpectedly.")
            length = block.size // channels
            magnitudes = np.abs(block)
            peak = max(peak, int(magnitudes.max()))
            clipped += int(np.count_nonzero(magnitudes >= 32767))
            squares = (block.astype(np.float64) / 32768) ** 2
            energy += float(squares.sum())
            # Use the loudest channel's window RMS so one quiet channel cannot mask the other.
            windows.append(float(np.sqrt(squares.reshape(-1, channels).mean(axis=0).max())))
            frame_peaks = magnitudes.reshape(-1, channels).max(axis=1)
            np.maximum.at(waveform, (read + np.arange(length)) // bucket, frame_peaks)
            read += length
            if len(windows) % 50 == 0 or read == frames:
                progress(read / frames)
        return {
            "duration_seconds": frames / SAMPLE_RATE,
            "channels": channels,
            "sample_rate_hz": SAMPLE_RATE,
            "peak_dbfs": dbfs(peak / 32768),
            "rms_dbfs": dbfs(math.sqrt(energy / (frames * channels))),
            "noise_floor_dbfs": dbfs(float(np.percentile(windows, 20))),
            "clipped_samples": clipped,
            "clip_sample_fraction": clipped / (frames * channels),
            "waveform": {
                "bucket_seconds": bucket / SAMPLE_RATE,
                "peaks": np.round(waveform / 32768, 4).tolist(),
            },
        }


def bounded_spans(ranges, duration: int):
    spans = []
    for start, end in ranges:
        low = max(0, round(start * 1000))
        high = min(duration, round(end * 1000))
        if low < high:
            spans.append(Span(start_ms=low, end_ms=high))
    return merge_spans(spans, duration)


def detect_quiet(ffmpeg: str, path: Path, threshold: float, minimum_ms: int, duration_ms: int, check):
    log = run_audio([
        ffmpeg, "-hide_banner", "-nostats", "-nostdin", "-v", "info",
        "-protocol_whitelist", "file", "-i", str(path),
        "-af", f"silencedetect=noise={threshold:g}dB:d={minimum_ms / 1000:g}", "-f", "null", "-",
    ], check)
    ranges = []
    start = None
    for kind, value in re.findall(r"silence_(start|end):\s*(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)", log):
        if kind == "start":
            start = float(value)
        elif start is not None:
            ranges.append((start, float(value)))
            start = None
    if start is not None:
        ranges.append((start, duration_ms / 1000))
    return bounded_spans(ranges, duration_ms)


def detect_speech(path: Path, duration_ms: int, check):
    import numpy as np
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    ranges = []
    with wave.open(str(path)) as audio:
        channels = audio.getnchannels()
        samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2").reshape(-1, channels)
    options = VadOptions(min_silence_duration_ms=250, speech_pad_ms=100)
    for channel in samples.T:
        check()
        detected = get_speech_timestamps(channel.astype(np.float32) / 32768, options)
        ranges.extend((item["start"] / SAMPLE_RATE, item["end"] / SAMPLE_RATE) for item in detected)
    return bounded_spans(ranges, duration_ms)
