import subprocess
from pathlib import Path


class AudioError(Exception):
    pass


def extract_audio(ffmpeg: str, source: Path, output: Path, check_cancel, *, mono=True):
    # Keep stream offsets relative to the input's common timeline origin.
    args = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
        "-protocol_whitelist", "file", "-f", "mov", "-enable_drefs", "0",
        "-copyts", "-start_at_zero", "-i", str(source), "-map", "0:a:0",
        "-vn", "-af", "aresample=16000:async=1:first_pts=0",
        *(["-ac", "1"] if mono else []), "-ar", "16000", "-c:a", "pcm_s16le", str(output),
    ]
    run_audio(args, check_cancel)


def run_audio(args, check_cancel):
    try:
        with subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) as process:
            try:
                while True:
                    check_cancel()
                    try:
                        _, error = process.communicate(timeout=0.3)
                        break
                    except subprocess.TimeoutExpired:
                        continue
            except BaseException:
                process.kill()
                process.communicate()
                raise
            if process.returncode:
                raise AudioError("FFmpeg could not process the audio track. " +
                                 error.decode(errors="replace").strip()[-500:])
            return error.decode(errors="replace")
    except FileNotFoundError as exc:
        raise AudioError("FFmpeg is unavailable. Install it and retry.") from exc
