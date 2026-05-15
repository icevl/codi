import { useEffect, useMemo, useRef, useState } from "react";
import {
  Bot,
  Brain,
  Loader2,
  LogOut,
  MoreVertical,
  Pencil,
  Pin,
  PinOff,
  Plus,
  Trash2,
  X,
} from "lucide-react";
import { SessionSummary } from "../api";

const ICON = 16;
const META_ICON = 12;

export function RuntimeIcon({
  runtime,
  size = META_ICON,
}: {
  runtime: string;
  size?: number;
}) {
  return (
    <Brain
      size={size}
      className={`runtime-icon runtime-icon-${runtime}`}
      aria-label={runtime}
    />
  );
}

interface Props {
  sessions: SessionSummary[];
  activeId: string | null;
  busyIds: Set<string>;
  doneIds: Set<string>;
  onSelect: (id: string) => void;
  onNew: () => void;
  onLogout: () => void;
  onClose: () => void;
  onRename: (session: SessionSummary) => void;
  onPin: (session: SessionSummary, pinned: boolean) => void;
  onDelete: (session: SessionSummary) => void;
}

function formatRelative(ts: number | null): string {
  if (!ts) return "";
  const sec = Math.floor(Date.now() / 1000 - ts);
  if (sec < 5) return "just now";
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}

export function Sidebar({
  sessions,
  activeId,
  busyIds,
  doneIds,
  onSelect,
  onNew,
  onLogout,
  onClose,
  onRename,
  onPin,
  onDelete,
}: Props) {
  // Pinned first; within each group, hot sessions on top with a stable
  // name-based tie-breaker. Mirrors the server-side sort so live WS bumps
  // re-sort identically.
  const ordered = useMemo(
    () =>
      [...sessions].sort((a, b) => {
        if (a.pinned !== b.pinned) return a.pinned ? -1 : 1;
        const aTs = a.last_activity ?? 0;
        const bTs = b.last_activity ?? 0;
        if (aTs !== bTs) return bTs - aTs;
        return a.name.toLowerCase().localeCompare(b.name.toLowerCase());
      }),
    [sessions],
  );

  const [menuFor, setMenuFor] = useState<string | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);

  // Close the popover on outside click / Escape / scroll inside the list.
  useEffect(() => {
    if (!menuFor) return;
    const onDocClick = (e: MouseEvent) => {
      const el = menuRef.current;
      if (el && !el.contains(e.target as Node)) setMenuFor(null);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMenuFor(null);
    };
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [menuFor]);

  return (
    <aside className="sidebar">
      <div className="sidebar-header">
        <div className="brand">
          <Bot size={16} className="brand-icon" />
          Codi
        </div>
        <div className="sidebar-header-actions">
          <button
            type="button"
            className="sidebar-close icon-button"
            onClick={onClose}
            title="Close menu"
            aria-label="Close menu"
          >
            <X size={ICON} />
          </button>
          <button
            className="icon-button"
            onClick={onLogout}
            title="Sign out"
            aria-label="Sign out"
          >
            <LogOut size={ICON} />
          </button>
        </div>
      </div>
      <div className="sidebar-actions">
        <button className="primary with-icon" onClick={onNew}>
          <Plus size={ICON} /> New
        </button>
      </div>
      <div className="session-list">
        {ordered.length === 0 ? (
          <div className="session-list-empty">No sessions yet.</div>
        ) : (
          ordered.map((s) => {
            const isOpen = menuFor === s.window_id;
            const isBusy = busyIds.has(s.window_id);
            const isDone = doneIds.has(s.window_id);
            return (
              <div
                key={s.window_id}
                className={`session-item${
                  s.window_id === activeId ? " active" : ""
                }${s.pinned ? " pinned" : ""}`}
                onClick={() => onSelect(s.window_id)}
              >
                <div className="session-row">
                  <div className="session-text">
                    <div className="session-name">
                      {s.pinned && (
                        <Pin
                          size={12}
                          className="pin-marker"
                          aria-label="Pinned"
                        />
                      )}
                      {isBusy ? (
                        <Loader2
                          size={12}
                          className="activity-spinner"
                          aria-label="Agent is working"
                        />
                      ) : isDone ? (
                        <span
                          className="activity-done"
                          title="Agent finished — click to open"
                          aria-label="Finished"
                        />
                      ) : null}
                      {s.name}
                    </div>
                    <div className="session-meta">
                      <RuntimeIcon runtime={s.runtime} />
                      <span className="session-time">
                        {formatRelative(s.last_activity)}
                      </span>
                      {s.last_activity ? " · " : ""}
                      <span>{s.cwd || "—"}</span>
                    </div>
                  </div>
                  <div
                    className={`session-menu${isOpen ? " open" : ""}`}
                    ref={isOpen ? menuRef : undefined}
                  >
                    <button
                      type="button"
                      className="session-menu-trigger"
                      title="More actions"
                      aria-label="Session actions"
                      aria-expanded={isOpen}
                      onClick={(e) => {
                        e.stopPropagation();
                        setMenuFor(isOpen ? null : s.window_id);
                      }}
                    >
                      <MoreVertical size={ICON} />
                    </button>
                    {isOpen && (
                      <div
                        className="session-menu-popover"
                        onClick={(e) => e.stopPropagation()}
                      >
                        <button
                          type="button"
                          onClick={() => {
                            setMenuFor(null);
                            onRename(s);
                          }}
                        >
                          <Pencil size={ICON} /> Rename
                        </button>
                        <button
                          type="button"
                          onClick={() => {
                            setMenuFor(null);
                            onPin(s, !s.pinned);
                          }}
                        >
                          {s.pinned ? (
                            <>
                              <PinOff size={ICON} /> Unpin
                            </>
                          ) : (
                            <>
                              <Pin size={ICON} /> Pin
                            </>
                          )}
                        </button>
                        <button
                          type="button"
                          className="danger"
                          onClick={() => {
                            setMenuFor(null);
                            onDelete(s);
                          }}
                        >
                          <Trash2 size={ICON} /> Delete
                        </button>
                      </div>
                    )}
                  </div>
                </div>
              </div>
            );
          })
        )}
      </div>
    </aside>
  );
}
