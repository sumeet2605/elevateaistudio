"use client";

import { Captions, Save } from "lucide-react";
import { useEffect, useRef, useState, type CSSProperties } from "react";
import { api, request } from "./api";

type Word = { text: string; start_ms: number; end_ms: number; highlighted: boolean };
type Cue = { start_ms: number; end_ms: number; words: Word[] };
type Style = {
  mode: string; animation: string; animation_ms: number; font_family: string; font_size: number; weight: string;
  primary: string; highlight: string; outline_color: string; outline: number; shadow: number; background_opacity: number;
  line_spacing: number; max_lines: number; alignment: "left" | "center" | "right"; vertical: number;
  safe_top: number; safe_bottom: number; safe_left: number; safe_right: number;
  words_per_cue: number; pause_ms: number; min_ms: number; reading_cps: number; sentence_first: boolean;
};
type Draft = { id?: string; created_at?: string; style: Style; preset: string; cues: Cue[]; introduced: string[] };
type Segment = { source_ms: number; start_ms: number; end_ms: number };
type Data = { selected: string | null; presets: Record<string, {name: string; style: Style}>;
  transcript: string; duration_ms: number; mapping: Segment[]; draft: Draft; history: Draft[] };

const look = [
  ["mode", "Highlight mode", [["static","Static"],["keyword","Keywords"],["active","Active word"],["reveal","Word reveal"]]],
  ["animation", "Animation", [["none","None"],["fade","Fade"],["scale","Gentle scale"]]],
  ["font_family", "Font family", [["auto","Auto by script"],["Noto Sans","Noto Sans"],["Noto Sans Devanagari","Noto Sans Devanagari"]]],
  ["weight", "Weight", [["regular","Regular"],["bold","Bold"]]],
  ["alignment", "Alignment", [["left","Left"],["center","Center"],["right","Right"]]],
] as const;
const geometry = [
  ["font_size","Size (px)",42,90], ["vertical","Vertical position (%)",20,90], ["outline","Outline (px)",0,10],
  ["shadow","Shadow (px)",0,8], ["background_opacity","Box opacity (%)",0,85],
  ["line_spacing","Line spacing (px)",0,28], ["max_lines","Maximum lines",1,3], ["animation_ms","Motion (ms)",60,240],
  ["safe_top","Safe top (%)",4,20], ["safe_bottom","Safe bottom (%)",8,30],
  ["safe_left","Safe left (%)",4,20], ["safe_right","Safe right (%)",4,25],
] as const;
const grouping = [
  ["words_per_cue","Words per cue",2,15], ["pause_ms","Pause (ms)",120,1000],
  ["min_ms","Preferred display (ms)",300,1600], ["reading_cps","Reading speed (characters/s)",10,32],
] as const;

const phrase = (cue: Cue) => cue.words.map(w => w.text).join(" ");
const time = (ms: number) => (ms/1000).toFixed(2) + " s";
const normal = (word: string) => word.replace(/^[.,?!।:;—…"'“”‘’()[\]{}]+|[.,?!।:;—…"'“”‘’()[\]{}]+$/g, "").toLowerCase();

export default function CaptionsStudio({ projectId, sourceUrl, seek }: {
  projectId: string; sourceUrl: string; seek: (sec: number) => void;
}) {
  const root = `/projects/${projectId}/captions`;
  const video = useRef<HTMLVideoElement>(null);
  const sequence = useRef(0);
  const [data, setData] = useState<Data>();
  const [draft, setDraft] = useState<Draft>();
  const [index, setIndex] = useState(0);
  const [splitAt, setSplitAt] = useState(1);
  const [edited, setEdited] = useState(0);
  const [safe, setSafe] = useState(true);
  const [busy, setBusy] = useState(false);
  const [manual, setManual] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [text, setText] = useState("");
  const cue = draft?.cues[index];
  const active = draft?.cues.find(c => c.start_ms <= edited && edited < c.end_ms);

  useEffect(() => {
    let done = false;
    request<Data>(root).then(item => { if (!done) {setData(item); setDraft(item.draft); } })
      .catch((problem: Error) => { if (!done) setError(problem.message); });
    return () => { done = true; };
  }, [root]);
  useEffect(() => {
    setText(cue ? phrase(cue) : "");
    setSplitAt(Math.max(1, Math.floor((cue?.words.length ?? 2)/2)));
  }, [cue]);

  function select(i: number, list = draft?.cues) {
    if (!data || !list?.[i]) return;
    setIndex(i);
    const start = list[i].start_ms;
    const span = data.mapping.find(x => x.start_ms <= start && start < x.end_ms);
    setEdited(start);
    if (span) {
      const time = (span.source_ms + start - span.start_ms)/1000;
      seek(time);
      if (video.current && video.current.readyState > 0) video.current.currentTime = time;
    }
  }

  function updateClock(element: HTMLVideoElement) {
    if (!data) return;
    const ms = Math.round(element.currentTime * 1000);
    const span = data.mapping.find(x => x.source_ms <= ms && ms < x.source_ms + x.end_ms - x.start_ms);
    if (span) setEdited(span.start_ms + ms - span.source_ms);
    else if (!element.paused && !element.seeking) {
      const next = data.mapping.find(x => x.source_ms > ms);
      if (next) element.currentTime = next.source_ms / 1000;
      else { element.pause(); setEdited(data.duration_ms); }
    }
  }

  async function regroup(changed: Draft, force: boolean) {
    if (!data || (manual && !force)) return;
    const requestId = ++sequence.current;
    try {
      const cues = await request<Cue[]>(root + "/group", {
        method: "POST", headers: {"Content-Type":"application/json"},
        body: JSON.stringify({plan_id:data.selected,preset:changed.preset,style:changed.style}),
      });
      if (requestId !== sequence.current) return;
      setDraft(current => current && {...current, cues});
      setIndex(0); setManual(false); setError(""); setConfirmed(false);
      setNotice("Regrouped from the pinned transcript.");
    } catch (problem) {
      if (requestId === sequence.current) setError((problem as Error).message);
    }
  }

  function change<K extends keyof Style>(key: K, value: Style[K]) {
    if (draft) setDraft({...draft, style: {...draft.style,[key]:value}});
    setNotice("");
  }

  function replace(index_: number, count: number, ...cues: Cue[]) {
    if (!draft) return;
    sequence.current++;
    setDraft({...draft, cues: [...draft.cues.slice(0,index_),...cues,...draft.cues.slice(index_+count)]});
    setManual(true); setNotice(""); setError(""); setConfirmed(false);
  }

  function applyText() {
    if (!cue) return;
    const values = text.trim().split(/\s+/);
    if (!text.trim() || values.length > 45 || values.some(word => word.length > 96)) {
      setError("Use one to 45 words per cue, with up to 96 characters per word."); return;
    }
    const duration = cue.end_ms-cue.start_ms;
    if (values.length > duration) {setError("The cue is too short for these words.");return;}
    const words = values.length === cue.words.length
      ? values.map((value,i)=>({...cue.words[i],text:value}))
      : values.map((value,i)=>({text:value,start_ms:cue.start_ms+Math.floor(i*duration/values.length),
        end_ms:cue.start_ms+Math.floor((i+1)*duration/values.length),highlighted:false}));
    replace(index,1,{...cue,words});
    if (values.length !== cue.words.length) setNotice("Word timings for this cue are estimated. Review the rendered preview.");
  }

  function split() {
    if (!cue || splitAt<1 || splitAt>=cue.words.length) return;
    const boundary=cue.words[splitAt].start_ms;
    if (boundary-cue.start_ms<40 || cue.end_ms-boundary<40 || cue.words[splitAt-1].end_ms>boundary) {
      setError("Choose a split between non-overlapping words with at least 40 ms on each side.");return;
    }
    replace(index,1,{...cue,end_ms:boundary,words:cue.words.slice(0,splitAt)},
      {...cue,start_ms:boundary,words:cue.words.slice(splitAt)});
  }

  function merge() {
    const next=draft?.cues[index+1];
    if (!cue || !next || !data) return;
    if (cue.words.length+next.words.length>45 || !data.mapping.some(s=>s.start_ms<=cue.start_ms&&next.end_ms<=s.end_ms)) {
      setError("Merge only cues in the same retained section, with at most 45 words.");return;
    }
    replace(index,2,{...cue,end_ms:next.end_ms,words:[...cue.words,...next.words]});
  }

  const known = new Set((data?.transcript ?? "").split(/\s+/).map(normal));
  const introduced = [...new Set(draft?.cues.flatMap(c=>c.words.map(w=>w.text)
    .filter(w=>normal(w)&&!known.has(normal(w)))))];
  async function save() {
    if (!data || !draft) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const version=await request<Draft>(root,{
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({plan_id:data.selected,preset:draft.preset,style:draft.style,cues:draft.cues,confirm_new_words:confirmed}),
      });
      setDraft(version);setData({...data,history:[version,...data.history]});setNotice("Revision saved. Generate a new preview in Render.");
    } catch(problem) {setError((problem as Error).message);}
    finally {setBusy(false);}
  }

  return <section className="panel captions-panel" id="captions-panel" role="tabpanel" aria-labelledby="captions-tab">
    <div className="section-head"><div className="icon"><Captions size={19}/></div>
      <div><h2>Captions</h2><p className="muted small">Style and correct captions for the current edit.</p></div></div>
    {error && <p className="alert" role="alert">{error}</p>}
    {notice && <p className="muted small audio-metrics" role="status">{notice}</p>}
    {data && !data.selected && <p>Select a reviewed edit in AI Director first.</p>}
    {draft && data && <>
      <label className="plan-history">Caption preset <select value={draft.preset} onChange={event=>{
        const preset=event.target.value;
        const next={...draft,preset,style:data.presets[preset].style};
        setDraft(next);void regroup(next,false);
      }}>{Object.entries(data.presets).map(([key,value])=><option value={key} key={key}>{value.name} · v{key.split("@")[1]}</option>)}</select></label>
      <div className="caption-phone">
        <video ref={video} src={sourceUrl} controls playsInline preload="metadata" onTimeUpdate={e=>updateClock(e.currentTarget)}/>
        {safe && <div className="caption-safe" style={{top:`${draft.style.safe_top}%`,bottom:`${draft.style.safe_bottom}%`,
          left:`${draft.style.safe_left}%`,right:`${draft.style.safe_right}%`}}/>}
        {active && <div className={`caption-live motion-${draft.style.animation}`} key={active.start_ms}
          style={{
            color:draft.style.primary,left:`${draft.style.safe_left}%`,right:`${draft.style.safe_right}%`,
            bottom:`${100-draft.style.vertical}%`,fontSize:`${draft.style.font_size/10.8}cqi`,
            textAlign:draft.style.alignment,fontWeight:draft.style.weight==="bold" ? 700 : 400,
            fontFamily:draft.style.font_family==="auto"?"system-ui, sans-serif":draft.style.font_family+", sans-serif",
            lineHeight:1.18+draft.style.line_spacing/draft.style.font_size,
            textShadow:`0 ${draft.style.shadow/3}px ${draft.style.outline/2}px ${draft.style.outline_color}`,
            animationDuration:`${draft.style.animation_ms}ms`,
            "--caption-lines": draft.style.max_lines,
          } as CSSProperties}>
          {active.words.map((word,i)=><span key={i} style={{
            visibility:draft.style.mode==="reveal"&&edited<word.start_ms?"hidden":"visible",
            color:draft.style.mode!=="static"&&(word.highlighted||(draft.style.mode==="active"&&word.start_ms<=edited&&edited<word.end_ms))?draft.style.highlight:undefined,
            backgroundColor:`rgb(21 21 21 / ${draft.style.background_opacity}%)`,
          }}>{word.text} </span>)}
        </div>}
      </div>
      <label className="noise-setting small"><input type="checkbox" checked={safe} onChange={e=>setSafe(e.target.checked)}/> Show mobile safe area</label>
      <p className="muted small">Interactive preview is approximate. Times refer to the edit at 1×.</p>
      <details className="detection-settings"><summary>Typography, colour and motion</summary>
        <div className="settings-grid render-settings">
          {look.map(([key,label,options])=><label key={key}>{label}<select value={draft.style[key]}
            onChange={e=>change(key,e.target.value as Style[typeof key])}>
            {options.map(([value,text_])=><option key={value} value={value}>{text_}</option>)}
          </select></label>)}
          {geometry.map(([key,label,min,max])=><label key={key}>{label}<input type="number" min={min} max={max}
            value={draft.style[key]} onChange={e=>{if (e.target.value!=="") change(key,Math.max(min,Math.min(max,Number(e.target.value))));}}/></label>)}
          {(["primary","highlight","outline_color"] as const).map((key,i)=><label key={key}>{["Text colour","Highlight colour","Outline colour"][i]}
            <input type="color" value={draft.style[key]} onChange={e=>change(key,e.target.value)}/></label>)}
        </div>
      </details>
      <details className="detection-settings"><summary>Grouping</summary>
        <div className="settings-grid render-settings">{grouping.map(([key,label,min,max])=><label key={key}>{label}
          <input type="number" min={min} max={max} value={draft.style[key]}
          onChange={e=>{if (e.target.value!=="") change(key,Math.max(min,Math.min(max,Number(e.target.value))));}}
          onBlur={()=>void regroup(draft,false)}/></label>)}</div>
        <label className="noise-setting small"><input type="checkbox" checked={draft.style.sentence_first}
          onChange={e=>{const next={...draft,style:{...draft.style,sentence_first:e.target.checked}};setDraft(next);void regroup(next,false);}}/> Prefer sentence boundaries</label>
        <button type="button" className="secondary" onClick={()=> {
          if (!manual || window.confirm("Replace the current draft’s cue edits with grouping from the original transcript?")) void regroup(draft,true);
        }}>Regroup from transcript</button>
      </details>
      <details className="detection-settings"><summary>Original transcript</summary><p className="muted small audio-metrics">{data.transcript}</p></details>
      <label className="plan-history">Cue <select aria-label="Caption cue" value={index} onChange={e=>select(Number(e.target.value))}>
        {draft.cues.map((item,i)=><option key={i} value={i}>{time(item.start_ms)} · {phrase(item)}</option>)}
      </select></label>
      {cue && <div className="caption-editor">
        <p className="muted small">{time(cue.start_ms)} – {time(cue.end_ms)}</p>
        <button className="timecode" type="button" onClick={()=>select(index)}>Seek to cue</button>
        <label>Caption text<textarea rows={2} value={text} onChange={e=>setText(e.target.value)}/></label>
        <button type="button" className="secondary" disabled={text.trim()===phrase(cue)} onClick={applyText}>Apply text</button>
        <div className="transcript-actions" aria-label="Keyword selection">
          {cue.words.map((word,i)=><button key={i} type="button" className="secondary" aria-pressed={word.highlighted}
            onClick={()=>replace(index,1,{...cue,words:cue.words.map((w,k)=>k===i?{...w,highlighted:!w.highlighted}:w)})}>{word.text}</button>)}
        </div>
        <div className="transcript-actions">
          {cue.words.length>1&&<><label className="small">Split after <select value={splitAt} onChange={e=>setSplitAt(Number(e.target.value))}>
            {cue.words.slice(0,-1).map((word,i)=><option key={i} value={i+1}>{word.text}</option>)}
          </select></label><button type="button" className="secondary" onClick={split}>Split cue</button></>}
          <button type="button" className="secondary" disabled={index+1>=draft.cues.length} onClick={merge}>Merge with next</button>
        </div>
      </div>}
      {introduced.length>0&&<label className="noise-setting small"><input type="checkbox" checked={confirmed} onChange={e=>setConfirmed(e.target.checked)}/>
        I confirm words absent from the transcript: {introduced.slice(0,8).join(", ")}{introduced.length>8?"…":""}</label>}
      <div className="transcript-actions"><button className="primary" disabled={busy||!draft.cues.length||(introduced.length>0&&!confirmed)}
        onClick={()=>void save()}><Save size={15}/> Save caption revision</button></div>
      {!!data.history.length&&<label className="plan-history">Revision history <select value="" onChange={e=>{
        const old=data.history.find(item=>item.id===e.target.value);
        if(old){sequence.current++;setDraft(old);setIndex(0);setManual(true);setConfirmed(false);setNotice("Revision loaded into draft. Save to restore it.");}
      }}><option value="">Choose a revision to restore</option>
      {data.history.map(item=><option value={item.id} key={item.id}>{item.preset} · {item.id?.slice(0,8)} · {item.created_at&&new Date(item.created_at).toLocaleString()}</option>)}</select></label>}
    </>}
  </section>;
}
