import { useEffect, useRef, useState } from "react";

interface Props {
  title?: string;
  initialValue: string;
  confirmLabel?: string;
  placeholder?: string;
  onConfirm: (value: string) => void | Promise<void>;
  onCancel: () => void;
}

export function RenameDialog({
  title = "Rename",
  initialValue,
  confirmLabel = "Save",
  placeholder,
  onConfirm,
  onCancel,
}: Props) {
  const [value, setValue] = useState(initialValue);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  const trimmed = value.trim();
  const unchanged = trimmed === initialValue.trim();
  const canSubmit = trimmed.length > 0 && !unchanged;

  const submit = () => {
    if (!canSubmit) return;
    onConfirm(trimmed);
  };

  return (
    <div className="modal-backdrop" onMouseDown={onCancel}>
      <div
        className="modal"
        style={{ width: 420 }}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <h2>{title}</h2>
        <div className="modal-row">
          <input
            ref={inputRef}
            value={value}
            placeholder={placeholder}
            maxLength={120}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                submit();
              }
              if (e.key === "Escape") {
                e.preventDefault();
                onCancel();
              }
            }}
          />
        </div>
        <div className="modal-actions">
          <button onClick={onCancel}>Cancel</button>
          <button
            className="primary"
            disabled={!canSubmit}
            onClick={submit}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
