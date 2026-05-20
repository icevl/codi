import { useCallback, useEffect, useRef, useState } from "react";
import { GitCommit, X } from "lucide-react";
import { api, WsEvent } from "../api";

const ICON = 16;
// Safety-net poll for changes the WS stream can't see (e.g. user editing
// files outside the agent). Refetches are normally driven by chat events.
const SAFETY_POLL_MS = 60000;
// Coalesce bursts of agent events (a turn often emits many tool_results).
const COALESCE_MS = 800;

interface Props {
  windowId: string;
  open: boolean;
  onClose: () => void;
  subscribeWs?: (listener: (e: WsEvent) => void) => () => void;
}

interface DiffState {
  is_repo: boolean;
  diff: string;
  additions: number;
  deletions: number;
  file_count: number;
  untracked: string[];
}

const EMPTY: DiffState = {
  is_repo: false,
  diff: "",
  additions: 0,
  deletions: 0,
  file_count: 0,
  untracked: [],
};

export function DiffPanel({ windowId, open, onClose, subscribeWs }: Props) {
  const [state, setState] = useState<DiffState>(EMPTY);
  const [error, setError] = useState<string | null>(null);
  // Ref instead of state so updates inside the polling closure don't
  // trigger the effect to restart the interval.
  const etagRef = useRef<string | null>(null);

  const fetchDiff = useCallback(async () => {
    try {
      const r = await api.getDiff(windowId, etagRef.current);
      if (r.etag) etagRef.current = r.etag;
      if (r.status === 200) {
        setState(r.data);
      }
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    }
  }, [windowId]);

  // Initial fetch when panel opens, plus a slow safety-net poll that
  // catches changes outside the agent (manual file edits, git ops).
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    const tick = async () => {
      if (cancelled) return;
      await fetchDiff();
    };
    tick();
    const t = setInterval(tick, SAFETY_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [open, fetchDiff]);

  // Event-driven refresh: agent activity in the current window is the
  // only reliable signal that the working tree might have changed.
  // Coalesce bursts so a tool-heavy turn fires one request, not ten.
  useEffect(() => {
    if (!open || !subscribeWs) return;
    let pending: number | null = null;
    const schedule = () => {
      if (pending !== null) return;
      pending = window.setTimeout(() => {
        pending = null;
        fetchDiff();
      }, COALESCE_MS);
    };
    const unsub = subscribeWs((event) => {
      if (!("window_id" in event) || event.window_id !== windowId) return;
      if (event.type === "message" || event.type === "completion") {
        schedule();
      }
    });
    return () => {
      unsub();
      if (pending !== null) window.clearTimeout(pending);
    };
  }, [open, subscribeWs, windowId, fetchDiff]);

  // Reset cached state and ETag when switching to a different window so we
  // don't briefly flash the previous repo's diff (or send a stale ETag the
  // server can't match).
  useEffect(() => {
    setState(EMPTY);
    etagRef.current = null;
  }, [windowId]);

  return (
    <aside className={`diff-panel${open ? " open" : ""}`} aria-hidden={!open}>
      <header className="diff-panel-header">
        <div className="diff-panel-title">
          <GitCommit size={ICON} />
          <span>Uncommitted</span>
        </div>
        <div className="diff-panel-stats">
          {state.is_repo ? (
            <>
              <span className="diff-stat-files">{state.file_count} files</span>
              <span className="diff-stat-add">+{state.additions}</span>
              <span className="diff-stat-del">-{state.deletions}</span>
            </>
          ) : (
            <span className="diff-stat-files">not a git repo</span>
          )}
        </div>
        <button
          type="button"
          className="icon-button"
          onClick={onClose}
          title="Close"
          aria-label="Close diff panel"
        >
          <X size={ICON} />
        </button>
      </header>
      <div className="diff-panel-body">
        {error && <div className="diff-panel-empty">{error}</div>}
        {!error && state.is_repo && state.diff === "" && state.untracked.length === 0 && (
          <div className="diff-panel-empty">Working tree clean.</div>
        )}
        {state.diff && <DiffText text={state.diff} />}
        {state.untracked.length > 0 && (
          <div className="diff-untracked">
            <div className="diff-untracked-title">Untracked</div>
            <ul>
              {state.untracked.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </aside>
  );
}

function DiffText({ text }: { text: string }) {
  // Split once on render. The diff can be large; for v1 we render it as a
  // single <pre> with line-level spans so +/- get colored without parsing
  // hunks. Files are separated by `diff --git` headers which we style as
  // sticky-ish bold lines.
  const lines = text.split("\n");
  return (
    <pre className="diff-pre">
      {lines.map((line, i) => {
        let cls = "diff-line";
        if (line.startsWith("diff --git ")) cls += " diff-line-file";
        else if (line.startsWith("@@")) cls += " diff-line-hunk";
        else if (line.startsWith("+++") || line.startsWith("---"))
          cls += " diff-line-meta";
        else if (line.startsWith("+")) cls += " diff-line-add";
        else if (line.startsWith("-")) cls += " diff-line-del";
        else if (line.startsWith("index ") || line.startsWith("new file ") || line.startsWith("deleted file "))
          cls += " diff-line-meta";
        return (
          <span key={i} className={cls}>
            {line}
            {"\n"}
          </span>
        );
      })}
    </pre>
  );
}
