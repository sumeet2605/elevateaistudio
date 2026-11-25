"use client";

import { Check, Download, Film, X } from "lucide-react";
import { useEffect, useState } from "react";
import { api, request } from "./api";

type Span = { start_ms: number; end_ms: number };
type Brand = { primary: string; highlight: string; background: string; width: number; height: number };
type ProjectPlan = { id: string; brand: Brand; brand_revision: string; playbook_revision: string; issue: string | null };
type RenderSettings = { framing: "fit" | "crop"; quality: "standard" | "high"; playback_speed: 1 | 1.1; caption_words: number; caption_gap_ms: number; caption_min_ms: number };
type Job = { id: string; status: string; stage: string; progress: number | null; error: string | null };
type CaptionWord = { text: string; highlighted: boolean };
type Render = {
  id: string; decision: "approved" | "rejected" | null; bound_approval: boolean; stale_reason: string | null;
  manifest: { plan_id: string; cuts: Span[]; notices: string[]; brand: Brand; settings: RenderSettings };
  metadata: { created_at: string; sha256: string; width: number; height: number; fps: number; video_codec: string; audio_codec: string;
    duration_seconds: number; render_seconds: number; caption_count: number; omitted_words: number;
    caption_sample: { time_ms: number; lines: CaptionWord[][] }[] };
};
type State = { selected: ProjectPlan | null; job: Job | null; history: Render[] };

const defaultSettings: RenderSettings = { framing: "fit", quality: "standard", playback_speed: 1, caption_words: 7, caption_gap_ms: 380, caption_min_ms: 650 };
const stageNames: Record<string, string> = {
  queued: "Waiting for the local worker", extracting_audio: "Preparing render", checking_source: "Checking the original",
  building_captions: "Building captions", encoding_preview: "Encoding preview", verifying_preview: "Verifying the MP4",
  publishing_preview: "Saving preview", completed: "Preview ready", failed: "Render failed",
  cancelled: "Render cancelled", cancelling: "Cancellation requested", interrupted: "Render interrupted",
};
const format = (ms: number) => (ms / 1000).toFixed(2) + " s";

export default function RenderStudio({ projectId, ready, seek }: { projectId: string; ready: boolean; seek: (seconds: number) => void }) {
  const root = `/projects/${projectId}/render`;
  const [state, setState] = useState<State>();
  const [settings, setSettings] = useState(defaultSettings);
  const [selectedRender, setSelectedRender] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [error, setError] = useState("");
  const [connection, setConnection] = useState("");
  const [busy, setBusy] = useState(false);
  const [playbackError, setPlaybackError] = useState(false);
  const job = state?.job;
  const active = job?.status === "queued" || job?.status === "running";
  const render = state?.history.find(({ id }) => id === selectedRender) ?? state?.history[0];
  const brand = render?.manifest.brand ?? state?.selected?.brand;

  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    async function poll() {
      try {
        const next = await request<State>(root);
        if (stopped) return;
        setState(next); setConnection("");
        if (next.job?.status === "queued" || next.job?.status === "running") timer = window.setTimeout(poll, 500);
      } catch (problem) {
        if (!stopped) { setConnection((problem as Error).message); timer = window.setTimeout(poll, 1500); }
      }
    }
    void poll();
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [root, refresh]);

  function edit<K extends keyof RenderSettings>(key: K, value: RenderSettings[K]) {
    setSettings((current) => ({ ...current, [key]: value }));
  }

  function post<T>(suffix: string, body?: object) {
    return request<T>(root + suffix, { method: "POST", ...(body && {
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }) });
  }

  async function act(action: () => Promise<void>) {
    setBusy(true); setError("");
    try { await action(); }
    catch (problem) { setError((problem as Error).message); }
    finally { setBusy(false); }
  }

  function queue(suffix: string, body?: object) {
    void act(async () => {
      const next = await post<Job>(suffix, body);
      setState((current) => current && { ...current, job: next });
      setSelectedRender(""); setPlaybackError(false); setRefresh((value) => value + 1);
    });
  }

  function review(decision: "approved" | "rejected") {
    if (!render) return;
    void act(async () => {
      setState(await post<State>(`/${render.id}/review`, { decision }));
    });
  }

  return <section className="panel render-panel" id="render-panel" role="tabpanel" aria-labelledby="render-tab">
    <div className="section-head"><div className="icon"><Film size={19}/></div>
      <div><h2>Render</h2><p className="muted small">Preview and approve the exact MP4 you’ll export.</p></div></div>
    {(error || connection) && <p className="alert audio-metrics" role="alert">{error || connection}</p>}
    {state && !state.selected && <p className="muted small">Generate and review an AI Director plan first.</p>}
    {state?.selected && <>
      <p className="muted small">Selected plan <code>{state.selected.id.slice(0, 8)}</code> · {state.selected.playbook_revision} · Brand {state.selected.brand_revision.slice(0, 8)}</p>
      {state.selected.issue && <p className="alert audio-metrics">{state.selected.issue}</p>}
      <details className="detection-settings">
        <summary>Render settings · 1080 × 1920</summary>
        <form className="settings-grid render-settings" id="render-settings" onSubmit={(event) => {
          event.preventDefault(); queue("", { plan_id: state.selected?.id, settings });
        }}>
          <label>Framing <select value={settings.framing} onChange={(event) => edit("framing", event.target.value as RenderSettings["framing"])}>
            <option value="fit">Fit · preserve the full frame</option><option value="crop">Crop · fill portrait frame</option>
          </select></label>
          <label>Quality <select value={settings.quality} onChange={(event) => edit("quality", event.target.value as RenderSettings["quality"])}>
            <option value="standard">Standard · faster encode</option><option value="high">High · cleaner detail</option>
          </select></label>
          <label>Playback speed <select value={settings.playback_speed}
            onChange={(event) => edit("playback_speed", Number(event.target.value) as RenderSettings["playback_speed"])}>
            <option value={1}>1× · original pace</option><option value={1.1}>1.1× · faster pace</option>
          </select></label>
          {([["caption_words", "Words per cue", 3, 14], ["caption_gap_ms", "Caption pause gap (ms)", 120, 1000],
            ["caption_min_ms", "Preferred minimum cue (ms)", 300, 1500]] as const).map(([key, name, min, max]) =>
            <label key={key}>{name}<input type="number" min={min} max={max} required value={Number.isNaN(settings[key]) ? "" : settings[key]}
              onChange={(event) => edit(key, event.target.valueAsNumber)}/></label>)}
        </form>
      </details>
      {!active && <button className="primary" form="render-settings" disabled={busy || !!state.selected.issue}>Generate Preview</button>}
    </>}
    {job && <div className="job-state audio-metrics" aria-live="polite">
      <span>{stageNames[job.stage] ?? job.status}</span>
      {active && job.progress !== null && <span>{Math.floor(job.progress * 100)}%</span>}
      {active && <progress aria-label="Rendering progress" max={1} value={job.progress ?? undefined}/>}
      {job.error && <p className="muted small">{job.error}</p>}
    </div>}
    {job && <div className="transcript-actions">
      {active && <button className="secondary" disabled={busy || job.stage === "cancelling"}
        onClick={() => queue(`/jobs/${job.id}/cancel`)}>Cancel render</button>}
      {(job.status === "failed" || job.status === "interrupted") && <button className="secondary" disabled={busy}
        onClick={() => queue(`/jobs/${job.id}/retry`)}>Retry render</button>}
    </div>}
    {render && <>
      <label className="plan-history">Render history <select value={render.id} onChange={(event) => {
        setSelectedRender(event.target.value); setPlaybackError(false);
      }}>{state?.history.map((item) => <option key={item.id} value={item.id}>
        {item.id.slice(0, 8)} · {item.decision ?? "pending review"} · {new Date(item.metadata.created_at).toLocaleString()}
      </option>)}</select></label>
      <div className="render-frame"><video key={render.id} aria-label="Rendered video preview" controls playsInline preload="metadata"
        src={`${api}${root}/${render.id}/preview`} onError={() => setPlaybackError(true)}/></div>
      {playbackError && <p className="alert audio-metrics" role="alert">This browser could not play the preview.</p>}
      <p className="small audio-metrics">{render.metadata.width} × {render.metadata.height} · {render.metadata.fps} fps · H.264 / AAC · {format(render.metadata.duration_seconds * 1000)}{render.manifest.settings.playback_speed === 1.1 ? " · 1.1×" : ""}</p>
      <p className="muted small">{render.metadata.caption_count} caption cues · {render.metadata.render_seconds.toFixed(2)} s render · Plan {render.manifest.plan_id.slice(0, 8)}</p>
      {render.stale_reason && <p className="alert audio-metrics" role="status">{render.stale_reason}</p>}
      <div className="transcript-actions">
        <button className="secondary" disabled={busy || !!render.stale_reason || render.decision === "approved"}
          onClick={() => review("approved")}><Check size={15}/> Approve preview</button>
        <button className="secondary" disabled={busy || render.decision === "rejected"}
          onClick={() => review("rejected")}><X size={15}/> Reject preview</button>
        {render.bound_approval && !render.stale_reason && <a className="primary" href={`${api}${root}/${render.id}/final`}>
          <Download size={15}/> Download Final MP4</a>}
      </div>
      <p className="muted small audio-metrics">Preview decision · {render.decision ?? "Pending review"}</p>
      <details className="detection-settings">
        <summary>Source cuts · {render.manifest.cuts.length}</summary>
        <ol className="render-cuts">{render.manifest.cuts.map((range) =>
          <li key={range.start_ms}><button className="timecode" disabled={!ready}
            onClick={() => seek(range.start_ms / 1000)}>{format(range.start_ms)}</button> to {format(range.end_ms)}</li>)}</ol>
        {render.manifest.notices.map((notice) => <p className="muted small" key={notice}>{notice}</p>)}
        {render.metadata.omitted_words > 0 && <p className="muted small">{render.metadata.omitted_words} transcript words were removed with cuts or omitted without usable timing.</p>}
      </details>
    </>}
    {brand && <details className="detection-settings" open={!render}>
      <summary>Caption style preview</summary>
      <div className="caption-style" style={{ background: brand.background, color: brand.primary }}>
        {render?.metadata.caption_sample.length ? render.metadata.caption_sample[0].lines.map((line, index) =>
          <div key={index}>{line.map((word, wordIndex) => <span key={wordIndex} style={{ color: word.highlighted ? brand.highlight : brand.primary }}>
            {wordIndex > 0 ? " " : ""}{word.text}
          </span>)}</div>) : <div>Caption text <span style={{ color: brand.highlight }}>highlight</span></div>}
      </div>
      <p className="muted small">Noto Sans Bold · Noto Sans Devanagari for Hindi</p>
    </details>}
  </section>;
}
