/**
 * Route `/login` — the password gate in front of the whole app.
 *
 * Every `/api` route but this one requires a session, so this view is the only
 * thing a logged-out user can reach. It sits *outside* the app shell (a
 * sibling route, not a child of `App`): the shell is what checks the session
 * and redirects here, and a gate that contained the login page would gate it
 * too.
 *
 * The credential itself never touches this app: the server verifies the
 * password and answers with an `HttpOnly; SameSite=Strict` cookie the browser
 * attaches to every same-origin request from then on. All this view learns is
 * whether the server said yes — a 401 here is a wrong name *or* a wrong
 * password, indistinguishable by design, and rendered as one sentence.
 */

import { useState } from "react";
import type { FormEvent } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { api, ApiError } from "../../api/client";
import { describeError } from "../../lib";
import { validateCredentials } from "./credentials";
import "./login.css";

/** Where a successful login lands when no redirect carried a target. */
const DEFAULT_DESTINATION = "/search";

/** The login route. */
export default function LoginView() {
  const navigate = useNavigate();
  const location = useLocation();
  const destination =
    (location.state as { from?: string } | null)?.from ?? DEFAULT_DESTINATION;

  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [pending, setPending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    const invalid = validateCredentials(name, password);
    if (invalid) {
      setProblem(invalid);
      return;
    }
    setPending(true);
    setProblem(null);
    void (async () => {
      try {
        await api.login(name.trim(), password);
        navigate(destination, { replace: true });
      } catch (error) {
        setPending(false);
        setProblem(
          error instanceof ApiError && error.status === 401
            ? "Wrong name or password."
            : describeError(error),
        );
      }
    })();
  };

  return (
    <div className="nd-login">
      <form className="nd-login__card" onSubmit={onSubmit}>
        <h1 className="nd-login__brand">
          nodum
          <span className="nd-login__tagline">knowledge graph</span>
        </h1>
        <p className="nd-meta">
          Sign in with a human account. Accounts and passwords are managed with
          the CLI (<code>nodum human create</code>, <code>nodum human passwd</code>).
        </p>

        <label className="nd-field">
          <span className="nd-label">Name</span>
          <input
            className="nd-input"
            type="text"
            autoComplete="username"
            autoFocus
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
        <label className="nd-field">
          <span className="nd-label">Password</span>
          <input
            className="nd-input"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>

        {problem ? (
          <p className="nd-login__problem" role="alert">
            {problem}
          </p>
        ) : null}

        <button type="submit" className="nd-button nd-button--primary" disabled={pending}>
          {pending ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}

export { LoginView };
