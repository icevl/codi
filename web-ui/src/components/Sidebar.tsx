import { useEffect, useMemo, useRef, useState } from "react";
import {
  Bell,
  BellOff,
  Bot,
  Brain,
  GripVertical,
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
import { TunioPlayer } from "tunio-player";
import "tunio-player/styles.css";
import { SessionSummary } from "../api";

const ICON = 16;
const OFFICE_STREAM_ID = "71824d03-660b-4722-843a-5e8fbe9ad4c2";
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
  // false → first /api/sessions response not yet in. Renders a spinner
  // in the list instead of the "No sessions yet" empty state, otherwise
  // we flash an empty list every time the page reloads.
  sessionsLoaded: boolean;
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
  onReorder: (windowIds: string[]) => void | Promise<void>;
  notificationsSupported: boolean;
  notificationsEnabled: boolean;
  notificationPermission: NotificationPermission | "unsupported";
  onToggleNotifications: () => void;
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

function sessionSortValue(session: SessionSummary): number | null {
  const order = session.sort_order;
  return typeof order === "number" && Number.isInteger(order) && order >= 0
    ? order
    : null;
}

function compareSessions(a: SessionSummary, b: SessionSummary): number {
  if (a.pinned !== b.pinned) return a.pinned ? -1 : 1;
  const aOrder = sessionSortValue(a);
  const bOrder = sessionSortValue(b);
  if (aOrder !== null && bOrder !== null && aOrder !== bOrder) {
    return aOrder - bOrder;
  }
  if (aOrder !== null && bOrder === null) return -1;
  if (aOrder === null && bOrder !== null) return 1;
  const aTs = a.last_activity ?? 0;
  const bTs = b.last_activity ?? 0;
  if (aTs !== bTs) return bTs - aTs;
  return a.name.toLowerCase().localeCompare(b.name.toLowerCase());
}

export function Sidebar({
  sessions,
  sessionsLoaded,
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
  onReorder,
  notificationsSupported,
  notificationsEnabled,
  notificationPermission,
  onToggleNotifications,
}: Props) {
  // Pinned first; manual order wins within each group, with activity/name as
  // fallback for sessions that predate persisted ordering.
  const ordered = useMemo(
    () => [...sessions].sort(compareSessions),
    [sessions],
  );
  const orderedById = useMemo(
    () => new Map(ordered.map((session) => [session.window_id, session])),
    [ordered],
  );

  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [draggingId, setDraggingId] = useState<string | null>(null);
  const [dragOverId, setDragOverId] = useState<string | null>(null);
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

  const notificationTitle = !notificationsSupported
    ? "Browser notifications are unavailable"
    : notificationsEnabled
    ? "Disable browser notifications"
    : notificationPermission === "denied"
    ? "Notifications are blocked in this browser"
    : "Enable browser notifications";

  const moveSession = (
    sourceId: string,
    targetId: string,
    placement: "before" | "after",
  ) => {
    if (sourceId === targetId) return;
    const source = orderedById.get(sourceId);
    const target = orderedById.get(targetId);
    if (!source || !target || source.pinned !== target.pinned) return;

    const next = [...ordered];
    const from = next.findIndex((session) => session.window_id === sourceId);
    if (from < 0) return;
    const [moved] = next.splice(from, 1);
    let to = next.findIndex((session) => session.window_id === targetId);
    if (to < 0) return;
    if (placement === "after") to += 1;
    next.splice(to, 0, moved);
    void onReorder(next.map((session) => session.window_id));
  };

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
          <TunioPlayer
            id={OFFICE_STREAM_ID}
            theme="dark"
            buttonOnly
            buttonOnlyClassName="codi-sidebar-play"
            buttonOnlySize={28}
          />
          <button
            className={`icon-button notification-toggle${
              notificationsEnabled ? " active" : ""
            }`}
            onClick={onToggleNotifications}
            title={notificationTitle}
            aria-label={notificationTitle}
            disabled={!notificationsSupported}
          >
            {notificationsEnabled ? <Bell size={ICON} /> : <BellOff size={ICON} />}
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
        <button className="ghost with-icon sidebar-new" onClick={onNew}>
          <Plus size={ICON} />
          <span>New session</span>
        </button>
      </div>
      <div className="session-list">
        {ordered.length === 0 ? (
          sessionsLoaded ? (
            <div className="session-list-empty">No sessions yet.</div>
          ) : (
            <div className="session-list-empty session-list-loading">
              <div className="empty-state-spinner small" />
              <span>Loading…</span>
            </div>
          )
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
                }${s.pinned ? " pinned" : ""}${
                  draggingId === s.window_id ? " dragging" : ""
                }${dragOverId === s.window_id ? " drag-over" : ""}`}
                draggable
                onDragStart={(e) => {
                  e.dataTransfer.effectAllowed = "move";
                  e.dataTransfer.setData("text/plain", s.window_id);
                  setDraggingId(s.window_id);
                  setDragOverId(null);
                }}
                onDragOver={(e) => {
                  const dragging = draggingId
                    ? orderedById.get(draggingId)
                    : null;
                  if (!dragging || dragging.pinned !== s.pinned) return;
                  e.preventDefault();
                  e.dataTransfer.dropEffect = "move";
                  setDragOverId(s.window_id);
                }}
                onDragLeave={() => {
                  setDragOverId((current) =>
                    current === s.window_id ? null : current,
                  );
                }}
                onDrop={(e) => {
                  e.preventDefault();
                  const sourceId =
                    draggingId || e.dataTransfer.getData("text/plain");
                  const rect = e.currentTarget.getBoundingClientRect();
                  const placement =
                    e.clientY > rect.top + rect.height / 2 ? "after" : "before";
                  setDraggingId(null);
                  setDragOverId(null);
                  if (sourceId) moveSession(sourceId, s.window_id, placement);
                }}
                onDragEnd={() => {
                  setDraggingId(null);
                  setDragOverId(null);
                }}
                onClick={() => onSelect(s.window_id)}
              >
                <div className="session-row">
                  <span
                    className="session-drag-handle"
                    title="Drag to reorder"
                    aria-hidden="true"
                  >
                    <GripVertical size={14} />
                  </span>
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
