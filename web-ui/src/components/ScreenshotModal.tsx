import { useEffect, useState } from "react";
import { api } from "../api";

interface Props {
  windowId: string;
  onClose: () => void;
}

export function ScreenshotModal({ windowId, onClose }: Props) {
  const [bust, setBust] = useState(Date.now());

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const url = `${api.screenshotUrl(windowId)}&_=${bust}`;

  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div
        className="modal screenshot-modal"
        style={{ width: 1000 }}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <h2>Screenshot</h2>
        <img src={url} alt="Terminal screenshot" />
        <div className="modal-actions">
          <button onClick={() => setBust(Date.now())}>↻ Refresh</button>
          <button className="primary" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
