import json
import shutil
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from time import perf_counter

from .captions import ass_asset, captions_for
from .dynamic_captions import CaptionStyle, Cue, dynamic_ass, retime_cues, wrapping
from .ffmpeg_render import HDR_TRANSFERS, ffmpeg_args, run_ffmpeg, streams
from .render_manifest import FPS
from .render_store import Renders, digest
from .transcription import Transcript

FONTS = Path(__file__).parent / "render_fonts"


def camera_notice(manifest, video):
    if not manifest.camera:
        return None
    width, height = video["width"], video["height"]
    sar = video.get("sample_aspect_ratio", "1:1")
    left, _, right = sar.partition(":")
    if left.isdecimal() and right.isdecimal() and int(left) and int(right):
        width *= int(left) / int(right)
    rotation = next((item["rotation"] for item in video.get("side_data_list", []) if "rotation" in item),
                    video.get("tags", {}).get("rotate", "0"))
    if str(rotation).lstrip("-").isdecimal() and int(rotation) % 180 == 90:
        width, height = height, width
    mode = manifest.camera.framing
    fit = mode == "fit" or (mode == "original" and manifest.settings.framing == "fit")
    source_scale = (max if fit else min)(width / 1080, height / 1920)
    peak = max((max(event.start_zoom, event.end_zoom) for event in manifest.motion_events
                if event.status == "accepted"), default=100)
    if source_scale < peak / 100:
        return "Selected framing upscales part of the source. Review fine detail before export."
    return None


def render_video(settings, jobs, job, stage):
    started = perf_counter()
    store = Renders(jobs)
    render_id = job["id"]
    manifest = store.manifest(render_id)
    with store.projects.connect() as db:
        issue = store.stale(db, manifest)
    if issue:
        raise ValueError(issue)
    project = store.projects.get(manifest.project_id)
    source = store.projects.source_path(manifest.project_id, project["media"]).resolve()
    stage("checking_source")
    if digest(source) != manifest.source_media_id:
        raise ValueError("The original no longer matches the approved plan.")
    transcript = Transcript.model_validate_json(store.transcript_path(
        manifest.project_id, manifest.transcript_revision).read_bytes())
    if (transcript.project_id != manifest.project_id or transcript.source_media_id != manifest.source_media_id
            or abs(transcript.source_duration * 1000 - manifest.source_duration_ms) >= 1):
        raise ValueError("The transcript does not match the original source.")
    stage("building_captions")
    caption_start = perf_counter()
    style = None
    if manifest.caption_revision:
        revision = store.pinned_caption(manifest)
        style = CaptionStyle.model_validate(revision["style"])
        cues = retime_cues([Cue.model_validate(cue) for cue in revision["cues"]],
                           manifest.settings.playback_speed, manifest.encoded_ms)
        omitted = 0
    else:
        cues, omitted = captions_for(manifest, transcript)
    asset = dynamic_ass(style, cues) if style else ass_asset(manifest.brand, cues)
    caption_seconds = round(perf_counter() - caption_start, 6)
    if not cues:
        raise ValueError("No usable recorded caption timestamps remain after the cuts.")
    video, audio, _ = streams(settings.ffprobe, source)
    directory = store.directory(manifest.project_id, render_id)
    directory.parent.mkdir(parents=True, exist_ok=True)
    work = directory.with_name(f".staging-{render_id}")
    work.mkdir()
    published = False
    try:
        (work / "captions.ass").write_text(asset, encoding="utf-8")
        (work / "manifest.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
        (work / "fonts").mkdir()
        for font in FONTS.glob("*.ttf"):
            shutil.copyfile(font, work / "fonts" / font.name)
        executable = shutil.which(settings.ffmpeg)
        if not executable:
            raise ValueError("FFmpeg with libass and libx264 is required to render.")
        args = ffmpeg_args(str(Path(executable).resolve()), source, manifest, video["index"], audio["index"],
                           color_transfer=video.get("color_transfer"))
        run_ffmpeg(args, work, manifest.encoded_ms, lambda progress: stage("encoding_preview", progress))
        stage("verifying_preview", 0.99)
        preview = work / "preview.mp4"
        verified_video, verified_audio, container = streams(settings.ffprobe, preview)
        actual = float(container["duration"])
        video_length = float(verified_video["duration"])
        audio_length = float(verified_audio["duration"])
        if (verified_video["codec_name"], verified_audio["codec_name"], verified_video["width"],
                verified_video["height"], verified_video["pix_fmt"]) != ("h264", "aac", 1080, 1920, "yuv420p"):
            raise ValueError("FFprobe found unexpected output codecs, dimensions, or pixel format.")
        hdr = video.get("color_transfer") in HDR_TRANSFERS
        if hdr and any(verified_video.get(field) != "bt709"
                       for field in ("color_transfer", "color_space", "color_primaries")):
            raise ValueError("FFprobe could not confirm the HDR source was exported as BT.709 SDR.")
        if Fraction(verified_video["avg_frame_rate"]) != FPS:
            raise ValueError("FFprobe found an unexpected output frame rate.")
        if max(abs(actual - manifest.encoded_ms / 1000), abs(video_length - audio_length)) > 0.07:
            raise ValueError("The encoded duration or stream alignment exceeded the expected frame tolerance.")
        metadata = {
            "created_at": datetime.now(timezone.utc).isoformat(), "sha256": digest(preview),
            "width": 1080, "height": 1920, "video_codec": "h264", "audio_codec": "aac",
            "pixel_format": "yuv420p", "fps": FPS, "color_transfer": verified_video.get("color_transfer"), "duration_seconds": actual,
            "video_seconds": video_length, "audio_seconds": audio_length, "size_bytes": preview.stat().st_size,
            "render_seconds": round(perf_counter() - started, 3), "caption_count": len(cues),
            "omitted_words": omitted, "caption_generation_seconds": caption_seconds,
            "camera_notice": camera_notice(manifest, video),
            "ass_events": asset.count("\nDialogue:"), "approval_fingerprint": manifest.approval_fingerprint,
            "caption_sample": [{"time_ms": cue.start_ms, "lines": [[
                {"text": word.text, "highlighted": word.highlighted} for word in line
            ] for line in ([ [word for _, word in line] for line in wrapping(cue.words, style)[0] ]
                         if style else cue.lines)]} for cue in cues[:3]],
        }
        (work / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        shutil.rmtree(work / "fonts")
        (work / "ffmpeg.log").unlink()
        for entry in work.iterdir():
            entry.chmod(0o444)
        stage("publishing_preview", 0.99)
        store.publish(manifest, render_id, metadata, work, directory)
        published = True
    finally:
        if work.exists():
            shutil.rmtree(work)
        if not published and directory.exists():
            shutil.rmtree(directory)
