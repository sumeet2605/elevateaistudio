from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .captions import ass_time, colour, escape_ass, width


class CaptionStyle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["static", "keyword", "active", "reveal"] = "keyword"
    animation: Literal["none", "fade", "scale"] = "fade"
    animation_ms: int = Field(default=140, ge=60, le=240)
    font_family: Literal["auto", "Noto Sans", "Noto Sans Devanagari"] = "auto"
    font_size: int = Field(default=72, ge=42, le=90)
    weight: Literal["regular", "bold"] = "bold"
    primary: str = Field(default="#FFFFFF", pattern=r"^#[A-Fa-f0-9]{6}$")
    highlight: str = Field(default="#EFC77F", pattern=r"^#[A-Fa-f0-9]{6}$")
    outline_color: str = Field(default="#161616", pattern=r"^#[A-Fa-f0-9]{6}$")
    outline: int = Field(default=5, ge=0, le=10)
    shadow: int = Field(default=1, ge=0, le=8)
    background_opacity: int = Field(default=0, ge=0, le=85)
    line_spacing: int = Field(default=12, ge=0, le=28)
    max_lines: int = Field(default=2, ge=1, le=3)
    alignment: Literal["left", "center", "right"] = "center"
    vertical: int = Field(default=79, ge=20, le=90)
    safe_top: int = Field(default=9, ge=4, le=20)
    safe_bottom: int = Field(default=15, ge=8, le=30)
    safe_left: int = Field(default=9, ge=4, le=20)
    safe_right: int = Field(default=13, ge=4, le=25)
    words_per_cue: int = Field(default=7, ge=2, le=15)
    pause_ms: int = Field(default=370, ge=120, le=1000)
    min_ms: int = Field(default=700, ge=300, le=1600)
    reading_cps: int = Field(default=18, ge=10, le=32)
    sentence_first: bool = False

    @model_validator(mode="after")
    def safe_position(self):
        line_step = round(self.font_size * 1.18) + self.line_spacing
        baseline = self.vertical * 1920 / 100
        top = baseline - self.font_size - (self.max_lines - 1) * line_step - self.outline
        if top < self.safe_top * 1920 / 100 or baseline + self.outline + self.shadow > (100-self.safe_bottom) * 1920 / 100:
            raise ValueError("Caption position and line height must fit inside the safe zone.")
        return self


class TimedWord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=96)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    highlighted: bool = False

    @model_validator(mode="after")
    def valid_word(self):
        if self.end_ms <= self.start_ms or any(c.isspace() for c in self.text):
            raise ValueError("Caption words need a positive duration and cannot contain spaces.")
        return self


class Cue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    words: list[TimedWord] = Field(min_length=1, max_length=45)

    @model_validator(mode="after")
    def valid_cue(self):
        if self.end_ms - self.start_ms < 40:
            raise ValueError("Caption cues need at least 40 milliseconds.")
        if any(not self.start_ms <= w.start_ms < w.end_ms <= self.end_ms for w in self.words):
            raise ValueError("Each word must stay inside its caption cue.")
        if any(a.start_ms > b.start_ms for a, b in zip(self.words, self.words[1:])):
            raise ValueError("Caption words must be ordered by time.")
        return self


def normalized(word):
    return word.strip(".,?!।:;—…\"'“”‘’()[]{}").casefold()


def caption_width(style):
    left, right = 1080 * style.safe_left // 100, 1080 * (100 - style.safe_right) // 100
    return (right-left-2*(10+style.outline+style.shadow))/style.font_size


def wrapping(words, max_width, max_lines=None):
    lines = [[]]
    size = 0
    for index, word in enumerate(words):
        w = width(word.text)
        if size + (0.35 if lines[-1] else 0) + w > max_width and lines[-1] and (
                max_lines is None or len(lines) < max_lines):
            lines.append([])
            size = 0
        lines[-1].append((index, word))
        size += w + (0.35 if len(lines[-1]) > 1 else 0)
    return lines


def caption_pages(cue, style, max_width):
    """Reflow at the requested font size, then advance pages on spoken boundaries."""
    lines = wrapping(cue.words, max_width)
    if len(lines) <= style.max_lines:
        yield cue, lines
        return
    # Fill the last page first so the end of the phrase can stay together.
    first_count = (len(lines)-1) % style.max_lines + 1
    pages = [lines[:first_count]]
    pages.extend(lines[i:i+style.max_lines] for i in range(first_count,len(lines),style.max_lines))
    starts = [cue.start_ms] + [page[0][0][1].start_ms for page in pages[1:]]
    stops = starts[1:] + [cue.end_ms]
    if (any(end//10 <= start//10 for start,end in zip(starts,stops))
            or any(max(word.end_ms for line in page for _,word in line)>end
                   for page,end in zip(pages,stops))):
        # Simultaneous words have no usable page boundary at ASS precision.
        yield cue, wrapping(cue.words,max_width,style.max_lines)
        return
    for page,start,end in zip(pages,starts,stops):
        words = [word for line in page for _,word in line]
        timed = cue.model_copy(update={"start_ms":start,"end_ms":end,"words":words})
        yield timed, wrapping(words,max_width,style.max_lines)


def retime_cues(cues, speed, duration_ms):
    def shift(value):
        return int(value / speed)
    result = []
    for cue in cues:
        start, end = shift(cue.start_ms), min(duration_ms, shift(cue.end_ms))
        if end - start < 30:
            continue
        result.append(cue.model_copy(update={"start_ms": start, "end_ms": end, "words": [
            w.model_copy(update={"start_ms": min(end-1, shift(w.start_ms)),
                                 "end_ms": min(end, max(shift(w.start_ms)+1, shift(w.end_ms)))})
            for w in cue.words]}))
    return result


def events_for(cue, mode):
    start, end = cue.start_ms // 10, cue.end_ms // 10
    if mode in ("static", "keyword"):
        return [(start, end)]
    points = {start, end}
    for word in cue.words:
        points.add(word.start_ms // 10)
        if mode == "active":
            points.add(word.end_ms // 10)
    ordered = sorted(point for point in points if start <= point <= end)
    return list(zip(ordered, ordered[1:]))


def motion(style, cue, start_cs, end_cs, scale):
    if style.animation == "none":
        return ""
    begin = cue.start_ms // 10 * 10
    finish = cue.end_ms // 10 * 10
    ramp = min(style.animation_ms, max(1, (finish - begin) // 3))
    start, end = start_cs * 10, end_cs * 10
    def level(time):
        return max(0.0, min(1.0, (time - begin) / ramp, (finish - time) / ramp))
    phases = [start] + [t for t in (begin + ramp, finish - ramp) if start < t < end] + [end]
    if style.animation == "fade":
        target = lambda v: f"\\alpha&H{round(255*(1-v)):02X}&"
    else:
        # Scale only vertically to retain fixed word spacing and line width.
        target = lambda v: f"\\fscx{scale}\\fscy{round(96+4*v)}"
    return target(level(start)) + "".join(
        f"\\t({a-start},{b-start},{target(level(b))})"
        for a, b in zip(phases, phases[1:]) if level(a) != level(b))


def dynamic_ass(style, cues):
    main, gold = colour(style.primary), colour(style.highlight)
    outline = colour(style.outline_color)
    box = f"&H{round(255*(1-style.background_opacity/100)):02X}151515"
    base = "Noto Sans" if style.font_family == "auto" else style.font_family
    weight = -1 if style.weight == "bold" else 0
    border_style = 3 if style.background_opacity else 1
    stroke = box if style.background_opacity else outline
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{base},{style.font_size},{main},{gold},{stroke},&H66000000,{weight},0,0,0,100,100,0,0,{border_style},{style.outline},{style.shadow},2,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    rows = []
    left, right = 1080 * style.safe_left // 100, 1080 * (100 - style.safe_right) // 100
    an, x = {"left": (1, left + 10), "center": (2, (left+right)//2), "right": (3, right - 10)}[style.alignment]
    baseline = style.vertical * 1920 // 100
    max_width = caption_width(style)
    for cue, lines in (page for cue in cues for page in caption_pages(cue,style,max_width)):
        for start, end in events_for(cue, style.mode):
            if end <= start:
                continue
            active = next((i for i, word in enumerate(cue.words)
                           if word.start_ms // 10 <= start < word.end_ms // 10), None)
            for li, line in enumerate(lines):
                y = baseline - (len(lines)-li-1) * (round(style.font_size*1.18) + style.line_spacing)
                length = width(" ".join(word.text for _, word in line))
                scale = min(100, int(100 * max_width / max(length, 1)))
                prefix = f"{{\\an{an}\\pos({x},{y})\\fscx{scale}{motion(style,cue,start,end,scale)}}}"
                chunks = []
                for i, word in line:
                    hindi = any("\u0900" <= char <= "\u097f" for char in word.text)
                    font = "Noto Sans Devanagari" if hindi else ("Noto Sans" if base == "Noto Sans Devanagari" else base)
                    highlighted = style.mode != "static" and (word.highlighted or (style.mode == "active" and i == active))
                    hidden = style.mode == "reveal" and word.start_ms // 10 > start
                    if hidden:
                        alpha = "\\alpha&HFF&"
                    elif style.mode == "reveal":
                        alpha = motion(style, cue, start, end, scale) if style.animation == "fade" else "\\alpha&H00&"
                    else:
                        alpha = ""
                    chunks.append(f"{{\\fn{font}\\1c{gold if highlighted else main}&{alpha}}}" + escape_ass(word.text))
                rows.append(f"Dialogue: 0,{ass_time(start*10)},{ass_time(end*10)},Main,,0,0,0,,{prefix}" + " ".join(chunks))
    return header + "\n".join(rows) + "\n"
