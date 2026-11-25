import math
import unicodedata
from dataclasses import dataclass

from .render_manifest import RenderManifest, milliseconds
from .transcription import Transcript


@dataclass(frozen=True)
class CaptionWord:
    text: str
    start_ms: int
    end_ms: int
    highlighted: bool


@dataclass(frozen=True)
class Caption:
    start_ms: int
    end_ms: int
    lines: list[list[CaptionWord]]


def width(text):
    return sum(0.35 if char.isspace() else 0 if unicodedata.combining(char)
               else 1 if ord(char) >= 0x900 else 0.62 for char in text)


def captions_for(manifest: RenderManifest, transcript: Transcript, grouping=None):
    # A missing word clock can use the recorded segment as one timed unit.
    # If that untimed unit straddles a seam, omit it rather than invent word timing.
    atoms = []
    for segment in transcript.segments:
        for word in segment.words:
            atoms.append((word.start, word.end, [word.text.strip()]))
        if not segment.words and segment.text.strip():
            atoms.append((segment.start, segment.end, segment.text.strip().split()))
    cues = []
    omitted = 0
    offset = 0
    used = set()
    style = manifest.settings
    word_limit = grouping.words_per_cue if grouping else style.caption_words
    pause_limit = grouping.pause_ms if grouping else style.caption_gap_ms
    width_limit = (grouping.max_lines * (1080 * (100 - grouping.safe_left - grouping.safe_right)
                   / 100 / grouping.font_size - 1)) if grouping else 29
    minimum = grouping.min_ms if grouping else style.caption_min_ms
    for keep in manifest.frame_ranges:
        start, end = milliseconds(keep.start_frame), milliseconds(keep.end_frame)
        limit = milliseconds(offset + keep.duration)
        shift = milliseconds(offset) - start
        mapped = []
        for index, (a, b, texts) in enumerate(atoms):
            low, high = math.floor(a * 1000), math.ceil(b * 1000)
            if not texts or not texts[0] or not start <= low < high <= end:
                continue
            used.add(index)
            highlight = any(low < mark.end_ms and mark.start_ms < high for mark in manifest.emphasis)
            mapped.append((texts, low + shift, min(limit, high + shift), highlight))

        groups = []
        current = []
        for texts, low, high, highlight in mapped:
            if current and (low - current[-1][2] > pause_limit
                            or (grouping and current[-1][0][-1].endswith((".", "?", "!", "।"))
                                and (grouping.sentence_first or low - current[-1][2] >= 100))
                            or high - current[0][1] > 3200
                            or sum(len(item[0]) for item in current) + len(texts) > word_limit
                            or width(" ".join(" ".join(item[0]) for item in current) + " " + " ".join(texts)) > width_limit):
                groups.append(current)
                current = []
            current.append((texts, low, high, highlight))
        if current:
            groups.append(current)

        for index, group in enumerate(groups):
            begin = group[0][1]
            following = groups[index + 1][0][1] if index + 1 < len(groups) else limit
            reading_ms = min(1600, math.ceil(1000 * len(" ".join(
                text for atom in group for text in atom[0])) / grouping.reading_cps)) if grouping else 0
            stop = min(limit, following, max(group[-1][2], begin + max(minimum, reading_ms)))
            if stop - begin < 40:
                omitted += sum(len(item[0]) for item in group)
                continue
            words = [CaptionWord(text, low, high, mark) for texts, low, high, mark in group for text in texts]
            lines = [words]
            if width(" ".join(word.text for word in words)) > 14 and len(words) > 1:
                split = min(range(1, len(words)), key=lambda i:
                            abs(width(" ".join(w.text for w in words[:i])) - width(" ".join(w.text for w in words[i:]))))
                lines = [words[:split], words[split:]]
            # ASS stores centiseconds. Floor both times to preserve non-overlap.
            speed = style.playback_speed
            cues.append(Caption(int(begin / speed) // 10 * 10, int(stop / speed) // 10 * 10, lines))
        offset += keep.duration
    omitted += sum(len(item[2]) for index, item in enumerate(atoms) if index not in used)
    return cues, omitted


def colour(hex_colour):
    return "&H00" + hex_colour[5:7] + hex_colour[3:5] + hex_colour[1:3]


def ass_time(ms):
    centiseconds = ms // 10
    return f"{centiseconds // 360000}:{centiseconds // 6000 % 60:02}:{centiseconds // 100 % 60:02}.{centiseconds % 100:02}"


def escape_ass(text):
    return text.replace("\\", "\\⁠").replace("{", r"\{").replace("}", r"\}").replace("\n", " ").replace("\r", " ")


def ass_asset(brand, cues):
    primary, highlight = colour(brand.primary), colour(brand.highlight)
    align = {"lower-third": 2, "center": 5, "upper-third": 8}[brand.subtitle_placement]
    margin = 320 if align == 2 else 260 if align == 8 else 0
    size = 62 if brand.caption_style == "minimal" else 68
    outline = 3 if brand.caption_style == "minimal" else 5
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,Noto Sans,{size},{primary},{highlight},&H00000000,&H72000000,-1,0,0,0,100,100,0,0,1,{outline},1,{align},90,90,{margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for cue in cues:
        wrapped = []
        for line in cue.lines:
            length = width(" ".join(word.text for word in line))
            scale = min(100, int(1240 / max(length, 1)))
            chunks = []
            for word in line:
                font = "Noto Sans Devanagari" if any("\u0900" <= char <= "\u097f" for char in word.text) else "Noto Sans"
                value = highlight if word.highlighted else primary
                chunks.append(f"{{\\fn{font}\\fscx{scale}\\1c{value}&}}" + escape_ass(word.text))
            wrapped.append(" ".join(chunks))
        text = r"\N".join(wrapped)
        lines.append(f"Dialogue: 0,{ass_time(cue.start_ms)},{ass_time(cue.end_ms)},Main,,0,0,0,,{text}")
    return header + "\n".join(lines) + "\n"
