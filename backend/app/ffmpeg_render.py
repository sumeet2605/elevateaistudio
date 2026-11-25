import json
import os
import selectors
import subprocess
from pathlib import Path

from .audio import AudioError
from .motion_render import baseline_crop, camera_zoom, tracked
from .render_manifest import FPS, SAMPLE_RATE, RenderManifest


def streams(executable, path):
    try:
        output = subprocess.run([
            executable, "-v", "error", "-protocol_whitelist", "file",
            "-show_streams", "-show_format",
            "-of", "json", str(path),
        ], capture_output=True, check=True, timeout=30)
        payload = json.loads(output.stdout)
        return (next(s for s in payload["streams"] if s["codec_type"] == "video"
                     and not s.get("disposition", {}).get("attached_pic")),
                next(s for s in payload["streams"] if s["codec_type"] == "audio"), payload["format"])
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, StopIteration) as error:
        raise AudioError("Could not inspect the video and audio streams with FFprobe.") from error


HDR_TRANSFERS = frozenset({"arib-std-b67", "smpte2084"})


def ffmpeg_args(executable, source: Path, manifest: RenderManifest, video_index: int, audio_index: int, *,
                color_transfer: str | None = None):
    if type(video_index) is not int or type(audio_index) is not int or min(video_index, audio_index) < 0:
        raise ValueError("Invalid source stream indices.")
    count = len(manifest.frame_ranges)
    colour = "0x" + manifest.brand.background[1:].upper()
    camera = manifest.camera
    mode = camera.framing if camera else "original"
    crop = manifest.settings.framing == "crop" if mode == "original" else mode != "fit"
    keys = tracked(manifest) if camera else []
    focus_crop = baseline_crop(camera, keys if mode == "face" else []) if camera else ""
    scale = ("scale=1080:1920:force_original_aspect_ratio="
             + ("increase" if crop else "decrease") + ":force_divisible_by=2")
    frame = ("crop=1080:1920" if crop else
             f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color={colour}")
    hdr = color_transfer in HDR_TRANSFERS
    tone = ("zscale=t=linear:npl=203,format=gbrpf32le,zscale=p=bt709,"
            "tonemap=hable:desat=2,zscale=t=bt709:m=bt709:r=tv,format=yuv420p," if hdr else "")
    framing = f"{focus_crop}{scale},{tone}{frame}"
    vprefix = (f"[0:{video_index}]fps={FPS}:start_time=0,"
               f"tpad=stop_mode=clone:stop_duration={manifest.source_duration_ms / 1000:.3f}")
    aprefix = f"[0:{audio_index}]aresample={SAMPLE_RATE}:async=1:first_pts=0,apad"
    if count > 1:
        video = vprefix + f",split={count}" + "".join(f"[v{i}]" for i in range(count))
        audio = aprefix + f",asplit={count}" + "".join(f"[a{i}]" for i in range(count))
    else:
        video, audio = vprefix + "[v0]", aprefix + "[a0]"

    nodes = [video, audio]
    for i, span in enumerate(manifest.frame_ranges):
        unit = SAMPLE_RATE // FPS
        samples = span.duration * unit
        fades = "" if count == 1 else (
            f",afade=t=in:ss=0:ns={min(384, samples // 4)}" if i else ""
        ) + (
            f",afade=t=out:ss={samples - min(384, samples // 4)}:ns={min(384, samples // 4)}"
            if i < count - 1 else ""
        )
        nodes.extend([
            f"[v{i}]trim=start_frame={span.start_frame}:end_frame={span.end_frame},setpts=PTS-STARTPTS[outv{i}]",
            f"[a{i}]atrim=start_sample={span.start_frame * unit}:end_sample={span.end_frame * unit},asetpts=N/SR/TB{fades}[outa{i}]",
        ])
    if count > 1:
        nodes.append("".join(f"[outv{i}][outa{i}]" for i in range(count)) + f"concat=n={count}:v=1:a=1[vjoin][ajoin]")
        vout, aout = "[vjoin]", "[ajoin]"
    else:
        vout, aout = "[outv0]", "[outa0]"
    zoom_filter = camera_zoom(manifest, keys) if camera else ""
    if manifest.zooms:
        terms = []
        for start, end, percent in manifest.edited_zooms():
            ramp = min(6, max(1, (end - start) // 3))
            strength = (percent - 100) / 100
            terms.append(f"if(between(on,{start},{end - 1}),{strength:.2f}*"
                         f"min(1,min((on-{start}+1)/{ramp},({end}-on)/{ramp})),0)")
        expression = "+".join(["1", *terms])
        zoom_filter = (f"zoompan=z='{expression}':x='(iw-iw/zoom)/2':y='(ih-ih/zoom)/2'"
                       f":d=1:s=1080x1920:fps={FPS},")
    speed = manifest.settings.playback_speed
    video_speed = (f"settb=AVTB,setpts=PTS/{speed:g},fps={FPS},tpad=stop_mode=clone:stop=1,"
                   if speed != 1 else "")
    # Convert the source's display aspect to square pixels before fitting or cropping.
    nodes.append(f"{vout}settb=1/{FPS},setpts=N,scale=w='max(2,trunc(iw*sar/2)*2)':h=ih,setsar=1,"
                 f"{framing},setsar=1,{zoom_filter}{video_speed}ass=filename=captions.ass:fontsdir=fonts,format=yuv420p[v]")
    leveling = "acompressor=threshold=0.126:ratio=2:attack=15:release=150:makeup=1.25," if manifest.voice_leveling else ""
    audio_speed = (f"atempo={speed:g},apad,atrim=end_sample={manifest.output_frames * (SAMPLE_RATE // FPS)},"
                   if speed != 1 else "")
    nodes.append(f"{aout}{audio_speed}{leveling}alimiter=limit=0.95:level=0:attack=1,asetpts=N/SR/TB[a]")
    preset, crf, audio_rate = ("medium", "17", "192k") if manifest.settings.quality == "high" else ("veryfast", "22", "160k")
    return [
        executable, "-hide_banner", "-nostdin", "-n", "-v", "error", "-nostats", "-progress", "pipe:1",
        "-protocol_whitelist", "file", "-f", "mov", "-enable_drefs", "0",
        "-copyts", "-start_at_zero", "-i", str(source), "-filter_complex_threads", "2",
        "-filter_complex", ";".join(nodes), "-map", "[v]", "-map", "[a]",
        "-t", f"{manifest.output_frames / FPS:.6f}", "-c:v", "libx264", "-threads:v", "4", "-preset", preset,
        "-crf", crf, "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", audio_rate, "-ar", str(SAMPLE_RATE),
        *(["-colorspace", "bt709", "-color_trc", "bt709", "-color_primaries", "bt709", "-color_range", "tv"] if hdr else []),
        "-map_metadata", "-1", "-map_chapters", "-1", "-movflags", "+faststart", "preview.mp4",
    ]


def run_ffmpeg(args, directory: Path, duration_ms: int, report):
    log = directory / "ffmpeg.log"
    try:
        with log.open("wb") as errors, subprocess.Popen(
            args, cwd=directory, stdout=subprocess.PIPE, stderr=errors,
        ) as process, selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            progress, pending = 0.0, b""
            try:
                while True:
                    report(progress)
                    if not selector.select(0.25):
                        continue
                    chunk = os.read(process.stdout.fileno(), 4096)
                    if not chunk:
                        break
                    records = (pending + chunk).split(b"\n")
                    pending = records.pop()
                    for line in records:
                        if line.startswith(b"out_time_us="):
                            value = line.split(b"=", 1)[1].strip()
                            if value.lstrip(b"-").isdigit():
                                progress = max(progress, min(0.99, int(value) / (duration_ms * 1000)))
                if process.wait():
                    message = log.read_text(encoding="utf-8", errors="replace").strip()[-600:]
                    if "No such filter: 'zscale'" in message:
                        raise AudioError("HDR footage requires an FFmpeg build with the zscale filter (libzimg).")
                    raise AudioError("FFmpeg rendering failed. " + message)
            except BaseException:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                raise
    except FileNotFoundError as error:
        raise AudioError("FFmpeg with libass and libx264 is required to render.") from error
