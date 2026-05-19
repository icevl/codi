import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, SessionSummary, WsEvent } from "./api";
import { EventStream } from "./ws";
import { Login } from "./components/Login";
import { Sidebar } from "./components/Sidebar";
import { ChatView } from "./components/ChatView";
import { DiffPanel } from "./components/DiffPanel";
import { OfficePanel } from "./components/OfficePanel";
import { NewSessionDialog } from "./components/NewSessionDialog";
import { ScreenshotModal } from "./components/ScreenshotModal";
import { ConfirmDialog } from "./components/ConfirmDialog";
import { RenameDialog } from "./components/RenameDialog";
import { Toast } from "./components/Toast";

type AuthState = "loading" | "anon" | "authed";

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
  const [diffOpen, setDiffOpen] = useState(false);
  const [officeOpen, setOfficeOpen] = useState(false);
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

  return (
    <div
      className={`app-shell${sidebarOpen ? " sidebar-open" : ""}${
        diffOpen && activeSession ? " diff-open" : ""
      }${officeOpen && activeSession ? " office-open" : ""}`}
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
        <>
          <ChatView
            session={activeSession}
            subscribeWs={subscribeWs}
            onRequestScreenshot={() => setScreenshotFor(activeSession.window_id)}
            onRequestKill={() => setKillTarget(activeSession)}
            onOpenSidebar={() => setSidebarOpen(true)}
            onToggleDiff={() => setDiffOpen((v) => !v)}
            diffOpen={diffOpen}
            onToggleOffice={() => setOfficeOpen((v) => !v)}
            officeOpen={officeOpen}
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
          <DiffPanel
            windowId={activeSession.window_id}
            open={diffOpen}
            onClose={() => setDiffOpen(false)}
            subscribeWs={subscribeWs}
          />
          <OfficePanel
            windowId={activeSession.window_id}
            sessionName={activeSession.name}
            busy={busyIds.has(activeSession.window_id)}
            open={officeOpen}
            onClose={() => setOfficeOpen(false)}
            subscribeWs={subscribeWs}
            showToast={showToast}
          />
        </>
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
    </div>
  );
}
