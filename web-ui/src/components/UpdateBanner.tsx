import { useEffect, useRef, useState } from "react";
import { Download, X } from "lucide-react";
import { api, WsEvent } from "../api";

const ICON = 16;
// Probe interval while waiting for the backend to come back up.
const RECONNECT_PROBE_MS = 1500;
// sessionStorage key — used so the final "reloading" state survives the
// brief gap between request and SIGTERM, and so the page can detect on
// the next mount whether it just came back from an update cycle.
const PENDING_KEY = "codi-update-pending";

interface Props {
  subscribeWs: (l: (e: WsEvent) => void) => () => void;
}

type Phase = "idle" | "offered" | "starting" | "reloading" | "error";

export function UpdateBanner({ subscribeWs }: Props) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [subject, setSubject] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const startSha = useRef<string | null>(null);

  // If the page was reloaded mid-update, the flag survives. Clear it
  // and show a quick toast-like banner so the user sees the result.
  useEffect(() => {
    if (window.sessionStorage.getItem(PENDING_KEY) !== "1") return;
    window.sessionStorage.removeItem(PENDING_KEY);
    // Optimistic: the page reload was triggered after the server came
    // back, so we're already on the new bundle.
  }, []);

  // Listen for WS-driven update notifications.
  useEffect(() => {
    return subscribeWs((event) => {
      if (event.type !== "update_available") return;
      // Don't downgrade a more advanced state (e.g. user is already
      // mid-update and another event somehow arrived).
      setPhase((prev) => (prev === "idle" || prev === "offered" ? "offered" : prev));
      setSubject(event.subject || "");
      startSha.current = event.current_sha;
    });
  }, [subscribeWs]);

  // Probe on mount for the case where an update became available while
  // the page was closed (no live WS event will fire). Skip entirely
  // when the server reports the auto-update checker is disabled.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const status = await api.getUpdateStatus();
        if (cancelled) return;
        if (!status.enabled) return;
        if (status.has_update) {
          setPhase((prev) => (prev === "idle" ? "offered" : prev));
          setSubject(status.subject || "");
          startSha.current = status.current_sha;
        }
      } catch {
        // ignore — banner just stays hidden until next WS event
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const runUpdate = async () => {
    setPhase("starting");
    setError(null);
    try {
      await api.runUpdate();
    } catch (err) {
      const msg = (err as Error).message;
      // 409 = dirty working tree on the server; nothing the user can do.
      setError(msg || "Update failed");
      setPhase("error");
      return;
    }
    window.sessionStorage.setItem(PENDING_KEY, "1");
    setPhase("reloading");
    // The backend will SIGTERM itself any second now via launchctl.
    // Wait for the *new* server to come up by polling /api/me, then
    // reload so the new bundle is fetched. Keep trying forever — the
    // service is managed by launchd and will come back, just maybe
    // slowly on first build.
    const probe = async () => {
      try {
        const r = await fetch("/api/me", {
          credentials: "include",
          cache: "no-store",
        });
        if (r.ok) {
          window.location.reload();
          return;
        }
      } catch {
        // server still down; keep waiting
      }
      window.setTimeout(probe, RECONNECT_PROBE_MS);
    };
    // Give launchd a moment to actually start the kill before we begin
    // probing — otherwise the first probe may succeed on the doomed
    // process and we'd reload too early.
    window.setTimeout(probe, RECONNECT_PROBE_MS);
  };

  if (phase === "idle") return null;

  return (
    <div
      className={`update-banner update-banner-${phase}`}
      role="status"
      aria-live="polite"
    >
      <Download size={ICON} />
      <div className="update-banner-body">
        {phase === "offered" && (
          <>
            <strong>Доступно обновление</strong>
            {subject && <span className="update-banner-subject">{subject}</span>}
          </>
        )}
        {phase === "starting" && <strong>Запускаю обновление…</strong>}
        {phase === "reloading" && (
          <strong>Обновляюсь, страница перезагрузится…</strong>
        )}
        {phase === "error" && (
          <>
            <strong>Не получилось обновиться</strong>
            {error && <span className="update-banner-subject">{error}</span>}
          </>
        )}
      </div>
      {phase === "offered" && (
        <>
          <button type="button" className="primary" onClick={runUpdate}>
            Обновить
          </button>
          <button
            type="button"
            className="icon-button"
            onClick={() => setPhase("idle")}
            aria-label="Позже"
            title="Позже"
          >
            <X size={ICON} />
          </button>
        </>
      )}
      {phase === "error" && (
        <button
          type="button"
          className="icon-button"
          onClick={() => setPhase("idle")}
          aria-label="Закрыть"
          title="Закрыть"
        >
          <X size={ICON} />
        </button>
      )}
    </div>
  );
}
