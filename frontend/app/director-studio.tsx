"use client";

import { Clapperboard, Download } from "lucide-react";
import { useEffect, useState } from "react";
import { api, request } from "./api";

type Playbook = { id: string; name: string; revision: number; objective: string; pacing: string; target_style: string };
type Usage = { id: string; mode: string; model: string; estimated_input_tokens: number; input_tokens: number | null;
  output_tokens: number | null; cached_tokens: number | null; cost_usd: number | null };
type State = { mode: string; model: string; credentials_ready: boolean; selected: string | null; playbooks: Playbook[];
  usage: Usage[]; job: { id: string; status: string; stage: string; error: string | null } | null };
type Span = { start_ms: number; end_ms: number };
type Op = Span & { kind: string; reason: string; quote: string | null; idea: string | null; zoom_percent: number | null;
  status: "pending" | "accepted" | "rejected" };
type Revision = { id: string; event: string; created_at: string; playbook_revision: string };
type Plan = Revision & { rationale: string; operations: Op[]; model: string; mode: string; brand_revision: string;
  stale_silence: boolean; source_duration_ms: number; edited_duration_ms: number;
  base_approved_cuts: Span[]; proposed_retained_segments: Span[] };

const stages: Record<string, string> = { queued: "Waiting for local worker", running: "Preparing context",
  extracting_audio: "Preparing context", requesting_director: "Generating recommendations", validating_director: "Validating decisions",
  saving_director: "Saving draft", completed: "Draft ready", failed: "Generation failed", interrupted: "Generation interrupted",
  cancelling: "Cancellation requested", cancelled: "Generation cancelled" };
const names: Record<string, string> = { cut: "Cut", hook: "Hook", emphasis: "Caption emphasis", zoom: "Zoom", b_roll: "B-roll idea", audio: "Audio" };
const time = (ms: number) => (ms / 1000).toFixed(2) + " s";
const label = (revision: Revision) => `${revision.event} · ${revision.playbook_revision} · ${revision.id.slice(0, 8)}`;

export default function DirectorStudio({ projectId, ready, seek, position }: {
  projectId: string; ready: boolean; position: number; seek: (seconds: number) => void;
}) {
  const root = `/projects/${projectId}/director`;
  const [state, setState] = useState<State>();
  const [playbook, setPlaybook] = useState("mystical-sumeet");
  const [estimate, setEstimate] = useState<number | null>(null);
  const [consent, setConsent] = useState(false);
  const [versions, setVersions] = useState<Revision[]>([]);
  const [viewed, setViewed] = useState("");
  const [plan, setPlan] = useState<Plan>();
  const [compared, setCompared] = useState<Plan>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [cutStart, setCutStart] = useState("");
  const [cutEnd, setCutEnd] = useState("");
  const [refresh, setRefresh] = useState(0);
  const job = state?.job;
  const active = job?.status === "queued" || job?.status === "running";
  const target = viewed || state?.selected;
  const selectedPlaybook = state?.playbooks.find(({ id }) => id === playbook);
  const retryPaid = state?.usage.find(({ id }) => id === job?.id)?.mode === "openai";
  const needsConsent = state?.mode === "openai" || retryPaid;

  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    async function poll() {
      try {
        const next = await request<State>(root);
        if (stopped) return;
        setState(next);
        if (next.job?.status === "queued" || next.job?.status === "running") timer = window.setTimeout(poll, 550);
      } catch (problem) {
        if (!stopped) { setError((problem as Error).message); timer = window.setTimeout(poll, 1500); }
      }
    }
    void poll();
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [root, refresh]);

  useEffect(() => {
    if (!target) return;
    let stopped = false;
    Promise.all([request<Plan>(root + "/revisions/" + target), request<Revision[]>(root + "/revisions")])
      .then(([item, items]) => { if (!stopped) { setPlan(item); setVersions(items); } })
      .catch((problem: Error) => { if (!stopped) setError(problem.message); });
    return () => { stopped = true; };
  }, [root, target, state?.selected]);

  async function act(action: () => Promise<void>) {
    setBusy(true); setError("");
    try { await action(); }
    catch (problem) { setError((problem as Error).message); }
    finally { setBusy(false); }
  }
  function post<T>(path: string, body?: object) {
    return request<T>(root + path, { method: "POST", ...(body && {
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }) });
  }
  function queue(path: string, body?: object) {
    void act(async () => {
      const next = await post<State["job"]>(path, body);
      setState((current) => current && { ...current, job: next });
      setViewed(""); setConsent(false); setRefresh((value) => value + 1);
    });
  }
  function revise(path: string, body?: object) {
    if (!plan) return;
    void act(async () => {
      const next = await post<Plan>(`/revisions/${plan.id}${path}`, body);
      setViewed(""); setPlan(next);
      setState((current) => current && { ...current, selected: next.id });
    });
  }

  return <section id="director-panel" role="tabpanel" aria-labelledby="director-tab" className="panel director-panel">
    <div className="section-head"><div className="icon"><Clapperboard size={19}/></div>
      <div><h2>AI Director</h2><p className="muted small">Generate editing instructions for review.</p></div></div>
    {state && <>
      <div className="job-state"><span>{state.mode === "mock" ? "Deterministic mock · no API charge" : `OpenAI · ${state.model}`}</span>
        {state.mode === "openai" && !state.credentials_ready && <span>Server key missing</span>}</div>
      <label className="plan-history">Editing playbook <select value={playbook} onChange={(event) => {
        setPlaybook(event.target.value); setEstimate(null); setConsent(false);
      }}>{state.playbooks.map((item) => <option key={item.id} value={item.id}>{item.name} · v{item.revision}</option>)}</select></label>
      {selectedPlaybook && <p className="muted small audio-metrics">{selectedPlaybook.objective} {selectedPlaybook.pacing}</p>}
      {estimate !== null && <p className="small audio-metrics">Approximate input · {estimate.toLocaleString()} tokens</p>}
      {needsConsent && <label className="noise-setting small"><input type="checkbox" checked={consent}
        onChange={(event) => setConsent(event.target.checked)}/>
        Send the transcript and editing context to OpenAI and authorize this paid request.</label>}
      <div className="transcript-actions">
        <button className="secondary" disabled={busy || active} onClick={() => void act(async () => {
          const data = await request<{ estimated_input_tokens: number }>(root + "/estimate?playbook=" + playbook);
          setEstimate(data.estimated_input_tokens);
        })}>Estimate input</button>
        {!active && <button className="primary" disabled={busy || (state.mode === "openai" && (!consent || !state.credentials_ready))}
          onClick={() => queue("", { playbook, confirm_paid: consent })}>{plan ? "Regenerate plan" : "Generate edit plan"}</button>}
        {active && <button className="secondary" disabled={busy || job.stage === "cancelling"}
          onClick={() => queue(`/jobs/${job.id}/cancel`)}>Cancel generation</button>}
        {(job?.status === "failed" || job?.status === "interrupted") &&
          <button className="secondary" disabled={busy || (retryPaid && !consent)}
            onClick={() => queue(`/jobs/${job.id}/retry`, { confirm_paid: consent })}>Retry generation</button>}
      </div>
    </>}
    {error && <p className="alert audio-metrics" role="alert">{error}</p>}
    {job && <div className="job-state audio-metrics" aria-live="polite">
      <span>{stages[job.stage] ?? job.status}</span>{active && <progress aria-label="Director progress"/>}
      {job.error && <p className="muted small">{job.error}</p>}
    </div>}
    {plan && <>
      <label className="plan-history">View revision <select value={viewed} onChange={(event) => setViewed(event.target.value)}>
        <option value="">Current revision</option>{versions.filter(({ id }) => id !== state?.selected)
          .map((item) => <option value={item.id} key={item.id}>{label(item)}</option>)}
      </select></label>
      {plan.stale_silence && <p className="alert audio-metrics">Silence decisions changed. Generate a fresh draft before review or export.</p>}
      <p className="audio-metrics small">{plan.rationale}</p>
      <p className="muted small audio-metrics">{plan.model} · {plan.playbook_revision} · Brand {plan.brand_revision.slice(0, 8)}</p>
      <div className="director-lanes" aria-label="Source timeline of proposed edits">
        {["Approved silence", "New cuts", "Recorded phrases", "Visual and audio"].map((name, lane) => {
          const ranges = lane === 0 ? plan.base_approved_cuts : plan.operations.filter((op) => lane === 1
            ? op.kind === "cut" : lane === 2 ? ["hook", "emphasis"].includes(op.kind)
            : ["zoom", "b_roll", "audio"].includes(op.kind));
          return <div key={name}><span>{name}</span><div>
            {ranges.map((span, index) => <i key={index} className={`lane-${lane}`} style={{
              left: `${100 * span.start_ms / plan.source_duration_ms}%`,
              width: `${100 * (span.end_ms - span.start_ms) / plan.source_duration_ms}%`,
            }}/>)}</div></div>;
        })}
      </div>
      <p className="muted small">Original {time(plan.source_duration_ms)} · Reviewed edit {time(plan.edited_duration_ms)}</p>
      <div className="transcript-actions">
        {plan.id !== state?.selected && <button className="secondary" disabled={busy} onClick={() => revise("/restore")}>Restore as new revision</button>}
        {!plan.stale_silence && <a className="secondary" href={`${api}${root}/revisions/${plan.id}/export`}>
          <Download size={15}/> Export reviewed plan</a>}
      </div>
      {versions.length > 1 && <label className="plan-history">Compare with <select value={compared?.id ?? ""} onChange={(event) => {
        const id = event.target.value;
        if (!id) { setCompared(undefined); return; }
        void act(async () => setCompared(await request<Plan>(root + "/revisions/" + id)));
      }}><option value="">Choose a revision</option>{versions.filter(({ id }) => id !== plan.id)
        .map((item) => <option value={item.id} key={item.id}>{label(item)}</option>)}</select></label>}
      {compared && <div className="comparison small">
        {[plan, compared].map((item) => <div key={item.id}><strong>{label(item)}</strong><p>{time(item.edited_duration_ms)} reviewed</p>
          <ul>{item.operations.map((op, index) => <li key={index}>{names[op.kind]} · {time(op.start_ms)} · {op.status}</li>)}</ul></div>)}
      </div>}
      {plan.id === state?.selected && !plan.stale_silence && <details className="detection-settings">
        <summary>Story cuts</summary>
        <p className="muted small audio-metrics">Mark a section to remove using the original video’s clock.</p>
        <form onSubmit={(event) => {
          event.preventDefault();
          revise("/cuts", { start_ms: Math.round(Number(cutStart) * 1000), end_ms: Math.round(Number(cutEnd) * 1000) });
        }}>
          <div className="settings-grid render-settings">
            <label>Cut start (seconds)<input type="number" min={0} max={plan.source_duration_ms / 1000}
              step={0.001} required value={cutStart} onChange={(event) => setCutStart(event.target.value)}/></label>
            <label>Cut end (seconds)<input type="number" min={0} max={plan.source_duration_ms / 1000}
              step={0.001} required value={cutEnd} onChange={(event) => setCutEnd(event.target.value)}/></label>
          </div>
          <div className="transcript-actions">
            <button className="secondary" type="button" disabled={!ready || busy}
              onClick={() => setCutStart(position.toFixed(3))}>Mark start at playhead</button>
            <button className="secondary" type="button" disabled={!ready || busy}
              onClick={() => setCutEnd(position.toFixed(3))}>Mark end at playhead</button>
            <button className="primary" type="submit" disabled={busy || active || !cutStart || !cutEnd ||
              Math.round(Number(cutEnd) * 1000) - Math.round(Number(cutStart) * 1000) < 100}>Add story cut</button>
          </div>
        </form>
      </details>}
      <ol className="cut-list" aria-label="Director recommendations">
        {plan.operations.map((operation, index) => <li key={index} className="cut-item">
          <div className="cut-heading"><button className="timecode" disabled={!ready} onClick={() => seek(operation.start_ms / 1000)}>
            {time(operation.start_ms)}</button><strong>{operation.reason === "Manual story cut." ? "Story cut" : names[operation.kind]}</strong><span className={`cut-status ${operation.status}`}>{operation.status}</span></div>
          <p className="small">{operation.quote || operation.idea || (operation.zoom_percent ? `${operation.zoom_percent}% zoom` : `Through ${time(operation.end_ms)}`)}</p>
          <p className="muted small">{operation.reason}</p>
          {plan.id === state?.selected && !plan.stale_silence && <div className="transcript-actions">
            <button className="secondary" disabled={busy || operation.status === "accepted"} onClick={() => revise(`/operations/${index}`, { status: "accepted" })}>Accept</button>
            <button className="secondary" disabled={busy || operation.status === "rejected"} onClick={() => revise(`/operations/${index}`, { status: "rejected" })}>Reject</button>
          </div>}
        </li>)}
      </ol>
    </>}
    {!!state?.usage.length && <details className="detection-settings"><summary>API usage history</summary>
      {state.usage.map((usage) => <p key={usage.id} className="muted small audio-metrics">
        {usage.model} · {usage.estimated_input_tokens} estimated input · {usage.input_tokens ?? "—"} input · {usage.cached_tokens ?? "—"} cached · {usage.output_tokens ?? "—"} output
        {usage.cost_usd !== null ? ` · $${usage.cost_usd.toFixed(6)}` : " · Cost unavailable"}
      </p>)}</details>}
  </section>;
}
