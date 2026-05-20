import { useEffect, useRef, useState } from "react";
import { Keyboard, Terminal as TerminalIcon, X } from "lucide-react";
import { Terminal as XTerm } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";

const ICON = 16;

type TermMode = "attach" | "shell";

interface Props {
  windowId: string;
  open: boolean;
  onClose: () => void;
}

const FONT_FAMILY =
  '"Roboto Mono", ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace';

// Exponential backoff for auto-reconnect, capped so we don't keep
// hammering when the backend is truly down. Manual reconnect resets it.
const RECONNECT_DELAYS = [500, 1000, 2000, 4000, 8000, 15000];
const TERM_MODE_STORAGE_PREFIX = "codexbot:terminal-mode:";
const TERM_CONTROLS_STORAGE_KEY = "codexbot:terminal-mobile-controls-open";

const CONTROL_GROUPS: {
  label: string;
  buttons: { label: string; title: string; data: string }[];
}[] = [
  {
    label: "Keys",
    buttons: [
      { label: "Esc", title: "Escape", data: "\x1b" },
      { label: "Tab", title: "Tab", data: "\t" },
      { label: "Enter", title: "Enter", data: "\r" },
      { label: "Bksp", title: "Backspace", data: "\x7f" },
    ],
  },
  {
    label: "Move",
    buttons: [
      { label: "←", title: "Left", data: "\x1b[D" },
      { label: "↑", title: "Up", data: "\x1b[A" },
      { label: "↓", title: "Down", data: "\x1b[B" },
      { label: "→", title: "Right", data: "\x1b[C" },
      { label: "Home", title: "Home", data: "\x1b[H" },
      { label: "End", title: "End", data: "\x1b[F" },
      { label: "PgUp", title: "Page Up", data: "\x1b[5~" },
      { label: "PgDn", title: "Page Down", data: "\x1b[6~" },
    ],
  },
  {
    label: "Ctrl",
    buttons: [
      { label: "C", title: "Ctrl-C", data: "\x03" },
      { label: "D", title: "Ctrl-D", data: "\x04" },
      { label: "L", title: "Ctrl-L", data: "\x0c" },
      { label: "A", title: "Ctrl-A", data: "\x01" },
      { label: "E", title: "Ctrl-E", data: "\x05" },
      { label: "U", title: "Ctrl-U", data: "\x15" },
      { label: "R", title: "Ctrl-R", data: "\x12" },
      { label: "Z", title: "Ctrl-Z", data: "\x1a" },
    ],
  },
];

function isTermMode(value: string | null): value is TermMode {
  return value === "attach" || value === "shell";
}

function readStoredTermMode(windowId: string): TermMode {
  try {
    const value = localStorage.getItem(`${TERM_MODE_STORAGE_PREFIX}${windowId}`);
    return isTermMode(value) ? value : "attach";
  } catch {
    return "attach";
  }
}

function writeStoredTermMode(windowId: string, mode: TermMode): void {
  try {
    localStorage.setItem(`${TERM_MODE_STORAGE_PREFIX}${windowId}`, mode);
  } catch {
    // ignore storage errors
  }
}

function readStoredControlsOpen(): boolean {
  try {
    const value = localStorage.getItem(TERM_CONTROLS_STORAGE_KEY);
    return value === null ? true : value === "true";
  } catch {
    return true;
  }
}

function writeStoredControlsOpen(open: boolean): void {
  try {
    localStorage.setItem(TERM_CONTROLS_STORAGE_KEY, String(open));
  } catch {
    // ignore storage errors
  }
}

export function TerminalPanel({ windowId, open, onClose }: Props) {
  const [modeState, setModeState] = useState<{
    windowId: string;
    mode: TermMode;
  }>(() => ({
    windowId,
    mode: readStoredTermMode(windowId),
  }));
  const mode =
    modeState.windowId === windowId ? modeState.mode : readStoredTermMode(windowId);
  const [status, setStatus] = useState<
    "connecting" | "open" | "reconnecting" | "closed"
  >("connecting");
  const [controlsOpen, setControlsOpen] = useState(readStoredControlsOpen);
  const [reconnectKey, setReconnectKey] = useState(0);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const termRef = useRef<XTerm | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const encoderRef = useRef(new TextEncoder());
  // Survives across the effect re-runs that auto-reconnect triggers,
  // so consecutive failures grow the backoff instead of resetting.
  const attemptsRef = useRef(0);

  useEffect(() => {
    if (modeState.windowId === windowId) return;
    setModeState({ windowId, mode });
  }, [mode, modeState.windowId, windowId]);

  const setTerminalMode = (nextMode: TermMode) => {
    writeStoredTermMode(windowId, nextMode);
    setModeState({ windowId, mode: nextMode });
  };

  const setMobileControlsOpen = (nextOpen: boolean) => {
    writeStoredControlsOpen(nextOpen);
    setControlsOpen(nextOpen);
    termRef.current?.focus();
  };

  const sendTerminalData = (data: string) => {
    const ws = wsRef.current;
    if (ws?.readyState !== WebSocket.OPEN) return;
    try {
      ws.send(encoderRef.current.encode(data));
      termRef.current?.focus();
    } catch {
      // ignore dropped input while reconnecting
    }
  };

  useEffect(() => {
    if (!open || !containerRef.current) return;
    const host = containerRef.current;
    let cancelled = false;
    let reconnectTimer: number | null = null;

    const term = new XTerm({
      fontFamily: FONT_FAMILY,
      fontSize: 13,
      lineHeight: 1.15,
      cursorBlink: true,
      convertEol: false,
      scrollback: 5000,
      allowProposedApi: true,
      theme: {
        background: "#0e0f12",
        foreground: "#ececef",
        cursor: "#a78bfa",
        cursorAccent: "#0e0f12",
        selectionBackground: "rgba(167,139,250,0.35)",
      },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(host);
    fit.fit();

    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}/api/sessions/${encodeURIComponent(
      windowId,
    )}/term?mode=${mode}`;
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    termRef.current = term;
    wsRef.current = ws;

    setStatus("connecting");

    const sendResize = () => {
      if (ws.readyState !== WebSocket.OPEN) return;
      try {
        ws.send(
          JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }),
        );
      } catch {
        // ignore
      }
    };

    const scheduleReconnect = () => {
      if (cancelled) return;
      const attempt = attemptsRef.current;
      attemptsRef.current = attempt + 1;
      const delay =
        RECONNECT_DELAYS[Math.min(attempt, RECONNECT_DELAYS.length - 1)];
      setStatus("reconnecting");
      reconnectTimer = window.setTimeout(() => {
        if (cancelled) return;
        // Bumping the key re-runs this effect with a fresh WS.
        setReconnectKey((k) => k + 1);
      }, delay);
    };

    ws.onopen = () => {
      attemptsRef.current = 0;
      setStatus("open");
      sendResize();
      term.focus();
    };
    ws.onmessage = (ev) => {
      if (typeof ev.data === "string") return;
      term.write(new Uint8Array(ev.data as ArrayBuffer));
    };
    ws.onclose = (ev) => {
      if (cancelled) return;
      // 4401 = bad cookie, 4403 = bad origin, 4404 = window gone.
      // Auto-reconnecting won't help any of these — wait for the user.
      const fatal = ev.code === 4401 || ev.code === 4403 || ev.code === 4404;
      if (fatal) {
        setStatus("closed");
        try {
          term.write(
            `\r\n\x1b[31m[disconnected: ${ev.code} ${ev.reason || ""}]\x1b[0m\r\n`,
          );
        } catch {
          // term may already be disposed
        }
        return;
      }
      try {
        term.write("\r\n\x1b[33m[disconnected — reconnecting…]\x1b[0m\r\n");
      } catch {
        // term may already be disposed
      }
      scheduleReconnect();
    };
    ws.onerror = () => {
      // onclose runs right after; let it handle reconnect/state.
    };

    // Keystrokes go as binary frames so the backend doesn't try to JSON-
    // parse them as control messages (resize / etc. stay as text frames).
    const dataDisp = term.onData((data: string) => {
      sendTerminalData(data);
    });

    // Resize on container size change. fit-addon recalculates rows/cols
    // from the host element; we then tell the PTY via the same WS.
    let resizeTimer: number | null = null;
    const ro = new ResizeObserver(() => {
      if (resizeTimer !== null) window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(() => {
        try {
          fit.fit();
        } catch {
          // host may be detaching
        }
        sendResize();
      }, 60);
    });
    ro.observe(host);

    return () => {
      cancelled = true;
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      ro.disconnect();
      if (resizeTimer !== null) window.clearTimeout(resizeTimer);
      dataDisp.dispose();
      try {
        ws.close();
      } catch {
        // ignore
      }
      if (wsRef.current === ws) wsRef.current = null;
      try {
        term.dispose();
      } catch {
        // ignore
      }
      if (termRef.current === term) termRef.current = null;
    };
  }, [open, windowId, mode, reconnectKey]);

  // Reset backoff whenever the user switches mode/window or opens the
  // panel, so a fresh attempt starts from the shortest delay.
  useEffect(() => {
    attemptsRef.current = 0;
  }, [open, windowId, mode]);

  return (
    <aside
      className={`term-panel${open ? " open" : ""}${
        controlsOpen ? " controls-open" : ""
      }`}
      aria-hidden={!open}
    >
      <header className="term-panel-header">
        <div className="term-panel-title">
          <TerminalIcon size={ICON} />
          <span>Terminal</span>
        </div>
        <div className="term-mode-toggle" role="group" aria-label="Terminal mode">
          <button
            type="button"
            className={mode === "attach" ? "active" : ""}
            onClick={() => setTerminalMode("attach")}
            title="Attach to the topic's tmux window"
          >
            Attach
          </button>
          <button
            type="button"
            className={mode === "shell" ? "active" : ""}
            onClick={() => setTerminalMode("shell")}
            title="Persistent shell in the session cwd"
          >
            Shell
          </button>
        </div>
        <span className={`term-status term-status-${status}`}>
          {status === "open"
            ? "live"
            : status === "connecting"
            ? "…"
            : status === "reconnecting"
            ? "retry"
            : "off"}
        </span>
        <button
          type="button"
          className="icon-button"
          onClick={onClose}
          aria-label="Close terminal panel"
          title="Close"
        >
          <X size={ICON} />
        </button>
      </header>
      <div className="term-panel-body" ref={containerRef} />
      <button
        type="button"
        className={`term-mobile-controls-toggle${controlsOpen ? " active" : ""}`}
        onClick={() => setMobileControlsOpen(!controlsOpen)}
        aria-label={controlsOpen ? "Hide terminal controls" : "Show terminal controls"}
        title={controlsOpen ? "Hide terminal controls" : "Show terminal controls"}
      >
        <Keyboard size={ICON} />
      </button>
      {controlsOpen && (
        <div className="term-mobile-controls" aria-label="Terminal controls">
          <div className="term-mobile-controls-header">
            <span>Controls</span>
            <button
              type="button"
              className="icon-button"
              onClick={() => setMobileControlsOpen(false)}
              aria-label="Hide terminal controls"
              title="Hide"
            >
              <X size={ICON} />
            </button>
          </div>
          {CONTROL_GROUPS.map((group) => (
            <div className="term-mobile-controls-group" key={group.label}>
              <div className="term-mobile-controls-label">{group.label}</div>
              <div className="term-mobile-controls-grid">
                {group.buttons.map((button) => (
                  <button
                    type="button"
                    key={`${group.label}:${button.label}`}
                    title={button.title}
                    aria-label={button.title}
                    disabled={status !== "open"}
                    onPointerDown={(e) => e.preventDefault()}
                    onClick={() => sendTerminalData(button.data)}
                  >
                    {button.label}
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </aside>
  );
}
