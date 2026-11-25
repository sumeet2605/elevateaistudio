"use client";

import { Aperture, Crosshair, Plus, ScanFace } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { request } from "./api";

type Focus = { x: number; y: number };
type Face = Focus & { width: number; height: number; confidence: number };
type Analysis = { id: string; width: number; height: number;
  samples: { source_ms: number; faces: Face[] }[];
  summary: { samples: number; single: number; multiple: number; missing: number } };
type Camera = { framing: "original" | "center" | "face" | "manual" | "fit"; focus: Focus;
  energy: "playbook" | "energetic"; dead_zone: number; smoothing: number };
type Motion = { id: string; source: { start_ms: number; end_ms: number }; start_zoom: number; end_zoom: number;
  strategy: "center" | "face" | "manual"; focus: Focus; transition: "punch" | "smooth" | "hold";
  easing: "linear" | "ease-in" | "ease-out" | "ease-in-out"; reason: string;
  status: "pending" | "accepted" | "rejected" };
type Revision = { id: string; settings: Camera; events: Motion[]; face_version: string | null; created_at: string };
type Job = { id: string; status: string; progress: number | null; error?: string };
type State = { default_zoom: number; plan_id: string | null; current: Revision | null; history: Revision[]; analysis: Analysis | null;
  latest_analysis: Analysis | null; job: Job | null; suggestions: Motion[] };

const center = { x: 0.5, y: 0.5 };
const defaults: Camera = { framing: "original", focus: center, energy: "playbook", dead_zone: 0.025, smoothing: 0.32 };
const json = (body?: unknown): RequestInit => ({
  method: "POST", headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body),
});
const active = (job?: Job | null) => job?.status === "queued" || job?.status === "running";
const time = (ms: number) => (ms / 1000).toFixed(2);

function labelStatus(status: Motion["status"]) {
  return status[0].toUpperCase() + status.slice(1);
}

function focusFields(point: Focus, update: (point: Focus) => void) {
  return (["x", "y"] as const).map((axis) => <label key={axis}>Focus {axis.toUpperCase()} (%)
    <input type="number" min={0} max={100} step={1} value={Math.round(point[axis] * 100)}
      onChange={(event) => update({ ...point, [axis]: Math.max(0, Math.min(100, Number(event.target.value))) / 100 })}/>
  </label>);
}

export default function MotionStudio({ projectId, sourceUrl, position, duration, seek }: {
  projectId: string; sourceUrl: string; position: number; duration: number; seek: (value: number) => void;
}) {
  const url = `/projects/${projectId}/motion`;
  const [state, setState] = useState<State | null>(null);
  const [camera, setCamera] = useState(defaults);
  const [events, setEvents] = useState<Motion[]>([]);
  const [face, setFace] = useState<Analysis | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [frequency, setFrequency] = useState(2);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [boxes, setBoxes] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);
  const [history, setHistory] = useState("");
  const previews = [useRef<HTMLVideoElement>(null), useRef<HTMLVideoElement>(null)];
  const sourceMs = Math.round(position * 1000);

  useEffect(() => {
    let ignore = false;
    request<State>(url).then((data) => {
      if (ignore) return;
      setState(data);
      setCamera(data.current?.settings ?? defaults);
      setEvents(data.current?.events ?? data.suggestions);
      setFace(data.current ? (data.current.face_version ? data.analysis : null) : data.latest_analysis);
      setJob(data.job);
    }).catch((reason) => { if (!ignore) setError(reason.message); });
    return () => { ignore = true; };
  }, [url]);

  useEffect(() => {
    if (!active(job)) return;
    let ignore = false;
    const timer = window.setInterval(() => {
      request<State>(url).then((data) => {
        if (ignore) return;
        setJob(data.job);
        if (!active(data.job)) {
          setState(data);
          if (data.job?.status === "completed") setFace(data.latest_analysis);
          if (data.job?.error) setError(data.job.error);
        }
      }).catch((reason) => { if (!ignore) setError(reason.message); });
    }, 1000);
    return () => { ignore = true; window.clearInterval(timer); };
  }, [job, url]);

  useEffect(() => {
    for (const preview of previews) {
      if (preview.current?.readyState && Math.abs(preview.current.currentTime - position) > 0.09)
        preview.current.currentTime = position;
    }
  });

  async function perform(action: () => Promise<void>) {
    setBusy(true); setError(""); setMessage("");
    try { await action(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(false); }
  }

  function update(event: Motion, changes: Partial<Motion>) {
    setEvents((previous) => previous.map((item) => item.id === event.id ? { ...item, ...changes } : item));
  }

  function add() {
    const end = Math.min(Math.round(duration * 1000), sourceMs + 1800);
    const item: Motion = { id: crypto.randomUUID(), source: { start_ms: Math.max(0, Math.min(sourceMs, end - 100)), end_ms: end },
      start_zoom: 100, end_zoom: state?.default_zoom ?? 105, strategy: "center", focus: center, transition: "smooth", easing: "ease-in-out",
      reason: "Manual camera emphasis.", status: "pending" };
    setEvents((saved) => [...saved, item]); setSelected(item.id);
  }

  const nearest = face?.samples.reduce<Analysis["samples"][number] | null>((best, sample) =>
    !best || Math.abs(sample.source_ms - sourceMs) < Math.abs(best.source_ms - sourceMs) ? sample : best, null);
  const visible = nearest && Math.abs(nearest.source_ms - sourceMs) < 1100 ? nearest.faces : [];
  const single = visible.length === 1 ? visible[0] : null;
  const automatic = single ? { x: single.x + single.width / 2, y: single.y + single.height / 2 } : camera.focus;
  const cropped = camera.framing === "manual" || camera.framing === "center" || camera.framing === "face";
  const base = camera.framing === "face" ? automatic : camera.framing === "manual" ? camera.focus : center;
  const playing = events.find((event) => event.status === "accepted" && event.source.start_ms <= sourceMs && sourceMs < event.source.end_ms);
  const progress = playing ? (sourceMs - playing.source.start_ms) / Math.max(1, playing.source.end_ms - playing.source.start_ms) : 0;
  const eased = playing?.easing === "ease-in" ? progress ** 2 : playing?.easing === "ease-out" ? 1 - (1 - progress) ** 2
    : playing?.easing === "ease-in-out" ? progress ** 2 * (3 - 2 * progress) : progress;
  const ramp = playing ? Math.min(1, progress * 6, (1 - progress) * 6) : 0;
  const magnify = playing ? (playing.transition === "smooth"
    ? 100 + (playing.start_zoom + (playing.end_zoom - playing.start_zoom) * eased - 100) * ramp
    : playing.transition === "hold" ? 100 + (playing.end_zoom - 100) * ramp : playing.end_zoom) : 100;
  const aim = playing?.strategy === "manual" ? playing.focus : playing?.strategy === "face" ? automatic : center;
  const picked = events.find((event) => event.id === selected);

  return <section className="panel motion-studio" role="tabpanel" id="motion-panel" aria-labelledby="motion-tab">
    <div className="section-head"><div className="icon"><Aperture size={19}/></div><div>
      <h2>Camera &amp; Motion</h2><p className="muted small">Browser framing is approximate. Review the rendered MP4 before export.</p>
    </div></div>
    {error && <p className="error" role="alert">{error}</p>}
    {message && <p className="note" role="status">{message}</p>}
    {duration > 0 && <div className="motion-previews">
      <div><h3>Original with tracking</h3><div className="motion-frame motion-source"
        style={{ aspectRatio: face ? `${face.width}/${face.height}` : undefined }}>
        <video ref={previews[0]} src={sourceUrl} muted playsInline preload="metadata"
          onLoadedMetadata={(event) => { event.currentTarget.currentTime = position; }}/>
        {boxes && visible.map((box, index) => <span key={index} className="motion-box"
          style={{ left: `${box.x * 100}%`, top: `${box.y * 100}%`, width: `${box.width * 100}%`, height: `${box.height * 100}%` }}>
          <span>{Math.round(box.confidence * 100)}%</span></span>)}
      </div></div>
      <div><h3>Reframed preview</h3><div className="motion-frame">
        <video ref={previews[1]} src={sourceUrl} muted playsInline preload="metadata"
          onLoadedMetadata={(event) => { event.currentTarget.currentTime = position; }}
          style={{ objectFit: cropped ? "cover" : "contain", objectPosition: `${base.x * 100}% ${base.y * 100}%`,
            transform: `scale(${magnify / 100})`, transformOrigin: `${aim.x * 100}% ${aim.y * 100}%` }}/>
      </div></div>
    </div>}
    {duration > 0 && <label className="motion-scrub">Source time · {time(sourceMs)} s
      <input type="range" min={0} max={Math.round(duration * 1000)} value={sourceMs} step={33}
        onChange={(event) => seek(Number(event.target.value) / 1000)}/>
    </label>}
    <div className="motion-controls">
      <div className="motion-actions">
        <label>Samples per second <select aria-label="Samples per second" value={frequency} onChange={(event) => setFrequency(Number(event.target.value))}>
          {[1, 2, 3].map((item) => <option key={item}>{item}</option>)}</select></label>
        <button className="secondary" disabled={busy || active(job) || !duration} onClick={() => void perform(async () => {
          const result = await request<{ analysis?: Analysis; job?: Job }>(url + "/analysis", json({ frequency }));
          if (result.analysis) { setFace(result.analysis); setMessage("Reused the saved face analysis."); }
          if (result.job) setJob(result.job);
        })}><ScanFace size={17}/> Analyse faces</button>
        {active(job) && <button className="secondary" disabled={busy} onClick={() => void perform(async () => {
          setJob(await request<Job>(`${url}/analysis/${job?.id}/cancel`, json()));
        })}>Cancel</button>}
        <label className="motion-toggle"><input type="checkbox" checked={boxes} onChange={(event) => setBoxes(event.target.checked)}/> Show face boxes</label>
      </div>
      <p className="muted small" role="status">{active(job) ? `Analysing faces · ${Math.round((job?.progress ?? 0) * 100)}%`
        : face ? `${face.summary.single}/${face.summary.samples} reliable single-face samples · ${face.summary.missing} missing · ${face.summary.multiple} multiple`
        : job ? `Face analysis ${job.status}.` : "Face analysis has not run."}</p>
    </div>
    {!state?.plan_id ? <p className="note">Select a director plan to save camera settings and zoom events.</p> : <>
      <div className="motion-settings">
        <label>Framing<select aria-label="Framing" value={camera.framing} onChange={(event) => setCamera({ ...camera, framing: event.target.value as Camera["framing"] })}>
          <option value="original">Original render framing</option><option value="center">Centre crop</option>
          <option value="face">Face-aware crop</option><option value="manual">Manual focus</option><option value="fit">Fit to frame</option>
        </select></label>
        <label>Zoom constraints<select value={camera.energy} onChange={(event) => setCamera({ ...camera, energy: event.target.value as Camera["energy"] })}>
          <option value="playbook">Playbook defaults</option><option value="energetic">High-energy</option>
        </select></label>
        {camera.framing === "manual" && focusFields(camera.focus, (focus) => setCamera({ ...camera, focus }))}
        {camera.framing === "face" && <>
          <label>Dead zone<input type="number" min={0.005} max={0.12} step={0.005} value={camera.dead_zone}
            onChange={(event) => setCamera({ ...camera, dead_zone: Number(event.target.value) })}/></label>
          <label>Tracking response<input type="number" min={0.1} max={0.7} step={0.05} value={camera.smoothing}
            onChange={(event) => setCamera({ ...camera, smoothing: Number(event.target.value) })}/></label>
        </>}
      </div>
      <div className="motion-timeline-head"><h3>Zoom timeline</h3><button className="secondary" disabled={duration < 0.1 || events.length >= 40} onClick={add}><Plus size={16}/> Add zoom</button></div>
      <div className="motion-track" aria-hidden="true">
        {events.map((event) => <span key={event.id} data-status={event.status}
          style={{ left: `${event.source.start_ms / (duration * 10)}%`, width: `${(event.source.end_ms - event.source.start_ms) / (duration * 10)}%` }}/>)}
        <i style={{ left: `${Math.min(100, position / duration * 100)}%` }}/>
      </div>
      <div className="motion-events">
        {events.map((event) => <button type="button" className="secondary" key={event.id} aria-pressed={selected === event.id}
          onClick={() => { setSelected(event.id); seek(event.source.start_ms / 1000); }}>
          {time(event.source.start_ms)}–{time(event.source.end_ms)} s · {event.end_zoom}% · {labelStatus(event.status)}
        </button>)}
        {!events.length && <p className="muted small">No zoom events. Static framing is available.</p>}
      </div>
      {picked && <div className="motion-event-details">
        <p className="small">{picked.reason}</p>
        <div className="motion-settings">
          {(["start_ms", "end_ms"] as const).map((key) => <label key={key}>{key === "start_ms" ? "Start (ms)" : "End (ms)"}
            <input type="number" min={0} max={Math.round(duration * 1000)} value={picked.source[key]}
              onChange={(event) => update(picked, { source: { ...picked.source, [key]: Math.round(Number(event.target.value)) } })}/></label>)}
          {(["start_zoom", "end_zoom"] as const).map((key) => <label key={key}>{key === "start_zoom" ? "Start zoom (%)" : "End zoom (%)"}
            <input type="number" min={100} max={125} value={picked[key]}
              onChange={(event) => update(picked, { [key]: Number(event.target.value) })}/></label>)}
          <label>Focus<select aria-label="Focus" value={picked.strategy} onChange={(event) => update(picked, { strategy: event.target.value as Motion["strategy"] })}>
            <option value="center">Centre</option><option value="face">Face</option><option value="manual">Manual</option></select></label>
          <label>Transition<select aria-label="Transition" value={picked.transition} onChange={(event) => update(picked, { transition: event.target.value as Motion["transition"] })}>
            <option value="punch">Instant punch</option><option value="smooth">Smooth</option><option value="hold">Hold</option></select></label>
          <label>Easing<select aria-label="Easing" value={picked.easing} onChange={(event) => update(picked, { easing: event.target.value as Motion["easing"] })}>
            {["linear", "ease-in", "ease-out", "ease-in-out"].map((item) => <option key={item}>{item}</option>)}</select></label>
          {picked.strategy === "manual" && focusFields(picked.focus, (focus) => update(picked, { focus }))}
        </div>
        <div className="motion-actions">
          <button className="secondary" aria-pressed={picked.status === "accepted"} onClick={() => update(picked, { status: "accepted" })}>Accept</button>
          <button className="secondary" aria-pressed={picked.status === "rejected"} onClick={() => update(picked, { status: "rejected" })}>Reject</button>
          <button className="secondary" onClick={() => { setEvents((draft) => draft.filter((item) => item.id !== picked.id)); setSelected(null); }}>Delete event</button>
        </div>
      </div>}
      <div className="motion-actions motion-save">
        <label>Revision history<select aria-label="Revision history" value={history} onChange={(event) => setHistory(event.target.value)}>
          <option value="">Choose a revision</option>
          {state.history.map((item) => <option key={item.id} value={item.id}>{item.id === state.current?.id ? "Current · " : ""}{new Date(item.created_at).toLocaleString()}</option>)}
        </select></label>
        <button className="secondary" disabled={!history || busy} onClick={() => void perform(async () => {
          const item = state.history.find((entry) => history === entry.id);
          if (!item) return;
          const analysis = item.face_version ? await request<Analysis>(`${url}/analysis/${item.face_version}`) : null;
          setCamera(item.settings); setEvents(item.events); setSelected(null); setFace(analysis);
          setMessage("Revision loaded into the draft. Save to apply it.");
        })}>Restore into draft</button>
        <button className="primary" disabled={busy || active(job)} onClick={() => void perform(async () => {
          const item = await request<Revision>(url, json({ plan_id: state.plan_id, settings: camera, events, face_version: face?.id ?? null }));
          setState({ ...state, current: item, history: [item, ...state.history] });
          setMessage("Motion revision saved. Render and review a new preview.");
        })}><Crosshair size={17}/> Save motion revision</button>
      </div>
    </>}
  </section>;
}
