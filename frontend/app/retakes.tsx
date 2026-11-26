"use client";

import { useEffect, useState } from "react";
import { api, request } from "./api";

type Span = {start_ms:number;end_ms:number};
type Attempt = {text:string;source:Span};
type Choice = "first"|"second"|"both";
type Candidate = {id:string;earlier:Attempt;later:Attempt;confidence:string;reasons:string[];
  recommendation:Choice|"manual";status:string;selected:Choice|null;adjustment:Span|null;cut:Span|null};
type Revision = {id:string;created_at:string;accepted_cuts:Span[];time_saved_ms:number};
type State = {job:{id:string;status:string;error?:string}|null; result_id:string|null; stale:boolean;
  current:Revision|null;history:Revision[];candidates:Candidate[]};
const post = (body:unknown):RequestInit => ({method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
const stamp = (ms:number) => `${Math.floor(ms/60000)}:${((ms%60000)/1000).toFixed(2).padStart(5,"0")}`;

function Card({item,seek,disabled,action,preview}:{item:Candidate;seek:(span:Span)=>void;disabled:boolean;
  action:(kind:"preview"|"review",body:unknown)=>Promise<void>;preview:string|undefined}) {
  const [choice,setChoice]=useState<Choice>(item.selected??"both");
  const [adjust,setAdjust]=useState(false);
  const [start,setStart]=useState("");
  const [end,setEnd]=useState("");
  useEffect(()=>{
    setChoice(item.selected??"both");
    setAdjust(!!item.adjustment);
    setStart(String(item.adjustment?.start_ms??""));
    setEnd(String(item.adjustment?.end_ms??""));
  },[item.selected,item.adjustment?.start_ms,item.adjustment?.end_ms]);
  const cut=choice==="first"?item.later.source:item.earlier.source;
  const body={selected:choice,adjustment:adjust&&choice!=="both"?{start_ms:Number(start||cut.start_ms),end_ms:Number(end||cut.end_ms)}:null};
  return <article className="retake-card">
    <p className="retake-status"><strong>{item.confidence} confidence</strong> · {item.reasons.join(" · ")} · {item.status}</p>
    <div className="retake-attempts">{([["First take",item.earlier],["Second take",item.later]] as const).map(([label,take])=>
      <section key={label}><h3>{label}</h3><p className="muted small">{stamp(take.source.start_ms)} – {stamp(take.source.end_ms)}</p>
        <p>{take.text}</p><button className="secondary" onClick={()=>seek(take.source)}>Play {label.toLowerCase()}</button></section>)}</div>
    <p className="muted small">Suggestion · {item.recommendation==="manual"?"Review manually":`Keep ${item.recommendation}`}</p>
    <div className="motion-actions">
      <label>Decision <select value={choice} disabled={disabled} onChange={e=>setChoice(e.target.value as Choice)}>
        <option value="both">Keep both</option><option value="first">Keep first</option><option value="second">Keep second</option></select></label>
      <label><input type="checkbox" checked={adjust} disabled={disabled||choice==="both"} onChange={e=>setAdjust(e.target.checked)}/> Adjust boundaries</label>
    </div>
    {adjust&&choice!=="both"&&<div className="motion-actions">
      <label>Start (ms) <input type="number" min={0} placeholder={String(cut.start_ms)} value={start} onChange={e=>setStart(e.target.value)}/></label>
      <label>End (ms) <input type="number" min={1} placeholder={String(cut.end_ms)} value={end} onChange={e=>setEnd(e.target.value)}/></label>
    </div>}
    <div className="motion-actions">
      <button className="secondary" disabled={disabled||choice==="both"} onClick={()=>void action("preview",body)}>Play proposed join</button>
      <button disabled={disabled} onClick={()=>void action("review",{...body,status:"accepted"})}>Accept</button>
      <button className="secondary" disabled={disabled} onClick={()=>void action("review",{selected:"both",status:"rejected"})}>Reject</button>
    </div>
    {preview&&<video controls autoPlay playsInline src={preview} aria-label="Proposed join"/>}
  </article>;
}

export default function RetakeReview({projectId,play}:{projectId:string;play:(span:Span)=>void}) {
  const root=`/projects/${projectId}/retakes`;
  const [state,setState]=useState<State|null>(null);
  const [preview,setPreview]=useState<{candidate:string;url:string}|null>(null);
  const [revision,setRevision]=useState("");
  const [error,setError]=useState("");
  const [busy,setBusy]=useState(false);
  const [padding,setPadding]=useState(40);
  const active=state?.job?.status==="running"||state?.job?.status==="queued";
  useEffect(()=>{
    let mounted=true;
    const load=()=>request<State>(root).then(next=>{if(mounted)setState(next);}).catch(e=>{if(mounted)setError(e.message);});
    void load();
    const timer=active?setInterval(load,900):undefined;
    return()=>{mounted=false;if(timer)clearInterval(timer);};
  },[root,active]);
  async function perform(action:()=>Promise<void>) {
    setError("");setBusy(true);
    try {await action();setState(await request<State>(root));}
    catch(e){setError(e instanceof Error?e.message:String(e));}
    finally{setBusy(false);}
  }
  return <section className="panel retake-panel" id="retake-panel" role="tabpanel" aria-labelledby="retake-tab">
    <div className="section-head"><div><h2>Retake Review</h2><p className="muted small">Compare both attempts before accepting any spoken cut.</p></div></div>
    {error&&<p role="alert">{error}</p>}
    <div className="motion-actions">
      <label>Cut padding (ms) <input type="number" min={0} max={120} value={padding} onChange={e=>setPadding(Number(e.target.value))}/></label>
      <button disabled={busy||active} onClick={()=>void perform(async()=>{setPreview(null);await request(root,post({padding_ms:padding}));})}>Find retakes</button>
      {active&&<button className="secondary" disabled={busy} onClick={()=>void perform(async()=>{await request(`${root}/jobs/${state?.job?.id}/cancel`,post({}));})}>Cancel</button>}
      {["failed","interrupted"].includes(state?.job?.status??"")&&<button className="secondary" onClick={()=>void perform(async()=>{
        await request(`${root}/jobs/${state?.job?.id}/retry`,post({}));
      })}>Retry</button>}
    </div>
    {state?.job&&<p role="status">{state.job.status}{state.job.error&&` · ${state.job.error}`}</p>}
    {state?.stale&&<p role="status">Source decisions changed. Run a new analysis before reviewing these suggestions.</p>}
    {!!state?.current?.time_saved_ms&&<p>Accepted cuts save {(state.current.time_saved_ms/1000).toFixed(2)} seconds.</p>}
    {state?.current&&<div className="motion-actions">
      <label>Review history <select value={revision} onChange={e=>setRevision(e.target.value)}>
        <option value="">Clear reviewed retakes</option>
        {state.history.map(item=><option key={item.id} value={item.id}>{new Date(item.created_at).toLocaleString()} · {item.accepted_cuts.length} cuts</option>)}
      </select></label>
      <button className="secondary" disabled={busy} onClick={()=>void perform(async()=>{
        setPreview(null);await request(root+"/restore",post({revision:revision||null,expected:state.current?.id}));
      })}>Restore decision revision</button>
      <p className="muted small">Refresh the AI Director draft before rendering after a review change.</p>
    </div>}
    {state?.job?.status==="completed"&&!state.candidates.length&&<p>No retake candidates found.</p>}
    {state?.candidates.map(item=><Card key={item.id} item={item} seek={play} disabled={busy||active||state.stale}
      preview={preview?.candidate===item.id?preview.url:undefined}
      action={(kind,body)=>perform(async()=>{
        setPreview(null);
        const data=await request<{id:string}>(`${root}/${state.result_id}/${item.id}/${kind}`,
          post(kind==="review"?{...(body as object),expected:state.current?.id??null}:body));
        if(kind==="preview")setPreview({candidate:item.id,url:`${api}${root}/previews/${data.id}`});
      })}/>)}
  </section>;
}
