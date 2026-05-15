import { useEffect, useState } from "react";
import {
  api,
  DirectoryListing,
  ResumeSession,
  RuntimeInfo,
} from "../api";

interface Props {
  onClose: () => void;
  onCreate: (body: {
    cwd: string;
    runtime: string;
    resume_session_id?: string | null;
    name?: string | null;
  }) => Promise<void>;
}

type Stage = "directory" | "runtime" | "resume";

export function NewSessionDialog({ onClose, onCreate }: Props) {
  const [stage, setStage] = useState<Stage>("directory");
  const [listing, setListing] = useState<DirectoryListing | null>(null);
  const [pathInput, setPathInput] = useState("~");
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [runtimes, setRuntimes] = useState<RuntimeInfo[]>([]);
  const [runtime, setRuntime] = useState<string>("codex");
  const [resumeOptions, setResumeOptions] = useState<ResumeSession[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.listRuntimes().then((r) => setRuntimes(r.runtimes)).catch(() => undefined);
  }, []);

  useEffect(() => {
    if (stage !== "directory") return;
    setError(null);
    api
      .listDirectories(pathInput)
      .then((r) => {
        setListing(r);
        setPathInput(r.path);
      })
      .catch((err: Error) => setError(err.message));
  }, [stage, pathInput]);

  function goUp() {
    if (!listing?.parent) return;
    setPathInput(listing.parent);
  }

  async function selectDirectory(path: string) {
    setSelectedPath(path);
    setBusy(true);
    try {
      const r = await api.listResumeSessions(path);
      setResumeOptions(r.sessions);
      if (r.sessions.length > 0) {
        setStage("resume");
      } else {
        setStage("runtime");
      }
    } catch {
      setResumeOptions([]);
      setStage("runtime");
    } finally {
      setBusy(false);
    }
  }

  async function submit(body: {
    cwd: string;
    runtime: string;
    resume_session_id?: string | null;
  }) {
    setBusy(true);
    setError(null);
    try {
      await onCreate(body);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div className="modal" onMouseDown={(e) => e.stopPropagation()}>
        <h2>New session</h2>
        {error && <div className="login-error" style={{ marginBottom: 10 }}>{error}</div>}

        {stage === "directory" && (
          <>
            <div className="modal-row">
              <label>Working directory</label>
              <div style={{ display: "flex", gap: 6 }}>
                <input
                  style={{ flex: 1 }}
                  value={pathInput}
                  onChange={(e) => setPathInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") setPathInput((p) => p.trim() || "~");
                  }}
                />
                <button onClick={goUp} disabled={!listing?.parent}>
                  ↑ Up
                </button>
              </div>
            </div>
            <div style={{ maxHeight: 280, overflowY: "auto" }}>
              {listing?.entries.length === 0 && (
                <div style={{ color: "var(--text-2)", padding: 8 }}>
                  No subdirectories.
                </div>
              )}
              {listing?.entries.map((d) => (
                <div
                  key={d.path}
                  className={`dir-row${selectedPath === d.path ? " selected" : ""}`}
                  onClick={() => setSelectedPath(d.path)}
                  onDoubleClick={() => setPathInput(d.path)}
                >
                  <span>📁 {d.name}</span>
                  <span className="path">{d.path}</span>
                </div>
              ))}
            </div>
            <div className="modal-actions">
              <button onClick={onClose}>Cancel</button>
              <button
                className="primary"
                disabled={!listing?.path || busy}
                onClick={() => selectDirectory(selectedPath ?? listing?.path ?? "")}
              >
                {busy ? "…" : "Use this directory →"}
              </button>
            </div>
          </>
        )}

        {stage === "resume" && (
          <>
            <div className="modal-row">
              <label>Existing sessions in {selectedPath}</label>
            </div>
            <div style={{ maxHeight: 320, overflowY: "auto" }}>
              {resumeOptions.map((s) => (
                <div
                  key={s.session_id}
                  className="resume-row"
                  onClick={() =>
                    submit({
                      cwd: selectedPath ?? "",
                      runtime,
                      resume_session_id: s.session_id,
                    })
                  }
                >
                  <div className="summary">{s.summary || "Untitled"}</div>
                  <div className="count">
                    {s.message_count} msgs · {s.session_id.slice(0, 8)}…
                  </div>
                </div>
              ))}
            </div>
            <div className="modal-actions">
              <button onClick={() => setStage("directory")}>← Back</button>
              <button onClick={() => setStage("runtime")}>Skip — new session</button>
            </div>
          </>
        )}

        {stage === "runtime" && (
          <>
            <div className="modal-row">
              <label>Working directory</label>
              <div className="path" style={{ color: "var(--text-1)" }}>
                {selectedPath}
              </div>
            </div>
            <div className="modal-row">
              <label>Agent runtime</label>
              <div className="runtime-buttons">
                {runtimes.map((r) => (
                  <button
                    key={r.name}
                    className={runtime === r.name ? "selected" : ""}
                    onClick={() => setRuntime(r.name)}
                  >
                    {r.emoji} {r.display_name}
                  </button>
                ))}
              </div>
            </div>
            <div className="modal-actions">
              <button onClick={() => setStage("directory")}>← Back</button>
              <button
                className="primary"
                disabled={busy || !selectedPath}
                onClick={() =>
                  submit({
                    cwd: selectedPath ?? "",
                    runtime,
                    resume_session_id: null,
                  })
                }
              >
                {busy ? "Creating…" : "Create"}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
