"use client";

import { Film, Plus, Star } from "lucide-react";
import { useEffect, useState } from "react";
import { api, request } from "./api";

type Event = { id: string; source: {start_ms: number; end_ms: number}; phrase: string; concept: string; rationale: string;
  words?: [number, number] | null; queries?: string[]; prompt?: string | null; asset_type: string; asset_id: string | null;
  purpose: string; crop: "fit" | "fill"; movement: boolean; placement: "cutaway"; transition: "cut";
  status: "pending" | "accepted" | "rejected" };
type Asset = { id: string; name: string; description: string; tags: string[]; duration: number | null; kind: "image" | "video";
  favourite: boolean; category: string; width: number; height: number; usage: unknown[];
  provenance: {provider: string; creator?: string; license?: string; source_url?: string; restrictions?: string; generated?: boolean} };
type Library = { categories: string[]; items: Asset[] };
type Revision = { id: string; speed: 1 | 1.1; events: Event[]; created_at: string };
type Job = { id: string; status: string; error?: string; asset_id?: string };
type State = { plan_id: string | null; history: Revision[]; current: Revision | null; suggestions: Event[]; default_speed: 1 | 1.1;
  prices: { mock: 0; openai: Record<string, number> }; job: Job | null };
type Stock = { id: string; title: string; creator: string; source_url?: string; license?: string; restrictions?: string };
const post = (data?: unknown): RequestInit => ({ method: "POST", headers: { "Content-Type": "application/json" },
  body: data === undefined ? undefined : JSON.stringify(data) });
const active = (job?: Job | null) => job?.status === "queued" || job?.status === "running";
const time = (ms: number) => (ms / 1000).toFixed(2);
function assetType(asset: Asset) {
  const origin = asset.provenance.provider;
  if (asset.provenance.generated) return "generated_image";
  if (origin === "studio") return "graphic";
  return `${origin === "mock" || origin === "pexels" ? "stock" : "personal"}_${asset.kind}`;
}

const assetURL = (id: string, path: "thumbnail" | "media") => `${api}/assets/${id}/${path}`;

export default function VisualStudio({projectId, position, duration, seek}: {projectId: string; position: number;
  duration: number; seek: (value: number) => void}) {
  const root = `/projects/${projectId}/visuals`;
  const [state, setState] = useState<State | null>(null);
  const [library, setLibrary] = useState<Library>({ categories: [], items: [] });
  const [events, setEvents] = useState<Event[]>([]);
  const [speed, setSpeed] = useState<1 | 1.1>(1.1);
  const [picked, setPicked] = useState("");
  const [restoring, setRestoring] = useState("");
  const [tab, setTab] = useState<"library" | "stock" | "generate" | "cards">("library");
  const [query, setQuery] = useState("");
  const [favourite, setFavourite] = useState(false);
  const [stock, setStock] = useState<Stock[]>([]);
  const [stockPage, setStockPage] = useState<number | null>(1);
  const [stockProvider, setStockProvider] = useState<"mock" | "pexels">("mock");
  const [stockKind, setStockKind] = useState<"image" | "video">("image");
  const [licensed, setLicensed] = useState(false);
  const [provider, setProvider] = useState<"mock" | "openai">("mock");
  const [quality, setQuality] = useState("low");
  const [size, setSize] = useState("1024x1536");
  const [prompt, setPrompt] = useState("");
  const [paid, setPaid] = useState(false);
  const [retry, setRetry] = useState(false);
  const [template, setTemplate] = useState("statement");
  const [lines, setLines] = useState("");
  const [job, setJob] = useState<Job | null>(null);
  const [render, setRender] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    let ignore = false;
    Promise.all([request<State>(root), request<Library>("/assets"),
      request<{history: {id: string; manifest: {visual_revision?: string | null}}[]}>(`/projects/${projectId}/render`)])
      .then(([data, assets, output]) => {
        if (ignore) return;
        setState(data); setLibrary(assets); setJob(data.job);
        setEvents(data.current?.events ?? data.suggestions);
        setSpeed(data.current?.speed ?? data.default_speed);
        if (data.current) setRender(output.history.find((item) => item.manifest.visual_revision === data.current?.id)?.id ?? null);
      }).catch((problem) => { if (!ignore) setError(problem.message); });
    return () => { ignore = true; };
  }, [root, projectId]);

  useEffect(() => {
    if (!active(job)) return;
    let ignore = false;
    const timer = window.setInterval(() => {
      request<State>(root).then(async (data) => {
        if (ignore) return;
        setJob(data.job);
        if (data.job?.status === "completed") {
          const assets = await request<Library>("/assets");
          if (!ignore) { setLibrary(assets); setMessage("Image ready in your asset library."); setTab("library"); }
        } else if (data.job?.error) setError(data.job.error);
      }).catch((problem) => { if (!ignore) setError(problem.message); });
    }, 1000);
    return () => { ignore = true; window.clearInterval(timer); };
  }, [root, job]);

  const event = events.find((item) => item.id === picked);
  const selected = library.items.find((item) => item.id === event?.asset_id);
  const filtered = library.items.filter((item) => (!favourite || item.favourite)
    && `${item.name} ${item.description} ${item.category} ${item.tags.join(" ")}`.toLowerCase().includes(query.toLowerCase()));
  const price = provider === "mock" ? 0 : state?.prices.openai[quality] ?? 0;
  const patch = (changes: Partial<Event>) => setEvents((items) => items.map((item) => item.id === picked ? { ...item, ...changes } : item));
  async function perform(action: () => Promise<void>) {
    setBusy(true); setError(""); setMessage("");
    try { await action(); } catch (problem) { setError(problem instanceof Error ? problem.message : String(problem)); }
    finally { setBusy(false); }
  }
  async function imported(item: Asset) {
    setLibrary(await request<Library>("/assets"));
    if (event) patch({asset_id:item.id,asset_type:assetType(item),movement:item.kind === "image" && event.movement});
    setMessage(event ? "Asset assigned to this draft insert." : "Asset added to your library.");
  }
  async function search(page: number) {
    const result = await request<{items: Stock[]; next_page: number | null}>(
      `/visual-stock?q=${encodeURIComponent(query)}&provider=${stockProvider}&kind=${stockKind}&page=${page}`);
    setStock(page === 1 ? result.items : [...stock, ...result.items]); setStockPage(result.next_page);
  }

  return <section className="panel visual-studio" role="tabpanel" id="visual-panel" aria-labelledby="visual-tab">
    <div className="section-head"><div className="icon"><Film size={19}/></div><div>
      <h2>B-roll &amp; Visuals</h2><p className="muted small">Review contextual cutaways against the original speech.</p>
    </div></div>
    {error && <p className="alert" role="alert">{error}</p>}
    {message && <p className="muted small" role="status">{message}</p>}
    {state?.plan_id && <>
      <div className="visual-row"><h3>Visual timeline</h3>
        <button className="secondary" disabled={events.length>=18 || duration<.3} onClick={() => {
          const end = Math.min(Math.round(duration*1000), Math.round(position*1000)+1800);
          const id = crypto.randomUUID();
          setEvents([...events,{id,source:{start_ms:Math.max(0,Math.min(Math.round(position*1000),end-300)),end_ms:end},
            concept:"Supporting visual",phrase:"",rationale:"",asset_type:"personal_image",purpose:"example",crop:"fill",
            movement:false,placement:"cutaway",transition:"cut",status:"pending",asset_id:null}]); setPicked(id);
        }}><Plus size={16}/> Add insert</button>
      </div>
      <div className="motion-track" aria-hidden="true">{events.map((item) => <span key={item.id} data-status={item.status}
        style={{left:`${item.source.start_ms / Math.max(1,duration*10)}%`,
          width:`${(item.source.end_ms-item.source.start_ms) / Math.max(1,duration*10)}%`}}/>)}</div>
      <div className="motion-events">{events.map((item) => <button key={item.id} className="secondary"
        aria-pressed={picked===item.id} onClick={() => {setPicked(item.id); seek(item.source.start_ms/1000);}}>
        {time(item.source.start_ms)}–{time(item.source.end_ms)} s · {item.concept} · {item.status}
      </button>)}</div>
      {event && <div className="motion-event-details">
        {event.phrase && <p>“{event.phrase}”</p>}
        {event.rationale && <p className="muted small">{event.rationale}</p>}
        <div className="motion-settings">
          <label>Start (ms)<input aria-label="Insert start (ms)" type="number" value={event.source.start_ms}
            onChange={(e)=>patch({source:{...event.source,start_ms:Number(e.target.value)}})}/></label>
          <label>End (ms)<input aria-label="Insert end (ms)" type="number" value={event.source.end_ms}
            onChange={(e)=>patch({source:{...event.source,end_ms:Number(e.target.value)}})}/></label>
          <label>Crop<select aria-label="Insert crop" value={event.crop} onChange={(e)=>patch({crop:e.target.value as Event["crop"]})}>
            <option value="fill">Fill frame</option><option value="fit">Fit whole asset</option>
          </select></label>
          <label className="finish-toggle"><input type="checkbox" checked={event.movement} disabled={selected?.kind==="video"}
            onChange={(e)=>patch({movement:e.target.checked})}/> Gentle still movement</label>
        </div>
        <p className="muted small">Full-screen cutaway · Hard cut · Captions stay on</p>
        {selected && <div className="visual-selected"><img src={assetURL(selected.id,"thumbnail")} alt="Assigned asset"/>
          <div><strong>{selected.name}</strong><p className="muted small">Asset preview · timing and crop are approximate</p></div>
        </div>}
        <div className="motion-actions">
          <button className="secondary" disabled={!event.asset_id} onClick={()=>patch({status:"accepted"})}>Accept</button>
          <button className="secondary" onClick={()=>patch({status:"rejected"})}>Reject</button>
          <button className="secondary" onClick={()=>{setTab("stock");setQuery(event.queries?.[0] ?? event.concept);}}>Search alternatives</button>
          <button className="secondary" onClick={()=>{setEvents(events.filter(({id})=>id!==picked));setPicked("");}}>Delete insert</button>
        </div>
      </div>}
    </>}
    <div className="motion-actions visual-tabs" role="group" aria-label="Asset sources">
      {([["library","Asset Library"],["stock","Stock"],["generate","Generate image"],["cards","Graphic cards"]] as const)
        .map(([key,label]) => <button key={key} className="secondary" aria-pressed={tab===key}
          onClick={()=>{setTab(key); if(key==="generate" && event?.prompt) setPrompt(event.prompt);}}>{label}</button>)}
    </div>
    {tab==="library" && <>
      <form className="motion-actions visual-upload" onSubmit={(e)=>{e.preventDefault();const form=new FormData(e.currentTarget);
        void perform(async()=>imported(await request<Asset>("/assets",{method:"POST",body:form})));}}>
        <label>Upload personal media<input required name="file" type="file" accept=".mp4,.mov,.jpg,.jpeg,.png,.webp"/></label>
        <label>Category<select name="category" defaultValue="Abstract visuals">{library.categories.map((item)=><option key={item}>{item}</option>)}</select></label>
        <label>Description<input name="description" maxLength={400}/></label><label>Tags<input name="tags" placeholder="Comma-separated"/></label>
        <button className="secondary" disabled={busy}>Upload asset</button>
      </form>
      <div className="motion-actions visual-controls">
        <label>Find assets<input value={query} onChange={(e)=>setQuery(e.target.value)}/></label>
        <label className="finish-toggle"><input type="checkbox" checked={favourite} onChange={(e)=>setFavourite(e.target.checked)}/> Favourites</label>
      </div>
      <div className="visual-grid">{filtered.map((asset)=><article key={asset.id}>
        <img src={assetURL(asset.id,"thumbnail")} alt=""/>
        <strong>{asset.name}</strong>
        <p className="muted small">{asset.provenance.provider}{asset.provenance.generated && (asset.provenance.provider === "mock" ? " · Mock illustration" : " · AI illustration")} · {asset.width} × {asset.height}
          {asset.duration!=null && ` · ${asset.duration.toFixed(1)} s`}</p>
        <p className="muted small">{asset.provenance.creator} {asset.provenance.license} · {asset.usage.length} plan uses</p>
        {asset.provenance.source_url && <a href={asset.provenance.source_url} target="_blank" rel="noreferrer">Provider source</a>}
        <div className="motion-actions">
          <button className="secondary" disabled={!event} onClick={()=>patch({asset_id:asset.id,asset_type:assetType(asset),movement:asset.kind === "image" && !!event?.movement})}>Use asset</button>
          <button className="secondary" aria-label={asset.favourite ? "Remove favourite" : "Mark favourite"} aria-pressed={asset.favourite}
            onClick={()=>void perform(async()=>{await request(`/assets/${asset.id}/favourite`,post({value:!asset.favourite}));
              setLibrary(await request<Library>("/assets"));})}><Star size={14}/></button>
        </div>
      </article>)}</div>
      {!filtered.length && <p className="muted small">No matching assets.</p>}
    </>}
    {tab==="stock" && <>
      <div className="motion-actions visual-controls">
        <label>Search query<input value={query} onChange={(e)=>setQuery(e.target.value)}/></label>
        <label>Provider<select value={stockProvider} onChange={(e)=>{setStockProvider(e.target.value as "mock"|"pexels");setStock([]);}}>
          <option value="mock">Mock stock</option><option value="pexels">Pexels</option></select></label>
        <label>Media<select value={stockKind} onChange={(e)=>{setStockKind(e.target.value as "image"|"video");setStock([]);}}>
          <option value="image">Images</option><option value="video">Video</option></select></label>
        <button className="secondary" disabled={busy} onClick={()=>void perform(()=>search(1))}>Search stock</button>
      </div>
      {stockProvider==="pexels" && <label className="finish-toggle"><input type="checkbox" checked={licensed}
        onChange={(e)=>setLicensed(e.target.checked)}/> I reviewed the provider licence for this edit</label>}
      {stock.map((item)=><div className="visual-stock" key={item.id}><strong>{item.title}</strong>
        <p className="muted small">{item.creator} · {stockProvider==="mock" ? "Synthetic test media" : item.restrictions}</p>
        {item.source_url && <a href={item.source_url} target="_blank" rel="noreferrer">View provider preview</a>}
        {item.license && <a href={item.license} target="_blank" rel="noreferrer">Licence</a>}
        <button className="secondary" disabled={busy || stockProvider==="pexels" && !licensed}
          onClick={()=>void perform(async()=>imported(await request<Asset>("/visual-stock/import",post({
            provider:stockProvider,asset_id:item.id,kind:stockKind,license_reviewed:licensed}))))}>Import asset</button>
      </div>)}
      {stock.length>0 && stockPage && stockPage>1 && <button className="secondary" onClick={()=>void perform(()=>search(stockPage))}>More results</button>}
    </>}
    {tab==="generate" && <div className="visual-controls">
      <label>Illustration prompt<textarea value={prompt} maxLength={700} onChange={(e)=>{setPrompt(e.target.value);setPaid(false);}}/></label>
      <div className="motion-actions">
        <label>Provider<select value={provider} onChange={(e)=>{setProvider(e.target.value as "mock"|"openai");setPaid(false);}}>
          <option value="mock">Local mock</option><option value="openai">OpenAI Images</option></select></label>
        <label>Quality<select value={quality} onChange={(e)=>{setQuality(e.target.value);setPaid(false);}}>
          <option>low</option><option>medium</option><option>high</option></select></label>
        <label>Size<select value={size} onChange={(e)=>{setSize(e.target.value);setPaid(false);}}><option>1024x1536</option><option>1024x1024</option></select></label>
      </div>
      <p className="muted small">{provider==="mock" ? "Local mock · $0" : price ? `Server estimate · $${(price/100).toFixed(2)}` : "No server estimate configured."}</p>
      {provider==="openai" && <label className="finish-toggle"><input type="checkbox" checked={paid} onChange={(e)=>setPaid(e.target.checked)}/> Approve this paid generation</label>}
      {job?.status==="failed" && <label className="finish-toggle"><input type="checkbox" checked={retry} onChange={(e)=>setRetry(e.target.checked)}/> Retry the failed request</label>}
      <div className="motion-actions">
        <button className="secondary" disabled={busy || active(job) || prompt.trim().length<3 || provider==="openai" && (!price || !paid)}
          onClick={()=>void perform(async()=>{setJob(await request<Job>(root+"/generate",post({
            provider,prompt,quality,size,approval_cents:provider==="openai" ? price : null,retry})));setPaid(false);})}>Queue image</button>
        {active(job) && <button className="secondary" onClick={()=>void perform(async()=>{
          setJob(await request<Job>(`${root}/generate/${job?.id}/cancel`,post()));})}>Cancel generation</button>}
        {job && <span className="muted small">Generation {job.status}</span>}
      </div>
    </div>}
    {tab==="cards" && <div className="visual-controls">
      <label>Template<select value={template} onChange={(e)=>setTemplate(e.target.value)}>
        <option value="statement">Statement Card</option><option value="flow">Concept Flow</option><option value="contrast">Contrast Card</option>
      </select></label>
      <label>One idea per line<textarea value={lines} onChange={(e)=>setLines(e.target.value)}/></label>
      <button className="secondary" disabled={!state?.plan_id || !lines.trim() || busy}
        onClick={()=>void perform(async()=>imported(await request<Asset>(root+"/card",post({
          plan_id:state?.plan_id,template,lines:lines.split("\n").map((line)=>line.trim()).filter(Boolean)}))))}>Create card</button>
    </div>}
    {state?.plan_id && <div className="motion-actions motion-save">
      <label>Playback speed<select aria-label="Visual playback speed" value={speed}
        onChange={(e)=>setSpeed(Number(e.target.value) as 1|1.1)}><option value={1}>1×</option><option value={1.1}>1.1×</option></select></label>
      <label>Revision history<select value={restoring} onChange={(e)=>setRestoring(e.target.value)}>
        <option value="">Choose a revision</option>{state.history.map((item)=><option key={item.id} value={item.id}>{new Date(item.created_at).toLocaleString()}</option>)}
      </select></label>
      <button className="secondary" disabled={!restoring} onClick={()=>{
        const prior=state.history.find(({id})=>id===restoring);
        if(prior){setEvents(prior.events);setSpeed(prior.speed);setPicked("");setMessage("Revision restored into draft.");}
      }}>Restore draft</button>
      <button className="primary" disabled={busy} onClick={()=>void perform(async()=>{
        const item=await request<Revision>(root,post({plan_id:state.plan_id,speed,events}));
        setState({...state,current:item,history:[item,...state.history]});setRender(null);setMessage("Saved. Generate a new MP4 in Render.");
      })}>Save visual revision</button>
    </div>}
    {render && <><h3>Rendered composition</h3><video className="visual-render" controls playsInline
      src={`${api}/projects/${projectId}/render/${render}/preview`}/></>}
  </section>;
}
