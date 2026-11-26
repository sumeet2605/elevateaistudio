"use client";

import { Aperture } from "lucide-react";
import { useEffect, useState, type MouseEvent } from "react";
import { api, request } from "./api";

type Draft = {
  enabled:boolean; preset:string; mode:"solid"|"gradient"|"asset"; background_id:string|null; key:string;
  similarity:number; blend:number; spill:number; refine:"none"|"erode"|"expand"; feather:number; opacity:number;
  edge_treatment:"legacy"|"motion_stable";
  foreground_scale:number; foreground_x:number; foreground_y:number; foreground_fit:"contain"|"cover";
  foreground_exposure:number; foreground_temperature:number; foreground_tint:number; foreground_contrast:number;
  foreground_midtone:number; foreground_sharpen:number;
  background_fit:"contain"|"cover";background_scale:number;background_x:number;background_y:number;background_blur:number;
  background_brightness:number;background_contrast:number;background_saturation:number;background_temperature:number;
  background_color:string;background_end_color:string;offset_ms:number;loop:boolean;camera:"static"|"coordinated";
};
const defaults:Draft={enabled:false,preset:"mystical-cinematic@2",mode:"gradient",background_id:null,key:"#00B140",
  similarity:.18,blend:.08,spill:.38,refine:"none",feather:.65,opacity:1,edge_treatment:"legacy",
  foreground_scale:1,foreground_x:.5,foreground_y:.5,foreground_fit:"contain",
  foreground_exposure:0,foreground_temperature:0,foreground_tint:0,foreground_contrast:1,
  foreground_midtone:0,foreground_sharpen:0,
  background_fit:"cover",background_scale:1,background_x:.5,background_y:.5,background_blur:2,
  background_brightness:0,background_contrast:1,background_saturation:1,background_temperature:0,
  background_color:"#181618",background_end_color:"#503120",offset_ms:0,loop:false,camera:"coordinated"};
type Asset={id:string;name:string;description:string;category:string;kind:string;favourite:boolean;
  provenance:{creator?:string;license?:string;generated?:boolean};usage:unknown[]};
type Library={items:Asset[]};
type Revision={id:string;settings:Draft;speed:1|1.1;created_at:string};
type Suggestion={enabled:boolean;preset:string;asset_id:string|null;placement:"center"|"left"|"right";blur:number;camera:"static"|"coordinated";rationale:string};
type State={plan_id:string|null;presets:Record<string,[string,string,string]>;preset_adjustments:Record<string,Partial<Draft>>;
  default_preset:string;current:Revision|null;history:Revision[];suggestion?:Suggestion|null};
type Analysis={key?:string;detected:boolean;confidence:number;similarity?:number;blend?:number;analysis_seconds:number;warnings:string[]};
type Sample={id:string;extension:"mp4"|"webm";seconds:number;cached:boolean};
type Mode="original"|"matte"|"foreground"|"black"|"white"|"composite";
const post=(data:unknown):RequestInit=>({method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)});
const controls = {
  key:[["similarity","Tolerance",.01,.45,.005],["blend","Edge blend",0,.35,.005],["spill","Spill suppression",0,1,.01],
    ["feather","Feather (pixels)",0,3,.1],["opacity","Foreground opacity",.35,1,.01]],
  foreground:[["foreground_scale","Scale (1 = 100%)",.55,1.10,.01],["foreground_x","Horizontal",0,1,.01],["foreground_y","Vertical",0,1,.01],
    ["foreground_exposure","Exposure offset",-.15,.15,.005],["foreground_midtone","Midtone lift",-.10,.10,.005],
    ["foreground_sharpen","Detail sharpening",0,.20,.02],["foreground_temperature","Temperature",-.045,.045,.005],
    ["foreground_tint","Tint",-.045,.045,.005],["foreground_contrast","Contrast",.8,1.2,.01]],
  background:[["background_scale","Scale",1,1.4,.01],["background_x","Horizontal",0,1,.01],["background_y","Vertical",0,1,.01],
    ["background_blur","Blur (pixels)",0,18,.5],["background_brightness","Brightness",-.2,.2,.01],
    ["background_contrast","Contrast",.75,1.25,.01],["background_saturation","Saturation",0,1.5,.01],
    ["background_temperature","Temperature",-.06,.06,.005]],
} as const;
const modes:Record<Mode,string>={original:"Original footage",matte:"Alpha matte",foreground:"Transparent foreground",
  black:"Foreground on black",white:"Foreground on white",composite:"Final composite"};

function presetFields(state:State,preset:string,assets:Asset[]):Partial<Draft> {
  const [,background_color,background_end_color]=state.presets[preset];
  const name=preset.split("@")[0].replaceAll("-"," ");
  const asset=assets.find(item=>item.name.toLowerCase().replaceAll("-"," ").includes(name));
  return {...state.preset_adjustments[preset],preset,mode:asset?"asset":"gradient",
    background_id:asset?.id??null,background_color,background_end_color};
}

export default function VirtualStudio({projectId,duration,position}:{projectId:string;duration:number;position:number}) {
  const root=`/projects/${projectId}/studio`;
  const [state,setState]=useState<State|null>(null);
  const [assets,setAssets]=useState<Asset[]>([]);
  const [draft,setDraft]=useState(defaults);
  const [speed,setSpeed]=useState<1|1.1>(1);
  const [tab,setTab]=useState<"controls"|"assets">("controls");
  const [analysis,setAnalysis]=useState<Analysis|null>(null);
  const [reference,setReference]=useState<Sample|null>(null);
  const [result,setResult]=useState<Sample|null>(null);
  const [mode,setMode]=useState<Mode>("composite");
  const [sampleMode,setSampleMode]=useState<Mode>("composite");
  const [picker,setPicker]=useState(false);
  const [search,setSearch]=useState("");
  const [favourites,setFavourites]=useState(false);
  const [revision,setRevision]=useState("");
  const [error,setError]=useState("");
  const [notice,setNotice]=useState("");
  const [busy,setBusy]=useState(false);
  useEffect(()=>{
    let cancelled=false;
    Promise.all([request<State>(root),request<Library>("/assets")]).then(([next,library])=>{
      if(cancelled)return;
      const backgrounds=library.items.filter(asset=>asset.category==="Virtual Studio");
      setState(next);setAssets(backgrounds);
      setDraft({...defaults,...(next.current?.settings??presetFields(next,next.default_preset,backgrounds))});
      setSpeed(next.current?.speed??1);
    }).catch(e=>{if(!cancelled)setError(e.message);});
    return()=>{cancelled=true;};
  },[root]);
  function update(value:Partial<Draft>){setDraft({...draft,...value});setResult(null);}
  async function perform(action:()=>Promise<void>){
    setBusy(true);setError("");setNotice("");
    try{await action();}catch(e){setError(e instanceof Error?e.message:String(e));}finally{setBusy(false);}
  }
  function loadPreset(preset:string) {
    if(!state)return;
    const prior=Object.keys(state.preset_adjustments[draft.preset]??{}) as (keyof Draft)[];
    const reset=Object.fromEntries(prior.map(key=>[key,defaults[key]]));
    const sameStudio=draft.preset.split("@")[0]===preset.split("@")[0]&&draft.mode==="asset";
    update({...reset,...presetFields(state,preset,assets),
      ...(sameStudio?{mode:"asset",background_id:draft.background_id}:{}),enabled:preset!=="original@1"});
  }
  function url(sample:Sample){return `${api}${root}/preview/${sample.id}.${sample.extension}`;}
  function pick(event:MouseEvent<HTMLVideoElement>){
    if(!picker)return;
    const video=event.currentTarget;
    const box=video.getBoundingClientRect();
    if(!video.videoWidth)return;
    const canvas=document.createElement("canvas");canvas.width=video.videoWidth;canvas.height=video.videoHeight;
    const scale=Math.min(box.width/video.videoWidth,box.height/video.videoHeight);
    const x=Math.floor((event.clientX-box.left-(box.width-video.videoWidth*scale)/2)/scale);
    const y=Math.floor((event.clientY-box.top-(box.height-video.videoHeight*scale)/2)/scale);
    if(x<0||y<0||x>=canvas.width||y>=canvas.height)return;
    const ctx=canvas.getContext("2d");if(!ctx)return;
    try {
      ctx.drawImage(video,0,0);
      const rgb=ctx.getImageData(x,y,1,1).data.slice(0,3);
      update({key:"#"+Array.from(rgb,x=>x.toString(16).padStart(2,"0")).join("")});
      setPicker(false);setNotice("Key colour sampled from the original frame.");
    } catch {setError("The browser could not sample this frame. Use the manual colour picker.");setPicker(false);}
  }
  async function preview() {
    const start_ms=Math.max(0,Math.min(Math.round(position*1000),Math.round((duration-10)*1000)));
    const original=await request<Sample>(root+"/preview",post({settings:draft,mode:"original",start_ms,duration:10}));
    setReference(original);
    if(mode!=="original"&&draft.enabled){
      const rendered=await request<Sample>(root+"/preview",post({settings:draft,mode,start_ms,duration:10}));
      setResult(rendered);setSampleMode(mode);setNotice(rendered.cached?"Cached FFmpeg sample ready.":`FFmpeg sample rendered in ${rendered.seconds.toFixed(2)} s.`);
    }else{setResult(null);setNotice("Original FFmpeg sample ready.");}
  }
  function sliders(group:keyof typeof controls){
    return <div className="motion-settings visual-controls">{controls[group].map(([key,label,min,max,step])=>
      <label key={key}>{label}<input type="number" min={min} max={max} step={step} value={draft[key]}
        onChange={e=>update({[key]:Number(e.target.value)})}/></label>)}</div>;
  }
  return <section className="panel visual-studio virtual-studio" role="tabpanel" id="virtual-panel" aria-labelledby="virtual-tab">
    <div className="section-head"><div className="icon"><Aperture size={19}/></div><div>
      <h2>Virtual Studio</h2><p className="muted small">Key the original and inspect the subject on a new background.</p></div></div>
    {error&&<p className="alert" role="alert">{error}</p>}{notice&&<p className="muted small" role="status">{notice}</p>}
    <div className="motion-actions visual-tabs"><button className="secondary" aria-pressed={tab==="controls"} onClick={()=>setTab("controls")}>Key &amp; Composite</button>
      <button className="secondary" aria-pressed={tab==="assets"} onClick={()=>setTab("assets")}>Studio Asset Library</button></div>
    {tab==="assets"?<>
      <form className="motion-actions visual-upload" onSubmit={e=>{e.preventDefault();const form=new FormData(e.currentTarget);form.set("category","Virtual Studio");
        void perform(async()=>{await request("/assets",{method:"POST",body:form});setAssets((await request<Library>("/assets")).items.filter(x=>x.category==="Virtual Studio"));setNotice("Background added.");});
      }}><label>Background file<input name="file" type="file" accept=".jpg,.jpeg,.png,.webp,.mp4,.mov" required/></label>
        <label>Description<input name="description"/></label><label>Style tags<input name="tags"/></label><label>Creator<input name="creator"/></label><label>Licence<input name="license"/></label>
        <button className="secondary" disabled={busy}>Upload background</button></form>
      <div className="motion-actions visual-controls"><label>Find backgrounds<input value={search} onChange={e=>setSearch(e.target.value)}/></label>
        <label className="finish-toggle"><input type="checkbox" checked={favourites} onChange={e=>setFavourites(e.target.checked)}/> Favourites</label></div>
      <div className="visual-grid">{assets.filter(a=>(!favourites||a.favourite)&&`${a.name} ${a.description}`.toLowerCase().includes(search.toLowerCase())).map(asset=><article key={asset.id}>
        <img src={`${api}/assets/${asset.id}/thumbnail`} alt=""/><strong>{asset.name}</strong>
        <p className="muted small">{asset.kind} · {asset.provenance.generated?"Generated studio plate":asset.provenance.creator??"Personal upload"} · {asset.usage.length} uses</p>
        <p className="muted small">{asset.provenance.license}</p>
        <button className="secondary" onClick={()=>{update({
          ...(state&&draft.preset==="original@1"?presetFields(state,state.default_preset,assets):{}),
          background_id:asset.id,mode:"asset",enabled:true});setTab("controls");}}>Use background</button>
        <button className="secondary" onClick={()=>void perform(async()=>{const updated=await request<Asset>(`/assets/${asset.id}/favourite`,post({value:!asset.favourite}));
          setAssets(assets.map(a=>a.id===updated.id?{...a,...updated}:a));})}>{asset.favourite?"Remove favourite":"Favourite"}</button>
      </article>)}</div>
    </>:<>
      <div className="motion-settings visual-controls">
        <label className="finish-toggle"><input type="checkbox" checked={draft.enabled} onChange={e=>update({
          ...(state&&draft.preset==="original@1"?presetFields(state,state.default_preset,assets):{}),
          enabled:e.target.checked})}/> Replace background</label>
        <label>Studio preset<select aria-label="Studio preset" value={draft.preset} onChange={e=>loadPreset(e.target.value)}>
          {Object.entries(state?.presets??{}).map(([key,[name]])=><option key={key} value={key}>{name}</option>)}</select></label>
        <label>Camera<select value={draft.camera} onChange={e=>update({camera:e.target.value as Draft["camera"]})}><option value="coordinated">Coordinated push-in</option><option value="static">Static</option></select></label>
      </div>
      {state?.suggestion&&<p className="muted small">{state.suggestion.rationale} <button className="secondary" onClick={()=>{
        const idea=state.suggestion!;
        const colors=state.presets[idea.preset];
        update({edge_treatment:defaults.edge_treatment,...state.preset_adjustments[idea.preset],enabled:idea.enabled,preset:idea.enabled?idea.preset:"original@1",background_id:idea.asset_id,mode:idea.asset_id?"asset":"gradient",
          background_color:colors?.[1]??defaults.background_color,background_end_color:colors?.[2]??defaults.background_end_color,
          foreground_x:idea.placement==="left"?0:idea.placement==="right"?1:.5,
          foreground_scale:idea.placement==="center"?1:.82,background_blur:idea.blur,camera:idea.camera});
      }}>Load Director suggestion</button></p>}
      <div className="motion-actions visual-controls">
        <button className="secondary" disabled={!duration||busy} onClick={()=>void perform(async()=>{const report=await request<Analysis>(root+"/analyse",post({}));
          setAnalysis(report);setNotice(report.detected?"Background sampled. Review the key before enabling it.":"Choose a key colour manually.");})}>Analyse background</button>
        {analysis?.key&&<button className="secondary" onClick={()=>update({
          key:analysis.key,similarity:analysis.similarity,
          blend:draft.preset==="mystical-cinematic@2"?draft.blend:analysis.blend,
        })}>Use analysed key</button>}
        <button className="secondary" onClick={()=>{if(state)setDraft({...defaults,...presetFields(state,state.default_preset,assets)});setResult(null);}}>Reset controls</button>
      </div>
      {analysis&&<><p className="muted small">Border sampling indicator {(100*analysis.confidence).toFixed(0)}% · {analysis.analysis_seconds.toFixed(2)} s</p>
        {analysis.warnings.map(warning=><p className="muted small" key={warning}>{warning}</p>)}</>}
      <details open><summary>Key and matte</summary>
        <div className="motion-settings visual-controls"><label>Key colour<input type="color" value={draft.key} onChange={e=>update({key:e.target.value})}/></label>
          <label>Edge treatment<select value={draft.edge_treatment} onChange={e=>update({edge_treatment:e.target.value as Draft["edge_treatment"]})}>
            <option value="legacy">Original matte</option><option value="motion_stable">Motion-stable matte</option></select></label>
          <label>Matte refinement<select value={draft.refine} onChange={e=>update({refine:e.target.value as Draft["refine"]})}>
            <option value="none">None</option><option value="erode">Erode</option><option value="expand">Expand</option></select></label>
          <button className="secondary" disabled={!reference} onClick={()=>setPicker(!picker)}>{picker?"Click a green area":"Pick from original sample"}</button></div>
        {sliders("key")}
      </details>
      <details><summary>Foreground placement and colour</summary><div className="motion-settings visual-controls">
        <label>Foreground fit<select value={draft.foreground_fit} onChange={e=>update({foreground_fit:e.target.value as Draft["foreground_fit"]})}>
          <option value="contain">Contain</option><option value="cover">Cover</option></select></label></div>{sliders("foreground")}</details>
      <details><summary>Background fit and colour</summary>
        <div className="motion-settings visual-controls">
          <label>Background type<select value={draft.mode} onChange={e=>update({mode:e.target.value as Draft["mode"]})}>
            <option value="gradient">Gradient placeholder</option><option value="solid">Solid colour</option><option value="asset">Library asset</option></select></label>
          <label>Background fit<select value={draft.background_fit} onChange={e=>update({background_fit:e.target.value as Draft["background_fit"]})}>
            <option value="cover">Cover</option><option value="contain">Contain</option></select></label>
          <label>Base colour<input type="color" value={draft.background_color} onChange={e=>update({background_color:e.target.value})}/></label>
          <label>Gradient end colour<input type="color" value={draft.background_end_color} onChange={e=>update({background_end_color:e.target.value})}/></label>
          {draft.mode==="asset"&&<>
            <label>Video offset (ms)<input type="number" min={0} value={draft.offset_ms} onChange={e=>update({offset_ms:Number(e.target.value)})}/></label>
            <label className="finish-toggle"><input type="checkbox" checked={draft.loop} onChange={e=>update({loop:e.target.checked})}/> Loop video background</label>
          </>}
        </div>{sliders("background")}</details>
      <div className="motion-actions visual-controls">
        <label>Preview mode<select aria-label="Preview mode" value={mode} onChange={e=>setMode(e.target.value as Mode)}>
          {Object.entries(modes).map(([key,name])=><option value={key} key={key}>{name}</option>)}</select></label>
        <button className="secondary" disabled={!duration||busy} onClick={()=>void perform(preview)}>Generate FFmpeg sample</button>
      </div>
      <p className="muted small">Samples show a ten-second original-source window at proxy resolution.</p>
      {reference&&<div className="motion-previews finish-previews">
        <div><h3>Original</h3><video className={picker?"virtual-picker":undefined} src={url(reference)} crossOrigin="anonymous"
          onClick={pick} controls={!picker} playsInline muted aria-label="Original studio sample"/></div>
        {result&&<div><h3>{modes[sampleMode]}</h3><video src={url(result)} controls playsInline muted aria-label="Rendered studio sample"/></div>}
      </div>}
      {state?.plan_id?<div className="motion-actions motion-save">
        <label>Playback speed<select value={speed} onChange={e=>setSpeed(Number(e.target.value) as 1|1.1)}><option value={1}>1×</option><option value={1.1}>1.1×</option></select></label>
        <label>Revision history<select value={revision} onChange={e=>setRevision(e.target.value)}><option value="">Choose revision</option>
          {state.history.map(item=><option key={item.id} value={item.id}>{new Date(item.created_at).toLocaleString()}</option>)}</select></label>
        <button className="secondary" disabled={!revision} onClick={()=>{const selected=state.history.find(item=>item.id===revision);
          if(selected){setDraft({...defaults,...selected.settings});setSpeed(selected.speed);setResult(null);}}}>Restore draft</button>
        <button className="primary" disabled={busy} onClick={()=>void perform(async()=>{
          const saved=await request<Revision>(root,post({plan_id:state.plan_id,speed,settings:draft}));
          setState({...state,current:saved,history:[saved,...state.history]});setNotice("Studio revision saved. Render and review a new MP4.");
        })}>Save studio revision</button>
      </div>:<p className="muted small">Review a Director plan before saving a full-render studio revision.</p>}
    </>}
  </section>;
}
