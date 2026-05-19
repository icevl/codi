import {
  ClipboardEvent,
  DragEvent,
  KeyboardEvent,
  memo,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import {
  Bot,
  Camera,
  ChevronDown,
  Eraser,
  GitCommit,
  Keyboard,
  Menu,
  MoreVertical,
  Paperclip,
  Pencil,
  Trash2,
  User,
  Users,
  X,
} from "lucide-react";
import { api, SessionMessage, SessionSummary, WsEvent } from "../api";
import { SkillsModal } from "./SkillsModal";
import { Markdown } from "./Markdown";
import { RuntimeIcon } from "./Sidebar";

const ICON = 16;

interface Props {
  session: SessionSummary;
  subscribeWs: (l: (e: WsEvent) => void) => () => void;
  onRequestScreenshot: () => void;
  onRequestKill: () => void;
  onOpenSidebar: () => void;
  onToggleDiff: () => void;
  diffOpen: boolean;
  onToggleOffice: () => void;
  officeOpen: boolean;
  onRename: (name: string) => Promise<void>;
  showToast: (text: string, kind?: "info" | "error") => void;
}

// Commands the Telegram bot intercepts (does NOT forward to the agent pane).
// Mirroring this set in the web composer keeps `/screenshot` etc. consistent.
const BOT_COMMANDS: Record<string, string> = {
  "/screenshot": "Open terminal screenshot",
  "/esc": "Send Escape to the pane",
  "/kill": "Kill the session",
  "/unbind": "Unbind topic (Telegram-only)",
  "/history": "Reload transcript",
  "/diag": "Diagnostics (Telegram-only)",
  "/skillhelp": "List available skills",
  "/start": "Welcome message",
};

function parseSlashCommand(text: string): string | null {
  const trimmed = text.trim();
  if (!trimmed.startsWith("/")) return null;
  return trimmed.split(/\s+/)[0].toLowerCase();
}

// Client-side _clientId keeps memo + anchor lookup stable across prepends.
type ChatMessage = SessionMessage & { pending?: boolean; _clientId: string };

let _clientIdCounter = 0;
function attachKey(m: SessionMessage & { pending?: boolean }): ChatMessage {
  return { ...m, _clientId: `m${++_clientIdCounter}` };
}


// Compact time label for a message bubble. Same day → HH:MM; same year
// but a different day → MMM dd HH:MM; otherwise drops the year in too.
// Falls back to the raw string if it isn't parseable.
function formatMessageTime(iso: string | undefined): {
  short: string;
  full: string;
} | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  const now = new Date();
  const hh = d.getHours().toString().padStart(2, "0");
  const mm = d.getMinutes().toString().padStart(2, "0");
  const time = `${hh}:${mm}`;
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
  let short: string;
  if (sameDay) {
    short = time;
  } else if (d.getFullYear() === now.getFullYear()) {
    short = `${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })} ${time}`;
  } else {
    short = `${d.toLocaleDateString()} ${time}`;
  }
  return { short, full: d.toLocaleString() };
}

// Memoized per-message bubble. Virtuoso re-renders the visible window on
// every scroll tick; without `memo` every bubble would re-parse its
// Markdown each time and large chats would lock the main thread.
const MessageBubble = memo(function MessageBubble({
  m,
}: {
  m: ChatMessage;
}) {
  const t = formatMessageTime(m.timestamp);
  const isUser = m.role === "user" && m.content_type !== "tool_result";
  return (
    <div className={`message-line ${isUser ? "user" : "assistant"}`}>
      <div className="message-avatar" aria-hidden="true">
        {isUser ? <User size={16} /> : <Bot size={16} />}
      </div>
      <div
        className={`bubble ${m.role} ${m.content_type}${m.pending ? " pending" : ""}`.trim()}
      >
        <div className="meta">
          <span>
            {m.role}
            {m.content_type && m.content_type !== "text"
              ? ` · ${m.content_type}`
              : ""}
            {m.pending ? " · sending…" : ""}
          </span>
          {t && (
            <time className="bubble-time" dateTime={m.timestamp} title={t.full}>
              {t.short}
            </time>
          )}
        </div>
        <Markdown text={m.text} />
      </div>
    </div>
  );
});

const StreamingBubble = memo(function StreamingBubble({
  text,
  status,
}: {
  text: string;
  status: string;
}) {
  return (
    <div className="message-line assistant">
      <div className="message-avatar" aria-hidden="true">
        <Bot size={16} />
      </div>
      <div className="bubble assistant streaming">
        <div className="meta">assistant · {status}</div>
        <pre className="stream-body">{text || "…"}</pre>
      </div>
    </div>
  );
});

const KEY_BUTTONS: Array<{ label: string; key: string }> = [
  { label: "Esc", key: "Escape" },
  { label: "↑", key: "Up" },
  { label: "↓", key: "Down" },
  { label: "←", key: "Left" },
  { label: "→", key: "Right" },
  { label: "Tab", key: "Tab" },
  { label: "Enter", key: "Enter" },
  { label: "Space", key: "Space" },
  { label: "PgUp", key: "PageUp" },
  { label: "PgDn", key: "PageDown" },
  { label: "⌫", key: "BSpace" },
  { label: "Ctrl+C", key: "C-c" },
];

// Codex/Claude-side slash commands — forwarded verbatim to the pane.
const AGENT_QUICK_COMMANDS = ["/clear", "/new", "/compact", "/status", "/help"];
// Bot-side commands — handled locally by the web UI.
const BOT_QUICK_COMMANDS = ["/screenshot", "/skillhelp", "/esc"];

export function ChatView({
  session,
  subscribeWs,
  onRequestScreenshot,
  onRequestKill,
  onOpenSidebar,
  onToggleDiff,
  diffOpen,
  onToggleOffice,
  officeOpen,
  onRename,
  showToast,
}: Props) {
  const [showSkills, setShowSkills] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const messagesRef = useRef<ChatMessage[]>([]);
  messagesRef.current = messages;
  // false while /api/sessions/<wid>/messages is in flight for the
  // currently selected session. Without this we'd render "No messages
  // yet" briefly between selecting a session and the history landing.
  const [historyLoaded, setHistoryLoaded] = useState(false);
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [editingName, setEditingName] = useState(false);
  const [nameDraft, setNameDraft] = useState(session.name);
  const [streaming, setStreaming] = useState<{ text: string; status: string } | null>(
    null,
  );
  const [attachments, setAttachments] = useState<
    Array<{ id: string; file: File; previewUrl: string }>
  >([]);
  const [dragOver, setDragOver] = useState(false);
  const [keysMenuOpen, setKeysMenuOpen] = useState(false);
  const [chatMenuOpen, setChatMenuOpen] = useState(false);
  const [gitBranch, setGitBranch] = useState<string | null>(null);
  const [gitIsRepo, setGitIsRepo] = useState(false);
  const [branchMenuOpen, setBranchMenuOpen] = useState(false);
  const [branchList, setBranchList] = useState<string[] | null>(null);
  const [branchLoadError, setBranchLoadError] = useState<string | null>(null);
  const [switchingBranch, setSwitchingBranch] = useState<string | null>(null);
  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const messagesListRef = useRef<HTMLDivElement | null>(null);
  const topSentinelRef = useRef<HTMLDivElement | null>(null);
  const [atBottom, setAtBottom] = useState(true);
  // Sync mirror of `atBottom` — useState lags one tick for layout-effect reads.
  const stickToBottomRef = useRef(true);
  const [hasMore, setHasMore] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  // Captured by loadOlder before prepend, consumed by the layout effect to
  // restore the same on-screen bubble position.
  const pendingAnchorRef = useRef<{ clientId: string; top: number } | null>(
    null,
  );

  const scrollToBottom = useCallback(() => {
    const el = scrollerRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, []);

  const scrollToLatest = useCallback(() => {
    const el = scrollerRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, []);

  // First bubble whose top is at-or-below the scroller's top edge.
  const captureAnchor = useCallback(() => {
    const el = scrollerRef.current;
    if (!el) return;
    const rootTop = el.getBoundingClientRect().top;
    const rows = el.querySelectorAll<HTMLElement>("[data-msg-key]");
    for (const row of Array.from(rows)) {
      const rect = row.getBoundingClientRect();
      if (rect.top >= rootTop) {
        pendingAnchorRef.current = {
          clientId: row.dataset.msgKey!,
          top: rect.top,
        };
        return;
      }
    }
  }, []);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const keysMenuRef = useRef<HTMLDivElement | null>(null);
  const chatMenuRef = useRef<HTMLDivElement | null>(null);
  const branchMenuRef = useRef<HTMLDivElement | null>(null);
  const sessionIdRef = useRef<string | null>(session.session_id);
  const windowIdRef = useRef(session.window_id);
  // Per-session draft cache. Switching sessions stashes the current
  // composer text under the previous window_id and restores any draft for
  // the new one, so each topic keeps its own pending message.
  const draftsRef = useRef<Record<string, string>>({});
  const textRef = useRef(text);
  useEffect(() => {
    textRef.current = text;
  }, [text]);

  useEffect(() => {
    const previousWid = windowIdRef.current;
    const isRealSwitch = previousWid && previousWid !== session.window_id;
    if (isRealSwitch) {
      draftsRef.current[previousWid] = textRef.current;
    }
    windowIdRef.current = session.window_id;
    sessionIdRef.current = session.session_id;
    const restored = draftsRef.current[session.window_id] ?? "";
    setText(restored);
    setNameDraft(session.name);
    setEditingName(false);
    setStreaming(null);
    setAttachments((prev) => {
      for (const a of prev) URL.revokeObjectURL(a.previewUrl);
      return [];
    });
    // Focus the composer after the new draft is rendered. Mobile browsers
    // ignore programmatic focus for keyboard summoning, so this only puts
    // a caret on desktop — exactly what we want. Defer with a microtask so
    // the textarea's `value` is the new draft when we move the caret.
    queueMicrotask(() => {
      const el = textareaRef.current;
      if (!el) return;
      el.focus();
      const pos = restored.length;
      el.setSelectionRange(pos, pos);
    });
  }, [session.window_id, session.session_id, session.name]);

  // Revoke any preview blobs we still hold when the chat view unmounts.
  useEffect(() => {
    return () => {
      setAttachments((prev) => {
        for (const a of prev) URL.revokeObjectURL(a.previewUrl);
        return [];
      });
    };
  }, []);


  const addFiles = useCallback((files: FileList | File[] | null) => {
    if (!files) return;
    const incoming = Array.from(files).filter((f) => f.type.startsWith("image/"));
    if (incoming.length === 0) return;
    setAttachments((prev) => [
      ...prev,
      ...incoming.map((file) => ({
        id: `${file.name}-${file.size}-${file.lastModified}-${Math.random()}`,
        file,
        previewUrl: URL.createObjectURL(file),
      })),
    ]);
  }, []);

  const removeAttachment = useCallback((id: string) => {
    setAttachments((prev) => {
      const next: typeof prev = [];
      for (const a of prev) {
        if (a.id === id) URL.revokeObjectURL(a.previewUrl);
        else next.push(a);
      }
      return next;
    });
  }, []);

  const loadHistory = useCallback(() => {
    let cancelled = false;
    setMessages([]);
    setHasMore(false);
    setHistoryLoaded(false);
    api
      .getMessages(session.window_id)
      .then((r) => {
        if (cancelled) return;
        setMessages(r.messages.map(attachKey));
        setHasMore(r.has_more);
        sessionIdRef.current = r.session_id;
      })
      .catch((err: Error) => {
        if (cancelled) return;
        showToast(err.message, "error");
      })
      .finally(() => {
        if (cancelled) return;
        setHistoryLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [session.window_id, showToast]);

  // Load history when session changes.
  useEffect(() => loadHistory(), [loadHistory]);

  // Ref-shape keeps the IO observer stable; new messages don't churn it.
  const loadingOlderRef = useRef(false);
  const loadOlderRef = useRef<() => void>(() => {});
  useEffect(() => {
    loadOlderRef.current = () => {
      if (loadingOlderRef.current || !hasMore) return;
      const oldest = messages.find((m) => !!m.timestamp);
      if (!oldest?.timestamp) return;
      captureAnchor();
      loadingOlderRef.current = true;
      setLoadingOlder(true);
      api
        .getMessages(session.window_id, { before: oldest.timestamp })
        .then((r) => {
          setMessages((prev) => [...r.messages.map(attachKey), ...prev]);
          setHasMore(r.has_more);
        })
        .catch((err: Error) => {
          pendingAnchorRef.current = null;
          showToast(err.message, "error");
        })
        .finally(() => {
          loadingOlderRef.current = false;
          setLoadingOlder(false);
        });
    };
  });
  const loadOlder = useCallback(() => loadOlderRef.current(), []);

  // Single scrollTop writer: restore anchor on prepend, snap on stick.
  useLayoutEffect(() => {
    const el = scrollerRef.current;
    if (!el) return;
    const anchor = pendingAnchorRef.current;
    if (anchor) {
      const row = el.querySelector<HTMLElement>(
        `[data-msg-key="${anchor.clientId}"]`,
      );
      if (row) {
        const newTop = row.getBoundingClientRect().top;
        const delta = newTop - anchor.top;
        if (delta !== 0) el.scrollTop += delta;
      }
      pendingAnchorRef.current = null;
      return;
    }
    if (stickToBottomRef.current) {
      el.scrollTop = el.scrollHeight;
    }
  });

  // Scroll event is the single source of "am I at the bottom?".
  const handleScroll = useCallback(() => {
    const el = scrollerRef.current;
    if (!el) return;
    const dist = el.scrollHeight - el.scrollTop - el.clientHeight;
    const next = dist < 4;
    stickToBottomRef.current = next;
    setAtBottom((prev) => (prev !== next ? next : prev));
  }, []);

  // New session always lands at the bottom regardless of prior scroll.
  useEffect(() => {
    stickToBottomRef.current = true;
    setAtBottom(true);
  }, [session.window_id]);

  // Re-pin to bottom on deferred layout (images, fonts, keyboard close).
  // Anchoring above-viewport rows is intentionally absent — any
  // scrollTop write during iOS momentum scroll cancels the momentum.
  useEffect(() => {
    const list = messagesListRef.current;
    const el = scrollerRef.current;
    if (!list || !el) return;
    const ro = new ResizeObserver(() => {
      if (stickToBottomRef.current) el.scrollTop = el.scrollHeight;
    });
    ro.observe(list);
    return () => ro.disconnect();
  }, [messages.length > 0]);

  // Top sentinel fires loadOlder as it slides into the overscan band.
  useEffect(() => {
    if (!hasMore) return;
    const sentinel = topSentinelRef.current;
    const root = scrollerRef.current;
    if (!sentinel || !root) return;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) loadOlder();
      },
      { root, rootMargin: "300px 0px 0px 0px" },
    );
    io.observe(sentinel);
    return () => io.disconnect();
  }, [hasMore, loadOlder]);

  // Catch-up after backgrounding: when the page is restored, ask the
  // server for any messages newer than the last one we have. Covers the
  // gap when iOS Safari suspended the WebSocket (`ws.ts` force-closes &
  // reconnects on visible, but events that arrived during the suspend
  // are not replayed by the server).
  useEffect(() => {
    let inflight = false;
    const catchUp = async () => {
      if (document.visibilityState !== "visible") return;
      if (inflight) return;
      const wid = windowIdRef.current;
      if (!wid) return;
      let lastTs = "";
      for (const m of messagesRef.current) {
        if (m.timestamp && m.timestamp > lastTs) lastTs = m.timestamp;
      }
      inflight = true;
      try {
        const r = await api.getMessages(wid, {
          after: lastTs || undefined,
          limit: 500,
        });
        if (windowIdRef.current !== wid) return;
        if (r.messages.length === 0) return;
        setMessages((prev) => {
          if (prev.length === 0) return r.messages.map(attachKey);
          const seen = new Set<string>();
          for (const m of prev) {
            if (m.timestamp) {
              seen.add(`${m.timestamp}|${m.role}|${m.content_type}|${m.text}`);
            }
          }
          const fresh = r.messages
            .filter(
              (m) =>
                !m.timestamp ||
                !seen.has(`${m.timestamp}|${m.role}|${m.content_type}|${m.text}`),
            )
            .map(attachKey);
          return fresh.length > 0 ? [...prev, ...fresh] : prev;
        });
      } catch {
        // Network blip — leave state alone, next reconnect will retry.
      } finally {
        inflight = false;
      }
    };
    const onVisible = () => {
      if (document.visibilityState === "visible") void catchUp();
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("pageshow", onVisible);
    window.addEventListener("focus", onVisible);
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("pageshow", onVisible);
      window.removeEventListener("focus", onVisible);
    };
  }, []);

  // Subscribe to live updates.
  useEffect(() => {
    const isCurrentSession = (
      ev: { window_id: string; session_id: string | null },
    ): boolean => {
      if (ev.window_id === windowIdRef.current) return true;
      if (sessionIdRef.current && ev.session_id === sessionIdRef.current) {
        return true;
      }
      return false;
    };

    return subscribeWs((event) => {
      if (event.type === "stream") {
        if (!isCurrentSession(event)) return;
        setStreaming({ text: event.text, status: event.status });
        return;
      }
      if (event.type === "stream_end") {
        if (!isCurrentSession(event)) return;
        setStreaming(null);
        return;
      }
      if (event.type !== "message") return;
      if (!isCurrentSession(event)) return;
      if (!event.is_complete) return;

      // Completion of any message implies streaming preview is stale.
      setStreaming(null);

      setMessages((prev) => {
        // Replace optimistic echo in place; keep _clientId so DOM node is reused.
        if (event.role === "user") {
          const idx = prev.findIndex(
            (m) => m.pending && m.role === "user" && m.text === event.text,
          );
          if (idx !== -1) {
            const next = prev.slice();
            next[idx] = {
              role: event.role,
              text: event.text,
              content_type: event.content_type,
              _clientId: prev[idx]._clientId,
            };
            return next;
          }
        }
        return [
          ...prev,
          attachKey({
            role: event.role,
            text: event.text,
            content_type: event.content_type,
          }),
        ];
      });
    });
  }, [subscribeWs]);

  // Poll git branch for the current window. The pane cwd can drift if the
  // user `cd`s inside the shell, and there's no event for that, so polling
  // is the simplest correct option. 3s is fast enough to feel live without
  // hammering git on a busy repo.
  useEffect(() => {
    let cancelled = false;
    const windowId = session.window_id;

    setGitBranch(null);
    setGitIsRepo(false);

    const fetchBranch = async () => {
      try {
        const r = await api.getGitInfo(windowId);
        if (cancelled || windowIdRef.current !== windowId) return;
        setGitIsRepo(r.is_repo);
        setGitBranch(r.branch);
      } catch {
        // Endpoint may 404 briefly while a window is being created; ignore.
      }
    };

    fetchBranch();
    const t = setInterval(fetchBranch, 3000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [session.window_id]);


  // Close the keys/commands popover on outside click or Escape.
  useEffect(() => {
    if (!keysMenuOpen) return;
    const onDocClick = (e: MouseEvent) => {
      const el = keysMenuRef.current;
      if (el && !el.contains(e.target as Node)) setKeysMenuOpen(false);
    };
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") setKeysMenuOpen(false);
    };
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [keysMenuOpen]);

  useEffect(() => {
    if (!chatMenuOpen) return;
    const onDocClick = (e: MouseEvent) => {
      const el = chatMenuRef.current;
      if (el && !el.contains(e.target as Node)) setChatMenuOpen(false);
    };
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") setChatMenuOpen(false);
    };
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [chatMenuOpen]);

  // Close branch popover on outside click or Escape, mirroring the keys menu.
  useEffect(() => {
    if (!branchMenuOpen) return;
    const onDocClick = (e: MouseEvent) => {
      const el = branchMenuRef.current;
      if (el && !el.contains(e.target as Node)) setBranchMenuOpen(false);
    };
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") setBranchMenuOpen(false);
    };
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [branchMenuOpen]);

  // Lazy-load the branch list when the popover opens. Refetch each time so
  // stale entries (deleted/renamed branches) don't linger.
  useEffect(() => {
    if (!branchMenuOpen) return;
    const windowId = session.window_id;
    let cancelled = false;
    setBranchList(null);
    setBranchLoadError(null);
    api
      .listBranches(windowId)
      .then((r) => {
        if (cancelled || windowIdRef.current !== windowId) return;
        if (!r.is_repo) {
          setBranchList([]);
          setBranchLoadError("not a git repo");
          return;
        }
        setBranchList(r.branches);
      })
      .catch((err: Error) => {
        if (cancelled) return;
        setBranchList([]);
        setBranchLoadError(err.message);
      });
    return () => {
      cancelled = true;
    };
  }, [branchMenuOpen, session.window_id]);

  const handleSwitchBranch = useCallback(
    async (branch: string) => {
      if (branch === gitBranch) {
        setBranchMenuOpen(false);
        return;
      }
      setSwitchingBranch(branch);
      try {
        await api.switchBranch(session.window_id, branch);
        setGitBranch(branch);
        setBranchMenuOpen(false);
        showToast(`Switched to ${branch}`);
        // Pull a fresh branch right away in case any post-switch hook
        // moved HEAD again (rare but harmless to re-check).
        try {
          const r = await api.getGitInfo(session.window_id);
          if (windowIdRef.current === session.window_id) {
            setGitIsRepo(r.is_repo);
            setGitBranch(r.branch);
          }
        } catch {
          /* ignore — the periodic poll will catch up */
        }
      } catch (err) {
        showToast((err as Error).message, "error");
      } finally {
        setSwitchingBranch(null);
      }
    },
    [gitBranch, session.window_id, showToast],
  );

  const handleBotCommand = useCallback(
    async (command: string): Promise<boolean> => {
      switch (command) {
        case "/screenshot":
          onRequestScreenshot();
          return true;
        case "/esc":
          try {
            await api.sendKey(session.window_id, "Escape");
            showToast("Escape sent");
          } catch (err) {
            showToast((err as Error).message, "error");
          }
          return true;
        case "/kill":
          onRequestKill();
          return true;
        case "/unbind":
          showToast("Unbind is Telegram-specific (web sessions are not topic-bound)");
          return true;
        case "/history":
          loadHistory();
          showToast("Transcript reloaded");
          return true;
        case "/diag":
          showToast("Diagnostics command is Telegram-only");
          return true;
        case "/skillhelp":
          setShowSkills(true);
          return true;
        case "/start":
          showToast("Codi — pick a session or create a new one");
          return true;
        default:
          return false;
      }
    },
    [
      session.window_id,
      onRequestScreenshot,
      onRequestKill,
      loadHistory,
      showToast,
    ],
  );

  const send = useCallback(
    async (payload: string) => {
      const caption = payload.trimEnd();
      const hasAttachments = attachments.length > 0;
      if (!caption && !hasAttachments) return;

      // Slash-commands run only when there are no attachments — otherwise the
      // user clearly meant to upload, not invoke a bot command.
      if (!hasAttachments) {
        const cmd = parseSlashCommand(caption);
        if (cmd && cmd in BOT_COMMANDS) {
          const handled = await handleBotCommand(cmd);
          if (handled) {
            setText("");
            textareaRef.current?.focus();
            return;
          }
        }
      }

      setSending(true);
      const pendingAttachments = attachments;
      // Reserve a stable optimistic text so we can find/remove the echo on
      // failure — actual paths are appended once uploads succeed.
      const optimisticText = hasAttachments
        ? `${caption ? caption + "\n\n" : ""}(uploading ${pendingAttachments.length} image${
            pendingAttachments.length === 1 ? "" : "s"
          }…)`
        : caption;
      setMessages((prev) => [
        ...prev,
        attachKey({
          role: "user",
          text: optimisticText,
          content_type: "text",
          pending: true,
        }),
      ]);
      // Snap to own send regardless of read position.
      stickToBottomRef.current = true;
      setAtBottom(true);

      // Clear composer immediately so sending feels instant. Restored on error.
      setText("");
      setAttachments([]);

      try {
        const paths: string[] = [];
        for (const att of pendingAttachments) {
          const r = await api.uploadImage(session.window_id, att.file);
          paths.push(r.path);
        }
        const lines: string[] = [];
        if (caption) lines.push(caption);
        for (const p of paths) lines.push(`(image attached: ${p})`);
        const finalText = lines.join("\n\n");

        await api.sendText(session.window_id, finalText, true);

        // Swap optimistic bubble's text to the actual outgoing prompt so the
        // WS echo matches and replaces it cleanly.
        setMessages((prev) => {
          const idx = prev.findIndex(
            (m) => m.pending && m.role === "user" && m.text === optimisticText,
          );
          if (idx === -1) return prev;
          const next = prev.slice();
          next[idx] = { ...next[idx], text: finalText };
          return next;
        });

        for (const a of pendingAttachments) URL.revokeObjectURL(a.previewUrl);
      } catch (err) {
        setMessages((prev) => {
          const idx = prev.findIndex(
            (m) => m.pending && m.role === "user" && m.text === optimisticText,
          );
          if (idx === -1) return prev;
          const next = prev.slice();
          next.splice(idx, 1);
          return next;
        });
        setText((cur) => (cur ? cur : payload));
        setAttachments((cur) => [...pendingAttachments, ...cur]);
        showToast((err as Error).message, "error");
      } finally {
        setSending(false);
        textareaRef.current?.focus();
      }
    },
    [
      session.window_id,
      showToast,
      handleBotCommand,
      attachments,
      scrollToBottom,
    ],
  );

  const onKey = useCallback(
    async (key: string) => {
      try {
        await api.sendKey(session.window_id, key);
      } catch (err) {
        showToast((err as Error).message, "error");
      }
    },
    [session.window_id, showToast],
  );

  const onCommand = useCallback(
    async (command: string) => {
      try {
        await api.sendCommand(session.window_id, command);
      } catch (err) {
        showToast((err as Error).message, "error");
      }
    },
    [session.window_id, showToast],
  );

  const onTextareaKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.metaKey && !e.ctrlKey) {
      e.preventDefault();
      send(text);
    }
  };

  const onPaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    const files: File[] = [];
    for (const item of Array.from(items)) {
      if (item.kind === "file") {
        const f = item.getAsFile();
        if (f && f.type.startsWith("image/")) files.push(f);
      }
    }
    if (files.length > 0) {
      e.preventDefault();
      addFiles(files);
    }
  };

  const onDragOver = (e: DragEvent<HTMLDivElement>) => {
    if (!e.dataTransfer || !Array.from(e.dataTransfer.types).includes("Files"))
      return;
    e.preventDefault();
    setDragOver(true);
  };

  const onDragLeave = (e: DragEvent<HTMLDivElement>) => {
    // Only clear when leaving the composer itself, not bubbling out of a child.
    if (e.currentTarget === e.target) setDragOver(false);
  };

  const onDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragOver(false);
    addFiles(e.dataTransfer?.files ?? null);
  };

  const commitRename = async () => {
    const next = nameDraft.trim();
    if (!next || next === session.name) {
      setEditingName(false);
      setNameDraft(session.name);
      return;
    }
    await onRename(next);
    setEditingName(false);
  };

  return (
    <main className="chat-area">
      <div className="chat-header">
        <button
          type="button"
          className="burger icon-button"
          aria-label="Open menu"
          onClick={onOpenSidebar}
        >
          <Menu size={20} />
        </button>
        <div className="chat-title">
          {editingName ? (
            <input
              autoFocus
              value={nameDraft}
              onChange={(e) => setNameDraft(e.target.value)}
              onBlur={commitRename}
              onKeyDown={(e) => {
                if (e.key === "Enter") commitRename();
                if (e.key === "Escape") {
                  setEditingName(false);
                  setNameDraft(session.name);
                }
              }}
            />
          ) : (
            <div
              className="name"
              onDoubleClick={() => setEditingName(true)}
              title="Double-click to rename"
            >
              <RuntimeIcon runtime={session.runtime} size={ICON} />
              <span className="chat-title-name">{session.name}</span>
            </div>
          )}
          <div className="path">{session.cwd || "—"}</div>
        </div>
        <div
          className={`chat-menu${chatMenuOpen ? " open" : ""}`}
          ref={chatMenuOpen ? chatMenuRef : undefined}
        >
          <button
            type="button"
            className="chat-menu-trigger"
            aria-label="Session actions"
            aria-expanded={chatMenuOpen}
            title="Session actions"
            onClick={() => setChatMenuOpen((v) => !v)}
          >
            <MoreVertical size={ICON} />
          </button>
          {chatMenuOpen && (
            <div className="chat-menu-popover">
              {gitIsRepo && (
                <button
                  type="button"
                  className={diffOpen ? "active" : ""}
                  onClick={() => {
                    setChatMenuOpen(false);
                    onToggleDiff();
                  }}
                >
                  <GitCommit size={ICON} />
                  <span>Diff</span>
                </button>
              )}
              <button
                type="button"
                className={officeOpen ? "active" : ""}
                onClick={() => {
                  setChatMenuOpen(false);
                  onToggleOffice();
                }}
              >
                <Users size={ICON} />
                <span>Office</span>
              </button>
              <button
                type="button"
                onClick={() => {
                  setChatMenuOpen(false);
                  onRequestScreenshot();
                }}
              >
                <Camera size={ICON} />
                <span>Screenshot</span>
              </button>
              <button
                type="button"
                onClick={() => {
                  setChatMenuOpen(false);
                  setEditingName(true);
                }}
              >
                <Pencil size={ICON} />
                <span>Rename</span>
              </button>
              <button
                type="button"
                onClick={() => {
                  setChatMenuOpen(false);
                  void onCommand("/clear");
                  setMessages([]);
                  setHasMore(false);
                  setStreaming(null);
                  showToast("Cleared — /clear sent to agent");
                }}
              >
                <Eraser size={ICON} />
                <span>Clear history</span>
              </button>
              <button
                type="button"
                className="danger"
                onClick={() => {
                  setChatMenuOpen(false);
                  onRequestKill();
                }}
              >
                <Trash2 size={ICON} />
                <span>Kill</span>
              </button>
            </div>
          )}
        </div>
      </div>

      <div className="messages-wrapper">
        <div className="messages" ref={scrollerRef} onScroll={handleScroll}>
        {messages.length === 0 && !streaming ? (
          historyLoaded ? (
            <div className="empty-state">
              <h2>No messages yet</h2>
              <p>Send your first prompt below.</p>
            </div>
          ) : (
            <div className="empty-state">
              <div className="empty-state-spinner" />
              <p>Loading messages…</p>
            </div>
          )
        ) : (
          <div className="messages-list" ref={messagesListRef}>
            <div ref={topSentinelRef} className="messages-top-sentinel" />
            {hasMore && (
              <div className="messages-loading-older">
                {loadingOlder ? (
                  <>
                    <div className="empty-state-spinner small" />
                    <span>Loading earlier messages…</span>
                  </>
                ) : (
                  <button
                    type="button"
                    className="load-older-button"
                    onClick={loadOlder}
                  >
                    Load earlier messages
                  </button>
                )}
              </div>
            )}
            {messages.map((m, index) => {
              const isFirst = !hasMore && index === 0;
              const isLast = index === messages.length - 1 && !streaming;
              const cls =
                "messages-row" +
                (isFirst ? " messages-row-first" : "") +
                (isLast ? " messages-row-last" : "");
              return (
                <div
                  key={m._clientId}
                  data-msg-key={m._clientId}
                  className={cls}
                >
                  <MessageBubble m={m} />
                </div>
              );
            })}
            {streaming && (
              <div className="messages-row messages-row-last">
                <StreamingBubble text={streaming.text} status={streaming.status} />
              </div>
            )}
          </div>
        )}
        </div>
        {!atBottom && messages.length > 0 && (
          <button
            type="button"
            className="scroll-to-latest"
            onClick={scrollToLatest}
            aria-label="Scroll to latest message"
            title="Scroll to latest message"
          >
            <ChevronDown size={24} />
          </button>
        )}
      </div>

      <div
        className={`composer${dragOver ? " drag-over" : ""}`}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
      >
        {attachments.length > 0 && (
          <div className="attachments-row">
            {attachments.map((a) => (
              <div key={a.id} className="attachment-chip" title={a.file.name}>
                <img src={a.previewUrl} alt={a.file.name} />
                <button
                  type="button"
                  className="attachment-remove"
                  aria-label={`Remove ${a.file.name}`}
                  onClick={() => removeAttachment(a.id)}
                >
                  <X size={12} />
                </button>
              </div>
            ))}
          </div>
        )}
        <textarea
          ref={textareaRef}
          value={text}
          placeholder="Send a message — Enter to send, Shift+Enter for newline. Paste or drop images to attach."
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onTextareaKeyDown}
          onPaste={onPaste}
        />
        <input
          ref={fileInputRef}
          type="file"
          accept="image/*"
          multiple
          style={{ display: "none" }}
          onChange={(e) => {
            addFiles(e.target.files);
            // Reset value so picking the same file again still fires onChange.
            e.target.value = "";
          }}
        />
        <div className="composer-controls">
          {gitIsRepo && gitBranch ? (
            <div
              className={`branch-menu${branchMenuOpen ? " open" : ""}`}
              ref={branchMenuOpen ? branchMenuRef : undefined}
            >
              <button
                type="button"
                className="branch-button"
                aria-haspopup="listbox"
                aria-expanded={branchMenuOpen}
                title="Switch branch"
                onClick={() => setBranchMenuOpen((v) => !v)}
              >
                branch: {gitBranch}
              </button>
              {branchMenuOpen && (
                <div className="branch-menu-popover" role="listbox">
                  {branchList === null && (
                    <div className="branch-menu-empty">Loading…</div>
                  )}
                  {branchList !== null && branchList.length === 0 && (
                    <div className="branch-menu-empty">
                      {branchLoadError || "No branches"}
                    </div>
                  )}
                  {branchList !== null &&
                    branchList.map((b) => {
                      const isCurrent = b === gitBranch;
                      const isSwitching = switchingBranch === b;
                      return (
                        <button
                          key={b}
                          type="button"
                          role="option"
                          aria-selected={isCurrent}
                          className={`branch-menu-item${isCurrent ? " current" : ""}`}
                          disabled={
                            isCurrent || switchingBranch !== null
                          }
                          onClick={() => handleSwitchBranch(b)}
                        >
                          <span className="branch-menu-mark">
                            {isCurrent ? "•" : isSwitching ? "…" : ""}
                          </span>
                          <span className="branch-menu-name">{b}</span>
                        </button>
                      );
                    })}
                </div>
              )}
            </div>
          ) : (
            <span className="hint">
              {session.session_id
                ? `session: ${session.session_id.slice(0, 8)}…`
                : "session: detecting…"}
            </span>
          )}
          <div className="composer-buttons">
            <div
              className={`keys-menu${keysMenuOpen ? " open" : ""}`}
              ref={keysMenuOpen ? keysMenuRef : undefined}
            >
              <button
                type="button"
                className="icon-button"
                aria-label="Keys and commands"
                aria-expanded={keysMenuOpen}
                title="Keys and commands"
                onClick={() => setKeysMenuOpen((v) => !v)}
              >
                <Keyboard size={ICON} />
              </button>
              {keysMenuOpen && (
                <div className="keys-menu-popover">
                  <div className="keys-menu-section">
                    <div className="keys-menu-label">Keys</div>
                    <div className="keys-menu-grid">
                      {KEY_BUTTONS.map((kb) => (
                        <button
                          key={kb.key}
                          onClick={() => {
                            onKey(kb.key);
                            setKeysMenuOpen(false);
                          }}
                          title={kb.key}
                        >
                          {kb.label}
                        </button>
                      ))}
                    </div>
                  </div>
                  <div className="keys-menu-section">
                    <div className="keys-menu-label">Agent commands</div>
                    <div className="keys-menu-grid">
                      {AGENT_QUICK_COMMANDS.map((cmd) => (
                        <button
                          key={cmd}
                          onClick={() => {
                            onCommand(cmd);
                            setKeysMenuOpen(false);
                          }}
                          title="Agent command"
                        >
                          {cmd}
                        </button>
                      ))}
                    </div>
                  </div>
                  <div className="keys-menu-section">
                    <div className="keys-menu-label">Bot commands</div>
                    <div className="keys-menu-grid">
                      {BOT_QUICK_COMMANDS.map((cmd) => (
                        <button
                          key={cmd}
                          onClick={() => {
                            handleBotCommand(cmd);
                            setKeysMenuOpen(false);
                          }}
                          title={BOT_COMMANDS[cmd] || cmd}
                        >
                          {cmd}
                        </button>
                      ))}
                    </div>
                  </div>
                </div>
              )}
            </div>
            <button
              type="button"
              className="icon-button"
              aria-label="Attach image"
              onClick={() => fileInputRef.current?.click()}
              title="Attach image"
            >
              <Paperclip size={ICON} />
            </button>
            <button
              className="primary"
              disabled={sending || (!text.trim() && attachments.length === 0)}
              onClick={() => send(text)}
            >
              {sending ? "Sending…" : "Send"}
            </button>
          </div>
        </div>
      </div>
      {showSkills && (
        <SkillsModal
          runtime={session.runtime}
          onClose={() => setShowSkills(false)}
          onPick={(prefix) => {
            setText((prev) => (prev ? `${prev} ${prefix}` : prefix));
            setShowSkills(false);
            textareaRef.current?.focus();
          }}
        />
      )}
    </main>
  );
}
