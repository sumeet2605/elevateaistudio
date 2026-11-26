"use client";

import { AudioLines } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import { api, request } from "./api";

type Cue = { id: string; asset_id: string; start_ms: number; end_ms: number; offset_ms: number; gain_db: number;
  fade_in_ms: number; fade_out_ms: number; loop: boolean; crossfade_ms: number; mute: boolean;
  status: "accepted" | "pending" | "rejected"; keyframes: {ms:number;gain_db:number}[] };
type Duck = {enabled:boolean; reduction:number; threshold:number; attack_ms:number; release_ms:number; hold_ms:number; floor_db:number};
type Mix = { preset: string; platform: string; commercial: boolean; voice_db:number; music_db:number; effects_db:number;
  mute_music:boolean; mute_effects:boolean; bypass:boolean; duck:Duck; music:Cue|null; effects:Cue[] };
type Asset = { id:string; title:string; kind:"music"|"effect"; duration:number; mood:string;
  lufs:number|null; true_peak:number|null; favourite:boolean; usage:string[];
  rights:{creator:string;license:string;origin:string;confirmed:boolean;commercial:boolean;platforms:string[]} };
type Advice = Cue & {kind:"music"|"effect"; preset:string; search:string; rationale:string; duck:boolean; reduction_db:number};
type Revision = {id:string;mix:Mix;speed:number;created_at:string};
type State = {plan_id:string|null; speed:number; duration_ms:number|null; presets:Record<string,string>;
  current:Revision|null; history:Revision[]; suggestions:Advice[]};
type Meter = {input_i?:number|null;input_tp?:number|null};
type Preview = {id:string;cached:boolean;start_ms:number;end_ms:number;
  metrics:Record<"voice"|"music"|"full"|"bed"|"effects",Meter>;waveforms:Record<"voice"|"music"|"effects",number[]>; rms_dbfs:Record<string,number>};
const initial:Mix = {preset:"none@1",platform:"instagram",commercial:true,voice_db:0,music_db:0,effects_db:0,
  mute_music:false,mute_effects:false,bypass:false,duck:{enabled:true,reduction:7,threshold:-34,attack_ms:100,release_ms:650,hold_ms:330,floor_db:-44},
  music:null,effects:[]};
const moods:Record<string,string>={"mystical-ambient@1":"ambient","healing-frequency@1":"healing","emotional-journey@1":"emotional",
  "inspired-action@1":"inspired","deep-reflection@1":"reflective","professional-focus@1":"professional"};
const post=(data:unknown):RequestInit=>({method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)});
const meter=(x?:number|null)=>x==null?"—":x.toFixed(2);
function Wave({values,label}:{values:number[]; label:string}){
  const peak = Math.max(.003,...values);
  return <div className="finish-bars sound-bars" role="img" aria-label={label}>
    {values.map((value,i)=><i key={i} style={{height:`${Math.max(2,value/peak*100)}%`}}/>)}
  </div>;
}

export default function SoundStudio({projectId}: {projectId:string}) {
  const url=`/projects/${projectId}/sound-mix`;
  const [state,setState]=useState<State|null>(null);
  const [assets,setAssets]=useState<Asset[]>([]);
  const [mix,setMix]=useState(initial);
  const [speed,setSpeed]=useState(1.1);
  const [section,setSection]=useState<"mix"|"library">("mix");
  const [search,setSearch]=useState("");
  const [mood,setMood]=useState("");
  const [minimum,setMinimum]=useState(0);
  const [effect,setEffect]=useState("chime");
  const [favourite,setFavourite]=useState(false);
  const [kind,setKind]=useState<"music"|"effect">("music");
  const [preview,setPreview]=useState<Preview|null>(null);
  const [start,setStart]=useState(0);
  const [matched,setMatched]=useState(true);
  const [solo,setSolo]=useState<"voice"|"music"|"full"|"bed"|"effects"|"all">("all");
  const [revision,setRevision]=useState("");
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState("");
  const [status,setStatus]=useState("");
  useEffect(()=>{
    let ignore=false;
    Promise.all([request<State>(url),request<{items:Asset[]}>("/sound-assets")]).then(([data,lib])=>{
      if(ignore)return;setState(data);setAssets(lib.items);setMix(data.current?.mix??initial);setSpeed(data.current?.speed??data.speed);
    }).catch((problem)=>{if(!ignore)setError(problem.message);});
    return()=>{ignore=true;};
  },[url]);
  async function perform(fn:()=>Promise<void>) {
    setBusy(true);setError("");setStatus("");
    try{await fn();}catch(problem){setError(problem instanceof Error?problem.message:String(problem));}finally{setBusy(false);}
  }
  const change=(draft:Partial<Mix>)=>{setMix({...mix,...draft});setPreview(null);};
  const duration=state?.duration_ms??10000;
  function cueFor(asset:Asset):Cue {
    const start_ms=asset.kind==="music"?500:Math.min(start*1000,Math.max(0,duration-100));
    const length=Math.min(Math.floor(asset.duration*1000),asset.kind==="effect"?1700:duration-start_ms,duration-start_ms);
    return {id:crypto.randomUUID(),asset_id:asset.id,start_ms,end_ms:start_ms+length,offset_ms:0,
      gain_db:asset.kind==="effect"?-28:-24,fade_in_ms:Math.min(1100,Math.floor(length/4)),
      fade_out_ms:Math.min(1500,Math.floor(length/4)),loop:false,crossfade_ms:180,mute:false,status:"accepted",keyframes:[]};
  }
  function selectAsset(asset:Asset) {
    if(asset.kind==="music") change({preset:mix.preset==="none@1"?"mystical-ambient@1":mix.preset,music:cueFor(asset)});
    else if(mix.effects.length<8) change({effects:[...mix.effects,cueFor(asset)]});
    setSection("mix");
  }
  async function upload(e:FormEvent<HTMLFormElement>){
    e.preventDefault();
    const form=new FormData(e.currentTarget);
    const payload=new FormData();
    payload.set("file",form.get("file") as File);
    payload.set("metadata",JSON.stringify({title:form.get("title"),kind,mood:form.get("mood"),genre:form.get("genre"),
      instrumentation:form.get("instrumentation"),
      rights:{origin:form.get("origin"),license:form.get("license"),creator:form.get("creator"),
        source_url:form.get("source"),restrictions:form.get("restrictions"),platforms:form.getAll("platform"),confirmed:form.has("confirmed"),commercial:form.has("commercial")}}));
    await perform(async()=>{await request("/sound-assets",{method:"POST",body:payload});setAssets((await request<{items:Asset[]}>("/sound-assets")).items);setStatus("Sound added to the library.");});
  }
  function cueControls(cue:Cue,write:(cue:Cue)=>void,remove:()=>void){
    return <article key={cue.id} className="motion-event-details">
      <strong>{assets.find((asset)=>asset.id===cue.asset_id)?.title??"Sound cue"}</strong>
      <div className="motion-settings">{([["start_ms","Start (ms)"],["end_ms","End (ms)"],["offset_ms","Asset offset (ms)"],
        ["gain_db","Cue gain (dB)"],["fade_in_ms","Fade in (ms)"],["fade_out_ms","Fade out (ms)"],["crossfade_ms","Loop crossfade (ms)"]] as const).map(([key,label])=>
        <label key={key}>{label}<input type="number" value={cue[key]} onChange={(e)=>write({...cue,[key]:Number(e.target.value)})}/></label>)}
      </div>
      <div className="motion-actions">
        {mix.music?.id===cue.id && <label className="finish-toggle"><input type="checkbox" checked={cue.loop}
          onChange={(e)=>write({...cue,loop:e.target.checked})}/> Loop explicitly</label>}
        <label className="finish-toggle"><input type="checkbox" checked={cue.mute} onChange={(e)=>write({...cue,mute:e.target.checked})}/> Mute cue</label>
        <button className="secondary" onClick={()=>write({...cue,status:cue.status==="accepted"?"rejected":"accepted"})}>
          {cue.status==="accepted"?"Reject":"Accept"}</button>
        <button className="secondary" onClick={remove}>Delete</button>
      </div>
      <details><summary>Manual gain keyframes</summary>
        {cue.keyframes.map((point,index)=><div className="motion-actions" key={index}>
          <label>Offset (ms)<input type="number" value={point.ms} onChange={(e)=>write({...cue,keyframes:cue.keyframes.map((x,j)=>j===index?{...x,ms:Number(e.target.value)}:x)})}/></label>
          <label>Relative gain (dB)<input type="number" value={point.gain_db} onChange={(e)=>write({...cue,keyframes:cue.keyframes.map((x,j)=>j===index?{...x,gain_db:Number(e.target.value)}:x)})}/></label>
          <button className="secondary" onClick={()=>write({...cue,keyframes:cue.keyframes.filter((_,j)=>j!==index)})}>Remove</button>
        </div>)}
        <button className="secondary" disabled={cue.keyframes.length>=20} onClick={()=>{
          const offset=(cue.keyframes.at(-1)?.ms??-500)+500;
          if(offset<cue.end_ms-cue.start_ms)write({...cue,keyframes:[...cue.keyframes,{ms:offset,gain_db:0}]});
        }}>Add keyframe</button>
      </details>
    </article>;
  }
  const draft=()=>({plan_id:state?.plan_id,speed,mix});
  const filtered=assets.filter((asset)=>asset.kind===kind && (!mood||asset.mood===mood) && asset.duration>=minimum &&
    (!favourite||asset.favourite) && `${asset.title} ${asset.rights.creator}`.toLowerCase().includes(search.toLowerCase()));
  return <section className="panel visual-studio sound-studio" role="tabpanel" id="sound-panel" aria-labelledby="sound-tab">
    <div className="section-head"><div className="icon"><AudioLines size={19}/></div><div>
      <h2>Music &amp; Sound Design</h2><p className="muted small">Balance the soundtrack against the edited voice.</p></div></div>
    {error&&<p className="alert" role="alert">{error}</p>}{status&&<p className="muted small" role="status">{status}</p>}
    <div className="motion-actions visual-tabs"><button className="secondary" aria-pressed={section==="mix"} onClick={()=>setSection("mix")}>Mix</button>
      <button className="secondary" aria-pressed={section==="library"} onClick={()=>setSection("library")}>Audio Library</button></div>
    {section==="library"?<>
      <div className="motion-settings"><label>Library track type<select aria-label="Library track type" value={kind} onChange={(e)=>setKind(e.target.value as "music"|"effect")}>
        <option value="music">Music</option><option value="effect">Sound effects</option></select></label>
        <label>Search<input value={search} onChange={(e)=>setSearch(e.target.value)}/></label>
        <label>Mood<select value={mood} onChange={(e)=>setMood(e.target.value)}><option value="">Any mood</option>
          {Object.values(moods).map((m)=><option key={m}>{m}</option>)}</select></label>
        <label>Minimum seconds<input type="number" min={0} value={minimum} onChange={(e)=>setMinimum(Number(e.target.value))}/></label>
        <label className="finish-toggle"><input type="checkbox" checked={favourite} onChange={(e)=>setFavourite(e.target.checked)}/> Favourites</label>
      </div>
      {kind==="effect"&&<div className="motion-actions visual-controls">
        <label>Original studio effect<select value={effect} onChange={(e)=>setEffect(e.target.value)}>
          {["chime","swell","whoosh","soft-impact","ambient-transition"].map((item)=><option key={item}>{item}</option>)}</select></label>
        <button className="secondary" disabled={busy} onClick={()=>void perform(async()=>{
          await request(`/sound-assets/local-effect/${effect}`,post({}));
          setAssets((await request<{items:Asset[]}>("/sound-assets")).items);setStatus("Original effect added to the library.");
        })}>Create local effect</button>
      </div>}
      <div className="sound-library">{filtered.map((asset)=><article className="visual-stock" key={asset.id}><strong>{asset.title}</strong>
        <p className="small muted">{asset.rights.creator} · {asset.duration.toFixed(1)} s · {meter(asset.lufs)} LUFS · {meter(asset.true_peak)} dBTP</p>
        <p className="small muted">{asset.rights.license} · {asset.rights.confirmed?asset.rights.platforms.join(", "):"Export rights pending"} · {asset.usage.length} mix uses</p>
        <audio controls preload="none" src={`${api}/sound-assets/${asset.id}/preview`}/>
        <div className="motion-actions">
          <button className="secondary" onClick={()=>selectAsset(asset)}>{asset.kind==="music"?"Use as music":"Add effect"}</button>
          <button className="secondary" onClick={()=>void perform(async()=>{
            const next=await request<Asset>(`/sound-assets/${asset.id}/tags`,post({favourite:!asset.favourite}));
            setAssets(assets.map((item)=>item.id===asset.id?{...item,...next}:item));
          })}>{asset.favourite?"Remove favourite":"Favourite"}</button>
        </div>
        <form className="motion-actions" onSubmit={(e)=>{e.preventDefault();const form=new FormData(e.currentTarget);
          void perform(async()=>{const next=await request<Asset>(`/sound-assets/${asset.id}/tags`,post({mood:form.get("mood")}));
            setAssets(assets.map((item)=>item.id===asset.id?{...item,...next}:item));});}}>
          <label>Edit mood<input name="mood" defaultValue={asset.mood}/></label>
          <button className="secondary" disabled={busy}>Save tag</button>
        </form>
      </article>)}</div>
      <details><summary>Upload audio and review its rights</summary><form className="motion-settings visual-upload" onSubmit={(e)=>void upload(e)}>
        <label>File<input name="file" type="file" accept=".wav,.mp3,.m4a,.aac,.flac,.ogg" required/></label>
        <label>Title<input name="title" required/></label><label>Creator<input name="creator" required/></label>
        <label>Licence<input name="license" required placeholder="Rights declaration or licence reference"/></label>
        <label>Origin<select name="origin"><option value="owned">My original recording</option><option value="licensed">Licensed</option>
          <option value="generated">User-uploaded generated audio</option><option value="fixture">Preview fixture</option></select></label>
        <label>Source URL<input name="source" type="url"/></label><label>Usage restrictions<input name="restrictions"/></label><label>Mood<input name="mood" defaultValue="ambient"/></label>
        <label>Genre<input name="genre"/></label><label>Instrumentation<input name="instrumentation"/></label>
        {["instagram","youtube","tiktok","other"].map((platform)=><label className="finish-toggle" key={platform}>
          <input type="checkbox" name="platform" value={platform}/>{platform}</label>)}
        <label className="finish-toggle"><input type="checkbox" name="commercial"/> Commercial use permitted</label>
        <label className="finish-toggle"><input type="checkbox" name="confirmed"/> I confirm these usage rights</label>
        <button className="secondary" disabled={busy}>Upload sound</button>
      </form></details>
    </>:<>
      <div className="motion-settings visual-controls">
        <label>Music preset<select aria-label="Music preset" value={mix.preset} onChange={(e)=>{
          const preset=e.target.value,asset=assets.find((item)=>item.kind==="music"&&item.mood===moods[preset]);
          change({preset,music:asset?cueFor(asset):null});
          if(preset!=="none@1"&&!asset)setStatus("Choose or upload a track for this preset.");
        }}>{Object.entries(state?.presets??{"none@1":"No Music"}).map(([key,name])=><option key={key} value={key}>{name}</option>)}</select></label>
        <label>Playback speed<select value={speed} onChange={(e)=>{setSpeed(Number(e.target.value));setPreview(null);}}><option value={1}>1×</option><option value={1.1}>1.1×</option></select></label>
        <label>Intended platform<select value={mix.platform} onChange={(e)=>change({platform:e.target.value})}>
          {["instagram","youtube","tiktok","other"].map((platform)=><option key={platform}>{platform}</option>)}</select></label>
        <label className="finish-toggle"><input type="checkbox" checked={mix.commercial} onChange={(e)=>change({commercial:e.target.checked})}/> Commercial use</label>
        {([["voice_db","Voice gain"],["music_db","Music gain"],["effects_db","Effects gain"]] as const).map(([key,label])=><label key={key}>
          {label} (dB)<input type="number" value={mix[key]} onChange={(e)=>change({[key]:Number(e.target.value)})}/></label>)}
        {([["mute_music","Mute music"],["mute_effects","Mute effects"],["bypass","Bypass mix"]] as const).map(([key,label])=><label key={key} className="finish-toggle">
          <input type="checkbox" checked={mix[key]} onChange={(e)=>change({[key]:e.target.checked})}/>{label}</label>)}
      </div>
      <details open><summary>Ducking</summary><div className="motion-settings visual-controls">
        <label className="finish-toggle"><input type="checkbox" checked={mix.duck.enabled}
          onChange={(e)=>change({duck:{...mix.duck,enabled:e.target.checked}})}/> Duck under voice</label>
        {([["reduction","Reduction (dB)"],["threshold","Speech threshold (dBFS)"],["attack_ms","Attack (ms)"],
          ["release_ms","Release (ms)"],["hold_ms","Hold (ms)"],["floor_db","Minimum bed gain (dB)"]] as const).map(([key,label])=>
          <label key={key}>{label}<input type="number" value={mix.duck[key]}
            onChange={(e)=>change({duck:{...mix.duck,[key]:Number(e.target.value)}})}/></label>)}
      </div></details>
      {state?.suggestions.map((idea)=><article key={idea.id} className="visual-stock"><strong>{idea.kind==="music"?"Suggested music":"Suggested effect"}</strong>
        <p className="small">{idea.rationale} {idea.search}</p>
        <button className="secondary" onClick={()=>{
          const asset=assets.find(({id})=>id===idea.asset_id);
          if(!asset){setKind(idea.kind);setSearch(idea.search);setSection("library");return;}
          const cue={...cueFor(asset),start_ms:idea.start_ms,end_ms:idea.end_ms,gain_db:idea.gain_db,
            fade_in_ms:idea.fade_in_ms,fade_out_ms:idea.fade_out_ms,status:"pending" as const};
          change(idea.kind==="music"?{music:cue,preset:idea.preset,duck:{...mix.duck,enabled:idea.duck,reduction:idea.reduction_db}}
            :{effects:[...mix.effects,cue]});
        }}>Load suggestion for review</button>
      </article>)}
      <div className="sound-timeline"><h3>Multitrack timeline</h3>
        {preview&&<p className="muted small">Visual waveform height is scaled per track.</p>}
        {( ["voice","music","effects"] as const).map((track)=><div key={track}>
          <strong>{track==="voice"?"Voice":track==="music"?"Music":"Effects"}</strong>
          <div className="motion-track" aria-label={`${track} cue timeline`}>
            {(track==="music"?(mix.music?[mix.music]:[]):track==="effects"?mix.effects:[]).map((cue)=>
              <span key={cue.id} data-status={cue.status} style={{left:`${Math.min(100,cue.start_ms/duration*100)}%`,
                width:`${Math.max(0,Math.min(100,cue.end_ms/duration*100)-cue.start_ms/duration*100)}%`}}/>)}
            {track==="voice"&&<span data-status="accepted" style={{left:0,width:"100%"}}/>}
          </div>
          {preview&&<Wave values={preview.waveforms[track]} label={`${track} waveform`}/>}
        </div>)}
      </div>
      {mix.music&&cueControls(mix.music,(music)=>change({music}),()=>change({music:null}))}
      {mix.effects.map((cue)=>cueControls(cue,(next)=>change({effects:mix.effects.map((row)=>row.id===cue.id?next:row)}),
        ()=>change({effects:mix.effects.filter((row)=>row.id!==cue.id)})))}
      <div className="motion-actions visual-controls">
        <label>Sample starts at (s)<input type="number" min={0} value={start} onChange={(e)=>setStart(Number(e.target.value))}/></label>
        <button className="secondary" disabled={busy||!state?.plan_id} onClick={()=>void perform(async()=>{
          const result=await request<Preview>(`${url}/sample?start_ms=${Math.round(start*1000)}`,post(draft()));
          setPreview(result);setStatus(result.cached?"Cached audio samples ready.":"Audio samples ready.");
        })}>Generate audio samples</button>
      </div>
      {preview&&<><div className="motion-actions">
        <label className="finish-toggle"><input type="checkbox" checked={matched} onChange={(e)=>setMatched(e.target.checked)}/> Loudness-matched playback</label>
        <label>Solo comparison<select value={solo} onChange={(e)=>setSolo(e.target.value as typeof solo)}>
          <option value="all">All comparisons</option><option value="voice">Voice only</option><option value="music">Voice and music</option>
          <option value="full">Full mix</option><option value="bed">Solo music bus</option><option value="effects">Solo effects bus</option></select></label>
      </div>
      <div className="finish-audio">{(solo==="all"?["voice","music","full"] as const:[solo]).map((key)=><div key={key}>
        <h3>{key==="voice"?"Voice only":key==="music"?"Voice and music":key==="bed"?"Music bus":key==="effects"?"Effects bus":"Full mix"}</h3>
        <audio controls aria-label={`${key} sample`} src={`${api}${url}/sample/${preview.id}/${key}${matched?"-matched":""}.wav`}/>
        <p className="muted small">{meter(preview.metrics[key]?.input_i)} LUFS · {meter(preview.metrics[key]?.input_tp)} dBTP</p>
      </div>)}</div><p className="small muted">Edited source window · {preview.start_ms/1000}–{preview.end_ms/1000} s. Measurements shown before playback matching.</p></>}
      <div className="motion-actions motion-save">
        <label>Revision history<select value={revision} onChange={(e)=>setRevision(e.target.value)}>
          <option value="">Choose saved mix</option>{state?.history.map((item)=><option key={item.id} value={item.id}>{new Date(item.created_at).toLocaleString()}</option>)}</select></label>
        <button className="secondary" disabled={!revision} onClick={()=>{const prior=state?.history.find(({id})=>id===revision);
          if(prior){setMix(prior.mix);setSpeed(prior.speed);setPreview(null);}}}>Restore draft</button>
        <button className="primary" disabled={busy||!state?.plan_id} onClick={()=>void perform(async()=>{
          const saved=await request<Revision>(url,post(draft()));
          if(state)setState({...state,current:saved,history:[saved,...state.history]});setStatus("Mix saved. Render and review a new preview.");
        })}>Save audio mix revision</button>
      </div>
    </>}
  </section>;
}
