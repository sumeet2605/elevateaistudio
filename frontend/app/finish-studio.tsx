"use client";

import { SlidersHorizontal } from "lucide-react";
import { useEffect, useState } from "react";
import { api, request } from "./api";

type Settings = Record<string, number | boolean>;
type Kind = "grade" | "voice";
type Revision = { id: string; preset: string; settings: Settings; created_at: string; analysis_id?: string };
type Meter = {input_i?: number | null; input_tp?: number | null; input_lra?: number | null};
type Analysis = { id: string; start: number; duration: number; histogram: number[];
  configuration?: Record<Kind, Settings>;
  dark_fraction: number | null; bright_fraction: number | null; available: string[];
  video: {interpretation: string; color_primaries?: string; color_transfer?: string; pix_fmt?: string};
  audio: {original: Meter; enhanced: Meter; waveform: number[]; rms_dbfs: number | null;
    quiet_window_rms_estimate: number | null} | null };
type Job = { id: string; status: string; progress: number | null; error: string | null };
type Studio = { plan_id: string | null; history: Record<Kind, Revision[]>; current: Record<Kind, Revision | null>; analysis: Analysis | null;
  job: Job | null; grade_presets: Record<string, [string, Settings]>; voice_presets: Record<string, [string, Settings]>;
  advice?: {grade: string; voice: string; grade_intensity: number; voice_intensity: number; rationale: string};
  recommendation: { grade: string; voice: string; rationale: string } };
type Control = [key: string, name: string, min: number, max: number, step: number];
const gradeControls: Control[] = [
  ["intensity","Grade intensity",0,1,.01], ["exposure","Exposure (stops)",-.5,.5,.01],
  ["brightness","Brightness",-.12,.12,.01], ["contrast","Contrast",-.3,.3,.01],
  ["saturation","Saturation",-.5,.35,.01], ["temperature","Temperature",-.08,.08,.005],
  ["tint","Tint",-.08,.08,.005], ["gamma","Gamma",-.25,.25,.01], ["highlights","Highlights",-.12,.12,.005],
  ["shadows","Shadows",-.12,.12,.005], ["black","Black level",-.08,.08,.005], ["white","White level",-.08,.08,.005],
  ["vibrance","Vibrance",-.3,.3,.01], ["sharpen","Sharpening",0,.7,.01], ["vignette","Vignette",0,.5,.01],
];
const voiceControls: Control[] = [
  ["intensity","Enhancement intensity",0,1,.01], ["gain","Input gain (dB)",-12,12,.1],
  ["highpass","High-pass (Hz)",0,180,1], ["denoise","Denoise (dB)",0,12,.1],
  ["presence","Presence EQ (dB)",-4,4,.1], ["threshold","Compressor threshold (dBFS)",-45,-6,.1],
  ["ratio","Compression ratio",1,4,.1], ["attack","Attack (ms)",2,100,1], ["release","Release (ms)",30,650,1],
  ["makeup","Makeup gain (dB)",0,6,.1], ["deess","De-essing",0,.55,.01],
  ["target","Target loudness (LUFS)",-28,-10,.1], ["peak","True-peak ceiling (dBTP)",-6,-1,.1],
];
const post = (body?: unknown): RequestInit => ({ method: "POST", headers: { "Content-Type": "application/json" },
  body: body === undefined ? undefined : JSON.stringify(body) });
const running = (job: Job | null) => job?.status === "queued" || job?.status === "running";
const display = (value?: number | null) => value == null ? "—" : value.toFixed(1);

function Bars({ values, label }: { values: number[]; label: string }) {
  const peak = Math.max(.01, ...values);
  return <div className="finish-bars" role="img" aria-label={label}>
    {values.map((value, index) => <i key={index} style={{ height: `${Math.max(2, value / peak * 100)}%` }}/>)}
  </div>;
}

export default function FinishStudio({ projectId, duration, position }: {projectId: string; duration: number; position: number}) {
  const root = `/projects/${projectId}/finish`;
  const [tab, setTab] = useState<Kind>("grade");
  const [state, setState] = useState<Studio | null>(null);
  const [draft, setDraft] = useState<Record<Kind, Settings>>({ grade: {}, voice: {} });
  const [selection, setSelection] = useState<Record<Kind, string>>({ grade: "", voice: "" });
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [busy, setBusy] = useState(false);
  const [matched, setMatched] = useState(true);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [historyId, setHistoryId] = useState("");

  useEffect(() => {
    let active = true;
    request<Studio>(root).then((result) => {
      if (!active) return;
      setState(result); setAnalysis(result.analysis); setJob(result.job);
      setSelection({ grade: result.current?.grade?.preset ?? result.recommendation.grade,
        voice: result.current?.voice?.preset ?? result.recommendation.voice });
      setDraft({ grade: result.current?.grade?.settings ?? result.grade_presets[result.recommendation.grade][1],
        voice: result.current?.voice?.settings ?? result.voice_presets[result.recommendation.voice][1] });
    }).catch((problem) => { if (active) setError(problem.message); });
    return () => { active = false; };
  }, [root]);

  useEffect(() => {
    if (!running(job)) return;
    let active = true;
    const timer = window.setInterval(() => {
      request<Studio>(root).then((result) => {
        if (!active) return;
        setJob(result.job);
        if (result.job?.status === "completed") { setAnalysis(result.analysis); setNotice("Rendered samples ready."); }
        if (result.job?.error) setError(result.job.error);
      }).catch((problem) => { if (active) setError(problem.message); });
    }, 900);
    return () => { active = false; window.clearInterval(timer); };
  }, [root, job]);

  async function perform(action: () => Promise<void>) {
    setBusy(true); setError(""); setNotice("");
    try { await action(); }
    catch (problem) { setError(problem instanceof Error ? problem.message : String(problem)); }
    finally { setBusy(false); }
  }

  function choose(preset: string) {
    if (!state) return;
    const presets = tab === "grade" ? state.grade_presets : state.voice_presets;
    setSelection({ ...selection, [tab]: preset }); setDraft({ ...draft, [tab]: presets[preset][1] });
  }
  const sampleUrl = (name: string) => `${api}${root}/analysis/${analysis?.id}/${name}`;
  const canMatch = analysis?.available.includes("original-matched.wav");
  const presets = tab === "grade" ? state?.grade_presets : state?.voice_presets;
  const matchesDraft = analysis?.configuration &&
    Object.entries(draft[tab]).every(([key, value]) => analysis.configuration?.[tab][key] === value);

  return <section className="panel finish-studio" role="tabpanel" id="finish-panel" aria-labelledby="finish-tab">
    <div className="section-head"><div className="icon"><SlidersHorizontal size={19}/></div><div>
      <h2>Colour &amp; Sound</h2><p className="muted small">Compare short FFmpeg samples before saving a revision.</p>
    </div></div>
    <div className="motion-actions finish-tabs" role="group" aria-label="Finishing controls">
      <button className="secondary" aria-pressed={tab === "grade"} onClick={() => { setTab("grade"); setHistoryId(""); }}>Colour</button>
      <button className="secondary" aria-pressed={tab === "voice"} onClick={() => { setTab("voice"); setHistoryId(""); }}>Sound</button>
    </div>
    {error && <p className="alert" role="alert">{error}</p>}
    {notice && <p className="muted small" role="status">{notice}</p>}
    {state && <div className="motion-actions finish-options">
      <label>Preset<select aria-label={tab === "grade" ? "Colour preset" : "Sound preset"} value={selection[tab]}
        onChange={(event) => choose(event.target.value)}>
        {Object.entries(presets ?? {}).map(([key, [name]]) => <option value={key} key={key}>{name}</option>)}</select></label>
      <button className="secondary" disabled={!selection[tab]} onClick={() => choose(selection[tab])}>Reset to preset</button>
      {state.advice && <button className="secondary" onClick={() => {
        const advice = state.advice!;
        const preset = advice[tab];
        const intensity = tab === "grade" ? advice.grade_intensity : advice.voice_intensity;
        setSelection({ ...selection, [tab]: preset });
        setDraft({ ...draft, [tab]: { ...presets?.[preset][1], intensity } });
      }}>Load Director suggestion</button>}
    </div>}
    {state?.advice && <p className="muted small">Director advice from transcript and available measurements. {state.advice.rationale}</p>}
    {state && <div className="motion-settings finish-settings">
      {(tab === "grade" ? gradeControls : voiceControls).map(([key, label, min, max, step]) => <label key={key}>{label}
        <input type="number" min={min} max={max} step={step} value={Number(draft[tab][key] ?? 0)}
          onChange={(event) => setDraft({ ...draft, [tab]: { ...draft[tab], [key]: Number(event.target.value) } })}/></label>)}
      {tab === "voice" && <label className="finish-toggle"><input type="checkbox" checked={Boolean(draft.voice.bypass)}
        onChange={(event) => setDraft({ ...draft, voice: { ...draft.voice, bypass: event.target.checked } })}/> Bypass enhancement</label>}
    </div>}
    <div className="motion-actions finish-options">
      <button className="secondary" disabled={!state || !duration || busy || running(job)} onClick={() => void perform(async () => {
        const result = await request<{job?: Job; analysis?: Analysis}>(root + "/analysis",
          post({ start: Math.min(position, Math.max(0, duration - 10)), grade: draft.grade, voice: draft.voice }));
        if (result.job) setJob(result.job);
        if (result.analysis) { setAnalysis(result.analysis); setNotice("Cached rendered samples ready."); }
      })}>Generate A/B samples</button>
      {running(job) && <><progress aria-label="Finishing sample progress" max={1} value={job?.progress ?? undefined}/>
        <button className="secondary" disabled={busy} onClick={() => void perform(async () => {
          setJob(await request<Job>(`${root}/analysis/${job?.id}/cancel`, post()));
        })}>Cancel</button></>}
    </div>
    {analysis && <>
      {!matchesDraft && <p className="muted small">This sample uses a previous configuration. Generate samples to hear or see current adjustments.</p>}
      <p className="muted small">Source sample {analysis.start.toFixed(2)}–{(analysis.start+analysis.duration).toFixed(2)} s. Full reels also include edited timing, framing and captions.</p>
      {tab === "grade" ? <>
        <div className="motion-previews finish-previews">
          <div><h3>Original reference</h3><video aria-label="Original picture reference" controls muted playsInline src={sampleUrl("reference.mp4")}/></div>
          <div><h3>Graded sample</h3><video aria-label="Graded picture sample" controls muted playsInline src={sampleUrl("graded.mp4")}/></div>
        </div>
        <Bars values={analysis.histogram} label="Sampled luma histogram from shadows to highlights"/>
        <p className="muted small">Sampled luma near black {display(analysis.dark_fraction == null ? null : analysis.dark_fraction*100)}%
          · near white {display(analysis.bright_fraction == null ? null : analysis.bright_fraction*100)}%</p>
        <p className="muted small">{analysis.video.interpretation} · {analysis.video.color_primaries ?? "Primaries unknown"} · {analysis.video.pix_fmt ?? "Pixel format unknown"}</p>
      </> : analysis.audio ? <>
        {canMatch && <label className="finish-toggle"><input type="checkbox" checked={matched}
          onChange={(event) => setMatched(event.target.checked)}/> Loudness-matched playback</label>}
        <div className="finish-audio">
          {(["original", "enhanced"] as const).map((name) => <div key={name}>
            <h3>{name === "original" ? "Original audio" : "Enhanced audio"}</h3>
            <audio aria-label={`${name} audio sample`} controls preload="metadata" src={sampleUrl(`${name}${matched && canMatch ? "-matched" : ""}.wav`)}/>
            <p className="muted small">{display(analysis.audio?.[name].input_i)} LUFS · {display(analysis.audio?.[name].input_tp)} dBTP</p>
          </div>)}
        </div>
        <Bars values={analysis.audio.waveform} label="Original sample waveform read as peak per window"/>
        <p className="muted small">Original RMS {display(analysis.audio.rms_dbfs)} dBFS · quiet-window estimate {display(analysis.audio.quiet_window_rms_estimate)} dBFS</p>
      </> : <p className="muted small">This source has no audio stream.</p>}
    </>}
    {state?.plan_id ? <div className="motion-actions motion-save">
      <label>Revision history<select aria-label="Finishing revision history" value={historyId} onChange={(event) => setHistoryId(event.target.value)}>
        <option value="">Choose a revision</option>
        {state.history[tab].map((item) => <option key={item.id} value={item.id}>{item.preset} · {new Date(item.created_at).toLocaleString()}</option>)}</select></label>
      <button className="secondary" disabled={!historyId} onClick={() => {
        const item = state.history[tab].find(({ id }) => id === historyId);
        if (item) { setDraft({ ...draft, [tab]: item.settings }); setSelection({ ...selection, [tab]: item.preset }); setNotice("Revision loaded into the draft."); }
      }}>Restore into draft</button>
      <button className="primary" disabled={busy} onClick={() => void perform(async () => {
        const item = await request<Revision>(`${root}/${tab}`, post({ plan_id: state.plan_id, preset: selection[tab],
          [tab]: draft[tab], analysis_id: analysis?.id }));
        setState({ ...state, current: { ...state.current, [tab]: item },
          history: { ...state.history, [tab]: [item, ...state.history[tab]] } });
        setNotice("Revision saved. Render and review a new preview.");
      })}>Save {tab === "grade" ? "colour" : "audio"} revision</button>
    </div> : <p className="muted small">Select a director plan to save finishing revisions.</p>}
  </section>;
}
