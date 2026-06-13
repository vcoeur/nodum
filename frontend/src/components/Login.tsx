// The sign-in view. When no password is configured, it shows the CLI bootstrap
// hint instead of a form. On success the session cookie is set server-side and
// the parent re-checks the session.

import { useState } from "react";
import type { FormEvent } from "react";

import { login } from "../api";
import { errorMessage } from "../util";

interface LoginProps {
  configured: boolean;
  onSignedIn: () => void;
}

export function Login({ configured, onSignedIn }: LoginProps) {
  const [password, setPassword] = useState("");
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);

  if (!configured) {
    return (
      <main className="login">
        <div className="login-card">
          <h1>nodum</h1>
          <p className="tagline">No main password is set yet.</p>
          <p className="status">
            On the machine where nodum is installed, run <code>nodum auth set-password</code> to
            initialise it, then reload this page.
          </p>
        </div>
      </main>
    );
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setStatus("Signing in…");
    try {
      await login(password);
      onSignedIn();
    } catch (error) {
      setStatus(errorMessage(error, "Sign-in failed."));
      setPassword("");
      setBusy(false);
    }
  }

  return (
    <main className="login">
      <div className="login-card">
        <h1>nodum</h1>
        <p className="tagline">Sign in with the main password.</p>
        <form onSubmit={submit}>
          <label className="block">
            password
            <input
              type="password"
              autoComplete="current-password"
              autoFocus
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </label>
          <button type="submit" disabled={busy}>
            Sign in
          </button>
        </form>
        <div className="status" aria-live="polite">
          {status}
        </div>
      </div>
    </main>
  );
}
