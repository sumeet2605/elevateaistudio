"use client";

import { Check, Copy, Download, FileText, RotateCcw, X } from "lucide-react";
import { useEffect, useState } from "react";
import { request } from "./api";

type Job = {
  id: string;
  status: "queued" | "running" | "completed" | "cancelled" | "failed" | "interrupted";
  stage: string;
  progress: number | null;
  error: string | null;
  cancellation_requested: boolean;
};
type Range = { start: number; end: number; text: string };
type Transcript = {
  model: string;
  language: string | null;
  language_confidence: number | null;
  text: string;
  segments: (Range & { words: (Range & { probability: number | null })[] })[];
};
type State = { job: Job | null; transcript_job_id: string | null };

const stages: Record<string, string> = {
  queued: "Waiting for the local worker",
  extracting_audio: "Extracting audio",
  loading_model: "Loading speech model",
  transcribing: "Transcribing speech",
  saving_transcript: "Saving transcript",
  cancelling: "Cancellation requested",
  cancelled: "Transcription cancelled",
  completed: "Transcript ready",
  failed: "Transcription failed",
  interrupted: "Transcription interrupted",
};

function timestamp(seconds: number) {
  const milliseconds = Math.round(seconds * 1000);
  const minutes = Math.floor(milliseconds / 60000);
  const wholeSeconds = Math.floor(milliseconds / 1000) % 60;
  return `${String(minutes).padStart(2, "0")}:${String(wholeSeconds).padStart(2, "0")}.${String(milliseconds % 1000).padStart(3, "0")}`;
}

function save(text: string, format: "txt" | "json") {
  const type = format === "txt" ? "text/plain" : "application/json";
  const url = URL.createObjectURL(new Blob([text], { type: `${type};charset=utf-8` }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `transcript.${format}`;
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export default function Transcription({ projectId, hasAudio, videoReady, seek }: {
  projectId: string;
  hasAudio: boolean;
  videoReady: boolean;
  seek: (seconds: number) => void;
}) {
  const root = `/projects/${projectId}`;
  const [state, setState] = useState<State | null>(null);
  const [transcript, setTranscript] = useState<Transcript | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [actionError, setActionError] = useState("");
  const [copied, setCopied] = useState(false);
  const job = state?.job;
  const active = job?.status === "queued" || job?.status === "running";
  const retry = job?.status === "failed" || job?.status === "interrupted";
  const language = transcript?.language
    ? new Intl.DisplayNames(["en"], { type: "language" }).of(transcript.language)
    : null;

  useEffect(() => {
    let disposed = false;
    let timer: number | undefined;
    let fetchedTranscript: string | null = null;

    async function poll() {
      try {
        const next = await request<State>(root + "/transcription");
        if (disposed) return;
        setState(next);
        if (next.transcript_job_id && next.transcript_job_id !== fetchedTranscript) {
          const result = await request<Transcript>(root + "/transcript?job_id=" + next.transcript_job_id);
          if (disposed) return;
          fetchedTranscript = next.transcript_job_id;
          setTranscript(result);
          setCopied(false);
        }
        setLoadError("");
        if (next.job?.status === "queued" || next.job?.status === "running") {
          timer = window.setTimeout(poll, 650);
        }
      } catch (problem) {
        if (disposed) return;
        setLoadError((problem as Error).message);
        timer = window.setTimeout(poll, 1800);
      }
    }

    void poll();
    return () => { disposed = true; window.clearTimeout(timer); };
  }, [root, refresh]);

  async function act(suffix = "") {
    setBusy(true);
    setActionError("");
    try {
      const next = await request<Job>(root + "/transcription" + suffix, { method: "POST" });
      setState((current) => ({ job: next, transcript_job_id: current?.transcript_job_id ?? null }));
    } catch (problem) {
      setActionError((problem as Error).message);
    } finally {
      setRefresh((current) => current + 1);
      setBusy(false);
    }
  }

  async function copy() {
    if (!transcript) return;
    try {
      await navigator.clipboard.writeText(transcript.text);
      setCopied(true);
      setActionError("");
    } catch {
      setActionError("The browser couldn’t access the clipboard. Try Export TXT.");
    }
  }

  return (
    <section className="panel transcript" id="transcription-panel" role="tabpanel" aria-labelledby="transcription-tab">
      <div className="section-head">
        <div className="icon"><FileText size={19}/></div>
        <div><h2>Transcription</h2><p className="muted small">Jump to speech in the original footage.</p></div>
      </div>
      {(loadError || actionError) && <p role="alert" className="alert">{actionError || loadError}</p>}
      {!hasAudio && <p className="muted small">This video has no audio track to transcribe.</p>}
      {!state && hasAudio && <p className="muted small" role="status">Checking transcription status…</p>}
      {job && (
        <div className="job-state" aria-live="polite">
          <span>{stages[job.stage] ?? job.status}</span>
          {active && job.progress !== null && <span>{Math.floor(job.progress * 100)}%</span>}
          {active && <progress aria-label="Transcription progress" max={1} value={job.progress ?? undefined} />}
          {job.error && <p className="muted small">{job.error}</p>}
        </div>
      )}
      {state && hasAudio && (
        <div className="transcript-actions">
          {active && <button className="secondary" disabled={busy || job.cancellation_requested}
            onClick={() => void act(`/jobs/${job.id}/cancel`)}><X size={15}/> Cancel transcription</button>}
          {retry && <button className="primary" disabled={busy}
            onClick={() => void act(`/jobs/${job.id}/retry`)}><RotateCcw size={15}/> Retry</button>}
          {!active && !retry && !transcript && job?.status !== "completed" &&
            <button className="primary" disabled={busy} onClick={() => void act()}><FileText size={15}/> Generate transcript</button>}
        </div>
      )}
      {transcript && <>
        <div className="transcript-summary">
          <strong>{language ?? "No speech detected"}</strong>
          <span className="muted small">{transcript.model}{transcript.language_confidence !== null &&
            ` · ${Math.round(transcript.language_confidence * 100)}% language confidence`}</span>
        </div>
        <div className="transcript-actions">
          <button className="secondary" onClick={() => void copy()}>{copied ? <Check size={15}/> : <Copy size={15}/>} {copied ? "Copied" : "Copy transcript"}</button>
          <button className="secondary" onClick={() => save(transcript.text, "txt")}><Download size={15}/> Export TXT</button>
          <button className="secondary" onClick={() => save(JSON.stringify(transcript, null, 2), "json")}><Download size={15}/> Export JSON</button>
        </div>
        <ol className="segments" aria-label="Transcript segments">
          {transcript.segments.map((segment, index) => (
            <li key={index}>
              <div className="segment">
                <button className="timecode" disabled={!videoReady} onClick={() => seek(segment.start)}
                  aria-label={`Seek to ${timestamp(segment.start)}`}>{timestamp(segment.start)}</button>
                <p>{segment.text}</p>
              </div>
              {segment.words.length > 0 && <details className="word-details">
                <summary>Word timestamps</summary>
                <div>{segment.words.map((word, wordIndex) => (
                  <button className="word" key={wordIndex} disabled={!videoReady} onClick={() => seek(word.start)}
                    title={`${timestamp(word.start)} – ${timestamp(word.end)}`}>
                    <span>{word.text.trim()}</span><span className="muted">{timestamp(word.start)}</span>
                  </button>
                ))}</div>
              </details>}
            </li>
          ))}
        </ol>
      </>}
    </section>
  );
}
