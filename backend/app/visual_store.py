import hashlib
import io
import json
import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from uuid import UUID, uuid4, uuid5

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .asset_library import (CATEGORIES, asset_model, asset_path, asset_row, library_root, store_asset, utc)
from .asset_providers import MOCK_STOCK, MockStock, OpenAIImages, Pexels, ProviderError
from .captions import escape_ass
from .render_manifest import RenderSettings
from .transcription import Transcript
from .visuals import BoundVisual, VisualEvent

IMAGE_JOB = "visual_image"
GUIDANCE = {
    "mystical-sumeet": "Mystical Storytelling defaults to restrained full-screen cutaways. Start with 2–3 meaningful inserts per edited minute, typically 2–4 playback seconds each, and aim to keep the speaker visible for roughly 75–85% when inserts are useful. Treat these as flexible guides and preserve more speaker time when the story benefits. Prefer relevant personal footage, then relevant stock, then clearly illustrative AI imagery. Use clean hard cuts. Suggest graphics only when they clarify the message. Speaker-only is a valid choice, especially for healing and personal reflections.",
    "educational": "Mixed Storytelling is optional for educational and concept-heavy reels. Use relevant footage plus a restrained card or concept flow when it makes an idea easier to understand. Speaker-only remains valid.",
    "storytelling": "Use a contextual cutaway when it advances the story and return to the speaker for the insight.",
    "authority": "Use restrained professional examples. Never fabricate supporting evidence.",
}


class VisualPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: UUID
    speed: Literal[1, 1.1] = 1
    events: list[VisualEvent] = Field(max_length=18)


class StockImport(BaseModel):
    provider: Literal["mock", "pexels"]
    asset_id: str = Field(max_length=40)
    kind: Literal["image", "video"]
    license_reviewed: bool = False


class Card(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template: Literal["statement", "flow", "contrast"]
    lines: list[str] = Field(min_length=1, max_length=3)
    plan_id: UUID


class Generate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["mock", "openai"] = "mock"
    prompt: str = Field(min_length=3, max_length=700)
    size: Literal["1024x1536", "1024x1024"] = "1024x1536"
    quality: Literal["low", "medium", "high"] = "low"
    approval_cents: int | None = Field(default=None, ge=0, le=10000)
    retry: bool = False


def latest(db, project, plan):
    return db.execute("""SELECT payload FROM visual_revisions WHERE project_id=? AND plan_id=?
                      ORDER BY rowid DESC LIMIT 1""", (str(project), str(plan))).fetchone()


def visual_manifest(row):
    if not row:
        return {}
    raw = row["payload"]
    value = json.loads(raw)
    return {"visual_revision": value["id"], "visual_fingerprint": hashlib.sha256(raw.encode()).hexdigest(),
            "visuals": value["approved"]}


def suggestions(renders, project, plan):
    if not plan:
        return []
    transcript = Transcript.model_validate_json(renders.transcript_path(project, plan["transcript_revision"]).read_bytes())
    words = [(max(0,round(w.start*1000)),min(plan["source_duration_ms"],round(w.end*1000)),w.text.strip())
             for segment in transcript.segments for w in segment.words if w.text.strip()
             and round(w.start*1000) < min(plan["source_duration_ms"],round(w.end*1000))]
    result = []
    proposals = plan.get("visual_ideas") or []
    if not proposals and not plan["prompt_version"].endswith("-visuals"):
        for op in plan["operations"]:
            if op["kind"] != "b_roll" or op["status"] == "rejected":
                continue
            indices = [i for i,(a,b,_) in enumerate(words) if op["start_ms"] <= a < b <= op["end_ms"]]
            if indices:
                proposals.append({"first_word":indices[0], "last_word":indices[-1],
                                  "concept":op["idea"] or "Illustrate the spoken example", "purpose":"example",
                                  "asset_type":"stock_image","queries":["everyday routine"],"prompt":None,"rationale":op["reason"]})
    for index, item in enumerate(proposals):
        first, last = item["first_word"], item["last_word"]
        if not 0 <= first <= last < len(words):
            continue
        a,b = words[first][0],words[last][1]
        if not 300 <= b-a <= 8000:
            continue
        result.append(VisualEvent(id=uuid5(UUID(plan["id"]), f"visual-{index}"),
                     source={"start_ms":a,"end_ms":b}, words=(first,last),
                     phrase=" ".join(w[2] for w in words[first:last+1])[:350],
                     **{key:item[key] for key in ("concept","purpose","asset_type","queries","prompt","rationale")}))
    return result


def visual_options():
    return {"mock": 0, "openai": {quality: max(0,int(os.getenv(f"ELEVATE_IMAGE_{quality.upper()}_CENTS","0")))
                                 for quality in ("low","medium","high")}}


def graphic_asset(projects, settings, lines, template, brand, *, mock=None, provenance=None):
    from .render_worker import FONTS
    palette = {"routine":"0x191b1e", "crossroads":"0x182822", "reflection":"0x1d2435"}
    with TemporaryDirectory(prefix=".graphic-", dir=library_root(projects)) as staging:
        folder = Path(staging)
        (folder/"fonts").mkdir()
        for font in FONTS.glob("*.ttf"):
            shutil.copyfile(font, folder/"fonts"/font.name)
        background = palette.get(mock, "0x"+brand["background"][1:])
        if mock and provenance and provenance.get("generated"):
            tone = hashlib.sha256(provenance["prompt"].encode()).hexdigest()
            background = "0x"+ "".join(f"{16+int(tone[i:i+2],16)%48:02x}" for i in (0,2,4))
        gold = brand["highlight"].lstrip("#")
        primary = brand["primary"].lstrip("#")
        color = lambda rgb: "&H00"+rgb[4:6]+rgb[2:4]+rgb[:2]
        filters = []
        if mock:
            if mock == "routine":
                filters += ["drawbox=x=130:y=285:w=820:h=1310:color=0x565343:t=fill",
                            "drawbox=x=210:y=400:w=660:h=1030:color=0xB38759:t=fill",
                            "drawbox=x=245:y=500:w=585:h=680:color=0xE4CEAB:t=fill",
                            "drawbox=x=515:y=500:w=18:h=680:color=0xA87C51:t=fill"]
                for y in range(580,1130,85):
                    filters.append(f"drawbox=x=280:y={y}:w=205:h=3:color=0x9D815F:t=fill")
                    filters.append(f"drawbox=x=565:y={y}:w=205:h=3:color=0x9D815F:t=fill")
            else:
                filters += ["drawbox=x=260:y=400:w=560:h=330:color=0xA68A61:t=fill",
                            "drawbox=x=500:y=720:w=80:h=630:color=0xC4B57E:t=fill",
                            "drawbox=x=160:y=1330:w=760:h=90:color=0x6D7754:t=fill"]
        else:
            style = "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n[V4+ Styles]\n"
            style += "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            style += f"Style: Main,Noto Sans,64,{color(primary)},{color(gold)},&H11000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,5,90,90,0,1\n"
            style += "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            y = 750 if len(lines) > 1 else 940
            for index,line in enumerate(lines):
                text = escape_ass(line)
                if template == "flow" and index:
                    text = "↓  " + text
                emphasis = color(gold) if index == len(lines)-1 else color(primary)
                font = r"\fnNoto Sans Devanagari" if any("\u0900" <= c <= "\u097f" for c in line) else ""
                start = 15*index if template == "flow" else 0
                style += (f"Dialogue: 0,0:00:00.{start:02},0:00:05.00,Main,,90,90,0,,"
                          f"{{\\an5\\pos(540,{y+index*175})\\1c{emphasis}{font}\\fad(160,180)}}{text}\n")
            (folder/"card.ass").write_text(style)
            filters += ["drawbox=x=400:y=595:w=280:h=5:color=0x"+gold+":t=fill",
                        "ass=card.ass:fontsdir=fonts"]
        if mock and provenance and provenance.get("generated"):
            width, height = provenance["size"].split("x")
            filters += [f"scale={width}:{height}:force_original_aspect_ratio=increase", f"crop={width}:{height}"]
        is_video = mock == "crossroads" or mock is None
        filename = "visual.mp4" if is_video else "visual.png"
        args = [settings.ffmpeg,"-nostdin","-v","error","-f","lavfi","-i",
                f"color=c={background}:s=1080x1920:r=30:d=5"]
        if mock == "crossroads":
            filters.append("zoompan=z='1+0.001*on':d=1:s=1080x1920:fps=30")
        args += ["-vf",",".join(filters),"-threads","1"]
        args += ["-c:v","libx264","-preset","veryfast","-pix_fmt","yuv420p"] if is_video else ["-frames:v","1"]
        subprocess.run(args+[filename],cwd=folder,capture_output=True,check=True,timeout=75)
        label = mock or template
        with (folder/filename).open("rb") as stream:
            return store_asset(projects,settings,stream,label+Path(filename).suffix,
                               "Local illustration" if mock else " / ".join(lines), "Abstract visuals", [label],
                               provenance or {"provider":"studio","asset_id":label,"license":"Locally created graphic"})


def generate_asset(settings, jobs, job, stage):
    with jobs.projects.connect() as db:
        record = db.execute("SELECT payload FROM image_requests WHERE id=?", (job["id"],)).fetchone()
    data = json.loads(record[0])
    stage("creating_visual", .15)
    brand = data["brand"]
    provenance = {"provider":data["provider"],"generated":True,"prompt":data["prompt"],"size":data["size"],
                  "quality":data["quality"],"model":data["model"],"illustrative":True}
    if data["provider"] == "mock":
        asset = graphic_asset(jobs.projects, settings, [], "statement", brand, mock="reflection", provenance=provenance)
    else:
        maximum = min(settings.max_upload_bytes, int(os.getenv("ELEVATE_ASSET_MAX_BYTES","100000000")))
        raw = OpenAIImages(os.getenv("OPENAI_API_KEY"), data["model"]).generate(
            data["prompt"],data["size"],data["quality"],maximum)
        stage("validating_visual", .85)
        asset = store_asset(jobs.projects,settings,io.BytesIO(raw),"generated.png",data["prompt"],
                            "Abstract visuals",["AI illustration"],provenance)
    jobs.complete(job["id"],lambda db:db.execute("UPDATE image_requests SET asset_id=? WHERE id=?",(asset["id"],job["id"])))


def visual_routes(renders, settings):
    router = APIRouter()
    projects, jobs = renders.projects, renders.jobs

    @router.get("/assets")
    def assets(q: str = "", favourite: bool = False):
        with projects.connect() as db:
            rows = [json.loads(item[0]) for item in db.execute("SELECT payload FROM visual_assets ORDER BY rowid DESC")]
            revisions = [json.loads(item[0]) for item in db.execute("SELECT payload FROM visual_revisions")]
        usage = {}
        for revision in revisions:
            for item in revision["approved"]:
                usage.setdefault(item["asset"]["id"], []).append({"revision":revision["id"],"project_id":revision["project_id"]})
        needle = q.casefold().strip()
        return {"categories": CATEGORIES, "items": [{**row,"usage":usage.get(row["id"],[])} for row in rows
                if (not favourite or row["favourite"]) and needle in " ".join(
                    [row["name"],row["description"],row["category"],*row["tags"]]).casefold()]}

    @router.post("/assets", status_code=201, max_body_size=settings.max_upload_bytes+65536)
    def upload(file: UploadFile = File(...), description: str = Form(""), category: str = Form("Abstract visuals"),
               tags: str = Form("")):
        if category not in CATEGORIES or len(description) > 400 or len(tags) > 500:
            raise HTTPException(422,"Check category, description and tags.")
        return store_asset(projects,settings,file.file,file.filename or "",description,category,
                           [tag.strip()[:50] for tag in tags.split(",") if tag.strip()],{"provider":"personal"})

    @router.get("/assets/{asset_id}/{kind}")
    def media(asset_id: UUID, kind: Literal["thumbnail","media"]):
        with projects.connect() as db:
            row = asset_row(db, asset_id)
        if row["provenance"]["provider"] == "pexels" and kind == "media":
            raise HTTPException(403,"Use the credited provider page or a composited preview for stock playback.")
        asset = asset_model(row)
        media_type = "image/jpeg" if kind == "thumbnail" else {".jpg":"image/jpeg",".jpeg":"image/jpeg",".png":"image/png",
                       ".webp":"image/webp",".mov":"video/quicktime",".mp4":"video/mp4"}[asset.extension]
        return FileResponse(asset_path(projects,asset,thumbnail=kind=="thumbnail"),media_type=media_type,
                            headers={"X-Content-Type-Options":"nosniff"})

    @router.post("/assets/{asset_id}/favourite")
    def star(asset_id: UUID, value: bool = Body(embed=True)):
        with projects.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = asset_row(db,asset_id)
            row["favourite"] = value
            db.execute("UPDATE visual_assets SET payload=? WHERE id=?",(json.dumps(row),str(asset_id)))
        return row

    @router.get("/visual-stock")
    def stock(q: str = "", page: int = 1, kind: Literal["image","video"]="image", provider: Literal["mock","pexels"]="mock"):
        if len(q)>100 or not 1 <= page <= 80:
            raise HTTPException(422,"Search or page is outside the supported range.")
        try:
            engine = MockStock() if provider=="mock" else Pexels(os.getenv("PEXELS_API_KEY"))
            return engine.search(q or "everyday moments",page,kind)
        except ProviderError as error:
            raise HTTPException(502,str(error)) from error

    @router.post("/visual-stock/import",status_code=201)
    def import_stock(body: StockImport):
        if body.provider=="mock":
            item = next((row for row in MOCK_STOCK if row["id"]==body.asset_id and row["kind"]==body.kind),None)
            if not item:
                raise HTTPException(404,"Mock asset not found.")
            from .brand import Brand
            return graphic_asset(projects,settings,[],"statement",Brand().model_dump(),mock=body.asset_id,
                                 provenance={"provider":"mock","asset_id":body.asset_id,"creator":"Studio demo",
                                             "license":"Local synthetic demo","illustrative":True})
        if not body.license_reviewed:
            raise HTTPException(422,"Review the Pexels licence before importing.")
        try:
            maximum = min(settings.max_upload_bytes, int(os.getenv("ELEVATE_ASSET_MAX_BYTES","100000000")))
            data, ext, meta = Pexels(os.getenv("PEXELS_API_KEY")).download(body.asset_id,body.kind,maximum)
            return store_asset(projects,settings,io.BytesIO(data),f"pexels-{body.asset_id}{ext}",meta["title"],
                               "Abstract visuals",["Pexels"],{**meta,"provider":"pexels"})
        except ProviderError as error:
            raise HTTPException(502,str(error)) from error

    @router.get("/projects/{project_id}/visuals")
    def state(project_id: UUID):
        projects.get(project_id)
        with projects.connect() as db:
            row=renders.selected(db,project_id)
            plan=row[1] if row else None
            history=[json.loads(item[0]) for item in db.execute(
                "SELECT payload FROM visual_revisions WHERE project_id=? ORDER BY rowid DESC",(str(project_id),))]
            current=next((item for item in history if plan and item["plan_id"]==plan["id"]),None)
            last_render = db.execute("""SELECT i.manifest FROM render_inputs i JOIN renders r ON r.id=i.job_id
                                      WHERE r.project_id=? ORDER BY r.rowid DESC LIMIT 1""",(str(project_id),)).fetchone()
            default_speed = json.loads(last_render[0])["settings"]["playback_speed"] if last_render else 1
            job=jobs.public(jobs.latest(db,project_id,IMAGE_JOB))
            if job:
                result=db.execute("SELECT asset_id FROM image_requests WHERE id=?",(job["id"],)).fetchone()
                job["asset_id"]=result[0] if result else None
        return {"plan_id":plan["id"] if plan else None,"history":history,"current":current,
                "suggestions":suggestions(renders,project_id,plan),"default_speed":default_speed,"job":job,"prices":visual_options()}

    @router.post("/projects/{project_id}/visuals",status_code=201,max_body_size=65536)
    def save(project_id: UUID, body: VisualPlan):
        manifest = renders.make_manifest(project_id,body.plan_id,RenderSettings(playback_speed=body.speed),include_visuals=False)
        with projects.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if issue := renders.stale(db,manifest,check_visuals=False):
                raise HTTPException(409,issue)
            if len({event.id for event in body.events}) != len(body.events) or any(
                    event.source.end_ms > manifest.source_duration_ms for event in body.events):
                raise HTTPException(422,"Visual event identity or source timing is invalid.")
            approved = []
            for event in sorted(body.events,key=lambda item:item.source.start_ms):
                if event.status=="accepted":
                    asset=asset_model(asset_row(db,event.asset_id))
                    asset_path(projects,asset)
                    approved.append(BoundVisual(event=event,asset=asset))
            revision={"id":str(uuid4()),"project_id":str(project_id),**body.model_dump(mode="json"),
                      "source_id":manifest.source_media_id,"transcript_revision":str(manifest.transcript_revision),
                      "cut_revision":manifest.plan_fingerprint,"brand_revision":manifest.brand_revision,
                      "playbook_revision":manifest.playbook_revision,"created_at":utc(),
                      "approved":[item.model_dump(mode="json") for item in approved]}
            raw=json.dumps(revision)
            try:
                manifest.__class__.model_validate({**manifest.model_dump(),**visual_manifest({"payload":raw})})
            except ValidationError as error:
                raise HTTPException(422,error.errors()[0]["msg"]) from error
            db.execute("INSERT INTO visual_revisions VALUES (?,?,?,?)",
                       (revision["id"],str(project_id),str(body.plan_id),raw))
        return revision

    @router.post("/projects/{project_id}/visuals/card",status_code=201,max_body_size=8192)
    def card(project_id: UUID, body: Card):
        with projects.connect() as db:
            row=renders.selected(db,project_id,body.plan_id)
        if not row:
            raise HTTPException(409,"Choose the current director plan.")
        expected={"statement":(1,1),"flow":(2,3),"contrast":(2,2)}[body.template]
        if not expected[0] <= len(body.lines) <= expected[1] or any(
                not 1<=len(line.strip())<=32 or "\n" in line or "\r" in line for line in body.lines):
            raise HTTPException(422,"Use the required number of lines, each 1–32 characters.")
        return graphic_asset(projects,settings,body.lines,body.template,row[1]["brand"])

    @router.post("/projects/{project_id}/visuals/generate",status_code=202,max_body_size=8192)
    def generate(project_id: UUID, body: Generate):
        projects.get(project_id)
        price=0
        if body.provider=="openai":
            price=visual_options()["openai"][body.quality]
            if not price or body.approval_cents != price or not os.getenv("OPENAI_API_KEY"):
                raise HTTPException(422,"Set a server estimate and API key, then approve the displayed estimate.")
        with projects.connect() as db:
            selected=renders.selected(db,project_id)
        from .brand import Brand
        brand=selected[1]["brand"] if selected else Brand().model_dump()
        data={"provider":body.provider,"prompt":body.prompt.strip()+"\nIllustration for "+brand["name"]+
              ". Warm natural light, cinematic portrait-friendly composition, minimal clutter, no text.",
              "model":os.getenv("ELEVATE_IMAGE_MODEL","gpt-image-1.5") if body.provider=="openai" else "local-mock-v1",
              "size":body.size,"quality":body.quality,"brand":brand}
        raw=json.dumps(data,sort_keys=True)
        key=hashlib.sha256(raw.encode()).hexdigest()
        def prepare(db, job_id):
            prior=db.execute("""SELECT j.status,i.asset_id FROM image_requests i JOIN jobs j ON j.id=i.id
                            WHERE i.project_id=? AND i.request_key=? ORDER BY i.rowid DESC LIMIT 1""",
                            (str(project_id),key)).fetchone()
            if prior and (prior["asset_id"] or not body.retry or prior["status"] not in {"failed","cancelled","interrupted"}):
                raise HTTPException(409,"An identical request already exists. A failed attempt needs an explicit retry.")
            spent=db.execute("SELECT COALESCE(SUM(reserved_cents),0) FROM image_requests WHERE project_id=?",
                             (str(project_id),)).fetchone()[0]
            if price and spent+price > int(os.getenv("ELEVATE_IMAGE_PROJECT_CAP_CENTS","0")):
                raise HTTPException(409,"The project image budget would be exceeded.")
            db.execute("INSERT INTO image_requests VALUES (?,?,?,?,?,NULL)",(str(job_id),str(project_id),key,price,raw))
        return jobs.enqueue(project_id,kind=IMAGE_JOB,prepare=prepare,require_audio=False)

    @router.post("/projects/{project_id}/visuals/generate/{job_id}/cancel")
    def cancel(project_id: UUID, job_id: UUID):
        return jobs.cancel(project_id,job_id,IMAGE_JOB)
    return router
