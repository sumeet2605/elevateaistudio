"use client";

import { ArrowUpRight, Check, ChevronRight, Clapperboard, CloudUpload, Film, LockKeyhole, Plus, Video } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";

import { api, request } from "./api";
import Transcription from "./transcription";
const number = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });

function displaySize(bytes: number) {
  return bytes < 1048576
    ? `${number.format(bytes / 1024)} KiB`
    : `${number.format(bytes / 1048576)} MiB`;
}

type Media = {
  filename: string;
  width: number;
  height: number;
  duration_seconds: number;
  video_codec: string;
  audio_codec: string | null;
  size_bytes: number;
};
type Project = { id: string; title: string; created_at: string; media: Media | null };
type Health = { tools: { ffprobe: boolean; ffmpeg: boolean }; max_upload_bytes: number };

export default function Studio() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [health, setHealth] = useState<Health | null>(null);
  const [ready, setReady] = useState(false);
  const [pending, setPending] = useState<"create" | "upload" | null>(null);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState("");
  const [previewError, setPreviewError] = useState(false);
  const [tab, setTab] = useState<"source" | "transcription">("source");
  const [videoReady, setVideoReady] = useState(false);
  const video = useRef<HTMLVideoElement>(null);
  const active = projects.find(({ id }) => id === activeId);
  const media = active?.media;
  const limit = health ? number.format(health.max_upload_bytes / 1048576) : null;
  const mediaEndpoint = active ? `/projects/${active.id}/source` : "";

  async function load() {
    setError("");
    try {
      const [items, status] = await Promise.all([request<Project[]>("/projects"), request<Health>("/health")]);
      setProjects(items);
      setHealth(status);
      const savedId = sessionStorage.getItem("elevate-project");
      setActiveId(items.find(({ id }) => id === savedId)?.id ?? items[0]?.id ?? null);
      if (sessionStorage.getItem("elevate-tab") === "transcription") setTab("transcription");
      setReady(true);
    } catch (problem) {
      setError((problem as Error).message);
    }
  }

  useEffect(() => { void load(); }, []);
  useEffect(() => {
    if (activeId) sessionStorage.setItem("elevate-project", activeId);
  }, [activeId]);

  function selectTab(next: "source" | "transcription") {
    setTab(next);
    sessionStorage.setItem("elevate-tab", next);
  }

  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!title.trim() || pending) return;
    setPending("create");
    setError("");
    try {
      const project = await request<Project>("/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: title.trim() }),
      });
      setProjects((items) => [project, ...items]);
      setActiveId(project.id);
      setVideoReady(false);
      selectTab("source");
      setTitle("");
      setPreviewError(false);
    } catch (problem) {
      setError((problem as Error).message);
    } finally {
      setPending(null);
    }
  }

  async function upload(file?: File) {
    if (!file || !active || media || pending || !health?.tools.ffprobe) return;
    setError("");
    if (!/\.(mp4|mov)$/i.test(file.name)) {
      setError("Choose an MP4 or MOV video.");
      return;
    }
    if (health && file.size > health.max_upload_bytes) {
      setError("Video exceeds the upload limit.");
      return;
    }
    setPending("upload");
    const body = new FormData();
    body.append("file", file);
    try {
      const project = await request<Project>(mediaEndpoint, { method: "POST", body });
      setProjects((items) => items.map((item) => item.id === project.id ? project : item));
      setPreviewError(false);
    } catch (problem) {
      setError((problem as Error).message);
    } finally {
      setPending(null);
    }
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="identity"><span className="mark"><ArrowUpRight size={21} /></span><div><strong>Elevate</strong><span>AI STUDIO</span></div></div>
        <div className="nav">
          <div className="nav-label">Workspace</div>
          <div className="nav-item"><Clapperboard size={17}/><span>Projects</span><ChevronRight size={15}/></div>
          <div className="nav-label project-label">Your projects <span>{projects.length}</span></div>
          {projects.length ? projects.map((project) => (
            <button type="button" key={project.id} className={`project ${activeId === project.id ? "selected" : ""}`} disabled={!!pending}
              aria-current={activeId === project.id ? "page" : undefined}
              onClick={() => {
                if (activeId === project.id) return;
                setActiveId(project.id); setError(""); setPreviewError(false); setVideoReady(false);
              }}>
              <Film size={15}/><span>{project.title}</span>{project.media && <Check size={14}/>}
            </button>
          )) : <p className="small muted empty-nav">No projects yet</p>}
        </div>
        <div className="brand"><span className="brand-avatar">E</span><div><strong>Elevate with Sumeet</strong><div className="muted small">Brand workspace</div></div></div>
      </aside>
      <div className="main">
        <header className="topbar"><span className="muted">Workspace</span><ChevronRight size={13}/><span>Projects</span>
          <span className="status"><i className={health?.tools.ffprobe ? "online" : ""}/>{health ? health.tools.ffprobe ? "Local studio" : "FFprobe missing" : "Connecting to studio"}</span>
        </header>
        <main className="content">
          <h1>Projects</h1>
          <p className="intro">Create a project and bring your original footage into the studio.</p>
          {error && <div role="alert" className="alert">{error}{!ready && <button onClick={() => void load()}>Retry connection</button>}</div>}
          {health && !health.tools.ffprobe && <p className="alert">Install FFprobe and restart the backend to inspect videos.</p>}
          <section className="panel create">
            <div><h2>New project</h2><p className="muted small">Give your video a working title.</p></div>
            <form className="form" onSubmit={create}>
              <label className="sr-only" htmlFor="title">Project title</label>
              <input id="title" placeholder="e.g. Make space for growth" value={title} onChange={(event) => setTitle(event.target.value)} maxLength={100} required />
              <button className="primary" disabled={!ready || !!pending || !title.trim()}><Plus size={17}/> Create project</button>
            </form>
          </section>
          {media && <div className="workspace-tabs" role="tablist" aria-label="Project workspace"
            onKeyDown={(event) => {
              if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
              event.preventDefault();
              const next = tab === "source" ? "transcription" : "source";
              selectTab(next);
              document.getElementById(`${next}-tab`)?.focus();
            }}>
            <button id="source-tab" role="tab" aria-selected={tab === "source"} aria-controls="source-panel"
              tabIndex={tab === "source" ? 0 : -1} onClick={() => selectTab("source")}>Original</button>
            <button id="transcription-tab" role="tab" aria-selected={tab === "transcription"} aria-controls="transcription-panel"
              tabIndex={tab === "transcription" ? 0 : -1} onClick={() => selectTab("transcription")}>Transcription</button>
          </div>}
          <div className={media && tab === "transcription" ? "review-grid" : undefined}>
          <section className="panel source" id="source-panel"
            role={media && tab === "source" ? "tabpanel" : undefined}
            aria-labelledby={media && tab === "source" ? "source-tab" : undefined}>
            <div className="section-head"><div className="icon"><Video size={19}/></div><div>
              <h2>Original footage</h2><p className="muted small">{active ? active.title : "Create a project to start uploading."}</p>
            </div>{media && <span className="saved"><Check size={14}/> Source saved</span>}</div>
            {media ? <>
              <video ref={video} key={mediaEndpoint} src={api + mediaEndpoint} controls preload="metadata"
                onLoadedMetadata={() => setVideoReady(true)}
                onError={() => { setPreviewError(true); setVideoReady(false); }} />
              {previewError && <p className="muted small">Your browser could not preview this codec. <a href={api + mediaEndpoint}>Open original</a></p>}
              <div className="media-info"><strong>{media.filename}</strong><span className="muted">{media.width} × {media.height} · {number.format(media.duration_seconds)} s · {media.video_codec.toUpperCase()} · {displaySize(media.size_bytes)}</span></div>
            </> : (
              <div className={`dropzone ${dragging ? "dragging" : ""}`}
                onDragOver={(event) => { event.preventDefault(); if (active && !pending) setDragging(true); }}
                onDragLeave={() => setDragging(false)}
                onDrop={(event) => { event.preventDefault(); setDragging(false); void upload(event.dataTransfer.files[0]); }}>
                <span className="upload-icon"><CloudUpload size={25}/></span>
                <strong>{pending === "upload" ? "Saving and inspecting footage…" : "Drop your video here"}</strong>
                <span className="muted small">MP4 or MOV{limit && ` · Up to ${limit} MiB`}</span>
                <label className={`browse ${!active || pending || !health?.tools.ffprobe ? "disabled" : ""}`}>
                  Browse files
                  <input type="file" className="sr-only" aria-label="Browse files" accept=".mp4,.mov"
                    disabled={!active || !!pending || !health?.tools.ffprobe}
                    onChange={(event) => { void upload(event.target.files?.[0]); event.target.value = ""; }} />
                </label>
              </div>
            )}
            <div className="note muted small"><LockKeyhole size={14}/> Original footage stays unchanged.</div>
          </section>
          {media && active && tab === "transcription" && <Transcription key={active.id} projectId={active.id}
            hasAudio={!!media.audio_codec} videoReady={videoReady}
            seek={(seconds) => { if (video.current) video.current.currentTime = seconds; }} />}
          </div>
        </main>
      </div>
    </div>
  );
}
