"""Bounded, source-clock retake suggestions. No decisions are applied here."""
import hashlib
import math
import unicodedata
from difflib import SequenceMatcher
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .timeline import Span

VERSION = "retakes-v1"
FILLERS = {"um", "uh", "erm", "hmm", "ah", "अं", "उम"}
NEGATION = {"not", "never", "no", "cant", "cannot", "wont", "नहीं", "मत"}
EMPHASIS = {"believe", "breathe", "keep", "always", "never", "repeat", "again", "दोहराएँ"}


class Options(BaseModel):
    model_config = ConfigDict(extra="forbid")
    padding_ms: int = Field(default=40, ge=0, le=120)


class Attempt(BaseModel):
    first: int = Field(ge=0)
    last: int = Field(ge=0)
    source: Span
    text: str

    @model_validator(mode="after")
    def ordered(self):
        if self.first > self.last:
            raise ValueError("Word indices must be ordered.")
        return self


class Candidate(BaseModel):
    schema_version: Literal[1] = 1
    id: str = Field(pattern=r"^[a-f0-9]{24}$")
    source_id: str
    transcript_revision: str
    base_edit_revision: str
    detector_version: Literal["retakes-v1"] = VERSION
    earlier: Attempt
    later: Attempt
    reasons: list[str]
    confidence: Literal["high", "medium", "ambiguous"]
    recommendation: Literal["first", "second", "both", "manual"]
    status: Literal["pending", "accepted", "rejected"] = "pending"
    selected: Literal["first", "second", "both"] | None = None
    adjustment: Span | None = None
    cut: Span | None = None

    @model_validator(mode="after")
    def ordered(self):
        if self.earlier.last >= self.later.first or self.earlier.source.end_ms > self.later.source.start_ms:
            raise ValueError("Attempts must be disjoint and ordered.")
        return self


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected: Literal["first", "second", "both"] = "both"
    adjustment: Span | None = None
    status: Literal["accepted", "rejected"] = "accepted"


def normalize(text):
    return "".join(c for c in unicodedata.normalize("NFC", text).casefold()
                   if c.isalnum() or unicodedata.category(c).startswith("M"))


def timed_words(transcript):
    duration = math.floor(transcript.source_duration * 1000)
    words = []
    for segment in transcript.segments:
        for word in segment.words:
            start, end = round(word.start * 1000), min(duration, round(word.end * 1000))
            if end > start and normalize(word.text):
                words.append((start, end, word.text.strip(), word.probability))
    return words


def overlap(a, b):
    return a.start_ms < b.end_ms and b.start_ms < a.end_ms


def detect(words, source_id, transcript_revision, base_edit_revision, excluded=(), tick=lambda: None):
    keys = [normalize(w[2]) for w in words]
    size = len(words)
    cursor, found = 0, []
    def attempt(first, end):
        return Attempt(first=first,last=end-1,source=Span(start_ms=words[first][0],end_ms=words[end-1][1]),
                       text=" ".join(w[2] for w in words[first:end]))
    while cursor+5 < size and len(found)<240:
        if cursor % 64 == 0:
            tick()
        i=cursor
        best=None
        for j in range(i+3,min(i+15,size-2)):
            if words[j][0]-words[i][0]>12000:
                break
            if keys[i]!=keys[j] or keys[i+1]!=keys[j+1]:
                continue
            before=keys[i:j]
            length=len(before)
            fillers=0
            while length>3 and fillers<2 and before[length-1] in FILLERS:
                length-=1
                fillers+=1
            if len(set(before[:length]))<3 or words[j][0]-words[i+length-1][1]>2200:
                continue
            right=keys[j:j+length]
            if len(right)<length:
                continue
            exact=before[:length]==right
            shared=0
            for a,b in zip(before[:length],right):
                if a!=b:
                    break
                shared+=1
            similarity=SequenceMatcher(None,before[:length],right,autojunk=False).ratio()
            if not exact and (shared<3 or similarity<.72 or length<4):
                continue
            next_repeats=keys[j+length:j+2*length]==before[:length]
            marker=words[i+length-1][2].endswith(("-", "—", "…"))
            incomplete=exact and (marker or (not next_repeats and j+length<size and
                       not words[i+length-1][2].endswith((".", "!", "?", "।")) and
                       words[j+length][0]-words[j+length-1][1]<320))
            reasons=["exact repetition" if exact else "corrected restart"]
            if fillers:
                reasons.append("filler-separated restart")
            if incomplete:
                reasons.append("incomplete attempt")
                right_end=j+length
                while right_end<min(size,j+length+5) and words[right_end][0]-words[right_end-1][1]<320 and not words[right_end-1][2].endswith((".", "!", "?", "।")):
                    right_end+=1
            else:
                right_end=j+length
            if next_repeats:
                reasons.append("multiple takes")
            earlier,later=attempt(i,j),attempt(j,right_end)
            if any(overlap(a.source,s) for a in (earlier,later) for s in excluded):
                continue
            probs=[w[3] for w in words[i:right_end] if w[3] is not None]
            rhetorical=bool(set(before[:length]) & EMPHASIS) or length==3 and not (fillers or incomplete or next_repeats)
            uncertain=bool(probs and min(probs)<.55)
            if rhetorical:
                reasons.append("possible deliberate emphasis")
            if uncertain:
                reasons.append("uncertain recognition")
            later_incomplete=words[right_end-1][2].endswith(("-", "—", "…"))
            if later_incomplete and not incomplete:
                reasons.append("later attempt incomplete")
            semantic = {w for w in before[:length] if w in NEGATION or w.isdecimal()} != {
                w for w in right if w in NEGATION or w.isdecimal()
            }
            if semantic:
                reasons.append("possible change of meaning")
            earlier_quality=[w[3] for w in words[i:i+length] if w[3] is not None]
            later_quality=[w[3] for w in words[j:right_end] if w[3] is not None]
            if earlier_quality and later_quality:
                quality_gap=sum(earlier_quality)/len(earlier_quality)-sum(later_quality)/len(later_quality)
            else:
                quality_gap=0
            confidence="ambiguous" if rhetorical or uncertain or semantic else ("high" if exact and not fillers and length>=5 else "medium")
            recommendation=("both" if rhetorical else "manual" if uncertain or not exact or semantic else
                            "first" if (later_incomplete or quality_gap>.13) and not incomplete else "second")
            identity=hashlib.sha256(f"{VERSION}:{source_id}:{transcript_revision}:{base_edit_revision}:{i}:{j}:{right_end}".encode()).hexdigest()[:24]
            candidate=Candidate(id=identity,source_id=source_id,transcript_revision=transcript_revision,
                                base_edit_revision=base_edit_revision,earlier=earlier,later=later,
                                reasons=reasons,confidence=confidence,recommendation=recommendation)
            score=(exact,length,not fillers)
            alternative=None
            if next_repeats:
                third_start=j+length
                third_end=third_start+length
                while (third_end<min(size,third_start+length+5)
                       and words[third_end][0]-words[third_end-1][1]<320
                       and not words[third_end-1][2].endswith((".", "!", "?", "।"))):
                    third_end+=1
                third=attempt(third_start,third_end)
                if words[third_start][0]-words[j+length-1][1]<=2200 and not any(overlap(third.source,s) for s in excluded):
                    alternate_id=hashlib.sha256(f"{identity}:take:{third_start}:{third_end}".encode()).hexdigest()[:24]
                    alternative=candidate.model_copy(update={"id":alternate_id,"later":third})
            if best is None or score>best[0]:
                best=(score,candidate,alternative)
        if best:
            found.append(best[1])
            if best[2] and len(found)<240:
                found.append(best[2])
            cursor=best[1].later.first
        else:
            cursor+=1
    return found


def safe_cut(candidate, decision, words, duration, vad, existing, options, unindexed=()):
    if decision.status=="rejected" or decision.selected=="both":
        if decision.adjustment:
            raise ValueError("Custom boundaries require choosing one take.")
        return None
    remove=candidate.later if decision.selected=="first" else candidate.earlier
    previous=words[remove.first-1][1] if remove.first else 0
    following=words[remove.last+1][0] if remove.last+1<len(words) else duration
    start=max(math.ceil(math.ceil(previous*30/1000)*1000/30),remove.source.start_ms-options.padding_ms)
    end=min(math.floor(math.floor(following*30/1000)*1000/30),remove.source.end_ms+options.padding_ms)
    if decision.adjustment is None and end<=start:
        raise ValueError("No frame-safe boundaries surround this take.")
    cut=decision.adjustment or Span(start_ms=start,end_ms=end)
    frame_start=cut.start_ms*30//1000*1000/30
    frame_end=((cut.end_ms*30+999)//1000)*1000/30
    if not previous<=frame_start<=cut.start_ms<=remove.source.start_ms or not remove.source.end_ms<=cut.end_ms<=frame_end<=following:
        raise ValueError("The cut or frame rounding would touch neighbouring speech or split the selected attempt.")
    if cut.duration<100 or any(overlap(cut,s) for s in existing):
        raise ValueError("This cut is too short or overlaps an existing accepted cut.")
    expanded=Span(start_ms=math.floor(frame_start),end_ms=min(duration,math.ceil(frame_end)))
    if any(overlap(expanded,s) for s in unindexed):
        raise ValueError("The cut touches speech without word-level timing.")
    # A long VAD-only region can be quiet or untranscribed speech. Don't remove it.
    cursor=expanded.start_ms
    gaps=[]
    for a,b,_,_ in words[remove.first:remove.last+1]:
        if a>cursor:
            gaps.append((cursor,a))
        cursor=max(cursor,b)
    gaps.append((cursor,expanded.end_ms))
    if any(min(b,s.end_ms)-max(a,s.start_ms)>110 for a,b in gaps for s in vad):
        raise ValueError("Detected speech without matching words overlaps the cut.")
    return cut
