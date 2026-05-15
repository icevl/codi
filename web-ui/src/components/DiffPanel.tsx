import { useEffect, useState } from "react";
import { GitCommit, RefreshCw, X } from "lucide-react";
import { api } from "../api";

const ICON = 16;
const POLL_MS = 3000;

interface Props {
  windowId: string;
  open: boolean;
  onClose: () => void;
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

export function DiffPanel({ windowId, open, onClose }: Props) {
  const [state, setState] = useState<DiffState>(EMPTY);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    const fetchDiff = async () => {
      setLoading(true);
      try {
        const r = await api.getDiff(windowId);
        if (cancelled) return;
        setState(r);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError((err as Error).message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    fetchDiff();
    const t = setInterval(fetchDiff, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [open, windowId]);

  // Reset cached state when switching to a different window so we don't
  // briefly flash the previous repo's diff.
  useEffect(() => {
    setState(EMPTY);
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
          onClick={() => api.getDiff(windowId).then(setState).catch(() => {})}
          title="Refresh"
          aria-label="Refresh"
        >
          <RefreshCw size={ICON} className={loading ? "spin" : ""} />
        </button>
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
