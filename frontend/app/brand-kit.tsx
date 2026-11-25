"use client";

import { Palette, Save } from "lucide-react";
import { useEffect, useState } from "react";
import { request } from "./api";

type Brand = {
  name: string; primary: string; highlight: string; background: string; typography: string;
  subtitle_placement: string; caption_style: string; max_zoom_percent: number; transitions: string;
  background_music: string; audio: string; width: number; height: number; video_codec: string;
  audio_codec: string; intro: string; outro: string;
};
type Revision = { id: string; created_at: string | null; settings: Brand };
const choices = [
  ["subtitle_placement", "Subtitle placement", ["lower-third", "center", "upper-third"]],
  ["caption_style", "Caption style", ["clean-bold", "minimal", "keyword-highlight"]],
  ["transitions", "Transitions", ["minimal", "hard-cuts"]],
  ["background_music", "Background music", ["none", "optional-low"]],
  ["audio", "Voice", ["natural", "voice-leveling"]],
] as const;

export default function BrandKit({ projectId }: { projectId: string }) {
  const root = `/projects/${projectId}/brand`;
  const [versions, setVersions] = useState<Revision[]>([]);
  const [selected, setSelected] = useState<Revision>();
  const [draft, setDraft] = useState<Brand>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let ended = false;
    request<Revision[]>(root).then((items) => {
      if (ended) return;
      setVersions(items); setSelected(items[0]); setDraft(items[0].settings);
    }).catch((problem: Error) => { if (!ended) setError(problem.message); });
    return () => { ended = true; };
  }, [root]);

  function edit<K extends keyof Brand>(key: K, value: Brand[K]) {
    setDraft((current) => current && { ...current, [key]: value });
  }
  async function save(restore = false) {
    setBusy(true); setError("");
    try {
      const version = await request<Revision>(root + (restore ? `/${selected?.id}/restore` : ""), {
        method: "POST", ...(!restore && { headers: { "Content-Type": "application/json" }, body: JSON.stringify(draft) }),
      });
      setVersions((items) => [version, ...items]); setSelected(version); setDraft(version.settings);
    } catch (problem) { setError((problem as Error).message); }
    finally { setBusy(false); }
  }

  return <section id="brand-panel" role="tabpanel" aria-labelledby="brand-tab" className="panel brand-kit">
    <div className="section-head"><div className="icon"><Palette size={19}/></div>
      <div><h2>Brand Kit</h2><p className="muted small">Set the look and sound of future edit plans.</p></div></div>
    {error && <p role="alert" className="alert">{error}</p>}
    {draft && selected && <>
      <label className="plan-history">Brand revision <select value={selected.id} disabled={busy} onChange={(event) => {
        const version = versions.find(({ id }) => id === event.target.value);
        if (version) { setSelected(version); setDraft(version.settings); }
      }}>{versions.map((item, index) => <option key={item.id} value={item.id}>
        {index === 0 ? "Current · " : ""}{item.created_at ? new Date(item.created_at).toLocaleString() : "Default v1"}
      </option>)}</select></label>
      <div className="brand-preview" style={{ background: draft.background, color: draft.primary }}>
        <span>{draft.name}</span><strong style={{ color: draft.highlight }}>{draft.outro || draft.name}</strong>
        <small>{draft.typography} · {draft.width} × {draft.height}</small>
      </div>
      <form onSubmit={(event) => { event.preventDefault(); void save(); }}>
        <div className="settings-grid">
          {([["name", "Brand name", 80], ["typography", "Typography", 80], ["intro", "Intro convention", 200],
            ["outro", "Outro convention", 120]] as const).map(([key, label, max]) =>
            <label key={key}>{label}<input value={draft[key]} required={key === "name" || key === "typography"}
              maxLength={max} onChange={(event) => edit(key, event.target.value)}/></label>)}
          {([["primary", "Primary colour"], ["highlight", "Highlight colour"], ["background", "Background colour"]] as const)
            .map(([key, label]) => <label key={key}>{label}<input type="color" value={draft[key]}
              onChange={(event) => edit(key, event.target.value)}/></label>)}
          {choices.map(([key, label, values]) => <label key={key}>{label}<select value={draft[key]}
            onChange={(event) => edit(key, event.target.value)}>{values.map((value) => <option key={value}>{value}</option>)}</select></label>)}
          {([["max_zoom_percent", "Maximum zoom (%)", 100, 125, 1], ["width", "Export width", 240, 4096, 2],
            ["height", "Export height", 240, 4096, 2]] as const).map(([key, label, low, high, step]) =>
            <label key={key}>{label}<input type="number" required min={low} max={high} step={step}
              value={Number.isNaN(draft[key]) ? "" : draft[key]} onChange={(event) => edit(key, event.target.valueAsNumber)}/></label>)}
        </div>
        <p className="muted small audio-metrics">Export codecs · H.264 video and AAC audio</p>
        <div className="transcript-actions">
          <button className="primary" disabled={busy || JSON.stringify(draft) === JSON.stringify(selected.settings)}>
            <Save size={15}/> Save new revision
          </button>
          {selected.id !== versions[0].id && <button type="button" className="secondary" disabled={busy}
            onClick={() => void save(true)}>Restore as new revision</button>}
        </div>
      </form>
    </>}
  </section>;
}
