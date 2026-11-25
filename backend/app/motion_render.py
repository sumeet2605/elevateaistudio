from bisect import bisect_right

from .motion_models import Focus, clamp_crop
from .render_manifest import FPS


def tracked(manifest):
    """Compress the pinned track to roughly one keyframe per retained second."""
    track = manifest.face_track
    if not track:
        return []
    times = [time for time, _ in track]
    keys = []
    offset = 0
    for span in manifest.frame_ranges:
        for index in sorted({*range(0, span.duration, FPS), span.duration - 1}):
            when = (span.start_frame + index) * 1000 / FPS
            position = bisect_right(times, when) - 1
            point = track[position][1] if position >= 0 else manifest.camera.focus
            keys.append((offset + index, point))
        offset += span.duration
    return keys


def curve(keys, axis, frame):
    def value(key):
        return getattr(key[1], axis)

    def compile_range(first, last):
        if last - first == 1:
            a, b = keys[first], keys[last]
            delta = value(b) - value(a)
            if not delta:
                return f"{value(a):.6f}"
            return f"({value(a):.6f}+({delta:.6f})*clip(({frame}-{a[0]})/{b[0]-a[0]},0,1))"
        middle = (first + last) // 2
        return f"if(lt({frame},{keys[middle][0]}),{compile_range(first,middle)},{compile_range(middle,last)})"

    if len(keys) == 1:
        return f"{value(keys[0]):.6f}"
    return compile_range(0, len(keys) - 1)


def baseline_crop(camera, keys):
    if camera.framing not in ("manual", "face"):
        return ""
    x = curve(keys, "x", "n") if keys else f"{camera.focus.x:.6f}"
    y = curve(keys, "y", "n") if keys else f"{camera.focus.y:.6f}"
    vertical = "0.40" if camera.framing == "face" else "0.50"
    return ("crop=w='max(2,trunc(min(iw,ih*9/16)/2)*2)':h='max(2,trunc(min(ih,iw*16/9)/2)*2)'"
            f":x='clip(iw*({x})-ow/2,0,iw-ow)':y='clip(ih*({y})-oh*{vertical},0,ih-oh)',")


def display_point(point, manifest, baseline):
    if not manifest.display_geometry:
        return point
    width, height = manifest.display_geometry
    aspect = width / height
    ratio = 9 / 16
    mode = manifest.camera.framing
    if mode == "fit" or (mode == "original" and manifest.settings.framing == "fit"):
        xscale, yscale = min(1, aspect / ratio), min(1, ratio / aspect)
        return Focus(x=0.5 + (point.x - 0.5)*xscale, y=0.5 + (point.y - 0.5)*yscale)
    w, h = min(1, ratio/aspect), min(1, aspect/ratio)
    x, y = clamp_crop(baseline, w, h, headroom=mode == "face")
    return Focus(x=max(0, min(1, (point.x-x)/w)), y=max(0, min(1, (point.y-y)/h)))


def smooth_delta(event, start, end):
    width = end - start
    u = f"clip((on-{start})/{max(1,width-1)},0,1)"
    eased = {
        "linear": u,
        "ease-in": f"pow({u},2)",
        "ease-out": f"(1-pow(1-{u},2))",
        "ease-in-out": f"(pow({u},2)*(3-2*{u}))",
    }[event.easing]
    if event.transition == "hold":
        first, diff = (event.end_zoom - 100)/100, 0
    else:
        first = (event.start_zoom - 100)/100
        diff = (event.end_zoom - event.start_zoom)/100
    ramp = min(6, max(1, (width - 1)//3))
    envelope = f"clip(min(on-{start},{end}-1-on)/{ramp},0,1)"
    return f"({first:.3f}+({diff:.3f})*{eased})*{envelope}"


def camera_zoom(manifest, keys):
    zooms, xs, ys = [], [], []
    for event in manifest.motion_events:
        if event.status != "accepted" or max(event.start_zoom, event.end_zoom) == 100:
            continue
        start, end = manifest.map_motion_span(event.source)
        active = f"between(on,{start},{end-1})"
        strength = smooth_delta(event, start, end) if event.transition != "punch" else f"{(event.end_zoom-100)/100:.3f}"
        zooms.append(f"if({active},{strength},0)")
        if event.strategy == "face" and keys:
            points = [(n, display_point(point, manifest, point if manifest.camera.framing == "face"
                       else manifest.camera.focus if manifest.camera.framing == "manual" else Focus())) for n, point in keys]
            # Bound expression size to this event and neighboring points.
            indices = [n for n, _ in points]
            left = max(0, bisect_right(indices, start)-1)
            right = bisect_right(indices, end)+1
            points = points[left:right]
        else:
            point = event.focus if event.strategy == "manual" else Focus()
            points = [(0, point)]
        xs.append(f"if({active},({curve(points,'x','on')})-0.5,0)")
        ys.append(f"if({active},({curve(points,'y','on')})-0.5,0)")
    if not zooms:
        return ""
    zx, zy, zoom = "+".join(["0.5", *xs]), "+".join(["0.5", *ys]), "+".join(["1", *zooms])
    return (f"zoompan=z='{zoom}':x='clip(iw*({zx})-iw/zoom/2,0,iw-iw/zoom)'"
            f":y='clip(ih*({zy})-ih/zoom/2,0,ih-ih/zoom)':d=1:s=1080x1920:fps={FPS},")
