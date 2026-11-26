import shutil
import subprocess
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.caption_api import DEFAULT, PRESETS
from app.dynamic_captions import CaptionStyle, Cue, TimedWord, caption_pages, caption_width, dynamic_ass, retime_cues
from app.render_store import Renders
from test_render import FFMPEG, prepared
from test_transcription import studio


def line(*words, end=1000):
    step = end // len(words)
    return Cue(start_ms=0, end_ms=end, words=[
        TimedWord(text=text, start_ms=index*step, end_ms=(index+1)*step, highlighted=index == 0)
        for index, text in enumerate(words)
    ])


def test_presets_and_safe_movement_bounds():
    assert list(PRESETS)[0] == DEFAULT and len(PRESETS) == 4
    for value in PRESETS.values():
        assert isinstance(value["style"], CaptionStyle)
    for overrides in ({"vertical":90}, {"animation_ms":600}, {"primary":"white"}, {"safe_right":50}):
        with pytest.raises(ValidationError):
            CaptionStyle(**overrides)


def test_active_word_keeps_fixed_layout_and_reveal_escapes_text():
    cue = line("same", "problem", "{override}", end=990)
    asset = dynamic_ass(CaptionStyle(mode="active",animation="none",font_size=54), [cue])
    events = [r for r in asset.splitlines() if r.startswith("Dialogue:")]
    assert len(events) == 3
    assert len({event.split(r"\pos", 1)[1].split(")", 1)[0] for event in events}) == 1
    assert "&H007FC7EF&}problem" in events[1] and "&H00FFFFFF&}problem" in events[0]
    revealed = dynamic_ass(CaptionStyle(mode="reveal",animation="none"), [cue])
    assert r"\alpha&HFF&}problem" in revealed and r"\{override\}" in revealed
    faded = dynamic_ass(CaptionStyle(mode="keyword",animation="fade",animation_ms=140), [cue])
    assert r"\t(0,140," in faded
    faster = retime_cues([cue], 1.1, 890)[0]
    assert faster.end_ms == 890 and faster.words[1].start_ms == int(cue.words[1].start_ms / 1.1)


def test_reflows_long_cue_before_reducing_font_size():
    style = CaptionStyle(font_size=88, safe_left=9, safe_right=15, vertical=77,
                         safe_bottom=20, animation="none")
    cue = line("an", "uncomfortable", "question.", end=1200)
    pages = list(caption_pages(cue, style, caption_width(style)))
    assert [(page.start_ms, page.end_ms) for page, _ in pages] == [(0, 400), (400, 1200)]
    assert [[word.text for _, word in row] for row in pages[-1][1]] == [
        ["uncomfortable"], ["question."],
    ]
    rows = [row for row in dynamic_ass(style, [cue]).splitlines() if row.startswith("Dialogue:")]
    assert len(rows) == 3
    assert all(r"\fscx100" in row for row in rows)
    assert len({row.split(r"\pos")[1].split(")")[0] for row in rows[1:]}) == 2


def test_mixed_script_complex_shaping(tmp_path):
    fonts = Path(__file__).parents[1] / "app" / "render_fonts"
    shutil.copytree(fonts, tmp_path / "fonts")
    caption = dynamic_ass(CaptionStyle(animation="none",weight="regular",font_family="Noto Sans"),
                          [line("प्रश्न", "निर्णय", "English")])
    assert r"\fnNoto Sans Devanagari" in caption and "\\fnNoto Sans\\" in caption
    (tmp_path / "captions.ass").write_text(caption,encoding="utf-8")
    def raster(shaping):
        return subprocess.run([FFMPEG,"-hide_banner","-v","verbose","-f","lavfi","-i","color=black:s=270x480",
            "-vf",f"ass=captions.ass:fontsdir=fonts:shaping={shaping},format=gray","-frames:v","1",
            "-f","rawvideo","-"],cwd=tmp_path,capture_output=True,check=True)
    complex_shape, simple_shape = raster("complex"), raster("simple")
    assert b"Noto Sans Devanagari" in complex_shape.stderr
    assert b"failed to find any fallback" not in complex_shape.stderr
    assert complex_shape.stdout != simple_shape.stdout


def test_caption_revisions_cut_safe_editing_and_render_gate(studio, monkeypatch):
    client, _, jobs = studio
    root, worker, render_request = prepared(studio, monkeypatch)
    old = client.post(root+"/render",json=render_request)
    assert old.status_code == 202
    worker.process(jobs.claim())
    preview = root+"/render/"+old.json()["id"]
    assert client.post(preview+"/review",json={"decision":"approved"}).status_code == 200
    assert client.get(preview+"/final").status_code == 200
    original = client.get(root+"/source").content
    transcript = client.get(root+"/transcript").json()
    endpoint = root+"/captions"
    entry = client.get(endpoint).json()
    initial = entry["draft"]
    first_response = client.post(endpoint,json={"plan_id":entry["selected"],"preset":initial["preset"],
                                              "style":initial["style"],"cues":initial["cues"]})
    assert first_response.status_code == 201,first_response.text
    first = first_response.json()
    assert client.get(preview+"/final").status_code == 409
    assert client.get(preview+"/preview").status_code == 200
    request = {"plan_id":entry["selected"],"preset":"high-impact@1",
               "style":entry["presets"]["high-impact@1"]["style"],"cues":initial["cues"]}
    original_cue=request["cues"][0]
    boundary=original_cue["words"][1]["start_ms"]
    first_piece={**original_cue,"end_ms":boundary,"words":original_cue["words"][:1]}
    last_piece={**original_cue,"start_ms":boundary,"words":original_cue["words"][1:]}
    request["cues"]=[first_piece,last_piece]+initial["cues"][1:]
    split=client.post(endpoint,json=request)
    assert split.status_code == 201,split.text
    assert split.json()["parent_id"]==first["id"]
    first_word={**original_cue["words"][0],"text":"Replacement"}
    request["cues"]=[{**original_cue,"words":[first_word,*original_cue["words"][1:]]}]+initial["cues"][1:]
    assert client.post(endpoint,json=request).status_code == 422
    corrected=client.post(endpoint,json={**request,"confirm_new_words":True})
    assert corrected.status_code == 201,corrected.text
    assert corrected.json()["introduced"]==["Replacement"]
    assert corrected.json()["corrections"]
    assert client.get(endpoint).json()["history"][-1]["cues"]==first["cues"]
    assert client.get(root+"/transcript").json()==transcript
    assert client.get(root+"/source").content==original

    new=client.post(root+"/render",json={**render_request,"settings":{"playback_speed":1.1}})
    assert new.status_code==202,new.text
    manifest=Renders(jobs).manifest(UUID(new.json()["id"]))
    assert str(manifest.caption_revision)==corrected.json()["id"]
    assert manifest.approval_fingerprint!=manifest.plan_fingerprint
    worker.process(jobs.claim())
    status=client.get(root+"/render").json()
    assert status["job"]["status"]=="completed",status["job"]
    assert status["history"][0]["metadata"]["ass_events"]>len(corrected.json()["cues"])
