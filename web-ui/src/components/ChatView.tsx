import {
  ClipboardEvent,
  DragEvent,
  KeyboardEvent,
  memo,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
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
  Terminal as TerminalIcon,
  Trash2,
  User,
  Users,
  X,
} from "lucide-react";
import {
  api,
  SessionMessage,
  SessionMessagesResponse,
  SessionSummary,
  SlashCommandHint,
  WsEvent,
} from "../api";
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
  onToggleTerm: () => void;
  termOpen: boolean;
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

const FALLBACK_CODEX_SLASH_COMMANDS: SlashCommandHint[] = [
  { command: "/clear", description: "Clear conversation history" },
  { command: "/new", description: "Start a new conversation" },
  { command: "/compact", description: "Compact conversation context" },
  { command: "/status", description: "Show current agent status" },
  { command: "/cost", description: "Show token and cost usage" },
  { command: "/help", description: "Show Codex help" },
  { command: "/memory", description: "Edit project memory instructions" },
  { command: "/model", description: "Switch AI model" },
  { command: "/plan", description: "Draft or update a plan" },
  { command: "/skills", description: "List Codex skills" },
];

const FALLBACK_CLAUDE_SLASH_COMMANDS: SlashCommandHint[] = [
  { command: "/clear", description: "Clear conversation history" },
  { command: "/compact", description: "Compact conversation context" },
  { command: "/config", description: "Open Claude Code configuration" },
  { command: "/cost", description: "Show token and cost usage" },
  { command: "/doctor", description: "Check Claude Code installation" },
  { command: "/help", description: "Show Claude Code help" },
  { command: "/init", description: "Initialize project instructions" },
  { command: "/login", description: "Sign in to Claude Code" },
  { command: "/logout", description: "Sign out of Claude Code" },
  { command: "/mcp", description: "Manage MCP servers" },
  { command: "/memory", description: "Edit Claude memory files" },
  { command: "/model", description: "Switch AI model" },
  { command: "/permissions", description: "Review tool permissions" },
  { command: "/resume", description: "Resume a previous conversation" },
  { command: "/review", description: "Request code review" },
  { command: "/status", description: "Show current agent status" },
];

function parseSlashCommand(text: string): string | null {
  const trimmed = text.trim();
  if (!trimmed.startsWith("/")) return null;
  return trimmed.split(/\s+/)[0].toLowerCase();
}

// Client-side _clientId keeps memo + anchor lookup stable across prepends.
// _order is the local arrival/history sequence used as a tie-breaker when
// REST catch-up reconciles live WebSocket messages with transcript history.
type ChatMessage = SessionMessage & {
  pending?: boolean;
  _clientId: string;
  _order: number;
};
type HistoryCacheEntry = {
  messages: ChatMessage[];
  hasMore: boolean;
  sessionId: string | null;
  oldestTimestamp: string | null;
  newestTimestamp: string | null;
  historyVersion: string | null;
};
type SlashTokenRange = {
  start: number;
  end: number;
  query: string;
};
type ChoicePromptOption = {
  label: string;
  description: string;
  value: string;
};
type ChoicePrompt = {
  kind: "plan" | "request";
  title: string;
  options: ChoicePromptOption[];
};

const HISTORY_CACHE_MAX_WINDOWS = 8;
const HISTORY_CACHE_MAX_MESSAGES = 2000;
const BOTTOM_STICKY_THRESHOLD_PX = 48;

let _clientIdCounter = 0;
let _messageOrderCounter = 0;
function nextMessageOrder(): number {
  _messageOrderCounter += 1;
  return _messageOrderCounter;
}

function attachKey(m: SessionMessage & { pending?: boolean }): ChatMessage {
  return {
    ...m,
    _clientId: `m${++_clientIdCounter}`,
    _order: nextMessageOrder(),
  };
}

function latestTimestamp(messages: SessionMessage[]): string | null {
  let latest: string | null = null;
  for (const m of messages) {
    if (m.timestamp && (!latest || m.timestamp > latest)) latest = m.timestamp;
  }
  return latest;
}

function firstTimestamp(messages: SessionMessage[]): string | null {
  for (const m of messages) {
    if (m.timestamp) return m.timestamp;
  }
  return null;
}

function messageContentKey(m: SessionMessage): string {
  const toolKey = m.tool_use_id || m.tool_name || "";
  return `${m.role}|${m.content_type}|${toolKey}|${m.text}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function hasBlockingPromptFlag(value: Record<string, unknown>): boolean {
  return ["isOther", "is_other", "isSecret", "is_secret"].some((key) =>
    Boolean(value[key]),
  );
}

function normalizePromptOptions(
  rawOptions: unknown,
): ChoicePromptOption[] | null {
  if (!Array.isArray(rawOptions)) return null;
  const options: ChoicePromptOption[] = [];
  for (const item of rawOptions) {
    let label = "";
    let description = "";
    if (typeof item === "string") {
      label = item.trim();
    } else if (isRecord(item)) {
      if (hasBlockingPromptFlag(item)) return null;
      for (const key of ["label", "title", "name", "value", "text"]) {
        label = stringValue(item[key]);
        if (label) break;
      }
      description = stringValue(item.description);
    }
    if (!label) continue;
    if (label.toLowerCase() === "other") return null;
    options.push({
      label,
      description,
      value: String(options.length + 1),
    });
  }
  return options.length > 0 ? options : null;
}

function choicePromptForMessage(m: SessionMessage): ChoicePrompt | null {
  if (m.role !== "assistant" || m.content_type !== "tool_use") return null;
  const toolName = m.tool_name ?? "";

  if (toolName === "ExitPlanMode" || toolName === "exit_plan_mode") {
    return {
      kind: "plan",
      title: "Plan decision",
      options: [
        {
          label: "Yes, implement this plan",
          description: "Switch to Default and start coding.",
          value: "1",
        },
        {
          label: "No, stay in Plan mode",
          description: "Continue planning with the model.",
          value: "2",
        },
      ],
    };
  }

  if (toolName !== "request_user_input" && toolName !== "AskUserQuestion") {
    return null;
  }
  if (!isRecord(m.tool_input) || hasBlockingPromptFlag(m.tool_input)) {
    return null;
  }

  let title = "";
  let rawOptions: unknown;
  const questions = m.tool_input.questions;
  if (Array.isArray(questions)) {
    if (questions.length !== 1) return null;
    const question = questions[0];
    if (!isRecord(question) || hasBlockingPromptFlag(question)) return null;
    title = stringValue(question.question);
    rawOptions = question.options;
  }

  if (!title) {
    title =
      stringValue(m.tool_input.question) ||
      stringValue(m.tool_input.prompt) ||
      stringValue(m.tool_input.title);
  }
  rawOptions ??=
    m.tool_input.options ?? m.tool_input.choices ?? m.tool_input.items;

  const options = normalizePromptOptions(rawOptions);
  if (!title || !options) return null;
  return { kind: "request", title, options };
}

function promptMessageKey(m: ChatMessage): string {
  return `${m.tool_use_id || "tool"}:${m._clientId}`;
}

function latestActiveChoiceMessageKey(messages: ChatMessage[]): string | null {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const m = messages[i];
    if (m.role === "user" || m.content_type === "tool_result") return null;
    if (m.pending) continue;
    const prompt = choicePromptForMessage(m);
    if (prompt) return promptMessageKey(m);
  }
  return null;
}

function parseMessageTimestamp(value: string | undefined): number | null {
  if (!value) return null;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? null : parsed;
}

function messageSequence(m: SessionMessage): number | null {
  return typeof m.seq === "number" && Number.isFinite(m.seq) ? m.seq : null;
}

function findDuplicateMessageIndex(
  messages: ChatMessage[],
  incoming: SessionMessage,
): number {
  const incomingKey = messageContentKey(incoming);
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const existing = messages[i];
    if (
      existing.pending &&
      incoming.role === "user" &&
      existing.role === "user" &&
      existing.text === incoming.text
    ) {
      return i;
    }
    if (messageContentKey(existing) !== incomingKey) continue;
    if (
      !existing.timestamp ||
      !incoming.timestamp ||
      existing.timestamp === incoming.timestamp
    ) {
      return i;
    }
  }
  return -1;
}

function reconcileMessage(
  existing: ChatMessage,
  incoming: SessionMessage,
): ChatMessage {
  const learnedTimestamp = !existing.timestamp && !!incoming.timestamp;
  const clearedPending = !!existing.pending;
  const learnedSequence =
    messageSequence(existing) === null && messageSequence(incoming) !== null;
  const needsOrderRefresh =
    learnedTimestamp ||
    clearedPending ||
    learnedSequence ||
    !!incoming.timestamp ||
    messageSequence(incoming) !== null;

  return {
    ...existing,
    role: incoming.role,
    text: incoming.text,
    content_type: incoming.content_type,
    timestamp: incoming.timestamp ?? existing.timestamp,
    seq: incoming.seq ?? existing.seq,
    tool_name: incoming.tool_name ?? existing.tool_name,
    tool_input: incoming.tool_input ?? existing.tool_input,
    tool_use_id: incoming.tool_use_id ?? existing.tool_use_id,
    pending: false,
    _clientId: existing._clientId,
    _order: needsOrderRefresh ? nextMessageOrder() : existing._order,
  };
}

function sortMessagesByKnownOrder(messages: ChatMessage[]): ChatMessage[] {
  return messages
    .map((message, index) => ({ message, index }))
    .sort((a, b) => {
      const aSeq = messageSequence(a.message);
      const bSeq = messageSequence(b.message);
      if (aSeq !== null && bSeq !== null && aSeq !== bSeq) {
        return aSeq - bSeq;
      }

      const aTime = parseMessageTimestamp(a.message.timestamp);
      const bTime = parseMessageTimestamp(b.message.timestamp);
      if (aTime !== null && bTime !== null && aTime !== bTime) {
        return aTime - bTime;
      }

      if (
        aTime !== null &&
        bTime !== null &&
        a.message._order !== b.message._order
      ) {
        return a.message._order - b.message._order;
      }

      return a.index - b.index;
    })
    .map(({ message }) => message);
}

function mergeAppendMessages(
  current: ChatMessage[],
  incoming: SessionMessage[],
): ChatMessage[] {
  if (incoming.length === 0) return current;
  let changed = false;
  const next = current.slice();
  for (const m of incoming) {
    const duplicateIndex = findDuplicateMessageIndex(next, m);
    if (duplicateIndex === -1) {
      next.push(attachKey(m));
      changed = true;
      continue;
    }
    const reconciled = reconcileMessage(next[duplicateIndex], m);
    if (reconciled !== next[duplicateIndex]) {
      next[duplicateIndex] = reconciled;
      changed = true;
    }
  }
  if (!changed) return current;
  return sortMessagesByKnownOrder(next);
}

function responseNewestTimestamp(
  response: SessionMessagesResponse,
  messages: SessionMessage[] = response.messages,
): string | null {
  return response.newest_timestamp ?? latestTimestamp(messages);
}

function timestampFromEvent(event: { timestamp?: string | null; ts: number }): string {
  return event.timestamp || new Date(event.ts * 1000).toISOString();
}

function agentSlashCommandsForRuntime(runtime: string): SlashCommandHint[] {
  return runtime === "claude"
    ? FALLBACK_CLAUDE_SLASH_COMMANDS
    : FALLBACK_CODEX_SLASH_COMMANDS;
}

function slashTokenRange(
  value: string,
  selectionStart: number | null | undefined,
): SlashTokenRange | null {
  const caret = Math.max(
    0,
    Math.min(selectionStart ?? value.length, value.length),
  );
  const beforeCaret = value.slice(0, caret);
  const tokenStart = Math.max(
    beforeCaret.lastIndexOf(" "),
    beforeCaret.lastIndexOf("\n"),
    beforeCaret.lastIndexOf("\t"),
  ) + 1;
  if (value.slice(0, tokenStart).trim().length > 0) return null;

  const afterCaret = value.slice(caret);
  const nextWhitespace = afterCaret.search(/\s/);
  const tokenEnd =
    nextWhitespace === -1 ? value.length : caret + nextWhitespace;
  const token = value.slice(tokenStart, tokenEnd);
  if (!token.startsWith("/")) return null;
  if (token.includes("@")) return null;
  return {
    start: tokenStart,
    end: tokenEnd,
    query: token.slice(1).toLowerCase(),
  };
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
  choicePrompt,
  choicePending = false,
  choiceDisabled = false,
  onSelectChoice,
}: {
  m: ChatMessage;
  choicePrompt?: ChoicePrompt;
  choicePending?: boolean;
  choiceDisabled?: boolean;
  onSelectChoice?: (option: ChoicePromptOption) => void;
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
        {choicePrompt && onSelectChoice && (
          <div className={`choice-panel ${choicePrompt.kind}`}>
            <div className="choice-panel-title">{choicePrompt.title}</div>
            <div className="choice-options">
              {choicePrompt.options.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  className="choice-option"
                  disabled={choiceDisabled || choicePending}
                  onClick={() => onSelectChoice(option)}
                >
                  <span className="choice-option-index">{option.value}</span>
                  <span className="choice-option-text">
                    <span className="choice-option-label">{option.label}</span>
                    {option.description && (
                      <span className="choice-option-description">
                        {option.description}
                      </span>
                    )}
                  </span>
                </button>
              ))}
            </div>
          </div>
        )}
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
  onToggleTerm,
  termOpen,
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
  const [slashRange, setSlashRange] = useState<SlashTokenRange | null>(null);
  const [slashActiveIndex, setSlashActiveIndex] = useState(0);
  const [choiceSendingKey, setChoiceSendingKey] = useState<string | null>(null);
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
  const hasMoreRef = useRef(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  // Captured by loadOlder before prepend, consumed by the layout effect to
  // restore the same on-screen bubble position.
  const pendingAnchorRef = useRef<{ clientId: string; top: number } | null>(
    null,
  );

  const snapToBottom = useCallback((behavior: ScrollBehavior = "auto") => {
    const el = scrollerRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior });
  }, []);

  const scheduleBottomSnap = useCallback(
    (behavior: ScrollBehavior = "auto") => {
      requestAnimationFrame(() => {
        if (!stickToBottomRef.current) return;
        snapToBottom(behavior);
        requestAnimationFrame(() => {
          if (stickToBottomRef.current) snapToBottom("auto");
        });
      });
    },
    [snapToBottom],
  );

  const scrollToLatest = useCallback(() => {
    stickToBottomRef.current = true;
    setAtBottom(true);
    scheduleBottomSnap("smooth");
  }, [scheduleBottomSnap]);

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
  const historyCacheRef = useRef<Map<string, HistoryCacheEntry>>(new Map());
  // Per-session draft cache. Switching sessions stashes the current
  // composer text under the previous window_id and restores any draft for
  // the new one, so each topic keeps its own pending message.
  const draftsRef = useRef<Record<string, string>>({});
  const textRef = useRef(text);
  useEffect(() => {
    textRef.current = text;
  }, [text]);
  useEffect(() => {
    hasMoreRef.current = hasMore;
  }, [hasMore]);

  const fallbackSlashCommands = useMemo(
    () => agentSlashCommandsForRuntime(session.runtime),
    [session.runtime],
  );
  const [agentSlashCommands, setAgentSlashCommands] =
    useState<SlashCommandHint[]>(fallbackSlashCommands);

  const loadSlashCommands = useCallback(async () => {
    const runtime = session.runtime;
    const windowId = session.window_id;
    setAgentSlashCommands(fallbackSlashCommands);
    try {
      const response = await api.listSlashCommands(runtime, windowId);
      if (windowIdRef.current !== windowId) return;
      const commands =
        response.commands.length > 0 ? response.commands : fallbackSlashCommands;
      setAgentSlashCommands(commands);
    } catch {
      if (windowIdRef.current === windowId) {
        setAgentSlashCommands(fallbackSlashCommands);
      }
    }
  }, [fallbackSlashCommands, session.runtime, session.window_id]);

  useEffect(() => {
    void loadSlashCommands();
  }, [loadSlashCommands]);

  const slashHints = useMemo(() => {
    if (!slashRange) return [];
    const prefix = `/${slashRange.query}`;
    return agentSlashCommands.filter((hint) =>
      hint.command.toLowerCase().startsWith(prefix),
    );
  }, [agentSlashCommands, slashRange]);
  const showSlashHints = slashRange !== null && slashHints.length > 0;
  const activeChoiceMessageKey = useMemo(
    () => latestActiveChoiceMessageKey(messages),
    [messages],
  );
  const activeChoiceMessage = useMemo(
    () =>
      activeChoiceMessageKey
        ? messages.find((m) => promptMessageKey(m) === activeChoiceMessageKey) ??
          null
        : null,
    [activeChoiceMessageKey, messages],
  );
  const displayMessages = useMemo(
    () =>
      activeChoiceMessage
        ? messages.filter((m) => m._clientId !== activeChoiceMessage._clientId)
        : messages,
    [activeChoiceMessage, messages],
  );

  useEffect(() => {
    setSlashActiveIndex((idx) =>
      slashHints.length === 0 ? 0 : Math.min(idx, slashHints.length - 1),
    );
  }, [slashHints.length]);

  useEffect(() => {
    if (choiceSendingKey && choiceSendingKey !== activeChoiceMessageKey) {
      setChoiceSendingKey(null);
    }
  }, [activeChoiceMessageKey, choiceSendingKey]);

  const refreshSlashHints = useCallback(
    (value: string, selectionStart: number | null | undefined) => {
      const nextRange = slashTokenRange(value, selectionStart);
      setSlashRange(nextRange);
      setSlashActiveIndex(0);
    },
    [],
  );

  const closeSlashHints = useCallback(() => {
    setSlashRange(null);
    setSlashActiveIndex(0);
  }, []);

  const insertSlashHint = useCallback(
    (hint: SlashCommandHint) => {
      const currentText = textRef.current;
      const el = textareaRef.current;
      const currentRange =
        slashRange ?? slashTokenRange(currentText, el?.selectionStart);
      if (!currentRange) return;

      const inserted = `${hint.command} `;
      const nextText =
        currentText.slice(0, currentRange.start) +
        inserted +
        currentText.slice(currentRange.end);
      const nextCaret = currentRange.start + inserted.length;
      textRef.current = nextText;
      setText(nextText);
      closeSlashHints();
      requestAnimationFrame(() => {
        const textarea = textareaRef.current;
        if (!textarea) return;
        textarea.focus();
        textarea.setSelectionRange(nextCaret, nextCaret);
      });
    },
    [closeSlashHints, slashRange],
  );

  const storeHistoryCache = useCallback(
    (
      windowId: string,
      entry: {
        messages: ChatMessage[];
        hasMore: boolean;
        sessionId: string | null;
        oldestTimestamp?: string | null;
        newestTimestamp?: string | null;
        historyVersion?: string | null;
      },
    ) => {
      const cache = historyCacheRef.current;
      const previous = cache.get(windowId);
      cache.delete(windowId);
      cache.set(windowId, {
        messages: entry.messages.slice(-HISTORY_CACHE_MAX_MESSAGES),
        hasMore: entry.hasMore,
        sessionId: entry.sessionId,
        oldestTimestamp:
          entry.oldestTimestamp ??
          previous?.oldestTimestamp ??
          firstTimestamp(entry.messages),
        newestTimestamp: entry.newestTimestamp ?? latestTimestamp(entry.messages),
        historyVersion: entry.historyVersion ?? previous?.historyVersion ?? null,
      });
      while (cache.size > HISTORY_CACHE_MAX_WINDOWS) {
        const oldest = cache.keys().next().value;
        if (!oldest) break;
        cache.delete(oldest);
      }
    },
    [],
  );

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
    setChoiceSendingKey(null);
    closeSlashHints();
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
  }, [closeSlashHints, session.window_id, session.session_id, session.name]);

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
    const windowId = session.window_id;
    const expectedSessionId = session.session_id;
    const cached = historyCacheRef.current.get(windowId);
    const cachedMatches =
      !!cached &&
      (!expectedSessionId || !cached.sessionId || cached.sessionId === expectedSessionId);

    stickToBottomRef.current = true;
    setAtBottom(true);
    pendingAnchorRef.current = null;

    if (cached && cachedMatches) {
      historyCacheRef.current.delete(windowId);
      historyCacheRef.current.set(windowId, cached);
      setMessages(cached.messages);
      setHasMore(cached.hasMore);
      hasMoreRef.current = cached.hasMore;
      sessionIdRef.current = cached.sessionId ?? expectedSessionId;
      setHistoryLoaded(true);
      scheduleBottomSnap();
    } else {
      if (cached) historyCacheRef.current.delete(windowId);
      setMessages([]);
      setHasMore(false);
      hasMoreRef.current = false;
      setHistoryLoaded(false);
    }

    const refreshAfter =
      cached && cachedMatches ? cached.newestTimestamp || undefined : undefined;
    api
      .getMessages(windowId, refreshAfter ? { after: refreshAfter } : undefined)
      .then((r) => {
        if (cancelled) return;
        if (windowIdRef.current !== windowId) return;
        if (refreshAfter) {
          const cacheWasRebased =
            cached &&
            cached.historyVersion &&
            r.history_version &&
            r.history_version !== cached.historyVersion &&
            r.oldest_timestamp !== cached.oldestTimestamp;
          if (cacheWasRebased) {
            return api.getMessages(windowId).then((fresh) => {
              if (cancelled) return;
              if (windowIdRef.current !== windowId) return;
              const loaded = fresh.messages.map(attachKey);
              setMessages(loaded);
              setHasMore(fresh.has_more);
              hasMoreRef.current = fresh.has_more;
              sessionIdRef.current = fresh.session_id;
              storeHistoryCache(windowId, {
                messages: loaded,
                hasMore: fresh.has_more,
                sessionId: fresh.session_id,
                oldestTimestamp: fresh.oldest_timestamp ?? null,
                newestTimestamp: responseNewestTimestamp(fresh),
                historyVersion: fresh.history_version ?? null,
              });
              scheduleBottomSnap();
            });
          }
          setMessages((prev) => {
            const next = mergeAppendMessages(prev, r.messages);
            storeHistoryCache(windowId, {
              messages: next,
              hasMore: hasMoreRef.current,
              sessionId: r.session_id,
              oldestTimestamp: r.oldest_timestamp ?? cached?.oldestTimestamp,
              newestTimestamp: responseNewestTimestamp(r, next),
              historyVersion: r.history_version ?? null,
            });
            return next;
          });
          scheduleBottomSnap();
        } else {
          const loaded = r.messages.map(attachKey);
          setMessages(loaded);
          setHasMore(r.has_more);
          hasMoreRef.current = r.has_more;
          storeHistoryCache(windowId, {
            messages: loaded,
            hasMore: r.has_more,
            sessionId: r.session_id,
            oldestTimestamp: r.oldest_timestamp ?? null,
            newestTimestamp: responseNewestTimestamp(r),
            historyVersion: r.history_version ?? null,
          });
          scheduleBottomSnap();
        }
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
  }, [
    scheduleBottomSnap,
    session.session_id,
    session.window_id,
    showToast,
    storeHistoryCache,
  ]);

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
          setMessages((prev) => {
            const next = mergeAppendMessages(prev, r.messages);
            storeHistoryCache(session.window_id, {
              messages: next,
              hasMore: r.has_more,
              sessionId: r.session_id,
              oldestTimestamp: r.oldest_timestamp ?? null,
              newestTimestamp: responseNewestTimestamp(r, next),
              historyVersion: r.history_version ?? null,
            });
            return next;
          });
          setHasMore(r.has_more);
          hasMoreRef.current = r.has_more;
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
    const next = dist <= BOTTOM_STICKY_THRESHOLD_PX;
    stickToBottomRef.current = next;
    setAtBottom((prev) => (prev !== next ? next : prev));
  }, []);

  // New session always lands at the bottom regardless of prior scroll.
  useEffect(() => {
    stickToBottomRef.current = true;
    setAtBottom(true);
    scheduleBottomSnap();
  }, [scheduleBottomSnap, session.window_id]);

  // Re-pin to bottom on deferred layout (images, fonts, keyboard close).
  // Anchoring above-viewport rows is intentionally absent — any
  // scrollTop write during iOS momentum scroll cancels the momentum.
  useEffect(() => {
    const list = messagesListRef.current;
    const el = scrollerRef.current;
    if (!list || !el) return;
    const ro = new ResizeObserver(() => {
      if (stickToBottomRef.current) scheduleBottomSnap();
    });
    ro.observe(list);
    return () => ro.disconnect();
  }, [messages.length > 0, scheduleBottomSnap]);

  useEffect(() => {
    const snapIfSticky = () => {
      if (stickToBottomRef.current) scheduleBottomSnap();
    };
    window.addEventListener("resize", snapIfSticky);
    window.visualViewport?.addEventListener("resize", snapIfSticky);
    return () => {
      window.removeEventListener("resize", snapIfSticky);
      window.visualViewport?.removeEventListener("resize", snapIfSticky);
    };
  }, [scheduleBottomSnap]);

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
          const next = mergeAppendMessages(prev, r.messages);
          storeHistoryCache(wid, {
            messages: next,
            hasMore: hasMoreRef.current,
            sessionId: r.session_id,
            oldestTimestamp: r.oldest_timestamp ?? null,
            newestTimestamp: responseNewestTimestamp(r, next),
            historyVersion: r.history_version ?? null,
          });
          return next;
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
  }, [storeHistoryCache]);

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
      if (event.type === "slash_commands_changed") {
        if (
          event.window_id === windowIdRef.current ||
          event.runtime === session.runtime
        ) {
          void loadSlashCommands();
        }
        return;
      }
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
        const next = mergeAppendMessages(prev, [
          {
            role: event.role,
            text: event.text,
            content_type: event.content_type,
            timestamp: timestampFromEvent(event),
            seq: event.seq,
            tool_name: event.tool_name,
            tool_input: event.tool_input,
            tool_use_id: event.tool_use_id,
          },
        ]);
        if (next === prev) return prev;
        storeHistoryCache(windowIdRef.current, {
          messages: next,
          hasMore: hasMoreRef.current,
          sessionId: event.session_id,
          newestTimestamp: latestTimestamp(next),
        });
        return next;
      });
    });
  }, [loadSlashCommands, session.runtime, storeHistoryCache, subscribeWs]);

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

  const handleChoiceSelect = useCallback(
    async (m: ChatMessage, option: ChoicePromptOption) => {
      const key = promptMessageKey(m);
      setChoiceSendingKey(key);
      try {
        await api.sendText(session.window_id, option.value, true);
      } catch (err) {
        setChoiceSendingKey((current) => (current === key ? null : current));
        showToast((err as Error).message, "error");
      }
    },
    [session.window_id, showToast],
  );

  const send = useCallback(
    async (payload: string) => {
      const caption = payload.trimEnd();
      const hasAttachments = attachments.length > 0;
      if (!caption && !hasAttachments) return;
      closeSlashHints();

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
      setMessages((prev) => {
        const next = [
          ...prev,
          attachKey({
            role: "user",
            text: optimisticText,
            content_type: "text",
            pending: true,
          }),
        ];
        storeHistoryCache(session.window_id, {
          messages: next,
          hasMore: hasMoreRef.current,
          sessionId: sessionIdRef.current,
        });
        return next;
      });
      // Snap to own send regardless of read position.
      stickToBottomRef.current = true;
      setAtBottom(true);
      scheduleBottomSnap();

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
          storeHistoryCache(session.window_id, {
            messages: next,
            hasMore: hasMoreRef.current,
            sessionId: sessionIdRef.current,
          });
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
          storeHistoryCache(session.window_id, {
            messages: next,
            hasMore: hasMoreRef.current,
            sessionId: sessionIdRef.current,
          });
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
      storeHistoryCache,
      closeSlashHints,
      scheduleBottomSnap,
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
    if (showSlashHints) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSlashActiveIndex((idx) => (idx + 1) % slashHints.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setSlashActiveIndex(
          (idx) => (idx - 1 + slashHints.length) % slashHints.length,
        );
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        insertSlashHint(slashHints[slashActiveIndex]);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        closeSlashHints();
        return;
      }
    }

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
                className={termOpen ? "active" : ""}
                onClick={() => {
                  setChatMenuOpen(false);
                  onToggleTerm();
                }}
              >
                <TerminalIcon size={ICON} />
                <span>Terminal</span>
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
                  hasMoreRef.current = false;
                  sessionIdRef.current = null;
                  historyCacheRef.current.delete(session.window_id);
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
            {displayMessages.map((m, index) => {
              const isFirst = !hasMore && index === 0;
              const isLast =
                index === displayMessages.length - 1 &&
                !streaming &&
                !activeChoiceMessage;
              const choicePrompt = choicePromptForMessage(m) ?? undefined;
              const choiceKey = choicePrompt ? promptMessageKey(m) : null;
              const isActiveChoice = choiceKey === activeChoiceMessageKey;
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
                  <MessageBubble
                    m={m}
                    choicePrompt={isActiveChoice ? choicePrompt : undefined}
                    choicePending={choiceKey === choiceSendingKey}
                    choiceDisabled={
                      choiceSendingKey !== null && choiceKey !== choiceSendingKey
                    }
                    onSelectChoice={(option) => handleChoiceSelect(m, option)}
                  />
                </div>
              );
            })}
            {streaming && (
              <div
                className={
                  "messages-row" +
                  (!activeChoiceMessage ? " messages-row-last" : "")
                }
              >
                <StreamingBubble text={streaming.text} status={streaming.status} />
              </div>
            )}
            {activeChoiceMessage && (
              <div
                key={activeChoiceMessage._clientId}
                data-msg-key={activeChoiceMessage._clientId}
                className="messages-row messages-row-last"
              >
                <MessageBubble
                  m={activeChoiceMessage}
                  choicePrompt={
                    choicePromptForMessage(activeChoiceMessage) ?? undefined
                  }
                  choicePending={
                    promptMessageKey(activeChoiceMessage) === choiceSendingKey
                  }
                  choiceDisabled={false}
                  onSelectChoice={(option) =>
                    handleChoiceSelect(activeChoiceMessage, option)
                  }
                />
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
        {showSlashHints && (
          <div
            id="slash-command-hints"
            className="slash-hints"
            role="listbox"
            aria-label={`${session.runtime === "claude" ? "Claude" : "Codex"} slash commands`}
            onMouseDown={(e) => e.preventDefault()}
          >
            <div className="slash-hints-label">
              {session.runtime === "claude" ? "Claude" : "Codex"} commands
            </div>
            {slashHints.map((hint, index) => (
              <button
                key={hint.command}
                type="button"
                role="option"
                aria-selected={index === slashActiveIndex}
                className={
                  "slash-hint-item" +
                  (index === slashActiveIndex ? " active" : "")
                }
                onMouseEnter={() => setSlashActiveIndex(index)}
                onClick={() => insertSlashHint(hint)}
              >
                <span className="slash-hint-command">{hint.command}</span>
                <span className="slash-hint-description">
                  {hint.description}
                </span>
              </button>
            ))}
          </div>
        )}
        <textarea
          ref={textareaRef}
          value={text}
          placeholder="Send a message — Enter to send, Shift+Enter for newline. Paste or drop images to attach."
          aria-controls={showSlashHints ? "slash-command-hints" : undefined}
          aria-expanded={showSlashHints}
          aria-autocomplete="list"
          onChange={(e) => {
            const next = e.target.value;
            setText(next);
            refreshSlashHints(next, e.target.selectionStart);
          }}
          onFocus={(e) =>
            refreshSlashHints(e.currentTarget.value, e.currentTarget.selectionStart)
          }
          onSelect={(e) =>
            refreshSlashHints(e.currentTarget.value, e.currentTarget.selectionStart)
          }
          onBlur={closeSlashHints}
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
                      {agentSlashCommands.map((hint) => (
                        <button
                          key={hint.command}
                          onClick={() => {
                            onCommand(hint.command);
                            setKeysMenuOpen(false);
                          }}
                          title={hint.description}
                        >
                          {hint.command}
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
