import {
  CSSProperties,
  Dispatch,
  SetStateAction,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  Group as PanelGroup,
  Panel,
  Separator as PanelResizeHandle,
  useDefaultLayout,
} from "react-resizable-panels";
import { api, SessionSummary, WsEvent } from "./api";
import { EventStream } from "./ws";
import { Login } from "./components/Login";
import { Sidebar } from "./components/Sidebar";
import { ChatView } from "./components/ChatView";
import { DiffPanel } from "./components/DiffPanel";
import { OfficePanel } from "./components/OfficePanel";
import { TerminalPanel } from "./components/TerminalPanel";
import { NewSessionDialog } from "./components/NewSessionDialog";
import { ScreenshotModal } from "./components/ScreenshotModal";
import { ConfirmDialog } from "./components/ConfirmDialog";
import { RenameDialog } from "./components/RenameDialog";
import { Toast } from "./components/Toast";
import { UpdateBanner } from "./components/UpdateBanner";

type AuthState = "loading" | "anon" | "authed";

// Per-topic open-state for the side panels lives in localStorage so
// reopening the app restores which panels each session had visible. The
// stored shape is sparse: `{ "<windowId>": { diff?, office?, term? } }`
// with `false` values omitted, so the file stays small and adding new
// panel kinds later doesn't require migrations.
const PANEL_STATE_KEY = "codexbot-panel-state-v1";

type PanelOpenMap = Record<
  string,
  Partial<Record<"diff" | "office" | "term", boolean>>
>;

function loadPanelOpenMap(): PanelOpenMap {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(PANEL_STATE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== "object") return {};
    return parsed as PanelOpenMap;
  } catch {
    return {};
  }
}

function savePanelOpenMap(map: PanelOpenMap): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(PANEL_STATE_KEY, JSON.stringify(map));
  } catch {
    // quota / private mode — best-effort persistence.
  }
}

function setFromMap(
  map: PanelOpenMap,
  kind: "diff" | "office" | "term",
): Set<string> {
  const s = new Set<string>();
  for (const [wid, flags] of Object.entries(map)) {
    if (flags?.[kind]) s.add(wid);
  }
  return s;
}

// react-resizable-panels v4 renders `[data-panel]` as `display: flex` with
// the default `flex-direction: row`. That makes the lib's own inner
// `flex-grow:1` wrapper grow only along the main axis (horizontal), so
// the panel content (.chat-area / .diff-panel / …) collapses to its
// natural height. Forcing column direction via inline style — external
// CSS doesn't override the lib's own inline `display: flex`.
const PANEL_COLUMN_STYLE: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  overflow: "hidden",
};

// 760px matches the @media break in styles.css that turns side panels
// into fixed-overlay drawers. Above it the panels live inside a resizable
// PanelGroup; at-or-below it we render them as overlays instead.
function useIsNarrow(): boolean {
  const [narrow, setNarrow] = useState(() => {
    if (typeof window === "undefined") return false;
    return window.matchMedia("(max-width: 760px)").matches;
  });
  useEffect(() => {
    const mq = window.matchMedia("(max-width: 760px)");
    const handler = (e: MediaQueryListEvent) => setNarrow(e.matches);
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);
  return narrow;
}

// Window-id-as-URL routing: paths like `/t/<window_id>` activate that
// session on direct load and survive browser back/forward navigation.
function readWindowIdFromUrl(): string | null {
  const m = window.location.pathname.match(/^\/t\/(.+)$/);
  if (!m) return null;
  try {
    return decodeURIComponent(m[1]);
  } catch {
    return m[1];
  }
}

export function App() {
  const isNarrow = useIsNarrow();
  const [auth, setAuth] = useState<AuthState>("loading");
  const [serverEnabled, setServerEnabled] = useState(true);
  const [totpRequired, setTotpRequired] = useState(false);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  // Track whether the first /api/sessions response has landed. Until
  // then we mustn't show an "empty state" — the actual data is just
  // in-flight, and flashing "No sessions" while it loads is a UX bug.
  const [sessionsLoaded, setSessionsLoaded] = useState(false);
  const [activeId, setActiveId] = useState<string | null>(() =>
    readWindowIdFromUrl(),
  );

  // Keep the URL in sync with the active session. Pushing a new entry
  // means the browser back button navigates between previously-viewed
  // sessions (and back out to the empty state).
  useEffect(() => {
    const next = activeId ? `/t/${encodeURIComponent(activeId)}` : "/";
    if (window.location.pathname === next) return;
    window.history.pushState({ activeId }, "", next);
  }, [activeId]);

  // Sync state when the user navigates with the browser back/forward.
  useEffect(() => {
    const onPop = () => setActiveId(readWindowIdFromUrl());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  // Windows that are currently streaming (agent working). Cleared on
  // stream_end / completion events.
  const [busyIds, setBusyIds] = useState<Set<string>>(() => new Set());
  // Windows where the agent finished while the user wasn't looking. Cleared
  // when the user opens that window.
  const [doneIds, setDoneIds] = useState<Set<string>>(() => new Set());
  // Per-window watchdog timers. WS reconnects don't replay history, so if a
  // `stream_end` fires while we're offline the busy flag would stick forever.
  // Each incoming `stream` event resets a 3s timer; on expiry we treat the
  // turn as ended. The server polls every ~300ms while active so this delay
  // doesn't flash off during real streaming.
  const busyWatchdogs = useRef<Record<string, number>>({});
  // Watchdog is a pure safety net for missed events across a WS
  // reconnect. The authoritative end-of-turn signal is the `completion`
  // event from the JSONL monitor (Codex `turn_complete` / Claude
  // `stop_reason=end_turn`). 90s comfortably covers a long-running tool
  // call without dropping busy; if WS drops and we miss the completion,
  // we fall back to idle after this timeout.
  const BUSY_WATCHDOG_MS = 90000;
  // We need the current activeId inside the WS callback, but capturing it
  // through the effect's closure would force re-subscribing the stream on
  // every selection change. A ref bypasses that.
  const activeIdRef = useRef<string | null>(null);
  useEffect(() => {
    activeIdRef.current = activeId;
    if (activeId) {
      setDoneIds((prev) => {
        if (!prev.has(activeId)) return prev;
        const next = new Set(prev);
        next.delete(activeId);
        return next;
      });
    }
  }, [activeId]);
  const [creating, setCreating] = useState(false);
  const [screenshotFor, setScreenshotFor] = useState<string | null>(null);
  const [killTarget, setKillTarget] = useState<SessionSummary | null>(null);
  const [renameTarget, setRenameTarget] = useState<SessionSummary | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  // Panel open-state is per-topic so the Diff / Office / Terminal panel
  // a user has open on session A stays open when they switch to it
  // after browsing session B (where they may have had nothing open).
  // Hydrated from localStorage on first render so a reload preserves the
  // exact panel layout the user left behind on each topic.
  const [diffOpenIds, setDiffOpenIds] = useState<Set<string>>(() =>
    setFromMap(loadPanelOpenMap(), "diff"),
  );
  const [officeOpenIds, setOfficeOpenIds] = useState<Set<string>>(() =>
    setFromMap(loadPanelOpenMap(), "office"),
  );
  const [termOpenIds, setTermOpenIds] = useState<Set<string>>(() =>
    setFromMap(loadPanelOpenMap(), "term"),
  );

  // Persist whenever the open-state changes. Rebuilds the sparse map so
  // sessions with no open panels are dropped entirely.
  useEffect(() => {
    const map: PanelOpenMap = {};
    const remember = (wid: string, kind: "diff" | "office" | "term") => {
      if (!map[wid]) map[wid] = {};
      map[wid][kind] = true;
    };
    for (const wid of diffOpenIds) remember(wid, "diff");
    for (const wid of officeOpenIds) remember(wid, "office");
    for (const wid of termOpenIds) remember(wid, "term");
    savePanelOpenMap(map);
  }, [diffOpenIds, officeOpenIds, termOpenIds]);
  const [toast, setToast] = useState<{ kind: "info" | "error"; text: string } | null>(
    null,
  );

  const streamRef = useRef<EventStream | null>(null);
  const wsListeners = useRef(new Set<(e: WsEvent) => void>());

  const showToast = useCallback((text: string, kind: "info" | "error" = "info") => {
    setToast({ text, kind });
    window.setTimeout(() => setToast(null), 3200);
  }, []);

  // Bootstrap auth check.
  useEffect(() => {
    let cancelled = false;
    api
      .me()
      .then((r) => {
        if (cancelled) return;
        setServerEnabled(r.enabled);
        setTotpRequired(r.totp_required);
        setAuth(r.authenticated ? "authed" : "anon");
      })
      .catch(() => {
        if (!cancelled) setAuth("anon");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const refreshSessions = useCallback(async () => {
    try {
      const r = await api.listSessions();
      setSessions(r.sessions);
      setActiveId((prev) => {
        if (prev && r.sessions.some((s) => s.window_id === prev)) return prev;
        return r.sessions[0]?.window_id ?? null;
      });
    } catch (err) {
      if ((err as Error & { code?: number }).code === 401) {
        setAuth("anon");
        return;
      }
      showToast((err as Error).message, "error");
    } finally {
      setSessionsLoaded(true);
    }
  }, [showToast]);

  // After login: load sessions, open WS.
  useEffect(() => {
    if (auth !== "authed") return;
    refreshSessions();
    const stream = new EventStream();
    streamRef.current = stream;
    const unsub = stream.subscribe((event) => {
      if (event.type === "sessions_changed") {
        refreshSessions();
      } else {
        // Bump the session's last_activity so the sidebar bubbles it up.
        if (
          (event.type === "message" || event.type === "completion") &&
          event.window_id
        ) {
          setSessions((prev) =>
            prev.map((s) =>
              s.window_id === event.window_id
                ? { ...s, last_activity: event.ts || Date.now() / 1000 }
                : s,
            ),
          );
        }

        // Sidebar busy/done indicators.
        const wid =
          "window_id" in event && typeof event.window_id === "string"
            ? event.window_id
            : "";
        if (wid) {
          const clearWatchdog = () => {
            const t = busyWatchdogs.current[wid];
            if (t) {
              window.clearTimeout(t);
              delete busyWatchdogs.current[wid];
            }
          };
          const markIdle = (markDone: boolean) => {
            clearWatchdog();
            setBusyIds((prev) => {
              if (!prev.has(wid)) return prev;
              const next = new Set(prev);
              next.delete(wid);
              return next;
            });
            if (markDone && wid !== activeIdRef.current) {
              setDoneIds((prev) => {
                if (prev.has(wid)) return prev;
                const next = new Set(prev);
                next.add(wid);
                return next;
              });
            }
          };

          // Activity signals from the JSONL monitor: Codex stream chunks
          // and any assistant/tool message that the transcript parser
          // surfaces. All re-arm the same watchdog; only `completion`
          // (from end_turn / turn_complete) clears busy authoritatively.
          const isActivity =
            event.type === "stream" ||
            (event.type === "message" &&
              (event.role === "assistant" ||
                !!event.tool_name ||
                !!event.tool_use_id));
          if (isActivity) {
            setBusyIds((prev) => {
              if (prev.has(wid)) return prev;
              const next = new Set(prev);
              next.add(wid);
              return next;
            });
            setDoneIds((prev) => {
              if (!prev.has(wid)) return prev;
              const next = new Set(prev);
              next.delete(wid);
              return next;
            });
            clearWatchdog();
            busyWatchdogs.current[wid] = window.setTimeout(
              () => markIdle(/*markDone*/ true),
              BUSY_WATCHDOG_MS,
            );
          } else if (event.type === "completion") {
            markIdle(/*markDone*/ true);
          }
        }

        for (const l of wsListeners.current) l(event);
      }
    });
    stream.start();
    return () => {
      unsub();
      stream.stop();
      streamRef.current = null;
      for (const t of Object.values(busyWatchdogs.current)) {
        window.clearTimeout(t);
      }
      busyWatchdogs.current = {};
      setBusyIds(new Set());
    };
  }, [auth, refreshSessions]);

  const subscribeWs = useCallback((listener: (e: WsEvent) => void) => {
    wsListeners.current.add(listener);
    return () => {
      wsListeners.current.delete(listener);
    };
  }, []);

  const activeSession = useMemo(
    () => sessions.find((s) => s.window_id === activeId) ?? null,
    [sessions, activeId],
  );

  // Prune panel-open entries for sessions that no longer exist so a
  // window_id that gets reused (very rare with tmux, but possible after
  // a server restart) doesn't auto-open panels that belonged to the
  // previous tenant. Gated on `sessionsLoaded` so the initial empty
  // sessions list (before the first /api/sessions response lands)
  // doesn't wipe the per-topic state we just hydrated from localStorage.
  useEffect(() => {
    if (!sessionsLoaded) return;
    const alive = new Set(sessions.map((s) => s.window_id));
    const prune = (s: Set<string>) => {
      let changed = false;
      const next = new Set<string>();
      for (const id of s) {
        if (alive.has(id)) next.add(id);
        else changed = true;
      }
      return changed ? next : s;
    };
    setDiffOpenIds((prev) => prune(prev));
    setOfficeOpenIds((prev) => prune(prev));
    setTermOpenIds((prev) => prune(prev));
  }, [sessions, sessionsLoaded]);

  const diffOpen = !!activeId && diffOpenIds.has(activeId);
  const officeOpen = !!activeId && officeOpenIds.has(activeId);
  const termOpen = !!activeId && termOpenIds.has(activeId);

  // Panel ids currently inside the PanelGroup. The layout hook keys
  // persisted sizes by this list, so different open-combinations remember
  // their own widths.
  const visiblePanelIds = useMemo(() => {
    const ids = ["chat"];
    if (diffOpen) ids.push("diff");
    if (officeOpen) ids.push("office");
    if (termOpen) ids.push("term");
    return ids;
  }, [diffOpen, officeOpen, termOpen]);

  // Per-topic layout id so each session remembers the exact widths the
  // user dragged for its own combination of panels. Falls back to a
  // global key when no session is selected (PanelGroup isn't rendered
  // in that case, but the hook still has to be called).
  const layoutProps = useDefaultLayout({
    id: `codexbot-panels:${activeId ?? "default"}`,
    panelIds: visiblePanelIds,
    storage:
      typeof window !== "undefined" ? window.localStorage : undefined,
  });

  const togglePanel = useCallback(
    (setter: Dispatch<SetStateAction<Set<string>>>) => {
      if (!activeId) return;
      setter((prev) => {
        const next = new Set(prev);
        if (next.has(activeId)) next.delete(activeId);
        else next.add(activeId);
        return next;
      });
    },
    [activeId],
  );

  const closePanel = useCallback(
    (setter: Dispatch<SetStateAction<Set<string>>>) => {
      if (!activeId) return;
      setter((prev) => {
        if (!prev.has(activeId)) return prev;
        const next = new Set(prev);
        next.delete(activeId);
        return next;
      });
    },
    [activeId],
  );

  const handleLogout = useCallback(async () => {
    try {
      await api.logout();
    } finally {
      streamRef.current?.stop();
      setAuth("anon");
      setSessions([]);
      setActiveId(null);
      setSessionsLoaded(false);
    }
  }, []);

  const handleLoginSuccess = useCallback(() => {
    setAuth("authed");
  }, []);

  const handleCreate = useCallback(
    async (body: {
      cwd: string;
      runtime: string;
      resume_session_id?: string | null;
      name?: string | null;
    }) => {
      const created = await api.createSession(body);
      setCreating(false);
      showToast(`Session "${created.name}" created`);
      await refreshSessions();
      setActiveId(created.window_id);
    },
    [refreshSessions, showToast],
  );

  const handleKill = useCallback(
    async (windowId: string) => {
      try {
        await api.killSession(windowId);
        showToast("Session killed");
        await refreshSessions();
      } catch (err) {
        showToast((err as Error).message, "error");
      }
    },
    [refreshSessions, showToast],
  );

  const closeSidebar = useCallback(() => setSidebarOpen(false), []);
  const handleSelectSession = useCallback((id: string) => {
    setActiveId(id);
    setSidebarOpen(false);
  }, []);

  const renameSession = useCallback(
    async (windowId: string, name: string) => {
      try {
        await api.renameSession(windowId, name);
        await refreshSessions();
        showToast("Renamed");
      } catch (err) {
        showToast((err as Error).message, "error");
      }
    },
    [refreshSessions, showToast],
  );

  const handleSidebarPin = useCallback(
    async (session: SessionSummary, pinned: boolean) => {
      // Optimistic update so the item floats up immediately; refresh
      // reconciles any drift.
      setSessions((prev) =>
        prev.map((s) =>
          s.window_id === session.window_id ? { ...s, pinned } : s,
        ),
      );
      try {
        await api.setSessionPinned(session.window_id, pinned);
        showToast(pinned ? "Pinned" : "Unpinned");
      } catch (err) {
        setSessions((prev) =>
          prev.map((s) =>
            s.window_id === session.window_id
              ? { ...s, pinned: !pinned }
              : s,
          ),
        );
        showToast((err as Error).message, "error");
      }
    },
    [showToast],
  );

  if (auth === "loading") {
    return (
      <div className="login-shell">
        <div className="login-card">
          <h1>Codi</h1>
          <p className="subtitle">Loading…</p>
        </div>
      </div>
    );
  }

  if (auth === "anon") {
    return (
      <>
        <Login
          enabled={serverEnabled}
          totpRequired={totpRequired}
          onSuccess={handleLoginSuccess}
        />
        {toast && <Toast {...toast} />}
      </>
    );
  }

  // The active session block is the same content on mobile and desktop;
  // only the wrapping layout differs. Pulling it out keeps both branches
  // small and avoids duplicating ~30 lines of prop wiring.
  const chatNode = activeSession ? (
    <ChatView
      session={activeSession}
      subscribeWs={subscribeWs}
      onRequestScreenshot={() => setScreenshotFor(activeSession.window_id)}
      onRequestKill={() => setKillTarget(activeSession)}
      onOpenSidebar={() => setSidebarOpen(true)}
      onToggleDiff={() => togglePanel(setDiffOpenIds)}
      diffOpen={diffOpen}
      onToggleOffice={() => togglePanel(setOfficeOpenIds)}
      officeOpen={officeOpen}
      onToggleTerm={() => togglePanel(setTermOpenIds)}
      termOpen={termOpen}
      onRename={async (name) => {
        try {
          await api.renameSession(activeSession.window_id, name);
          await refreshSessions();
          showToast("Renamed");
        } catch (err) {
          showToast((err as Error).message, "error");
        }
      }}
      showToast={showToast}
    />
  ) : null;

  const diffNode = activeSession ? (
    <DiffPanel
      windowId={activeSession.window_id}
      open={diffOpen}
      onClose={() => closePanel(setDiffOpenIds)}
      subscribeWs={subscribeWs}
    />
  ) : null;

  const officeNode = activeSession ? (
    <OfficePanel
      windowId={activeSession.window_id}
      sessionName={activeSession.name}
      busy={busyIds.has(activeSession.window_id)}
      open={officeOpen}
      onClose={() => closePanel(setOfficeOpenIds)}
      subscribeWs={subscribeWs}
      showToast={showToast}
    />
  ) : null;

  const termNode = activeSession ? (
    <TerminalPanel
      windowId={activeSession.window_id}
      open={termOpen}
      onClose={() => closePanel(setTermOpenIds)}
    />
  ) : null;

  return (
    <div
      className={`app-shell${sidebarOpen ? " sidebar-open" : ""}${
        isNarrow && diffOpen && activeSession ? " diff-open" : ""
      }${isNarrow && officeOpen && activeSession ? " office-open" : ""}${
        isNarrow && termOpen && activeSession ? " term-open" : ""
      }`}
    >
      <Sidebar
        sessions={sessions}
        sessionsLoaded={sessionsLoaded}
        activeId={activeId}
        busyIds={busyIds}
        doneIds={doneIds}
        onSelect={handleSelectSession}
        onNew={() => {
          setCreating(true);
          setSidebarOpen(false);
        }}
        onLogout={handleLogout}
        onClose={closeSidebar}
        onRename={setRenameTarget}
        onPin={handleSidebarPin}
        onDelete={setKillTarget}
      />
      <div
        className="sidebar-backdrop"
        onClick={closeSidebar}
        aria-hidden="true"
      />
      {activeSession ? (
        isNarrow ? (
          // Mobile: panels are fixed full-screen overlays driven by CSS
          // (.app-shell.*-open). The wrapping layout is irrelevant.
          <>
            {chatNode}
            {diffNode}
            {officeNode}
            {termNode}
          </>
        ) : (
          // Desktop: chat + open side panels share a horizontal
          // PanelGroup with draggable resize handles. Sizes persist via
          // localStorage (autoSaveId). Closed panels and their preceding
          // handles are simply not rendered.
          <PanelGroup
            orientation="horizontal"
            className="panel-group"
            {...layoutProps}
          >
            <Panel
              id="chat"
              minSize={25}
              defaultSize={50}
              style={PANEL_COLUMN_STYLE}
            >
              {chatNode}
            </Panel>
            {diffOpen && (
              <>
                <PanelResizeHandle className="panel-resize-handle" />
                <Panel
                  id="diff"
                  minSize={12}
                  defaultSize={22}
                  style={PANEL_COLUMN_STYLE}
                >
                  {diffNode}
                </Panel>
              </>
            )}
            {officeOpen && (
              <>
                <PanelResizeHandle className="panel-resize-handle" />
                <Panel
                  id="office"
                  minSize={14}
                  defaultSize={26}
                  style={PANEL_COLUMN_STYLE}
                >
                  {officeNode}
                </Panel>
              </>
            )}
            {termOpen && (
              <>
                <PanelResizeHandle className="panel-resize-handle" />
                <Panel
                  id="term"
                  minSize={15}
                  defaultSize={28}
                  style={PANEL_COLUMN_STYLE}
                >
                  {termNode}
                </Panel>
              </>
            )}
          </PanelGroup>
        )
      ) : (
        <main className="chat-area">
          <div className="chat-header">
            <button
              type="button"
              className="burger"
              aria-label="Open menu"
              onClick={() => setSidebarOpen(true)}
            >
              ☰
            </button>
            <div className="chat-title">
              <div className="name">Codi</div>
            </div>
          </div>
          {sessionsLoaded ? (
            <div className="empty-state">
              <h2>No active sessions</h2>
              <p>Create a new one to get started.</p>
              <button className="primary" onClick={() => setCreating(true)}>
                + New session
              </button>
            </div>
          ) : (
            <div className="empty-state">
              <div className="empty-state-spinner" />
              <p>Loading sessions…</p>
            </div>
          )}
        </main>
      )}

      {creating && (
        <NewSessionDialog onClose={() => setCreating(false)} onCreate={handleCreate} />
      )}
      {screenshotFor && (
        <ScreenshotModal
          windowId={screenshotFor}
          onClose={() => setScreenshotFor(null)}
        />
      )}
      {renameTarget && (
        <RenameDialog
          title={`Rename "${renameTarget.name}"`}
          initialValue={renameTarget.name}
          placeholder="Session name"
          onCancel={() => setRenameTarget(null)}
          onConfirm={async (value) => {
            const target = renameTarget;
            setRenameTarget(null);
            await renameSession(target.window_id, value);
          }}
        />
      )}
      {killTarget && (
        <ConfirmDialog
          title={`Kill "${killTarget.name}"?`}
          body="This terminates the tmux window. The agent process inside will receive SIGTERM."
          confirmLabel="Kill"
          danger
          onCancel={() => setKillTarget(null)}
          onConfirm={async () => {
            const id = killTarget.window_id;
            setKillTarget(null);
            await handleKill(id);
          }}
        />
      )}
      {toast && <Toast {...toast} />}
      <UpdateBanner subscribeWs={subscribeWs} />
    </div>
  );
}
