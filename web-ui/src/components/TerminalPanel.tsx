import { useEffect, useRef, useState } from "react";
import { Terminal as TerminalIcon, X } from "lucide-react";
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

export function TerminalPanel({ windowId, open, onClose }: Props) {
  const [mode, setMode] = useState<TermMode>("attach");
  const [status, setStatus] = useState<
    "connecting" | "open" | "reconnecting" | "closed"
  >("connecting");
  const [reconnectKey, setReconnectKey] = useState(0);
  const containerRef = useRef<HTMLDivElement | null>(null);
  // Survives across the effect re-runs that auto-reconnect triggers,
  // so consecutive failures grow the backoff instead of resetting.
  const attemptsRef = useRef(0);

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
    const encoder = new TextEncoder();
    const dataDisp = term.onData((data: string) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(encoder.encode(data));
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
      try {
        term.dispose();
      } catch {
        // ignore
      }
    };
  }, [open, windowId, mode, reconnectKey]);

  // Reset backoff whenever the user switches mode/window or opens the
  // panel, so a fresh attempt starts from the shortest delay.
  useEffect(() => {
    attemptsRef.current = 0;
  }, [open, windowId, mode]);

  return (
    <aside
      className={`term-panel${open ? " open" : ""}`}
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
            onClick={() => setMode("attach")}
            title="Attach to the topic's tmux window"
          >
            Attach
          </button>
          <button
            type="button"
            className={mode === "shell" ? "active" : ""}
            onClick={() => setMode("shell")}
            title="Fresh shell in the session cwd"
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
    </aside>
  );
}
