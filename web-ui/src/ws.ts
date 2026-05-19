// Auto-reconnecting WebSocket client that emits typed events.
import { WsEvent } from "./api";

export type WsListener = (event: WsEvent) => void;

export class EventStream {
  private socket: WebSocket | null = null;
  private listeners = new Set<WsListener>();
  private retryDelay = 1000;
  private retryTimer: number | null = null;
  private closed = false;

  start() {
    if (this.socket || this.closed) return;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}/api/ws`;
    const ws = new WebSocket(url);
    this.socket = ws;
    ws.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data) as WsEvent;
        for (const l of this.listeners) l(data);
      } catch (err) {
        console.warn("ws parse error", err);
      }
    };
    ws.onclose = () => this.scheduleReconnect();
    ws.onopen = () => {
      this.retryDelay = 1000;
    };
    ws.onerror = () => {
      // Browsers normally fire `close` after `error`, but if a connection
      // never completes we still want to retry — guard with the same
      // scheduler (no-op if `close` already armed it).
      this.scheduleReconnect();
    };
    this.armVisibilityRefresh();
  }

  // iOS Safari (and to a lesser extent other mobile browsers) suspends
  // background tabs without firing `close` on their WebSockets. When the
  // page comes back, the socket looks `OPEN` but is dead — no events
  // arrive. On every transition to visible, kick the socket so we either
  // confirm it's healthy on the next message or rebuild it.
  private visibilityArmed = false;
  private armVisibilityRefresh() {
    if (this.visibilityArmed) return;
    this.visibilityArmed = true;
    const refresh = () => {
      if (this.closed) return;
      if (document.visibilityState !== "visible") return;
      const sock = this.socket;
      if (!sock) {
        this.scheduleReconnect();
        return;
      }
      if (sock.readyState !== WebSocket.OPEN) return;
      // Force a clean reconnect. onclose handler will reschedule.
      sock.close();
    };
    document.addEventListener("visibilitychange", refresh);
    window.addEventListener("pageshow", refresh);
    window.addEventListener("focus", refresh);
  }

  private scheduleReconnect() {
    this.socket = null;
    if (this.closed || this.retryTimer !== null) return;
    // ±50% jitter so multiple tabs / clients don't reconnect in lockstep
    // after a server outage.
    const jitter = 0.5 + Math.random();
    this.retryTimer = window.setTimeout(() => {
      this.retryTimer = null;
      this.retryDelay = Math.min(this.retryDelay * 1.6, 10_000);
      this.start();
    }, this.retryDelay * jitter);
  }

  stop() {
    this.closed = true;
    if (this.retryTimer) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    if (this.socket) {
      this.socket.close();
      this.socket = null;
    }
  }

  subscribe(listener: WsListener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
}
