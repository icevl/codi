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
    ws.onclose = () => {
      this.socket = null;
      if (this.closed) return;
      this.retryTimer = window.setTimeout(() => {
        this.retryDelay = Math.min(this.retryDelay * 1.6, 10_000);
        this.start();
      }, this.retryDelay);
    };
    ws.onopen = () => {
      this.retryDelay = 1000;
    };
    ws.onerror = () => {
      // close handler retries
    };
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
