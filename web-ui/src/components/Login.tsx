import { FormEvent, useState } from "react";
import { api } from "../api";

interface Props {
  enabled: boolean;
  totpRequired: boolean;
  onSuccess: () => void;
}

export function Login({ enabled, totpRequired, onSuccess }: Props) {
  const [password, setPassword] = useState("");
  const [totpCode, setTotpCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setPending(true);
    try {
      await api.login(password, totpRequired ? totpCode : undefined);
      onSuccess();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setPending(false);
    }
  }

  const codeReady = !totpRequired || totpCode.replace(/\s/g, "").length === 6;

  return (
    <div className="login-shell">
      <form className="login-card" onSubmit={submit}>
        <h1>Codi</h1>
        <p className="subtitle">
          {enabled
            ? totpRequired
              ? "Enter your password and the 6-digit code from your authenticator app."
              : "Enter the password to continue."
            : "Web UI is disabled. Set WEB_UI_PASSWORD in your .env."}
        </p>
        <label htmlFor="password">Password</label>
        <input
          id="password"
          type="password"
          autoFocus
          autoComplete="current-password"
          disabled={!enabled || pending}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        {totpRequired && (
          <>
            <label htmlFor="totp">2FA code</label>
            <input
              id="totp"
              type="text"
              inputMode="numeric"
              pattern="[0-9]*"
              autoComplete="one-time-code"
              maxLength={7}
              placeholder="000000"
              disabled={!enabled || pending}
              value={totpCode}
              onChange={(e) =>
                setTotpCode(e.target.value.replace(/[^0-9]/g, "").slice(0, 6))
              }
            />
          </>
        )}
        {error && <div className="login-error">{error}</div>}
        <button
          className="primary"
          type="submit"
          disabled={!enabled || pending || !password || !codeReady}
        >
          {pending ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
