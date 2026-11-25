import hashlib
import json
import os
import platform
import selectors
import subprocess
import tempfile
from pathlib import Path

from .motion_models import Sample

DETECTOR = "apple-vision-rectangles-r3-ffmpeg-v2"
SWIFT = Path(__file__).parent / "motion_native" / "detect.swift"


def detector_key(source_id, options):
    data = f"{source_id}:{DETECTOR}:{platform.mac_ver()[0]}:{options.model_dump_json()}"
    return hashlib.sha256(data.encode()).hexdigest()


def locate_binary(data):
    if platform.system() != "Darwin":
        raise ValueError("Local face analysis requires macOS Apple Vision. Manual framing works on this host.")
    folder = data / "motion-tools"
    folder.mkdir(exist_ok=True)
    fingerprint = hashlib.sha256(SWIFT.read_bytes()).hexdigest()[:12]
    binary = folder / f"detector-{fingerprint}"
    if not binary.is_file():
        try:
            subprocess.run(["xcrun","swiftc","-O","-module-cache-path",str(folder/"modules"),str(SWIFT),"-o",str(binary)],
                           check=True, timeout=120, capture_output=True)
        except (OSError, subprocess.SubprocessError) as error:
            raise ValueError("Install the macOS Command Line Tools to run Apple Vision face analysis.") from error
    return binary


def detect_faces(data, source, duration, options, update, *, ffmpeg, ffprobe, media):
    from .ffmpeg_render import HDR_TRANSFERS, run_ffmpeg, streams

    if duration > 30 * 60 * 1000:
        raise ValueError("Face analysis supports originals up to 30 minutes.")
    binary = locate_binary(data)
    video, _, _ = streams(ffprobe, source)
    filters = f"fps={options.frequency}:start_time=0:round=up,scale=640:640:force_original_aspect_ratio=decrease"
    if video.get("color_transfer") in HDR_TRANSFERS:
        filters += (",zscale=t=linear:npl=203,format=gbrpf32le,zscale=p=bt709,"
                    "tonemap=hable:desat=2,zscale=t=bt709:m=bt709:r=tv,format=yuv420p")
    with tempfile.TemporaryDirectory(prefix="face-frames-", dir=data) as temporary:
        folder = Path(temporary)
        args = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-progress", "pipe:1",
                "-protocol_whitelist", "file", "-f", "mov", "-enable_drefs", "0",
                "-copyts", "-start_at_zero", "-i", str(source), "-map", f"0:{video['index']}",
                "-an", "-vf", filters, "-t", f"{duration/1000:.3f}",
                "-q:v", "3", "-threads:v", "2", str(folder/"sample-%06d.jpg")]
        run_ffmpeg(args, folder, duration, lambda progress: update(progress*0.65))
        frames = read_boxes(binary, folder, options, duration, update)
        size = jpeg_dimensions(next(folder.glob("sample-*.jpg")))
    # Encoded dimensions and upright sampled geometry can be swapped by display rotation.
    width, height = media["width"], media["height"]
    if (width > height) != (size[0] > size[1]):
        width, height = height, width
    return {"width": width, "height": height, "samples": [frame.model_dump() for frame in frames]}


def jpeg_dimensions(path):
    # FFmpeg writes baseline JPEG with a start-of-frame marker before the scan data.
    data = path.read_bytes()
    offset = 2
    while offset + 9 <= len(data) and data[offset] == 255:
        if data[offset+1] in (0xC0, 0xC1, 0xC2):
            return int.from_bytes(data[offset+7:offset+9]), int.from_bytes(data[offset+5:offset+7])
        offset += 2 + int.from_bytes(data[offset+2:offset+4])
    raise ValueError("Could not read upright face sample geometry.")


def read_boxes(binary, folder, options, duration, update):
    frames = []
    with subprocess.Popen([str(binary), str(folder), str(options.frequency)],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as job, selectors.DefaultSelector() as poll:
        poll.register(job.stdout, selectors.EVENT_READ)
        pending = b""
        try:
            while True:
                update(.65 + .34 * min(1, len(frames)*1000/(duration*options.frequency)))
                if not poll.select(.3):
                    continue
                chunk = os.read(job.stdout.fileno(), 65536)
                if not chunk:
                    break
                lines = (pending+chunk).split(b"\n")
                pending = lines.pop()
                frames.extend(Sample.model_validate_json(line) for line in lines)
            if job.wait() or not frames:
                raise ValueError("Apple Vision could not analyse the sampled video frames.")
        except BaseException:
            if job.poll() is None:
                job.kill()
                job.wait()
            raise
    return [frame for frame in frames if frame.source_ms <= duration]
