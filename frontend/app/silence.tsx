"use client";

import { Activity, Check, Download, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api, request } from "./api";

type Span = { start_ms: number; end_ms: number };
type Status = "pending" | "accepted" | "rejected" | "modified";
type Cut = {
  id: string; source: Span; proposed: Span; modification: Span | null; status: Status;
  confidence: "high" | "ambiguous"; reason: string; removable_duration_ms: number;
};
type Settings = {
  threshold_dbfs: number; minimum_silence_ms: number; leading_padding_ms: number;
  trailing_padding_ms: number; minimum_removable_ms: number; noise_aware: boolean;
};
type AnalysisState = {
  plan_id: string | null; settings: Settings; presets: Record<string, Settings>;
  job: { id: string; status: string; stage: string; progress: number | null; error: string | null } | null;
};
type Plan = {
  id: string; transcript_version: string | null; source_duration_ms: number; proposed_cuts: Cut[];
  approved_cuts: Span[]; estimated_edited_duration_ms: number; effective_threshold_dbfs: number;
};
type Waveform = {
  peak_dbfs: number; rms_dbfs: number; noise_floor_dbfs: number; clipped_samples: number;
  waveform: { bucket_seconds: number; peaks: number[] };
};
type History = { id: string; created_at: string; approved_count: number };

const stages: Record<string, string> = {
  queued: "Waiting for the local worker", extracting_audio: "Extracting audio", measuring_audio: "Measuring audio",
  detecting_quiet: "Measuring quiet intervals", detecting_speech: "Detecting speech", building_plan: "Building cut suggestions",
  saving_plan: "Saving review plan", completed: "Analysis ready", failed: "Analysis failed",
  interrupted: "Analysis interrupted", cancelling: "Cancellation requested", cancelled: "Analysis cancelled",
};
const fields: [keyof Omit<Settings, "noise_aware">, string, number, number][] = [
  ["threshold_dbfs", "Threshold (dBFS)", -90, -5], ["minimum_silence_ms", "Minimum silence (ms)", 200, 30000],
  ["leading_padding_ms", "Leading padding (ms)", 0, 5000], ["trailing_padding_ms", "Trailing padding (ms)", 0, 5000],
  ["minimum_removable_ms", "Minimum cut (ms)", 50, 10000],
];
const colors: Record<Status, string> = { pending: "#b58b4e50", accepted: "#4fae795a", rejected: "#b46b7350", modified: "#4fae795a" };

function seconds(ms: number) { return (ms / 1000).toFixed(2) + " s"; }

function Wave({ data, plan, position, seek, ready }: {
  data: Waveform; plan: Plan; position: number; seek: (s: number) => void; ready: boolean;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const duration = plan.source_duration_ms;
  useEffect(() => {
    const element = canvas.current;
    const ctx = element?.getContext("2d");
    if (!element || !ctx) return;
    function draw() {
      if (!element || !ctx) return;
      const { width, height } = element.getBoundingClientRect();
      const scale = window.devicePixelRatio || 1;
      element.width = width * scale;
      element.height = height * scale;
      ctx.scale(scale, scale);
      const x = (ms: number) => ms / duration * width;
      ctx.fillStyle = "#111316";
      ctx.fillRect(0, 0, width, height);
      for (const cut of plan.proposed_cuts) {
        ctx.fillStyle = colors[cut.status];
        ctx.fillRect(x(cut.source.start_ms), 0, x(cut.source.end_ms - cut.source.start_ms), height);
      }
      const step = data.waveform.bucket_seconds * 1000;
      ctx.fillStyle = "#dbc28c";
      data.waveform.peaks.forEach((peak, index) => {
        const half = Math.max(0.7, Math.sqrt(peak) * height * 0.44);
        ctx.fillRect(x(index * step), height / 2 - half, Math.max(1, x(step) - 0.5), 2 * half);
      });
      ctx.fillStyle = "#fff4d9";
      ctx.fillRect(Math.min(width - 2, x(position * 1000)), 0, 2, height);
    }
    draw();
    const observer = new ResizeObserver(draw);
    observer.observe(element);
    return () => observer.disconnect();
  }, [data, duration, plan.proposed_cuts, position]);
  return <div className="wave">
    <canvas ref={canvas} aria-hidden="true" onPointerDown={(event) => {
      if (!ready) return;
      const rect = event.currentTarget.getBoundingClientRect();
      seek(Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)) * duration / 1000);
    }} />
    <div className="wave-controls">
      <span>{seconds(position * 1000)}</span>
      <input aria-label="Waveform playhead" type="range" min={0} max={duration} step={1}
        value={Math.min(duration, Math.round(position * 1000))} disabled={!ready}
        onChange={(event) => seek(Number(event.target.value) / 1000)} />
      <span>{seconds(duration)}</span>
    </div>
    <div className="wave-legend">
      <span className="legend-pending">Pending</span><span className="legend-approved">Approved</span><span className="legend-rejected">Rejected</span>
    </div>
  </div>;
}

function CutItem({ cut, ready, disabled, seek, review }: {
  cut: Cut; ready: boolean; disabled: boolean; seek: (s: number) => void;
  review: (status: Status, modification?: Span) => Promise<boolean>;
}) {
  const span = cut.modification ?? cut.proposed;
  const [start, setStart] = useState(String(span.start_ms / 1000));
  const [end, setEnd] = useState(String(span.end_ms / 1000));
  const [open, setOpen] = useState(false);
  useEffect(() => {
    setStart(String(span.start_ms / 1000));
    setEnd(String(span.end_ms / 1000));
  }, [span.start_ms, span.end_ms]);

  return <li className="cut-item">
    <div className="cut-heading">
      <button className="timecode" disabled={!ready} onClick={() => seek(cut.source.start_ms / 1000)}
        aria-label={`Seek to silence at ${seconds(cut.source.start_ms)}`}>{seconds(cut.source.start_ms)}</button>
      <strong>{cut.confidence === "high" ? "High-confidence silence" : "Ambiguous audio"}</strong>
      <span className={`cut-status ${cut.status}`}>{cut.status}</span>
    </div>
    <p className="muted small">{cut.reason}</p>
    <p className="small">Cut {seconds(span.start_ms)}–{seconds(span.end_ms)} <span className="muted">· {seconds(span.end_ms - span.start_ms)} removable</span></p>
    <div className="transcript-actions">
      {cut.status !== "accepted" && <button className="secondary" disabled={disabled} onClick={() => void review("accepted")}><Check size={14}/> Accept</button>}
      {cut.status !== "rejected" && <button className="secondary" disabled={disabled} onClick={() => void review("rejected")}><X size={14}/> Reject</button>}
      <button className="secondary" disabled={disabled} aria-expanded={open} onClick={() => setOpen(!open)}>Adjust</button>
      {cut.status !== "pending" && <button className="secondary" disabled={disabled} onClick={() => void review("pending")}>Reset to pending</button>}
    </div>
    {open && <form className="cut-adjust" onSubmit={async (event) => {
      event.preventDefault();
      if (await review("modified", { start_ms: Math.round(Number(start) * 1000), end_ms: Math.round(Number(end) * 1000) })) setOpen(false);
    }}>
      <label>Cut start (s)<input required type="number" step=".001" min={cut.source.start_ms / 1000} max={cut.source.end_ms / 1000}
        value={start} onChange={(event) => setStart(event.target.value)} /></label>
      <label>Cut end (s)<input required type="number" step=".001" min={cut.source.start_ms / 1000} max={cut.source.end_ms / 1000}
        value={end} onChange={(event) => setEnd(event.target.value)} /></label>
      <button className="secondary" disabled={disabled}>Save adjustment</button>
    </form>}
  </li>;
}

export default function SilenceDetection({ projectId, hasAudio, ready, position, seek }: {
  projectId: string; hasAudio: boolean; ready: boolean; position: number; seek: (s: number) => void;
}) {
  const root = `/projects/${projectId}/analysis`;
  const [state, setState] = useState<AnalysisState | null>(null);
  const [draft, setDraft] = useState<Settings | null>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [wave, setWave] = useState<Waveform | null>(null);
  const [history, setHistory] = useState<History[]>([]);
  const [version, setVersion] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [networkError, setNetworkError] = useState("");
  const job = state?.job;
  const active = job?.status === "queued" || job?.status === "running";
  const editable = plan?.id === state?.plan_id;
  const targetVersion = version || state?.plan_id;
  const proposed = plan?.proposed_cuts.filter((cut) => cut.status !== "rejected").reduce(
    (sum, cut) => sum + (cut.modification ? cut.modification.end_ms - cut.modification.start_ms : cut.removable_duration_ms), 0);
  const dirty = draft && state && Object.keys(draft).some((key) => draft[key as keyof Settings] !== state.settings[key as keyof Settings]);

  useEffect(() => {
    let ended = false;
    let timer: number | undefined;
    async function poll() {
      try {
        const next = await request<AnalysisState>(root);
        if (ended) return;
        setState(next);
        setDraft((current) => current ?? next.settings);
        setNetworkError("");
        if (next.job?.status === "queued" || next.job?.status === "running") {
          timer = window.setTimeout(poll, 600);
        }
      } catch (problem) {
        if (ended) return;
        setNetworkError((problem as Error).message);
        timer = window.setTimeout(poll, 1600);
      }
    }
    void poll();
    return () => { ended = true; window.clearTimeout(timer); };
  }, [root, refresh]);

  useEffect(() => {
    if (!targetVersion) return;
    let ended = false;
    const query = "?plan_id=" + targetVersion;
    Promise.all([
      request<Plan>(root + "/plan" + query), request<Waveform>(root + "/waveform" + query),
      request<History[]>(root + "/plans"),
    ]).then(([next, data, versions]) => {
      if (ended) return;
      setPlan(next);
      setWave(data);
      setHistory(versions);
    }).catch((problem: Error) => { if (!ended) setError(problem.message); });
    return () => { ended = true; };
  }, [root, targetVersion, state?.plan_id]);

  async function change(action: () => Promise<unknown>) {
    setBusy(true);
    setError("");
    try {
      await action();
      return true;
    } catch (problem) {
      setError((problem as Error).message);
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function run(suffix: string) {
    await change(async () => {
      const next = await request<AnalysisState["job"]>(root + suffix, { method: "POST" });
      setState((current) => current && { ...current, job: next });
      setRefresh((current) => current + 1);
    });
  }

  async function review(suffix: string, decision?: { status: Status; modification?: Span }) {
    if (!plan) return false;
    return change(async () => {
      const next = await request<Plan>(root + "/plans/" + plan.id + suffix, {
        method: "POST", ...(decision && { headers: { "Content-Type": "application/json" }, body: JSON.stringify(decision) }),
      });
      setPlan(next);
    });
  }

  return <section className="panel silence-panel" id="silence-panel" role="tabpanel" aria-labelledby="silence-tab">
    <div className="section-head"><div className="icon"><Activity size={19}/></div>
      <div><h2>Silence Detection</h2><p className="muted small">Review pauses and estimate the shorter edit.</p></div>
    </div>
    {(error || networkError) && <p role="alert" className="alert">{error || networkError}</p>}
    {!hasAudio && <p className="muted small">This video has no audio track to analyse.</p>}
    {job && <div className="job-state" aria-live="polite">
      <span>{stages[job.stage] ?? job.status}</span>
      {active && job.progress != null && <span>{Math.round(job.progress * 100)}%</span>}
      {active && <progress aria-label="Audio analysis progress" max={1} value={job.progress ?? undefined}/>}
      {job.error && <p className="muted small">{job.error}</p>}
    </div>}
    {state && hasAudio && <div className="transcript-actions">
      {active ? <button className="secondary" disabled={busy || job.stage === "cancelling"} onClick={() => void run(`/jobs/${job.id}/cancel`)}>Cancel analysis</button> :
        <button className="primary" disabled={busy || !!dirty} onClick={() => { setVersion(""); void run(plan ? "/regenerate" : ""); }}>
          <Activity size={16}/> {state.plan_id ? "Regenerate suggestions" : "Analyse Audio"}
        </button>}
      {(job?.status === "failed" || job?.status === "interrupted") &&
        <button className="secondary" disabled={busy} onClick={() => void run(`/jobs/${job.id}/retry`)}>Retry</button>}
    </div>}
    {state && draft && hasAudio && <details className="detection-settings">
      <summary>Detection settings</summary>
      <div className="preset-row">
        {Object.entries(state.presets).map(([key, value]) => {
          const selected = Object.keys(value).every((field) => draft[field as keyof Settings] === value[field as keyof Settings]);
          return <button key={key} type="button" className={`secondary ${selected ? "chosen" : ""}`} aria-pressed={selected}
            onClick={() => setDraft(value)}>{({ gentle: "Gentle", balanced: "Balanced", "fast-paced": "Fast-paced" })[key] ?? key}</button>;
        })}
      </div>
      <form onSubmit={(event) => {
        event.preventDefault();
        void change(async () => {
          const next = await request<Settings>(root + "/settings", {
            method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(draft),
          });
          setDraft(next);
          setState((current) => current && { ...current, settings: next });
        });
      }}>
        <div className="settings-grid">
          {fields.map(([key, label, min, max]) => <label key={key}>{label}
            <input type="number" min={min} max={max} step={key === "threshold_dbfs" ? "0.1" : "1"}
              required value={Number.isNaN(draft[key]) ? "" : draft[key]}
              onChange={(event) => setDraft({ ...draft, [key]: event.target.valueAsNumber })} />
          </label>)}
        </div>
        <label className="noise-setting"><input type="checkbox" checked={draft.noise_aware}
          onChange={(event) => setDraft({ ...draft, noise_aware: event.target.checked })} /> Adjust for estimated noise floor</label>
        <button className="secondary" disabled={busy || !dirty}>Save settings</button>
      </form>
    </details>}
    {plan && wave && <>
      <div className="analysis-totals">
        <div><span>Original</span><strong>{seconds(plan.source_duration_ms)}</strong></div>
        <div><span>Estimated edit</span><strong>{seconds(plan.estimated_edited_duration_ms)}</strong></div>
        <div><span>Removable</span><strong>{seconds(proposed ?? 0)}</strong></div>
        <div><span>Approved</span><strong>{seconds(plan.source_duration_ms - plan.estimated_edited_duration_ms)}</strong></div>
      </div>
      <Wave data={wave} plan={plan} position={position} seek={seek} ready={ready} />
      <p className="muted small audio-metrics">Peak {wave.peak_dbfs} · RMS {wave.rms_dbfs} · Noise floor {wave.noise_floor_dbfs} · Threshold {plan.effective_threshold_dbfs} dBFS
        {wave.clipped_samples > 0 && ` · ${wave.clipped_samples} full-scale samples`}</p>
      {!plan.transcript_version && <p className="muted small audio-metrics">Generate a transcript and regenerate suggestions to check word boundaries.</p>}
      <div className="transcript-actions">
        {editable && plan.proposed_cuts.some((cut) => cut.confidence === "high" && cut.status === "pending") &&
          <button className="secondary" disabled={busy} onClick={() => void review("/accept-high")}>Accept high confidence</button>}
        <a className="secondary" href={api + root + "/export?plan_id=" + plan.id}><Download size={15}/> Export editing plan</a>
      </div>
      {history.length > 1 && <label className="plan-history">Plan version <select value={version} onChange={(event) => setVersion(event.target.value)}>
        <option value="">Current plan</option>
        {history.filter(({ id }) => id !== state?.plan_id).map((item) =>
          <option key={item.id} value={item.id}>{new Date(item.created_at).toLocaleString()} · {item.approved_count} approved</option>)}
      </select></label>}
      {plan.proposed_cuts.length ? <ol className="cut-list" aria-label="Cut suggestions">
        {plan.proposed_cuts.map((cut) => <CutItem key={cut.id} cut={cut} disabled={busy || !editable}
          ready={ready} seek={seek} review={(status, modification) => review(`/suggestions/${cut.id}`, { status, modification })}/>)}
      </ol> : <p className="muted small audio-metrics">No intervals met these detection settings.</p>}
    </>}
  </section>;
}
