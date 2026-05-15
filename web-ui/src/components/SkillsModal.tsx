import { useEffect, useState } from "react";
import { api } from "../api";

interface Props {
  runtime: string;
  onPick: (skill: string) => void;
  onClose: () => void;
}

// Codex invokes skills with `$name`; Claude uses `/name`. The web mirrors
// the Telegram bot's skill picker — clicking a skill appends the prefix
// to the composer text so the user can finish their prompt.
export function SkillsModal({ runtime, onPick, onClose }: Props) {
  const [skills, setSkills] = useState<string[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .listSkills(runtime)
      .then((r) => setSkills(r.skills))
      .catch((err: Error) => setError(err.message));
  }, [runtime]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const prefix = runtime === "claude" ? "/" : "$";

  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div className="modal" onMouseDown={(e) => e.stopPropagation()}>
        <h2>Skills · {runtime}</h2>
        <p style={{ color: "var(--text-2)", marginTop: 0 }}>
          Invoke a skill with <code>{prefix}name</code> followed by your prompt.
        </p>
        {error && <div className="login-error">{error}</div>}
        {!skills && !error && (
          <div style={{ color: "var(--text-2)" }}>Loading…</div>
        )}
        {skills?.length === 0 && (
          <div style={{ color: "var(--text-2)" }}>
            No skills found for this runtime.
          </div>
        )}
        <div style={{ maxHeight: 360, overflowY: "auto" }}>
          {skills?.map((name) => (
            <div
              key={name}
              className="dir-row"
              onClick={() => onPick(`${prefix}${name} `)}
              title="Insert into composer"
            >
              <span>
                {prefix}
                {name}
              </span>
            </div>
          ))}
        </div>
        <div className="modal-actions">
          <button className="primary" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
