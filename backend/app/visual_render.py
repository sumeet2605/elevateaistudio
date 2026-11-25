from .render_manifest import FPS


def compose(nodes, manifest, paths, base, tone_map):
    """Replace picture at output frame boundaries; the existing audio graph is separate."""
    output = base
    for index, (visual, path) in enumerate(zip(manifest.visuals, paths, strict=True), 1):
        event, asset = visual.event, visual.asset
        start, end = manifest.visual_frames(event.source)
        span = end - start
        scaling = "decrease" if event.crop == "fit" else "increase"
        frame = "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black" if event.crop == "fit" else "crop=1080:1920"
        filters = ["scale=w='max(2,trunc(iw*sar/2)*2)':h=ih", "setsar=1",
                   f"scale=1080:1920:force_original_aspect_ratio={scaling}:force_divisible_by=2:out_color_matrix=bt709",
                   *( [tone_map] if asset.hdr else []), frame, "setsar=1", "format=yuv420p"]
        if asset.kind == "image":
            if event.movement:
                filters.append(f"zoompan=z='1+0.055*on/{max(1,span-1)}':x='(iw-iw/zoom)/2':"
                               f"y='(ih-ih/zoom)/2':d={span}:s=1080x1920:fps={FPS}")
            else:
                filters.extend((f"fps={FPS}", f"tpad=stop_mode=clone:stop={span}"))
        else:
            filters.append(f"fps={FPS}:start_time=0")
        filters.extend((f"trim=end_frame={span}", f"tpad=start_mode=clone:start={start}", f"settb=1/{FPS}", "setpts=N"))
        nodes.append(f"[{index}:v:0]" + ",".join(filters) + f"[insert{index}]")
        nodes.append(f"{output}[insert{index}]overlay=eof_action=repeat:repeatlast=1:"
                     f"enable='between(n,{start},{end-1})'[shot{index}]")
        output = f"[shot{index}]"
    return output
