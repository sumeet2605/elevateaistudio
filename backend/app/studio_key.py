from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .visuals import VisualAsset

PRESETS = {
    "original@1": ("Original Background", "#191B1E", "#191B1E"),
    "mystical-cinematic@1": ("Mystical Cinematic", "#181618", "#503120"),
    "mystical-cinematic@2": ("Mystical Cinematic V2", "#181618", "#503120"),
    "healing-sanctuary@1": ("Healing Sanctuary", "#A89179", "#715A49"),
    "executive-authority@1": ("Executive Authority", "#4A4541", "#757375"),
    "mystical-digital@1": ("Mystical Digital", "#151625", "#524025"),
    "minimal-creator@1": ("Minimal Creator", "#D7CBB8", "#88786B"),
    "nature-retreat@1": ("Nature Retreat", "#2E4235", "#746C4F"),
}

StudioPreset = Literal["original@1", "mystical-cinematic@1", "mystical-cinematic@2", "healing-sanctuary@1",
                       "executive-authority@1", "mystical-digital@1", "minimal-creator@1", "nature-retreat@1"]

PRESET_ADJUSTMENTS = {
    "mystical-cinematic@2": {
        "similarity": .10, "blend": .036, "spill": .95, "refine": "none", "feather": .32,
        "foreground_scale": 1.05, "foreground_x": .88, "foreground_y": .9, "foreground_exposure": .045,
        "foreground_midtone": .022, "foreground_temperature": .012, "foreground_tint": .003,
        "background_brightness": .018, "background_blur": 1.5,
    },
}


class StudioSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = False
    preset: StudioPreset = "mystical-cinematic@1"
    mode: Literal["solid","gradient","asset"] = "gradient"
    background_id: UUID | None = None
    key: str = Field(default="#00B140",pattern=r"^#[0-9a-fA-F]{6}$")
    similarity: float = Field(default=.18,ge=.01,le=.45)
    blend: float = Field(default=.08,ge=0,le=.35)
    spill: float = Field(default=.38,ge=0,le=1)
    refine: Literal["none","erode","expand"] = "none"
    feather: float = Field(default=.65,ge=0,le=3)
    opacity: float = Field(default=1,ge=.35,le=1)
    foreground_scale: float = Field(default=1,ge=.55,le=1.10)
    foreground_x: float = Field(default=.5,ge=0,le=1)
    foreground_y: float = Field(default=.5,ge=0,le=1)
    foreground_fit: Literal["contain","cover"] = "contain"
    foreground_exposure: float = Field(default=0,ge=-.15,le=.15)
    foreground_midtone: float = Field(default=0,ge=-.10,le=.10)
    foreground_sharpen: float = Field(default=0,ge=0,le=.20)
    foreground_temperature: float = Field(default=0,ge=-.045,le=.045)
    foreground_tint: float = Field(default=0,ge=-.045,le=.045)
    foreground_contrast: float = Field(default=1,ge=.8,le=1.2)
    background_fit: Literal["contain","cover"] = "cover"
    background_scale: float = Field(default=1,ge=1,le=1.4)
    background_x: float = Field(default=.5,ge=0,le=1)
    background_y: float = Field(default=.5,ge=0,le=1)
    background_blur: float = Field(default=2,ge=0,le=18)
    background_brightness: float = Field(default=0,ge=-.2,le=.2)
    background_contrast: float = Field(default=1,ge=.75,le=1.25)
    background_saturation: float = Field(default=1,ge=0,le=1.5)
    background_temperature: float = Field(default=0,ge=-.06,le=.06)
    background_color: str = Field(default="#181618",pattern=r"^#[0-9a-fA-F]{6}$")
    background_end_color: str = Field(default="#503120",pattern=r"^#[0-9a-fA-F]{6}$")
    offset_ms: int = Field(default=0,ge=0,le=3600000)
    loop: bool = False
    camera: Literal["static","coordinated"] = "coordinated"

    @model_validator(mode="after")
    def valid(self):
        if self.enabled and self.preset == "original@1":
            raise ValueError("Original Background bypasses keying.")
        if self.enabled and self.mode=="asset" and not self.background_id:
            raise ValueError("Choose a background asset.")
        return self


def key_graph(nodes,input_label,settings,tag="studio",grade=""):
    rgb=f"[{tag}rgb]"
    matte=f"[{tag}matte]"
    filtered=f"[{tag}alpha]"
    foreground=f"[{tag}fore]"
    key="0x"+settings.key[1:]
    nodes.append(f"{input_label}format=yuva444p,chromakey={key}:{settings.similarity:.4f}:{settings.blend:.4f}"
                 f",split=2{rgb}[{tag}key]")
    mask=f"[{tag}key]alphaextract"
    if settings.refine!="none":
        mask+=","+("erosion" if settings.refine=="erode" else "dilation")
    if settings.feather:
        mask+=f",gblur=sigma={settings.feather:.4f}"
    # Remove the faint alpha haze without contracting the silhouette by whole pixels.
    v2 = settings.preset == "mystical-cinematic@2"
    alpha = "clip((val-10)*255/245,0,255)" if v2 else "val"
    mask+=f",lut=y='{alpha}*{settings.opacity:.4f}'"
    nodes.append(mask+matte)
    color=f"{rgb}format=gbrp"
    if settings.spill and v2:
        color+=f",despill=type=green:mix=0.5:expand=0:green=-{settings.spill:.4f}:alpha=0"
    elif settings.spill:
        color+=f",despill=type=green:mix={settings.spill:.4f}:expand=0.05"
    temp,tint=settings.foreground_temperature,settings.foreground_tint
    exposure,midtone=settings.foreground_exposure,settings.foreground_midtone
    if v2 and any((exposure,midtone,temp,tint)):
        # One bounded curve per channel. Anchor at black and taper toward white.
        gain = 2**(3*exposure)-1
        offsets = (temp*4, -tint*4, -temp*4)
        channels = ":".join(
            f"{channel}='val+val*(1-val/maxval)*({gain+offset:.5f}+{4*midtone:.5f}*val/maxval)'"
            for channel,offset in zip("rgb",offsets)
        )
        color+=",lutrgb="+channels
    elif temp or tint:
        color+=f",colorbalance=rm={temp:.4f}:bm={-temp:.4f}:gm={-tint:.4f}:pl=1"
    if settings.foreground_contrast!=1 or (not v2 and (exposure or midtone)):
        color+=(f",eq=brightness={0 if v2 else exposure:.4f}:contrast={settings.foreground_contrast:.4f}"
                f":gamma={1 if v2 else 1+midtone:.4f}")
    if settings.foreground_sharpen:
        color+=f",unsharp=lx=3:ly=3:la={settings.foreground_sharpen:.4f}:ca=0"
    nodes.append(color+","+grade+f"format=yuv444p{filtered}")
    nodes.append(f"{filtered}{matte}alphamerge{foreground}")
    return foreground


def static_background(nodes,settings,width,height,fps,label):
    shade=settings.background_color[1:]
    if settings.mode=="gradient":
        a=[int(settings.background_color[i:i+2],16) for i in (1,3,5)]
        b=[int(settings.background_end_color[i:i+2],16) for i in (1,3,5)]
        gradients=":".join(f"{channel}='{first}+({last-first})*Y/H'" for channel,first,last in zip("rgb",a,b))
        nodes.append(f"color=c=0x{shade}:s={width}x{height}:r={fps},trim=end_frame=1,format=gbrp,geq={gradients},"
                     f"format=yuv420p{label}")
    else:
        nodes.append(f"color=c=0x{shade}:s={width}x{height}:r={fps},trim=end_frame=1{label}")


def studio_graph(nodes,raw,settings,asset,index,frames,*,width=1080,height=1920,fps=30,tone="",grade="",diagnostic="composite"):
    fg=key_graph(nodes,raw,settings,grade=grade)
    if diagnostic=="matte":
        nodes.append(f"{fg}alphaextract[studiomixed]")
        return "[studiomixed]"
    if diagnostic=="foreground":
        return fg
    sw=int(width*settings.foreground_scale)//2*2
    sh=int(height*settings.foreground_scale)//2*2
    ratio="increase" if settings.foreground_fit=="cover" else "decrease"
    filters=f"scale={sw}:{sh}:force_original_aspect_ratio={ratio}:force_divisible_by=2,setsar=1"
    if ratio=="increase":
        filters+=f",crop={sw}:{sh}"
    nodes.append(f"{fg}{filters},format=yuva444p[studioplaced]")
    still = diagnostic in {"black","white"} or not asset or asset.kind=="image"
    if diagnostic in {"black","white"}:
        nodes.append(f"color=c={diagnostic}:s={width}x{height}:r={fps},trim=end_frame=1[studiofloor]")
    elif asset:
        bw=int(width*settings.background_scale)//2*2
        bh=int(height*settings.background_scale)//2*2
        fit="increase" if settings.background_fit=="cover" else "decrease"
        frame=(f"crop={width}:{height}:(iw-ow)*{settings.background_x:.4f}:(ih-oh)*{settings.background_y:.4f}" if fit=="increase" else
               f"crop=w='min(iw,{width})':h='min(ih,{height})':x='(iw-ow)*{settings.background_x:.4f}':y='(ih-oh)*{settings.background_y:.4f}',"
               f"pad={width}:{height}:(ow-iw)*{settings.background_x:.4f}:(oh-ih)*{settings.background_y:.4f}:color=0x{settings.background_color[1:]}")
        start=0 if asset.kind=="image" else settings.offset_ms/1000
        nodes.append(f"[{index}:v:0]trim=start={start:.3f},setpts=PTS-STARTPTS,fps={fps},"
                     f"scale='max(2,trunc(iw*sar/2)*2)':ih,setsar=1,"
                     f"scale={bw}:{bh}:force_original_aspect_ratio={fit}:force_divisible_by=2"
                     f"{','+tone if asset.hdr and tone else ''},{frame},trim=end_frame={1 if still else frames}[studiofloor]")
    else:
        static_background(nodes,settings,width,height,fps,"[studiofloor]")
    bg=(f"[studiofloor]eq=brightness={settings.background_brightness:.4f}:contrast={settings.background_contrast:.4f}"
        f":saturation={settings.background_saturation:.4f}")
    if settings.background_blur:
        bg+=f",gblur=sigma={settings.background_blur:.4f}"
    if settings.background_temperature:
        temp=settings.background_temperature
        bg+=f",colorbalance=rm={temp:.4f}:bm={-temp:.4f}:pl=1"
    # Prepare a still plate once, then extend its frames for the composite.
    hold = f",tpad=stop_mode=clone:stop={frames-1}" if still and frames>1 else ""
    nodes.append(bg+",format=yuv420p"+hold+"[studiobg]")
    nodes.append(f"[studiobg][studioplaced]overlay=x='(main_w-overlay_w)*{settings.foreground_x:.4f}':"
                 f"y='(main_h-overlay_h)*{settings.foreground_y:.4f}':alpha=straight:shortest=1,setsar=1[studiomixed]")
    return "[studiomixed]"


def sample_key(frames):
    import numpy as np

    h,w=frames.shape[1:3]
    edge=np.ones((h,w),dtype=bool)
    edge[h//6:5*h//6,w//6:5*w//6]=False
    border=frames[:,edge,:].reshape((-1,3)).astype(float)
    green=(border[:,1]-border[:,0]>25)&(border[:,1]-border[:,2]>20)
    count=float(np.mean(green))
    if count<.05:
        return {"detected":False,"confidence":0,"warnings":["Little green was detected near frame edges. Pick a colour manually."]}
    samples=border[green]
    median=np.median(samples,axis=0)
    rgb=np.rint(median).astype(int)
    # Robust chroma distance on border samples. Ignore brightness when proposing tolerance.
    delta=samples-median
    chroma=delta-delta.mean(axis=1,keepdims=True)
    spread=np.percentile(np.linalg.norm(chroma,axis=1),90)
    shadows=float(np.mean(samples[:,1] < np.percentile(samples[:,1],90)*.7))
    center=frames[:,h//3:2*h//3,w//3:2*w//3].astype(float)
    green_center=float(np.mean((center[:,:,:,1]-center[:,:,:,0]>25) & (center[:,:,:,1]-center[:,:,:,2]>20)))
    warnings=["Green objects and clothing can match the screen. Check the matte around hands and clothing."]
    if spread>33 or shadows>.2:
        warnings.append("Edge samples show uneven colour or shadows. Check corners and keep tolerance conservative.")
    if green_center>.2:
        warnings.append("Green occurs in the subject area. This may be uncovered screen, spill or foreground colour.")
    return {"detected":True,"key":"#"+"".join(f"{n:02x}" for n in rgb),"edge_green_fraction":round(count,3),
            "confidence":round(min(1,count*1.25)*max(.12,1-spread/150),3),
            "chroma_spread_90":round(float(spread),2),"shadow_fraction":round(shadows,3),
            "center_green_fraction":round(green_center,3),"similarity":round(min(.24,max(.10,.10+spread/450)),3),
            "blend":.065,"warnings":warnings}


class StudioIdea(BaseModel):
    model_config=ConfigDict(extra="forbid",strict=True)
    enabled: bool
    preset: StudioPreset
    asset_id: str | None
    placement: Literal["center","left","right"]
    blur: float
    camera: Literal["static","coordinated"]
    rationale: str
