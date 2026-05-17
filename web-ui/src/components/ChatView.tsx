import {
  ClipboardEvent,
  DragEvent,
  KeyboardEvent,
  memo,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { Virtuoso, VirtuosoHandle } from "react-virtuoso";
import {
  Camera,
  GitCommit,
  Keyboard,
  Menu,
  Paperclip,
  Pencil,
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

type ChatMessage = SessionMessage & { pending?: boolean };

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
  return (
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
    <div className="bubble assistant streaming">
      <div className="meta">assistant · {status}</div>
      <pre className="stream-body">{text || "…"}</pre>
    </div>
  );
});

// Stable footer component for the virtualized list. Identity stays the
// same across renders so Virtuoso doesn't remount it on every streaming
// chunk; the live data flows in through Virtuoso's `context` prop.
type StreamCtx = { text: string; status: string } | null;
function VirtuosoStreamingFooter({ context }: { context?: StreamCtx }) {
  if (!context) return null;
  return <StreamingBubble text={context.text} status={context.status} />;
}
const VIRTUOSO_COMPONENTS = { Footer: VirtuosoStreamingFooter };

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
  const [gitBranch, setGitBranch] = useState<string | null>(null);
  const [gitIsRepo, setGitIsRepo] = useState(false);
  const [branchMenuOpen, setBranchMenuOpen] = useState(false);
  const [branchList, setBranchList] = useState<string[] | null>(null);
  const [branchLoadError, setBranchLoadError] = useState<string | null>(null);
  const [switchingBranch, setSwitchingBranch] = useState<string | null>(null);
  const virtuosoRef = useRef<VirtuosoHandle | null>(null);
  // Whether the user is parked at the bottom of the chat. Virtuoso
  // reports this on every scroll; we use it to decide if a new message
  // should auto-stick to the bottom (true) or stay where the user is
  // currently reading (false).
  const [atBottom, setAtBottom] = useState(true);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const keysMenuRef = useRef<HTMLDivElement | null>(null);
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
    if (previousWid && previousWid !== session.window_id) {
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
    setHistoryLoaded(false);
    api
      .getMessages(session.window_id)
      .then((r) => {
        if (cancelled) return;
        setMessages(r.messages);
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
        // Reconcile optimistic user echo: replace the matching pending bubble.
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
            };
            return next;
          }
        }
        return [
          ...prev,
          {
            role: event.role,
            text: event.text,
            content_type: event.content_type,
          },
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

  // Auto-stick on append / stream growth, but only when the user is
  // already parked at the bottom — Virtuoso would otherwise rip the
  // viewport away from someone reading older history.
  useEffect(() => {
    if (!atBottom) return;
    const handle = virtuosoRef.current;
    if (!handle) return;
    handle.scrollToIndex({
      index: "LAST",
      behavior: "auto",
      align: "end",
    });
  }, [messages.length, streaming?.text, atBottom]);

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
        { role: "user", text: optimisticText, content_type: "text", pending: true },
      ]);

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
    [session.window_id, showToast, handleBotCommand, attachments],
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
        <div className="chat-actions">
          {gitIsRepo && (
            <button
              className={`with-icon${diffOpen ? " active" : ""}`}
              onClick={onToggleDiff}
              aria-label="Toggle diff panel"
              aria-pressed={diffOpen}
              title="Uncommitted diff"
            >
              <GitCommit size={ICON} />
              <span className="btn-label">Diff</span>
            </button>
          )}
          <button
            className={`with-icon${officeOpen ? " active" : ""}`}
            onClick={onToggleOffice}
            aria-label="Toggle office visualization"
            aria-pressed={officeOpen}
            title="Office (agent visualization)"
          >
            <Users size={ICON} />
            <span className="btn-label">Office</span>
          </button>
          <button
            className="with-icon"
            onClick={onRequestScreenshot}
            aria-label="Screenshot"
            title="Screenshot"
          >
            <Camera size={ICON} />
            <span className="btn-label">Screenshot</span>
          </button>
          <button
            className="with-icon"
            onClick={() => setEditingName(true)}
            aria-label="Rename"
            title="Rename"
          >
            <Pencil size={ICON} />
            <span className="btn-label">Rename</span>
          </button>
          <button className="danger" onClick={onRequestKill}>
            Kill
          </button>
        </div>
      </div>

      <div className="messages">
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
          <Virtuoso
            ref={virtuosoRef}
            data={messages}
            computeItemKey={(_index, m) =>
              m.timestamp
                ? `${m.role}:${m.timestamp}:${m.content_type}`
                : `${m.role}:${_index}`
            }
            itemContent={(_index, m) => <MessageBubble m={m} />}
            initialTopMostItemIndex={Math.max(0, messages.length - 1)}
            followOutput={atBottom ? "auto" : false}
            atBottomStateChange={setAtBottom}
            atBottomThreshold={64}
            increaseViewportBy={{ top: 200, bottom: 200 }}
            // Stable `components` object + live data via `context`. The
            // Footer reads context and renders the streaming bubble when
            // present — keeping component identity stable across stream
            // chunks (otherwise Virtuoso 4.x merges defaults against an
            // undefined components prop and explodes on EmptyPlaceholder).
            components={VIRTUOSO_COMPONENTS}
            context={streaming}
            className="messages-virtuoso"
          />
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
